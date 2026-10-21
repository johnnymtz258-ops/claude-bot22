"""Your trades: fills -> round trips, the trade check (stocks and options), recaps, the monitor and the coach."""
import asyncio

from daybot.check import GREEN, RED, YELLOW, format_check
from daybot.clock import at
from daybot.coach import format_leaks, leaks
from daybot.commands import parse_check_args
from daybot.db import Database
from daybot.ledger import Ledger
from daybot.options import occ
from tests.day_helpers import (DAY, PREV, chop_day, daily_history, flag_day, gap_day, make_app, option_snapshot,
                               stock_fill)


def _fill(ledger, f):
    fid = ledger.add_fill(f)
    return ledger.apply(fid)


def test_round_trip_with_scale_out_and_fees():
    led = Ledger(Database(":memory:"))
    t0 = at(DAY, "10:00")
    assert [e["type"] for e in _fill(led, stock_fill("a", "BUY", 100, 50.0, t0))] == ["open"]
    assert [e["type"] for e in _fill(led, stock_fill("b", "BUY", 100, 52.0, t0 + 60))] == ["add"]
    ev = _fill(led, stock_fill("c", "SELL", 50, 55.0, t0 + 120))
    assert ev[0]["type"] == "reduce" and ev[0]["trade"]["open_qty"] == 150
    ev = _fill(led, {**stock_fill("d", "SELL", 150, 54.0, t0 + 180), "fee": 1.0})
    t = ev[0]["trade"]
    assert ev[0]["type"] == "close" and t["entry_avg"] == 51.0 and abs(t["exit_avg"] - 54.25) < 1e-9
    assert abs(t["pnl"] - (3.25 * 200 - 1.0)) < 1e-9
    assert _fill(led, stock_fill("d", "SELL", 150, 54.0, t0 + 180)) == []          # same fill twice: ignored


def test_selling_more_than_you_hold_flips_to_short():
    led = Ledger(Database(":memory:"))
    t0 = at(DAY, "10:00")
    _fill(led, stock_fill("a", "BUY", 100, 50.0, t0))
    ev = _fill(led, stock_fill("b", "SELL", 150, 49.0, t0 + 60))
    assert [e["type"] for e in ev] == ["close", "open"]
    assert ev[0]["trade"]["pnl"] == -100.0 and ev[1]["trade"]["direction"] == "short" and ev[1]["trade"]["qty"] == 50
    ev = _fill(led, stock_fill("c", "BUY", 50, 47.0, t0 + 120))
    assert ev[0]["type"] == "close" and ev[0]["trade"]["pnl"] == 100.0


def test_an_expired_option_closes_at_zero():
    led = Ledger(Database(":memory:"))
    sym = occ("NVDA", "2026-10-23", "C", 140)
    base = {"account_id": "acc1", "kind": "option", "symbol": sym, "underlying": "NVDA", "option_type": "CALL",
            "strike": 140.0, "expiry": "2026-10-23", "multiplier": 100.0, "fee": 0.0, "source": "order", "effect": "open"}
    _fill(led, {**base, "order_id": "o1", "ts": at(DAY, "10:00"), "side": "BUY", "qty": 2, "price": 1.5})
    ev = _fill(led, {**base, "order_id": "x1", "ts": at("2026-10-23", "16:00"), "side": "EXPIRE", "qty": 2, "price": 0.0})
    assert ev[0]["type"] == "close" and ev[0]["trade"]["pnl"] == -300.0


def test_day_state_counts_streaks_and_revenge_windows():
    led = Ledger(Database(":memory:"))
    t0 = at(DAY, "10:00")
    for i, (a, b) in enumerate(((50, 49), (50, 49.5), (20, 21))):
        _fill(led, stock_fill(f"o{i}", "BUY", 10, a, t0 + i * 600, "AAA" if i < 2 else "BBB"))
        _fill(led, stock_fill(f"c{i}", "SELL", 10, b, t0 + i * 600 + 60, "AAA" if i < 2 else "BBB"))
    st = led.day_state(DAY)
    assert st["closed"] == 3 and st["streak"] == 0 and st["wins"] == 1 and st["realized"] == -5.0
    assert "AAA" in st["last_loss"] and "BBB" not in st["last_loss"]


