"""Alpaca market data (free account: real-time IEX prices, full-market history older than 15 minutes, indicative
option quotes) plus the market calendar and option open interest from its trading API.

DayBot only reads from Alpaca. It never places orders.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import time

import aiohttp

from .util import num

DATA = "https://data.alpaca.markets"
PAPER = "https://paper-api.alpaca.markets"
LIVE = "https://api.alpaca.markets"
SIP_DELAY = 16 * 60          # the free plan may read full-market (SIP) data once it's 15+ minutes old
MIN_GAP = 0.32               # ~190 requests a minute: under the free plan's 200


class AlpacaError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"Alpaca {status}: {text[:200]}")
        self.status = status
        self.text = text


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?\s*(Z|[+-]\d{2}:?\d{2})?$")


def parse_ts(text) -> int:
    """RFC 3339 / ISO time -> unix seconds (nanoseconds, 'Z', offsets and bare dates all fine). 0 if unreadable.
    A bare date is taken as noon UTC so it stays on the same New York day."""
    m = _TS.match(str(text or "").strip())
    if not m:
        return 0
    y, mo, d, h, mi, sec, _frac, zone = m.groups()
    try:
        t = dt.datetime(int(y), int(mo), int(d), int(h or 12), int(mi or 0), int(sec or 0), tzinfo=dt.timezone.utc)
    except ValueError:
        return 0
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        hh, mm = int(zone[1:3]), int(zone[-2:])
        t -= sign * dt.timedelta(hours=hh, minutes=mm)
    return int(t.timestamp())


def bar_tuple(b: dict) -> tuple:
    return (parse_ts(b.get("t")), num(b.get("o")), num(b.get("h")), num(b.get("l")), num(b.get("c")), num(b.get("v")))


class Alpaca:
    def __init__(self, session: aiohttp.ClientSession | None, key: str, secret: str, feed: str = "iex",
                 paper: bool = True):
        self.session = session
        self.key, self.secret = key, secret
        self.feed = feed if feed in {"iex", "sip"} else "iex"
        self.trading = PAPER if paper else LIVE
        self.data_base = DATA
        self.last_error = ""
        self.last_ok = 0.0          # when a request last worked
        self.last_fail = 0.0
        self.requests = 0
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._sip_ok = True

    @property
    def ok(self) -> bool:
        return bool(self.key and self.secret and self.session)

    @property
    def realtime_sip(self) -> bool:
        return self.feed == "sip"

    async def _pace(self) -> None:
        async with self._lock:
            wait = MIN_GAP - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def _get(self, base: str, path: str, params: dict | None = None):
        if not self.ok:
            raise AlpacaError(0, "no Alpaca keys")
        headers = {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret, "accept": "application/json"}
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        for attempt in range(4):
            await self._pace()
            self.requests += 1
            try:
                async with self.session.get(base + path, params=clean, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    text = await resp.text()
                    if resp.status == 429 or resp.status >= 500:
                        self.last_error = f"{time.strftime('%H:%M:%S')} {path} {resp.status}"
                        await asyncio.sleep(min(2 ** attempt, 10))
                        continue
                    if resp.status >= 400:
                        self.last_error = f"{time.strftime('%H:%M:%S')} {path} {resp.status}: {text[:120]}"
                        if resp.status in (401, 403) and "subscription" not in text.lower():
                            self.last_fail = time.time()
                        raise AlpacaError(resp.status, text)
                    self.last_ok = time.time()
                    return json.loads(text) if text else None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.last_error = f"{time.strftime('%H:%M:%S')} {path} {type(exc).__name__}"
                self.last_fail = time.time()
                await asyncio.sleep(min(2 ** attempt, 10))
        raise AlpacaError(0, self.last_error or "no answer")

    @property
    def healthy(self) -> bool:
        """Keys set and the last request worked (or none has failed yet)."""
        return self.ok and self.last_fail <= self.last_ok

    # ---- stocks ------------------------------------------------------------------------------------------------
    async def bars(self, symbols: list[str], timeframe: str, start: float, end: float | None = None,
                   feed: str | None = None) -> dict[str, list[tuple]]:
        """Bars for many symbols: {symbol: [(ts, o, h, l, c, v), ...]} oldest first. timeframe: 1Min, 5Min, 1Day…

        feed='sip' on the free plan reads the full market up to 16 minutes ago (more accurate volume and premarket);
        the default feed is real-time."""
        feed = feed or self.feed
        end = end or time.time()
        if feed == "sip" and not self.realtime_sip:
            end = min(end, time.time() - SIP_DELAY)
            if not self._sip_ok:
                feed = "iex"
        out: dict[str, list[tuple]] = {}
        syms = sorted({s.upper() for s in symbols if s})
        for i in range(0, len(syms), 100):
            chunk = syms[i:i + 100]
            token = None
            for _ in range(200):
                params = {"symbols": ",".join(chunk), "timeframe": timeframe, "start": iso(start), "end": iso(end),
                          "limit": 10000, "adjustment": "split", "feed": feed, "sort": "asc", "page_token": token}
                try:
                    data = await self._get(self.data_base, "/v2/stocks/bars", params)
                except AlpacaError as exc:
                    if exc.status == 403 and feed == "sip":    # plan doesn't include SIP: real-time IEX instead
                        self._sip_ok = False
                        feed = "iex"
                        continue
                    raise
                for sym, rows in ((data or {}).get("bars") or {}).items():
                    out.setdefault(sym, []).extend(bar_tuple(b) for b in rows or [])
                token = (data or {}).get("next_page_token")
                if not token:
                    break
        return out

    async def daily(self, symbols: list[str], days: int = 60, end: float | None = None) -> dict[str, list[tuple]]:
        end = end or time.time()
        start = end - (days * 7 // 5 + 10) * 86400
        return await self.bars(symbols, "1Day", start, end - SIP_DELAY if not self.realtime_sip else end, feed="sip")

    async def snapshots(self, symbols: list[str], feed: str | None = None) -> dict[str, dict]:
        out: dict[str, dict] = {}
        syms = sorted({s.upper() for s in symbols if s})
        for i in range(0, len(syms), 200):
            data = await self._get(self.data_base, "/v2/stocks/snapshots", {"symbols": ",".join(syms[i:i + 200]),
                                                                  "feed": feed or self.feed})
            for sym, snap in (data or {}).items():
                if isinstance(snap, dict):
                    out[sym] = snap
        return out

    async def movers(self, top: int = 50) -> dict:
        return await self._get(self.data_base, "/v1beta1/screener/stocks/movers", {"top": top}) or {}

    async def most_actives(self, top: int = 50) -> list[dict]:
        data = await self._get(self.data_base, "/v1beta1/screener/stocks/most-actives", {"by": "volume", "top": top}) or {}
        return data.get("most_actives") or []

    async def news(self, symbols: list[str], start: float, limit: int = 50) -> list[dict]:
        if not symbols:
            return []
        data = await self._get(self.data_base, "/v1beta1/news", {"symbols": ",".join(sorted(set(symbols))[:50]),
                                                       "start": iso(start), "limit": min(limit, 50), "sort": "desc"})
        return (data or {}).get("news") or []

    # ---- options -----------------------------------------------------------------------------------------------
    async def option_chain(self, underlying: str, expiry: str | None = None, kind: str | None = None,
                           strike_lo: float | None = None, strike_hi: float | None = None,
                           exp_to: str | None = None) -> dict[str, dict]:
        """{OCC symbol: snapshot with latestQuote, greeks, impliedVolatility} (free plan: indicative quotes)."""
        out: dict[str, dict] = {}
        token = None
        for _ in range(20):
            data = await self._get(self.data_base, f"/v1beta1/options/snapshots/{underlying.upper()}", {
                "feed": "opra" if self.realtime_sip else "indicative", "expiration_date": expiry,
                "expiration_date_lte": exp_to if not expiry else None, "type": kind, "strike_price_gte": strike_lo, "strike_price_lte": strike_hi, "limit": 1000,
                "page_token": token})
            out.update((data or {}).get("snapshots") or {})
            token = (data or {}).get("next_page_token")
            if not token:
                break
        return out

    async def option_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        data = await self._get(self.data_base, "/v1beta1/options/snapshots", {
            "symbols": ",".join(symbols[:100]), "feed": "opra" if self.realtime_sip else "indicative"})
        return (data or {}).get("snapshots") or {}

    async def option_contracts(self, underlying: str, exp_from: str | None = None, exp_to: str | None = None,
                               kind: str | None = None, strike_lo: float | None = None,
                               strike_hi: float | None = None) -> list[dict]:
        """Contract list with open interest (trading API)."""
        out: list[dict] = []
        token = None
        for _ in range(10):
            data = await self._get(self.trading, "/v2/options/contracts", {
                "underlying_symbols": underlying.upper(), "expiration_date_gte": exp_from,
                "expiration_date_lte": exp_to, "type": kind, "strike_price_gte": strike_lo,
                "strike_price_lte": strike_hi, "limit": 1000, "page_token": token})
            out.extend((data or {}).get("option_contracts") or [])
            token = (data or {}).get("next_page_token")
            if not token:
                break
        return out

    # ---- market calendar -------------------------------------------------------------------------------------
    async def calendar(self, start_day: str, end_day: str) -> list[dict]:
        data = await self._get(self.trading, "/v2/calendar", {"start": start_day, "end": end_day})
        return data if isinstance(data, list) else []

    async def clock(self) -> dict:
        return await self._get(self.trading, "/v2/clock") or {}


def snap_price(snap: dict) -> float:
    """Best current price from a stock snapshot: last trade, else the quote's mid, else the minute bar."""
    trade = num((snap.get("latestTrade") or {}).get("p"))
    if trade > 0:
        return trade
    q = snap.get("latestQuote") or {}
    bid, ask = num(q.get("bp")), num(q.get("ap"))
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return num((snap.get("minuteBar") or {}).get("c")) or num((snap.get("dailyBar") or {}).get("c"))


def snap_spread_pct(snap: dict) -> float | None:
    q = snap.get("latestQuote") or {}
    bid, ask = num(q.get("bp")), num(q.get("ap"))
    if bid > 0 and ask >= bid:
        return (ask - bid) / ((ask + bid) / 2) * 100
    return None
