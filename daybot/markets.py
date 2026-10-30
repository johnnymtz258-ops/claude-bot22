"""Public prices for the "Bitcoin Up or Down — 15 minutes" markets, so DayBot can compare its odds with what the
market charges instead of asking you to type the price. Read-only, no keys.

  Polymarket  gamma-api.polymarket.com/events?slug=btc-updown-15m-<window start, unix>
  Kalshi      api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC15M

Both parsers accept the response shapes seen so far (prices as cents or dollars, strings or numbers, lists encoded
as JSON text) and return None rather than guess. Prices are in CENTS per contract (a contract pays 100¢ if it wins).
A fetch that fails is simply "no quote": typing the price by hand (/btc15 62) always still works.
"""
from __future__ import annotations

import json
import time

import aiohttp

from .alpaca import parse_ts
from .util import num

POLY_URL = "https://gamma-api.polymarket.com/events"
KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_SERIES = "KXBTC15M"
TIMEOUT = aiohttp.ClientTimeout(total=6)
VENUES = ("polymarket", "kalshi")
VENUE_NAME = {"polymarket": "Polymarket", "kalshi": "Kalshi"}


def _json_list(value) -> list:
    """Polymarket sends some lists as JSON text: '["Up","Down"]'."""
    if isinstance(value, list):
        return value
    try:
        out = json.loads(value) if isinstance(value, str) else []
        return out if isinstance(out, list) else []
    except ValueError:
        return []


def _dollars(value) -> float | None:
    """A price quoted in dollars (0.57 or '0.57') as cents; None if it isn't a price."""
    v = num(value, -1)
    return v * 100 if 0 <= v <= 1 and value not in (None, "") else None


def _cents(value) -> float | None:
    """A price already in cents (57 or '57'); None if it isn't a price."""
    v = num(value, -1)
    return v if 0 <= v <= 100 and value not in (None, "") else None


def _find_key(obj, needle: str):
    """The first value whose key contains `needle` (case-insensitive), searching nested dicts and lists."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if needle in str(k).lower().replace("_", "") and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            hit = _find_key(v, needle)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_key(v, needle)
            if hit is not None:
                return hit
    return None


def quote(venue: str, ua, ub, da, db, strike=None, approx: bool = False) -> dict | None:
    """A normalized quote — prices in CENTS (None where unknown) — or None unless an Up and a Down price are there.
    Missing sides are filled from the other side (Up ask + Down bid = 100)."""
    ua = ua if ua is not None else (100 - db if db is not None else None)
    da = da if da is not None else (100 - ub if ub is not None else None)
    ub = ub if ub is not None else (100 - da if da is not None else None)
    db = db if db is not None else (100 - ua if ua is not None else None)
    if ua is None or da is None or not (0 < ua < 100 and 0 < da < 100):
        return None
    return {"venue": venue, "up_ask": ua, "up_bid": ub, "dn_ask": da, "dn_bid": db,
            "strike": num(strike) or None, "approx": approx, "ts": time.time()}


def parse_polymarket(events, start: int) -> dict | None:
    """The window starting at `start` out of gamma's /events answer."""
    rows = events if isinstance(events, list) else [events] if isinstance(events, dict) else []
    slug = f"btc-updown-15m-{start}"
    event = next((e for e in rows if isinstance(e, dict) and e.get("slug") == slug), None) or \
        next((e for e in rows if isinstance(e, dict)), None)
    if not event:
        return None
    strike = _find_key(event, "pricetobeat") or _find_key(event, "strike")
    for m in event.get("markets") or []:
        outcomes = [str(o).lower() for o in _json_list(m.get("outcomes"))]
        prices = _json_list(m.get("outcomePrices"))
        if "up" in outcomes and len(prices) == len(outcomes):
            mid_up = _dollars(prices[outcomes.index("up")])
            ask, bid = _dollars(m.get("bestAsk")), _dollars(m.get("bestBid"))
            if ask and bid is not None:
                return quote("polymarket", ask, bid, None, None, strike)
            if mid_up is not None:                                   # only mid prices: assume a 1¢ spread
                return quote("polymarket", mid_up + 0.5, mid_up - 0.5, None, None, strike, approx=True)
    return None


