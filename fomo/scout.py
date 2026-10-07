"""Whale scanner: finds wallets that were early in today's runners and keeps the ones YOU can copy.

Every AUTO_SCOUT_HOURS:
  1. Pick up to 3 coins that just ran (today's biggest runners, and coins your alerts caught that went 3x+).
  2. Find each coin's early buyers (same method as /find), skipping snipers, creators and bots.
  3. Read each candidate's recent trades and replay them as a copier who buys COPY_DELAY_SECONDS late
     (GeckoTerminal minute candles), with the exit plan. A wallet qualifies only if it is a 🟢 holder or
     🔵 swing trader (not a 🟠 flipper that dumps within minutes), is profitable itself, and copying it at
     your speed made money: avg ≥ 1.10x over 4+ coins, 35%+ won.
  4. AUTO_WHALES on: strong ones are followed automatically (you're told, with an Unfollow button).
     Off, or no candle data: they're sent to you as picks with ➕ Follow buttons.
Every day it re-checks the whales it added (never the ones you added) and drops any that turned into a
flipper, stopped being copyable, went COLD, or stopped trading. Whales you added that turn out to be
flippers or losers are muted for entries automatically, and you get one message saying why.
"""
from __future__ import annotations

import asyncio
import json
import time

from . import profiles
from .discovery import Discovery
from .util import num, esc, mc, pct, short

MIN_TRIPS = 5
MIN_PNL_SOL = 0.5
MIN_WIN_RATE = 0.40
MAX_IDLE_DAYS = 4
RUN_CALL_BUDGET = 2500       # Helius calls one scanner run may use; live whale tracking always has the rest
MIN_TYPICAL_BUY_USD = 300    # a wallet that usually buys less can't pass the alert quality gate
TRIAL_MIN_COPIES = 4
TRIAL_DROP_AVG = -25.0       # a new whale whose first copies average worse than this is dropped
PRUNE_EVERY = 2 * 3600
RECHECK_AFTER_DAYS = 3
CANDIDATE_RETRY_DAYS = 3
MAX_CHECKS_PER_RUN = 15      # each wallet replay costs ~30 GeckoTerminal calls (free limit: 30 a minute)
SMALL_BUDGET = {"pages": 30, "samples": 40, "dense": 160, "holders": 15, "wallet_txs": 200}


COPY_MIN_COINS = 4
COPY_MIN_AVG = 1.10
COPY_MIN_WIN = 0.35


def copyable(prof: dict | None) -> tuple[str, str]:
    """('strong'|'unscored'|'no', reason) for a candidate's profile."""
    if not prof:
        return "unscored", "no price history to replay"
    pb = prof.get("playbook") or {}
    if pb.get("ok"):   # its replayed buys made money with one of the exit plans, whatever its style
        return "strong", (f"📘 {pb['label'].split(',')[0]}: {pb['avg']:.2f}x avg over {pb['n']} replayed buys, "
                          f"{pb['won'] * 100:.0f}% won")
    if prof["style"] in ("FLIPPER", "BOT"):
        return "no", profiles.verdict(prof)[1]
    if prof.get("copy_n", 0) < COPY_MIN_COINS:
        return "unscored", f"{profiles.STYLE_LABEL[prof['style']]}, only {prof.get('copy_n', 0)} coins with price history"
    if prof["copy_avg"] < COPY_MIN_AVG or prof["copy_win"] < COPY_MIN_WIN:
        return "no", f"copying at your speed: {prof['copy_avg']:.2f}x avg, {prof['copy_win'] * 100:.0f}% won"
    return "strong", profiles.describe(prof)


