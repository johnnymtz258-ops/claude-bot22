"""One place for market data, cached: today's minute bars per symbol, daily history, quotes, news and option chains.

Free-plan care: real-time bars come from IEX (a slice of the market), history and the premarket come from the
full market 15+ minutes late. Relative volume always compares like with like (today's IEX volume vs past IEX volume).
"""
from __future__ import annotations

import datetime as dt
import statistics
import time

from .alpaca import Alpaca, AlpacaError, snap_price
from .bars import H, L, T, V, day_levels, resample, rvol, split_day, vwap
from .clock import Market, at, day_of
from .options import parse_occ
from .setups import BAR, Day
from .util import num


class MarketData:
    def __init__(self, alpaca: Alpaca, market: Market, cfg):
        # pre_sip values: (high, low, fetched at, volume)
        self.alpaca, self.market, self.cfg = alpaca, market, cfg
        self.minute: dict[str, list[tuple]] = {}           # today's 1-min bars (4:00 -> now), live feed
        self.minute_day: dict[str, str] = {}
        self.fetched: dict[str, float] = {}                # when each symbol's minute bars were last brought up to date
        self.pre_sip: dict[str, tuple[float, float, float]] = {}   # full-market premarket (high, low, volume)
        self.daily_sip: dict[str, tuple[float, list]] = {}   # (fetched, bars) — prices
        self.live_avg: dict[str, tuple[float, float]] = {}   # (fetched, avg daily volume in the live feed)
        self.snaps: dict[str, tuple[float, dict]] = {}
        self.news_cache: dict[str, tuple[float, list]] = {}
        self.chains: dict[tuple, tuple[float, dict]] = {}
        self.oi: dict[tuple, tuple[float, dict]] = {}
        self.last_error = ""

    @property
    def ok(self) -> bool:
        return self.alpaca.ok

    # ---- daily ---------------------------------------------------------------------------------------------
    async def load_daily(self, symbols: list[str], now: float | None = None) -> None:
        now = now or time.time()
        need = [s for s in symbols if now - self.daily_sip.get(s, (0, []))[0] > 6 * 3600]
        if not need:
            return
        try:
            start = now - 100 * 86400
            prices = await self.alpaca.bars(need, "1Day", start, now, feed="sip")
            vols = prices if self.alpaca.feed == "sip" else await self.alpaca.bars(need, "1Day", start, now,
                                                                                   feed=self.alpaca.feed)
        except AlpacaError as exc:
            self.last_error = str(exc)
            return
        today = day_of(now)
        for s in need:
            rows = [b for b in prices.get(s) or [] if day_of(b[T]) < today]
            self.daily_sip[s] = (now, rows)
            live = [b for b in vols.get(s) or [] if day_of(b[T]) < today]
            self.live_avg[s] = (now, statistics.mean(b[V] for b in live[-20:]) if live else 0.0)

    def daily(self, symbol: str) -> list:
        return self.daily_sip.get(symbol, (0, []))[1]

    # ---- intraday ------------------------------------------------------------------------------------------
    async def refresh(self, symbols: list[str], now: float | None = None) -> None:
        """Bring today's minute bars up to date for these symbols."""
        now = now or time.time()
        day = day_of(now)
        if not symbols or not self.ok:
            return
        fresh = [s for s in symbols if self.minute_day.get(s) != day]
        known = [s for s in symbols if self.minute_day.get(s) == day]
        try:
            if fresh:
                got = await self.alpaca.bars(fresh, "1Min", at(day, "04:00"), now)
                for s in fresh:
                    self.minute[s] = got.get(s) or []
                    self.minute_day[s] = day
            if known:
                # from each symbol's last bar (or last fetch, for one with no trades yet) — not the whole day again
                since = min((self.minute[s][-1][T] if self.minute.get(s) else self.fetched.get(s, at(day, "04:00")) - 120)
                            for s in known)
                got = await self.alpaca.bars(known, "1Min", since, now)
                for s in known:
                    self._merge(s, got.get(s) or [])
            for s in fresh + known:
                self.fetched[s] = now
        except AlpacaError as exc:
            self.last_error = str(exc)
        await self._premarket_sip(symbols, now)

    def _merge(self, symbol: str, new: list[tuple]) -> None:
        if not new:
            return
        cur = self.minute.setdefault(symbol, [])
        first = new[0][T]
        while cur and cur[-1][T] >= first:
            cur.pop()
        cur.extend(new)

    async def _premarket_sip(self, symbols: list[str], now: float) -> None:
        """Full-market premarket high/low/volume (free plan: up to 15 minutes late — complete by 9:46)."""
        day = day_of(now)
        if self.market.session(now) not in {"pre", "open"}:
            return
        need = [s for s in symbols if s not in self.pre_sip or (self.pre_sip[s][2] < now - 600
                                                                 and now < self.market.open_ts(day) + 20 * 60)]
        if not need:
            return
        try:
            got = await self.alpaca.bars(need, "5Min", at(day, "04:00"), min(now, self.market.open_ts(day)), feed="sip")
        except AlpacaError as exc:
            self.last_error = str(exc)
            return
        for s in need:
            rows = [b for b in got.get(s) or [] if b[T] < self.market.open_ts(day)]
            if rows:
                self.pre_sip[s] = (max(b[H] for b in rows), min(b[L] for b in rows), now, sum(b[V] for b in rows))
            else:
                self.pre_sip[s] = (0.0, 0.0, now, 0.0)

    def bars1(self, symbol: str) -> list[tuple]:
        return self.minute.get(symbol) or []

    def last_price(self, symbol: str) -> float:
        rows = self.bars1(symbol)
        if rows:
            return rows[-1][4]
        snap = self.snaps.get(symbol)
        return snap_price(snap[1]) if snap else 0.0

    def levels(self, symbol: str, now: float | None = None) -> dict:
        now = now or time.time()
        day = day_of(now)
        lv = day_levels(day, self.bars1(symbol), self.daily(symbol), self.market, int(self.cfg.get("ORB_MINUTES")))
        sip = self.pre_sip.get(symbol)
        if sip and sip[0] > 0:
            lv["pmh"] = max(lv.get("pmh", 0.0), sip[0])
            lv["pml"] = min(lv.get("pml") or sip[1], sip[1])
            lv["pm_vol_full"] = sip[3] if len(sip) > 3 else 0.0
        lv["avg_vol_live"] = self.live_avg.get(symbol, (0, 0.0))[1]
        _, rth, _ = split_day(self.bars1(symbol), day, self.market)
        if rth:
            vw = vwap(rth)
            lv["vwap"] = vw[-1]
            lv["rvol"] = 0.0
            if lv["avg_vol_live"]:
                lv["rvol"] = rvol(sum(b[V] for b in rth), (now - self.market.open_ts(day)) / 60, lv["avg_vol_live"])
        return lv

    def day_obj(self, symbol: str, now: float | None = None) -> Day | None:
        """Today's regular-session 5-minute bars (completed only) as a setups.Day."""
        now = now or time.time()
        day = day_of(now)
        _, rth, _ = split_day(self.bars1(symbol), day, self.market)
        bars5 = [b for b in resample(rth, 5) if b[T] + BAR <= now]
        if not bars5:
            return None
        lv = self.levels(symbol, now)
        if not lv.get("atr") or not lv.get("pdc"):
            return None
        return Day(symbol, bars5, vwap(bars5), self.market.open_ts(day), self.market.close_ts(day), lv["atr"],
                   lv.get("avg_vol_live", 0.0), lv["pdc"], lv.get("pmh", 0.0), lv.get("gap_pct", 0.0), 1,
                   lv.get("pml", 0.0))

    # ---- quotes, news, options -----------------------------------------------------------------------------
    async def snapshot(self, symbol: str, max_age: float = 10) -> dict:
        got = self.snaps.get(symbol)
        if got and time.time() - got[0] < max_age:
            return got[1]
        try:
            snaps = await self.alpaca.snapshots([symbol])
        except AlpacaError as exc:
            self.last_error = str(exc)
            return got[1] if got else {}
        snap = snaps.get(symbol) or {}
        self.snaps[symbol] = (time.time(), snap)
        return snap

    async def snapshots(self, symbols: list[str]) -> dict[str, dict]:
        if not symbols:
            return {}
        try:
            snaps = await self.alpaca.snapshots(symbols)
        except AlpacaError as exc:
            self.last_error = str(exc)
            return {}
        t = time.time()
        for s, snap in snaps.items():
            self.snaps[s] = (t, snap)
        return snaps

    async def news(self, symbol: str, hours: float = 24) -> list[dict]:
        got = self.news_cache.get(symbol)
        if got and time.time() - got[0] < 600:
            return got[1]
        try:
            rows = await self.alpaca.news([symbol], time.time() - hours * 3600, 20)
        except AlpacaError as exc:
            self.last_error = str(exc)
            rows = got[1] if got else []
        self.news_cache[symbol] = (time.time(), rows)
        return rows

    async def chain(self, underlying: str, expiry: str | None = None, kind: str | None = None,
                    spot: float = 0.0) -> dict:
        key = (underlying, expiry, kind)
        got = self.chains.get(key)
        if got and time.time() - got[0] < 60:
            return got[1]
        lo, hi = (spot * 0.9, spot * 1.1) if spot else (None, None)
        exp_to = (dt.date.today() + dt.timedelta(days=21)).isoformat() if not expiry else None
        try:
            snaps = await self.alpaca.option_chain(underlying, expiry, kind, lo, hi, exp_to)
        except AlpacaError as exc:
            self.last_error = str(exc)
            snaps = got[1] if got else {}
        self.chains[key] = (time.time(), snaps)
        return snaps

    async def open_interest(self, underlying: str, exp_from: str, exp_to: str, spot: float = 0.0) -> dict[str, float]:
        key = (underlying, exp_from, exp_to)
        got = self.oi.get(key)
        if got and time.time() - got[0] < 3 * 3600:
            return got[1]
        lo, hi = (spot * 0.85, spot * 1.15) if spot else (None, None)
        try:
            rows = await self.alpaca.option_contracts(underlying, exp_from, exp_to, None, lo, hi)
        except AlpacaError as exc:
            self.last_error = str(exc)
            rows = []
        out = {str(r.get("symbol")): num(r.get("open_interest"), -1) for r in rows}
        self.oi[key] = (time.time(), out)
        return out

    async def expiries(self, underlying: str, today: dt.date, spot: float, days: int = 30) -> list[str]:
        """Expiration dates available in the next `days` days (from the contract list)."""
        oi = await self.open_interest(underlying, today.isoformat(), (today + dt.timedelta(days=days)).isoformat(), spot)
        return sorted({p["expiry"] for p in (parse_occ(s) for s in oi) if p})

    def prune(self, now: float) -> None:
        day = day_of(now)
        for s in [s for s, d in self.minute_day.items() if d != day]:
            self.minute.pop(s, None)
            self.minute_day.pop(s, None)
            self.pre_sip.pop(s, None)
        for cache in (self.snaps, self.news_cache, self.chains, self.oi):
            for k in [k for k, v in cache.items() if now - v[0] > 6 * 3600]:
                cache.pop(k, None)

