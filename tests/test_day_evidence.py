"""The evidence rules: a setup must beat luck, counted by whole days, to be called promising or proven."""
import random

from daybot.backtest import NO_EDGE, _stats, grade
from daybot.stats import clustered_mean_ci, sharpe_ci


def _trades(per_day, mean, sd, days=60, seed=1, pct_scale=0.3):
    rnd = random.Random(seed)
    out = []
    for d in range(days):
        for k in range(per_day):
            r = rnd.gauss(mean, sd)
            out.append({"setup": "x", "direction": "long", "symbol": "A", "day": "2026-01-%02d" % (d % 28 + 1) + f"-{d}",
                        "ts": d * 1000 + k, "r": r, "pct": r * pct_scale})
    return out


def test_a_real_edge_has_a_confidence_range_above_zero():
    ci = clustered_mean_ci([(t["day"], t["r"]) for t in _trades(8, 0.25, 1.0)])
    assert ci["lo"] > 0 and ci["p_pos"] > 0.95 and ci["lo"] < ci["mean"] < ci["hi"]


def test_noise_is_not_called_an_edge():
    wrong = 0
    for seed in range(60):
        ci = clustered_mean_ci([(t["day"], t["r"]) for t in _trades(8, 0.0, 1.0, seed=seed)], boot=300)
        wrong += ci["lo"] > 0
    assert wrong <= 12                                   # a 10% one-sided tail: about 6 of 60 by chance


def test_trades_on_the_same_day_count_as_one_piece_of_evidence():
    # 400 trades that all win or lose together by day: 20 independent days, so the range must be wide
    rows = []
    rnd = random.Random(3)
    for d in range(20):
        shock = rnd.gauss(0.05, 1.0)
        rows += [(f"d{d}", shock + rnd.gauss(0, 0.05)) for _ in range(20)]
    wide = clustered_mean_ci(rows)
    naive_half_width = 1.28 * (sum((v - sum(x for _, x in rows) / 400) ** 2 for _, v in rows) / 400) ** 0.5 / 20
    assert wide["hi"] - wide["lo"] > 3 * naive_half_width


def test_grade_wants_a_real_average_not_a_lucky_one():
    base = {"n": 500, "half1": 0.1, "half2": 0.1, "no_best": 0.05}
    assert grade({**base, "avg_r": 0.12, "ci_lo": 0.03, "p_pos": 0.97}) == "proven"
    assert grade({**base, "avg_r": 0.12, "ci_lo": -0.02, "p_pos": 0.9}) == "promising"
    assert grade({**base, "avg_r": 0.02, "ci_lo": -0.05, "p_pos": 0.65}) == "flat"      # +0.02R is zero to the eye
    assert grade({**base, "avg_r": -0.04, "ci_lo": -0.1, "p_pos": 0.1}) == "negative"
    assert grade({**base, "n": 10, "avg_r": 0.5}) == "thin"
    lottery = {**base, "avg_r": 0.14, "no_best": -0.05, "ci_lo": 0.01, "p_pos": 0.9}     # lives on a few big winners
    assert grade(lottery) == "promising"
    assert {"flat", "negative", "benched"} <= NO_EDGE and "promising" not in NO_EDGE


def test_stats_report_what_a_trade_earns_in_percent_of_the_money():
    st = _stats(_trades(8, 0.2, 1.0, days=80, seed=4))
    assert abs(st["avg_pct"] - 0.3 * st["avg_r"]) < 1e-9
    assert st["pct_lo"] < st["avg_pct"] < st["pct_hi"] and st["list"][0][3] == round(st["list"][0][2] * 0.3, 3)
    assert st["ci_lo"] < st["avg_r"] < st["ci_hi"]


def test_sharpe_ci_brackets_the_estimate():
    rnd = random.Random(2)
    xs = [rnd.gauss(0.001, 0.01) for _ in range(750)]
    s = sharpe_ci(xs)
    assert s["lo"] < s["sharpe"] < s["hi"]


