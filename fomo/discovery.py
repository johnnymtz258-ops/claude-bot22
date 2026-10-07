"""Find new whales worth following, and check a wallet before you follow it.

/find MINT [MINT2 ...]
    For a coin that already ran: sample its whole trade history to find the peak, then read
    the trades from the "early" window (price at most 1/3 of the peak) and rank the wallets
    that bought there by size and how early they were. Launch-block snipers and the creator
    are flagged (they are usually bots you can't copy in time). With 2+ coins, wallets that
    were early in more than one are listed first — one lucky call is common, repeats aren't.

/whale WALLET  (analyze)
    Read the wallet's recent swaps, rebuild its round trips, and say whether its style is
    copyable: profitable, not a sub-minute bot, and buying early rather than chasing.

Both are best with a Helius key; public RPC works but is slow and may be rate limited.
"""
from __future__ import annotations

import asyncio
import math
import statistics
import time

from .swaps import parse_swap, trader_swap
from .util import num

EARLY_FRACTION = 1 / 3   # "early" = bought at a price no higher than a third of the later peak
MIN_BUY_USD = 100
SNIPER_SLOTS = 2          # buys within this many slots of the first trade = launch snipers


def _budget(helius: bool) -> dict:
    return ({"pages": 120, "samples": 120, "dense": 600, "holders": 25, "wallet_txs": 200} if helius
            else {"pages": 25, "samples": 50, "dense": 220, "holders": 12, "wallet_txs": 100})


def _spread(items: list, n: int) -> list:
    """Up to n items spread evenly across the list (always including both ends)."""
    if len(items) <= n:
        return list(items)
    step = (len(items) - 1) / (n - 1)
    return [items[round(i * step)] for i in range(n)]


