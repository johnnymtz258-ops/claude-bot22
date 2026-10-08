"""Paper autopilot: trades a pretend balance on every alert the bot sends, mechanically, so you can see
whether the system makes money before risking any.

Entry   PAPER_TRADE_USD at the alert's confirmed price plus PAPER_SLIPPAGE_PCT (what a real swap
        a few seconds later costs you), at most PAPER_MAX_OPEN coins at once.
Exits   the same plan the alerts give you:
          half at 2x · the rest if it falls PROTECT_TRAIL_PCT from its top after 1.5x · STOP_LOSS_PCT
          below entry · when the whale has sold half its bag · after 24h.
        Every sell also pays PAPER_SLIPPAGE_PCT.
A price that jumps 8x+ in one reading must be seen twice before it counts (DexScreener glitches).
"""
from __future__ import annotations

import time

from . import playbook
from .util import esc, mult, num, usd

GLITCH_JUMP = 8.0
MAX_HOLD = 24 * 3600
EQUITY_EVERY = 300


class PaperTrader:
    def __init__(self, db, cfg, notify):
        self.db = db
        self.cfg = cfg
        self.notify = notify
        self._pending: dict[int, tuple[float, int]] = {}
        self._last_equity = 0
        self.live = None   # LiveTrader: makes these trades for real when LIVE_TRADING is on

    # -- book keeping ---------------------------------------------------------------------------
    def start_balance(self) -> float:
        saved = self.db.get_meta("paper_start")
        if not saved:
            saved = str(self.cfg.get("PAPER_START_USD"))
            self.db.set_meta("paper_start", saved)
            self.db.set_meta("paper_start_ts", int(time.time()))
        return num(saved)

    def cash(self) -> float:
        realized = num(self.db.scalar("select coalesce(sum(proceeds_usd - size_usd),0) from paper_trades "
                                      "where status='closed'"))
        open_cost = num(self.db.scalar("select coalesce(sum(size_usd - proceeds_usd),0) from paper_trades "
                                       "where status='open'"))
        return self.start_balance() + realized - open_cost

    def open_trades(self) -> list[dict]:
        return self.db.rows("select * from paper_trades where status='open' order by open_ts")

    def summary(self, prices: dict | None = None) -> dict:
        prices = prices or {}
        open_value = 0.0
        opens = []
        for t in self.open_trades():
            price = num(prices.get(t["mint"])) or num(t["last_price"])
            value = t["remaining"] * t["tokens"] * price * (1 - self._slip())
            open_value += value
            opens.append({**t, "value": value, "multiple": price / t["entry_price"] if t["entry_price"] else 0.0})
        closed = self.db.rows("select * from paper_trades where status='closed'")
        pnl = [c["proceeds_usd"] - c["size_usd"] for c in closed]
        equity = self.cash() + open_value
        start = self.start_balance()
        return {"start": start, "equity": equity, "cash": self.cash(), "return_pct": (equity / start - 1) * 100 if start else 0,
                "open": opens, "closed": len(closed), "won": sum(p > 0 for p in pnl), "realized": sum(pnl),
                "best": max(pnl) if pnl else 0.0, "worst": min(pnl) if pnl else 0.0,
                "since_ts": int(num(self.db.get_meta("paper_start_ts", "0")))}

    def _slip(self) -> float:
        return self.cfg.get("PAPER_SLIPPAGE_PCT") / 100

    # -- trading --------------------------------------------------------------------------------
    def open(self, *, alert_id: int, mint: str, whale: str, symbol: str, price: float, mc_usd: float,
             ts: int | None = None) -> int:
        if not (self.cfg.flag("PAPER_TRADING") or self.cfg.flag("LIVE_TRADING")) or price <= 0:
            return 0
        if self.db.scalar("select 1 from paper_trades where mint=? and status='open'", (mint,)):
            return 0
        if len(self.open_trades()) >= int(self.cfg.get("PAPER_MAX_OPEN")):
            return 0
        size = min(self.cfg.get("PAPER_TRADE_USD"), self.cash())
        if size < 1:
            return 0
        entry = price * (1 + self._slip())
        trade_id = self.db.insert("""insert into paper_trades(alert_id,mint,whale,symbol,open_ts,entry_price,entry_mc,
                size_usd,tokens,remaining,proceeds_usd,peak_x,last_price,status) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                  (alert_id, mint, whale, symbol, int(ts or time.time()), entry, mc_usd, size,
                                   size / entry, 1.0, 0.0, 1.0, price, "open"))
        if trade_id and self.live and self.live.ready()[0]:
            self.live.spawn(self.live.buy(trade_id, mint, symbol))
        return trade_id

    def _accept(self, t: dict, price: float, now: int) -> float | None:
        last = num(t["last_price"])
        if price <= 0:
            return None
        if last > 0 and (price / last >= GLITCH_JUMP or last / price >= GLITCH_JUMP):
            pending = self._pending.get(t["id"])
            if pending and 0.8 <= price / pending[0] <= 1.25 and now - pending[1] >= 15:
                self._pending.pop(t["id"], None)
                return price
            self._pending[t["id"]] = (price, now)
            return None
        self._pending.pop(t["id"], None)
        return price

    def _whale_half_out(self, whale: str, mint: str, since: int) -> bool:
        peak = num(self.db.scalar("select max(max(holding_after), max(pre_holding)) from swaps where wallet=? and mint=?",
                                  (whale, mint)))
        last = self.db.row("select holding_after from swaps where wallet=? and mint=? order by ts desc, id desc limit 1",
                           (whale, mint))
        return bool(peak > 0 and last and num(last["holding_after"]) <= 0.5 * peak
                    and self.db.scalar("select 1 from swaps where wallet=? and mint=? and side='SELL' and ts>=?",
                                       (whale, mint, since - 120)))

    async def tick(self, infos: dict, now: int | None = None) -> list[tuple[dict, str, float]]:
        """Apply the exit plan to every open paper trade. Returns [(trade, what, usd)] for what happened."""
        now = int(now or time.time())
        done = []
        trail, stop = self.cfg.get("PROTECT_TRAIL_PCT"), self.cfg.get("STOP_LOSS_PCT")
        for t in self.open_trades():
            price = self._accept(t, num((infos.get(t["mint"]) or {}).get("price_usd")), now)
            if price is None:
                if now - t["open_ts"] >= MAX_HOLD and num(t["last_price"]) > 0:
                    done.append(self._sell(t, 1.0, num(t["last_price"]), "24h max hold", now))
                continue
            x = price / t["entry_price"]
            peak = max(num(t["peak_x"]), x)
            self.db.run("update paper_trades set last_price=?, peak_x=? where id=?", (price, peak, t["id"]))
            t.update(last_price=price, peak_x=peak)
            if t["whale"] == "hype":
                from .hype import exit_plan
                plan = exit_plan(self.cfg)                       # hype coins: take the first move
            else:
                plan = playbook.plan_for(self.db, t["whale"])   # this whale's own winning exit plan, if it has one
            if plan and not plan["half"] and x >= plan["tp"]:
                why = "hype take-profit" if t["whale"] == "hype" else "whale's playbook"
                done.append(self._sell(t, 1.0, price, f"all out at {plan['tp']:g}x ({why})", now))
                continue
            if not t["half_taken"] and x >= 2:
                done.append(self._sell(t, 0.5, price, "half at 2x", now))
                t = self.db.row("select * from paper_trades where id=?", (t["id"],))
            stop_pct = (1 - plan["stop"]) * 100 if plan else stop
            reason = ""
            if stop_pct > 0 and x <= 1 - stop_pct / 100:
                reason = f"stop -{stop_pct:.0f}%"
            elif peak >= 1.5 and x <= peak * (1 - trail / 100):
                reason = f"fell {trail:.0f}% from its {peak:.1f}x top"
            elif plan and now - t["open_ts"] >= plan["hold"]:
                reason = (f"{plan['hold'] // 60} min limit (hype exit)" if t["whale"] == "hype"
                          else f"{plan['hold'] // 3600}h max hold (whale's playbook)")
            elif (not plan or plan["follow_whale"]) and self._whale_half_out(t["whale"], t["mint"], t["open_ts"]):
                reason = "whale sold half its bag"
            elif now - t["open_ts"] >= MAX_HOLD:
                reason = "24h max hold"
            if reason:
                done.append(self._sell(t, 1.0, price, reason, now))
        self._record_equity(infos, now)
        for trade, what, amount in done:
            if self.cfg.flag("PAPER_NOTIFY"):
                await self.notify(self._note(trade, what, amount), mint=trade["mint"], silent=True, kind="PAPER")
        return done

    def _sell(self, t: dict, share_of_remaining: float, price: float, reason: str, now: int) -> tuple[dict, str, float]:
        if self.live:
            self.live.spawn(self.live.sell(t["mint"], share_of_remaining, reason))
        part = t["remaining"] * share_of_remaining
        got = part * t["tokens"] * price * (1 - self._slip())
        remaining = t["remaining"] - part
        proceeds = num(t["proceeds_usd"]) + got
        closed = remaining <= 1e-9
        self.db.run("""update paper_trades set remaining=?, proceeds_usd=?, half_taken=case when ?='half at 2x'
                then 1 else half_taken end, status=?, close_ts=?, close_reason=? where id=?""",
                    (0.0 if closed else remaining, proceeds, reason, "closed" if closed else "open",
                     now if closed else None, reason if closed else t.get("close_reason"), t["id"]))
        trade = self.db.row("select * from paper_trades where id=?", (t["id"],))
        return trade, reason, got

    async def close(self, trade_id: int, reason: str = "closed by you") -> dict | None:
        """Close a paper trade now at its last price (dashboard button). Live mirrors it like any paper sell."""
        t = self.db.row("select * from paper_trades where id=? and status='open'", (trade_id,))
        if not t or num(t["last_price"]) <= 0:
            return None
        trade, what, got = self._sell(t, 1.0, num(t["last_price"]), reason, int(time.time()))
        return trade

    def _record_equity(self, infos: dict, now: int) -> None:
        if now - self._last_equity < EQUITY_EVERY:
            return
        self._last_equity = now
        # open trades are valued at their last ACCEPTED price, so a glitch the filter rejected can't spike the curve
        self.db.run("insert or replace into paper_equity(ts,equity) values(?,?)", (now, self.summary()["equity"]))

    def _note(self, t: dict, what: str, got: float) -> str:
        x = num(t["last_price"]) / t["entry_price"] if t["entry_price"] else 0
        if t["status"] == "closed":
            pnl = t["proceeds_usd"] - t["size_usd"]
            head = f"{'🟩' if pnl >= 0 else '🟥'} Paper: closed ${esc(t['symbol'])} {usd(pnl, signed=True)}"
        else:
            head = f"💰 Paper: sold half of ${esc(t['symbol'])} at {mult(x)}"
        return (f"<b>{head}</b>\n{esc(what)} · got {usd(got)} · balance {usd(self.summary()['equity'])}"
                f"\n<code>{t['mint']}</code>")
