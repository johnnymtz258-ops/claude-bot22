"""Backtest: replay every setup over the last N trading days and grade it honestly.

What gets replayed:
  • the core liquid names every day (SPY, QQQ, mega caps, the usual day-trading names), and
  • every stock in the wider universe on the days it gapped GAP_MIN_PCT+ — rebuilt from daily bars, so the test
    isn't limited to whatever happens to be hot today.
Each signal is traded with the standard plan on the bars that follow it (entry at the next bar's open, costs on
every fill, flat by 3:55pm, stop assumed first when a bar touches both).

  • the stocks in play each day: the ORB5_TOP stocks with the most first-5-minute volume against their own usual
    first 5 minutes (price, volume and daily-range filters as in Zarattini, Barbon & Aziz 2024), replayed on
    1-minute bars, and
  • SPY / QQQ every day for the noise-band strategy (the band is rebuilt from the 14 days before each day), and
  • the end-of-day bounce: each day at 3:30pm, the universe's biggest losers (and winners, for the short side)
    since yesterday's close, held to the close.

A setup is PROVEN only when it made money after costs in BOTH halves of the period, still does without its 3
best trades, has 40+ trades and its average is clearly above zero even when whole days are resampled (so a lucky
week can't carry it). PROMISING means a positive average that is probably real (85% sure it isn't luck); FLAT means
no measurable edge either way. Only proven and promising setups reach you and the autopilot; the rest are
paper-traded (silently) and can earn their way in.
"""
from __future__ import annotations

import statistics
from pathlib import Path

from .bars import C, O, T, V, atr, split_day, vwap
from .clock import Market, at, day_of
from .universe import LEVERED, all_stocks, cost_pct, dollar_volume, liquid
from .setups import (BAND_EXIT_LAST, BAND_STEP, EOD_START, SETUP_NAMES, Day, detect, eod_rank, eod_signal,
                     label, simulate)
from .stats import clustered_mean_ci, series_stats
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

BAND_SYMBOLS = ("SPY", "QQQ")
INPLAY_LOOKBACK = 14            # days of first-5-minute volume an in-play stock is compared with
INPLAY_MIN_RVOL = 1.0           # at least its usual first 5 minutes
INPLAY_MIN_ATR = 0.50           # $ — a daily range big enough to trade
SETUPS_VERSION = "5"            # bump when setups change: the backtest runs again on its own (4: whole market, real costs)

MIN_TRADES_PROVEN = 40
STATUS_TEXT = {"proven": "✅ proven", "promising": "🟡 promising", "thin": "⚪ not enough trades",
               "flat": "⚪ no measurable edge", "negative": "🔴 loses money",
               "benched": "⏸ benched (losing in paper)", "untested": "⚪ untested"}
NO_EDGE = frozenset({"negative", "flat", "benched"})     # paper only: never alerted, never followed by the autopilot
MIN_AVG_PROMISING = 0.05                                  # R a trade, after costs
SURE_PROMISING = 0.85                                     # how often resampled days must still be positive


def make_day(symbol: str, day: str, bars5: list, daily_prior: list, market: Market, in_play: int = 0,
             rvol5: float = 0.0, band: dict | None = None) -> tuple[Day | None, list]:
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
    d = Day(symbol, rth, vwap(rth), market.open_ts(day), market.close_ts(day), atr_d, avg_vol, pdc, pmh, gap, 1, pml,
            in_play, rvol5, band)
    return d, rth