class Discovery:
    def __init__(self, rpc, market, db, cfg):
        self.rpc = rpc
        self.market = market
        self.db = db
        self.cfg = cfg
        self.budget = _budget(cfg.uses_helius)

    async def _parse_many(self, sigs: list[dict], parse, progress=None, label="") -> list:
        """Fetch + parse transactions with bounded concurrency. `parse(tx, sigrow)` -> item or None."""
        out: list = []
        done = 0
        sem = asyncio.Semaphore(8 if self.cfg.uses_helius else 3)

        async def one(row):
            nonlocal done
            async with sem:
                strained = getattr(self.rpc, "strained", None)
                for _ in range(30):   # live whale trades come first: step aside while the RPC is rate limited
                    if not (strained and strained()):
                        break
                    await asyncio.sleep(1)
                tx = await self.rpc.transaction(row["signature"], wait=0)
            done += 1
            if progress and done % 50 == 0:
                await progress(f"{label} {done}/{len(sigs)}")
            return parse(tx, row) if tx else None

        for item in await asyncio.gather(*(one(r) for r in sigs)):
            if item:
                out.append(item)
        return out

    # -- coin research ------------------------------------------------------------------------
    async def _history(self, mint: str, progress=None) -> tuple[list[dict], bool]:
        """All successful signatures touching the mint, oldest first (capped by the page budget)."""
        rows: list[dict] = []
        before = None
        for page in range(self.budget["pages"]):
            batch = await self.rpc.signatures(mint, limit=1000, before=before)
            if not batch:
                return list(reversed(rows)), page > 0 or bool(rows)
            rows.extend(r for r in batch if r.get("err") is None)
            before = batch[-1]["signature"]
            if len(batch) < 1000:
                return list(reversed(rows)), True
            if progress and page % 10 == 9:
                await progress(f"reading history… {len(rows):,} trades so far")
        return list(reversed(rows)), False

    async def research_coin(self, mint: str, progress=None) -> dict:
        sol_usd = await self.market.sol_usd()
        info = await self.market.token(mint, max_age=60)
        now_price = num(info.get("price_usd"))
        supply = num(info.get("mc_usd")) / now_price if now_price > 0 and num(info.get("mc_usd")) > 0 else 0.0
        if not supply:
            supply = num((await self.market.safety(mint)).get("supply"))
        history, from_launch = await self._history(mint, progress)
        if not history:
            return {"mint": mint, "ok": False, "error": "no trade history found (check the address / RPC)"}

        def trade(tx, row):
            found = trader_swap(tx, mint, sol_usd)
            if not found:
                return None
            wallet, swap = found
            rate = sol_usd if swap["base"] == "SOL" else 1.0
            usd_value = swap["base_amount"] * rate
            if usd_value < 1:
                return None  # dust trades give noisy prices
            return {"wallet": wallet, "side": swap["side"], "usd": usd_value, "tokens": swap["token_amount"],
                    "price": usd_value / swap["token_amount"], "ts": swap["ts"] or row.get("blockTime") or 0,
                    "slot": swap["slot"] or row.get("slot") or 0}

        if progress:
            await progress(f"sampling prices across {len(history):,} trades…")
        samples = await self._parse_many(_spread(history, self.budget["samples"]), trade, progress, "sampling")
        if len(samples) < 3:
            return {"mint": mint, "ok": False, "error": "couldn't read enough trades from the RPC"}
        samples.sort(key=lambda t: t["ts"])
        peak = max(samples, key=lambda t: t["price"])
        threshold = peak["price"] * EARLY_FRACTION
        early_until = max((t["ts"] for t in samples if t["ts"] <= peak["ts"] and t["price"] <= threshold), default=0)
        if not early_until:  # never 3x'd from the first sample: use the first third of the run-up
            early_until = samples[0]["ts"] + (peak["ts"] - samples[0]["ts"]) // 3
        window = [r for r in history if int(r.get("blockTime") or 0) <= early_until]
        if progress:
            await progress(f"reading {min(len(window), self.budget['dense'])} early trades…")
        trades = await self._parse_many(_spread(window, self.budget["dense"]), trade, progress, "early trades")

        launch_slot = int(history[0].get("slot") or 0) if from_launch else 0
        creator = trades[0]["wallet"] if from_launch and trades and trades[0]["ts"] <= int(history[0].get("blockTime") or 0) else ""
        buyers: dict[str, dict] = {}
        for t in sorted(trades, key=lambda x: x["ts"]):
            b = buyers.setdefault(t["wallet"], {"wallet": t["wallet"], "buy_usd": 0.0, "tokens": 0.0, "buys": 0,
                                                "sells": 0, "first_ts": t["ts"], "first_slot": t["slot"]})
            if t["side"] == "BUY":
                b["buy_usd"] += t["usd"]
                b["tokens"] += t["tokens"]
                b["buys"] += 1
            else:
                b["sells"] += 1
        ranked = sorted((b for b in buyers.values() if b["buy_usd"] >= MIN_BUY_USD and b["tokens"] > 0),
                        key=lambda b: -b["buy_usd"])[:self.budget["holders"]]
        balances = await asyncio.gather(*(self.rpc.token_balance(b["wallet"], mint) for b in ranked))
        tracked = {r["address"] for r in self.db.rows("select address from whales where active=1")}
        candidates = []
        for b, bal in zip(ranked, balances):
            entry = b["buy_usd"] / b["tokens"]
            to_peak = peak["price"] / entry if entry > 0 else 0.0
            flags = []
            if launch_slot and b["first_slot"] and b["first_slot"] - launch_slot <= SNIPER_SLOTS:
                flags.append("launch sniper")
            if b["wallet"] == creator:
                flags.append("creator")
            if b["buys"] >= 15:
                flags.append("bot-like")
            candidates.append({
                "wallet": b["wallet"], "buy_usd": b["buy_usd"], "entry_mc": entry * supply, "to_peak": to_peak,
                "to_now": now_price / entry if entry > 0 and now_price > 0 else 0.0,
                "holding_now": bal, "held_pct": (bal / b["tokens"] * 100) if bal is not None else None,
                "first_ts": b["first_ts"], "buys": b["buys"], "flags": flags, "tracked": b["wallet"] in tracked,
                "score": math.log2(max(to_peak, 1.0)) * math.log10(1 + b["buy_usd"]) * (0.3 if flags else 1.0),
            })
        candidates.sort(key=lambda c: -c["score"])
        return {"mint": mint, "ok": True, "symbol": info.get("symbol") or mint[:4], "trades": len(history),
                "from_launch": from_launch, "peak_mc": peak["price"] * supply, "now_mc": now_price * supply,
                "early_mc": threshold * supply, "early_trades_read": len(trades), "candidates": candidates}

    async def find(self, mints: list[str], progress=None) -> dict:
        coins = []
        for i, mint in enumerate(mints):
            async def step(text, i=i):
                if progress:
                    await progress(f"coin {i + 1}/{len(mints)}: {text}")
            coins.append(await self.research_coin(mint, step))
        seen: dict[str, dict] = {}
        for coin in coins:
            for c in coin.get("candidates") or []:
                agg = seen.setdefault(c["wallet"], {**c, "coins": [], "score": 0.0})
                agg["coins"].append(coin.get("symbol"))
                agg["score"] += c["score"]
        overall = sorted(seen.values(), key=lambda c: (-len(c["coins"]), -c["score"]))
        return {"coins": coins, "wallets": overall[:20], "ts": int(time.time())}

    # -- wallet analysis --------------------------------------------------------------------
    async def analyze_wallet(self, wallet: str, progress=None) -> dict:
        sol_usd = await self.market.sol_usd()
        sigs = [s for s in await self.rpc.signatures(wallet, limit=self.budget["wallet_txs"]) if s.get("err") is None]
        if not sigs:
            return {"wallet": wallet, "ok": False, "error": "no recent transactions found"}

        def swap_of(tx, row):
            s = parse_swap(tx, wallet, sol_usd)
            if s:
                s["ts"] = s["ts"] or int(row.get("blockTime") or 0)
                s["sol"] = s["base_amount"] if s["base"] == "SOL" else (s["base_amount"] / sol_usd if sol_usd else 0)
            return s

        swaps = sorted(await self._parse_many(sigs, swap_of, progress, "reading swaps"), key=lambda s: s["ts"])
        if not swaps:
            return {"wallet": wallet, "ok": False, "error": "no swaps in the recent history"}
        infos = await self.market.tokens({s["mint"] for s in swaps}, max_age=300)
        trips, open_bags, trip_list = [], [], []
        books: dict[str, dict] = {}
        for s in swaps:
            b = books.setdefault(s["mint"], {"tokens": 0.0, "cost": 0.0, "first_ts": s["ts"], "proceeds": 0.0,
                                             "spent": 0.0, "entry_price_sol": 0.0, "peak": 0.0,
                                             "first_sell_ts": None, "half_ts": None})
            if s["side"] == "BUY":
                if b["tokens"] <= 0:
                    b.update(first_ts=s["ts"], proceeds=0.0, spent=0.0, peak=0.0, first_sell_ts=None, half_ts=None)
                b["tokens"] += s["token_amount"]
                b["peak"] = max(b["peak"], b["tokens"])
                b["cost"] += s["sol"]
                b["spent"] += s["sol"]
            elif b["tokens"] > 0:
                q = min(s["token_amount"], b["tokens"])
                basis = b["cost"] * q / b["tokens"]
                b["proceeds"] += s["sol"] * (q / s["token_amount"])
                b["tokens"] -= q
                b["cost"] -= basis
                b["first_sell_ts"] = b["first_sell_ts"] or s["ts"]
                if b["half_ts"] is None and b["tokens"] <= 0.5 * b["peak"]:
                    b["half_ts"] = s["ts"]
                if b["tokens"] <= 1e-9 or s["holding_after"] <= 0:
                    trips.append({"mint": s["mint"], "roi": (b["proceeds"] / b["spent"] - 1) * 100 if b["spent"] else 0,
                                  "pnl_sol": b["proceeds"] - b["spent"], "hold_s": s["ts"] - b["first_ts"],
                                  "spent_sol": b["spent"]})
                    trip_list.append({"mint": s["mint"], "buy_ts": b["first_ts"], "first_sell_ts": b["first_sell_ts"],
                                      "half_out_ts": b["half_ts"] or s["ts"], "bought_usd": b["spent"] * sol_usd,
                                      "sold_usd": b["proceeds"] * sol_usd, "closed": True})
                    b.update(tokens=0.0, cost=0.0)
        for mint, b in books.items():
            if b["tokens"] > 0 and b["cost"] > 0:
                price = num((infos.get(mint) or {}).get("price_usd"))
                open_bags.append({"mint": mint, "cost_sol": b["cost"],
                                  "value_sol": b["tokens"] * price / sol_usd if sol_usd and price else None})
                trip_list.append({"mint": mint, "buy_ts": b["first_ts"], "first_sell_ts": b["first_sell_ts"],
                                  "half_out_ts": b["half_ts"], "bought_usd": b["spent"] * sol_usd,
                                  "sold_usd": b["proceeds"] * sol_usd, "closed": False})
        buys = [s for s in swaps if s["side"] == "BUY"]
        entry_mcs = []
        for s in buys:
            info = infos.get(s["mint"]) or {}
            if num(info.get("price_usd")) > 0 and num(info.get("mc_usd")) > 0 and sol_usd:
                supply = num(info["mc_usd"]) / num(info["price_usd"])
                entry_mcs.append(s["sol"] * sol_usd / s["token_amount"] * supply)
        span_days = max((swaps[-1]["ts"] - swaps[0]["ts"]) / 86400, 1 / 24)
        rois = [t["roi"] for t in trips]
        holds = [t["hold_s"] for t in trips]
        result = {
            "wallet": wallet, "ok": True, "swaps": len(swaps), "tokens": len(books), "trips": len(trips),
            "win_rate": sum(r > 0 for r in rois) / len(rois) if rois else 0.0,
            "pnl_sol": sum(t["pnl_sol"] for t in trips), "median_roi": statistics.median(rois) if rois else 0.0,
            "best_roi": max(rois) if rois else 0.0, "worst_roi": min(rois) if rois else 0.0,
            "median_hold_s": statistics.median(holds) if holds else 0, "buys_per_day": len(buys) / span_days,
            "avg_buy_sol": statistics.mean(s["sol"] for s in buys) if buys else 0.0,
            "median_entry_mc": statistics.median(entry_mcs) if entry_mcs else 0.0,
            "open_bags": len(open_bags), "span_days": span_days, "sol_usd": sol_usd, "trip_list": trip_list,
            "last_trade_ts": swaps[-1]["ts"],
        }
        result["verdict"], result["why"] = verdict(result)
        return result


