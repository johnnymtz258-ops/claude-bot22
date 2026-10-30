"""Invest lab: slow, cheap, well-studied ETF strategies, tested on all the daily history Alpaca gives for free
(about 10 years: the 2018 drop, the 2020 crash, the 2022 bear market) and compared with simply holding SPY.

Why this exists: on a small account the day-trading setups earn a few hundredths of a percent a trade before the
real-world slippage the test can't see, while ETF strategies trade a few times a YEAR (so costs vanish), use free
fractional shares, and have decades of research behind them. Most of them don't beat holding SPY in a bull market —
they exist to dodge the big drops. The lab says so plainly: if nothing beats holding, the plan is "keep buying SPY".

Honest by construction
  • decisions use only data up to that day's close; trades happen at the NEXT day's open, paying a cost per dollar
    traded; prices include dividends and splits (Alpaca adjustment="all")
  • all strategies are the textbook versions with their textbook numbers (200-day average, 12-month momentum…) —
    no tuning on this data, so there's little to overfit
  • each result is compared with holding SPY over the same days, with a bootstrap interval on its Sharpe ratio
Strategies (all long-only; the defensive asset is T-bills, BIL):
  trend_spy / trend_qqq   hold the index while it's above its 200-day average, else T-bills (Faber 2007)
  dual_mom                monthly: the better of US and developed-world stocks if it beat T-bills over 12 months,
                          else bonds (Antonacci, Global Equities Momentum)
  sector3                 monthly: the 3 best sector ETFs over 6 months, each only while above its 200-day average
  faber5                  monthly: equal parts US stocks, world stocks, bonds, real estate and gold, each only while
                          above its 10-month average (Faber, Global Tactical Asset Allocation)
  rsi2_dips               buy SPY / QQQ after a sharp 2-day dip inside an uptrend, sell on the bounce (Connors)
  tqqq_trend (aggressive) the QQQ trend signal applied to 3× QQQ — big upside, and deep drops
Benchmarks: hold SPY, and 60% SPY / 40% bonds.
"""
from __future__ import annotations

import asyncio
import math

from .bars import C, O, T
from .clock import day_of
from .stats import sharpe_ci
from .util import dumps, esc, loads, money, num, pct

DEF = "BIL"                                     # the defensive asset: T-bills
SECTORS = ("XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC")
SYMBOLS = ("SPY", "QQQ", "IWM", "EFA", "EEM", "AGG", "BIL", "VNQ", "GLD", "TLT", "TQQQ") + SECTORS
YEARS = 10                                      # Alpaca's free history reaches back to 2016
TRADING_DAYS = 252