def _app_at(hhmm, **kw):
    daily = {s: daily_history(PREV, 100.0, 2.0, seed=i) for i, s in enumerate(["TEST", "SPY", "QQQ", "GAPR"])}
    minute = {"TEST": flag_day(), "SPY": chop_day(seed=5), "QQQ": chop_day(seed=6), "GAPR": gap_day()}
    app = make_app(minute, daily, now=at(DAY, hhmm), **kw)
    app.db.set_meta("backtest_ts", 1)
    return app


def test_check_of_a_clean_long_and_a_chase():
    app = _app_at("10:57")
    res = asyncio.run(app.checker.check({"symbol": "TEST", "kind": "stock", "direction": "long", "qty": 100,
                                         "price": 102.6}, at(DAY, "10:57")))
    assert res["verdict"] in (GREEN, YELLOW) and res["plan"]["stop"] < 102.6 < res["plan"]["t1"] < res["plan"]["t2"]
    assert "Plan:" in format_check(res)
    app = _app_at("10:31")
    res = asyncio.run(app.checker.check({"symbol": "GAPR", "kind": "stock", "direction": "long", "qty": 50,
                                         "price": 108.1}, at(DAY, "10:31")))
    assert "chasing" in res["flags"]


def test_check_size_and_your_rules():
    app = _app_at("10:57", total=10_000)
    now = at(DAY, "10:57")
    unknown = asyncio.run(app.checker.check({"symbol": "TEST", "kind": "stock", "direction": "long", "qty": 2000,
                                             "price": 102.6}, now))
    assert "Account size unknown" in format_check(unknown)
    asyncio.run(app.broker.refresh_accounts(now))
    big = asyncio.run(app.checker.check({"symbol": "TEST", "kind": "stock", "direction": "long", "qty": 2000,
                                         "price": 102.6, "account_id": "acc1"}, now))
    assert big["verdict"] == RED and "oversized" in big["flags"]
    led = app.ledger
    _fill(led, stock_fill("l1", "BUY", 100, 105, now - 900, "TEST"))
    _fill(led, stock_fill("l2", "SELL", 100, 101, now - 840, "TEST"))      # -$400 = 4% of $10K: past the 3% limit
    res = asyncio.run(app.checker.check({"symbol": "TEST", "kind": "stock", "direction": "long", "qty": 10,
                                         "price": 102.6, "account_id": "acc1"}, now))
    assert res["verdict"] == RED and {"over_daily_limit", "revenge"} <= set(res["flags"])


def test_option_check_reads_spread_open_interest_and_late_0dte():
    sym = occ("TEST", DAY, "C", 103)
    chain = {"TEST": {sym: option_snapshot(0.40, 0.70, 0.45)}}
    contracts = {"TEST": [{"symbol": sym, "open_interest": 40}]}
    app = _app_at("14:45", chain=chain, contracts=contracts)
    res = asyncio.run(app.checker.check({"symbol": "TEST", "kind": "option", "contract": sym, "direction": "long",
                                         "qty": 2, "price": 0.70}, at(DAY, "14:45")))
    assert res["verdict"] == RED
    assert {"option_spread", "low_oi", "zero_dte_late"} <= set(res["flags"])
    assert "Option ≈" in format_check(res)


def test_option_check_with_a_weekend_date_finds_the_real_expiry():
    real = occ("TEST", "2026-10-23", "C", 103)
    chain = {"TEST": {real: option_snapshot(1.20, 1.25, 0.5)}}
    contracts = {"TEST": [{"symbol": real, "open_interest": 5000}]}
    app = _app_at("10:57", chain=chain, contracts=contracts)
    args = parse_check_args("TEST 103c 10/24 2", app.fake.now and __import__("datetime").date(2026, 10, 21))
    assert args["kind"] == "option" and args["qty"] == 2
    res = asyncio.run(app.checker.check(args, at(DAY, "10:57")))
    assert res["contract_info"]["expiry"] == "2026-10-23" and res["oi"] == 5000
    assert not {"option_spread", "low_oi"} & set(res["flags"])


