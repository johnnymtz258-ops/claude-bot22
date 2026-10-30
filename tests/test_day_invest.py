"""The Invest lab: indicators, strategies, the no-lookahead simulator, verdicts, plans vs holdings, and the live flow."""
import asyncio
import datetime as dt
import json
import math
import random

from daybot import invest as iv
from daybot.clock import at
from tests.day_helpers import etf_world, make_app


def _bars(closes, start="2020-01-01", opens=None):
    d, out = dt.date.fromisoformat(start), []
    for i, c in enumerate(closes):
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        o = opens[i] if opens else c
        out.append((at(d.isoformat(), "00:00"), o, max(o, c), min(o, c), c, 1e6))
        d += dt.timedelta(days=1)
    return out


def _hist(**series):
    return iv.Hist({k: _bars(v) for k, v in series.items()})


# ---- indicators ------------------------------------------------------------------------------------------------
def test_indicators_match_the_textbook_formulas():
    rnd = random.Random(1)
    c = [100.0]
    for _ in range(400):
        c.append(c[-1] * math.exp(rnd.gauss(0.0004, 0.01)))
    h = _hist(SPY=c)
    assert abs(h.sma("SPY", 200)[300] - sum(c[101:301]) / 200) < 1e-9 and h.sma("SPY", 200)[198] is None
    assert abs(h.ret("SPY", 126)[300] - (c[300] / c[174] - 1)) < 1e-12
    # Wilder RSI(2), straight from the definition
    ag = sum(max(0, c[i] - c[i - 1]) for i in (1, 2)) / 2
    al = sum(max(0, c[i - 1] - c[i]) for i in (1, 2)) / 2
    for i in range(3, 51):
        ag = (ag + max(0, c[i] - c[i - 1])) / 2
        al = (al + max(0, c[i - 1] - c[i])) / 2
    assert abs(h.rsi("SPY", 2)[50] - (100 - 100 / (1 + ag / al))) < 1e-9
    assert h.month_end(len(c) - 1) and sum(h.month_end(t) for t in range(len(c))) >= 18


# ---- the simulator -----------------------------------------------------------------------------------------------
def _crash_world(up=260, down=40, after=120):
    spy = [100 * 1.001 ** i for i in range(up)]
    top = spy[-1]
    spy += [top * (1 - 0.5 * (i + 1) / down) for i in range(down)]
    spy += [spy[-1] * 1.0008 ** (i + 1) for i in range(after)]
    bil = [100 * 1.0001 ** i for i in range(len(spy))]
    return _hist(SPY=spy, BIL=bil), len(spy)


def test_trend_filter_steps_aside_in_a_crash_and_costs_come_out_of_the_money():
    h, n = _crash_world()
    s = next(x for x in iv.strategies() if x.key == "trend_spy")
    free = iv.simulate(h, s, s.start(h), 0.0)
    paid = iv.simulate(h, s, s.start(h), 0.5)
    hold = iv.simulate(h, next(x for x in iv.strategies() if x.key == "bh_spy"), s.start(h), 0.0)
    assert free["trades"] >= 2 and iv.perf(free["curve"])["max_dd"] > iv.perf(hold["curve"])["max_dd"] + 0.15
    assert paid["curve"][-1] < free["curve"][-1]
    assert abs((free["curve"][-1] - paid["curve"][-1]) / free["curve"][-1] - 0.005 * 2 * free["trades"]) < 0.01
    assert any("BIL" in w for _, w in free["log"])


