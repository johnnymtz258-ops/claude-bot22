"""Watches your open trades against their plan: stop hit, first and second target, VWAP lost, the close coming,
a same-day option running out of time. Each message is sent once per trade."""
from __future__ import annotations

from .bars import C, T, resample, split_day, vwap
from .clock import day_of
from .setups import BAR
from .util import esc, num, price as fmt_price


def stock_side(t: dict) -> int:
    """+1 when the trade makes money as the stock goes up, -1 when it makes money as it falls."""
    if t["kind"] == "option":
        call = (t["option_type"] or "C").upper().startswith("C")
        return 1 if call == (t["direction"] == "long") else -1
    return 1 if t["direction"] == "long" else -1


def name_of(t: dict) -> str:
    if t["kind"] == "option":
        return f"{t['underlying']} {num(t['strike']):g}{(t['option_type'] or 'C')[0]} {t['expiry'][5:].replace('-', '/')}"
    return t["symbol"]


class Monitor:
    def __init__(self, cfg, db, data, market, notify):
        self.cfg, self.db, self.data, self.market, self.notify = cfg, db, data, market, notify

    def open_trades(self) -> list[dict]:
        return self.db.rows("select * from trades where status='open' order by opened_ts")

    def symbols(self) -> list[str]:
        alerts = {r["symbol"] for r in self.db.rows("select symbol from price_alerts where triggered=0")}
        return sorted({t["underlying"] for t in self.open_trades()} | alerts)

    async def price_alerts(self, now: float) -> int:
        """Your /alert levels: a message the first time the price trades through."""
        n = 0
        for a in self.db.rows("select * from price_alerts where triggered=0"):
            px = self.data.last_price(a["symbol"])
            if not px:
                continue
            hit = px >= a["price"] if a["direction"] == "above" else px <= a["price"]
            if hit:
                self.db.run("update price_alerts set triggered=? where id=?", (int(now), a["id"]))
                arrow = "⬆️" if a["direction"] == "above" else "⬇️"
                await self.notify(f"🔔 {arrow} <b>{esc(a['symbol'])} {fmt_price(px)}</b> — through your "
                                  f"{fmt_price(a['price'])} alert" + (f" ({esc(a['note'])})" if a["note"] else ""),
                                  kind="PRICE", symbol=a["symbol"],
                                  buttons=[[("📈 Chart", None, f"chart:{a['symbol']}"),
                                            ("🧾 Check", None, f"check:{a['symbol']}")]])
                n += 1
        return n

    async def _say(self, key: str, now: float, text: str, loud: bool, symbol: str) -> None:
        if self.db.once(key, int(now)):
            await self.notify(text, silent=not loud, kind="MONITOR", symbol=symbol,
                              buttons=[[("📈 Chart", None, f"chart:{symbol}")]])

    async def tick(self, now: float) -> None:
        if self.market.session(now) in {"pre", "open", "post"}:
            await self.price_alerts(now)
        if not self.cfg.flag("MONITOR_POSITIONS"):
            return
        session = self.market.session(now)
        day = day_of(now)
        for t in self.open_trades():
            und = t["underlying"]
            px = self.data.last_price(und)
            what = esc(name_of(t))
            key = f"mon:{t['id']}:"
            same_day = t["day"] == day
            if session == "open" and px > 0 and num(t["plan_stop"]):
                s = stock_side(t)
                stop, t1, t2 = num(t["plan_stop"]), num(t["plan_t1"]), num(t["plan_t2"])
                if s * (px - stop) <= 0:
                    await self._say(key + "stop", now, f"⚠️ <b>{what}: your stop {fmt_price(stop)} is hit</b> "
                                                       f"({esc(und)} {fmt_price(px)}). The plan says out — the next "
                                                       "trade is always there.", True, und)
                elif t1 and s * (px - t1) >= 0 and not num(t["closed_qty"]):
                    await self._say(key + "t1", now, f"🎯 {what} reached the first target {fmt_price(t1)} "
                                                     f"({esc(und)} {fmt_price(px)}): take half, move the stop to "
                                                     "break-even.", True, und)
                if t2 and s * (px - t2) >= 0:
                    await self._say(key + "t2", now, f"🎯 {what} reached the second target {fmt_price(t2)} — "
                                                     "take the rest or trail it tight.", True, und)
                _, rth, _ = split_day(self.data.bars1(und), day, self.market)
                bars5 = [b for b in resample(rth, 5) if b[T] + BAR <= now]
                if len(bars5) >= 2 and same_day:
                    vw = vwap(bars5)
                    entered_right = s * (num(t["und_entry"]) - vw[0]) >= 0 if num(t["und_entry"]) else True
                    if entered_right and s * (bars5[-1][C] - vw[-1]) < 0 and s * (bars5[-2][C] - vw[-2]) >= 0:
                        await self._say(key + "vwap", now, f"🟡 {esc(und)} just closed a 5-min bar on the wrong side of "
                                                           f"VWAP {fmt_price(vw[-1])} — your {what} trade is weaker; "
                                                           "tighten the stop or take some off.", False, und)
            if session == "open" and same_day:
                left = self.market.minutes_left(now)
                if left <= 15:
                    await self._say(key + "close", now, f"⏰ 15 minutes to the close and you still hold {what}. A day "
                                                        "trade is flat by 3:55 — or decide on purpose to hold it "
                                                        "overnight.", True, und)
                if t["kind"] == "option" and t["expiry"] == day and left <= 30:
                    await self._say(key + "0dte", now, f"⏰ {what} expires today — {int(left)} minutes left. Out of the "
                                                       "money at the close means worthless.", True, und)
