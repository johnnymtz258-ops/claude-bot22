"""Is there a real community behind a coin, or just a few big wallets?

Signals (all from free sources):
- holder concentration: how much of the supply the 10 biggest *wallets* hold. Pools, bonding
  curves and lockers are program accounts (addresses off the ed25519 curve), so they are left out
  — otherwise every coin would look "90% held by one holder" (its own liquidity pool).
- activity: number of trades in the last 24h and buys in the last hour (DexScreener).
- socials: X / Telegram / website listed on DexScreener.

Label: STRONG, OK, THIN, or WHALE-ONLY (a few wallets hold most of it and few people trade it).
"""
from __future__ import annotations

import time

from .util import num

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def is_on_curve(address: str) -> bool:
    """True for normal wallets (ed25519 public keys); False for program-derived addresses."""
    try:
        data = _b58decode(address)
    except ValueError:
        return False
    if len(data) != 32:
        return False
    y = int.from_bytes(data, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    x2 = (y2 - 1) * pow(_D * y2 + 1, _P - 2, _P) % _P
    return x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1  # Euler's criterion: x2 has a square root


async def holder_concentration(rpc, mint: str, supply: float) -> dict:
    """Share of supply held by the top 1 / top 10 wallets (program accounts excluded)."""
    largest = await rpc.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    rows = (largest or {}).get("value") if isinstance(largest, dict) else None
    if not rows or supply <= 0:
        return {"ok": False}
    accounts = [r["address"] for r in rows if r.get("address")]
    info = await rpc.call("getMultipleAccounts", [accounts, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    values = (info or {}).get("value") if isinstance(info, dict) else None
    if not isinstance(values, list):
        return {"ok": False}
    by_owner: dict[str, float] = {}
    for row, acct in zip(rows, values):
        try:
            owner = acct["data"]["parsed"]["info"]["owner"]
        except (KeyError, TypeError):
            continue
        if not is_on_curve(owner):
            continue  # pool, bonding curve, locker or other program account
        by_owner[owner] = by_owner.get(owner, 0.0) + num(row.get("uiAmountString") or row.get("uiAmount"))
    top = sorted(by_owner.values(), reverse=True)
    return {"ok": True, "top1_pct": (top[0] / supply * 100) if top else 0.0,
            "top10_pct": sum(top[:10]) / supply * 100, "wallets_seen": len(top)}


def socials_count(info: dict) -> int:
    return int(num(info.get("socials_count")))


def assess(info: dict, conc: dict | None) -> dict:
    """Community label + grading points + one readable line."""
    trades = int(num(info.get("buys_h24")) + num(info.get("sells_h24")))
    buys_h1 = int(num(info.get("buys_h1")))
    socials = socials_count(info)
    top10 = num((conc or {}).get("top10_pct"), -1) if (conc or {}).get("ok") else -1.0
    if top10 >= 60 or (top10 >= 45 and trades < 300):
        label, points = "WHALE-ONLY", -2
    elif trades < 300 or top10 >= 45 or (socials == 0 and trades < 1500):
        label, points = "THIN", -1
    elif trades >= 2000 and socials >= 2 and 0 <= top10 < 30 and buys_h1 >= 100:
        label, points = "STRONG", 1
    else:
        label, points = "OK", 0
    parts = [f"{trades:,} trades 24h", f"{socials} social link{'s' if socials != 1 else ''}"]
    if top10 >= 0:
        parts.append(f"top-10 wallets hold {top10:.0f}%")
    return {"label": label, "points": points, "trades_24h": trades, "socials": socials, "top10_pct": top10,
            "line": f"Community {label}: " + " · ".join(parts)}


class CommunityChecker:
    """Cached holder checks (two RPC calls per coin, refreshed every 10 minutes)."""

    def __init__(self, rpc, market):
        self.rpc = rpc
        self.market = market
        self._cache: dict[str, tuple[float, dict]] = {}

    async def check(self, mint: str, info: dict) -> dict:
        hit = self._cache.get(mint)
        if hit and time.time() - hit[0] < 600:
            conc = hit[1]
        else:
            price, mcap = num(info.get("price_usd")), num(info.get("mc_usd"))
            supply = mcap / price if price > 0 and mcap > 0 else num((await self.market.safety(mint)).get("supply"))
            conc = await holder_concentration(self.rpc, mint, supply)
            self._cache[mint] = (time.time(), conc)
        return assess(info, conc)
