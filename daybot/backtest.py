"""Backtest: replay every setup over the last N trading days and grade it honestly.

What gets replayed:
  • the core liquid names every day (SPY, QQQ, mega caps, the usual day-trading names), and
  • every stock in the wider universe on the days it gapped GAP_MIN_PCT+ — rebuilt from daily bars, so the test
    isn't limited to whatever happens to be hot today.
Each signal is traded with the standard plan on the bars that follow it (entry at the next bar's open, costs on
every fill, flat by 3:55pm, stop assumed first when a bar touches both).

A setup is PROVEN only when it made money after costs in BOTH halves of the period, still does without its 3
best trades, and has 40+ trades. Proven setups ping you; the rest are paper-traded (silently) until they are.
"""
from __future__ import annotations

import statistics

from .bars import C, O, T, V, atr, split_day, vwap
from .clock import Market, at, day_of
from .setups import Day, detect, label, simulate
from .util import dumps, num

CORE = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META", "AMZN", "GOOGL", "NFLX", "AVGO", "PLTR",
        "COIN", "MSTR", "SMCI", "MU", "BA", "JPM", "UBER", "SHOP", "SOFI", "HOOD", "ARM", "INTC", "CRWD", "SNOW",
        "DIS", "XOM"]

# Liquid US stocks and ETFs (gapper candidates). Missing / delisted names are simply skipped.
UNIVERSE = sorted(set(CORE + """
AAL ABBV ABNB ABT ACN ADBE ADI ADP AEP AFRM AI AIG AMAT AMGN ANET APA APH APP ASML ASTS AXP AZN BABA BAC BBY BIDU BIIB
BK BKNG BLK BMY BX C CAT CAVA CCL CDNS CELH CHWY CI CLF CMCSA CME CMG COF COP COST CPNG CRM CSCO CSX CVNA CVS CVX DAL
DASH DDOG DE DELL DG DHI DHR DKNG DLTR DOCU DOW DUK DVN EBAY ENPH EOG ETSY EXPE F FCX FDX FSLR FTNT GD GE GILD GIS GM
GME GOOG GS HAL HD HON HPE HPQ IBM INTU IONQ ISRG JD JNJ KHC KKR KO KR LCID LI LLY LMT LOW LRCX LULU LUV LVS LYFT MA
MARA MCD MDB MDLZ MDT MET MMM MO MRK MRNA MRVL MS NEE NET NIO NKE NOW NU NUE NVO OKTA ON ORCL OXY PANW PARA PDD PEP PFE
PG PINS PM PYPL QCOM RBLX RCL RDDT RIOT RIVN ROKU RTX SBUX SCHW SE SHW SIRI SLB SMH SNAP SO SPOT SQ T TGT TJX TMO TMUS
TQQQ TSM TTD TXN U UAL UNH UNP UPS UPST USB V VALE VZ W WBA WBD WDAY WFC WMT X XLE XLF XLK XPEV ZM ZS SQQQ SOXL SOXS
TNA UVXY GLD SLV TLT HYG EEM FXI KWEB ARKK XBI KRE OKLO RKLB SOUN ACHR JOBY CLSK HIMS TEM VST CEG NBIS CRWV
""".split()))

MIN_TRADES_PROVEN = 40
STATUS_TEXT = {"proven": "✅ proven", "promising": "🟡 promising", "thin": "⚪ not enough trades",
               "negative": "🔴 loses money", "benched": "⏸ benched (losing in paper)", "untested": "⚪ untested"}


def make_day(symbol: str, day: str, bars5: list, daily_prior: list, market: Market) -> tuple[Day | None, list]:
    """A full-day Day object (all regular-session bars) plus those bars, or (None, []) without data."""
    pre, rth, _ = split_day(bars5, day, market)
    prior = [d for d in daily_prior if day_of(d[T]) < day]
    if not rth or len(prior) < 15:
        return None, []
    atr_d = atr(prior[-15:], 14)
    pdc = prior[-1][C]
    avg_vol = statistics.mean(d[V] for d in prior[-20:])
    gap = (rth[0][O] / pdc - 1) * 100 if pdc else 0.0
    pmh = max((b[2] for b in pre), default=0.0)
    pml = min((b[3] for b in pre), default=0.0)
    d = Day(symbol, rth, vwap(rth), market.open_ts(day), market.close_ts(day), atr_d, avg_vol, pdc, pmh, gap, 1, pml)
    return d, rth