def replay_day(symbol: str, day: str, bars5: list, daily_prior: list, market: Market, cfg: dict,
               slippage_pct: float, *, in_play: int = 0, rvol5: float = 0.0, band: dict | None = None,
               bars1: list | None = None) -> list[dict]:
    """Every setup on one stock-day. Stop entries held to the close (in-play breakouts) are traded on 1-minute bars
    when they're given: 5-minute bars can't tell whether the stop or the entry came first."""
    full, rth = make_day(symbol, day, bars5, daily_prior, market, in_play, rvol5, band)
    if not full:
        return []
    fired: set = set()
    trades = []
    close_ts = market.close_ts(day)
    rth1 = split_day(bars1, day, market)[1] if bars1 else []
    for i in range(len(rth)):
        for sig in detect(full.upto(i), i, cfg, fired):
            if sig.entry_type == "stop" and rth1:
                res = simulate(sig, rth1, close_ts, slippage_pct, bar_seconds=60)
            else:
                res = simulate(sig, rth, close_ts, slippage_pct)
            if res and res["status"] == "closed":
                trades.append({"setup": sig.setup, "direction": sig.direction, "symbol": symbol, "day": day,
                               "ts": sig.ts, "r": res["r"], "reason": res["reason"], "entry": res["entry"],
                               "exit": res["exit"], "entry_ts": res["entry_ts"], "pct": _pct(res)})
    return trades


def _pct(res: dict) -> float:
    """What the trade made, in % of the money put in (after costs): R × the stop distance as a share of the price."""
    return round(res["r"] * res["risk"] / res["entry"] * 100, 4) if res.get("entry") else 0.0


def band_moves(bars5: list, day: str, market: Market) -> dict[int, float]:
    """How far the day had moved from its open at each half-hour mark (fraction), from 5-minute bars."""
    _, rth, _ = split_day(bars5, day, market)
    if not rth:
        return {}
    o, open_ts = rth[0][O], market.open_ts(day)
    out = {}
    for b in rth:
        minutes = int(round((b[T] + 300 - open_ts) / 60))
        if minutes % BAND_STEP == 0 and minutes <= BAND_EXIT_LAST and o:
            out[minutes] = abs(b[C] / o - 1)
    return out