# ---- history -------------------------------------------------------------------------------------------------------
class Hist:
    """Daily prices of many ETFs on one shared calendar (SPY's), with the indicators the strategies use."""

    def __init__(self, bars: dict[str, list[tuple]]):
        anchor = bars.get("SPY") or (max(bars.values(), key=len) if bars else [])
        self.dates = [day_of(b[T]) for b in anchor]
        index = {d: i for i, d in enumerate(self.dates)}
        n = len(self.dates)
        self.n = n
        self.o: dict[str, list] = {}
        self.c: dict[str, list] = {}
        for sym, rows in bars.items():
            o: list = [None] * n
            c: list = [None] * n
            for b in rows:
                i = index.get(day_of(b[T]))
                if i is not None and num(b[C]) > 0:
                    c[i] = num(b[C])
                    o[i] = num(b[O]) if num(b[O]) > 0 else c[i]
            last = None
            for i in range(n):                                  # an odd missing day: carry the last price
                if c[i] is None:
                    if last is not None:
                        c[i] = o[i] = last
                else:
                    last = c[i]
            if any(x is not None for x in c):
                self.o[sym], self.c[sym] = o, c
        self._cache: dict = {}

    def has(self, sym: str) -> bool:
        return sym in self.c

    def first(self, sym: str) -> int:
        c = self.c.get(sym)
        return next((i for i, v in enumerate(c) if v is not None), self.n) if c else self.n

    def sma(self, sym: str, n: int) -> list:
        key = ("sma", sym, n)
        if key not in self._cache:
            c, out = self.c[sym], [None] * self.n
            f = self.first(sym)
            pre = [0.0]
            for i in range(self.n):
                pre.append(pre[-1] + (c[i] or 0.0))
            for i in range(f + n - 1, self.n):
                out[i] = (pre[i + 1] - pre[i + 1 - n]) / n
            self._cache[key] = out
        return self._cache[key]

    def ret(self, sym: str, n: int) -> list:
        key = ("ret", sym, n)
        if key not in self._cache:
            c, out = self.c[sym], [None] * self.n
            for i in range(self.first(sym) + n, self.n):
                out[i] = c[i] / c[i - n] - 1
            self._cache[key] = out
        return self._cache[key]

    def rsi(self, sym: str, n: int = 2) -> list:
        """Wilder's RSI (the smoothing Connors' RSI(2) uses)."""
        key = ("rsi", sym, n)
        if key not in self._cache:
            c, out = self.c[sym], [None] * self.n
            f = self.first(sym)
            if self.n > f + n:
                gains = [max(0.0, c[i] - c[i - 1]) for i in range(f + 1, f + n + 1)]
                loss = [max(0.0, c[i - 1] - c[i]) for i in range(f + 1, f + n + 1)]
                ag, al = sum(gains) / n, sum(loss) / n
                out[f + n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
                for i in range(f + n + 1, self.n):
                    ag = (ag * (n - 1) + max(0.0, c[i] - c[i - 1])) / n
                    al = (al * (n - 1) + max(0.0, c[i - 1] - c[i])) / n
                    out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
            self._cache[key] = out
        return self._cache[key]

    def month_end(self, t: int) -> bool:
        """Is day t the last trading day of its month (in this data)?"""
        return t == self.n - 1 or self.dates[t][:7] != self.dates[t + 1][:7]


# ---- strategies ---------------------------------------------------------------------------------------------------
class Strategy:
    key = ""
    label = ""
    about = ""
    risk = "normal"                 # normal | aggressive | benchmark
    monthly = False                 # decides on the last trading day of each month and rebalances then
    assets: tuple = ()              # what it needs prices for (it is skipped when one is missing)
    lookback = 0                    # trading days of history it needs before its first decision

    def usable(self, h: Hist) -> bool:
        return all(h.has(a) for a in self.assets)

    def start(self, h: Hist) -> int:
        return max(h.first(a) for a in self.assets) + self.lookback

    def target(self, h: Hist, t: int, state: dict) -> dict:
        """Wanted weights (summing to 1) decided on day t's close; {} means all cash."""
        raise NotImplementedError

    def explain(self, h: Hist, t: int) -> str:
        return ""


def _above(h: Hist, sym: str, n: int, t: int) -> tuple[bool | None, float | None]:
    s, c = h.sma(sym, n)[t], h.c[sym][t]
    if s is None or c is None:
        return None, None
    return c > s, c / s - 1


class BuyHold(Strategy):
    risk = "benchmark"

    def __init__(self, key, label, weights, about=""):
        self.key, self.label, self.about, self.weights = key, label, about, weights
        self.assets = tuple(weights)
        self.monthly = len(weights) > 1

    def target(self, h, t, state):
        return dict(self.weights)

    def explain(self, h, t):
        return "just holds it"


class Trend(Strategy):
    lookback = 200

    def __init__(self, key, label, signal, hold=None, about="", risk="normal"):
        self.key, self.label, self.signal, self.hold, self.about, self.risk = key, label, signal, hold or signal, about, risk
        self.assets = tuple(dict.fromkeys((signal, self.hold)))

    def target(self, h, t, state):
        up, _ = _above(h, self.signal, 200, t)
        return {self.hold: 1.0} if up else {DEF: 1.0}

    def explain(self, h, t):
        up, gap = _above(h, self.signal, 200, t)
        if up is None:
            return "not enough history yet"
        return (f"{self.signal} is {abs(gap) * 100:.1f}% {'above' if up else 'below'} its 200-day average → "
                + (f"hold {self.hold}" if up else "T-bills"))


class DualMomentum(Strategy):
    key, label, monthly, lookback = "dual_mom", "Dual momentum (US vs world stocks)", True, TRADING_DAYS
    assets = ("SPY", "EFA", "AGG")
    about = "Each month hold whichever of US or developed-world stocks gained more over 12 months — but only if that " \
            "beat T-bills; otherwise bonds (Antonacci)."

    def target(self, h, t, state):
        a, b = h.ret("SPY", TRADING_DAYS)[t], h.ret("EFA", TRADING_DAYS)[t]
        bil = h.ret(DEF, TRADING_DAYS)[t] if h.has(DEF) and h.ret(DEF, TRADING_DAYS)[t] is not None else 0.0
        if a is None or b is None:
            return {DEF: 1.0}
        best, r = ("SPY", a) if a >= b else ("EFA", b)
        return {best: 1.0} if r > bil else {"AGG": 1.0}

    def explain(self, h, t):
        a, b = h.ret("SPY", TRADING_DAYS)[t], h.ret("EFA", TRADING_DAYS)[t]
        if a is None or b is None:
            return "not enough history yet"
        w = next(iter(self.target(h, t, {})))
        return f"12-month: US {a * 100:+.1f}%, world {b * 100:+.1f}% → {w}"


class Sector3(Strategy):
    key, label, monthly, lookback = "sector3", "Sector rotation (top 3)", True, 200
    about = "Each month hold the 3 sector ETFs with the best 6-month gain, each only while above its 200-day average " \
            "(otherwise that third sits in T-bills)."

    @property
    def assets(self):
        return ("SPY",) + SECTORS[:9]                        # the 9 long-standing sectors are required

    def _ranked(self, h, t):
        out = []
        for s in SECTORS:
            if h.has(s) and h.ret(s, 126)[t] is not None and h.sma(s, 200)[t] is not None:
                out.append((h.ret(s, 126)[t], s))
        return sorted(out, reverse=True)[:3]

    def target(self, h, t, state):
        w: dict = {}
        picks = self._ranked(h, t)
        for _, s in picks:
            key = s if h.c[s][t] > h.sma(s, 200)[t] else DEF
            w[key] = w.get(key, 0.0) + 1 / 3
        left = 1 - sum(w.values())
        if left > 1e-9:
            w[DEF] = w.get(DEF, 0.0) + left
        return w

    def explain(self, h, t):
        picks = self._ranked(h, t)
        return "top 6-month sectors: " + ", ".join(f"{s} {r * 100:+.0f}%" for r, s in picks) if picks else "no data"


class Faber5(Strategy):
    key, label, monthly, lookback = "faber5", "Five-asset trend (Faber)", True, 210
    assets = ("SPY", "EFA", "AGG", "VNQ", "GLD")
    about = "Equal parts US stocks, world stocks, bonds, real estate and gold — each held only while above its " \
            "10-month average, else T-bills."

    def target(self, h, t, state):
        w: dict = {}
        for a in self.assets:
            up, _ = _above(h, a, 210, t)
            key = a if up else DEF
            w[key] = w.get(key, 0.0) + 0.2
        return w

    def explain(self, h, t):
        ins = [a for a in self.assets if _above(h, a, 210, t)[0]]
        return f"in: {', '.join(ins) or 'none'}" + ("" if len(ins) == 5 else f" · out: {', '.join(a for a in self.assets if a not in ins)}")


class Rsi2Dips(Strategy):
    key, label, lookback = "rsi2_dips", "Dip buying (RSI-2)", 200
    assets = ("SPY", "QQQ")
    about = "After a sharp 2-day drop inside an uptrend, buy SPY or QQQ; sell the first close above its 5-day average " \
            "(Connors). Holds a few days at a time."

    def target(self, h, t, state):
        held = state.setdefault("in", {})
        w = {DEF: 0.0}
        for s in self.assets:
            r, c = h.rsi(s, 2)[t], h.c[s][t]
            sma5, sma200 = h.sma(s, 5)[t], h.sma(s, 200)[t]
            if r is None or sma200 is None:
                continue
            if held.get(s):
                if c > sma5:
                    held[s] = False
            elif r < 10 and c > sma200:
                held[s] = True
            w[s if held.get(s) else DEF] = w.get(s if held.get(s) else DEF, 0.0) + 0.5
        return {k: v for k, v in w.items() if v > 1e-9} or {DEF: 1.0}

    def explain(self, h, t):
        parts = []
        for s in self.assets:
            r = h.rsi(s, 2)[t]
            if r is not None:
                parts.append(f"{s} RSI-2 {r:.0f}")
        return "buys when RSI-2 < 10 in an uptrend · now " + ", ".join(parts)


def strategies() -> list[Strategy]:
    return [
        BuyHold("bh_spy", "Hold SPY", {"SPY": 1.0}, "The benchmark: buy and never sell."),
        BuyHold("mix6040", "60/40 stocks and bonds", {"SPY": 0.6, "AGG": 0.4}, "The classic balanced mix, rebalanced monthly."),
        Trend("trend_spy", "Trend filter · SPY", "SPY",
              about="Hold SPY while it is above its 200-day average; otherwise T-bills (Faber)."),
        Trend("trend_qqq", "Trend filter · QQQ", "QQQ",
              about="Hold QQQ (Nasdaq-100) while it is above its 200-day average; otherwise T-bills."),
        DualMomentum(),
        Sector3(),
        Faber5(),
        Rsi2Dips(),
        Trend("tqqq_trend", "Trend filter · 3× Nasdaq (aggressive)", "QQQ", hold="TQQQ", risk="aggressive",
              about="The QQQ trend signal, applied to TQQQ (3× Nasdaq-100): far more upside — and drops of 50% or "
                    "more. Only for money you can watch fall."),
    ]


# ---- simulating ---------------------------------------------------------------------------------------------------
def simulate(h: Hist, strat: Strategy, start: int, cost_pct: float, end: int | None = None) -> dict:
    """Run a strategy from day `start`. Decisions use day t's close; trades happen at day t+1's open and cost
    cost_pct of the dollars traded. Returns the equity curve (1.0 at start) and trade counts."""
    n = (end if end is not None else h.n)
    cost = cost_pct / 100
    pos: dict[str, float] = {}
    cash = 1.0
    curve = [1.0]
    pending = None
    last_target: dict | None = None
    state: dict = {}
    trades = changes = 0
    invested = []
    log = []
    for t in range(start, n):
        if t > start:
            for s in pos:                                       # the night: close(t-1) → open(t)
                pos[s] *= h.o[s][t] / h.c[s][t - 1]
            if pending is not None:                             # trade at the open
                eq = cash + sum(pos.values())
                new = {s: w * eq for s, w in pending.items() if h.has(s) and h.o[s][t]}
                dollars = sum(abs(new.get(s, 0.0) - pos.get(s, 0.0)) for s in set(new) | set(pos))
                fee = dollars * cost
                scale = (eq - fee) / eq if eq > 0 else 1.0
                pos = {s: v * scale for s, v in new.items()}
                cash = eq - fee - sum(pos.values())
                if dollars > 1e-9:
                    trades += 1
                pending = None
            for s in pos:                                       # the day: open(t) → close(t)
                pos[s] *= h.c[s][t] / h.o[s][t]
            curve.append(cash + sum(pos.values()))
        eq = cash + sum(pos.values())
        invested.append(sum(v for s, v in pos.items() if s != DEF) / eq if eq > 0 else 0.0)
        if strat.monthly and not h.month_end(t) and last_target is not None:
            continue
        tgt = {k: round(v, 6) for k, v in strat.target(h, t, state).items() if v > 1e-9}
        tgt = {k: v for k, v in tgt.items() if h.has(k)}
        if strat.monthly and h.month_end(t):
            if last_target is None or tgt != last_target or len(tgt) > 1:
                pending = tgt                                   # a monthly strategy rebalances to its mix each month
                if tgt != last_target:
                    changes += 1
                    log.append((h.dates[t], tgt))
        elif tgt != last_target:
            pending = tgt
            changes += 1
            log.append((h.dates[t], tgt))
        last_target = tgt
    return {"key": strat.key, "start": start, "end": n, "curve": curve, "trades": trades, "changes": changes,
            "exposure": sum(invested) / len(invested) if invested else 0.0, "log": log[-12:],
            "weights": last_target or {}}


# ---- measuring ----------------------------------------------------------------------------------------------------
def perf(curve: list[float], rf: list[float] | None = None) -> dict:
    """Total return, yearly growth, worst drop, Sharpe and Calmar of an equity curve of trading days. Sharpe is
    measured over T-bills (rf: their daily returns), so sitting in cash earns no credit for its calm."""
    if len(curve) < 2 or curve[0] <= 0:
        return {"total": 0.0, "cagr": 0.0, "max_dd": 0.0, "sharpe": 0.0, "vol": 0.0, "calmar": 0.0, "years": 0.0}
    years = (len(curve) - 1) / TRADING_DAYS
    rets = [curve[i] / curve[i - 1] - 1 - (rf[i - 1] if rf else 0.0) for i in range(1, len(curve))]
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0.0
    peak, dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    total = curve[-1] / curve[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else -1.0
    return {"total": total, "cagr": cagr, "max_dd": dd, "sharpe": mean / sd * math.sqrt(TRADING_DAYS) if sd > 0 else 0.0,
            "vol": sd * math.sqrt(TRADING_DAYS), "calmar": (cagr / -dd) if dd < 0 else 0.0, "years": years}


def by_year(dates: list[str], curve: list[float]) -> dict[str, float]:
    """Return for each calendar year, each measured from the last value of the year before."""
    out, base, year = {}, curve[0], dates[0][:4]
    last = curve[0]
    for d, v in zip(dates, curve):
        if d[:4] != year:
            out[year] = last / base - 1
            base, year = last, d[:4]
        last = v
    out[year] = last / base - 1
    return out


def verdict(res: dict, bench: dict, risk: str) -> str:
    """beat (more return AND better risk-adjusted) · smoother (a calmer ride, even if it earns less) ·
    aggressive (more return, deeper drops) · no_better · negative · thin."""
    s, b = res["perf"], bench["perf"]
    if s["years"] < 4:
        return "thin"
    if s["cagr"] <= 0:
        return "negative"
    halves_ok = res["half1"] > 0 and res["half2"] > 0
    if risk == "aggressive":
        return "aggressive" if s["cagr"] > b["cagr"] else "no_better"
    if not halves_ok:
        return "no_better"
    if s["cagr"] >= b["cagr"] and s["sharpe"] >= b["sharpe"]:
        return "beat"
    shallower = s["max_dd"] >= 0.7 * b["max_dd"] if b["max_dd"] < 0 else True
    if (shallower and s["cagr"] >= 0.5 * b["cagr"] and s["sharpe"] >= b["sharpe"] - 0.05
            and res["sharpe_ci"]["lo"] > 0):
        return "smoother"
    return "no_better"


VERDICT_TEXT = {
    "beat": "✅ Beat holding SPY (more return and a better ride)",
    "smoother": "🟢 A smoother ride than holding SPY (it earns a bit less, falls much less)",
    "aggressive": "🟠 More return than holding SPY — with much deeper drops",
    "no_better": "⚪ No better than just holding SPY",
    "negative": "🔴 Lost money",
    "thin": "⚪ Too little history to judge",
}


def run_lab(h: Hist, cost_pct: float, since: str | None = None) -> dict:
    """Every usable strategy over the same days, with benchmarks and verdicts. `since`: a date (YYYY-MM-DD) to start
    at if later than the common warm-up (used to show what you'd have had since you started following)."""
    strats = [s for s in strategies() if s.usable(h)]
    if not any(s.key == "bh_spy" for s in strats) or h.n < 300:
        return {"error": "not enough price history yet", "strategies": []}
    start = max(s.start(h) for s in strats)
    if since:
        start = max(start, next((i for i, d in enumerate(h.dates) if d >= since), start))
    if h.n - start < 60:
        return {"error": "not enough price history yet", "strategies": []}
    rf = ([h.c[DEF][t] / h.c[DEF][t - 1] - 1 for t in range(start + 1, h.n)]
          if h.has(DEF) and h.first(DEF) < start else [0.0] * (h.n - start - 1))
    results = {}
    for s in strats:
        sim = simulate(h, s, start, cost_pct)
        curve = sim["curve"]
        mid = len(curve) // 2
        excess = [curve[i] / curve[i - 1] - 1 - rf[i - 1] for i in range(1, len(curve))]
        results[s.key] = {
            "key": s.key, "label": s.label, "about": s.about, "risk": s.risk, "perf": perf(curve, rf),
            "half1": curve[mid] / curve[0] - 1, "half2": curve[-1] / curve[mid] - 1,
            "sharpe_ci": sharpe_ci(excess), "years": by_year(h.dates[start:], curve), "trades": sim["trades"],
            "trades_per_year": sim["trades"] / max(0.1, (len(curve) - 1) / TRADING_DAYS),
            "exposure": sim["exposure"], "weights": sim["weights"], "log": sim["log"], "curve": curve,
            "explain": s.explain(h, h.n - 1)}
    bench = results["bh_spy"]
    for r in results.values():
        r["verdict"] = verdict(r, bench, r["risk"]) if r["key"] != "bh_spy" else "benchmark"
    return {"start": h.dates[start], "end": h.dates[-1], "days": h.n - start, "cost_pct": cost_pct,
            "strategies": [r for r in results.values()], "dates": h.dates[start:]}


def recommend(lab: dict) -> str:
    """The plan worth following: the best Sharpe among those that beat or smooth holding SPY — or 'bh_spy'."""
    best, best_sharpe = "bh_spy", -9.0
    for r in lab.get("strategies", []):
        if r["verdict"] in ("beat", "smoother") and r["perf"]["sharpe"] > best_sharpe:
            best, best_sharpe = r["key"], r["perf"]["sharpe"]
    return best


def thin_curve(dates: list[str], curve: list[float], n: int = 180) -> list[list]:
    step = max(1, -(-len(curve) // n))
    idx = list(range(0, len(curve), step))
    if idx[-1] != len(curve) - 1:
        idx.append(len(curve) - 1)
    return [[dates[i], round(curve[i] * 100, 2)] for i in idx]


def month_label(day: str) -> str:
    return day[:7]


# ---- what you hold vs what the plan wants -------------------------------------------------------------------------
BUCKETS = {
    "SPY": ("SPY", "VOO", "VTI", "IVV", "SPLG", "ITOT", "SCHB"), "QQQ": ("QQQ", "QQQM", "ONEQ"),
    "EFA": ("EFA", "VEA", "IEFA", "SCHF", "VXUS", "IXUS"), "EEM": ("EEM", "VWO", "IEMG", "SCHE"),
    "AGG": ("AGG", "BND", "SCHZ", "BNDX"), "BIL": ("BIL", "SGOV", "SHV", "SHY", "USFR"),
    "GLD": ("GLD", "IAU", "GLDM"), "VNQ": ("VNQ", "SCHH", "IYR"), "IWM": ("IWM", "VB", "IJR"),
    "TLT": ("TLT", "VGLT"), "TQQQ": ("TQQQ",), **{s: (s,) for s in SECTORS}}
PREFER = {"SPY": "VOO", "QQQ": "QQQM", "EFA": "VEA", "EEM": "VWO", "AGG": "BND", "BIL": "BIL", "GLD": "GLD",
          "VNQ": "VNQ", "IWM": "IWM", "TLT": "TLT", "TQQQ": "TQQQ"}
BUCKET_NAME = {"SPY": "US stocks", "QQQ": "Nasdaq-100", "EFA": "world stocks", "EEM": "emerging markets",
               "AGG": "bonds", "BIL": "T-bills / cash", "GLD": "gold", "VNQ": "real estate", "IWM": "small caps",
               "TLT": "long bonds", "TQQQ": "3× Nasdaq", "XLK": "tech", "XLF": "financials", "XLE": "energy",
               "XLV": "health care", "XLY": "consumer discretionary", "XLP": "consumer staples", "XLI": "industrials",
               "XLU": "utilities", "XLB": "materials", "XLRE": "real estate sector", "XLC": "communications"}
_BUCKET_OF = {t: b for b, ts in BUCKETS.items() for t in ts}
MIN_MOVE = 5.0                      # dollars: smaller differences aren't worth a trade


def bucket_of(symbol: str) -> str | None:
    return _BUCKET_OF.get(str(symbol).upper())


def holdings_by_bucket(rows: list[dict]) -> tuple[dict, dict, float]:
    """({bucket: dollars}, {bucket: {ticker: dollars}}, dollars held outside the plan's world) from position rows."""
    by_bucket: dict[str, float] = {}
    tickers: dict[str, dict] = {}
    other = 0.0
    for r in rows:
        value = num(r.get("qty")) * num(r.get("price"))
        if value <= 0 or r.get("kind", "stock") != "stock":
            continue
        b = bucket_of(r.get("symbol"))
        if not b:
            other += value
            continue
        by_bucket[b] = by_bucket.get(b, 0.0) + value
        t = tickers.setdefault(b, {})
        t[str(r["symbol"]).upper()] = t.get(str(r["symbol"]).upper(), 0.0) + value
    return by_bucket, tickers, other


def plan_actions(weights: dict, held: dict, tickers: dict, budget: float) -> list[dict]:
    """The trades that move what you hold to the plan's weights, sells first. weights are by bucket (SPY, BIL…)."""
    out = []
    floor = max(MIN_MOVE, 0.02 * budget)
    for b in sorted(set(weights) | set(held)):
        want, have = weights.get(b, 0.0) * budget, held.get(b, 0.0)
        diff = want - have
        if abs(diff) < floor:
            continue
        mine = tickers.get(b) or {}
        ticker = max(mine, key=mine.get) if mine else PREFER.get(b, b)
        out.append({"ticker": ticker, "bucket": b, "name": BUCKET_NAME.get(b, b), "side": "buy" if diff > 0 else "sell",
                    "dollars": round(abs(diff), 2), "have": round(have, 2), "want": round(want, 2)})
    return sorted(out, key=lambda a: (a["side"] != "sell", -a["dollars"]))


def prefer_names(weights: dict, tickers: dict) -> str:
    return " + ".join(f"{int(round(w * 100))}% "
                      f"{(max(tickers[b], key=tickers[b].get) if tickers.get(b) else PREFER.get(b, b))}"
                      for b, w in sorted(weights.items(), key=lambda kv: -kv[1]))


# ---- live ---------------------------------------------------------------------------------------------------------
KEEP_CURVE = ("key", "label", "about", "risk", "verdict", "perf", "half1", "half2", "sharpe_ci", "years",
              "trades_per_year", "exposure", "weights", "explain", "log")


def compact(lab: dict) -> dict:
    """The lab result without the full daily curves (thinned to ~180 points each), small enough to keep in meta."""
    dates = lab.get("dates") or []
    out = {k: v for k, v in lab.items() if k not in ("strategies", "dates")}
    out["strategies"] = []
    for r in lab.get("strategies", []):
        row = {k: r[k] for k in KEEP_CURVE if k in r}
        row["curve"] = thin_curve(dates, r["curve"]) if r.get("curve") and dates else r.get("curve_thin", [])
        out["strategies"].append(row)
    return out


class Invest:
    """The live Invest lab: loads ETF history after each close, runs the lab, tells you when the plan changes and what
    that means for the money you hold. Nothing here places a trade."""

    def __init__(self, cfg, db, alpaca, market, notify):
        self.cfg, self.db, self.alpaca, self.market, self.notify = cfg, db, alpaca, market, notify
        self.bars: dict[str, list] = {}
        self.lab: dict | None = None
        self.last_error = ""
        self._last: dict = {}
        self._lab_key = None
        self._marker = None
        self._busy = False

    @property
    def on(self) -> bool:
        return self.cfg.flag("INVEST")

    def every(self, name: str, seconds: float, now: float) -> bool:
        if now - self._last.get(name, 0) >= seconds:
            self._last[name] = now
            return True
        return False

    # ---- data and the lab ----------------------------------------------------------------------------------------
    def _complete(self, bar: tuple, now: float) -> bool:
        day, today = day_of(bar[T]), day_of(now)
        if day < today:
            return True
        return day == today and self.market.is_trading_day(day) and now >= self.market.close_ts(day) + 1200

    async def load(self, now: float) -> None:
        start = now - (YEARS * 365 + 30) * 86400
        try:
            got = await self.alpaca.bars(list(SYMBOLS), "1Day", start, now, feed="sip", adjustment="all")
        except Exception as exc:
            self.last_error = f"ETF history: {exc}"
            return
        self.bars = {sym: [b for b in rows if self._complete(b, now)] for sym, rows in got.items() if rows}

    def lab_now(self) -> dict | None:
        return self.lab or loads(self.db.get_meta("invest_lab"), None)

    def cost(self) -> float:
        return self.cfg.get("INVEST_COST_PCT")

    async def refresh_lab(self, now: float) -> dict | None:
        if not self.bars.get("SPY"):
            return None
        key = (self.bars["SPY"][-1][T], len(self.bars), self.cost())
        if key == self._lab_key and self.lab:
            return self.lab
        self._busy = True
        try:
            def work():
                h = Hist(self.bars)
                return run_lab(h, self.cost())
            lab = await asyncio.get_running_loop().run_in_executor(None, work)
        except Exception as exc:
            self.last_error = f"invest lab: {type(exc).__name__}: {exc}"
            return None
        finally:
            self._busy = False
        if lab.get("error"):
            self.last_error = lab["error"]
            return None
        lab["recommended"] = recommend(lab)
        lab["ts"] = int(now)
        self.lab, self._lab_key = compact(lab), key
        self.db.set_meta("invest_lab", dumps(self.lab))
        if not self.db.get_meta("invest_since"):
            self.db.set_meta("invest_since", lab["end"])
        return lab

    def result(self, key: str | None = None) -> dict | None:
        lab = self.lab_now()
        if not lab:
            return None
        want = key or self.plan_key()
        return next((r for r in lab.get("strategies", []) if r["key"] == want), None)

    def plan_key(self) -> str:
        lab = self.lab_now() or {}
        keys = {r["key"] for r in lab.get("strategies", [])}
        chosen = self.db.get_meta("invest_plan")
        if chosen in keys:
            return chosen
        return lab.get("recommended") or recommend(lab) if lab else "bh_spy"

    def set_plan(self, key: str) -> str | None:
        lab = self.lab_now() or {}
        r = next((x for x in lab.get("strategies", []) if x["key"] == key), None)
        if not r:
            return None
        self.db.set_meta("invest_plan", key)
        return r["label"]

    # ---- the loop ------------------------------------------------------------------------------------------------
    async def tick(self, now: float) -> None:
        if not self.on or not getattr(self.alpaca, "ok", False):
            return
        day = day_of(now)
        marker = (day, self.market.is_trading_day(day) and now >= self.market.close_ts(day) + 1200)
        if not self.bars:
            if self.every("invest_first", 300, now):
                await self.load(now)
                self._marker = marker
        elif marker != self._marker and self.every("invest_load", 900, now):
            await self.load(now)                                # once a day, and again when the closing bar is in
            self._marker = marker
        if self.bars:
            before, old = self.targets_now(), self.lab
            lab = await self.refresh_lab(now)
            if lab and lab is not old:
                await self.announce(lab, before, now)

    # ---- changes and messages ------------------------------------------------------------------------------------
    def targets_now(self) -> dict:
        return loads(self.db.get_meta("invest_targets"), {}) or {}

    async def announce(self, lab: dict, before: dict, now: float) -> None:
        after = {r["key"]: {"day": lab["end"], "weights": r["weights"]} for r in lab["strategies"]}
        first = not before
        plan = self.plan_key()
        changed = [k for k in after if k in before and before[k]["weights"] != after[k]["weights"]]
        self.db.set_meta("invest_targets", dumps(after))
        if first or not self.cfg.flag("INVEST_ALERTS"):
            return
        month = lab["end"][:7]
        if plan in changed and self.db.once(f"invest_change:{plan}:{lab['end']}", int(now)):
            await self.notify(self.change_text(plan, before[plan]["weights"], after[plan]["weights"], lab), kind="INVEST")
        elif self.db.get_meta("invest_month") != month and self.market.is_trading_day(day_of(now)):
            if self.db.get_meta("invest_month"):
                await self.notify(self.monthly_text(lab), kind="INVEST", silent=True)
            self.db.set_meta("invest_month", month)

    def holdings(self) -> tuple[dict, dict, float]:
        rows = self.db.rows("select symbol, qty, price, kind from positions where qty>0")
        return holdings_by_bucket(rows)

    def budget(self, held: dict) -> float:
        b = self.cfg.get("INVEST_BUDGET")
        return b if b > 0 else sum(held.values())

    def actions(self, weights: dict) -> tuple[list[dict], float, dict, float]:
        held, tickers, other = self.holdings()
        budget = self.budget(held)
        return (plan_actions(weights, held, tickers, budget) if budget > 0 else []), budget, tickers, other

    def action_lines(self, weights: dict) -> list[str]:
        acts, budget, tickers, other = self.actions(weights)
        if budget <= 0:
            return [f"Set the money this plan manages: /set INVEST_BUDGET 500 (it shows {prefer_names(weights, tickers)})."]
        head = f"For {money(budget)}" + ("" if self.cfg.get("INVEST_BUDGET") > 0 else " (what you hold in ETFs)")
        if not acts:
            return [f"{head}: you already hold the plan ({prefer_names(weights, tickers)})."]
        lines = [f"{head}:"]
        for a in acts:
            lines.append(f"  {'🔴 Sell' if a['side'] == 'sell' else '🟢 Buy'} {money(a['dollars'])} of <b>{esc(a['ticker'])}</b>"
                         f" ({esc(a['name'])}: {money(a['have'])} → {money(a['want'])})")
        return lines

    def change_text(self, plan: str, old: dict, new: dict, lab: dict) -> str:
        r = next(x for x in lab["strategies"] if x["key"] == plan)
        lines = [f"<b>🧺 Invest plan changed — {esc(r['label'])}</b>", esc(r.get("explain", "")),
                 f"Now: {prefer_names(new, self.holdings()[1])} (was {prefer_names(old, self.holdings()[1])}) — "
                 "place it tomorrow at the open (fractional shares, no commission)."]
        lines += self.action_lines(new)
        lines.append(f"<i>{VERDICT_TEXT.get(r['verdict'], '')}. Taxes: selling in a taxable account can realize gains.</i>")
        return "\n".join(lines)

    def monthly_text(self, lab: dict) -> str:
        r = next(x for x in lab["strategies"] if x["key"] == self.plan_key())
        lines = [f"<b>🧺 Monthly check — {esc(r['label'])}</b>", esc(r.get("explain", "")),
                 f"Plan holds: {prefer_names(r['weights'], self.holdings()[1])}"]
        lines += self.action_lines(r["weights"])
        return "\n".join(lines)

    # ---- views -----------------------------------------------------------------------------------------------------
    def since(self, plan: str, lab: dict) -> dict | None:
        start = self.db.get_meta("invest_since")
        res = next((x for x in lab["strategies"] if x["key"] == plan), None)
        bench = next((x for x in lab["strategies"] if x["key"] == "bh_spy"), None)
        if not (start and res and bench and res.get("curve") and isinstance(res["curve"][0], list)):
            return None

        def at(curve):
            pts = [p for p in curve if p[0] >= start]
            return (curve[-1][1] / pts[0][1] - 1) if pts and pts[0][1] else None
        return {"since": start, "plan": at(res["curve"]), "spy": at(bench["curve"])}

    def summary(self, now: float) -> dict:
        lab = self.lab_now()
        base = {"on": self.on, "error": self.last_error, "loading": self._busy}
        if not self.on or not lab:
            return {**base, "ready": False}
        plan = self.plan_key()
        res = next((r for r in lab["strategies"] if r["key"] == plan), lab["strategies"][0])
        acts, budget, tickers, other = self.actions(res["weights"])
        held, _, _ = self.holdings()
        return {**base, "ready": True, "start": lab["start"], "end": lab["end"], "days": lab["days"],
                "cost_pct": lab["cost_pct"], "plan": plan, "recommended": lab.get("recommended") or recommend(lab),
                "strategies": [{**r, "verdict_text": VERDICT_TEXT.get(r["verdict"], "")} for r in lab["strategies"]],
                "actions": acts, "budget": budget, "held": {BUCKET_NAME.get(b, b): round(v, 2) for b, v in held.items()},
                "other": round(other, 2), "since": self.since(plan, lab),
                "plan_now": prefer_names(res["weights"], tickers), "explain": res.get("explain", "")}

    def text(self, now: float) -> str:
        if not self.on:
            return "🧺 The Invest lab is off. /set INVEST 1 turns it on."
        lab = self.lab_now()
        if not lab:
            return ("🧺 Loading ETF history from Alpaca (about 10 years, free)… try again in a minute."
                    + (f"\n{esc(self.last_error)}" if self.last_error else ""))
        plan, rec = self.plan_key(), lab.get("recommended") or "bh_spy"
        res = next(r for r in lab["strategies"] if r["key"] == plan)
        bench = next(r for r in lab["strategies"] if r["key"] == "bh_spy")
        lines = [f"<b>🧺 Invest lab</b> · {lab['start']} → {lab['end']} ({lab['days'] / TRADING_DAYS:.1f} years) · "
                 f"{lab['cost_pct']:g}% cost per trade included"]
        if rec == "bh_spy":
            lines.append("Verdict: <b>nothing here beat simply holding SPY</b> — the plan is to keep buying a broad "
                         "index fund (VOO / VTI) and not trade it.")
        else:
            r = next(x for x in lab["strategies"] if x["key"] == rec)
            lines.append(f"Verdict: the plan worth following is <b>{esc(r['label'])}</b> — {VERDICT_TEXT[r['verdict']].lstrip('✅🟢🟠⚪🔴 ')}.")
        lines.append(f"\n<b>Your plan: {esc(res['label'])}</b>" + ("" if plan == rec else " (your choice)"))
        if res.get("explain"):
            lines.append(esc(res["explain"]))
        lines += self.action_lines(res["weights"])
        since = self.since(plan, lab)
        if since and since["plan"] is not None:
            lines.append(f"Since {since['since']}: plan {pct(since['plan'] * 100)} · SPY {pct((since['spy'] or 0) * 100)}")
        lines.append("\n<b>Scoreboard</b> (a year · worst drop · Sharpe)")
        icon = {"beat": "✅", "smoother": "🟢", "aggressive": "🟠", "no_better": "⚪", "negative": "🔴", "thin": "⚪",
                "benchmark": "▫️"}
        for r in sorted(lab["strategies"], key=lambda x: -x["perf"]["sharpe"]):
            p = r["perf"]
            lines.append(f"{icon.get(r['verdict'], '⚪')} {esc(r['label'])}: {p['cagr'] * 100:+.1f}% · "
                         f"{p['max_dd'] * 100:.0f}% · {p['sharpe']:.2f}")
        lines.append(f"\nMost of these don't beat holding SPY in a rising market (it did {bench['perf']['cagr'] * 100:+.1f}% "
                     f"a year here) — they're built to dodge crashes. /invest plan trend_spy to follow another · "
                     f"/invest test to re-run.")
        return "\n".join(lines)

    def week_line(self, now: float) -> str:
        lab = self.lab_now()
        if not (self.on and lab):
            return ""
        res = next((r for r in lab["strategies"] if r["key"] == self.plan_key()), None)
        return f"🧺 Invest plan ({res['label']}): {prefer_names(res['weights'], self.holdings()[1])}" if res else ""

    def status_line(self, now: float) -> str:
        if not self.on:
            return "⚪ Invest lab: off (/set INVEST 1)"
        lab = self.lab_now()
        if not lab:
            return "⚪ Invest lab: loading ETF history" + (f" ({esc(self.last_error)})" if self.last_error else "")
        return f"✅ Invest lab: data to {lab['end']} · plan {esc(next(r['label'] for r in lab['strategies'] if r['key'] == self.plan_key()))}"
