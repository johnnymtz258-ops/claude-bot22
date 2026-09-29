"""Background loops: price tracking for copies and positions, rug watch, backup polling.

The only price-based message it can send is a rug alarm (liquidity pulled, seen on two
readings at least 20s apart). Dips never trigger a sell message.
"""
from __future__ import annotations

import asyncio
import time

from . import copies, messages
from .util import esc, num

PRICE_EVERY = 15
WHALE_BALANCE_CHECK_EVERY = 600
RUG_CONFIRM_SECONDS = 20
RUG_REALERT_SECONDS = 6 * 3600
LIQ_PEAK_WINDOW = 2 * 3600
DEAD_PRICE_FRACTION = 0.03   # a copy whose price fell 97%+ (confirmed) is closed as rugged


class Tracker:
    def __init__(self, cfg, db, rpc, market, whales, portfolio, engine, notify):
        self.cfg = cfg
        self.db = db
        self.rpc = rpc
        self.market = market
        self.whales = whales
        self.portfolio = portfolio
        self.engine = engine
        self.notify = notify
        self._last_balance_check = 0.0
        self._last_mute_check = 0.0
        self.loops = 0
        self.last_error = ""

    # -- price loop ------------------------------------------------------------------------------
    async def run_prices(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:
                self.last_error = f"{time.strftime('%H:%M:%S')} {type(exc).__name__}: {exc}"
            await asyncio.sleep(PRICE_EVERY)

    async def tick(self, now: int | None = None) -> None:
        now = int(now or time.time())
        open_copies = self.db.rows("select * from copies where status='open'")
        positions = self.portfolio.open_positions()
        recent = [r["mint"] for r in self.db.rows(
            "select distinct mint from alerts where kind='BUY' and ts>=?", (now - 3 * 3600,))]
        mints = {c["mint"] for c in open_copies} | {p["mint"] for p in positions} | set(recent)
        infos = await self.market.tokens(sorted(mints), max_age=PRICE_EVERY - 2) if mints else {}
        for c in open_copies:
            self._update_copy(c, infos.get(c["mint"]), now)
        for p in positions:
            await self._watch_position(p, infos.get(p["mint"]), now)
        if now - self._last_balance_check >= WHALE_BALANCE_CHECK_EVERY:
            self._last_balance_check = now
            await self._check_whale_balances(now)
        if now - self._last_mute_check >= 600:
            self._last_mute_check = now
            await self._auto_mutes()
        self.loops += 1

    def _update_copy(self, c: dict, info: dict | None, now: int) -> None:
        fee = self.cfg.get("COPY_FEE_PCT")
        if info and num(info.get("price_usd")) > 0:
            c = copies.update_path(self.db, c, num(info["price_usd"]), now)
            price = num(c.get("last_price"))
            if price > 0 and price <= num(c["entry_price"]) * DEAD_PRICE_FRACTION:
                copies.sell(self.db, c, 1.0, price, fee, "rugged / price collapsed", now)
                return
        max_hold = self.cfg.get("COPY_MAX_HOLD_HOURS") * 3600
        if now - int(c["open_ts"]) >= max_hold and num(c.get("last_price")) > 0:
            copies.sell(self.db, c, 1.0, num(c["last_price"]), fee, "max hold reached", now)

    async def _check_whale_balances(self, now: int) -> None:
        """Catch whale exits the stream missed (e.g. tokens moved to another wallet)."""
        for c in self.db.rows("select * from copies where status='open' and open_ts<=?", (now - 600,)):
            balance = await self.rpc.token_balance(c["whale"], c["mint"])
            if balance is not None and balance <= 0 and num(c.get("last_price")) > 0:
                copies.sell(self.db, c, 1.0, num(c["last_price"]), self.cfg.get("COPY_FEE_PCT"),
                            "whale balance is zero", now)

    async def _auto_mutes(self) -> None:
        for whale, change in self.whales.refresh_auto_mutes():
            s = self.whales.stats(whale["address"])
            name = esc(whale["name"])
            if change == "muted":
                text = (f"🧊 Auto-muted <b>{name}</b>: copying their buys averaged {s['avg']:+.0f}% "
                        f"over {s['n']} trades. Still tracked and scored.\nUse /unmute {name} to hear them again.")
            else:
                text = f"✅ <b>{name}</b> recovered and is un-muted again ({s['avg']:+.0f}% over {s['n']})."
            await self.notify(text, silent=True, kind="INFO")

    # -- your positions ------------------------------------------------------------------------
    def lp_pulled(self, w: dict, liq: float, price: float) -> bool:
        """True when liquidity fell far more than the price move explains (LP removed, not a dip).

        In a normal pool, USD liquidity moves with roughly the square root of the price, so a
        -50% dip alone takes liquidity down only ~30%. Requiring the drop to exceed the configured
        threshold AND to be twice as deep as the price explains keeps ordinary dips from alarming.
        """
        peak, peak_price = num(w.get("liq_peak")), num(w.get("liq_peak_price"))
        if peak <= 0 or liq < 0 or liq >= peak * (1 - self.cfg.get("RUG_LIQUIDITY_DROP_PCT") / 100):
            return False
        if peak_price > 0 and price > 0:
            explained = min(1.0, (price / peak_price) ** 0.5)
            return liq / peak < 0.5 * explained
        return True

    async def _watch_position(self, p: dict, info: dict | None, now: int) -> None:
        if not info:
            return
        mint = p["mint"]
        price = num(info.get("price_usd"))
        w = self.db.row("select * from token_watch where mint=?", (mint,)) or {
            "liq_peak": 0.0, "liq_peak_ts": 0, "liq_peak_price": 0.0, "rug_pending_ts": 0, "rug_alert_ts": 0,
            "initial_note_ts": 0}
        liq = num(info.get("liquidity_usd"), -1)
        if liq >= 0:  # unknown liquidity is ignored, never treated as zero
            if self.lp_pulled(w, liq, price):
                if not w["rug_pending_ts"]:
                    w["rug_pending_ts"] = now
                elif now - int(w["rug_pending_ts"]) >= RUG_CONFIRM_SECONDS and \
                        now - int(w["rug_alert_ts"] or 0) >= RUG_REALERT_SECONDS:
                    await self.notify(messages.rug_alert(symbol=info.get("symbol", "?"), mint=mint,
                                                         liq_before=num(w["liq_peak"]), liq_now=liq,
                                                         position=self.portfolio.position(mint, price)),
                                      mint=mint, kind="RUG")
                    w["rug_alert_ts"] = now
            else:
                w["rug_pending_ts"] = 0
                if now - int(w["liq_peak_ts"] or 0) > LIQ_PEAK_WINDOW or liq >= num(w["liq_peak"]):
                    w["liq_peak"], w["liq_peak_ts"], w["liq_peak_price"] = liq, now, price
        target = self.cfg.get("TAKE_INITIAL_AT_X")
        pos = self.portfolio.position(mint, price)
        if target > 0 and pos and pos["multiple"] >= target and not w["initial_note_ts"]:
            await self.notify(messages.take_initial_note(symbol=pos["symbol"], mint=mint, position=pos,
                                                         multiple=pos["multiple"]), mint=mint, kind="INFO")
            w["initial_note_ts"] = now
        self.db.run("""insert into token_watch(mint,liq_peak,liq_peak_ts,liq_peak_price,rug_pending_ts,rug_alert_ts,
            initial_note_ts) values(?,?,?,?,?,?,?) on conflict(mint) do update set liq_peak=excluded.liq_peak,
            liq_peak_ts=excluded.liq_peak_ts,liq_peak_price=excluded.liq_peak_price,
            rug_pending_ts=excluded.rug_pending_ts,rug_alert_ts=excluded.rug_alert_ts,
            initial_note_ts=excluded.initial_note_ts""",
                    (mint, w["liq_peak"], w["liq_peak_ts"], w["liq_peak_price"], w["rug_pending_ts"],
                     w["rug_alert_ts"], w["initial_note_ts"]))

    # -- backup polling ------------------------------------------------------------------------
    async def run_poller(self) -> None:
        """Re-check each wallet's latest signatures so a dropped WebSocket never loses a trade."""
        every = 45 if self.cfg.uses_helius else 120
        await self.backfill_my_wallets()
        while True:
            await asyncio.sleep(every)
            try:
                await self.poll_once()
            except Exception as exc:
                self.last_error = f"{time.strftime('%H:%M:%S')} poll {type(exc).__name__}: {exc}"

    async def poll_once(self) -> None:
        since = self.engine.started_ts - 120
        for wallet in self.engine.watched_wallets():
            for s in await self.rpc.signatures(wallet, limit=10):
                if s.get("err") is None and int(s.get("blockTime") or 0) >= since:
                    await self.engine.enqueue(wallet, str(s.get("signature")), "poll")

    async def backfill_my_wallets(self, limit: int | None = None) -> None:
        """Rebuild your positions from recent wallet history (trades made while the bot was off).

        Already-processed transactions are skipped, so only the first start reads the full window.
        """
        limit = limit or (600 if self.cfg.uses_helius else 200)
        for attempt in range(3):  # anything that failed (e.g. rate limited) is retried on the next pass
            for wallet in sorted(self.engine.my_wallets):
                for s in await self.rpc.signatures(wallet, limit=limit):
                    if s.get("err") is None:
                        await self.engine.enqueue(wallet, str(s.get("signature")), "backfill")
            await self.engine.queue.join()
            await asyncio.sleep(30)