def band_table(history: list[dict[int, float]], lookback: int = INPLAY_LOOKBACK) -> dict[int, float]:
    """The usual move from the open at each half-hour mark: the average of the last `lookback` days."""
    days = [h for h in history if h][-lookback:]
    if len(days) < max(5, lookback // 2):
        return {}
    keys = sorted({k for h in days for k in h})
    return {k: sum(h[k] for h in days if k in h) / max(1, sum(1 for h in days if k in h)) for k in keys}


def eod_moves(bars30: dict[str, list], daily_prior: dict[str, list], day: str, t0: int, cfg) -> dict:
    """{symbol: (return since yesterday's close at 3:30pm, the 3:30 bar, daily ATR)} for liquid, non-leveraged stocks."""
    out = {}
    for sym, rows in bars30.items():
        if sym in LEVERED or sym in BAND_SYMBOLS:
            continue
        row = next((b for b in rows if b[T] == t0), None)
        prior = [d for d in daily_prior.get(sym, []) if day_of(d[T]) < day]
        if not row or len(prior) < 15 or prior[-1][C] <= 0:
            continue
        if prior[-1][C] < cfg.get("MIN_PRICE") or statistics.mean(d[V] for d in prior[-14:]) < cfg.get("MIN_AVG_VOLUME"):
            continue
        out[sym] = (row[O] / prior[-1][C] - 1, row, atr(prior[-15:], 14))
    return out


def rank_in_play(first5: dict[str, tuple], history: dict[str, list[float]], daily_prior: dict[str, list], day: str,
                 cfg, top: int) -> list[tuple[str, int, float]]:
    """Today's stocks in play: [(symbol, rank, rvol5)] — first-5-minute volume against its own usual first 5 minutes,
    on stocks over MIN_PRICE with MIN_AVG_VOLUME shares a day and a daily range of $0.50+, best first."""
    scored = []
    for sym, bar in first5.items():
        if sym in LEVERED or sym in BAND_SYMBOLS:
            continue
        past = [v for v in history.get(sym, [])[-INPLAY_LOOKBACK:] if v > 0]
        prior = [d for d in daily_prior.get(sym, []) if day_of(d[T]) < day]
        if len(past) < 5 or len(prior) < 15:
            continue
        avg_vol = statistics.mean(d[V] for d in prior[-14:])
        if prior[-1][C] < cfg.get("MIN_PRICE") or avg_vol < cfg.get("MIN_AVG_VOLUME") or atr(prior[-15:], 14) < INPLAY_MIN_ATR:
            continue
        rv = bar[V] / statistics.mean(past)
        if rv >= INPLAY_MIN_RVOL:
            scored.append((rv, sym))
    scored.sort(reverse=True)
    return [(sym, n + 1, rv) for n, (rv, sym) in enumerate(scored[:top])]


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
    ci = clustered_mean_ci([(t["day"], t["r"]) for t in trades])
    pcts = [num(t.get("pct")) for t in trades]
    pci = clustered_mean_ci([(t["day"], num(t.get("pct"))) for t in trades], seed=12)
    ps = series_stats(pcts)
    return {"n": n, "won": len(wins) / n, "avg_r": sum(rs) / n, "total_r": sum(rs),
            "list": [[t["day"], t.get("symbol", ""), round(t["r"], 3), round(num(t.get("pct")), 3)] for t in trades],
            "pf": (sum(wins) / abs(sum(losses))) if losses and sum(losses) else (99.0 if wins else 0.0),
            "half1": sum(first) / len(first), "half2": sum(second) / len(second),
            "no_best": (sum(rest) / len(rest)) if rest else 0.0, "max_dd": dd,
            "days": len({t["day"] for t in trades}), "ci_lo": ci["lo"], "ci_hi": ci["hi"], "p_pos": ci["p_pos"],
            "avg_pct": ps["mean"], "med_pct": ps["median"], "pct_lo": pci["lo"], "pct_hi": pci["hi"]}


def grade(st: dict) -> str:
    """thin < 15 trades · negative (average at or below zero) · proven · promising (a positive average that's
    probably real) · flat (positive but indistinguishable from zero)."""
    if not st.get("n") or st["n"] < 15:
        return "thin"
    if st["avg_r"] <= 0:
        return "negative"
    if (st["n"] >= MIN_TRADES_PROVEN and st["half1"] >= 0.05 and st["half2"] >= 0.05 and st["no_best"] > 0
            and st["avg_r"] >= 0.08 and num(st.get("ci_lo")) > 0):
        return "proven"
    if st["avg_r"] >= MIN_AVG_PROMISING and num(st.get("p_pos")) >= SURE_PROMISING:
        return "promising"
    return "flat"


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
        if sym in LEVERED:
            continue
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
              core: list[str] | None = None, universe: list[str] | None = None, cache: Path | None = None) -> dict:
    """Fetch history and replay. Returns {"stats": {...}, "trades": n, "pairs": n, "days": [...]}.

    With no `universe`, gappers and stocks in play are looked for across the whole market (every listed stock that
    trades enough), like the research does; the end-of-day bounce stays on the ~250 big names, where it's cheap to
    trade. Every trade pays the cost of its stock (spread + slippage by how much money trades in it)."""
    days = int(days or cfg.get("BACKTEST_DAYS"))
    ext = market.trading_days_back(today, days + INPLAY_LOOKBACK)
    day_list = ext[INPLAY_LOOKBACK:]
    core = core or CORE
    first_ts = at(day_list[0], "00:00")
    start, end = first_ts - 45 * 86400, at(day_list[-1], "20:00")
    if universe is None:
        names = await all_stocks(alpaca, cache)
        if progress:
            await progress(f"reading {len(names):,} stocks")
        daily = await liquid(alpaca, sorted(set(names) | set(UNIVERSE)), start, end, cfg)
        missing = [x for x in set(core) | set(UNIVERSE) | set(BAND_SYMBOLS) if x not in daily]
        if missing:
            daily.update(await alpaca.bars(missing, "1Day", start, end, feed="sip"))
        scan, eod_names = sorted(x for x in daily if x not in LEVERED), list(UNIVERSE)
    else:
        daily = await alpaca.bars(universe, "1Day", start, end, feed="sip")
        scan, eod_names = list(universe), list(universe)
    extra = cfg.get("SLIPPAGE_PCT")

    def cost(sym: str, day: str) -> float:
        return cost_pct(dollar_volume(daily.get(sym) or [], at(day, "00:00")), extra)

    pairs = {(x, d) for x in core for d in day_list}
    pairs |= set(gapper_days(daily, day_list, cfg))

    # stocks in play: everyone's first 5 minutes, each day, against their own usual first 5 minutes
    top = int(cfg.get("ORB5_TOP"))
    in_play: dict[tuple[str, str], tuple[int, float]] = {}
    first5_hist: dict[str, list[float]] = {}
    for n, day in enumerate(ext):
        open_ts = market.open_ts(day)
        got = await alpaca.bars(scan, "5Min", open_ts, open_ts + 300, feed="sip")
        first5 = {x: rows[0] for x, rows in got.items() if rows and rows[0][T] == open_ts}
        if day in day_list and top > 0:
            for x, rank, rv in rank_in_play(first5, first5_hist, daily, day, cfg, top):
                in_play[(x, day)] = (rank, rv)
        for x, bar in first5.items():
            first5_hist.setdefault(x, []).append(bar[V])
        if progress and n % 5 == 0:
            await progress(f"finding stocks in play {n + 1}/{len(ext)} days ({len(scan):,} stocks)")
    pairs |= set(in_play)

    # SPY / QQQ noise bands, each day from the 14 before it
    bands: dict[tuple[str, str], dict] = {}
    band_syms = [x for x in BAND_SYMBOLS if x in daily or x in core]
    if band_syms:
        got = await alpaca.bars(band_syms, "5Min", at(ext[0], "04:00"), end, feed="sip")
        for x in band_syms:
            history = []
            for day in ext:
                if day in day_list:
                    table = band_table(history)
                    if table:
                        bands[(x, day)] = table
                history.append(band_moves([b for b in got.get(x) or [] if day_of(b[T]) == day], day, market))
                if day in day_list:
                    pairs.add((x, day))

    # the end-of-day bounce: 3:30pm to the close, the day's biggest losers (and winners)
    trades: list[dict] = []
    eod_universe = [x for x in eod_names if x not in LEVERED]
    for n, day in enumerate(day_list):
        close = market.close_ts(day)
        t0 = close - EOD_START
        got = await alpaca.bars(eod_universe, "30Min", t0, close, feed="sip")
        moves = eod_moves(got, daily, day, t0, cfg)
        losers, winners = eod_rank({k: v[0] for k, v in moves.items()})
        for direction, names_ in (("long", losers), ("short", winners)):
            for rank, x in enumerate(names_, 1):
                move, row, daily_atr = moves[x]
                sig = eod_signal(x, direction, row[O], t0, close, daily_atr, move, rank)
                res = simulate(sig, [row], close, cost(x, day), bar_seconds=1800) if sig else None
                if res and res["status"] == "closed":
                    trades.append({"setup": "eod", "direction": direction, "symbol": x, "day": day, "ts": t0,
                                   "r": res["r"], "reason": res["reason"], "entry": res["entry"], "exit": res["exit"],
                                   "entry_ts": res["entry_ts"], "pct": _pct(res)})
        if progress and n % 20 == 0:
            await progress(f"end-of-day test {n + 1}/{len(day_list)}")

    by_day: dict[str, list[str]] = {}
    for x, day in pairs:
        by_day.setdefault(day, []).append(x)
    values = {name: cfg.get(name) for name in ("ORB_MINUTES", "GAP_MIN_PCT")}
    for n, day in enumerate(sorted(by_day)):
        syms = sorted(by_day[day])
        bars = await alpaca.bars(syms, "5Min", at(day, "04:00"), at(day, "16:00"), feed="sip")
        hot = sorted(x for x in syms if (x, day) in in_play)
        bars1 = await alpaca.bars(hot, "1Min", market.open_ts(day), market.close_ts(day), feed="sip") if hot else {}
        for x in syms:
            rank, rv = in_play.get((x, day), (0, 0.0))
            trades.extend(replay_day(x, day, bars.get(x) or [], daily.get(x) or [], market, values, cost(x, day),
                                     in_play=rank, rvol5=rv, band=bands.get((x, day)), bars1=bars1.get(x)))
        if progress:
            await progress(f"{n + 1}/{len(by_day)} days")
    stats = summarize(trades)
    return {"stats": stats, "trades": len(trades), "pairs": len(pairs), "days": day_list, "list": trades,
            "in_play": len(in_play), "scanned": len(scan)}


