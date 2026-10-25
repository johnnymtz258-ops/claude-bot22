"""Paper trading: every setup the bot sees is traded on paper with its exit plan.

Two uses of the same trades:
  • the setup gauge — every signal, measured in R, so a setup that loses live gets benched and one that wins live
    gets promoted (backtest.Edge)
  • the autopilot (autopilot.py) — the trades the bot actually alerts you on, sized like a real small account,
    in dollars: "if I had followed DayBot, where would I be?"

Entries: a market entry fills at the next minute's open after the signal; a stop entry (the in-play breakout's
buy-stop) fills in the first minute that trades through the level, at the level (or the open if it gapped through),
and is cancelled at 3:00pm. Costs (SLIPPAGE_PCT) on every fill.
Exits by plan (setups.py): standard — half at 1R, stop to breakeven, the rest at 2R; close — the stop or 3:55pm;
band — a half-hour close back inside the noise band / VWAP, or 3:55pm; eod — sold at 3:59pm. When one minute touches both the stop and a
target, the stop counts (the honest way); a stop entry filled in a minute that touches the stop is stopped out.
"""
from __future__ import annotations

from .bars import C, H, L, O, T, V
from .setups import BAND_EXIT_LAST, BAND_STEP, EXIT_BEFORE_CLOSE, Signal, band_trail, fill_minute_stopped
from .universe import cost_pct, dollar_volume
from .util import dumps, loads, num


def _vwaps(bars: list) -> list[float]:
    out, pv, vol = [], 0.0, 0.0
    for b in bars:
        pv += (b[H] + b[L] + b[C]) / 3 * b[V]
        vol += b[V]
        out.append(pv / vol if vol else b[C])
    return out