def test_parse_check_args():
    import datetime as dt
    today = dt.date(2026, 10, 21)
    assert parse_check_args("NVDA", today)["kind"] == "stock"
    a = parse_check_args("NVDA short 100 @141.2", today)
    assert a["direction"] == "short" and a["qty"] == 100 and a["price"] == 141.2
    o = parse_check_args("SPY 580p 0dte 3 @1.1", today)
    assert o["kind"] == "option" and o["direction"] == "short" and o["qty"] == 3 and o["price"] == 1.1
    assert o["contract"] == "SPY261021P00580000"
    assert parse_check_args("hello there friend", today) is None
    assert parse_check_args("ok", today) is None and parse_check_args("OK", today)["symbol"] == "OK"
    assert parse_check_args("$nvda 50", today)["qty"] == 50


def test_fills_flow_check_recap_break_and_journal():
    app = _app_at("10:00")
    snap = app.snap
    times = {}
    asyncio.run(app.broker_tick(at(DAY, "09:00")))          # the bot's first look at the account: nothing to say

    def step(hhmm, fill=None):
        t = at(DAY, hhmm)
        app.fake.now = t
        if fill:
            snap.fills["acc1"].append(fill)
        asyncio.run(app.broker_tick(t))
        times[hhmm] = t
    step("10:31", stock_fill("g1", "BUY", 50, 108.1, at(DAY, "10:31"), "GAPR"))
    chk = app.notes.last("CHECK")
    assert chk and "You bought 50 GAPR" in chk["text"] and not chk["silent"]
    step("10:41", stock_fill("g2", "SELL", 50, 107.3, at(DAY, "10:41"), "GAPR"))
    assert "Closed GAPR long" in app.notes.last("TRADE")["text"]
    step("10:44", stock_fill("g3", "BUY", 50, 107.6, at(DAY, "10:44"), "GAPR"))
    step("10:50", stock_fill("g4", "SELL", 50, 107.0, at(DAY, "10:50"), "GAPR"))
    recap = app.notes.last("TRADE")
    assert "2 losses in a row" in recap["text"] and not recap["silent"]
    revenge = [m for m in app.notes.sent if m["kind"] == "CHECK" and "Revenge" in m["text"]]
    assert revenge and "AVOID" in revenge[0]["text"]
    lk = leaks(app.db, 30, at(DAY, "16:00"), min_n=1)
    names = [g["name"] for g in lk["leaks"]]
    assert any("revenge" in n for n in names) and lk["total"]["n"] == 2
    assert "Where the money leaks" in format_leaks(lk)


def test_partial_fills_are_booked_as_they_grow():
    app = _app_at("10:00")
    order = stock_fill("p1", "BUY", 40, 100.0, at(DAY, "10:00"), "TEST")
    app.snap.fills["acc1"] = [order]
    asyncio.run(app.broker.poll(at(DAY, "10:00")))
    app.snap.fills["acc1"] = [{**order, "qty": 100, "price": 100.6}]       # the rest filled, average now 100.60
    asyncio.run(app.broker.poll(at(DAY, "10:01")))
    asyncio.run(app.broker.poll(at(DAY, "10:02")))                          # nothing new
    t = app.db.row("select * from trades where symbol='TEST'")
    assert t["qty"] == 100 and abs(t["entry_avg"] - 100.6) < 1e-6
    assert app.db.scalar("select count(*) from fills") == 2


def test_the_first_sync_is_quiet_and_late_fills_are_booked_not_checked():
    app = _app_at("12:00")
    app.snap.fills["acc1"] = [stock_fill("old1", "BUY", 10, 102.0, at(DAY, "10:00"), "TEST")]
    asyncio.run(app.broker.poll(at(DAY, "12:00")))
    assert app.notes.sent == [] and app.db.scalar("select count(*) from trades") == 1
    app.snap.fills["acc1"].append(stock_fill("old2", "SELL", 10, 103.0, at(DAY, "11:00"), "TEST"))   # seen late
    app.snap.fills["acc1"].append(stock_fill("new1", "BUY", 5, 103.5, at(DAY, "11:58"), "TEST"))
    asyncio.run(app.broker.poll(at(DAY, "12:01")))
    kinds = app.notes.kinds()
    assert kinds.count("TRADE") == 1 and kinds.count("CHECK") == 1      # the late close gets its recap; the new buy its check


