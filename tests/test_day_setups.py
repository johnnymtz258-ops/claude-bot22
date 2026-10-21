"""DayBot setups, the exit plan, the backtest's grading and the edge gate."""
import asyncio

from daybot.backtest import Edge, gapper_days, grade, replay_day, run, summarize
from daybot.bars import C, H, L, O, T, V, resample, rvol, volume_share, vwap
from daybot.clock import Market, at, day_of
from daybot.db import Database
from daybot.setups import Signal, simulate
from tests.day_helpers import (DAY, PREV, FakeAlpaca, chop_day, daily_history, five, flag_day, gap_day, orb_day,
                               vwap_day)

CFG = {"ORB_MINUTES": 15, "GAP_MIN_PCT": 3}


def trades_for(bars):
    return replay_day("TEST", DAY, five(bars), daily_history(PREV, 100.0, 2.0), Market(), CFG, 0.05)


def test_each_setup_fires_on_its_pattern_both_ways_and_wins():
    for builder, setup in ((orb_day, "orb"), (flag_day, "leg2"), (vwap_day, "vwap")):
        for up in (True, False):
            got = trades_for(builder(up=up))
            assert [(t["setup"], t["direction"]) for t in got] == [(setup, "long" if up else "short")], (setup, up, got)
            assert got[0]["r"] > 1.0 and got[0]["reason"] == "target"


def test_gap_and_go_fires_with_the_opening_range_breakout():
    got = trades_for(gap_day())
    assert {(t["setup"], t["direction"]) for t in got} == {("orb", "long"), ("pmh", "long")}
    down = trades_for(gap_day(up=False))
    assert {(t["setup"], t["direction"]) for t in down} == {("orb", "short"), ("pmh", "short")}


def test_a_choppy_day_fires_nothing():
    for seed in (1, 2, 3, 4):
        assert trades_for(chop_day(seed=seed)) == []


def test_mirrored_setups_give_mirrored_results():
    up = trades_for(flag_day(up=True))[0]
    down = trades_for(flag_day(up=False))[0]
    assert abs(up["r"] - down["r"]) < 0.1


def _bars(rows, start="10:00"):
    t0 = at(DAY, start)
    return [(t0 + i * 300, *r) for i, r in enumerate(rows)]


def test_simulate_assumes_the_stop_first_when_a_bar_hits_both():
    sig = Signal("orb", "long", "X", at(DAY, "10:00"), 100, 99, 101, 102, "")
    bars = _bars([(100, 100.2, 99.9, 100.1, 1), (100.1, 101.5, 98.5, 100, 1)])
    res = simulate(sig, bars, at(DAY, "16:00"), 0.0)
    assert res["reason"] == "stop" and abs(res["r"] + 1) < 1e-9


def test_simulate_half_at_1r_then_breakeven():
    sig = Signal("orb", "long", "X", at(DAY, "10:00"), 100, 99, 101, 102, "")
    bars = _bars([(100, 100.2, 99.9, 100.1, 1), (100.1, 101.2, 100.05, 101, 1), (101, 101.1, 99.8, 99.9, 1)])
    res = simulate(sig, bars, at(DAY, "16:00"), 0.0)
    assert res["half"] and res["reason"] == "breakeven" and abs(res["r"] - 0.5) < 1e-9


def test_simulate_full_target_and_time_exit_and_gap_through_stop():
    sig = Signal("orb", "short", "X", at(DAY, "10:00"), 100, 101, 99, 98, "")
    bars = _bars([(100, 100.1, 99.5, 99.6, 1), (99.6, 99.7, 97.9, 98, 1)])
    res = simulate(sig, bars, at(DAY, "16:00"), 0.0)
    assert res["reason"] == "target" and abs(res["r"] - 1.5) < 1e-9
    late = _bars([(100, 100.2, 99.9, 100.1, 1)] * 3, start="15:45")
    res = simulate(Signal("orb", "long", "X", at(DAY, "15:45"), 100, 99, 101, 102, ""), late, at(DAY, "16:00"), 0.0)
    assert res["reason"] == "time"
    gap = _bars([(98.5, 99, 98, 98.8, 1)])
    assert simulate(sig.__class__("orb", "long", "X", at(DAY, "10:00"), 100, 99, 101, 102, ""), gap,
                    at(DAY, "16:00"), 0.0) is None


def test_costs_lower_the_result():
    sig = Signal("orb", "long", "X", at(DAY, "10:00"), 100, 99, 101, 102, "")
    bars = _bars([(100, 100.2, 99.9, 100.1, 1), (100.1, 102.5, 100.05, 102.4, 1)])
    free = simulate(sig, bars, at(DAY, "16:00"), 0.0)["r"]
    paid = simulate(sig, bars, at(DAY, "16:00"), 0.1)["r"]
    assert paid < free