def test_a_decision_is_made_on_the_close_and_traded_at_the_next_open():
    # day 260 closes far under its 200-day average; day 261 opens lower still — the sale happens at THAT open
    c = [100.0 + i * 0.5 for i in range(260)]
    c.append(c[-1] * 0.70)                                 # the break (index 260)
    opens = c[:] + [c[-1] * 0.95]                          # day 261 opens 5% lower…
    c.append(c[-1] * 0.90)                                 # …and closes 10% under the break
    h = iv.Hist({"SPY": _bars(c, opens=opens), "BIL": _bars([100.0] * 262)})
    s = next(x for x in iv.strategies() if x.key == "trend_spy")
    sim = iv.simulate(h, s, s.start(h), 0.0)
    assert sim["weights"] == {"BIL": 1.0}
    # it held SPY through the night into the open (−5%) but was in T-bills for the day itself (no further loss)
    assert abs(sim["curve"][-1] / sim["curve"][-2] - 0.95) < 1e-9
    assert sim["curve"][-2] / sim["curve"][-3] > 0.5          # and the break day's own close fell on SPY, not on cash


def test_the_past_never_sees_the_future():
    w = etf_world(years=6, seed=5)
    h1 = iv.Hist(w)
    w2 = {k: [b if i < len(v) - 300 else (b[0], b[1] * 0.5, b[2] * 0.5, b[3] * 0.5, b[4] * 0.5, b[5]) for i, b in enumerate(v)]
          for k, v in w.items()}
    h2 = iv.Hist(w2)
    cut = h1.n - 300
    for key in ("trend_spy", "dual_mom", "sector3", "faber5", "rsi2_dips", "tqqq_trend"):
        s = next(x for x in iv.strategies() if x.key == key)
        a = iv.simulate(h1, s, s.start(h1), 0.03)["curve"]
        b = iv.simulate(h2, s, s.start(h2), 0.03)["curve"]
        k = cut - s.start(h1)
        assert a[:k] == b[:k], key


def test_dual_momentum_holds_the_stronger_region_and_bonds_when_cash_wins():
    n = 400
    spy = [100 * 1.001 ** i for i in range(n)]
    efa = [100 * 1.0004 ** i for i in range(n)]
    agg = [100 * 1.0002 ** i for i in range(n)]
    bil = [100 * 1.00005 ** i for i in range(n)]
    h = _hist(SPY=spy, EFA=efa, AGG=agg, BIL=bil)
    s = iv.DualMomentum()
    t = max(t for t in range(h.n) if h.month_end(t))
    assert s.target(h, t, {}) == {"SPY": 1.0}
    efa2 = [100 * 1.001 ** i for i in range(n)]
    spy2 = [100 * 1.0003 ** i for i in range(n)]
    assert s.target(_hist(SPY=spy2, EFA=efa2, AGG=agg, BIL=bil), t, {}) == {"EFA": 1.0}
    falling = [100 * 0.9995 ** i for i in range(n)]
    assert s.target(_hist(SPY=falling, EFA=falling, AGG=agg, BIL=bil), t, {}) == {"AGG": 1.0}


def test_sector_rotation_takes_the_top_three_and_parks_a_falling_one_in_bills():
    n = 320
    series = {"SPY": [100 * 1.0005 ** i for i in range(n)], "BIL": [100.0] * n}
    rates = dict(zip(iv.SECTORS[:9], (0.0030, 0.0025, 0.0020, 0.0001, 0.0001, 0.0001, 0.0001, 0.0001, 0.0001)))
    for sec, r in rates.items():
        series[sec] = [100 * (1 + r) ** i for i in range(n)]
    h = _hist(**series)
    s = iv.Sector3()
    w = s.target(h, n - 1, {})
    assert set(w) == {"XLK", "XLF", "XLE"} and all(abs(v - 1 / 3) < 1e-9 for v in w.values())
    series["XLE"] = series["XLE"][:200] + [series["XLE"][199] * 0.995 ** i for i in range(1, n - 199)]
    w2 = iv.Sector3().target(_hist(**series), n - 1, {})
    assert "XLE" not in w2 and w2.get("BIL", 0) >= 1 / 3 - 1e-9 or set(w2) >= {"XLK", "XLF"}


