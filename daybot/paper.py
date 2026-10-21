"""Paper trading: every setup the bot sees is traded on paper with the standard plan.

Two uses of the same trades:
  • the setup gauge — every signal, measured in R, so a setup that loses live gets benched and one that wins live
    gets promoted (backtest.Edge)
  • the autopilot (autopilot.py) — the trades the bot actually alerts you on, sized like a real small account,
    in dollars: "if I had followed DayBot, where would I be?"

The plan: enter at the next minute's open after the signal (plus costs), half off at 1R and the stop to breakeven,
the rest at 2R, out by 3:55pm. When one bar touches both the stop and a target, the stop counts (the honest way).
"""
from __future__ import annotations

from .bars import C, H, L, O, T
from .setups import EXIT_BEFORE_CLOSE, Signal
from .util import num


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
        pid = self.db.insert("""insert into paper(alert_id, day, symbol, setup, direction, opened_ts, entry, stop, t1, t2,
                                qty, stop_now, status) values(?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
                             (alert_id, day, sig.symbol, sig.setup, sig.direction, sig.ts, sig.entry, sig.stop, sig.t1,
                              sig.t2, qty, sig.stop))
        if pid and auto and self.auto:
            self.auto.take(pid, sig)
        return pid

    def symbols(self) -> list[str]:
        return [r["symbol"] for r in self.db.rows("select distinct symbol from paper where status in ('pending','open')")]

    def update(self, now: float) -> list[dict]:
        """Walk open paper trades through the minute bars that arrived.
        Returns what happened: [{"type": entry | half | close | skipped, "trade": the row after it, "price": …}]."""
        events = []
        slip = self.cfg.get("SLIPPAGE_PCT") / 100
        for p in self.db.rows("select * from paper where status in ('pending','open') order by id"):
            close = self.market.close_ts(p["day"])
            bars = [b for b in self.data.bars1(p["symbol"]) if p["opened_ts"] <= b[T] < close]   # its own day only
            if now > self.market.close_ts(p["day"]) + 3600 and (p["status"] == "pending" or not bars):
                self.db.run("update paper set status='skipped', reason='no data' where id=?", (p["id"],))
                continue
            if not bars:
                continue
            s = 1 if p["direction"] == "long" else -1
            deadline = self.market.close_ts(p["day"]) - EXIT_BEFORE_CLOSE
            if p["status"] == "pending":
                b0 = bars[0]
                entry = b0[O] * (1 + s * slip)
                if s * (entry - p["stop"]) <= 0 or b0[T] >= deadline:
                    self.db.run("update paper set status='skipped', reason=? where id=?",
                                ("opened past the stop" if b0[T] < deadline else "too late in the day", p["id"]))
                    events.append({"type": "skipped", "trade": {**p, "status": "skipped"}, "price": entry})
                    continue
                qty = num(p["qty"])
                if p["auto"] and self.auto:
                    qty = self.auto.size_at_fill(p, entry)
                self.db.run("update paper set status='open', entry=?, opened_ts=?, qty=? where id=?",
                            (entry, b0[T], qty, p["id"]))
                p = self.db.row("select * from paper where id=?", (p["id"],))
                events.append({"type": "entry", "trade": p, "price": entry})
            entry = num(p["entry"])
            risk = s * (entry - num(p["stop"]))
            stop_now, half = num(p["stop_now"]), bool(p["half_done"])
            booked = num(p["t1"]) * (1 - s * slip) if half else 0.0
            exit_px, reason, exit_ts = None, "", 0
            seen = int(num(p.get("seen_ts")))
            for b in bars:
                if b[T] <= seen and seen:
                    continue                                        # already walked through this bar
                if b[T] + 60 > now:
                    break                                           # still forming
                seen = b[T]
                if b[T] >= deadline:
                    exit_px, reason, exit_ts = b[O] * (1 - s * slip), "time", b[T]
                    break
                lo, hi = (b[L], b[H]) if s > 0 else (-b[H], -b[L])
                if lo <= stop_now * s:
                    exit_px = min(b[O] * s, stop_now * s) * s * (1 - s * slip)
                    reason, exit_ts = ("breakeven" if half else "stop"), b[T]
                    break
                if not half and hi >= num(p["t1"]) * s:
                    half, booked, stop_now = True, num(p["t1"]) * (1 - s * slip), entry
                    self.db.run("update paper set half_done=1, stop_now=?, half_ts=? where id=?",
                                (stop_now, b[T], p["id"]))
                    events.append({"type": "half", "trade": {**p, "half_done": 1, "stop_now": stop_now,
                                                             "half_ts": b[T]}, "price": booked})
                if half and hi >= num(p["t2"]) * s:
                    exit_px, reason, exit_ts = num(p["t2"]) * (1 - s * slip), "target", b[T]
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