def _auto_rows(app, rs, start_day="2026-10-01"):
    import datetime as dt
    from daybot.clock import at
    app.autopilot._begin(at(start_day, "09:00"))
    for i, r in enumerate(rs):
        day = (dt.date.fromisoformat(start_day) + dt.timedelta(days=i // 3)).isoformat()
        ts = at(day, "10:00") + i
        app.db.run("""insert into paper(day, symbol, setup, direction, opened_ts, entry, stop, t1, t2, qty, status, auto,
                      closed_ts, exit_price, r_mult, pnl, reason, plan, entry_type, risk, stop_now)
                      values(?,?,?,?,?,?,?,?,?,?,'closed',1,?,?,?,?,'time','close','stop',1,100)""",
                   (day, f"S{i}", "orb5", "long", ts, 100, 99, 101, 102, 1, ts + 3600, 100 + r, r, r))


def test_the_autopilot_verdict_waits_for_enough_trades_and_then_separates_skill_from_luck():
    from tests.day_helpers import make_app
    from daybot.clock import at
    now = at("2026-11-20", "16:00")
    rnd = random.Random(8)
    app = make_app(now=now)
    _auto_rows(app, [rnd.gauss(0.5, 1) for _ in range(20)])
    assert "Too early to judge: 20 trades" in app.autopilot.summary(now)["verdict"]
    strong = make_app(now=now)
    _auto_rows(strong, [rnd.gauss(0.6, 1.0) for _ in range(90)])
    v = strong.autopilot.summary(now)["verdict"]
    assert v.startswith("✅") and "80% range" in v
    losing = make_app(now=now)
    _auto_rows(losing, [rnd.gauss(-0.6, 1.0) for _ in range(90)])
    assert losing.autopilot.summary(now)["verdict"].startswith("🔴") and "more than bad luck" in losing.autopilot.summary(now)["verdict"]
    noisy = make_app(now=now)
    _auto_rows(noisy, [rnd.gauss(0.02, 1.5) for _ in range(70)])
    assert noisy.autopilot.summary(now)["verdict"].startswith("🟡") and "luck" in noisy.autopilot.summary(now)["verdict"]


def test_an_old_backtest_is_regraded_by_todays_rules_the_moment_it_is_read():
    import random as _r
    from daybot.backtest import Edge
    from daybot.db import Database
    from daybot.util import dumps
    db = Database(":memory:")
    rnd = _r.Random(2)
    trades = [[f"2026-06-{1 + i % 28:02d}", "X", round(rnd.gauss(0.019, 1.0), 3)] for i in range(1400)]     # +0.02R: noise
    rs = [t[2] for t in trades]
    # saved by the old code: no confidence numbers, graded 'promising' because the average was positive
    db.insert("""insert into backtests(ts, setup, direction, n, won, avg_r, total_r, pf, half1, half2, no_best, max_dd,
                 status, days, detail) values(100,'orb','long',1400,0.5,?,?,1.0,0.03,0.0,0.0,-5,'promising',120,?)""",
              (sum(rs) / len(rs), sum(rs), dumps({"days": 28, "trades": trades})))
    db.set_meta("backtest_ts", 100)
    edge = Edge(db)
    st = edge.status("orb", "long")
    assert st["status"] == "flat" and st["backtest"]["p_pos"] != 0.5            # no longer 'promising'
    assert edge.backtest() is edge.backtest()                                    # cached: the stored trades are read once
    real = [[f"2026-06-{1 + i % 28:02d}", "Y", round(rnd.gauss(0.5, 1.0), 3)] for i in range(600)]
    db.insert("""insert into backtests(ts, setup, direction, n, won, avg_r, total_r, pf, half1, half2, no_best, max_dd,
                 status, days, detail) values(100,'vwap','long',600,0.6,0.5,300,2.0,0.5,0.5,0.4,-5,'promising',120,?)""",
              (dumps({"days": 28, "trades": real}),))
    edge._cache = None
    assert edge.status("vwap", "long")["status"] == "proven"                      # a real edge keeps its verdict


def _save_stats(app, setup, direction, **st):
    from daybot.util import dumps
    row = {"n": 900, "won": 0.14, "avg_r": 0.138, "total_r": 124.0, "pf": 1.1, "half1": 0.1, "half2": 0.17, "no_best": -0.05,
           "max_dd": -20, "status": "flat", "days": 120, "ci_lo": -0.09, "ci_hi": 0.37, "p_pos": 0.78, **st}
    app.db.insert("""insert into backtests(ts, setup, direction, n, won, avg_r, total_r, pf, half1, half2, no_best, max_dd,
                     status, days, detail, ci_lo, ci_hi, p_pos) values(500,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (setup, direction, row["n"], row["won"], row["avg_r"], row["total_r"], row["pf"], row["half1"], row["half2"],
                   row["no_best"], row["max_dd"], row["status"], row["days"], dumps({"days": 120, "trades": []}),
                   row["ci_lo"], row["ci_hi"], row["p_pos"]))
    app.db.set_meta("backtest_ts", 500)


def test_a_probable_but_unproven_setup_is_paper_traded_by_the_autopilot_and_never_alerted():
    import asyncio
    from daybot.clock import at
    from daybot.setups import Signal
    from tests.day_helpers import DAY, make_app
    app = make_app(now=at(DAY, "10:30"))
    _save_stats(app, "orb5", "long")                                   # +0.14R, 78% sure: flat, but a candidate
    _save_stats(app, "orb", "long", avg_r=0.019, p_pos=0.61, n=1400)   # +0.02R: just flat
    _save_stats(app, "vwap", "long", avg_r=-0.1, p_pos=0.0, status="negative")
    assert app.edge.is_candidate("orb5", "long") and not app.edge.is_candidate("orb", "long")
    assert not app.edge.is_candidate("vwap", "long")
    now = at(DAY, "10:30")
    for setup, sym in (("orb5", "AAA"), ("orb", "BBB"), ("vwap", "CCC")):
        asyncio.run(app.scanner.alert(Signal(setup, "long", sym, int(now), 100.0, 99.0, 101.0, 102.0, "x"), now))
    rows = {r["symbol"]: r for r in app.db.rows("select * from paper")}
    assert rows["AAA"]["auto"] == 1 and rows["BBB"]["auto"] == 0 and rows["CCC"]["auto"] == 0
    assert not [m for m in app.notes.sent if m["kind"] == "SETUP"]       # a candidate never pings you
    sm = app.autopilot.summary(now)
    assert [(f["kind"], f["label"]) for f in sm["following"]] == [("candidate", "In-play 5-min breakout")]
    assert "candidate: +0.14R, 78% sure it's real — paper only" in app.autopilot.text(now)
    item = next(i for i in app.today(now)["items"] if i["sleeve"] == "day")
    assert item["status"] == "skip" and "paper-trading the best candidate" in " ".join(item["lines"])
