"""Intraday setups, written once for longs and run on mirrored prices for shorts (so both sides follow exactly
the same rules), plus the one exit plan every alert, paper trade and backtest uses.

Signals are judged on completed 5-minute bars of the regular session:

  orb   Opening-range breakout: first close beyond the opening range (default 15 min), with VWAP and volume behind it
  vwap  VWAP reclaim / rejection: a stock on the right side of yesterday's close dips through VWAP and takes it back
        on volume within 30 minutes
  pmh   Gap-and-go: a gapper's first close through its premarket high (low for gap-downs) in the first hour, in play
  leg2  Second leg (bull / bear flag): a strong first leg, an orderly pullback on lighter volume that holds VWAP,
        then the break of the flag — the "pump, dip, pump again" pattern, on a 5-minute chart

Exit plan (the same everywhere so results compare): stop as given; take half at 1R and move the stop to break-even;
the rest at 2R; anything still open is closed at 3:55pm. Inside a bar that touches both the stop and a target the
stop is assumed to come first.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .bars import C, H, L, O, T, V, rvol

BAR = 300                       # 5-minute bars
EXIT_BEFORE_CLOSE = 5 * 60      # flat at 3:55pm

LABELS = {
    ("orb", "long"): "Opening-range breakout", ("orb", "short"): "Opening-range breakdown",
    ("vwap", "long"): "VWAP reclaim", ("vwap", "short"): "VWAP rejection",
    ("pmh", "long"): "Gap-and-go (premarket high break)", ("pmh", "short"): "Gap-down breakdown (premarket low break)",
    ("leg2", "long"): "Second leg up (bull flag)", ("leg2", "short"): "Second leg down (bear flag)",
}
SETUP_NAMES = ("orb", "vwap", "pmh", "leg2")


def label(setup: str, direction: str) -> str:
    return LABELS.get((setup, direction), f"{setup} {direction}")


@dataclass
class Signal:
    setup: str
    direction: str
    symbol: str
    ts: int                      # when the signal bar closed
    entry: float
    stop: float
    t1: float
    t2: float
    reason: str
    context: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.setup}_{self.direction}"

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def label(self) -> str:
        return label(self.setup, self.direction)


@dataclass
class Day:
    """One symbol, one day: regular-session 5-minute bars, VWAP at each bar, and the day's reference levels.
    For shorts the same object is built on negated prices (`mirror`)."""
    symbol: str
    bars: list                   # regular-session 5-minute bars so far (completed)
    vw: list                     # VWAP at each bar's close
    open_ts: int
    close_ts: int
    atr: float                   # daily ATR (positive in both directions)
    avg_vol: float = 0.0
    pdc: float = 0.0
    pmh: float = 0.0             # premarket high (premarket low for the mirrored side)
    gap_pct: float = 0.0
    sign: int = 1

    def mirror(self) -> "Day":
        flip = [(b[T], -b[O], -b[L], -b[H], -b[C], b[V]) for b in self.bars]
        return Day(self.symbol, flip, [-x for x in self.vw], self.open_ts, self.close_ts, self.atr, self.avg_vol,
                   -self.pdc if self.pdc else 0.0, -self.pmh_low if self.pmh_low else 0.0, -self.gap_pct, -1)

    pmh_low: float = 0.0         # premarket low (used when mirroring)

    def rvol(self, i: int) -> float:
        if self.avg_vol <= 0:
            return 0.0
        cum = sum(b[V] for b in self.bars[:i + 1])
        minutes = (self.bars[i][T] + BAR - self.open_ts) / 60
        return rvol(cum, minutes, self.avg_vol)


def _avg(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _orb(d: Day, i: int, cfg: dict) -> tuple | None:
    k = max(1, int(cfg.get("ORB_MINUTES", 15)) // 5)
    minutes = (d.bars[i][T] + BAR - d.open_ts) / 60
    if i < k or minutes > 90:
        return None
    orh = max(b[H] for b in d.bars[:k])
    orl = min(b[L] for b in d.bars[:k])
    width = orh - orl
    b = d.bars[i]
    if not (0.15 * d.atr <= width <= 1.0 * d.atr):
        return None
    if b[C] <= orh or any(x[C] > orh for x in d.bars[k:i]):
        return None                                          # not the first close above the range
    if b[C] <= d.vw[i]:
        return None
    vol_x = b[V] / max(_avg(x[V] for x in d.bars[max(0, i - 6):i]), 1)
    if vol_x < 1.2:
        return None
    stop = (orh + orl) / 2
    return b[C], stop, (f"first close above the {k * 5}-min opening range high {abs(orh):.2f} on {vol_x:.1f}x volume, "
                        f"{'above' if d.sign > 0 else 'below'} VWAP"), {"orh": orh, "orl": orl, "vol_x": vol_x}


def _vwap(d: Day, i: int, cfg: dict) -> tuple | None:
    minutes = (d.bars[i][T] + BAR - d.open_ts) / 60
    if i < 3 or not 20 <= minutes <= 300:
        return None
    b = d.bars[i]
    if d.pdc and b[C] <= d.pdc:
        return None                                          # only with the day's direction
    if b[C] <= d.vw[i] or d.bars[i - 1][C] >= d.vw[i - 1]:
        return None
    j = i - 1
    while j >= 0 and d.bars[j][C] < d.vw[j]:
        j -= 1
    dip = i - 1 - j
    if not 1 <= dip <= 6 or j < 1:
        return None
    above_before = sum(1 for x in range(0, j + 1) if d.bars[x][C] > d.vw[x])
    if above_before < 2:
        return None                                          # a downtrend, not a dip in an uptrend
    vol_x = b[V] / max(_avg(x[V] for x in d.bars[max(0, i - 6):i]), 1)
    if vol_x < 1.2:
        return None
    low = min(x[L] for x in d.bars[j + 1:i + 1])
    stop = low - 0.02 * d.atr
    return b[C], stop, (f"dipped {'below' if d.sign > 0 else 'above'} VWAP for {dip * 5} min and took it back on "
                        f"{vol_x:.1f}x volume, {'green' if d.sign > 0 else 'red'} on the day"), {"vol_x": vol_x, "dip_min": dip * 5}


def _pmh(d: Day, i: int, cfg: dict) -> tuple | None:
    minutes = (d.bars[i][T] + BAR - d.open_ts) / 60
    if i < 1 or minutes > 60 or not d.pmh or d.gap_pct < cfg.get("GAP_MIN_PCT", 3):
        return None
    b = d.bars[i]
    if b[C] <= d.pmh or any(x[C] > d.pmh for x in d.bars[:i]) or b[C] <= d.vw[i]:
        return None
    rv = d.rvol(i)
    if d.avg_vol and rv < 1.5:
        return None
    stop = min(max(d.vw[i], b[L]) - 0.02 * d.atr, b[C] - 0.12 * d.atr)
    return b[C], stop, (f"gapped {abs(d.gap_pct):.1f}% and closed through the premarket "
                        f"{'high' if d.sign > 0 else 'low'} {abs(d.pmh):.2f}" + (f" on {rv:.1f}x relative volume" if rv else "")), \
        {"rvol": rv}


def _leg2(d: Day, i: int, cfg: dict) -> tuple | None:
    minutes = (d.bars[i][T] + BAR - d.open_ts) / 60
    if i < 4 or not 25 <= minutes <= 300:
        return None
    h = max(range(i), key=lambda x: d.bars[x][H])
    pull = i - h - 1
    if h < 1 or not 2 <= pull <= 8:
        return None
    hod = d.bars[h][H]
    start = min(range(h + 1), key=lambda x: d.bars[x][L])
    leg1 = hod - d.bars[start][L]
    if leg1 < 0.5 * d.atr or h - start < 1:
        return None
    pb = d.bars[h + 1:i]
    pl = min(x[L] for x in pb)
    retrace = (hod - pl) / leg1
    if not 0.25 <= retrace <= 0.62:
        return None
    if pl < min(d.vw[h + 1:i]) - 0.05 * d.atr:
        return None                                          # lost VWAP: not a flag any more
    leg_vol = _avg(x[V] for x in d.bars[start:h + 1])
    pb_vol = _avg(x[V] for x in pb)
    if leg_vol <= 0 or pb_vol > 0.8 * leg_vol:
        return None
    b = d.bars[i]
    flag_high = max(d.bars[i - 1][H], d.bars[i - 2][H])
    if not (flag_high < b[C] < hod) or b[C] <= d.vw[i] or b[V] < 1.2 * pb_vol:
        return None
    stop = min(pl, b[L]) - 0.02 * d.atr
    return b[C], stop, (f"first leg {leg1 / d.atr:.1f} ATR to {abs(hod):.2f}, pulled back {retrace * 100:.0f}% on lighter "
                        f"volume holding VWAP, now breaking the flag"), \
        {"leg_atr": leg1 / d.atr, "retrace": retrace, "hod": hod}


DETECTORS = {"orb": _orb, "vwap": _vwap, "pmh": _pmh, "leg2": _leg2}


def detect(day: Day, i: int, cfg: dict, fired: set, mirror: Day | None = None) -> list[Signal]:
    """Signals that trigger on bar i (each setup + direction fires at most once a day: `fired` remembers)."""
    if day.atr <= 0 or i >= len(day.bars):
        return []
    out = []
    sides = (("long", day), ("short", mirror or day.mirror()))
    for name, fn in DETECTORS.items():
        for direction, d in sides:
            key = f"{name}_{direction}"
            if key in fired:
                continue
            got = fn(d, i, cfg)
            if not got:
                continue
            entry, stop, reason, ctx = got
            risk = entry - stop
            if not (0.08 * d.atr <= risk <= 0.6 * d.atr) or risk < 0.0015 * abs(entry):
                continue
            s = d.sign
            sig = Signal(name, direction, day.symbol, int(d.bars[i][T] + BAR), entry * s, stop * s,
                         (entry + risk) * s, (entry + 2 * risk) * s, reason,
                         {k: (v * s if k in {"orh", "orl", "hod"} else v) for k, v in ctx.items()})
            fired.add(key)
            out.append(sig)
    return out


def simulate(sig: Signal, bars: list, close_ts: int, slippage_pct: float, bar_seconds: int = BAR) -> dict | None:
    """Trade a signal with the standard plan on the bars that follow it.

    Entry: the open of the first bar after the signal (plus slippage). Returns None when that open is already past
    the stop (nobody should take it). status 'closed' or 'open' (ran out of bars before the plan finished)."""
    after = [b for b in bars if b[T] >= sig.ts]
    if not after:
        return None
    s = 1 if sig.direction == "long" else -1
    slip = slippage_pct / 100
    entry = after[0][O] * (1 + s * slip)
    if s * (entry - sig.stop) <= 0:
        return None
    risk = s * (entry - sig.stop)
    stop, half, booked = sig.stop, False, 0.0
    deadline = close_ts - EXIT_BEFORE_CLOSE
    exit_price, exit_ts, reason = None, 0, ""
    for b in after:
        if b[T] >= deadline:
            exit_price, exit_ts, reason = b[O] * (1 - s * slip), b[T], "time"
            break
        lo, hi = (b[L], b[H]) if s > 0 else (-b[H], -b[L])
        op = b[O] * s
        if lo <= stop * s:
            fill = min(op, stop * s) * s
            exit_price, exit_ts = fill * (1 - s * slip), b[T]
            reason = "breakeven" if half else "stop"
            break
        if not half and hi >= sig.t1 * s:
            half, booked = True, sig.t1 * (1 - s * slip)
            stop = entry
        if half and hi >= sig.t2 * s:
            exit_price, exit_ts, reason = sig.t2 * (1 - s * slip), b[T], "target"
            break
    status = "closed"
    if exit_price is None:
        last = after[-1]
        exit_price, exit_ts, reason, status = last[C], last[T] + bar_seconds, "open", "open"
    move = s * (exit_price - entry)
    r = ((0.5 * s * (booked - entry) + 0.5 * move) if half else move) / risk
    return {"entry": entry, "exit": exit_price, "exit_ts": exit_ts, "r": r, "half": half, "reason": reason,
            "risk": risk, "status": status, "entry_ts": after[0][T]}
