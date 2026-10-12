"""Jupiter token data (free): which coins are trending on Jupiter, and how much of a coin's buying is REAL.

Jupiter scores every token's trading for bots and wash trading — the "organic score" (0-100) — counts organic buyers
and holders, and audits the dev (how many coins this dev has minted) and the top holders. The hype scanner uses it:
  🌿 real buyers  — organic score 60+ with 10+ organic buyers in the hour: people, not a bot loop
  🤖 bot volume   — organic score under 25 while the "rush" fires: the buying is mostly bots / wash trading
  🏭 serial dev   — the dev has minted 20+ coins: a launch-and-dump factory
  ⚠️ suspicious   — Jupiter's own audit flags the token
  𝕏 smart CT     — Crypto-Twitter accounts Jupiter tracks liked it (only when Jupiter includes it)
Jupiter's trending and top-organic lists also feed the scanner's universe, next to DexScreener and GeckoTerminal.

No key needed (a slow free allowance); JUPITER_API_KEY in .env (free at developers.jup.ag) raises it. The bot makes
about 3 calls a minute.
"""
from __future__ import annotations

import asyncio
import os
import time

import aiohttp

from .util import is_address, num

BASES = ("https://lite-api.jup.ag/tokens/v2", "https://api.jup.ag/tokens/v2")
LIST_EVERY = 60
STATS_FOR = 60
SPACING = 2.2           # keyless Jupiter allows about one call every two seconds
BATCH = 100


def parse(tok: dict) -> dict:
    s5, s1 = tok.get("stats5m") or {}, tok.get("stats1h") or {}
    audit = tok.get("audit") or {}
    return {"organic": num(tok.get("organicScore"), -1), "label": str(tok.get("organicScoreLabel") or ""),
            "holders": int(num(tok.get("holderCount"))), "top_holders_pct": num(audit.get("topHoldersPercentage"), -1),
            "dev_mints": int(num(audit.get("devMints"))), "sus": bool(audit.get("isSus")),
            "organic_buyers_1h": int(num(s1.get("numOrganicBuyers"))), "organic_buyers_5m": int(num(s5.get("numOrganicBuyers"))),
            "traders_1h": int(num(s1.get("numTraders"))), "smart_ct_likes": int(num(tok.get("smartCtLikes"))),
            "ct_likes": int(num(tok.get("ctLikes")))}


class JupiterTokens:
    def __init__(self, session):
        self.session = session
        self.key = os.environ.get("JUPITER_API_KEY", "").strip()
        self.bases = (BASES[1],) if self.key else BASES
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._lists: tuple[float, dict] = (0.0, {})
        self._stats: dict[str, tuple[float, dict]] = {}
        self.calls = 0
        self.last_error = ""

    async def _get(self, path: str):
        """GET from Jupiter's token API (falls back to the other address if one stops answering)."""
        async with self._lock:
            wait = self._last + SPACING - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.time()
            headers = {"accept": "application/json", **({"x-api-key": self.key} if self.key else {})}
            for base in self.bases:
                self.calls += 1
                try:
                    async with self.session.get(f"{base}{path}", headers=headers,
                                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            return await resp.json(content_type=None)
                        self.last_error = f"Jupiter HTTP {resp.status}"
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                    self.last_error = f"Jupiter {type(exc).__name__}"
            return None

    async def lists(self) -> dict[str, list[str]]:
        """Trending and top-organic coins on Jupiter in the last 5 minutes (cached a minute)."""
        if time.time() - self._lists[0] < LIST_EVERY:
            return dict(self._lists[1])
        out = {}
        for name, category in (("jup_trending", "toptrending"), ("jup_organic", "toporganicscore")):
            data = await self._get(f"/{category}/5m?limit=50")
            out[name] = [str(t.get("id")) for t in data if isinstance(t, dict) and is_address(str(t.get("id") or ""))] \
                if isinstance(data, list) else []
            for t in data if isinstance(data, list) else []:
                if isinstance(t, dict) and t.get("id"):
                    self._stats[str(t["id"])] = (time.time(), parse(t))   # the list already carries each coin's data
        self._lists = (time.time(), out)
        return dict(out)

    async def stats(self, mints: list[str]) -> dict[str, dict]:
        """Organic score, organic buyers, holders and dev audit for up to 100 coins (one call, cached a minute)."""
        now = time.time()
        fresh = {m: v for m, (ts, v) in ((m, self._stats[m]) for m in mints if m in self._stats) if now - ts < STATS_FOR}
        need = [m for m in mints if m not in fresh and is_address(m)][:BATCH]
        if need:
            data = await self._get(f"/search?query={','.join(need)}")
            for t in data if isinstance(data, list) else []:
                if isinstance(t, dict) and t.get("id"):
                    parsed = parse(t)
                    self._stats[str(t["id"])] = (now, parsed)
                    if str(t["id"]) in need:
                        fresh[str(t["id"])] = parsed
        if len(self._stats) > 5000:   # keep the cache small
            self._stats = {m: v for m, v in self._stats.items() if now - v[0] < STATS_FOR}
        return fresh
