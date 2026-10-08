"""Background loops: price tracking for copies and positions, rug watch, backup polling.

The only price-based message it can send is a rug alarm (liquidity pulled, seen on two
readings at least 20s apart). The only dip message is one STOP_LOSS_PCT warning per coin you hold.
"""
from __future__ import annotations

import asyncio
import statistics
import time

from . import copies, exits, messages
from .db import OUTCOME_POINTS
from .util import is_address, dur, esc, mc, mult, num, usd

PRICE_EVERY = 15
WHALE_BALANCE_CHECK_EVERY = 600
RUG_CONFIRM_SECONDS = 20
RUG_REALERT_SECONDS = 6 * 3600
LIQ_PEAK_WINDOW = 2 * 3600
DEAD_PRICE_FRACTION = 0.03   # a copy whose price fell 97%+ (confirmed) is closed as rugged
GAP_WARN_SECONDS = 600         # loop silent this long = the Mac slept or the bot was off
RECONCILE_EVERY = 900          # compare positions with the wallet's real balances every 15 minutes
PROFILE_EVERY = 1800         # re-profile followed whales (style, copy score) every 30 minutes
OUTCOME_EVERY = 300          # alerts older than 3h are re-priced every 5 minutes until their 24h reading
OUTCOME_WINDOW = 30 * 3600


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
        self.marks = exits.MarkRecorder(db)
        self._protect_pending: dict[str, int] = {}
        self._lab_cache: tuple[float, dict] = (0.0, {})
        self._last_outcome = 0
        self._last_profile = 0
        self._last_reconcile = 0
        self._peak_pending: dict[int, tuple[float, int]] = {}
        self.loops = 0
        self.last_error = ""

    # -- price loop ------------------------------------------------------------------------------
    async def run_prices(self) -> None:
        while True:
            try:
                await self.check_gap()
                await self.tick()
            except Exception as exc:
                self.last_error = f"{time.strftime('%H:%M:%S')} {type(exc).__name__}: {exc}"
            await self._step("quiet update", self.quiet_update)   # runs even if the price step failed
            await asyncio.sleep(PRICE_EVERY)

    async def _step(self, name: str, fn, *args):
        """Run one part of the loop; a failure is recorded but never stops the other parts."""
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            self.last_error = f"{time.strftime('%H:%M:%S')} {name}: {type(exc).__name__}: {exc}"

    async def check_gap(self, now: float | None = None) -> float:
        """The price loop runs every 15s. A much longer pause means the Mac slept (lid closed, battery)
        or the bot was stopped — whale trades in that window can't be alerted in time, so say so."""
        now = now or time.time()
        last = float(self.db.get_meta("loop_heartbeat", "0") or 0)
        self.db.set_meta("loop_heartbeat", int(now))
        gap = now - last if last else 0.0
        if gap >= GAP_WARN_SECONDS:
            self.db.set_meta("last_gap", f"{int(last)}-{int(now)}")
            await self.notify(
                f"💤 <b>The bot was paused for {dur(gap)}</b> ({time.strftime('%H:%M', time.localtime(last))}"
                f" → {time.strftime('%H:%M', time.localtime(now))}).\nYour Mac slept or the bot was stopped, so whale "
                "buys in that window were missed. Keep the lid open and the charger in while it runs.",
                silent=True, kind="GAP")
        return gap

    async def tick(self, now: int | None = None) -> None:
        now = int(now or time.time())
        open_copies = self.db.rows("select * from copies where status='open'")
        positions = self.portfolio.open_positions()
        recent = [r["mint"] for r in self.db.rows(
            "select distinct mint from alerts where kind in ('BUY','HYPE') and ts>=?", (now - 3 * 3600,))]
        closed = self.portfolio.recently_closed()
        watches = self.db.rows("select * from watches where hit_ts=0")
        outcomes = self.db.rows("""select id,ts,mint,price_usd,peak_price,low_price,p1h,p6h,p24h from alerts
            where kind in ('BUY','HYPE') and price_usd>0 and ts>=? and p24h is null""", (now - OUTCOME_WINDOW,))
        older = set()
        if now - self._last_outcome >= OUTCOME_EVERY:
            self._last_outcome = now
            older = {a["mint"] for a in outcomes}
        paper = self.engine.paper.open_trades()
        tracked = self.engine.coins.active()
        mints = ({c["mint"] for c in open_copies} | {p["mint"] for p in positions} | set(recent) | set(closed)
                 | {w["mint"] for w in watches} | older | {t["mint"] for t in paper} | {c["mint"] for c in tracked})
        infos = await self.market.tokens(sorted(mints), max_age=PRICE_EVERY - 2) if mints else {}
        for mint, info in infos.items():
            self.marks.record(mint, num(info.get("price_usd")), now)
        for c in open_copies:
            self._update_copy(c, infos.get(c["mint"]), now)
        for p in positions:
            await self._watch_position(p, infos.get(p["mint"]), now)
        await self._check_watches(watches, infos, now)
        await self._step("outcomes", self._update_outcomes, outcomes, infos, now)
        await self._step("alert reports", self._alert_reports)
        await self._step("paper autopilot", self.engine.paper.tick, infos, now)
        if tracked:
            await self._step("tracked coins", self.engine.coins.tick, infos, now)
        if now - self._last_balance_check >= WHALE_BALANCE_CHECK_EVERY:
            self._last_balance_check = now
            await self._check_whale_balances(now)
        if now - self._last_mute_check >= 600:
            self._last_mute_check = now
            await self._auto_mutes()
        if self.cfg.my_wallets and now - self._last_reconcile >= RECONCILE_EVERY:
            self._last_reconcile = now
            fixed = await self.portfolio.reconcile(self.rpc, self.cfg.my_wallets)
            if fixed:
                await self.notify("🧹 Closed " + ", ".join(f"${esc(s)}" for s in fixed) + " — your wallet no longer holds "
                                  "them (the sale wasn't seen, so no profit/loss is booked for it).", silent=True, kind="INFO")
        if now - self._last_profile >= PROFILE_EVERY:
            self._last_profile = now
            self.engine.profiles.refresh_tracked()
        self.loops += 1

    def _update_copy(self, c: dict, info: dict | None, now: int) -> None:
        fee = self.cfg.get("COPY_FEE_PCT")
        if info and num(info.get("price_usd")) > 0:
            c = copies.update_path(self.db, c, num(info["price_usd"]), now)
            price = num(c.get("last_price"))
            if price > 0 and price <= num(c["entry_price"]) * DEAD_PRICE_FRACTION:
                copies.sell(self.db, c, 1.0, price, fee, "rugged / price collapsed", now)
                return
        max_hold = self.cfg.get("RUNNER_HOLD_HOURS" if c["whale"] == "runner" else "COPY_MAX_HOLD_HOURS") * 3600
        if now - int(c["open_ts"]) >= max_hold and num(c.get("last_price")) > 0:
            copies.sell(self.db, c, 1.0, num(c["last_price"]), fee, "max hold reached", now)

    def _update_outcomes(self, alerts: list[dict], infos: dict, now: int) -> None:
        """Record what each alerted coin did afterwards: its peak, and its price 1h / 6h / 24h later."""
        for a in alerts:
            price = num((infos.get(a["mint"]) or {}).get("price_usd"))
            if price <= 0:
                continue
            updates = {}
            for col, after, grace in OUTCOME_POINTS:
                if a[col] is None and a["ts"] + after <= now <= a["ts"] + after + grace:
                    updates[col] = price
            if now - a["ts"] <= 3600 and (not num(a.get("low_price")) or price < num(a["low_price"])):
                updates["low_price"] = price   # lowest in the first hour: did the stop get hit?
            peak = max(num(a["peak_price"]), num(a["price_usd"]))
            if price > peak and now - a["ts"] <= 86400:
                if price < peak * copies.GLITCH_JUMP:
                    updates["peak_price"], updates["peak_ts"] = price, now
                else:  # a huge jump in one tick has to be seen twice before it counts
                    pending = self._peak_pending.get(a["id"])
                    if pending and 0.8 <= price / pending[0] <= 1.25 and now - pending[1] >= 15:
                        updates["peak_price"], updates["peak_ts"] = price, now
                        self._peak_pending.pop(a["id"], None)
                    else:
                        self._peak_pending[a["id"]] = (price, now)
            if updates:
                self.db.run(f"update alerts set {', '.join(k + '=?' for k in updates)} where id=?",
                            (*updates.values(), a["id"]))

    async def quiet_update(self, now: int | None = None) -> bool:
        """When nothing has been sent for QUIET_UPDATE_MINUTES, say what the bot saw and did instead of staying silent."""
        now = int(now or time.time())
        minutes = self.cfg.get("QUIET_UPDATE_MINUTES")
        if minutes <= 0:
            return False
        window = int(minutes * 60)
        last = max(int(num(self.db.get_meta("quiet_update_ts", "0"))),
                   int(num(self.db.scalar("""select max(ts) from alerts where kind='BUY' and status in ('sent','silent')"""))))
        if not last:
            self.db.set_meta("quiet_update_ts", now)
            return False
        if now - last < window:
            return False
        since = last
        self.db.set_meta("quiet_update_ts", now)
        trades = self.db.row("select count(*) n, sum(side='BUY') buys, count(distinct wallet) w from swaps "
                             "where is_me=0 and seen_ts>=?", (since,)) or {}
        judged = self.db.rows("""select a.status, a.chase_pct, t.symbol, w.name from alerts a left join tokens t on t.mint=a.mint
            left join whales w on w.address=a.wallet where a.kind='BUY' and a.ts>=? order by a.ts desc""", (since,))
        why = {"flipper": "flipper whale", "weak_whale": "losing whale", "chased": "already ran", "dumping": "dumping",
               "micro": "bonding curve", "whale_only": "no real holders", "muted": "muted whale", "late": "seen too late",
               "unsafe": "unsafe token", "paused": "alerts paused"}
        lines = [f"🔍 <b>Last {minutes:g} min</b> — no entry worth sending yet"]
        if trades.get("n"):
            lines.append(f"Saw {trades['n']} trades from {trades['w']} of your whales ({int(num(trades['buys']))} buys).")
        else:
            lines.append(f"Your {self.whales.count()} whales didn't trade.")
        blocked = [j for j in judged if j["status"] in why]
        for j in blocked[:4]:
            extra = f" ({j['chase_pct']:+.0f}% already)" if j["status"] == "chased" else ""
            lines.append(f"• skipped ${esc(j['symbol'] or '?')} from {esc(j['name'] or '?')}: {why[j['status']]}{extra}")
        if getattr(self.rpc, "primary_down", lambda: False)():
            lines.append("⚠️ Helius isn't answering (credits used up?) — running on the public Solana RPC: slower, "
                         "whale scanner paused until Helius is back.")
        last_scan = int(num(self.db.get_meta("scout_last_run", "0")))
        if last_scan:
            nxt = last_scan + int(self.cfg.get("AUTO_SCOUT_HOURS") * 3600) - now
            found = self.db.scalar("select count(*) from whales where active=1 and source='auto'", default=0)
            lines.append(f"Whale scanner: last run {dur(now - last_scan)} ago, next in {dur(max(0, nxt))} · "
                         f"{found} whales found so far.")
            checked = self.db.rows("select status, reason from whale_candidates where analyzed_ts>=?", (last_scan - 60,))
            if checked:
                rejected = [r["reason"] or "" for r in checked if r["status"] == "rejected"]
                kinds = {"flipper": sum("Flipper" in r or "Too fast" in r for r in rejected),
                         "losing": sum("Losing" in r or "lost" in r for r in rejected)}
                kinds["other"] = len(rejected) - kinds["flipper"] - kinds["losing"]
                lines.append(f"Last scan checked {len(checked)} wallets: {len(checked) - len(rejected)} kept · "
                             + " · ".join(f"{n} {k}" for k, n in kinds.items() if n) + " rejected")
        else:
            lines.append("Whale scanner: first run starts a minute after launch.")
        await self.notify("\n".join(lines), kind="QUIET")
        return True

    async def _alert_reports(self) -> int:
        """An hour after each alert, reply to it with what actually happened — the bot grades its own calls."""
        if not self.cfg.flag("ALERT_REPORTS"):
            return 0
        rows = self.db.rows("""select a.*, t.symbol from alerts a left join tokens t on t.mint=a.mint
            where a.kind='BUY' and a.status in ('sent','silent') and a.tg_message_id>0 and a.followed_up=0
            and a.p1h is not null and a.price_usd>0 and a.ts>=?""", (int(time.time()) - 3 * 3600,))
        for a in rows:
            self.db.run("update alerts set followed_up=1 where id=?", (a["id"],))
            entry = num(a["price_usd"])
            now_x, top_x = num(a["p1h"]) / entry, max(num(a["peak_price"]), num(a["p1h"])) / entry
            low_x = num(a["low_price"]) / entry if num(a["low_price"]) else now_x
            stop = 1 - self.cfg.get("STOP_LOSS_PCT") / 100
            if top_x >= 2:
                verdict, icon = "the plan sold half at 2x — a winner", "🟩"
            elif top_x >= 1.3 and now_x >= 1:
                verdict, icon = "up, still running", "🟩"
            elif low_x <= stop:
                verdict, icon = f"hit the -{self.cfg.get('STOP_LOSS_PCT'):.0f}% stop — the plan cut it", "🟥"
            elif now_x < 1:
                verdict, icon = "below the alert price", "🟥"
            else:
                verdict, icon = "flat so far", "⬜️"
            await self.notify(f"{icon} <b>1h report · ${esc(a['symbol'] or a['mint'][:4])}</b>: {mult(now_x)} from the alert "
                              f"(top {mult(top_x)}) — {verdict}.", silent=True, mint=a["mint"], kind="REPORT",
                              reply_to=a["tg_message_id"])
        return len(rows)

    async def _check_whale_balances(self, now: int) -> None:
        """Catch whale exits the stream missed (e.g. tokens moved to another wallet)."""
        for c in self.db.rows("select * from copies where status='open' and open_ts<=?", (now - 600,)):
            if not is_address(c["whale"]):
                continue   # pseudo-wallets ('runner', 'hype') hold nothing on chain
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
        if pos and pos["open"] and pos["priced"]:
            await self._exit_nudges(pos, now)
        self.db.run("""insert into token_watch(mint,liq_peak,liq_peak_ts,liq_peak_price,rug_pending_ts,rug_alert_ts,
            initial_note_ts) values(?,?,?,?,?,?,?) on conflict(mint) do update set liq_peak=excluded.liq_peak,
            liq_peak_ts=excluded.liq_peak_ts,liq_peak_price=excluded.liq_peak_price,
            rug_pending_ts=excluded.rug_pending_ts,rug_alert_ts=excluded.rug_alert_ts,
            initial_note_ts=excluded.initial_note_ts""",
                    (mint, w["liq_peak"], w["liq_peak_ts"], w["liq_peak_price"], w["rug_pending_ts"],
                     w["rug_alert_ts"], w["initial_note_ts"]))

    # -- your market-cap targets (/watch) ------------------------------------------------------------
    async def _check_watches(self, watches: list[dict], infos: dict, now: int) -> None:
        for w in watches:
            info = infos.get(w["mint"]) or {}
            mcap = num(info.get("mc_usd"))
            if mcap <= 0:
                continue
            hit = mcap >= w["target_mc"] if w["direction"] == "up" else mcap <= w["target_mc"]
            if not hit:
                continue
            self.db.run("update watches set hit_ts=? where id=?", (now, w["id"]))
            position = self.portfolio.position(w["mint"], num(info.get("price_usd")))
            holders = [h for h in self.whales.holders_of(w["mint"]) if h["still_in"]]
            move = (mcap / w["base_mc"] - 1) * 100 if w["base_mc"] > 0 else 0.0
            text = (f"<b>🎯 ${esc(info.get('symbol', '?'))} hit your {mc(w['target_mc'])} target</b>\n"
                    f"Now {mc(mcap)} MC ({move:+.0f}% since you set it)."
                    + (f" You hold {usd(position['value'])} ({position['pnl_pct']:+.0f}%)." if position and position["open"] else "")
                    + (f" {len(holders)} tracked whale{'s' if len(holders) != 1 else ''} still in." if holders else "")
                    + f"\n<code>{w['mint']}</code>")
            await self.notify(text, mint=w["mint"], kind="WATCH")

    # -- exit nudges ---------------------------------------------------------------------------------
    def _noted(self, mint: str, kind: str, level: float, since: int) -> bool:
        return bool(self.db.scalar("select 1 from position_notes where mint=? and kind=? and level=? and ts>=?",
                                   (mint, kind, level, since)))

    def _note(self, mint: str, kind: str, level: float, now: int) -> None:
        self.db.run("insert or replace into position_notes(mint,kind,level,ts) values(?,?,?,?)", (mint, kind, level, now))

    def _lab(self) -> dict:
        ts, lab = self._lab_cache
        if time.time() - ts > 1800:
            lab = exits.exit_lab(self.db, self.cfg)
            self._lab_cache = (time.time(), lab)
        return lab

    def _typical_peak(self, holders: list[dict]) -> float:
        wallets = [h["wallet"] for h in holders]
        if not wallets:
            return 0.0
        rows = self.db.rows(f"""select peak_price/entry_price x from copies where entry_price>0 and whale in
            ({','.join('?' * len(wallets))}) order by open_ts desc limit 60""", wallets)
        values = [num(r["x"]) for r in rows if num(r["x"]) > 0]
        return statistics.median(values) if len(values) >= 3 else 0.0

    async def _exit_nudges(self, pos: dict, now: int) -> None:
        """Ladder (2x/3x/5x/10x) and profit protector — each at most once per holding period."""
        mint, since = pos["mint"], int(pos.get("episode_ts") or pos.get("first_ts") or 0)
        holders = self.whales.holders_of(mint)
        c = exits.coach(self.db, self.cfg, pos, holders, now)
        if self.cfg.flag("PROFIT_LADDER"):
            reached = [lv for lv in exits.LADDER_LEVELS if c["multiple"] >= lv and not self._noted(mint, "ladder", lv, since)]
            if reached:
                for lv in reached:
                    self._note(mint, "ladder", lv, now)
                best = self._lab().get("best")
                await self.notify(messages.ladder_note(
                    symbol=pos["symbol"], mint=mint, level=reached[-1], position=pos, whales_in=c["whales_in"],
                    typical_peak=self._typical_peak(holders),
                    best_rule=best["label"] if best and best["n"] >= 5 else "", scalp=c["scalp"]),
                    mint=mint, kind="LADDER")
        stop = self.cfg.get("STOP_LOSS_PCT")
        if stop > 0 and 0 < c["multiple"] <= 1 - stop / 100 and not self._noted(mint, "stop", 0, since):
            first = self._protect_pending.setdefault(mint + ":stop", now)
            if now - first >= RUG_CONFIRM_SECONDS:  # seen on two readings, not a single wick
                self._note(mint, "stop", 0, now)
                self._protect_pending.pop(mint + ":stop", None)
                await self.notify(messages.stop_note(symbol=pos["symbol"], mint=mint, coach=c, position=pos,
                                                     stop_pct=stop), mint=mint, kind="STOP")
        else:
            self._protect_pending.pop(mint + ":stop", None)
        armed = c["protect_after"] > 0 and c["peak_multiple"] >= c["protect_after"]
        if armed and c["from_peak_pct"] >= c["trail"] and not self._noted(mint, "protect", 0, since):
            first = self._protect_pending.setdefault(mint, now)
            if now - first >= RUG_CONFIRM_SECONDS:  # seen on two readings, not a single wick
                self._note(mint, "protect", 0, now)
                self._protect_pending.pop(mint, None)
                await self.notify(messages.protect_note(symbol=pos["symbol"], mint=mint, coach=c, position=pos),
                                  mint=mint, kind="PROTECT")
        else:
            self._protect_pending.pop(mint, None)

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
            try:
                sigs = await self.rpc.signatures(wallet, limit=10, priority=True)   # backup for live trades
            except TypeError:
                sigs = await self.rpc.signatures(wallet, limit=10)
            for s in sigs:
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
