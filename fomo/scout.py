"""Whale picks: finds wallets that are profitable right now in today's runners and sends them to you.

By default it only *suggests* (you tap ➕ Follow) — following picks automatically (AUTO_WHALES) made
entries worse in practice, so it's off unless you turn it on.

Every AUTO_SCOUT_HOURS:
  1. Pick up to 3 coins that just ran: today's biggest community runners (2x+ in 24h with real
     liquidity) and coins your own alerts caught that went 3x+.
  2. Find each coin's early buyers (same method as /find), skipping snipers, creators and bots.
  3. Read each candidate's recent swaps (same as /analyze) and auto-follow only wallets that are
     profitable *right now*: 6+ closed round trips, +1 SOL or more, 45%+ won, traded in the last
     2 days, not a sub-minute bot.
Every day it re-checks the whales it added (never the ones you added) and drops any that:
  - turned COLD (copying them lost money), or
  - haven't traded for 4 days, or
  - after 3 days still have too few copies to judge and their own recent trading is no longer
    profitable.
Every follow and drop is reported in Telegram (silently) with the reason.
"""
from __future__ import annotations

import asyncio
import json
import time

from .discovery import Discovery
from .util import esc, mc, pct, short

MIN_TRIPS = 6
MIN_PNL_SOL = 1.0
MIN_WIN_RATE = 0.45
MAX_IDLE_DAYS = 4
RECHECK_AFTER_DAYS = 3
CANDIDATE_RETRY_DAYS = 3
SMALL_BUDGET = {"pages": 40, "samples": 60, "dense": 300, "holders": 12, "wallet_txs": 150}


def qualifies(r: dict, now: float | None = None) -> tuple[bool, str]:
    """Is this wallet profitable right now? Returns (yes, reason)."""
    now = now or time.time()
    if not r.get("ok"):
        return False, r.get("error", "couldn't read history")
    if not r["verdict"].startswith("✅"):
        return False, r["verdict"]
    if r["trips"] < MIN_TRIPS:
        return False, f"only {r['trips']} closed trades"
    if r["pnl_sol"] < MIN_PNL_SOL:
        return False, f"only {r['pnl_sol']:+.2f} SOL profit"
    if r["win_rate"] < MIN_WIN_RATE:
        return False, f"{r['win_rate'] * 100:.0f}% won"
    if now - r.get("last_trade_ts", 0) > 2 * 86400:
        return False, "no trades in the last 2 days"
    return True, f"{r['pnl_sol']:+.1f} SOL over {r['trips']} trades, {r['win_rate'] * 100:.0f}% won"


class WhaleScout:
    def __init__(self, cfg, db, rpc, market, whales, runners, notify, refresh_wallets):
        self.cfg = cfg
        self.db = db
        self.whales = whales
        self.runners = runners
        self.notify = notify
        self.refresh_wallets = refresh_wallets
        self.discovery = Discovery(rpc, market, db, cfg)
        self.discovery.budget = dict(SMALL_BUDGET if cfg.uses_helius else
                                     {"pages": 10, "samples": 30, "dense": 120, "holders": 6, "wallet_txs": 80})
        self.last_run = int(db.get_meta("scout_last_run", "0") or 0)
        self.last_error = ""
        self.running = False

    async def run(self) -> None:
        await asyncio.sleep(300)  # let the bot settle (wallet backfill etc.) first
        while True:
            try:
                active = self.cfg.flag("WHALE_PICKS") or self.cfg.flag("AUTO_WHALES")
                if active and time.time() - self.last_run >= self.cfg.get("AUTO_SCOUT_HOURS") * 3600:
                    await self.scout()
                if self.cfg.flag("AUTO_WHALES"):
                    await self.prune()
            except Exception as exc:
                self.last_error = f"{time.strftime('%H:%M:%S')} {type(exc).__name__}: {exc}"
            finally:
                self.running = False
            await asyncio.sleep(600)

    def auto_count(self) -> int:
        return int(self.db.scalar("select count(*) from whales where active=1 and source='auto'", default=0))

    def pick_coins(self, limit: int = 3) -> list[str]:
        now = int(time.time())
        mints = [c["mint"] for c in self.runners.suggest_coins(limit)]
        mints += [r["mint"] for r in self.db.rows("""select mint, max(peak_price/entry_price) x from copies
            where open_ts>=? and entry_price>0 group by mint having x>=3 order by x desc limit 5""", (now - 2 * 86400,))]
        fresh = []
        for m in dict.fromkeys(mints):
            if now - int(self.db.get_meta(f"scouted:{m}", "0") or 0) > 86400:
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
        room = int(self.cfg.get("AUTO_WHALE_LIMIT")) - self.auto_count() if auto else 3
        for mint in coins:
            self.db.set_meta(f"scouted:{mint}", now)
            coin = await self.discovery.research_coin(mint)
            if not coin.get("ok"):
                continue
            for cand in coin["candidates"][:4]:
                if cand["tracked"] or cand["flags"] or room <= 0:
                    continue
                seen = self.db.row("select * from whale_candidates where address=?", (cand["wallet"],))
                if seen and now - int(seen["analyzed_ts"] or 0) < CANDIDATE_RETRY_DAYS * 86400:
                    continue
                result = await self.discovery.analyze_wallet(cand["wallet"])
                result["last_trade_ts"] = await self._last_trade_ts(cand["wallet"])
                checked += 1
                ok, reason = qualifies(result, now)
                self._remember(cand, coin, result, ("followed" if auto else "picked") if ok else "rejected", reason, now)
                if ok and not auto:
                    room -= 1
                    picks.append((cand, coin, result, reason))
                elif ok:
                    name = f"auto-{short(cand['wallet'])[:4]}"
                    added, _ = self.whales.add(cand["wallet"], name, source="auto")
                    if added:
                        room -= 1
                        followed.append((cand, coin, result, reason))
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
            lines = ["🔎 <b>Whale picks</b> — profitable right now and early in today's runners. Tap to follow:"]
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

    async def prune(self, now: int | None = None) -> list[tuple[dict, str]]:
        """Drop auto-followed whales that stopped working. Checked at most once a day."""
        now = int(now or time.time())
        if now - int(self.db.get_meta("scout_last_prune", "0") or 0) < 86400:
            return []
        self.db.set_meta("scout_last_prune", now)
        dropped = []
        for w in self.db.rows("select * from whales where active=1 and source='auto'"):
            stats = self.whales.stats(w["address"], fresh=True)
            age_days = (now - int(w["added_ts"] or now)) / 86400
            last = int(w["last_trade_ts"] or 0) or int(w["added_ts"] or now)
            reason = ""
            if stats["status"] == "COLD":
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
