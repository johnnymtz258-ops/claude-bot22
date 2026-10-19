"""Buy check: before (or the moment) you buy a coin, everything the bot knows that says "careful" — in one verdict.

Paste a coin address, tap 🧾 Buy check on an alert, or /check COIN. With your wallet synced (MY_WALLETS) and
BUY_CHECK on, every buy you make is checked automatically and you get the verdict with a plan.

🔴 AVOID   something that has taken people's money in your data or can't be undone: a brand-new launch, freeze or mint
           authority, Token-2022 traps, a failed sell-back, bundled supply, a pool too thin to sell into, dumping now
🟡 CAREFUL risk you can manage with size and a stop: young coin, chasing after a big run, mostly bots, serial dev,
           every whale already out, a cold market
🟢 CLEAN   none of the above — not a promise it goes up, just none of the traps the bot can see
"""
from __future__ import annotations

import asyncio
import time

from .util import esc, mc, num, pct, usd

RED, YELLOW, GREEN = "🔴", "🟡", "🟢"


def _age_text(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)} min"
    return f"{hours:.0f}h" if hours < 48 else f"{hours / 24:.0f} days"


class BuyCheck:
    def __init__(self, cfg, db, market, engine, whales, live=None, pulse=None, hype=None):
        self.cfg, self.db, self.market, self.engine, self.whales = cfg, db, market, engine, whales
        self.live, self.pulse, self.hype = live, pulse, hype

    async def _sellback(self, mint: str) -> float | None:
        """% lost buying PAPER_TRADE_USD and selling straight back (Jupiter quotes: fees, price impact, hidden tax)."""
        live = self.live
        if not live or not hasattr(live, "quote"):
            return None
        try:
            sol_usd = await self.market.sol_usd()
            if sol_usd <= 0:
                return None
            lamports = int(self.cfg.get("PAPER_TRADE_USD") / sol_usd * 1e9)
            wsol = "So11111111111111111111111111111111111111112"
            buy = await asyncio.wait_for(live.quote(wsol, mint, lamports, 1500), 6)
            back = await asyncio.wait_for(live.quote(mint, wsol, int(buy["outAmount"]), 1500), 6)
            return (1 - int(back["outAmount"]) / lamports) * 100
        except Exception:
            return None

    async def check(self, mint: str, now: float | None = None) -> dict:
        now = now or time.time()
        info = await self.market.token(mint, max_age=15) or {}
        if not info:
            return {"mint": mint, "verdict": RED, "items": [(RED, "No market for this coin on DexScreener — no pool to "
                                                                   "sell into")], "info": {}}
        rug_task = self.market.rugcheck(mint) if hasattr(self.market, "rugcheck") else asyncio.sleep(0)
        safety, rug, community, jup, sellback = await asyncio.gather(
            self.market.safety(mint), rug_task, self.engine.community.check(mint, info), self.engine._jupiter(mint),
            self._sellback(mint), return_exceptions=True)
        safety = safety if isinstance(safety, dict) else {}
        rug = rug if isinstance(rug, dict) else None
        community = community if isinstance(community, dict) else {}
        jup = jup if isinstance(jup, dict) else {}
        sellback = sellback if isinstance(sellback, (int, float)) else None
        items: list[tuple[str, str]] = []
        created = num(info.get("pair_created_ts"))
        age_h = (now - created) / 3600 if created else None
        mcap, liq = num(info.get("mc_usd")), num(info.get("liquidity_usd"), -1)
        m5, h1, h24 = num(info.get("change_m5")), num(info.get("change_h1")), num(info.get("change_h24"))
        # age: where the rugs were
        if age_h is None:
            items.append((RED, "Launch time unknown — treat it as brand new"))
        elif age_h < 1:
            items.append((RED, f"Brand-new launch ({_age_text(age_h)} old): in your data 58% of whale buys this early were "
                               "down half within the hour — insiders and snipers are ahead of you"))
        elif age_h < 6:
            items.append((YELLOW, f"Young coin ({_age_text(age_h)} old): past the worst, still risky — size small"))
        else:
            items.append((GREEN, f"Coin {_age_text(age_h)} old — past the launch danger zone"))
        # contract
        if safety.get("freeze_authority"):
            items.append((RED, "Freeze authority still on: the dev can freeze your tokens"))
        if safety.get("mint_authority"):
            items.append((RED, "Mint authority still on: the dev can print more"))
        for trap in safety.get("traps") or []:
            items.append((RED, f"Token trap: {trap}"))
        if safety.get("ok") and not (safety.get("freeze_authority") or safety.get("mint_authority") or safety.get("traps")):
            items.append((GREEN, "No freeze/mint authority, no Token-2022 traps"))
        if rug:
            for d in (rug.get("danger") or [])[:3]:
                items.append((RED, f"RugCheck danger: {d}"))
            lp = rug.get("lp_locked")
            if lp is not None and lp < 50 and info.get("dex") != "pumpfun":
                items.append((YELLOW, f"Only {lp:.0f}% of the liquidity is locked or burned — it can be pulled"))
        # can you get out?
        if sellback is not None:
            if sellback > self.cfg.get("LIVE_MAX_ROUNDTRIP_LOSS_PCT"):
                items.append((RED, f"Sell-back test: buying {usd(self.cfg.get('PAPER_TRADE_USD'))} and selling straight back "
                                   f"loses {sellback:.0f}% — honeypot, hidden tax or no liquidity"))
            elif sellback > 6:
                items.append((YELLOW, f"Sell-back test: {sellback:.1f}% lost on a round trip — expensive to trade"))
            else:
                items.append((GREEN, f"Sell-back test: {sellback:.1f}% round trip (fees + price impact)"))
        if liq < 0 or liq < 5_000:
            items.append((RED, f"Liquidity {usd(max(liq, 0))} — almost nothing to sell into"))
        elif mcap > 0 and liq < 0.03 * mcap:
            items.append((RED, f"Liquidity only {liq / mcap * 100:.1f}% of the market cap — the price can't hold a sell"))
        elif liq < 15_000:
            items.append((YELLOW, f"Thin liquidity ({usd(liq)}): your own sell will move the price"))
        # who holds it
        top10 = num(community.get("top10_pct"), -1)
        if top10 > 35:
            items.append((RED, f"Top 10 wallets hold {top10:.0f}% of the supply — bundled / insider supply"))
        elif top10 > 25:
            items.append((YELLOW, f"Top 10 wallets hold {top10:.0f}% — a few wallets can dump it"))
        elif top10 >= 0:
            items.append((GREEN, f"Supply spread out: top 10 wallets hold {top10:.0f}%"))
        if jup:
            org = num(jup.get("organic"), -1)
            if 0 <= org < 25:
                items.append((YELLOW, f"Mostly bots / wash trading (Jupiter organic score {org:.0f})"))
            elif org >= 60:
                items.append((GREEN, f"Real buyers (Jupiter organic score {org:.0f})"))
            if jup.get("sus"):
                items.append((RED, "Jupiter's audit flags this token as suspicious"))
            if num(jup.get("dev_mints")) >= 20:
                items.append((YELLOW, f"Its dev has launched {int(num(jup.get('dev_mints')))} coins"))
        # timing
        if m5 < -15 or h1 < -35:
            items.append((RED, f"Dumping right now ({pct(m5)} 5m, {pct(h1)} 1h)"))
        elif h1 > 100 or m5 > 40:
            items.append((YELLOW, f"Chasing: already {pct(h1)} this hour ({pct(m5)} in 5 min) — buyers before you are "
                                  "about to take profit"))
        elif h24 > 300:
            items.append((YELLOW, f"Up {pct(h24)} in 24h — late in the move"))
        # your whales
        holders = self.whales.holders_of(mint) if self.whales else []
        if holders:
            still = [h for h in holders if h["still_in"]]
            if still:
                items.append((GREEN, f"{len(still)} of your whales still in: " + ", ".join(h["name"] for h in still[:3])))
            else:
                items.append((YELLOW, f"All {len(holders)} of your whales that traded it have sold"))
        # the market
        regime = self.pulse.regime() if self.pulse else "learning"
        if regime == "cold":
            items.append((YELLOW, "Cold market (/market): few real pumps, more rugs — take profit early"))
        reds = [t for lvl, t in items if lvl == RED]
        yellows = [t for lvl, t in items if lvl == YELLOW]
        verdict = RED if reds else YELLOW if yellows else GREEN
        return {"mint": mint, "info": info, "items": items, "verdict": verdict, "age_h": age_h, "sellback": sellback,
                "reds": len(reds), "yellows": len(yellows)}

    def day_so_far(self, now: float | None = None) -> dict:
        """Your buys today and how your closed coins went (from your synced wallet)."""
        now = now or time.time()
        start = int(now) - int(now) % 86400
        buys = int(self.db.scalar("select count(*) from swaps where is_me=1 and side='BUY' and ts>=?", (start,),
                                  default=0))
        spent = num(self.db.scalar("select coalesce(sum(usd_value),0) from swaps where is_me=1 and side='BUY' and ts>=?",
                                   (start,)))
        return {"buys": buys, "spent": spent}

    def plan(self, res: dict) -> str:
        price_mc = num(res["info"].get("mc_usd"))
        if res["verdict"] == RED:
            return "💼 Size: don't — or only what you'd happily lose. If you're in: sell into the first green candle."
        stop, tp = (0.75, 1.3) if (res.get("age_h") or 0) < 24 else (0.8, 1.5)
        size = "half your usual size" if res["verdict"] == YELLOW else "your usual size"
        return (f"💼 Size: {size} · stop {mc(price_mc * stop)} ({(stop - 1) * 100:.0f}%) · take half at "
                f"{mc(price_mc * tp)} (+{(tp - 1) * 100:.0f}%) · let the rest ride with a 30% trailing stop")


def format_check(res: dict, header: str = "") -> str:
    info = res.get("info") or {}
    sym = esc(info.get("symbol") or res["mint"][:4])
    word = {RED: "AVOID", YELLOW: "CAREFUL", GREEN: "CLEAN"}[res["verdict"]]
    lines = [header or f"<b>🧾 Buy check · ${sym} · {res['verdict']} {word}</b>"]
    if info:
        lines.append(f"{mc(info.get('mc_usd'))} MC · liq {usd(max(num(info.get('liquidity_usd'), 0), 0))} · "
                     f"5m {pct(info.get('change_m5'))} · 1h {pct(info.get('change_h1'))} · 24h {pct(info.get('change_h24'))}")
    order = {RED: 0, YELLOW: 1, GREEN: 2}
    for lvl, text in sorted(res["items"], key=lambda it: order[it[0]]):
        lines.append(f"{lvl} {esc(text)}")
    return "\n".join(lines)
