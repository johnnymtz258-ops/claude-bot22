"""Tracked coins: follow a coin you hold (or are watching) and get told when to take profits.

Add one with /add COIN (a coin address instead of a wallet), /track COIN, or the dashboard. Its progress is
measured from your entry if you give one ("/track COIN at 850k") or hold it (wallet sync), otherwise from
the price when you started tracking. It gets the same exit plan as alerts, as messages:
  📈 2x / 3x / 5x / 10x — take a slice           🛡 fell 35% from its top after 1.5x — lock in what's left
  ✂️ 40% below your entry — the plan says cut it   🐋 a tracked whale bought or sold it
Coins you hold through wallet sync already get these from your position, so a held coin isn't nudged twice.
"""
from __future__ import annotations

import time

from . import exits
from .util import esc, find_address, mc, mult, num, usd

LADDER = exits.LADDER_LEVELS
CONFIRM_SECONDS = 20


class CoinTracker:
    def __init__(self, db, cfg, market, whales, portfolio, notify):
        self.db = db
        self.cfg = cfg
        self.market = market
        self.whales = whales
        self.portfolio = portfolio
        self.notify = notify
        self._pending: dict[str, int] = {}

    # -- list --------------------------------------------------------------------------------
    def active(self) -> list[dict]:
        return self.db.rows("select * from coins where active=1 order by added_ts")

    def is_tracked(self, mint: str) -> bool:
        return bool(self.db.scalar("select 1 from coins where mint=? and active=1", (mint,)))

    def find(self, text: str) -> dict | None:
        t = str(text or "").strip().lstrip("$")
        address = find_address(t)
        if address:
            return self.db.row("select * from coins where mint=? and active=1", (address,))
        return self.db.row("select * from coins where lower(symbol)=lower(?) and active=1", (t,))

    async def add(self, mint: str, entry_mc: float = 0.0, source: str = "manual") -> tuple[bool, str]:
        info = await self.market.token(mint, max_age=15)
        price, mcap = num(info.get("price_usd")), num(info.get("mc_usd"))
        if price <= 0:
            return False, "No live price for that coin yet (not on DexScreener?). Try again in a minute."
        entry_price = price * entry_mc / mcap if entry_mc > 0 and mcap > 0 else 0.0
        self.db.run("""insert into coins(mint,symbol,added_ts,base_price,base_mc,entry_price,active,source)
            values(?,?,?,?,?,?,1,?) on conflict(mint) do update set active=1, symbol=excluded.symbol,
            added_ts=excluded.added_ts, base_price=excluded.base_price, base_mc=excluded.base_mc,
            entry_price=excluded.entry_price, source=excluded.source""",
                    (mint, info.get("symbol") or mint[:4], int(time.time()), price, mcap, entry_price, source))
        start = f"your entry {mc(entry_mc)}" if entry_price else f"{mc(mcap)} now"
        return True, (f"📍 Tracking ${info.get('symbol') or mint[:4]} from {start}. You'll get take-profit "
                      f"messages at 2x ({mc((entry_mc or mcap) * 2)} MC), 3x, 5x, 10x, if it drops 35% from its top "
                      f"after 1.5x, and at -40%.")

    def remove(self, mint: str) -> bool:
        return bool(self.db.run("update coins set active=0 where mint=?", (mint,)))

    # -- progress ----------------------------------------------------------------------------
    def reference(self, coin: dict) -> tuple[float, str]:
        """(price progress is measured from, label): your position, your stated entry, or tracking start."""
        pos = self.portfolio.position(coin["mint"])
        if pos and pos["open"] and pos["tokens"] > 0 and pos["cost"] > 0:
            return pos["cost"] / pos["tokens"], "your cost"
        if num(coin["entry_price"]) > 0:
            return num(coin["entry_price"]), "your entry"
        return num(coin["base_price"]), "when tracked"

    def status(self, coin: dict, price: float | None = None, now: int | None = None) -> dict:
        now = int(now or time.time())
        info = self.market.cached(coin["mint"])
        price = num(price if price is not None else info.get("price_usd"))
        ref, label = self.reference(coin)
        pseudo = {"mint": coin["mint"], "cost": 1.0, "tokens": 1.0 / ref if ref > 0 else 0.0, "price": price,
                  "episode_ts": coin["added_ts"], "first_ts": coin["added_ts"]}
        holders = self.whales.holders_of(coin["mint"])
        c = exits.coach(self.db, self.cfg, pseudo, holders, now)
        return {**coin, "price": price, "mc_now": num(info.get("mc_usd")), "ref_price": ref, "ref_label": label,
                "multiple": c.get("multiple", 0.0), "peak_multiple": c.get("peak_multiple", 0.0),
                "from_peak_pct": c.get("from_peak_pct", 0.0), "hint": c.get("hint", ""),
                "whales_in": c.get("whales_in", 0), "coach": c,
                "held": self.portfolio.holds(coin["mint"])}

    # -- take-profit messages -------------------------------------------------------------------
    def _noted(self, mint: str, kind: str, level: float, since: int) -> bool:
        return bool(self.db.scalar("select 1 from position_notes where mint=? and kind=? and level=? and ts>=?",
                                   (mint, kind, level, since)))

    def _note(self, mint: str, kind: str, level: float, now: int) -> None:
        self.db.run("insert or replace into position_notes(mint,kind,level,ts) values(?,?,?,?)", (mint, kind, level, now))

    async def tick(self, infos: dict, now: int | None = None) -> list[str]:
        now = int(now or time.time())
        sent = []
        for coin in self.active():
            info = infos.get(coin["mint"])
            price = num((info or {}).get("price_usd"))
            if price <= 0 or self.portfolio.holds(coin["mint"]):
                continue  # held coins get these nudges from your position already
            s = self.status(coin, price, now)
            mint, since, x = coin["mint"], coin["added_ts"], s["multiple"]
            sym = esc(coin["symbol"] or mint[:4])
            reached = [lv for lv in LADDER if x >= lv and not self._noted(mint, "c-ladder", lv, since)]
            if reached:
                for lv in reached:
                    self._note(mint, "c-ladder", lv, now)
                lv = reached[-1]
                whales = f" {s['whales_in']} tracked whale{'s' if s['whales_in'] != 1 else ''} still in." if s["whales_in"] else ""
                await self.notify(f"<b>📈 ${sym} is {mult(x)} ({s['ref_label']})</b>\nNow {mc(s['mc_now'])} MC. "
                                  f"{'Take half — the plan says sell half at 2x.' if lv == 2 else 'Take another slice, let the rest ride.'}"
                                  f"{whales}\n<code>{mint}</code>", mint=mint, kind="COIN")
                sent.append(f"ladder {lv:g}")
            coach = s["coach"]
            armed = coach.get("protect_after", 0) > 0 and s["peak_multiple"] >= coach["protect_after"]
            if armed and s["from_peak_pct"] >= coach.get("trail", 100) and not self._noted(mint, "c-protect", 0, since):
                if self._confirmed(mint + ":p", now):
                    self._note(mint, "c-protect", 0, now)
                    await self.notify(f"<b>🛡 ${sym} gave back {s['from_peak_pct']:.0f}% from its top</b>\n"
                                      f"It reached {mult(s['peak_multiple'])} and is {mult(x)} now ({s['ref_label']}). "
                                      f"The plan says sell what's left.\n<code>{mint}</code>", mint=mint, kind="COIN")
                    sent.append("protect")
            else:
                self._pending.pop(mint + ":p", None)
            stop = self.cfg.get("STOP_LOSS_PCT")
            if stop > 0 and 0 < x <= 1 - stop / 100 and not self._noted(mint, "c-stop", 0, since):
                if self._confirmed(mint + ":s", now):
                    self._note(mint, "c-stop", 0, now)
                    await self.notify(f"<b>✂️ ${sym} is down {100 - x * 100:.0f}% ({s['ref_label']})</b>\n"
                                      f"Now {mc(s['mc_now'])} MC. The plan says cut it at -{stop:.0f}%."
                                      f"\n<code>{mint}</code>", mint=mint, kind="COIN")
                    sent.append("stop")
            else:
                self._pending.pop(mint + ":s", None)
        return sent

    def _confirmed(self, key: str, now: int) -> bool:
        """Seen on two readings at least CONFIRM_SECONDS apart, not a single wick."""
        first = self._pending.setdefault(key, now)
        if now - first >= CONFIRM_SECONDS:
            self._pending.pop(key, None)
            return True
        return False

    def card_lines(self, limit: int = 5) -> list[str]:
        lines = []
        for coin in self.active()[:limit]:
            s = self.status(coin)
            line = f"• <b>${esc(coin['symbol'] or coin['mint'][:4])}</b> {mult(s['multiple'])} ({s['ref_label']})"
            if s["mc_now"]:
                line += f" · {mc(s['mc_now'])} MC"
            if s["peak_multiple"] > s["multiple"] * 1.05:
                line += f" · top {mult(s['peak_multiple'])}"
            if s["hint"]:
                line += f"\n   ↳ {esc(s['hint'])}"
            lines.append(line)
        return lines

    def summary_value(self, coin: dict) -> str:
        pos = self.portfolio.position(coin["mint"])
        return usd(pos["value"]) if pos and pos["open"] else ""
