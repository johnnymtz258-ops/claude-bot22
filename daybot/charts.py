"""Chart pictures for Telegram and the dashboard: candles, VWAP, the day's levels, the plan, your fills, volume.

Needs matplotlib (in requirements-day.txt). Without it every message still goes out, just without the picture.
"""
from __future__ import annotations

import io

from .bars import C, H, L, O, T, V, ema, resample, vwap
from .clock import at, et

BG, FG, GRID = "#0f1419", "#d6dde6", "#1f2a35"
UP, DOWN = "#26a69a", "#ef5350"
LEVEL_STYLE = {"pmh": ("PM high", "#b39ddb"), "pml": ("PM low", "#b39ddb"), "pdh": ("Y high", "#90a4ae"),
               "pdl": ("Y low", "#90a4ae"), "pdc": ("Y close", "#78909c"), "orh": ("OR high", "#4fc3f7"),
               "orl": ("OR low", "#4fc3f7")}


def available() -> bool:
    import importlib.util
    return importlib.util.find_spec("matplotlib") is not None


def render(symbol: str, bars1: list[tuple], day: str, market, *, levels: dict | None = None, plan: dict | None = None,
           fills: list[dict] | None = None, title: str = "", minutes: int = 5, start: str = "08:00") -> bytes | None:
    """PNG bytes of today's chart, or None (no matplotlib / no bars)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    t0, open_ts, close_ts = at(day, start), market.open_ts(day), market.close_ts(day)
    rows = [b for b in bars1 if t0 <= b[T] < close_ts + 3600]
    bars = resample(rows, minutes)
    if len(bars) < 2:
        return None
    rth = [b for b in bars if open_ts <= b[T] < close_ts]
    vw = vwap(rth)

    index = {b[T]: i for i, b in enumerate(bars)}
    fig, (ax, axv) = plt.subplots(2, 1, figsize=(10, 5.8), dpi=110, sharex=True,
                                  gridspec_kw={"height_ratios": [4, 1], "hspace": 0.04})
    fig.patch.set_facecolor(BG)
    for a in (ax, axv):
        a.set_facecolor(BG)
        a.tick_params(colors=FG, labelsize=8)
        for spine in a.spines.values():
            spine.set_color(GRID)
        a.grid(color=GRID, linewidth=0.6)
    width = 0.62
    pre_end = None
    for i, b in enumerate(bars):
        color = UP if b[C] >= b[O] else DOWN
        ax.vlines(i, b[L], b[H], color=color, linewidth=0.9)
        body = abs(b[C] - b[O]) or (b[H] - b[L]) * 0.02 or 0.0001
        ax.add_patch(plt.Rectangle((i - width / 2, min(b[O], b[C])), width, body, color=color, linewidth=0))
        axv.bar(i, b[V], width=width, color=color, alpha=0.55)
        if b[T] < open_ts:
            pre_end = i
    if pre_end is not None:
        ax.axvspan(-0.5, pre_end + 0.5, color="#ffffff", alpha=0.035, linewidth=0)
        axv.axvspan(-0.5, pre_end + 0.5, color="#ffffff", alpha=0.035, linewidth=0)
    if rth:
        rx = [index[b[T]] for b in rth]
        ax.plot(rx, vw, color="#ffb74d", linewidth=1.4, label="VWAP")
        e9 = ema([b[C] for b in rth], 9)
        ax.plot(rx, e9, color="#64b5f6", linewidth=0.8, alpha=0.7, label="EMA 9")
    lo = min(b[L] for b in bars)
    hi = max(b[H] for b in bars)
    span = hi - lo or hi * 0.01
    lines = []                                   # (y, text, color, style, width)
    for key, (text, color) in LEVEL_STYLE.items():
        y = (levels or {}).get(key)
        if y:
            lines.append((y, text, color, ":", 0.8))
    if plan:
        for key, text, color, style, lw in (("entry", "entry", "#e0e0e0", "-", 1.0), ("stop", "stop", DOWN, "-", 1.2),
                                            ("t1", "target 1", UP, "--", 1.0), ("t2", "target 2", UP, "--", 1.0)):
            if plan.get(key):
                lines.append((plan[key], text, color, style, lw))
    lines = [ln for ln in lines if lo - span * 0.6 <= ln[0] <= hi + span * 0.6]
    label_x = len(bars) + 0.6
    gap = span * 0.045
    placed = []
    for y, text, color, style, lw in sorted(lines, key=lambda ln: ln[0]):
        ax.hlines(y, -0.8, label_x - 0.4, colors=color, linestyles=style, linewidth=lw, alpha=0.85)
        ty = max(y, placed[-1] + gap) if placed else y      # keep labels from sitting on top of each other
        placed.append(ty)
        ax.text(label_x, ty, f"{text} {y:.2f}", color=color, fontsize=7.2, va="center", ha="left",
                bbox={"facecolor": BG, "edgecolor": "none", "pad": 0.5})
    for f in fills or []:
        ts = int(f.get("ts", 0))
        i = next((index[b[T]] for b in reversed(bars) if b[T] <= ts), None)
        if i is None:
            continue
        buy = f.get("side") == "BUY"
        ax.scatter([i], [f.get("price")], marker="^" if buy else "v", s=70, color="#ffee58" if buy else "#ff7043",
                   edgecolors="#000000", linewidths=0.6, zorder=5)
    ticks, labels = [], []
    for i, b in enumerate(bars):
        t = et(b[T])
        if t.minute == 0 or (t.hour == 9 and t.minute == 30):
            ticks.append(i)
            labels.append(t.strftime("%-I:%M"))
    axv.set_xticks(ticks)
    axv.set_xticklabels(labels)
    axv.set_yticks([])
    ax.set_xlim(-0.8, len(bars) + max(14, len(bars) * 0.16))
    pad = span * 0.08
    ys = [lo, hi] + [ln[0] for ln in lines] + placed
    ax.set_ylim(min(ys) - pad, max(ys) + pad)
    ax.yaxis.tick_right()
    ax.legend(loc="upper left", fontsize=7.5, facecolor=BG, edgecolor=GRID, labelcolor=FG)
    ax.set_title(title or f"{symbol} · {minutes}-min · {et(open_ts).strftime('%a %b %d')}", color=FG, fontsize=10,
                 loc="left")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()



