"""Intraday setups, written once for longs and run on mirrored prices for shorts (so both sides follow exactly
the same rules), plus the exit plans every alert, paper trade and backtest uses.

Signals are judged on completed 5-minute bars of the regular session:

  orb5  In-play 5-minute breakout (Zarattini, Barbon & Aziz, 2024): only the ~20 stocks trading far more than usual in
        their first 5 minutes; a green first candle sets a buy-stop at its high, the stop is 10% of the daily range
        (ATR) and the trade is held to the close — few big winners pay for many small losses
  band  Index momentum (Zarattini, Aziz & Barbon, 2024): SPY / QQQ breaking out of the day's "noise band" (how far it
        usually moves from the open by that time of day), checked every half hour; out when it closes back inside the
        band or under VWAP at a half-hour check
  eod   End-of-day bounce (Baltussen, Da & Soebhag, 2024 — 2nd place, Quantpedia Awards 2025): at 3:30pm, the day's
        biggest losers (bottom tenth of the liquid universe by return since yesterday's close) tend to bounce into the
        close; bought at 3:30, sold at 3:59. A small edge (a few hundredths of a percent): costs decide it
  orb   Opening-range breakout: first close beyond the opening range (default 15 min), with VWAP and volume behind it
  vwap  VWAP reclaim / rejection: a stock on the right side of yesterday's close dips through VWAP and takes it back
        on volume within 30 minutes
  pmh   Gap-and-go: a gapper's first close through its premarket high (low for gap-downs) in the first hour, in play
  leg2  Second leg (bull / bear flag): a strong first leg, an orderly pullback on lighter volume that holds VWAP,
        then the break of the flag — the "pump, dip, pump again" pattern, on a 5-minute chart

Exit plans:
  standard  stop as given; half at 1R and the stop to break-even; the rest at 2R          (orb, vwap, pmh, leg2)
  close     stop as given, no target: held to 3:55pm                                       (orb5)
  band      no fixed stop: out when a half-hour close falls back inside the band / VWAP   (band)
  eod       no stop: bought at 3:30pm, sold at 3:59pm                                      (eod)
Everything still open is closed at 3:55pm. Inside a bar that touches both the stop and a target the stop is assumed
to come first. A buy-stop entry that fills in the same bar that touches the stop counts as stopped out.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .bars import C, H, L, O, T, V, rvol, vwap

BAR = 300                       # 5-minute bars
EXIT_BEFORE_CLOSE = 5 * 60      # flat at 3:55pm

ORB5_STOP_ATR = 0.10            # in-play breakout: stop at 10% of the daily ATR
STOP_ENTRY_CUTOFF = 60 * 60     # a buy-stop that hasn't filled by 3pm is cancelled
BAND_FIRST, BAND_LAST, BAND_STEP = 30, 330, 30      # band entries checked 10:00am … 3:00pm (minutes after the open)
BAND_EXIT_LAST = 360            # band exits checked through 3:30pm; 3:55pm closes the rest
BAND_MIN_R_ATR = 0.20           # a band trade's 1R is never under 20% of the daily ATR (results compare with stops)
PLANS = {"orb5": "close", "band": "band", "eod": "eod"}    # everything else: "standard"
EOD_START = 30 * 60             # the end-of-day bounce ranks at 3:30pm (30 minutes before the close)
EOD_R_ATR = 0.25                # its 1R (it has no stop): a quarter of the daily ATR
EOD_DECILE = 0.10               # the bottom (top) tenth of the universe by return since yesterday's close
ENTRIES = {"orb5": "stop"}                         # everything else: "market" (the next bar's open)

LABELS = {
    ("orb5", "long"): "In-play 5-min breakout", ("orb5", "short"): "In-play 5-min breakdown",
    ("band", "long"): "Index momentum up (noise-band breakout)",
    ("band", "short"): "Index momentum down (noise-band breakdown)",
    ("eod", "long"): "End-of-day bounce (day's biggest losers)", ("eod", "short"): "End-of-day fade (day's biggest winners)",
    ("orb", "long"): "Opening-range breakout", ("orb", "short"): "Opening-range breakdown",
    ("vwap", "long"): "VWAP reclaim", ("vwap", "short"): "VWAP rejection",
    ("pmh", "long"): "Gap-and-go (premarket high break)", ("pmh", "short"): "Gap-down breakdown (premarket low break)",
    ("leg2", "long"): "Second leg up (bull flag)", ("leg2", "short"): "Second leg down (bear flag)",
}
SETUP_NAMES = ("orb5", "band", "eod", "orb", "vwap", "pmh", "leg2")
ABOUT = {
    "orb5": "The day's ~20 busiest stocks at the open: buy if it breaks its first 5-minute high, small stop (10% of its "
            "daily range), hold to the close. Few winners, but big ones. From Zarattini, Barbon & Aziz (2024).",
    "band": "SPY / QQQ: buy when, at a half-hour check, it's further from the open than it usually gets by that time; "
            "out when it falls back. From Zarattini, Aziz & Barbon (2024).",
    "eod": "At 3:30pm, buy the day's biggest losers among liquid stocks; sell at 3:59pm. They tend to bounce into the "
           "close. Small edge — costs decide it. From Baltussen, Da & Soebhag (2024; 2nd place, Quantpedia Awards 2025).",
    "orb": "First 5-minute close above the first 15 minutes' range, on volume, above VWAP. Half at 1R, rest at 2R.",
    "vwap": "On a green day, a dip under VWAP that's bought back on volume. Half at 1R, rest at 2R.",
    "pmh": "A stock that gapped 3%+ breaking its premarket high in the first hour. Half at 1R, rest at 2R.",
    "leg2": "A strong first leg, an orderly pullback on light volume, then the next leg. Half at 1R, rest at 2R.",
}


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
    plan: str = "standard"       # standard | close | band (see the module docstring)
    entry_type: str = "market"   # market: next bar's open · stop: when price trades through `entry`
    expires: int = 0             # a stop entry not filled by then is cancelled

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

    pmh_low: float = 0.0         # premarket low (used when mirroring)
    in_play: int = 0             # rank among today's stocks in play (0 = not in play)
    rvol5: float = 0.0           # first-5-minute volume vs its usual first 5 minutes
    band: dict | None = None     # minutes after the open -> usual move from the open by then (fraction), SPY / QQQ

    def mirror(self) -> "Day":
        flip = [(b[T], -b[O], -b[L], -b[H], -b[C], b[V]) for b in self.bars]
        return replace(self, bars=flip, vw=[-x for x in self.vw], pdc=-self.pdc if self.pdc else 0.0,
                       pmh=-self.pmh_low if self.pmh_low else 0.0, pmh_low=-self.pmh if self.pmh else 0.0,
                       gap_pct=-self.gap_pct, sign=-self.sign)

    def upto(self, i: int) -> "Day":
        """The day as it looked when bar i closed."""
        return replace(self, bars=self.bars[:i + 1], vw=self.vw[:i + 1])

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
    up = d.sign > 0
    return b[C], stop, (f"first close {'above' if up else 'below'} the {k * 5}-min opening range "
                        f"{'high' if up else 'low'} {abs(orh):.2f} on {vol_x:.1f}x volume, {'above' if up else 'below'} "
                        f"VWAP"), {"orh": orh, "orl": orl, "vol_x": vol_x}


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
    return b[C], stop, (f"gapped {'up' if d.sign > 0 else 'down'} {abs(d.gap_pct):.1f}% and closed through the premarket "
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


def _orb5(d: Day, i: int, cfg: dict) -> tuple | None:
    if not d.in_play or i != 0:
        return None                                          # decided once, on the first 5-minute candle
    b = d.bars[0]
    if b[C] <= b[O]:
        return None                                          # a red first candle is a short (the mirror), a doji nothing
    level = b[H]
    stop = level - ORB5_STOP_ATR * d.atr
    up = d.sign > 0
    return level, stop, (f"in play today: #{d.in_play} by opening volume ({d.rvol5:.1f}x its usual first 5 minutes), "
                         f"first 5-min candle {'green' if up else 'red'} — triggers if it trades "
                         f"{'above' if up else 'below'} {abs(level):.2f}"), {"rank": d.in_play, "rvol5": d.rvol5}


def band_upper(d: Day, minutes: int) -> float | None:
    """The top of the noise band at `minutes` after the open (long side; the mirror gives the bottom)."""
    sigma = (d.band or {}).get(minutes)
    if not sigma or not d.bars:
        return None
    o = d.bars[0][O]
    return max(o, d.pdc) + sigma * abs(o) if d.pdc else o + sigma * abs(o)


def _band(d: Day, i: int, cfg: dict) -> tuple | None:
    if not d.band:
        return None
    end = int(round((d.bars[i][T] + BAR - d.open_ts) / 60))
    if end % BAND_STEP or not BAND_FIRST <= end <= BAND_LAST:
        return None
    upper = band_upper(d, end)
    if upper is None:
        return None
    b = d.bars[i]
    if b[C] <= max(upper, d.vw[i]):
        return None
    o = d.bars[0][O]
    dist = max(d.band[end] * abs(o), BAND_MIN_R_ATR * d.atr)    # 1R: the usual move by now (a fifth of a day at least)
    ref = max(o, d.pdc) if d.pdc else o
    up = d.sign > 0
    return b[C], b[C] - dist, (f"{'broke out above' if up else 'broke down below'} today's noise band "
                               f"({abs(upper):.2f}) at the half-hour check, on the right side of VWAP"), \
        {"upper": upper, "ref": ref, "open": abs(o), "band": dict(d.band), "dist": dist}


DETECTORS = {"orb5": _orb5, "band": _band, "orb": _orb, "vwap": _vwap, "pmh": _pmh, "leg2": _leg2}
PRICE_KEYS = {"orh", "orl", "hod", "upper", "ref"}


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
            plan = PLANS.get(name, "standard")
            if plan == "standard" and not (0.08 * d.atr <= risk <= 0.6 * d.atr):
                continue
            if risk <= 0 or (plan != "band" and risk < 0.0015 * abs(entry)):
                continue                                     # a stop this tight is eaten by the spread
            s = d.sign
            t1, t2 = ((entry + risk) * s, (entry + 2 * risk) * s) if plan == "standard" else (0.0, 0.0)
            entry_type = ENTRIES.get(name, "market")
            sig = Signal(name, direction, day.symbol, int(d.bars[i][T] + BAR), entry * s, stop * s, t1, t2, reason,
                         {k: (v * s if k in PRICE_KEYS else v) for k, v in ctx.items()}, plan, entry_type,
                         d.close_ts - STOP_ENTRY_CUTOFF if entry_type == "stop" else 0)
            fired.add(key)
            out.append(sig)
    return out


def eod_rank(moves: dict[str, float], decile: float = EOD_DECILE, least: int = 5, most: int = 30) -> tuple[list, list]:
    """{symbol: return since yesterday's close} -> (biggest losers, biggest winners), worst / best first."""
    order = sorted(moves, key=lambda k: moves[k])
    n = min(most, max(least, int(len(order) * decile))) if len(order) >= 2 * least else 0
    return order[:n], order[::-1][:n]


def eod_signal(symbol: str, direction: str, price: float, ts: int, close_ts: int, atr: float, move: float,
               rank: int) -> Signal | None:
    """The end-of-day bounce (long, a loser) or fade (short, a winner), bought at `price` at 3:30pm."""
    if price <= 0 or atr <= 0:
        return None
    s = 1 if direction == "long" else -1
    risk = EOD_R_ATR * atr
    word = "down" if move < 0 else "up"
    reason = (f"#{rank} {'biggest loser' if s > 0 else 'biggest winner'} of the day at 3:30pm "
              f"({word} {abs(move) * 100:.1f}% since yesterday's close) — these tend to "
              f"{'bounce' if s > 0 else 'give some back'} into the close")
    return Signal("eod", direction, symbol, int(ts), price, price - s * risk, 0.0, 0.0, reason,
                  {"rank": rank, "move": move}, "eod", "market", 0)


def fill_minute_stopped(b: tuple, s: int, level: float, stop: float) -> bool:
    """A buy-stop filled inside this minute: was it stopped in the same minute? Yes if the minute closed past the
    stop, or if it opened through the level (filled at the open) and then traded through the stop. A minute that
    climbed through the level and closed above the stop most likely made its low before the fill."""
    op, cl = b[O] * s, b[C] * s
    low = b[L] if s > 0 else -b[H]
    return cl <= stop * s or (op >= level * s and low <= stop * s)


def band_trail(ctx: dict, s: int, minutes: int, vw: float) -> float | None:
    """Where a band trade gets out at a half-hour check, in the trade's direction (s=1 long, -1 short): the band edge
    or VWAP, whichever is tighter. None when there's no band value for that time."""
    table = ctx.get("band") or {}
    sigma = table.get(minutes, table.get(str(minutes)))
    if not sigma or not ctx.get("ref"):
        return None
    edge = s * float(ctx["ref"]) + float(sigma) * float(ctx.get("open") or abs(ctx["ref"]))
    return max(edge, s * vw)


def simulate(sig: Signal, bars: list, close_ts: int, slippage_pct: float, bar_seconds: int = BAR) -> dict | None:
    """Trade a signal with its exit plan on the regular-session bars of its day (`bars` start at the open).

    Market entry: the open of the first bar after the signal. Stop entry: the first bar that trades through the level,
    filled at the level (or the open, if it gapped through). Plus slippage both ways. Returns None for a trade nobody
    could take (opened past the stop, or a stop entry that never filled). status 'closed' or 'open' (ran out of bars)."""
    first = next((k for k, b in enumerate(bars) if b[T] >= sig.ts), None)
    if first is None:
        return None
    s = 1 if sig.direction == "long" else -1
    slip = slippage_pct / 100
    deadline = close_ts - EXIT_BEFORE_CLOSE
    open_ts = bars[0][T]
    vws = vwap(bars) if sig.plan == "band" else []
    if sig.entry_type == "stop":
        until = sig.expires or deadline
        k = next((k for k in range(first, len(bars)) if bars[k][T] < until
                  and (bars[k][H] if s > 0 else -bars[k][L]) >= sig.entry * s), None)
        if k is None:
            return None
        fill = max(bars[k][O] * s, sig.entry * s) * s
    else:
        k = first
        fill = bars[k][O]
    entry = fill * (1 + s * slip)
    if s * (entry - sig.stop) <= 0:
        return None
    risk = sig.risk if sig.plan in ("band", "eod") else s * (entry - sig.stop)
    stop, half, booked = sig.stop, False, 0.0
    exit_price, exit_ts, reason = None, 0, ""
    if sig.plan == "eod":
        last = [b for b in bars[k:] if b[T] < close_ts]
        if not last or last[-1][T] + bar_seconds < close_ts - 60:
            return None                                      # no data into the close
        exit_price = last[-1][C] * (1 - s * slip)
        move = s * (exit_price - entry)
        return {"entry": entry, "exit": exit_price, "exit_ts": close_ts, "r": move / risk, "half": False,
                "reason": "close", "risk": risk, "status": "closed", "entry_ts": bars[k][T]}
    for j in range(k, len(bars)):
        b = bars[j]
        if b[T] >= deadline:
            exit_price, exit_ts, reason = b[O] * (1 - s * slip), b[T], "time"
            break
        lo, hi = (b[L], b[H]) if s > 0 else (-b[H], -b[L])
        if sig.plan != "band" and lo <= stop * s and not (j == k and sig.entry_type == "stop"
                                                          and not fill_minute_stopped(b, s, sig.entry, stop)):
            op = b[O] * s if j > k or sig.entry_type != "stop" else fill * s
            px = min(op, stop * s) * s
            exit_price, exit_ts = px * (1 - s * slip), b[T]
            reason = "breakeven" if half else "stop"
            break
        if sig.plan == "standard":
            if not half and hi >= sig.t1 * s:
                half, booked = True, sig.t1 * (1 - s * slip)
                stop = entry
            if half and hi >= sig.t2 * s:
                exit_price, exit_ts, reason = sig.t2 * (1 - s * slip), b[T], "target"
                break
        elif sig.plan == "band" and j > k:
            minutes = int(round((b[T] + bar_seconds - open_ts) / 60))
            if minutes % BAND_STEP == 0 and minutes <= BAND_EXIT_LAST:
                trail = band_trail(sig.context, s, minutes, vws[j])
                if trail is not None and s * b[C] < trail:
                    exit_price, exit_ts, reason = b[C] * (1 - s * slip), b[T] + bar_seconds, "trail"
                    break
    status = "closed"
    if exit_price is None:
        last = bars[-1]
        exit_price, exit_ts, reason, status = last[C], last[T] + bar_seconds, "open", "open"
    move = s * (exit_price - entry)
    r = ((0.5 * s * (booked - entry) + 0.5 * move) if half else move) / risk
    return {"entry": entry, "exit": exit_price, "exit_ts": exit_ts, "r": r, "half": half, "reason": reason,
            "risk": risk, "status": status, "entry_ts": bars[k][T]}
