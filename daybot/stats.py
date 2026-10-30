"""Evidence: how sure can we be that a setup's average is a real edge and not luck?

Trades on the same day are not independent (a market-wide move makes them win or lose together), so the confidence
interval resamples whole DAYS, not trades. Pure Python, seeded, so the same trades always give the same answer.
"""
from __future__ import annotations

import math
import random

from .util import num

BOOT = 600          # resamples
CONF = 0.80         # the interval covers the middle 80%: "80% sure it's between lo and hi"


def clustered_mean_ci(rows: list[tuple[str, float]], boot: int = BOOT, conf: float = CONF, seed: int = 11) -> dict:
    """Bootstrap the average of values[] where rows = [(day, value)], resampling days.
    Returns {"mean", "lo", "hi", "p_pos"} — p_pos: how often the resampled average came out above zero."""
    if not rows:
        return {"mean": 0.0, "lo": 0.0, "hi": 0.0, "p_pos": 0.5}
    by_day: dict[str, list[float]] = {}
    for day, v in rows:
        by_day.setdefault(day, []).append(num(v))
    sums = [sum(v) for v in by_day.values()]
    counts = [len(v) for v in by_day.values()]
    k = len(sums)
    total_n = sum(counts)
    mean = sum(sums) / total_n
    if k < 3:
        return {"mean": mean, "lo": mean if mean < 0 else 0.0, "hi": mean if mean > 0 else 0.0, "p_pos": 0.5}
    rnd = random.Random(seed)
    means = []
    for _ in range(boot):
        s = c = 0
        for _ in range(k):
            i = rnd.randrange(k)
            s += sums[i]
            c += counts[i]
        means.append(s / c if c else 0.0)
    means.sort()
    tail = (1 - conf) / 2
    lo, hi = means[int(tail * boot)], means[min(boot - 1, int((1 - tail) * boot))]
    return {"mean": mean, "lo": lo, "hi": hi, "p_pos": sum(1 for m in means if m > 0) / boot}


def series_stats(values: list[float]) -> dict:
    """Mean, standard deviation, median and the share of winners."""
    n = len(values)
    if not n:
        return {"n": 0, "mean": 0.0, "sd": 0.0, "median": 0.0, "won": 0.0}
    mean = sum(values) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1)) if n > 1 else 0.0
    s = sorted(values)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    return {"n": n, "mean": mean, "sd": sd, "median": med, "won": sum(1 for x in values if x > 0) / n}


def sharpe_ci(returns: list[float], periods: int = 252, boot: int = 400, block: int = 10, conf: float = CONF,
              seed: int = 5) -> dict:
    """Annualized Sharpe of a return series with a moving-block bootstrap interval (blocks keep the streaks)."""
    n = len(returns)
    if n < 30:
        return {"sharpe": 0.0, "lo": 0.0, "hi": 0.0}

    def sharpe(xs):
        m = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) if len(xs) > 1 else 0.0
        return m / sd * math.sqrt(periods) if sd > 0 else 0.0
    base = sharpe(returns)
    rnd = random.Random(seed)
    out = []
    nb = max(1, n // block)
    for _ in range(boot):
        xs: list[float] = []
        for _ in range(nb):
            i = rnd.randrange(0, max(1, n - block + 1))
            xs.extend(returns[i:i + block])
        out.append(sharpe(xs))
    out.sort()
    tail = (1 - conf) / 2
    return {"sharpe": base, "lo": out[int(tail * boot)], "hi": out[min(boot - 1, int((1 - tail) * boot))]}


def months_in(days: int) -> float:
    """Trading days -> months."""
    return days / 21.0