def verdict(r: dict) -> tuple[str, list[str]]:
    why = []
    if r["buys_per_day"] > 60:
        why.append(f"{r['buys_per_day']:.0f} buys/day — looks like a bot")
    if why:
        return "🤖 Too fast to copy", why
    if r["trips"] >= 3 and r["median_hold_s"] < 60:
        # not rejected: the scanner replays its buys and keeps it if an exit plan of your own makes money
        return "⚡ Very fast flipper", [f"median hold {r['median_hold_s']:.0f}s — you can't follow its exits, "
                                       "only your own plan"]
    if r["trips"] < 5:
        return "🆕 Not enough closed trades to judge", [f"only {r['trips']} round trips in the recent history"]
    if r["pnl_sol"] <= 0 or r["win_rate"] < 0.35:
        return "❌ Losing lately", [f"{r['pnl_sol']:+.2f} SOL over {r['trips']} round trips, "
                                   f"{r['win_rate'] * 100:.0f}% won"]
    why.append(f"{r['pnl_sol']:+.2f} SOL over {r['trips']} round trips, {r['win_rate'] * 100:.0f}% won")
    if r["median_entry_mc"] and r["median_entry_mc"] < 1_000_000:
        why.append("usually buys early (median entry under $1M MC)")
    return "✅ Looks copyable", why
