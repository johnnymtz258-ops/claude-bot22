"""The whole US stock market, so the day's stocks in play and gappers are found the way the research finds them —
among every liquid stock, not only the ~250 big names DayBot always watches.

  all_stocks  every active, tradable common stock on NYSE / Nasdaq / NYSE American / Arca / Cboe (no funds, no
              leveraged or inverse products, no OTC), from Alpaca's asset list, kept for a week
  liquid      the ones that trade enough to day-trade: about MIN_AVG_VOLUME shares a day at MIN_PRICE or more
  cost_pct    what a buy or a sell really costs (half the spread + slippage), by how much money trades in the stock
"""
from __future__ import annotations

import json
import re
import statistics
import time
from pathlib import Path

from .bars import C, T, V
from .util import num

# 2x / 3x / inverse funds: they "gap" every day only because an index or one stock moved, and decay when held
LEVERED = set("""TQQQ SQQQ SOXL SOXS TNA TZA UVXY UVIX SVXY SPXL SPXS SPXU UPRO SDS SSO QLD QID LABU LABD FAS FAZ TECL
TECS FNGU FNGD YINN YANG NUGT DUST JNUG JDST BOIL KOLD UCO SCO TMF TMV TSLL TSLQ TSLZ TSLS NVDL NVDX NVDQ NVD NVDU
MSTU MSTX MSTZ CONL AMDL AAPU GGLL METU AMZU MSFU BITX BITU ETHU SBIT DPST WEBL WEBS HIBL HIBS CURE DRN DRV TSLT
NAIL ERX ERY GUSH DRIP UDOW SDOW URTY SRTY PLTU SMCX SMCZ MUU ARMG KORU""".split())
LEVERED_NAME = re.compile(r"\b\d(\.\d+)?x\b|ultrapro|ultrashort|proshares ultra|\binverse\b|\bleveraged\b|"
                          r"daily target|\bbear\b.*\b(etf|shares|fund)\b|\bbull\b.*\b(etf|shares|fund)\b", re.I)
EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
FUND_NAME = re.compile(r"\b(etf|etn|exchange[- ]traded|fund|notes due|preferred|warrants?|rights?|"
                       r"units?)\b|ishares|spdr|proshares|direxion|vaneck|wisdomtree|graniteshares|defiance|tradr|"
                       r"t-rex|yieldmax|roundhill|invesco|vanguard", re.I)
ASSETS_MAX_AGE = 7 * 86400


def is_stock(a: dict) -> bool:
    sym = str(a.get("symbol") or "")
    name = str(a.get("name") or "")
    return (a.get("tradable", True) and str(a.get("status", "active")) == "active"
            and str(a.get("exchange") or "").upper() in EXCHANGES and sym.isalpha() and 1 <= len(sym) <= 5
            and sym not in LEVERED and not FUND_NAME.search(name) and not LEVERED_NAME.search(name))


async def all_stocks(alpaca, cache: Path | None = None, now: float | None = None) -> list[str]:
    """Every listed common stock (cached in `cache` for a week). [] if Alpaca can't be asked."""
    now = now or time.time()
    if cache and cache.exists():
        try:
            saved = json.loads(cache.read_text())
            if now - num(saved.get("ts")) < ASSETS_MAX_AGE and saved.get("symbols"):
                return list(saved["symbols"])
        except (ValueError, OSError):
            pass
    try:
        rows = await alpaca.assets()
    except Exception:
        rows = []
    syms = sorted({str(a["symbol"]).upper() for a in rows if isinstance(a, dict) and is_stock(a)})
    if syms and cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"ts": int(now), "symbols": syms}))
        except OSError:
            pass
    return syms


async def liquid(alpaca, symbols: list[str], start: float, end: float, cfg, keep_bars: bool = True,
                 chunk: int = 100) -> dict[str, list]:
    """{symbol: daily bars} for the stocks that traded about MIN_AVG_VOLUME shares a day (3/4 of it on a typical day,
    so stocks that were liquid for part of the period stay in) at MIN_PRICE or more. Fetched in chunks so memory
    stays small."""
    out: dict[str, list] = {}
    min_vol, min_px = 0.75 * cfg.get("MIN_AVG_VOLUME"), cfg.get("MIN_PRICE")
    for i in range(0, len(symbols), chunk):
        got = await alpaca.bars(symbols[i:i + chunk], "1Day", start, end, feed="sip")
        for sym, rows in got.items():
            if len(rows) < 15:
                continue
            if statistics.median(r[V] for r in rows) < min_vol or max(r[C] for r in rows) < min_px:
                continue
            out[sym] = rows if keep_bars else []
    return out


def dollar_volume(daily: list, before_ts: float | None = None) -> float:
    """Average dollars traded a day over the last 20 days (before `before_ts`)."""
    rows = [d for d in daily if before_ts is None or d[T] < before_ts][-20:]
    return statistics.mean(d[C] * d[V] for d in rows) if rows else 0.0


def cost_pct(dollar_vol: float, extra: float = 0.01) -> float:
    """Cost of one buy or one sell, % of the price: half the spread plus market-order slippage, by how much money
    trades in the stock a day (SPY ~0.005%, a big stock ~0.01%, a small one 0.15%), plus `extra` (SLIPPAGE_PCT)."""
    for floor, c in ((5e9, 0.005), (1e9, 0.01), (2e8, 0.02), (5e7, 0.04), (1e7, 0.08)):
        if dollar_vol >= floor:
            return c + extra
    return 0.15 + extra