def qualifies(r: dict, now: float | None = None) -> tuple[bool, str]:
    """Is this wallet profitable right now? Two ways in:
      an active trader — 5+ closed trades, +0.5 SOL or more, 40%+ won; or
      a conviction holder — few trades but big wins: 2+ closed, +2 SOL or more, half or more won
      (the most profitable wallets your scanner found were exactly these, and the old rules threw them out)."""
    now = now or time.time()
    if not r.get("ok"):
        return False, r.get("error", "couldn't read history")
    if r["verdict"].startswith(("🤖", "❌")):
        return False, r["verdict"]
    if now - r.get("last_trade_ts", 0) > 3 * 86400:
        return False, "no trades in the last 3 days"
    trader = r["trips"] >= MIN_TRIPS and r["pnl_sol"] >= MIN_PNL_SOL and r["win_rate"] >= MIN_WIN_RATE
    holder = r["trips"] >= 2 and r["pnl_sol"] >= 2.0 and r["win_rate"] >= 0.5
    if not (trader or holder):
        return False, f"{r['pnl_sol']:+.2f} SOL over {r['trips']} closed trades, {r['win_rate'] * 100:.0f}% won"
    sizes = sorted(t.get("bought_usd", 0) for t in r.get("trip_list") or [] if t.get("bought_usd"))
    if sizes and sizes[len(sizes) // 2] < MIN_TYPICAL_BUY_USD:
        return False, f"buys too small to copy (typical ${sizes[len(sizes) // 2]:,.0f})"
    kind = "conviction holder" if holder and not trader else "trader"
    return True, f"{kind}: {r['pnl_sol']:+.1f} SOL over {r['trips']} trades, {r['win_rate'] * 100:.0f}% won"


class WhaleScout:
    def __init__(self, cfg, db, rpc, market, whales, runners, notify, refresh_wallets, profiler=None):
        self.cfg = cfg
        self.db = db
        self.whales = whales
        self.runners = runners
        self.notify = notify
        self.refresh_wallets = refresh_wallets
        self.discovery = Discovery(rpc, market, db, cfg)
        self.discovery.budget = dict(SMALL_BUDGET if cfg.uses_helius else
                                     {"pages": 10, "samples": 30, "dense": 120, "holders": 6, "wallet_txs": 80})
        self.profiler = profiler or profiles.Profiler(db, cfg, market, self.discovery)
        if self.profiler.discovery is None:
            self.profiler.discovery = self.discovery
        self.last_run = int(db.get_meta("scout_last_run", "0") or 0)
        if db.get_meta("candidates_reset_v12") != "1":
            # wallets rejected only for having few trades get another look under the conviction-holder rule
            db.run("delete from whale_candidates where status='rejected' and (reason like '%closed trades%' "
                   "or reason like '%Not enough%' or verdict like '%Not enough%')")
            db.set_meta("candidates_reset_v12", "1")
            self.last_run = 0
        self.last_error = ""
        self.running = False

    async def run(self) -> None:
        await asyncio.sleep(60)   # let the bot settle (wallet backfill etc.) first
        while True:
            try:
                active = self.cfg.flag("WHALE_PICKS") or self.cfg.flag("AUTO_WHALES")
                if active and time.time() - self.last_run >= self.cfg.get("AUTO_SCOUT_HOURS") * 3600:
                    await self.scout()
                if self.cfg.flag("AUTO_WHALES"):
                    await self.promote_picks()
                    await self.prune()
                await self.flag_followed()
            except Exception as exc:
                self.last_error = f"{time.strftime('%H:%M:%S')} {type(exc).__name__}: {exc}"
            finally:
                self.running = False
            await asyncio.sleep(600)

    def auto_count(self) -> int:
        return int(self.db.scalar("select count(*) from whales where active=1 and source='auto'", default=0))

    def pick_coins(self, limit: int = 16) -> list[str]:
        """Coins whose early buyers are worth checking: today's runners, coins your alerts caught that went 3x+,
        coins 2+ of your whales bought, and coins you made money on yourself."""
        now = int(time.time())
        mints = [r["mint"] for r in self.db.rows("""select mint, max(peak_price/price_usd) x from alerts
            where kind='BUY' and price_usd>0 and peak_price>0 and ts>=? group by mint having x>=5 order by x desc
            limit 8""", (now - 3 * 86400,))]                # coins your whale alerts caught that ran 5x+
        mints += [c["mint"] for c in self.runners.suggest_coins(limit)]
        mints += [r["mint"] for r in self.db.rows("""select mint, max(peak_price/entry_price) x from copies
            where open_ts>=? and entry_price>0 group by mint having x>=3 order by x desc limit 5""", (now - 2 * 86400,))]
        mints += [r["mint"] for r in self.db.rows("""select mint, count(distinct wallet) n from swaps
            where side='BUY' and is_me=0 and ts>=? group by mint having n>=2 order by n desc limit 5""", (now - 86400,))]
        mints += [r["mint"] for r in self.db.rows("""select mint, sum(case when side='SELL' then usd else -usd end) pnl
            from my_trades where ts>=? group by mint having pnl>0 order by pnl desc limit 5""", (now - 3 * 86400,))]
        fresh = []
        for m in dict.fromkeys(mints):
            if now - int(self.db.get_meta(f"scouted:{m}", "0") or 0) > 6 * 3600:
                fresh.append(m)
        return fresh[:limit]

    async def scout(self, now: int | None = None) -> dict:
        """One discovery pass. Returns a summary (also used by /scout and the dashboard)."""
        now = int(now or time.time())
        self.running = True
        self.last_run = now
        self.db.set_meta("scout_last_run", now)
        coins = self.pick_coins()
        followed, picks, checked = [], [], 0
        auto = self.cfg.flag("AUTO_WHALES")
        room = int(self.cfg.get("AUTO_WHALE_LIMIT")) - self.auto_count() if auto else 5
        # 1) research every winner first and pool their early buyers
        pool: dict[str, dict] = {}
        start_calls = getattr(self.discovery.rpc, "calls", 0)

        def over_budget() -> bool:
            return getattr(self.discovery.rpc, "calls", 0) - start_calls > RUN_CALL_BUDGET

        for mint in coins:
            if over_budget():
                break
            self.db.set_meta(f"scouted:{mint}", now)
            coin = await self.discovery.research_coin(mint)
            if not coin.get("ok"):
                continue
            for cand in coin["candidates"]:
                if cand["tracked"] or cand["flags"]:
                    continue
                entry = pool.setdefault(cand["wallet"], {"cand": cand, "coin": coin, "wins": set(), "usd": 0.0})
                entry["wins"].add(coin.get("symbol") or mint[:4])
                entry["usd"] += num(cand.get("buy_usd"))
        # 2) wallets early in several winners are the real finds: check those first
        ranked = sorted(pool.values(), key=lambda e: (-len(e["wins"]), -e["usd"]))
        for e in ranked:
            cand, coin = e["cand"], e["coin"]
            if room <= 0 or checked >= MAX_CHECKS_PER_RUN or over_budget():
                break
            seen = self.db.row("select * from whale_candidates where address=?", (cand["wallet"],))
            multi = len(e["wins"]) >= 2
            retry_days = 1 if multi else CANDIDATE_RETRY_DAYS   # a repeat winner gets a fresh look sooner
            if seen and now - int(seen["analyzed_ts"] or 0) < retry_days * 86400:
                continue
            result = await self.discovery.analyze_wallet(cand["wallet"])
            result["last_trade_ts"] = result.get("last_trade_ts") or await self._last_trade_ts(cand["wallet"])
            checked += 1
            ok, reason = qualifies(result, now)
            if not ok and multi and result.get("pnl_sol", 0) > 0 and not reason.startswith(("🤖", "❌")) \
                    and now - result.get("last_trade_ts", 0) <= 3 * 86400:
                ok, reason = True, f"early in {len(e['wins'])} winners ({', '.join(sorted(e['wins'])[:3])})"
            elif multi:
                reason = f"early in {len(e['wins'])} winners · {reason}"
            level, why = ("no", reason)
            prof = None
            if ok:
                prof = await self.profiler.profile_history(cand["wallet"], result) if result.get("trip_list") else None
                level, why = copyable(prof)
                reason = why if level != "unscored" else f"{reason} · {why}"
            if level == "unscored" and auto:
                level, reason = "strong", f"on trial — {reason}"   # follow now; dropped if it turns out a flipper/loser
            if level == "strong" and auto:
                name = f"auto-{short(cand['wallet'])[:4]}"
                added, _ = self.whales.add(cand["wallet"], name, source="auto")
                if added:
                    room -= 1
                    self.profiler.save(cand["wallet"], prof)
                    followed.append((cand, coin, result, reason))
            elif level in ("strong", "unscored"):
                room -= 1
                picks.append((cand, coin, result, reason))
            status = "followed" if (level == "strong" and auto) else "picked" if level != "no" else "rejected"
            self._remember(cand, coin, result, status, reason, now)
        if followed:
            self.refresh_wallets()
            for cand, coin, result, reason in followed:
                await self.notify(
                    f"🤖 <b>Auto-followed {esc(short(cand['wallet']))}</b>\n"
                    f"Early in ${esc(coin['symbol'])} at {mc(cand['entry_mc'])} (peaked {cand['to_peak']:.0f}x). "
                    f"Recent record: {esc(reason)}, median hold {int(result['median_hold_s'] // 60)}m.\n"
                    f"<code>{cand['wallet']}</code>",
                    buttons=[[("Unfollow", None, f"untrack:{cand['wallet']}"),
                              ("GMGN", f"https://gmgn.ai/sol/address/{cand['wallet']}", None)]],
                    silent=True, kind="AUTO")
        if picks:
            lines = ["🔎 <b>Whale picks</b> — early in today's runners and not flippers. Tap to follow:"]
            buttons = []
            for i, (cand, coin, result, reason) in enumerate(picks, 1):
                lines.append(f"{i}. <code>{cand['wallet']}</code>\n   early in ${esc(coin['symbol'])} at "
                             f"{mc(cand['entry_mc'])} (peaked {cand['to_peak']:.0f}x) · {esc(reason)} · "
                             f"median hold {int(result['median_hold_s'] // 60)}m")
                buttons.append([(f"➕ Follow #{i}", None, f"track:{cand['wallet']}"),
                                (f"GMGN #{i}", f"https://gmgn.ai/sol/address/{cand['wallet']}", None)])
            await self.notify("\n".join(lines), buttons=buttons, silent=True, kind="PICKS")
        summary = {"ts": now, "coins": coins, "checked": checked, "followed": len(followed), "picked": len(picks)}
        self.db.set_meta("scout_last_summary", json.dumps(summary))
        return summary

    async def _last_trade_ts(self, wallet: str) -> int:
        sigs = await self.discovery.rpc.signatures(wallet, limit=1)
        return int(sigs[0].get("blockTime") or 0) if sigs else 0

    def _remember(self, cand, coin, result, status, reason, now) -> None:
        self.db.run("""insert into whale_candidates(address,found_ts,analyzed_ts,coins,verdict,pnl_sol,win_rate,trips,
                median_hold_s,last_trade_ts,status,reason) values(?,?,?,?,?,?,?,?,?,?,?,?)
            on conflict(address) do update set analyzed_ts=excluded.analyzed_ts,
                coins=whale_candidates.coins||','||excluded.coins, verdict=excluded.verdict, pnl_sol=excluded.pnl_sol,
                win_rate=excluded.win_rate, trips=excluded.trips, median_hold_s=excluded.median_hold_s,
                last_trade_ts=excluded.last_trade_ts, status=excluded.status, reason=excluded.reason""",
                    (cand["wallet"], now, now, coin.get("symbol", ""), result.get("verdict", ""),
                     result.get("pnl_sol", 0), result.get("win_rate", 0), result.get("trips", 0),
                     result.get("median_hold_s", 0), result.get("last_trade_ts", 0), status, reason))

    async def promote_picks(self, now: int | None = None) -> list[str]:
        """With AUTO_WHALES on, follow the profitable wallets earlier runs only suggested (found while it was off)."""
        now = int(now or time.time())
        room = int(self.cfg.get("AUTO_WHALE_LIMIT")) - self.auto_count()
        added = []
        for c in self.db.rows("""select * from whale_candidates where status='picked' and pnl_sol>0
                and last_trade_ts>=? order by pnl_sol desc""", (now - MAX_IDLE_DAYS * 86400,)):
            if room <= 0:
                break
            ok, _ = self.whales.add(c["address"], f"auto-{short(c['address'])[:4]}", source="auto")
            self.db.run("update whale_candidates set status='followed' where address=?", (c["address"],))
            if ok:
                room -= 1
                added.append(c)
        if added:
            self.refresh_wallets()
            await self.notify("🤖 <b>Auto-followed " + str(len(added)) + " whale pick(s)</b>\n" + "\n".join(
                f"• <code>{c['address']}</code> {esc(c['reason'] or '')}" for c in added), silent=True, kind="AUTO")
        return [c["address"] for c in added]

    async def prune(self, now: int | None = None) -> list[tuple[dict, str]]:
        """Drop auto-followed whales that stopped working. Checked at most once a day."""
        now = int(now or time.time())
        if now - int(self.db.get_meta("scout_last_prune", "0") or 0) < PRUNE_EVERY:
            return []
        self.db.set_meta("scout_last_prune", now)
        dropped = []
        for w in self.db.rows("select * from whales where active=1 and source='auto'"):
            stats = self.whales.stats(w["address"], fresh=True)
            age_days = (now - int(w["added_ts"] or now)) / 86400
            last = int(w["last_trade_ts"] or 0) or int(w["added_ts"] or now)
            reason = ""
            send_ok, why = profiles.verdict(self.profiler.profile_local(w["address"]), self.cfg.get("MIN_COPY_SCORE"))
            trial = self._trial_result(w["address"], int(w["added_ts"] or now))
            buys = sorted(num(r["usd_value"]) for r in self.db.rows(
                "select usd_value from swaps where wallet=? and side='BUY' and ts>=?", (w["address"], now - 3 * 86400)))
            typical = buys[len(buys) // 2] if len(buys) >= 3 else None
            if trial is not None and trial[0] >= TRIAL_MIN_COPIES and trial[1] < TRIAL_DROP_AVG:
                reason = f"its first {trial[0]} copies averaged {trial[1]:+.0f}%"
            elif typical is not None and typical < MIN_TYPICAL_BUY_USD and self.cfg.flag("QUALITY_GATE"):
                reason = f"buys too small for alerts (typical ${typical:,.0f})"
            elif not send_ok:
                reason = why
            elif stats["status"] == "COLD":
                reason = f"copying them lost money ({pct(stats['avg'])} over {stats['n']} buys)"
            elif (now - last) / 86400 >= MAX_IDLE_DAYS:
                reason = f"no trades for {int((now - last) / 86400)} days"
            elif age_days >= RECHECK_AFTER_DAYS and stats["n"] < 5:
                result = await self.discovery.analyze_wallet(w["address"])
                result["last_trade_ts"] = last
                ok, why = qualifies(result, now)
                if not ok:
                    reason = f"recent trading no longer profitable ({why})"
            if reason:
                self.whales.remove(w["address"])
                self.db.run("update whale_candidates set status='dropped', reason=? where address=?",
                            (reason, w["address"]))
                dropped.append((w, reason))
        if dropped:
            self.refresh_wallets()
            await self.notify("🤖 <b>Autopilot dropped " + str(len(dropped)) + " whale(s)</b>\n" + "\n".join(
                f"• {esc(w['name'])}: {esc(r)}" for w, r in dropped), silent=True, kind="AUTO")
        return dropped

    def _trial_result(self, address: str, since: int) -> tuple[int, float] | None:
        """(copies, average %) of a whale's copies since it was followed, open ones at today's price."""
        rows = self.db.rows("select * from copies where whale=? and open_ts>=?", (address, since))
        if not rows:
            return None
        fee = self.cfg.get("COPY_FEE_PCT")
        rets = []
        for c in rows:
            if c["status"] == "closed":
                rets.append(num(c["return_pct"]))
            elif num(c["entry_price"]) > 0 and num(c["last_price"]) > 0:
                rets.append((num(c["last_price"]) / num(c["entry_price"]) - 1) * 100 - 2 * fee)
        return (len(rets), sum(min(r, 300) for r in rets) / len(rets)) if rets else None

    async def flag_followed(self) -> list[str]:
        """Tell you once about each whale you follow that turned out to be a flipper or a losing copy."""
        flagged = []
        for w in self.db.rows("select * from whales where active=1 and source<>'auto'"):
            prof = self.profiler.get(w["address"])
            ok, why = profiles.verdict(prof, self.cfg.get("MIN_COPY_SCORE"))
            key = f"flagged:{w['address']}"
            if ok or self.db.get_meta(key) == prof.get("style", "?"):
                continue
            self.db.set_meta(key, prof.get("style", "?"))
            flagged.append(w["address"])
            await self.notify(
                f"🚫 <b>{esc(w['name'])}: entry alerts stopped</b>\n{esc(why)}.\n"
                "Still tracked and scored — its buys show in /status as blocked. Remove it to make room for "
                "whales you can actually copy, or /set BLOCK_FLIPPERS off to hear every buy again.",
                buttons=[[("Remove", None, f"untrack:{w['address']}"),
                          ("GMGN", f"https://gmgn.ai/sol/address/{w['address']}", None)]],
                silent=True, kind="AUTO")
        return flagged

    def status(self) -> dict:
        try:
            summary = json.loads(self.db.get_meta("scout_last_summary", "") or "{}")
        except ValueError:
            summary = {}
        return {"enabled": self.cfg.flag("WHALE_PICKS") or self.cfg.flag("AUTO_WHALES"),
                "auto_follow": self.cfg.flag("AUTO_WHALES"), "running": self.running, "last_run": self.last_run,
                "auto_whales": self.auto_count(), "limit": int(self.cfg.get("AUTO_WHALE_LIMIT")),
                "last_summary": summary, "error": self.last_error,
                "candidates": self.db.rows("select * from whale_candidates order by analyzed_ts desc limit 40")}