def parse_kalshi(payload, end: int) -> dict | None:
    """The market that closes at `end` out of Kalshi's /markets answer."""
    markets = (payload or {}).get("markets") if isinstance(payload, dict) else None
    best = None
    for m in markets or []:
        if not isinstance(m, dict):
            continue
        close = parse_ts(m.get("close_time") or m.get("expected_expiration_time"))
        if close == end:
            best = m
            break
    if not best:
        return None

    def px(name: str):
        """Kalshi sends prices as cents (yes_ask: 57) or as dollars text (yes_ask_dollars: "0.5700")."""
        d = _dollars(best.get(f"{name}_dollars"))
        return d if d is not None else _cents(best.get(name))
    strike = next((best[k] for k in ("floor_strike", "strike", "cap_strike") if best.get(k) not in (None, "")), None)
    return quote("kalshi", px("yes_ask"), px("yes_bid"), px("no_ask"), px("no_bid"), strike)


async def fetch_polymarket(session, start: int) -> dict | None:
    async with session.get(POLY_URL, params={"slug": f"btc-updown-15m-{start}"}, timeout=TIMEOUT) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Polymarket {resp.status}")
        return parse_polymarket(await resp.json(content_type=None), start)


async def fetch_kalshi(session, end: int) -> dict | None:
    async with session.get(KALSHI_URL, params={"series_ticker": KALSHI_SERIES, "status": "open", "limit": 20},
                           timeout=TIMEOUT) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Kalshi {resp.status}")
        return parse_kalshi(await resp.json(content_type=None), end)


# ---- fees and edges -------------------------------------------------------------------------------------------------
def fee_cents(price_c: float, fee50: float) -> float:
    """The taker fee per contract, in cents: biggest at 50¢ (fee50) and shrinking toward the ends, like both
    venues' schedules (Kalshi: 7% × price × (1 − price))."""
    p = price_c / 100
    return fee50 * 4 * p * (1 - p)


def edges(p_up: float, q: dict, fee50: float) -> dict:
    """What each side is worth to you after the fee: fair value minus the ask minus the fee, in cents."""
    up = p_up * 100 - q["up_ask"] - fee_cents(q["up_ask"], fee50)
    dn = (1 - p_up) * 100 - q["dn_ask"] - fee_cents(q["dn_ask"], fee50)
    side, edge, ask = ("Up", up, q["up_ask"]) if up >= dn else ("Down", dn, q["dn_ask"])
    return {"edge_up": up, "edge_dn": dn, "side": side, "edge": edge, "ask": ask}


def paper_trades(rows: list[dict], min_edge: float, fee50: float) -> dict:
    """What following the signal WOULD have made. rows: finished windows with the model's Up probability and the
    market's asks at 10 minutes in ({"day", "p10", "outcome", "up_ask", "dn_ask"}). Each qualifying window buys one
    contract of the cheap side at the ask and pays the fee; also the baseline of always buying the favorite."""
    sig, fav = [], []
    for r in rows:
        e = edges(r["p10"], {"up_ask": r["up_ask"], "dn_ask": r["dn_ask"]}, fee50)
        won_up = bool(r["outcome"])

        def result(side: str, ask: float):
            return (r["day"], (100 if (side == "Up") == won_up else 0) - ask - fee_cents(ask, fee50),
                    (side == "Up") == won_up)
        if e["edge"] >= min_edge:
            sig.append(result(e["side"], e["ask"]))
        fav_side = "Up" if r["p10"] >= 0.5 else "Down"
        fav.append(result(fav_side, r["up_ask"] if fav_side == "Up" else r["dn_ask"]))

    def agg(xs):
        n = len(xs)
        return {"n": n, "win": sum(1 for _, _, w in xs if w) / n if n else None,
                "avg_c": sum(p for _, p, _ in xs) / n if n else None, "total_c": sum(p for _, p, _ in xs),
                "days": sorted({d for d, _, _ in xs}), "pnls": [(d, p) for d, p, _ in xs]}
    return {"windows": len(rows), "signal": agg(sig), "favorite": agg(fav)}
