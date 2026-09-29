"""Market data: prices, market cap and liquidity (DexScreener), SOL/USD, and token safety.

Missing data is kept as "unknown" (liquidity = -1), never as zero, so an API hiccup can't
look like a rug or a -100% move.
"""
from __future__ import annotations

import asyncio
import time

import aiohttp

from .swaps import USDC, USDT, WSOL
from .util import is_address, num

DEX = "https://api.dexscreener.com"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
COINBASE_SOL = "https://api.coinbase.com/v2/prices/SOL-USD/spot"

# Coins that are never "early whale buys": SOL itself, stables, liquid staking and majors.
IGNORED_MINTS = {
    WSOL, USDC, USDT,
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # jitoSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",   # bSOL
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v",   # jupSOL
    "27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4",  # JLP
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",   # JUP
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",  # WBTC
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # WETH
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",   # cbBTC
}


def pair_to_info(pair: dict) -> dict:
    base = pair.get("baseToken") or {}
    liq = (pair.get("liquidity") or {}).get("usd")
    txns = (pair.get("txns") or {}).get("h1") or {}
    change = pair.get("priceChange") or {}
    info = pair.get("info") or {}
    return {
        "mint": str(base.get("address") or ""),
        "symbol": str(base.get("symbol") or "?")[:24],
        "name": str(base.get("name") or "")[:48],
        "price_usd": num(pair.get("priceUsd")),
        "mc_usd": num(pair.get("marketCap")) or num(pair.get("fdv")),
        "liquidity_usd": num(liq, -1) if liq is not None else -1.0,
        "pair_address": str(pair.get("pairAddress") or ""),
        "dex": str(pair.get("dexId") or ""),
        "pair_created_ts": int(num(pair.get("pairCreatedAt")) / 1000),
        "url": str(pair.get("url") or ""),
        "image": str(info.get("imageUrl") or ""),
        "change_m5": num(change.get("m5")),
        "change_h1": num(change.get("h1")),
        "change_h24": num(change.get("h24")),
        "buys_h1": int(num(txns.get("buys"))),
        "sells_h1": int(num(txns.get("sells"))),
        "volume_h1": num((pair.get("volume") or {}).get("h1")),
        "volume_h24": num((pair.get("volume") or {}).get("h24")),
    }


def best_pairs(pairs: list) -> dict[str, dict]:
    """For each base token keep the pair with the deepest known liquidity."""
    best: dict[str, dict] = {}
    for pair in pairs or []:
        if not isinstance(pair, dict) or str(pair.get("chainId") or "solana") != "solana":
            continue
        info = pair_to_info(pair)
        mint = info["mint"]
        if not mint or info["price_usd"] <= 0:
            continue
        cur = best.get(mint)
        if cur is None or info["liquidity_usd"] > cur["liquidity_usd"]:
            best[mint] = info
    return best