class Paper:
    def __init__(self, cfg, db, data, market):
        self.cfg, self.db, self.data, self.market = cfg, db, data, market
        self.auto = None             # the Autopilot, set by the app: sizes and announces its trades

    def open(self, sig: Signal, alert_id: int, day: str, auto: bool = False) -> int:
        """Paper-trade one signal. auto: one the bot alerts you on — the autopilot may take it too."""
        risk_usd = self.cfg.get("PAPER_ACCOUNT") * self.cfg.get("RISK_PER_TRADE_PCT") / 100
        qty = int(risk_usd // sig.risk) if sig.risk > 0 else 0
        if qty <= 0:
            return 0
        plan_data = dumps({k: sig.context[k] for k in ("band", "ref", "open", "dist") if k in sig.context}) \
            if sig.plan == "band" else ""
        pid = self.db.insert("""insert into paper(alert_id, day, symbol, setup, direction, opened_ts, entry, stop, t1, t2,
                                qty, stop_now, status, plan, entry_type, expires, plan_data, risk, cost)
                                values(?,?,?,?,?,?,?,?,?,?,?,?, 'pending',?,?,?,?,?,?)""",
                             (alert_id, day, sig.symbol, sig.setup, sig.direction, sig.ts, sig.entry, sig.stop, sig.t1,
                              sig.t2, qty, sig.stop, sig.plan, sig.entry_type, sig.expires, plan_data, sig.risk,
                              self.cost(sig.symbol)))
        if pid and auto and self.auto:
            self.auto.take(pid, sig)
        return pid

    def cost(self, symbol: str) -> float:
        """What one buy or sell of this stock costs, % (its spread + SLIPPAGE_PCT)."""
        daily = self.data.daily(symbol) if hasattr(self.data, "daily") else []
        return cost_pct(dollar_volume(daily), self.cfg.get("SLIPPAGE_PCT"))

    def symbols(self) -> list[str]:
        return [r["symbol"] for r in self.db.rows("select distinct symbol from paper where status in ('pending','open')")]

    def update(self, now: float) -> list[dict]:
        """Walk open paper trades through the minute bars that arrived.
        Returns what happened: [{"type": entry | half | close | skipped, "trade": the row after it, "price": …}]."""
        events = []
        for p in self.db.rows("select * from paper where status in ('pending','open') order by id"):
            slip = (num(p.get("cost")) or self.cfg.get("SLIPPAGE_PCT")) / 100
            open_ts, close = self.market.open_ts(p["day"]), self.market.close_ts(p["day"])
            session = [b for b in self.data.bars1(p["symbol"]) if open_ts <= b[T] < close]   # its own day only
            bars = [b for b in session if b[T] >= p["opened_ts"]]
            if now > close + 3600 and (p["status"] == "pending" or not bars):
                why = "never triggered" if p["entry_type"] == "stop" and p["status"] == "pending" else "no data"
                self.db.run("update paper set status='skipped', reason=? where id=?", (why, p["id"]))
                continue
            if not bars:
                continue
            s = 1 if p["direction"] == "long" else -1
            plan = p["plan"] or "standard"
            deadline = close - (60 if plan == "eod" else EXIT_BEFORE_CLOSE)
            fill_ts = 0
            if p["status"] == "pending":
                if p["entry_type"] == "stop":
                    until = p["expires"] or deadline
                    hit = next((b for b in bars if b[T] + 60 <= now and b[T] < until
                                and (b[H] if s > 0 else -b[L]) >= num(p["entry"]) * s), None)
                    if hit is None:
                        if now >= until + 60:
                            self.db.run("update paper set status='skipped', reason='never triggered' where id=?",
                                        (p["id"],))
                            events.append({"type": "skipped", "trade": {**p, "status": "skipped"}, "price": 0.0})
                        continue
                    b0 = hit
                    entry = max(b0[O] * s, num(p["entry"]) * s) * s * (1 + s * slip)
                    fill_ts = b0[T]
                else:
                    b0 = bars[0]
                    entry = b0[O] * (1 + s * slip)
                if s * (entry - p["stop"]) <= 0 or b0[T] >= deadline:
                    self.db.run("update paper set status='skipped', reason=? where id=?",
                                ("opened past the stop" if b0[T] < deadline else "too late in the day", p["id"]))
                    events.append({"type": "skipped", "trade": {**p, "status": "skipped"}, "price": entry})
                    continue
                qty = num(p["qty"])
                if p["auto"] and self.auto:
                    qty = self.auto.size_at_fill(p, entry, b0[T])
                risk = num(p["risk"]) if plan in ("band", "eod") else s * (entry - num(p["stop"]))
                self.db.run("update paper set status='open', entry=?, opened_ts=?, qty=?, risk=? where id=?",
                            (entry, b0[T], qty, risk, p["id"]))
                p = self.db.row("select * from paper where id=?", (p["id"],))
                events.append({"type": "entry", "trade": p, "price": entry})
                bars = [b for b in session if b[T] >= p["opened_ts"]]
            entry = num(p["entry"])
            risk = num(p["risk"]) or s * (entry - num(p["stop"]))
            stop_now, half = num(p["stop_now"]), bool(p["half_done"])
            booked = num(p["t1"]) * (1 - s * slip) if half else 0.0
            ctx = loads(p["plan_data"], {}) or {}
            vws = _vwaps(session) if plan == "band" else []
            index = {b[T]: k for k, b in enumerate(session)}
            exit_px, reason, exit_ts = None, "", 0
            seen = int(num(p.get("seen_ts")))
            for b in bars:
                if b[T] <= seen and seen:
                    continue                                        # already walked through this bar
                if b[T] + 60 > now:
                    break                                           # still forming
                seen = b[T]
                if b[T] >= deadline:
                    exit_px, reason, exit_ts = b[O] * (1 - s * slip), ("close" if plan == "eod" else "time"), b[T]
                    break
                lo, hi = (b[L], b[H]) if s > 0 else (-b[H], -b[L])
                if plan not in ("band", "eod") and lo <= stop_now * s and not (
                        b[T] == fill_ts and not fill_minute_stopped(b, s, num(p["entry"]) / (1 + s * slip), stop_now)):
                    op = num(p["entry"]) / (1 + s * slip) * s if b[T] == fill_ts else b[O] * s
                    exit_px = min(op, stop_now * s) * s * (1 - s * slip)
                    reason, exit_ts = ("breakeven" if half else "stop"), b[T]
                    break
                if plan == "standard":
                    if not half and hi >= num(p["t1"]) * s:
                        half, booked, stop_now = True, num(p["t1"]) * (1 - s * slip), entry
                        self.db.run("update paper set half_done=1, stop_now=?, half_ts=? where id=?",
                                    (stop_now, b[T], p["id"]))
                        events.append({"type": "half", "trade": {**p, "half_done": 1, "stop_now": stop_now,
                                                                 "half_ts": b[T]}, "price": booked})
                    if half and hi >= num(p["t2"]) * s:
                        exit_px, reason, exit_ts = num(p["t2"]) * (1 - s * slip), "target", b[T]
                        break
                elif plan == "band" and b[T] > p["opened_ts"]:
                    minutes = int(round((b[T] + 60 - open_ts) / 60))
                    if minutes % BAND_STEP == 0 and minutes <= BAND_EXIT_LAST and b[T] in index:
                        trail = band_trail(ctx, s, minutes, vws[index[b[T]]])
                        if trail is not None and s * b[C] < trail:
                            exit_px, reason, exit_ts = b[C] * (1 - s * slip), "trail", b[T] + 60
                            break
            if exit_px is None and now > deadline + 3600 and bars:
                last = bars[-1]                                     # the data stopped: close at the last price seen
                exit_px, reason, exit_ts = last[C] * (1 - s * slip), "time", last[T]
            if exit_px is None:
                self.db.run("update paper set seen_ts=? where id=?", (seen, p["id"]))
                continue
            move = s * (exit_px - entry)
            r = ((0.5 * s * (booked - entry) + 0.5 * move) if half else move) / risk if risk > 0 else 0.0
            pnl = r * risk * num(p["qty"])
            self.db.run("""update paper set status='closed', closed_ts=?, exit_price=?, r_mult=?, pnl=?, reason=?,
                           seen_ts=? where id=?""", (exit_ts, exit_px, r, pnl, reason, seen, p["id"]))
            events.append({"type": "close", "trade": self.db.row("select * from paper where id=?", (p["id"],)),
                           "price": exit_px})
        return events

    def stats(self, since: int = 0) -> dict:
        """The setup gauge: every signal's paper trade, in R."""
        rows = self.db.rows("select r_mult from paper where status='closed' and closed_ts>=?", (since,))
        rs = [num(r["r_mult"]) for r in rows]
        return {"n": len(rs), "r": sum(rs), "won": (sum(1 for r in rs if r > 0) / len(rs)) if rs else 0.0,
                "avg_r": sum(rs) / len(rs) if rs else 0.0,
                "open": int(self.db.scalar("select count(*) from paper where status in ('open','pending')", default=0))}
