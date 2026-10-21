"""Small helpers: safe numbers and the money/percent formats every message uses."""
from __future__ import annotations

import html
import json
import math
import time


def now() -> int:
    return int(time.time())


def num(value, default: float = 0.0) -> float:
    """Float conversion that never raises and never returns NaN/inf."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return float(default)
    return x if math.isfinite(x) else float(default)


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def money(value, signed: bool = False) -> str:
    """$1,234 / $12.30 / -$45.10 / +$3.20 (signed)."""
    v = num(value)
    sign = ("+" if v > 0 else "-" if v < 0 else "") if signed else ("-" if v < 0 else "")
    a = abs(v)
    body = f"{a:,.0f}" if a >= 10_000 else f"{a:,.2f}"
    return f"{sign}${body}"


def price(value) -> str:
    """A share price: $142.37, $3.4150 under $1."""
    v = num(value)
    return f"${v:,.2f}" if abs(v) >= 1 else f"${v:.4f}"


def pct(value, digits: int = 1) -> str:
    return f"{num(value):+.{digits}f}%"


def big(value) -> str:
    """Volume / share counts: 950, 12.4K, 3.1M, 1.2B."""
    v = num(value)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= size:
            return f"{v / size:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{v:,.0f}"


def rmult(value) -> str:
    return f"{num(value):+.2f}R"


def dur(seconds) -> str:
    seconds = max(0, int(num(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    hours, minutes = seconds // 3600, seconds % 3600 // 60
    if hours < 24:
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    return f"{seconds // 86400}d"


def dumps(value) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def loads(text, default=None):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