def replay_day(symbol: str, day: str, bars5: list, daily_prior: list, market: Market, cfg: dict,
               slippage_pct: float) -> list[dict]:
    full, rth = make_day(symbol, day, bars5, daily_prior, market)
    if not full:
        return []
    fired: set = set()
    trades = []
    close_ts = market.close_ts(day)
    for i in range(len(rth)):
        part = Day(symbol, rth[:i + 1], full.vw[:i + 1], full.open_ts, full.close_ts, full.atr, full.avg_vol, full.pdc,
                   full.pmh, full.gap_pct, 1, full.pmh_low)
        for sig in detect(part, i, cfg, fired):
            res = simulate(sig, rth, close_ts, slippage_pct)
            if res and res["status"] == "closed":
                trades.append({"setup": sig.setup, "direction": sig.direction, "symbol": symbol, "day": day,
                               "ts": sig.ts, "r": res["r"], "reason": res["reason"], "entry": res["entry"],
                               "exit": res["exit"]})
    return trades


def _stats(trades: list[dict]) -> dict:
    trades = sorted(trades, key=lambda t: (t["day"], t["ts"]))
    rs = [t["r"] for t in trades]
    n = len(rs)
    if not n:
        return {"n": 0}
    mid = n // 2
    first, second = rs[:mid] or rs, rs[mid:] or rs
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    top = sorted(rs, reverse=True)[:3]
    rest = rs.copy()
    for x in top:
        rest.remove(x)
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return {"n": n, "won": len(wins) / n, "avg_r": sum(rs) / n, "total_r": sum(rs),
            "pf": (sum(wins) / abs(sum(losses))) if losses and sum(losses) else (99.0 if wins else 0.0),
            "half1": sum(first) / len(first), "half2": sum(second) / len(second),
            "no_best": (sum(rest) / len(rest)) if rest else 0.0, "max_dd": dd,
            "days": len({t["day"] for t in trades})}


def grade(st: dict) -> str:
    if not st.get("n") or st["n"] < 15:
        return "thin"
    if st["avg_r"] <= 0:
        return "negative"
    if (st["n"] >= MIN_TRADES_PROVEN and st["half1"] >= 0.05 and st["half2"] >= 0.05 and st["no_best"] > 0
            and st["avg_r"] >= 0.08):
        return "proven"
    return "promising"


def summarize(trades: list[dict]) -> dict[str, dict]:
    groups: dict[str, list] = {}
    for t in trades:
        groups.setdefault(f"{t['setup']}_{t['direction']}", []).append(t)
    out = {}
    for key, rows in groups.items():
        st = _stats(rows)
        st["status"] = grade(st)
        out[key] = st
    return out


def gapper_days(daily: dict[str, list], days: list[str], cfg) -> list[tuple[str, str]]:
    """(symbol, day) pairs where a liquid stock opened GAP_MIN_PCT+ away from the prior close."""
    out = []
    want = set(days)
    for sym, rows in daily.items():
        for idx in range(1, len(rows)):
            prev, cur = rows[idx - 1], rows[idx]
            day = day_of(cur[T])
            if day not in want or prev[C] <= 0:
                continue
            gap = (cur[O] / prev[C] - 1) * 100
            avg_vol = statistics.mean(r[V] for r in rows[max(0, idx - 20):idx]) if idx else 0
            if (abs(gap) >= cfg.get("GAP_MIN_PCT") and cur[O] >= cfg.get("MIN_PRICE")
                    and avg_vol >= cfg.get("MIN_AVG_VOLUME")):
                out.append((sym, day))
    return out