def save(db, result: dict, now: int) -> None:
    days = len(result.get("days") or [])
    for key, st in result["stats"].items():
        setup, direction = key.split("_", 1)
        db.insert("""insert into backtests(ts, setup, direction, n, won, avg_r, total_r, pf, half1, half2, no_best,
                     max_dd, status, days, detail, ci_lo, ci_hi, p_pos, avg_pct, med_pct, pct_lo, pct_hi)
                     values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (now, setup, direction, st["n"], st["won"], st["avg_r"], st["total_r"], st["pf"], st["half1"],
                   st["half2"], st["no_best"], st["max_dd"], st["status"], days,
                   dumps({"days": st.get("days"), "trades": st.get("list") or []}), st.get("ci_lo", 0.0),
                   st.get("ci_hi", 0.0), st.get("p_pos", 0.5), st.get("avg_pct", 0.0), st.get("med_pct", 0.0),
                   st.get("pct_lo", 0.0), st.get("pct_hi", 0.0)))
    db.run("delete from backtests where ts<?", (now - 120 * 86400,))         # keep a few months of history
    db.set_meta("backtest_ts", now)
    db.set_meta("backtest_setups", f"{SETUPS_VERSION}:{','.join(SETUP_NAMES)}")
    db.set_meta("backtest_summary", dumps({"trades": result.get("trades"), "pairs": result.get("pairs"), "days": days,
                                           "scanned": result.get("scanned"), "in_play": result.get("in_play")}))


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
                               and closed_ts>=? order by closed_ts""", (setup, direction, since))
        rs = [num(r["r_mult"]) for r in rows]
        half = len(rs) // 2
        avg = (lambda xs: sum(xs) / len(xs) if xs else 0.0)
        return {"n": len(rs), "avg_r": avg(rs), "won": (sum(1 for r in rs if r > 0) / len(rs)) if rs else 0.0,
                "half1": avg(rs[:half]), "half2": avg(rs[half:]),
                "no_best": avg(sorted(rs)[:-3]) if len(rs) > 3 else 0.0}

    def status(self, setup: str, direction: str) -> dict:
        bt = self.backtest().get(f"{setup}_{direction}")
        since = int(num(self.db.get_meta("backtest_ts"), 0)) - 30 * 86400
        paper = self.paper(setup, direction, max(since, 0))
        st = bt["status"] if bt else "untested"
        live = False
        if paper["n"] >= self.BENCH_N and paper["avg_r"] <= self.BENCH_R:
            st = "benched"
        elif st != "proven" and self.proven_live(paper):
            st, live = "proven", True              # it earned it live, the same bar the backtest has to clear
        return {"status": st, "proven": st == "proven", "backtest": bt, "paper": paper, "live": live}

    LIVE_N = 30
    LIVE_R = 0.12

    def proven_live(self, paper: dict) -> bool:
        """Winning in live paper trading: 30+ trades, +0.12R or better on average, positive in both halves and
        still positive without its 3 best trades."""
        return (paper["n"] >= self.LIVE_N and paper["avg_r"] >= self.LIVE_R and paper["half1"] > 0
                and paper["half2"] > 0 and paper["no_best"] > 0)

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
    parts = ["✅ proven live" if edge_status.get("live") else
             STATUS_TEXT.get(edge_status.get("status"), edge_status.get("status", ""))]
    if bt and bt.get("n"):
        parts.append(f"backtest {int(bt['n'])} trades, {num(bt['avg_r']):+.2f}R avg, {num(bt['won']) * 100:.0f}% won"
                     + (f", {num(bt['p_pos']) * 100:.0f}% sure it's real" if bt.get("p_pos") not in (None, 0.5) else ""))
    if paper.get("n"):
        parts.append(f"paper {paper['n']} trades {paper['avg_r']:+.2f}R")
    return " · ".join(parts)

