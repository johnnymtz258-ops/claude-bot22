"""Small shared helpers: number parsing, formatting and address checks."""
from __future__ import annotations

import html
import math
import re
import time

BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
_ADDRESS_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def now() -> int:
    return int(time.time())


def num(value, default: float = 0.0) -> float:
    """Float conversion that never raises and never returns NaN/inf."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return float(default)
    return x if math.isfinite(x) else float(default)


def is_address(value) -> bool:
    s = str(value or "").strip()
    return 32 <= len(s) <= 44 and all(ch in BASE58 for ch in s)


def find_address(text) -> str:
    """First Solana-looking address inside free text or a URL, else ''."""
    for match in _ADDRESS_RE.findall(str(text or "")):
        if is_address(match):
            return match
    return ""


def short(address: str) -> str:
    a = str(address or "")
    return a if len(a) <= 10 else f"{a[:4]}…{a[-4:]}"


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def usd(value, signed: bool = False) -> str:
    """$1,234 / $12.30 / $0.042 — sized to stay readable for tiny and large amounts."""
    v = num(value)
    sign = ("+" if v > 0 else "-" if v < 0 else "") if signed else ("-" if v < 0 else "")
    a = abs(v)
    if a >= 1000:
        body = f"{a:,.0f}"
    elif a >= 1:
        body = f"{a:,.2f}"
    elif a == 0:
        body = "0.00"
    else:
        body = f"{a:.3g}"
    return f"{sign}${body}"


def mc(value) -> str:
    """Market cap the way trading apps show it: $857.4K, $1.3M, $72.4B."""
    v = num(value)
    if v <= 0:
        return "?"
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= size:
            text = f"{v / size:.1f}".rstrip("0").rstrip(".")
            return f"${text}{suffix}"
    return f"${v:,.0f}"


def pct(value, digits: int = 0) -> str:
    return f"{num(value):+.{digits}f}%"


def mult(value) -> str:
    """Price multiple: 0.62x, 3.8x, 12x."""
    v = num(value)
    if v <= 0:
        return "?"
    return f"{v:.2f}x" if v < 1 else (f"{v:.1f}x" if v < 10 else f"{v:.0f}x")


def dur(seconds) -> str:
    """Compact duration: 45s, 12m, 3h, 2d."""
    seconds = max(0, int(num(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def ago(ts, ref=None) -> str:
    """How long ago a unix timestamp was (as a compact duration)."""
    return dur((time.time() if ref is None else ref) - num(ts))


def parse_amount(text) -> float:
    """Parse '850k', '$1.2M', '2,500', '0.5b' into a number (0 when unparseable)."""
    s = str(text or "").strip().lower().replace("$", "").replace(",", "")
    if s.endswith("mc"):
        s = s[:-2].strip()
    scale = 1.0
    if s and s[-1] in "kmb":
        scale = {"k": 1e3, "m": 1e6, "b": 1e9}[s[-1]]
        s = s[:-1]
    try:
        return max(0.0, float(s) * scale)
    except ValueError:
        return 0.0
