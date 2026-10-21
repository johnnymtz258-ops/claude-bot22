"""Price bars and the indicators day traders actually use.

A bar is a tuple (ts, open, high, low, close, volume) with ts = the bar's start (unix seconds).
"""
from __future__ import annotations

import bisect
import statistics

from .clock import Market, at, day_of

T, O, H, L, C, V = range(6)


def resample(bars: list[tuple], minutes: int) -> list[tuple]:
    """1-minute bars -> N-minute bars aligned to the clock (9:30, 9:35, ...)."""
    step = minutes * 60
    out: list[list] = []
    for b in bars:
        start = b[T] - b[T] % step
        if out and out[-1][T] == start:
            cur = out[-1]
            cur[H] = max(cur[H], b[H])
            cur[L] = min(cur[L], b[L])
            cur[C] = b[C]
            cur[V] += b[V]
        else:
            out.append([start, b[O], b[H], b[L], b[C], b[V]])
    return [tuple(x) for x in out]


def complete(bars: list[tuple], minutes: int, now: float) -> list[tuple]:
    """Drop the last bar if it's still forming."""
    if bars and bars[-1][T] + minutes * 60 > now:
        return bars[:-1]
    return bars


def split_day(bars: list[tuple], day: str, market: Market) -> tuple[list, list, list]:
    """(premarket, regular hours, after hours) bars of one day."""
    pre_start, open_ts, close_ts, end = at(day, "04:00"), market.open_ts(day), market.close_ts(day), at(day, "20:00")
    pre = [b for b in bars if pre_start <= b[T] < open_ts]
    rth = [b for b in bars if open_ts <= b[T] < close_ts]
    post = [b for b in bars if close_ts <= b[T] < end]
    return pre, rth, post


def by_day(bars: list[tuple]) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for b in bars:
        out.setdefault(day_of(b[T]), []).append(b)
    return out


def vwap(bars: list[tuple]) -> list[float]:
    """Cumulative VWAP at each bar's close (anchor = the first bar, normally 9:30)."""
    out, pv, vol = [], 0.0, 0.0
    for b in bars:
        typical = (b[H] + b[L] + b[C]) / 3
        pv += typical * b[V]
        vol += b[V]
        out.append(pv / vol if vol > 0 else b[C])
    return out


def ema(values: list[float], n: int) -> list[float]:
    out, k, cur = [], 2 / (n + 1), None
    for v in values:
        cur = v if cur is None else v * k + cur * (1 - k)
        out.append(cur)
    return out


def sma(values: list[float], n: int) -> float:
    vals = values[-n:]
    return sum(vals) / len(vals) if vals else 0.0


def atr(bars: list[tuple], n: int = 14) -> float:
    """Average true range of the last n bars (daily bars -> the stock's normal daily move in $)."""
    if len(bars) < 2:
        return (bars[0][H] - bars[0][L]) if bars else 0.0
    trs = []
    for prev, b in zip(bars, bars[1:]):
        trs.append(max(b[H] - b[L], abs(b[H] - prev[C]), abs(b[L] - prev[C])))
    trs = trs[-n:]
    return sum(trs) / len(trs)


# Share of a normal day's volume that has traded by N minutes after the open (US stocks: heavy open, quiet lunch,
# heavy close). Used for relative volume when there's no intraday history for the stock.
VOLUME_CURVE = [(0, 0.0), (5, 0.04), (15, 0.10), (30, 0.17), (60, 0.27), (90, 0.35), (150, 0.47), (210, 0.56),
                (270, 0.65), (330, 0.77), (360, 0.85), (390, 1.0)]


def volume_share(minutes: float) -> float:
    xs = [x for x, _ in VOLUME_CURVE]
    if minutes <= 0:
        return 0.0
    if minutes >= xs[-1]:
        return 1.0
    i = bisect.bisect_right(xs, minutes)
    (x0, y0), (x1, y1) = VOLUME_CURVE[i - 1], VOLUME_CURVE[i]
    return y0 + (y1 - y0) * (minutes - x0) / (x1 - x0)


def rvol(cum_volume: float, minutes_since_open: float, avg_daily_volume: float) -> float:
    """Relative volume: today's volume so far vs a normal day at this time (1.0 = normal, 3.0 = 3x)."""
    expected = avg_daily_volume * max(volume_share(minutes_since_open), 0.02)
    return cum_volume / expected if expected > 0 else 0.0


def day_levels(day: str, bars: list[tuple], daily: list[tuple], market: Market, orb_minutes: int = 15) -> dict:
    """The levels that matter today: premarket high/low, yesterday's high/low/close, the opening range, HOD/LOD,
    gap, ATR, average volume. `bars` are this day's 1-minute (or 5-minute) bars; `daily` are prior days."""
    pre, rth, _ = split_day(bars, day, market)
    prior = [d for d in daily if day_of(d[T]) < day]
    lv: dict = {"day": day}
    if prior:
        y = prior[-1]
        lv.update(pdh=y[H], pdl=y[L], pdc=y[C], pdo=y[O])
        lv["atr"] = atr(prior[-15:], 14)
        lv["avg_vol"] = statistics.mean(d[V] for d in prior[-20:])
        closes = [d[C] for d in prior]
        lv["sma20"] = sma(closes, 20)
        lv["sma50"] = sma(closes, 50)
    if pre:
        lv["pmh"] = max(b[H] for b in pre)
        lv["pml"] = min(b[L] for b in pre)
        lv["pm_vol"] = sum(b[V] for b in pre)
        lv["pm_last"] = pre[-1][C]
    if rth:
        lv["open"] = rth[0][O]
        lv["hod"] = max(b[H] for b in rth)
        lv["lod"] = min(b[L] for b in rth)
        lv["last"] = rth[-1][C]
        lv["vol"] = sum(b[V] for b in rth)
        or_end = market.open_ts(day) + orb_minutes * 60
        orb = [b for b in rth if b[T] < or_end]
        if orb and rth[-1][T] + 60 >= or_end:
            lv["orh"] = max(b[H] for b in orb)
            lv["orl"] = min(b[L] for b in orb)
    ref = lv.get("open") or lv.get("pm_last")
    if ref and lv.get("pdc"):
        lv["gap_pct"] = (ref / lv["pdc"] - 1) * 100
    return lv


def pivots(bars: list[tuple], left: int = 3, right: int = 3) -> tuple[list[float], list[float]]:
    """Swing highs and lows (a bar higher/lower than `left` bars before and `right` after it)."""
    highs, lows = [], []
    for i in range(left, len(bars) - right):
        window = bars[i - left:i + right + 1]
        if bars[i][H] == max(b[H] for b in window):
            highs.append(bars[i][H])
        if bars[i][L] == min(b[L] for b in window):
            lows.append(bars[i][L])
    return highs, lows


def nearest_levels(price: float, levels: dict, above: bool) -> list[tuple[str, float]]:
    """Named levels above (or below) a price, nearest first."""
    names = {"pmh": "premarket high", "pml": "premarket low", "pdh": "yesterday's high", "pdl": "yesterday's low",
             "pdc": "yesterday's close", "hod": "high of day", "lod": "low of day", "orh": "opening-range high",
             "orl": "opening-range low"}
    out = []
    for key, name in names.items():
        v = levels.get(key)
        if not v:
            continue
        if (above and v > price) or (not above and v < price):
            out.append((name, float(v)))
    out.sort(key=lambda kv: abs(kv[1] - price))
    return out