async def run(alpaca, market: Market, cfg, today: str, days: int | None = None, progress=None,
              core: list[str] | None = None, universe: list[str] | None = None) -> dict:
    """Fetch history and replay. Returns {"stats": {...}, "trades": n, "pairs": n, "days": [...]}."""
    days = int(days or cfg.get("BACKTEST_DAYS"))
    day_list = market.trading_days_back(today, days)
    core = core or CORE
    universe = universe or UNIVERSE
    first_ts = at(day_list[0], "00:00")
    daily = await alpaca.bars(universe, "1Day", first_ts - 45 * 86400, at(day_list[-1], "20:00"), feed="sip")
    pairs = {(s, d) for s in core for d in day_list}
    pairs |= set(gapper_days(daily, day_list, cfg))
    by_day: dict[str, list[str]] = {}
    for sym, day in pairs:
        by_day.setdefault(day, []).append(sym)
    values = {name: cfg.get(name) for name in ("ORB_MINUTES", "GAP_MIN_PCT")}
    slip = cfg.get("SLIPPAGE_PCT")
    trades: list[dict] = []
    for n, day in enumerate(sorted(by_day)):
        syms = sorted(by_day[day])
        bars = await alpaca.bars(syms, "5Min", at(day, "04:00"), at(day, "16:00"), feed="sip")
        for sym in syms:
            trades.extend(replay_day(sym, day, bars.get(sym) or [], daily.get(sym) or [], market, values, slip))
        if progress:
            await progress(n + 1, len(by_day))
    stats = summarize(trades)
    return {"stats": stats, "trades": len(trades), "pairs": len(pairs), "days": day_list, "list": trades}


def save(db, result: dict, now: int) -> None:
    days = len(result.get("days") or [])
    for key, st in result["stats"].items():
        setup, direction = key.split("_", 1)
        db.insert("""insert into backtests(ts, setup, direction, n, won, avg_r, total_r, pf, half1, half2, no_best,
                     max_dd, status, days, detail) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (now, setup, direction, st["n"], st["won"], st["avg_r"], st["total_r"], st["pf"], st["half1"],
                   st["half2"], st["no_best"], st["max_dd"], st["status"], days, dumps({"days": st.get("days")})))
    db.set_meta("backtest_ts", now)
    db.set_meta("backtest_summary", dumps({"trades": result.get("trades"), "pairs": result.get("pairs"), "days": days}))


class Edge:
    """What the bot knows about each setup: its backtest grade and how its paper trades have gone since."""

    BENCH_N = 15
    BENCH_R = -0.15

    def __init__(self, db):
        self.db = db

    def backtest(self) -> dict[str, dict]:
        ts = int(num(self.db.get_meta("backtest_ts"), 0))
        rows = self.db.rows("select * from backtests where ts=?", (ts,)) if ts else []
        return {f"{r['setup']}_{r['direction']}": r for r in rows}

    def paper(self, setup: str, direction: str, since: int = 0) -> dict:
        rows = self.db.rows("""select r_mult from paper where setup=? and direction=? and status='closed'
                               and closed_ts>=?""", (setup, direction, since))
        rs = [num(r["r_mult"]) for r in rows]
        return {"n": len(rs), "avg_r": (sum(rs) / len(rs)) if rs else 0.0,
                "won": (sum(1 for r in rs if r > 0) / len(rs)) if rs else 0.0}

    def status(self, setup: str, direction: str) -> dict:
        bt = self.backtest().get(f"{setup}_{direction}")
        since = int(num(self.db.get_meta("backtest_ts"), 0)) - 30 * 86400
        paper = self.paper(setup, direction, max(since, 0))
        st = bt["status"] if bt else "untested"
        if paper["n"] >= self.BENCH_N and paper["avg_r"] <= self.BENCH_R:
            st = "benched"
        return {"status": st, "proven": st == "proven", "backtest": bt, "paper": paper}

    def table(self) -> list[dict]:
        from .setups import SETUP_NAMES
        out = []
        for setup in SETUP_NAMES:
            for direction in ("long", "short"):
                s = self.status(setup, direction)
                out.append({"setup": setup, "direction": direction, "label": label(setup, direction), **s})
        return out


def describe(edge_status: dict) -> str:
    """One line about a setup's record, for alerts."""
    bt = edge_status.get("backtest")
    paper = edge_status.get("paper") or {}
    parts = [STATUS_TEXT.get(edge_status.get("status"), edge_status.get("status", ""))]
    if bt and bt.get("n"):
        parts.append(f"backtest {int(bt['n'])} trades, {num(bt['avg_r']):+.2f}R avg, {num(bt['won']) * 100:.0f}% won")
    if paper.get("n"):
        parts.append(f"paper {paper['n']} trades {paper['avg_r']:+.2f}R")
    return " · ".join(parts)