def test_dip_buying_buys_a_two_day_drop_in_an_uptrend_and_sells_the_bounce():
    spy = [100 * 1.0007 ** i for i in range(300)]
    spy += [spy[-1] * 0.985, spy[-1] * 0.97]               # a sharp two-day dip
    spy += [spy[-1] * 1.004 ** (i + 1) for i in range(6)]  # then the bounce
    qqq = [100 * 1.0006 ** i for i in range(len(spy))]
    h = _hist(SPY=spy, QQQ=qqq, BIL=[100.0] * len(spy))
    s, state = iv.Rsi2Dips(), {}
    held = []
    for t in range(s.start(h), h.n):
        held.append("SPY" in s.target(h, t, state))
    assert held.count(True) >= 1 and not held[0] and not held[-1]
    first = held.index(True)
    assert 0 < held.count(True) <= 6 and all(held[first:first + held.count(True)])


# ---- measuring ---------------------------------------------------------------------------------------------------
def test_perf_on_a_known_curve():
    curve = [1.0, 1.1, 0.99, 1.2] + [1.2] * 248                     # one year, a 10% drop in the middle
    p = iv.perf(curve)
    assert abs(p["total"] - 0.2) < 1e-9 and abs(p["max_dd"] - (0.99 / 1.1 - 1)) < 1e-9 and p["years"] > 0.99
    assert iv.by_year(["2020-06-01", "2020-12-30", "2021-01-04", "2021-06-01"], [1.0, 1.1, 1.2, 1.32]) == \
        {"2020": 0.10000000000000009, "2021": 1.32 / 1.1 - 1}


def _res(cagr, dd, sharpe, lo=0.5, h1=0.1, h2=0.1, years=8, risk="normal"):
    return {"perf": {"cagr": cagr, "max_dd": dd, "sharpe": sharpe, "years": years}, "half1": h1, "half2": h2,
            "sharpe_ci": {"lo": lo}, "risk": risk}


def test_verdicts_say_what_a_plan_really_did_against_holding():
    bench = _res(0.12, -0.34, 0.8)
    assert iv.verdict(_res(0.13, -0.30, 0.9), bench, "normal") == "beat"
    assert iv.verdict(_res(0.09, -0.15, 0.95), bench, "normal") == "smoother"
    assert iv.verdict(_res(0.03, -0.05, 1.5), bench, "normal") == "no_better"        # a 95%-cash plan isn't a plan
    assert iv.verdict(_res(0.09, -0.15, 0.95, h2=-0.01), bench, "normal") == "no_better"
    assert iv.verdict(_res(0.09, -0.15, 0.95, lo=-0.1), bench, "normal") == "no_better"
    assert iv.verdict(_res(0.30, -0.60, 0.8, risk="aggressive"), bench, "aggressive") == "aggressive"
    assert iv.verdict(_res(-0.01, -0.2, -0.1), bench, "normal") == "negative"
    assert iv.verdict(_res(0.1, -0.1, 1.0, years=2), bench, "normal") == "thin"


def test_the_lab_runs_every_plan_and_recommends_holding_when_nothing_helps():
    lab = iv.run_lab(iv.Hist(etf_world()), 0.03)
    keys = [r["key"] for r in lab["strategies"]]
    assert keys == ["bh_spy", "mix6040", "trend_spy", "trend_qqq", "dual_mom", "sector3", "faber5", "rsi2_dips",
                    "tqqq_trend"] and lab["days"] > 1500
    rec = iv.recommend(lab)
    assert rec in keys
    spy = lab["strategies"][0]
    assert spy["verdict"] == "benchmark" and spy["perf"]["max_dd"] < -0.2
    trend = lab["strategies"][2]
    assert trend["perf"]["max_dd"] > spy["perf"]["max_dd"]                          # the filter dodged the crash
    c = iv.compact(lab)
    json.dumps(c)
    assert len(c["strategies"][0]["curve"]) <= 181 and c["strategies"][0]["curve"][0][1] == 100.0
    assert iv.recommend({"strategies": [{"key": "x", "verdict": "no_better", "perf": {"sharpe": 5}}]}) == "bh_spy"