def test_grading_is_honest():
    win = [{"setup": "orb", "direction": "long", "day": f"2026-09-{d:02d}", "ts": d, "r": r}
           for d, r in zip(range(1, 61), [1.2, -1, 0.5] * 20)]
    assert summarize(win)["orb_long"]["status"] == "proven"
    one_lucky = [{**t, "r": -0.2} for t in win[:57]] + [{**t, "r": 9.0} for t in win[57:]]
    assert summarize(one_lucky)["orb_long"]["status"] != "proven"         # all of it from 3 trades
    first_half_only = [{**t, "r": 1.0 if i < 30 else -0.4} for i, t in enumerate(win)]
    assert summarize(first_half_only)["orb_long"]["status"] == "promising"  # stopped working
    assert grade({"n": 10, "avg_r": 2.0}) == "thin"
    assert summarize([{**t, "r": -0.3} for t in win])["orb_long"]["status"] == "negative"


def test_edge_benches_a_setup_that_loses_on_paper():
    db = Database(":memory:")
    db.insert("""insert into backtests(ts, setup, direction, n, won, avg_r, total_r, pf, half1, half2, no_best, max_dd,
                 status, days) values(100, 'orb', 'long', 80, .5, .2, 16, 1.4, .2, .2, .15, -4, 'proven', 60)""")
    db.set_meta("backtest_ts", 100)
    edge = Edge(db)
    assert edge.status("orb", "long")["proven"]
    for i in range(16):
        db.insert("insert into paper(setup, direction, status, closed_ts, r_mult) values('orb','long','closed',?,?)",
                  (200 + i, -0.5))
    st = edge.status("orb", "long")
    assert st["status"] == "benched" and not st["proven"]
    assert edge.status("vwap", "short")["status"] == "untested"


def test_gapper_days_are_rebuilt_from_daily_bars():
    rows = daily_history(PREV, 100.0, 2.0)
    big = (at("2026-10-19", "00:00"), rows[-2][C] * 1.08, rows[-2][C] * 1.1, rows[-2][C] * 1.05, rows[-2][C] * 1.09, 9e6)
    rows = rows[:-2] + [big] + rows[-1:]
    from daybot.config import Config
    got = gapper_days({"ABC": rows}, ["2026-10-19", "2026-10-20"], Config())
    assert got == [("ABC", "2026-10-19"), ("ABC", "2026-10-20")]     # gapped up 8%, then back down the next day
    assert gapper_days({"ABC": rows}, ["2026-10-16"], Config()) == []


def test_backtest_run_end_to_end_on_fake_history():
    days = Market().trading_days_back(DAY, 3)
    minute, daily = {}, {}
    for i, d in enumerate(days):
        minute.setdefault("TEST", []).extend(flag_day(day=d))
    daily["TEST"] = daily_history([x for x in PREV if x < days[0]], 100.0, 2.0) + \
        [(at(d, "00:00"), 100, 104, 99.5, 104.5, 5e6) for d in days]
    fake = FakeAlpaca(minute, daily, now=at(DAY, "08:00"))
    from daybot.config import Config
    res = asyncio.run(run(fake, Market(), Config(), DAY, days=3, core=["TEST"], universe=["TEST"]))
    assert res["trades"] >= 3 and "leg2_long" in res["stats"]


def test_bars_helpers():
    bars = flag_day()
    b5 = resample(bars, 5)
    assert all(b[T] % 300 == 0 for b in b5)
    assert abs(sum(b[V] for b in bars) - sum(b[V] for b in b5)) < 1e-6
    assert b5[0][O] == bars[0][O] and b5[-1][C] == bars[-1][C] and max(b[H] for b in bars) == max(b[H] for b in b5)
    assert min(b[L] for b in bars) == min(b[L] for b in b5)
    rth = [b for b in b5 if b[T] >= at(DAY, "09:30")]
    vw = vwap(rth)
    assert min(b[L] for b in rth) <= vw[-1] <= max(b[H] for b in rth)
    assert volume_share(0) == 0 and volume_share(390) == 1 and 0.25 < volume_share(60) < 0.3
    assert abs(rvol(270_000, 60, 1_000_000) - 1.0) < 0.01


def test_market_calendar_fallback():
    m = Market()
    assert m.is_trading_day("2026-10-21") and not m.is_trading_day("2026-10-24")      # Saturday
    assert not m.is_trading_day("2026-11-26")                                           # Thanksgiving
    assert m.hours("2026-11-27") == ("09:30", "13:00")                                  # early close
    assert m.session(at(DAY, "08:00")) == "pre" and m.session(at(DAY, "10:00")) == "open"
    assert m.session(at(DAY, "17:00")) == "post" and m.session(at("2026-10-24", "10:00")) == "closed"
    assert m.prev_day("2026-10-26") == "2026-10-23" and m.next_day("2026-10-23") == "2026-10-26"
    m.load_calendar([{"date": "2026-10-21", "open": "09:30", "close": "16:00"}])
    assert day_of(m.open_ts(DAY)) == DAY