def test_an_expired_option_the_feed_never_closed_is_closed_at_zero():
    app = _app_at("10:00")
    sym = occ("TEST", "2026-10-16", "C", 110)
    led = app.ledger
    _fill(led, {"account_id": "acc1", "order_id": "o", "ts": at("2026-10-14", "10:00"), "kind": "option", "symbol": sym,
                "underlying": "TEST", "option_type": "CALL", "strike": 110.0, "expiry": "2026-10-16", "side": "BUY",
                "effect": "open", "qty": 1, "price": 0.8, "multiplier": 100.0, "fee": 0.0, "source": "order"})
    assert asyncio.run(app.broker.close_expired(at(DAY, "10:00"))) == 1
    t = app.db.row("select * from trades where symbol=?", (sym,))
    assert t["status"] == "closed" and t["pnl"] == -80.0 and "expired" in app.notes.last("TRADE")["text"]
    assert asyncio.run(app.broker.close_expired(at(DAY, "10:05"))) == 0


def test_a_trade_closed_while_the_bot_was_off_is_reconciled_with_the_broker():
    app = _app_at("10:00")
    _fill(app.ledger, stock_fill("y1", "BUY", 10, 99.0, at("2026-10-19", "10:00"), "TEST"))
    asyncio.run(app.broker.refresh_accounts(at(DAY, "10:00")))
    app.snap.positions_list = []                                        # the broker holds nothing now
    asyncio.run(app.broker.refresh_positions(at(DAY, "10:00")))
    t = app.db.row("select * from trades")
    assert t["status"] == "closed" and "exit price unknown" in t["note"]
    assert leaks(app.db, 30, at(DAY, "16:00"))["total"]["n"] == 0


def test_selling_an_old_holding_is_not_a_short():
    app = _app_at("10:00")
    asyncio.run(app.broker.refresh_accounts(at(DAY, "09:00")))
    app.snap.positions_list = [{"account_id": "acc1", "key": "AAPL", "symbol": "AAPL", "underlying": "AAPL",
                                "kind": "stock", "option_type": "", "strike": 0.0, "expiry": "", "qty": 100,
                                "avg_price": 150.0, "price": 230.0, "multiplier": 1.0}]
    asyncio.run(app.broker.refresh_positions(at(DAY, "09:00")))
    asyncio.run(app.broker.poll(at(DAY, "09:30")))                       # prime
    app.snap.fills["acc1"] = [stock_fill("s1", "SELL", 50, 230.0, at(DAY, "10:00"), "AAPL")]
    asyncio.run(app.broker.poll(at(DAY, "10:01")))
    assert not app.db.rows("select * from trades where direction='short'")
    msg = app.notes.last("TRADE")["text"]
    assert "holding from before DayBot" in msg and "+$4,000" in msg and not app.notes.last("CHECK")
    assert leaks(app.db, 30, at(DAY, "16:00"))["total"]["n"] == 0


def test_an_investing_account_is_synced_but_not_checked():
    app = _app_at("10:57")
    asyncio.run(app.broker.refresh_accounts(at(DAY, "09:00")))
    asyncio.run(app.broker.poll(at(DAY, "09:30")))
    out = asyncio.run(app.commands.text("/accounts off 1", at(DAY, "10:00")))[0]["text"]
    assert "investing" in out
    app.snap.fills["acc1"] = [stock_fill("b1", "BUY", 10, 102.6, at(DAY, "10:57"), "TEST")]
    asyncio.run(app.broker.poll(at(DAY, "10:58")))
    assert app.db.scalar("select count(*) from trades") == 1 and not app.notes.last("CHECK")


def test_monitor_says_stop_and_close_once():
    app = _app_at("11:00")
    led = app.ledger
    ev = _fill(led, stock_fill("m1", "BUY", 100, 102.6, at(DAY, "10:57"), "TEST"))
    led.set_plan(ev[0]["trade"]["id"], {"stop": 103.5, "t1": 104, "t2": 105, "risk_usd": 90, "und_price": 102.6}, 0)
    for hhmm in ("11:00", "11:01"):
        app.fake.now = at(DAY, hhmm)
        asyncio.run(app.data.refresh(["TEST"], at(DAY, hhmm)))
        asyncio.run(app.monitor.tick(at(DAY, hhmm)))
    stops = [m for m in app.notes.sent if m["kind"] == "MONITOR" and "stop" in m["text"]]
    assert len(stops) == 1
    app.fake.now = at(DAY, "15:46")
    asyncio.run(app.data.refresh(["TEST"], at(DAY, "15:46")))
    asyncio.run(app.monitor.tick(at(DAY, "15:46")))
    assert any("15 minutes to the close" in m["text"] for m in app.notes.sent)
