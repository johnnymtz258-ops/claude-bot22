"""Paper autopilot: every setup alert is traded on paper with the standard plan, as a gauge of what's working in
the market right now (and as the forward test that can bench a setup the backtest liked)."""
from __future__ import annotations

from .bars import H, L, O, T
from .setups import EXIT_BEFORE_CLOSE, Signal
from .util import num


class Paper:
    def __init__(self, cfg, db, data, market):
        self.cfg, self.db, self.data, self.market = cfg, db, data, market

    def balance(self) -> float:
        start = self.cfg.get("PAPER_ACCOUNT")
        return start + num(self.db.scalar("select coalesce(sum(pnl),0) from paper where status='closed'"))

    def open(self, sig: Signal, alert_id: int, day: str) -> int:
        risk_usd = self.balance() * self.cfg.get("RISK_PER_TRADE_PCT") / 100
        qty = int(risk_usd // sig.risk) if sig.risk > 0 else 0
        if qty <= 0:
            return 0
        return self.db.insert("""insert into paper(alert_id, day, symbol, setup, direction, opened_ts, entry, stop, t1, t2,
                                 qty, stop_now, status) values(?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
                              (alert_id, day, sig.symbol, sig.setup, sig.direction, sig.ts, sig.entry, sig.stop, sig.t1,
                               sig.t2, qty, sig.stop))

    def symbols(self) -> list[str]:
        return [r["symbol"] for r in self.db.rows("select distinct symbol from paper where status in ('pending','open')")]

    def update(self, now: float) -> list[dict]:
        """Walk open paper trades through the minute bars that arrived. Returns trades that closed."""
        closed = []
        slip = self.cfg.get("SLIPPAGE_PCT") / 100
        for p in self.db.rows("select * from paper where status in ('pending','open')"):
            bars = [b for b in self.data.bars1(p["symbol"]) if b[T] >= p["opened_ts"]]
            if now > self.market.close_ts(p["day"]) + 3600 and p["status"] in ("pending", "open"):
                if p["status"] == "pending" or not bars:
                    self.db.run("update paper set status='skipped', reason='no data' where id=?", (p["id"],))
                    continue
            if not bars:
                continue
            s = 1 if p["direction"] == "long" else -1
            deadline = self.market.close_ts(p["day"]) - EXIT_BEFORE_CLOSE
            if p["status"] == "pending":
                b0 = bars[0]
                entry = b0[O] * (1 + s * slip)
                if s * (entry - p["stop"]) <= 0:
                    self.db.run("update paper set status='skipped', reason='opened past the stop' where id=?", (p["id"],))
                    continue
                self.db.run("update paper set status='open', entry=?, opened_ts=? where id=?", (entry, b0[T], p["id"]))
                p = {**p, "entry": entry, "status": "open", "opened_ts": b0[T]}
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
                    self.db.run("update paper set half_done=1, stop_now=? where id=?", (stop_now, p["id"]))
                if half and hi >= num(p["t2"]) * s:
                    exit_px, reason, exit_ts = num(p["t2"]) * (1 - s * slip), "target", b[T]
                    break
            if exit_px is None and now > deadline + 3600 and bars:
                last = bars[-1]                                     # the data stopped: close at the last price seen
                exit_px, reason, exit_ts = last[4] * (1 - s * slip), "time", last[T]
            if exit_px is None:
                self.db.run("update paper set seen_ts=? where id=?", (seen, p["id"]))
                continue
            move = s * (exit_px - entry)
            r = ((0.5 * s * (booked - entry) + 0.5 * move) if half else move) / risk if risk > 0 else 0.0
            pnl = r * risk * num(p["qty"])
            self.db.run("""update paper set status='closed', closed_ts=?, exit_price=?, r_mult=?, pnl=?, reason=?
                           where id=?""", (exit_ts, exit_px, r, pnl, reason, p["id"]))
            closed.append({**p, "r_mult": r, "pnl": pnl, "reason": reason})
        return closed

    def stats(self, since: int = 0) -> dict:
        rows = self.db.rows("select * from paper where status='closed' and closed_ts>=?", (since,))
        rs = [num(r["r_mult"]) for r in rows]
        return {"n": len(rs), "r": sum(rs), "won": (sum(1 for r in rs if r > 0) / len(rs)) if rs else 0.0,
                "pnl": sum(num(r["pnl"]) for r in rows), "balance": self.balance(),
                "open": int(self.db.scalar("select count(*) from paper where status in ('open','pending')", default=0))}