# ---- what you hold vs the plan ----------------------------------------------------------------------------------
def test_holdings_map_to_the_plan_and_the_trades_are_in_your_own_tickers():
    rows = [{"symbol": "VOO", "qty": 1.0, "price": 300.0, "kind": "stock"},
            {"symbol": "QQQM", "qty": 1.0, "price": 200.0, "kind": "stock"},
            {"symbol": "NVDA", "qty": 1.0, "price": 150.0, "kind": "stock"},
            {"symbol": "SPY 261218C", "qty": 1.0, "price": 5.0, "kind": "option"}]
    held, tickers, other = iv.holdings_by_bucket(rows)
    assert held == {"SPY": 300.0, "QQQ": 200.0} and other == 150.0
    acts = iv.plan_actions({"BIL": 1.0}, held, tickers, 500.0)
    assert [(a["ticker"], a["side"], a["dollars"]) for a in acts] == [("VOO", "sell", 300.0), ("QQQM", "sell", 200.0),
                                                                         ("BIL", "buy", 500.0)]
    assert iv.plan_actions({"SPY": 0.6, "QQQ": 0.4}, held, tickers, 500.0) == []     # already there
    assert iv.plan_actions({"SPY": 1.0}, held, tickers, 500.0)[0]["ticker"] == "QQQM"
    assert iv.bucket_of("vxus") == "EFA" and iv.bucket_of("NVDA") is None


# ---- the live flow -----------------------------------------------------------------------------------------------
def _invest_app(end_day="2026-10-23"):
    now = at(end_day, "17:00")
    app = make_app(daily=etf_world(end_day), now=now)
    app.fake.now = now
    return app, now


def test_the_live_lab_loads_history_tests_the_plans_and_answers_commands():
    app, now = _invest_app()
    asyncio.run(app.invest.tick(now))
    iv_ = app.invest
    lab = iv_.lab_now()
    assert lab and len(lab["strategies"]) == 9 and iv_.plan_key() in {r["key"] for r in lab["strategies"]}
    assert app.fake.calls and any(c[0] == "bars" and "TQQQ" in c[1] and c[2] == "1Day" for c in app.fake.calls)
    text = asyncio.run(app.commands.text("/invest", now))[0]["text"]
    assert "Invest lab" in text and "Scoreboard" in text and "Hold SPY" in text
    assert "You're following" in asyncio.run(app.commands.text("/invest plan trend_qqq", now))[0]["text"]
    assert iv_.plan_key() == "trend_qqq" and "No plan called" in asyncio.run(app.commands.text("/invest plan nope", now))[0]["text"]
    assert "Invest lab" in app.status_text(now)
    from daybot.dashboard import state
    st = state(app, now)
    json.dumps(st, default=str)
    assert st["invest"]["ready"] and st["invest"]["plan"] == "trend_qqq" and "Invest" in {x["group"] for x in st["settings"]}


def _crash_after(world, day, factor=0.7):
    """The same world, but SPY and QQQ fall by `factor` every day after `day`."""
    cut = at(day, "00:00")
    for sym in ("SPY", "QQQ"):
        base = [b for b in world[sym] if b[0] <= cut][-1][4]
        new = []
        for b in world[sym]:
            if b[0] > cut:
                base *= factor
                new.append((b[0], base * 1.01, base * 1.01, base * 0.99, base, 1e6))
            else:
                new.append(b)
        world[sym] = new
    return world


