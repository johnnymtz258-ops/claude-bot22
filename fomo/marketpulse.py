"""Market pulse: what the memecoin market is doing, what's trending, and what to look out for.

Memecoins move as a crowd: when SOL bleeds and fear is high, fewer coins run and more launches are rugs; when the
market is hot, the same alerts work better. The pulse measures that, every 15 minutes, from free sources and from
what the bot itself sees:
  • majors and mood   — SOL and BTC 24h change (CoinGecko), the Crypto Fear & Greed index (alternative.me)
  • the meme board    — Jupiter's 100 most traded tokens: how many are up on the day, their volume, their launchpads
  • launch pace       — new Solana pools per hour (GeckoTerminal)
  • the bot's own eyes — how many hype coins doubled, how many were rugs (pool emptied within 2 hours), how much
                         your whales bought, how the paper autopilot did — per hour the bot was RUNNING, so a sleeping
                         Mac or a dead RPC never looks like a market crash
  • narratives        — which themes (Chinese-name coins, AI, dogs, cats…) and words are rising vs last week
  • launchpads        — which platforms' coins run and which ones rug
From those it calls the market (🔥 hot · 🟢 healthy · 🟡 choppy · 🧊 cold) and writes what to look out for.
/market any time, a brief every morning (MARKET_BRIEF_HOUR), a note when the market turns, the Market tab on the
dashboard. With MARKET_GUARD on, the live autopilot doesn't open new real trades in a cold market (paper keeps going).
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
from datetime import datetime

from .tuner import dead_from
from .util import esc, num

SNAP_EVERY = 900
REPORT_CACHE = 300
FATE_SPAN = 7200        # a hype coin's fate: the 2 hours after its alert (the hype exit plan's longest hold)
SELLABLE_LIQ = 5_000    # a price only counts as a pump if the pool had at least this much in it
COINGECKO = ("https://api.coingecko.com/api/v3/simple/price?ids=solana,bitcoin&vs_currencies=usd"
             "&include_24hr_change=true")
FEAR_GREED = "https://api.alternative.me/fng/?limit=1"
REGIMES = {"hot": "🔥 Hot", "healthy": "🟢 Healthy", "choppy": "🟡 Choppy", "cold": "🧊 Cold", "learning": "⏳ Learning"}

THEMES = [
    ("chinese", "🇨🇳 Chinese-name coins", re.compile(r"[一-鿿]")),
    ("korean", "🇰🇷 Korean-name coins", re.compile(r"[가-힯]")),
    ("japanese", "🇯🇵 Japanese-name coins", re.compile(r"[぀-ヿ]")),
    ("ai", "🤖 AI / agents", re.compile(r"\b(ai|agi|agent|agents|gpt|llm|claude|grok|bot|robot|neural)\b", re.I)),
    ("dog", "🐕 dogs", re.compile(r"(dog|doge|inu|shib|puppy|pup)", re.I)),
    ("cat", "🐈 cats", re.compile(r"(cat|kitty|meow|neko)", re.I)),
    ("frog", "🐸 frogs / pepe", re.compile(r"(pepe|frog|toad)", re.I)),
    ("politics", "🏛 politics & celebrities", re.compile(r"(trump|melania|elon|musk|biden|maga|kanye|tate)", re.I)),
    ("stocks", "📈 stocks & big-tech names", re.compile(r"(tesla|nvidia|apple|google|amazon|startup|stock|nasdaq|\bapi\b)", re.I)),
    ("animal", "🦦 other animals", re.compile(r"(capybara|dolphin|monkey|ape|penguin|otter|hamster|bear|bull|fish|bird)", re.I)),
]
STOPWORDS = {"the", "coin", "token", "of", "and", "on", "in", "sol", "solana", "official", "for", "with", "a", "to", "is",
             "inu", "meme", "new", "by", "at", "my", "your", "this", "that", "it", "be", "are"}


def theme_of(symbol: str, name: str) -> str:
    text = f"{symbol or ''} {name or ''}"
    for key, _, rx in THEMES:
        if rx.search(text):
            return key
    return "other"


def theme_label(key: str) -> str:
    return next((label for k, label, _ in THEMES if k == key), "other")


def launchpad_of(dex: str, launchpad: str = "") -> str:
    """Where a coin was launched / trades, in plain words."""
    lp, dex = (launchpad or "").lower(), (dex or "").lower()
    if "pump" in lp or dex in ("pumpfun", "pumpswap"):
        return "pump.fun"
    if "bonk" in lp or "launchlab" in lp or "raydium" in dex:
        return "Raydium / LetsBonk"
    if "dbc" in lp or "meteora" in lp or "bags" in lp or "believe" in lp or "meteora" in dex:
        return "Meteora (bags / believe)"
    if "moon" in lp or "moonshot" in dex:
        return "Moonshot"
    return dex or lp or "other"


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]{3,}", (text or "").lower()) if w not in STOPWORDS]


def _pct(x: float | None, digits: int = 0) -> str:
    return "—" if x is None else f"{x * 100:.{digits}f}%"


class MarketPulse:
    def __init__(self, cfg, db, market, notify, runners=None):
        self.cfg, self.db, self.market, self.notify, self.runners = cfg, db, market, notify, runners
        self._report: tuple[float, dict] = (0.0, {})
        self._daily: tuple[float, int, list] = (0.0, 0, [])
        self._fates: dict[tuple[str, int], dict | None] = {}
        self.stream = None   # the live whale stream (set by the app): a whale count is only fair while it was up
        self.last_error = ""
        db.run("create table if not exists market_pulse(ts integer primary key, data text)")

    # -- collecting ------------------------------------------------------------------------------
    async def _json(self, url: str):
        try:
            return await asyncio.wait_for(self.market._get(url), 10)
        except Exception:
            return None

    async def snapshot(self, now: int | None = None) -> dict:
        """One reading of the market (every 15 minutes), stored for the trend."""
        now = int(now or time.time())
        snap: dict = {"ts": now}
        prices = await self._json(COINGECKO)
        if isinstance(prices, dict):
            for coin, key in (("solana", "sol"), ("bitcoin", "btc")):
                row = prices.get(coin) or {}
                if row.get("usd"):
                    snap[f"{key}_usd"] = num(row.get("usd"))
                    snap[f"{key}_24h"] = num(row.get("usd_24h_change"))
        if "sol_usd" not in snap:
            try:
                snap["sol_usd"] = await self.market.sol_usd()
            except Exception:
                pass
        fng = await self._json(FEAR_GREED)
        try:
            item = (fng or {}).get("data", [])[0]
            snap["fng"], snap["fng_label"] = int(item.get("value")), str(item.get("value_classification") or "")
        except (AttributeError, IndexError, TypeError, ValueError):
            pass
        jup = getattr(self.market, "jup", None)
        board = []
        if jup and hasattr(jup, "board"):
            try:
                board = await jup.board()
            except Exception:
                board = []
        memes = [b for b in board if not b.get("verified") or b.get("launchpad")]
        if memes:
            snap["board_n"] = len(memes)
            snap["board_up"] = sum(b["chg24"] > 0 for b in memes) / len(memes)
            snap["board_median_chg"] = statistics.median(b["chg24"] for b in memes)
            snap["board_volume"] = sum(b["vol24"] for b in memes)
            snap["board_top"] = [{k: b[k] for k in ("mint", "symbol", "name", "launchpad", "chg24", "mcap")}
                                 for b in sorted(memes, key=lambda b: -b["vol24"])[:30]]
        universe = getattr(self.runners, "universe", None) or {}
        if universe:
            infos = list(universe.values())
            snap["scanned"] = len(infos)
            snap["runners_h24"] = sum(num(i.get("change_h24")) >= 100 for i in infos)
            snap["pumping_h1"] = sum(num(i.get("change_h1")) >= 50 for i in infos)
            snap["median_h1"] = statistics.median(num(i.get("change_h1")) for i in infos)
        stream = self.stream
        if stream is not None:
            wanted = len(getattr(stream, "wanted", ()) or ())
            snap["whales_followed"] = wanted
            snap["feed_ok"] = bool(getattr(stream, "connected", False)) and \
                (not wanted or getattr(stream, "subscribed", 0) >= 0.8 * wanted)
        rate = num(getattr(self.market, "new_pools_per_hour", 0))
        if rate:
            snap["new_pools_per_hour"] = round(rate)
        self.db.run("insert or replace into market_pulse(ts, data) values(?, ?)", (now, json.dumps(snap)))
        self.db.run("delete from market_pulse where ts<?", (now - 45 * 86400,))
        return snap

    def snapshots(self, since: int, until: int | None = None) -> list[dict]:
        rows = self.db.rows("select data from market_pulse where ts>=? and ts<=? order by ts",
                            (int(since), int(until or time.time())))
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["data"]))
            except ValueError:
                pass
        return out

    # -- measuring from the bot's own data -------------------------------------------------------
    def uptime_hours(self, start: int, end: int) -> float:
        """Hours the bot was running in [start, end): the paper balance is recorded every 5 minutes while it runs."""
        points = int(self.db.scalar("select count(*) from paper_equity where ts>=? and ts<?", (start, end), default=0))
        if points:
            return min((end - start) / 3600, points / 12)
        hours = self.db.scalar("""select count(distinct ts / 3600) from (select ts from hype_seen where ts>=? and ts<?
            union all select seen_ts ts from swaps where seen_ts>=? and seen_ts<?)""", (start, end, start, end), default=0)
        return float(hours or 0)

    def fate(self, mint: str, ts: int, price: float, now: int) -> dict | None:
        """What a hype coin really did in the 2 hours after its alert: the best multiple you could have SOLD at (before
        the pool was emptied, with real liquidity under the price) and whether it was a rug. None when too few prices
        were recorded. A price printed into an emptied pool is not a pump — the same rule the paper autopilot uses."""
        key = (mint, int(ts))
        if key in self._fates:
            return self._fates[key]
        marks = [(int(m["ts"]), num(m["price"]), m["liq"]) for m in self.db.rows(
            "select ts, price, liq from price_marks where mint=? and ts>? and ts<=? order by ts", (mint, ts, ts + FATE_SPAN))]
        out = None
        if len(marks) >= 20 and price > 0:
            dead = dead_from(marks)
            usable = marks[:dead] if dead is not None else marks
            best = 0.0
            for i, (_, p, liq) in enumerate(usable):
                if liq is not None and 0 <= liq < SELLABLE_LIQ:
                    continue   # too little in the pool to sell into
                if liq is None:   # older records without liquidity: the price has to hold for two readings
                    if i + 1 >= len(usable):
                        continue
                    p = min(p, usable[i + 1][1])
                best = max(best, p / price)
            out = {"best": best, "x2": best >= 2, "rug": dead is not None}
        if now - ts > FATE_SPAN + 600:   # settled: it won't change any more
            if len(self._fates) > 20000:
                self._fates.clear()
            self._fates[key] = out
        return out

    def window(self, start: int, end: int, now: int | None = None) -> dict:
        """What the bot saw in [start, end)."""
        now = int(now or time.time())
        alerts = self.db.rows("""select a.ts, a.mint, a.price_usd, t.symbol, t.name, t.dex from alerts a
            left join tokens t on t.mint=a.mint where a.kind='HYPE' and a.price_usd>0 and a.ts>=? and a.ts<?""", (start, end))
        alerts = [dict(a, fate=self.fate(a["mint"], int(a["ts"]), num(a["price_usd"]), now)) for a in alerts]
        fates = [a["fate"] for a in alerts if a["fate"]]
        x2 = sum(f["x2"] for f in fates)
        died = sum(f["rug"] for f in fates)
        whales = self.db.row("""select coalesce(sum(side='BUY'),0) buys, coalesce(sum(side='SELL'),0) sells,
            count(distinct case when side='BUY' then mint end) coins from swaps where is_me=0 and ts>=? and ts<?""",
                             (start, end)) or {}
        paper = self.db.rows("""select size_usd, proceeds_usd from paper_trades where status='closed'
            and coalesce(interrupted,0)=0 and close_ts>=? and close_ts<?""", (start, end))
        up = self.uptime_hours(start, end)
        return {"start": start, "end": end, "up_h": up, "hype": len(alerts), "checked": len(fates), "x2": x2,
                "x2_rate": x2 / len(fates) if fates else None, "x2_per_h": x2 / up if up >= 1 else None,
                "rug_rate": died / len(fates) if fates else None, "rug_n": len(fates),
                "whale_buys": int(whales.get("buys") or 0), "whale_sells": int(whales.get("sells") or 0),
                "whale_coins": int(whales.get("coins") or 0),
                "whale_buys_per_h": int(whales.get("buys") or 0) / up if up >= 1 else None,
                "paper_n": len(paper), "paper_pnl": sum(num(p["proceeds_usd"]) - num(p["size_usd"]) for p in paper),
                "alerts": alerts}

    def daily(self, days: int = 10, now: int | None = None) -> list[dict]:
        cached = now is None and self._daily[1] == days and time.time() - self._daily[0] < REPORT_CACHE
        if cached:
            return self._daily[2]   # the dashboard asks every few seconds; the day table changes slowly
        stamp, now = time.time(), int(now or time.time())
        today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        out = []
        for back in range(days - 1, -1, -1):
            start = int(today.timestamp()) - back * 86400
            w = self.window(start, min(now, start + 86400), now)
            snaps = self.snapshots(start, start + 86400)
            last = snaps[-1] if snaps else {}
            row = {k: v for k, v in w.items() if k != "alerts"}
            row.update(day=datetime.fromtimestamp(start).strftime("%a %b %d"), sol_usd=last.get("sol_usd"),
                       sol_24h=last.get("sol_24h"), fng=last.get("fng"), board_up=last.get("board_up"))
            out.append(row)
        self._daily = (stamp, days, out)
        return out

    def narratives(self, now: int, hours: int = 24, base_days: int = 7) -> dict:
        """Themes and words in what's trending now (hype coins + Jupiter's top traded) vs the week before."""
        def coins(start: int, end: int) -> dict[str, tuple[str, str]]:
            found = {}
            for r in self.db.rows("""select h.mint, t.symbol, t.name from hype_seen h left join tokens t on t.mint=h.mint
                    where h.ts>=? and h.ts<?""", (start, end)):
                found[r["mint"]] = (r["symbol"] or "", r["name"] or "")
            for s in self.snapshots(start, end):
                for b in s.get("board_top") or []:
                    found[b["mint"]] = (b.get("symbol") or "", b.get("name") or "")
            return found
        now_c = coins(now - hours * 3600, now)
        base_c = coins(now - (base_days * 24 + hours) * 3600, now - hours * 3600)

        def shares(found: dict) -> dict[str, float]:
            counts: dict[str, int] = {}
            for sym, name in found.values():
                k = theme_of(sym, name)
                counts[k] = counts.get(k, 0) + 1
            total = max(1, len(found))
            return {k: v / total for k, v in counts.items()} | {"_n": len(found), "_counts": counts}
        a, b = shares(now_c), shares(base_c)
        themes = []
        for key, label, _ in THEMES:
            n = a["_counts"].get(key, 0)
            if not n:
                continue
            now_s, base_s = a.get(key, 0.0), b.get(key, 0.0)
            themes.append({"key": key, "label": label, "n": n, "share": now_s, "base_share": base_s,
                           "rising": n >= 5 and now_s >= 0.03 and now_s >= 1.5 * max(base_s, 0.01)})
        themes.sort(key=lambda t: -t["n"])
        words_now: dict[str, int] = {}
        for sym, name in now_c.values():
            for w in set(_words(f"{sym} {name}")):
                words_now[w] = words_now.get(w, 0) + 1
        words_base: dict[str, int] = {}
        for sym, name in base_c.values():
            for w in set(_words(f"{sym} {name}")):
                words_base[w] = words_base.get(w, 0) + 1
        scale = max(1, len(base_c)) / max(1, len(now_c))
        words = sorted(((w, n) for w, n in words_now.items() if n >= 3 and n * scale >= 2 * words_base.get(w, 0)),
                       key=lambda wn: -wn[1])[:6]
        return {"coins": len(now_c), "base_coins": len(base_c), "themes": themes, "rising_words": words}

    def launchpads(self, now: int, days: int = 3) -> list[dict]:
        """Hype coins by launchpad: how many, how many really doubled, how many were rugs."""
        by: dict[str, dict] = {}
        for a in self.window(now - days * 86400, now, now)["alerts"]:
            lp = launchpad_of(a["dex"] or "")
            row = by.setdefault(lp, {"launchpad": lp, "n": 0, "x2": 0, "checked": 0, "died": 0})
            row["n"] += 1
            if a["fate"]:
                row["checked"] += 1
                row["x2"] += a["fate"]["x2"]
                row["died"] += a["fate"]["rug"]
        out = sorted(by.values(), key=lambda r: -r["n"])
        for r in out:
            r["x2_rate"] = r["x2"] / r["checked"] if r["checked"] else None
            r["rug_rate"] = r["died"] / r["checked"] if r["checked"] else None
        return out

    def best_hours(self, now: int, days: int = 7) -> list[int]:
        """Local hours when the hype coins that really doubled were alerted (most first)."""
        counts: dict[int, int] = {}
        for a in self.window(now - days * 86400, now, now)["alerts"]:
            if a["fate"] and a["fate"]["x2"]:
                h = datetime.fromtimestamp(a["ts"]).hour
                counts[h] = counts.get(h, 0) + 1
        return [h for h, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n >= 2][:3]

    # -- the call ------------------------------------------------------------------------------------
    def report(self, now: int | None = None, fresh: bool = False) -> dict:
        now = int(now or time.time())
        if not fresh and self._report[1] and now - self._report[0] < REPORT_CACHE:
            return self._report[1]
        today = self.window(now - 86400, now, now)
        base = self.window(now - 8 * 86400, now - 86400, now)
        snaps = self.snapshots(now - 3 * 3600, now)
        latest = snaps[-1] if snaps else {}
        week_snaps = self.snapshots(now - 8 * 86400, now - 86400)
        score, reasons, lookouts = 0, [], []

        def ratio(a, b):
            return a / b if a is not None and b else None

        # the SHARE of measured hype coins that really doubled — a count per hour would move with the filter's settings
        runners = ratio(today["x2_rate"], base["x2_rate"]) if base["checked"] >= 20 and today["checked"] >= 8 else None
        if runners is not None:
            if runners >= 1.5:
                score += 2
            elif runners >= 1.1:
                score += 1
            elif runners <= 0.5:
                score -= 2
            elif runners <= 0.8:
                score -= 1
            word = "more" if runners >= 1 else "fewer"
            reasons.append(f"{word} real pumps: {_pct(today['x2_rate'])} of hype coins really doubled (2x you could sell) "
                           f"vs {_pct(base['x2_rate'])} this week")
            if runners <= 0.8:
                lookouts.append(f"Fewer real pumps: {today['x2']} hype coins really doubled in the last 24h "
                                f"({_pct(today['x2_rate'])} of those measured) vs {_pct(base['x2_rate'])} this week — take "
                                "profit faster, use a smaller size")
        sol = latest.get("sol_24h")
        if sol is not None:
            if sol >= 5:
                score += 1
            elif sol <= -10:
                score -= 2
            elif sol <= -5:
                score -= 1
            reasons.append(f"SOL {sol:+.1f}% in 24h")
            if sol <= -5:
                lookouts.append(f"SOL is down {sol:.1f}% today — memecoins usually fall harder than SOL; wait for SOL to "
                                "steady before sizing up")
        fng = latest.get("fng")
        if fng is not None:
            if fng >= 60:
                score += 1
            elif fng <= 25:
                score -= 1
            reasons.append(f"Fear & Greed {fng} ({latest.get('fng_label', '').lower()})")
            if fng <= 25:
                lookouts.append(f"Extreme fear ({fng}/100): sellers are in control — most pumps get sold into fast")
        up = latest.get("board_up")
        if up is not None:
            if up >= 0.55:
                score += 1
            elif up <= 0.3:
                score -= 1
            reasons.append(f"{up * 100:.0f}% of the top-traded memecoins are up on the day")
        if today["rug_rate"] is not None and today["rug_n"] >= 8:
            if today["rug_rate"] >= 0.5 or (base["rug_rate"] is not None and today["rug_rate"] >= base["rug_rate"] + 0.15):
                score -= 1
            reasons.append(f"rugs {_pct(today['rug_rate'])} of hype coins")
            if base["rug_rate"] is not None and today["rug_rate"] >= base["rug_rate"] + 0.15:
                lookouts.append(f"More rugs: {_pct(today['rug_rate'])} of today's hype coins had their pool emptied within "
                                f"2 hours (vs {_pct(base['rug_rate'])} this week) — stay away from pools under 30 min old")
            elif today["rug_rate"] >= 0.4:
                lookouts.append(f"{_pct(today['rug_rate'])} of today's hype coins were rugs (pool emptied within 2 hours)")
        feed = [bool(s["feed_ok"]) for s in self.snapshots(now - 86400, now) if "feed_ok" in s]
        feed_up = sum(feed) / len(feed) if feed else None   # None: not measured (readings from before v28.4)
        wb = ratio(today["whale_buys_per_h"], base["whale_buys_per_h"])
        if wb is not None and base["whale_buys"] >= 10:
            pace = base["whale_buys"] / max(1.0, base["up_h"]) * max(1.0, today["up_h"])
            if feed_up is not None and feed_up < 0.75:
                lookouts.append(f"The whale feed was down {(1 - feed_up) * 100:.0f}% of the last 24h — today's whale numbers "
                                "aren't comparable (/health)")
            elif wb <= 0.5:
                score -= feed_up is not None
                lookouts.append(f"Your whales are sitting out: {today['whale_buys']} buys in 24h vs {pace:.0f} at last week's "
                                "pace" + ("" if feed_up is not None else " (or the whale feed was down — /health shows it)"))
            elif wb >= 1.5:
                score += feed_up is not None
                lookouts.append(f"Your whales are active: {today['whale_buys']} buys across {today['whale_coins']} coins in 24h")
        breadth = [s["runners_h24"] / s["scanned"] for s in week_snaps if s.get("scanned")]
        if latest.get("scanned") and len(breadth) >= 24 and statistics.median(breadth):
            typical = statistics.median(breadth)
            share = latest["runners_h24"] / latest["scanned"]
            if share >= 1.5 * typical:
                score += 1
            elif share <= 0.6 * typical:
                score -= 1
            reasons.append(f"{share * 100:.0f}% of the coins the scanners watch are up 100%+ on the day "
                           f"(typical this week {typical * 100:.0f}%)")
        if latest.get("new_pools_per_hour") and week_snaps:
            past = [s["new_pools_per_hour"] for s in week_snaps if s.get("new_pools_per_hour")]
            if past:
                pace = latest["new_pools_per_hour"] / statistics.median(past)
                if pace <= 0.6 or pace >= 1.5:
                    lookouts.append(f"Launch pace {latest['new_pools_per_hour']:.0f} new pools/hour "
                                    f"({'down' if pace < 1 else 'up'} {abs(pace - 1) * 100:.0f}% on the week)")
        narr = self.narratives(now)
        for t in narr["themes"]:
            if t["rising"]:
                lookouts.append(f"Rising theme: {t['label']} — {t['n']} of {narr['coins']} trending coins "
                                f"({t['share'] * 100:.0f}%, was {t['base_share'] * 100:.0f}% last week)")
        if narr["rising_words"]:
            lookouts.append("Words showing up a lot: " + ", ".join(f"{w} ×{n}" for w, n in narr["rising_words"]))
        pads = self.launchpads(now)
        for p in pads:
            if p["checked"] >= 8 and (p["rug_rate"] or 0) >= 0.6:
                lookouts.append(f"{p['launchpad']}: {_pct(p['rug_rate'])} of its hype coins were rugs in 3 days — "
                                "be extra careful with fresh launches there")
            elif p["checked"] >= 6 and (p["x2_rate"] or 0) >= 0.35:
                lookouts.append(f"{p['launchpad']}: {_pct(p['x2_rate'])} of its hype coins really doubled in 3 days")
        hours = self.best_hours(now)
        if hours:
            lookouts.append("Coins that really doubled this week were mostly alerted around "
                            + ", ".join(f"{h:02d}:00" for h in hours) + " (your time)")
        enough = base["checked"] >= 20 or bool(snaps)
        regime = ("learning" if not enough else "hot" if score >= 3 else "healthy" if score >= 1
                  else "choppy" if score >= -1 else "cold")
        advice = {"hot": "More coins are running than usual — the bot's alerts tend to work better now. Normal size.",
                  "healthy": "A normal market: follow the plan, normal size.",
                  "choppy": "Mixed signals: take profit early, smaller size, prefer coins your whales are in.",
                  "cold": "Cold market: few real pumps, more rugs. Paper only or very small size; wait for whale "
                          "confluence. The live autopilot holds new buys (MARKET_GUARD).",
                  "learning": "Still learning this market — it needs a day of readings."}[regime]
        rep = {"ts": now, "regime": regime, "label": REGIMES[regime], "score": score, "reasons": reasons,
               "advice": advice, "lookouts": lookouts,
               "today": {k: v for k, v in today.items() if k != "alerts"},
               "week": {k: v for k, v in base.items() if k != "alerts"},
               "latest": {k: v for k, v in latest.items() if k != "board_top"},
               "board_top": (latest.get("board_top") or [])[:10], "narratives": narr, "launchpads": pads,
               "best_hours": hours}
        self._report = (now, rep)
        return rep

    def regime(self) -> str:
        return (self._report[1] or {}).get("regime", "learning")

    # -- running ---------------------------------------------------------------------------------------
    async def tick(self, now: int | None = None) -> None:
        now = int(now or time.time())
        last = int(num(self.db.get_meta("pulse_last_snap", "0")))
        if now - last >= SNAP_EVERY:
            self.db.set_meta("pulse_last_snap", now)
            await self.snapshot(now)
            rep = self.report(now, fresh=True)
            prev = self.db.get_meta("market_regime")
            if prev and prev != rep["regime"] and rep["regime"] != "learning" \
                    and now - int(num(self.db.get_meta("market_regime_ts", "0"))) >= 6 * 3600:
                self.db.set_meta("market_regime_ts", now)
                await self.notify(f"🌡 <b>The market turned: {REGIMES.get(prev, prev)} → {rep['label']}</b>\n"
                                  + esc("; ".join(rep["reasons"][:3])) + f"\n{esc(rep['advice'])}\n/market for the details",
                                  silent=True, kind="INFO")
            if rep["regime"] != "learning":
                self.db.set_meta("market_regime", rep["regime"])
        hour = int(self.cfg.get("MARKET_BRIEF_HOUR"))
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if hour >= 0 and datetime.fromtimestamp(now).hour == hour and self.db.get_meta("market_brief_day") != day:
            self.db.set_meta("market_brief_day", day)
            await self.notify(format_report(self.report(now, fresh=True)), silent=True, kind="DAILY")

    async def run(self) -> None:
        await asyncio.sleep(90)   # let the first scans fill the universe
        while True:
            try:
                await self.tick()
            except Exception as exc:
                self.last_error = f"{time.strftime('%H:%M:%S')} {type(exc).__name__}: {exc}"
            await asyncio.sleep(60)


def format_report(r: dict) -> str:
    """/market and the morning brief."""
    if not r:
        return "🌡 The market pulse has no readings yet."
    t, w, lt = r["today"], r["week"], r.get("latest") or {}
    lines = [f"<b>🌡 Market: {r['label']}</b>", esc(r["advice"])]
    if r["reasons"]:
        lines.append("Why: " + esc("; ".join(r["reasons"])))
    majors = []
    if lt.get("sol_usd"):
        majors.append(f"SOL ${lt['sol_usd']:,.2f}" + (f" ({lt['sol_24h']:+.1f}%)" if lt.get("sol_24h") is not None else ""))
    if lt.get("btc_24h") is not None:
        majors.append(f"BTC {lt['btc_24h']:+.1f}%")
    if lt.get("fng") is not None:
        majors.append(f"Fear & Greed {lt['fng']}")
    if lt.get("board_up") is not None:
        majors.append(f"top memecoins up on the day: {lt['board_up'] * 100:.0f}%")
    if lt.get("new_pools_per_hour"):
        majors.append(f"{lt['new_pools_per_hour']:.0f} new pools/hour")
    if majors:
        lines.append(" · ".join(majors))
    lines.append(f"\n<b>Last 24h</b> (bot running {t['up_h']:.0f}h): {t['hype']} hype coins · {t['x2']} really doubled "
                 f"({_pct(t['x2_rate'])}) · rugs {_pct(t['rug_rate'])} · whales bought {t['whale_buys']} times · paper "
                 f"{t['paper_pnl']:+,.0f}$ on {t['paper_n']} trades")
    lines.append(f"<b>Week before</b> (running {w['up_h']:.0f}h): {w['hype']} hype coins · {w['x2']} really doubled "
                 f"({_pct(w['x2_rate'])}) · rugs {_pct(w['rug_rate'])} · whales bought {w['whale_buys']} times")
    if r["lookouts"]:
        lines.append("\n<b>👀 What to look out for</b>")
        lines += [f"• {esc(x)}" for x in r["lookouts"][:8]]
    themes = [x for x in r["narratives"]["themes"] if x["key"] != "other"][:4]
    if themes:
        lines.append("\n<b>Trending themes</b> (24h): " + " · ".join(
            f"{esc(x['label'])} {x['n']}{' ↑' if x['rising'] else ''}" for x in themes))
    top = r.get("board_top") or []
    if top:
        lines.append("<b>Most traded memecoins now</b>: " + ", ".join(
            f"${esc(b['symbol'])} {b['chg24']:+.0f}%" for b in top[:6]))
    return "\n".join(lines)
