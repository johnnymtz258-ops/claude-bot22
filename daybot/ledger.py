"""Your fills -> round trips (first in, first out), with P&L, R and the facts the coach needs.

A round trip opens with the first fill that takes a position from flat, can be added to and trimmed, and closes
when the position is flat again. A fill that flips a position (sell more than you hold) closes one round trip and
opens the other way. An option that expires (or is assigned / exercised) closes at 0.
"""
from __future__ import annotations

from .clock import at, day_of
from .util import dumps, loads, num


def fill_key(f: dict) -> str:
    return f["symbol"] if f.get("kind") == "option" else f.get("underlying") or f["symbol"]


class Ledger:
    def __init__(self, db):
        self.db = db

    def add_fill(self, f: dict) -> int:
        """Store a fill; returns its id, or 0 if it was already stored."""
        return self.db.insert("""insert or ignore into fills(account_id, order_id, ts, day, symbol, underlying, kind,
            option_type, strike, expiry, side, effect, qty, price, multiplier, fee, source)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (f["account_id"], f.get("order_id", ""), int(f["ts"]), day_of(f["ts"]), f["symbol"],
                               f.get("underlying") or f["symbol"], f.get("kind", "stock"), f.get("option_type", ""),
                               num(f.get("strike")), f.get("expiry", ""), f["side"], f.get("effect", ""),
                               num(f["qty"]), num(f["price"]), num(f.get("multiplier"), 1) or 1.0, num(f.get("fee")),
                               f.get("source", "")))

    def open_trade(self, account_id: str, key: str) -> dict | None:
        return self.db.row("select * from trades where account_id=? and key=? and status='open' order by id desc limit 1",
                           (account_id, key))

    def apply(self, fill_id: int) -> list[dict]:
        """Book one stored fill into round trips. Returns events: open / add / reduce / close."""
        f = self.db.row("select * from fills where id=?", (fill_id,))
        if not f:
            return []
        key = fill_key(f)
        events: list[dict] = []
        qty = num(f["qty"])
        cur = self.open_trade(f["account_id"], key)
        if f["side"] == "EXPIRE":
            if cur:
                events.append(self._reduce(cur, f, num(cur["open_qty"]), 0.0))
            return events
        sign = 1 if f["side"] == "BUY" else -1
        if not cur and sign < 0 and f["effect"] != "open":
            # selling what the journal never saw you buy: a holding from before DayBot, not a new short
            return [self._sell_holding(f)]
        if cur:
            cur_sign = 1 if cur["direction"] == "long" else -1
            if cur_sign == sign:
                events.append(self._add(cur, f, qty))
                return events
            closing = min(qty, num(cur["open_qty"]))
            events.append(self._reduce(cur, f, closing, num(f["price"])))
            qty -= closing
            if qty <= 1e-9:
                return events
        events.append(self._open(f, qty, "long" if sign > 0 else "short"))
        return events

    def _open(self, f: dict, qty: float, direction: str) -> dict:
        mult = num(f["multiplier"], 1) or 1.0
        trade_id = self.db.insert("""insert into trades(account_id, key, symbol, underlying, kind, option_type, strike,
            expiry, direction, day, opened_ts, qty, open_qty, entry_avg, multiplier, cost, fees, status)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')""",
                                  (f["account_id"], fill_key(f), f["symbol"], f["underlying"], f["kind"], f["option_type"],
                                   f["strike"], f["expiry"], direction, f["day"], f["ts"], qty, qty, f["price"], mult,
                                   qty * f["price"] * mult, num(f["fee"])))
        return {"type": "open", "trade": self.db.row("select * from trades where id=?", (trade_id,)), "fill": f}

    def _sell_holding(self, f: dict) -> dict:
        mult = num(f["multiplier"], 1) or 1.0
        pos = self.db.row("select avg_price from positions where account_id=? and key=?", (f["account_id"], fill_key(f)))
        avg = num((pos or {}).get("avg_price"))
        qty, px = num(f["qty"]), num(f["price"])
        pnl = (px - avg) * qty * mult - num(f["fee"]) if avg else 0.0
        trade_id = self.db.insert("""insert into trades(account_id, key, symbol, underlying, kind, option_type, strike,
            expiry, direction, day, opened_ts, closed_ts, qty, open_qty, closed_qty, entry_avg, exit_avg, multiplier,
            cost, proceeds, pnl, pnl_pct, fees, status, tags, note)
            values(?,?,?,?,?,?,?,?, 'long', ?,?,?,?,0,?,?,?,?,?,?,?,?,?, 'closed', ?, ?)""",
                                  (f["account_id"], fill_key(f), f["symbol"], f["underlying"], f["kind"], f["option_type"],
                                   f["strike"], f["expiry"], f["day"], f["ts"], f["ts"], qty, qty, avg, px, mult,
                                   avg * qty * mult, px * qty * mult, pnl, (px / avg - 1) * 100 if avg else 0.0,
                                   num(f["fee"]), dumps({"holding": 1, **({} if avg else {"gap": 1})}),
                                   "sold a holding from before DayBot"))
        return {"type": "holding", "trade": self.db.row("select * from trades where id=?", (trade_id,)), "fill": f}

    def _add(self, t: dict, f: dict, qty: float) -> dict:
        mult = num(t["multiplier"], 1) or 1.0
        new_qty = num(t["qty"]) + qty
        entry = (num(t["entry_avg"]) * num(t["qty"]) + num(f["price"]) * qty) / new_qty
        self.db.run("""update trades set qty=?, open_qty=open_qty+?, entry_avg=?, cost=cost+?, fees=fees+? where id=?""",
                    (new_qty, qty, entry, qty * num(f["price"]) * mult, num(f["fee"]), t["id"]))
        return {"type": "add", "trade": self.db.row("select * from trades where id=?", (t["id"],)), "fill": f}

    def _reduce(self, t: dict, f: dict, qty: float, price: float) -> dict:
        mult = num(t["multiplier"], 1) or 1.0
        closed = num(t["closed_qty"]) + qty
        exit_avg = (num(t["exit_avg"]) * num(t["closed_qty"]) + price * qty) / closed if closed else 0.0
        open_qty = max(0.0, num(t["open_qty"]) - qty)
        s = 1 if t["direction"] == "long" else -1
        fees = num(t["fees"]) + num(f["fee"])
        pnl = s * (exit_avg - num(t["entry_avg"])) * closed * mult - fees
        basis = num(t["entry_avg"]) * closed * mult
        pnl_pct = pnl / basis * 100 if basis else 0.0
        status = "closed" if open_qty <= 1e-9 else "open"
        r = pnl / num(t["plan_risk"]) if num(t["plan_risk"]) > 0 and status == "closed" else None
        self.db.run("""update trades set closed_qty=?, exit_avg=?, open_qty=?, proceeds=proceeds+?, fees=?, pnl=?,
                       pnl_pct=?, status=?, closed_ts=?, r_mult=? where id=?""",
                    (closed, exit_avg, open_qty, qty * price * mult, fees, pnl, pnl_pct, status,
                     f["ts"] if status == "closed" else None, r, t["id"]))
        row = self.db.row("select * from trades where id=?", (t["id"],))
        return {"type": "close" if status == "closed" else "reduce", "trade": row, "fill": f}

    def set_plan(self, trade_id: int, plan: dict, check_id: int, setup: str = "", extra_tags: dict | None = None) -> None:
        t = self.db.row("select * from trades where id=?", (trade_id,))
        if not t:
            return
        tags = loads(t["tags"], {}) or {}
        tags.update(extra_tags or {})
        self.db.run("""update trades set plan_stop=?, plan_t1=?, plan_t2=?, plan_risk=?, und_entry=?, check_id=?, setup=?,
                       tags=? where id=?""",
                    (num(plan.get("stop")), num(plan.get("t1")), num(plan.get("t2")), num(plan.get("risk_usd")),
                     num(plan.get("und_price")), check_id, setup or t["setup"] or "", dumps(tags), trade_id))

    def tag(self, trade_id: int, **tags) -> None:
        t = self.db.row("select tags from trades where id=?", (trade_id,))
        cur = loads((t or {}).get("tags"), {}) or {}
        cur.update(tags)
        self.db.run("update trades set tags=? where id=?", (dumps(cur), trade_id))

    # ---- what today looks like -------------------------------------------------------------------------------
    def day_state(self, day: str, account_id: str | None = None) -> dict:
        start = at(day, "00:00")
        acct, params = ("and account_id=?", [account_id]) if account_id else ("", [])
        closed = self.db.rows(f"""select * from trades where status='closed' and closed_ts>=? and closed_ts<? {acct}
                                  order by closed_ts""", [start, start + 86400, *params])
        open_ = self.db.rows(f"select * from trades where status='open' and day=? {acct}", [day, *params])
        streak = 0
        for t in reversed(closed):
            if num(t["pnl"]) < 0:
                streak += 1
            else:
                break
        last_loss = {}
        for t in closed:
            if num(t["pnl"]) < 0:
                last_loss[t["underlying"]] = max(last_loss.get(t["underlying"], 0), int(t["closed_ts"] or 0))
        return {"realized": sum(num(t["pnl"]) for t in closed), "closed": len(closed), "open": len(open_),
                "trades": len(closed) + len(open_), "streak": streak, "last_loss": last_loss,
                "wins": sum(1 for t in closed if num(t["pnl"]) > 0),
                "last_closed_ts": int(closed[-1]["closed_ts"] or 0) if closed else 0}