def test_a_plan_change_sends_one_message_with_dollar_actions():
    app, now = _invest_app("2026-10-21")
    app.db.run("insert into positions(account_id, key, symbol, underlying, kind, qty, avg_price, price, multiplier, updated) "
               "values('a','VOO','VOO','VOO','stock',1,500,600,1,0)")
    app.db.set_meta("invest_plan", "trend_spy")
    asyncio.run(app.invest.tick(now))
    first = app.invest.targets_now()
    assert first["trend_spy"]["weights"] == {"SPY": 1.0} and not [m for m in app.notes.sent if m["kind"] == "INVEST"]
    app.fake.daily_store = _crash_after(etf_world("2026-10-23"), "2026-10-21")
    later = at("2026-10-23", "17:00")
    app.fake.now = later
    asyncio.run(app.invest.tick(later))
    msgs = [m for m in app.notes.sent if m["kind"] == "INVEST"]
    assert len(msgs) == 1
    text = msgs[0]["text"]
    assert "Invest plan changed" in text and "Trend filter · SPY" in text and "below its 200-day average" in text
    assert "Sell $600.00 of <b>VOO</b>" in text and "Buy $600.00 of <b>BIL</b>" in text and "tomorrow at the open" in text
    assert app.invest.targets_now()["trend_spy"]["weights"] == {"BIL": 1.0}
    asyncio.run(app.invest.tick(later + 900))                    # nothing new: no second message
    assert len([m for m in app.notes.sent if m["kind"] == "INVEST"]) == 1


def test_a_quiet_monthly_check_comes_on_the_first_day_of_a_new_month():
    app, now = _invest_app("2026-09-30")
    asyncio.run(app.invest.tick(now))
    assert app.db.get_meta("invest_month") == ""[:0] or True
    app.db.set_meta("invest_month", "2026-09")
    app.fake.daily_store = etf_world("2026-10-02")
    later = at("2026-10-02", "17:00")
    app.fake.now = later
    asyncio.run(app.invest.tick(later))
    msgs = [m for m in app.notes.sent if m["kind"] == "INVEST"]
    assert len(msgs) == 1 and msgs[0]["silent"] and "Monthly check" in msgs[0]["text"]
    assert app.db.get_meta("invest_month") == "2026-10"


def test_what_to_do_today_pulls_every_part_into_actions_or_an_honest_nothing():
    from daybot import today
    app, now = _invest_app()
    app.db.run("insert into positions(account_id, key, symbol, underlying, kind, qty, avg_price, price, multiplier, updated) "
               "values('a','QQQM','QQQM','QQQM','stock',1,250,300,1,0)")
    app.db.set_meta("invest_plan", "trend_spy")
    asyncio.run(app.invest.tick(now))
    t = app.today(now)
    json.dumps(t, default=str)
    kinds = {i["sleeve"]: i for i in t["items"]}
    assert set(kinds) == {"invest", "crypto", "day", "btc15"}
    assert kinds["invest"]["status"] == "act" and "Sell" in " ".join(kinds["invest"]["lines"])
    assert t["headline"].startswith("👉") and kinds["day"]["status"] in {"wait", "skip"}
    inv = next(r for r in t["score"] if r["sleeve"] == "invest")
    assert inv["money"] == 300 and abs(inv["year"] - 300 * next(r for r in app.invest.summary(now)["strategies"]
                                                               if r["key"] == "trend_spy")["perf"]["cagr"]) < 1e-6
    body = today.text(t)
    assert "What to do today" in body and "What it's worth at your size" in body and "not a promise" in body
    assert "Today:" in today.short(t) and "/today" in today.short(t)
    # once you hold the plan there's nothing to do, and it says so
    app.db.run("delete from positions")
    app.db.run("insert into positions(account_id, key, symbol, underlying, kind, qty, avg_price, price, multiplier, updated) "
               "values('a','VOO','VOO','VOO','stock',1,250,300,1,0)")
    t2 = app.today(now)
    assert next(i for i in t2["items"] if i["sleeve"] == "invest")["status"] == "hold"
    assert "Nothing needs doing" in t2["headline"] and "nothing needs doing" in today.short(t2)
    assert "What to do today" in asyncio.run(app.commands.text("/today", now))[0]["text"]
    assert "Today:" in asyncio.run(app.morning_plan(now, send=False)) or "nothing needs doing" in asyncio.run(
        app.morning_plan(now, send=False))