class Market:
    def __init__(self, session: aiohttp.ClientSession, rpc, db):
        self.session = session
        self.rpc = rpc
        self.db = db
        self._cache: dict[str, tuple[float, dict]] = {}
        self._sol = (0.0, 0.0)
        self._rug_down_until = 0.0
        self.errors = 0
        self.last_error = ""

    async def _get(self, url: str, timeout: float = 8.0):
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                                        headers={"accept": "application/json"}) as resp:
                if resp.status != 200:
                    self.errors += 1
                    self.last_error = f"{url.split('/')[2]} HTTP {resp.status}"
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            self.errors += 1
            self.last_error = f"{url.split('/')[2]} {type(exc).__name__}"
            return None

    # -- SOL price ------------------------------------------------------------------------
    async def sol_usd(self) -> float:
        price, ts = self._sol
        if price > 0 and time.time() - ts < 30:
            return price
        data = await self._get(f"{DEX}/tokens/v1/solana/{WSOL}")
        fresh = 0.0
        if isinstance(data, list):
            quotes = [p for p in data if isinstance(p, dict)
                      and (p.get("baseToken") or {}).get("address") == WSOL
                      and (p.get("quoteToken") or {}).get("address") in (USDC, USDT)]
            quotes.sort(key=lambda p: num((p.get("liquidity") or {}).get("usd")), reverse=True)
            if quotes:
                fresh = num(quotes[0].get("priceUsd"))
        if fresh <= 0:
            data = await self._get(COINBASE_SOL)
            fresh = num(((data or {}).get("data") or {}).get("amount")) if isinstance(data, dict) else 0.0
        if fresh > 0:
            self._sol = (fresh, time.time())
            self.db.set_meta("sol_usd", f"{fresh:.4f}")
            return fresh
        return price or num(self.db.get_meta("sol_usd"), 0.0)

    # -- token market data -----------------------------------------------------------------
    async def tokens(self, mints, max_age: float = 20.0) -> dict[str, dict]:
        wanted = [m for m in dict.fromkeys(mints) if is_address(m) and m not in IGNORED_MINTS]
        now = time.time()
        out = {m: self._cache[m][1] for m in wanted if m in self._cache and now - self._cache[m][0] <= max_age}
        missing = [m for m in wanted if m not in out]
        chunks = [missing[i:i + 30] for i in range(0, len(missing), 30)]
        results = await asyncio.gather(*(self._get(f"{DEX}/tokens/v1/solana/{','.join(c)}") for c in chunks))
        for chunk, data in zip(chunks, results):
            found = best_pairs(data if isinstance(data, list) else [])
            for mint in chunk:
                info = found.get(mint)
                if info:
                    self._cache[mint] = (now, info)
                    out[mint] = info
                    self._save(info)
        return out

    async def token(self, mint: str, max_age: float = 20.0) -> dict:
        return (await self.tokens([mint], max_age)).get(mint, {})

    def cached(self, mint: str) -> dict:
        hit = self._cache.get(mint)
        if hit:
            return hit[1]
        row = self.db.row("select * from tokens where mint=?", (mint,))
        return row or {}

    def _save(self, info: dict) -> None:
        self.db.run("""insert into tokens(mint,symbol,name,price_usd,mc_usd,liquidity_usd,pair_address,dex,
                pair_created_ts,image,url,updated_ts) values(?,?,?,?,?,?,?,?,?,?,?,?)
            on conflict(mint) do update set symbol=excluded.symbol,name=excluded.name,price_usd=excluded.price_usd,
                mc_usd=excluded.mc_usd,liquidity_usd=excluded.liquidity_usd,pair_address=excluded.pair_address,
                dex=excluded.dex,pair_created_ts=excluded.pair_created_ts,image=excluded.image,url=excluded.url,
                updated_ts=excluded.updated_ts""",
            (info["mint"], info["symbol"], info["name"], info["price_usd"], info["mc_usd"], info["liquidity_usd"],
             info["pair_address"], info["dex"], info["pair_created_ts"], info["image"], info["url"], int(time.time())))

    def symbol(self, mint: str) -> str:
        return str(self.cached(mint).get("symbol") or "") or mint[:4]

    # -- safety -----------------------------------------------------------------------------
    async def safety(self, mint: str, max_age: float = 6 * 3600) -> dict:
        """Mint/freeze authority + supply straight from the chain (cached)."""
        row = self.db.row("select mint_authority,freeze_authority,decimals,supply,safety_ts from tokens where mint=?",
                          (mint,))
        if row and row["safety_ts"] and time.time() - row["safety_ts"] < max_age:
            return {"mint_authority": row["mint_authority"] or "", "freeze_authority": row["freeze_authority"] or "",
                    "decimals": row["decimals"], "supply": row["supply"], "ok": True}
        info = await self.rpc.mint_info(mint)
        if not info:
            return {"ok": False}
        self.db.run("""insert into tokens(mint,mint_authority,freeze_authority,decimals,supply,safety_ts)
            values(?,?,?,?,?,?) on conflict(mint) do update set mint_authority=excluded.mint_authority,
            freeze_authority=excluded.freeze_authority,decimals=excluded.decimals,supply=excluded.supply,
            safety_ts=excluded.safety_ts""",
            (mint, info["mint_authority"], info["freeze_authority"], info["decimals"], info["supply"], int(time.time())))
        return {**info, "ok": True}

    async def rugcheck(self, mint: str, timeout: float = 3.0) -> dict | None:
        """RugCheck summary: {'score': 0-100 (higher = riskier), 'danger': [...], 'warn': [...]}."""
        if time.time() < self._rug_down_until:
            return None
        data = await self._get(RUGCHECK.format(mint), timeout=timeout)
        if not isinstance(data, dict):
            self._rug_down_until = time.time() + 120
            return None
        danger, warn = [], []
        for risk in data.get("risks") or []:
            if not isinstance(risk, dict):
                continue
            name = str(risk.get("name") or "risk")[:60]
            (danger if str(risk.get("level") or "").lower() in {"danger", "critical"} else warn).append(name)
        score = data.get("score_normalised")
        return {"score": num(score, -1) if score is not None else -1, "danger": danger, "warn": warn}

    # -- lookups --------------------------------------------------------------------------
    async def resolve_mint(self, address: str) -> str:
        """Accept a token mint or a pool/pair address (DexScreener, Axiom and Photon URLs use pairs)."""
        if not is_address(address):
            return ""
        if self.db.scalar("select 1 from swaps where mint=? limit 1", (address,)) or \
                self.db.scalar("select 1 from tokens where mint=? and symbol<>'' limit 1", (address,)):
            return address
        known = self.db.scalar("select mint from tokens where pair_address=?", (address,))
        if known:
            return known
        if await self.token(address, max_age=300):
            return address
        data = await self._get(f"{DEX}/latest/dex/pairs/solana/{address}")
        pairs = (data or {}).get("pairs") if isinstance(data, dict) else None
        if isinstance(pairs, list) and pairs:
            mint = str(((pairs[0] or {}).get("baseToken") or {}).get("address") or "")
            if is_address(mint):
                return mint
        return address

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        from urllib.parse import quote
        data = await self._get(f"{DEX}/latest/dex/search?q={quote(query)}")
        pairs = (data or {}).get("pairs") if isinstance(data, dict) else None
        found = best_pairs(pairs or [])
        return sorted(found.values(), key=lambda i: i["liquidity_usd"], reverse=True)[:limit]
