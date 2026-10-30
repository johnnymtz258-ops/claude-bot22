"""Crypto: the trend plan (signal, sizing, trading, backtest, live autopilot) and the BTC 15-minute Up or Down call."""
import asyncio
import json
import math
import statistics

from daybot import crypto as cr
from daybot import updown as ud
from tests.day_helpers import btc_minutes, crypto_days, make_app

T0 = 1_640_995_200            # 2022-01-01 00:00 UTC


def _naive_flags(closes, n):
    out, inside, stop = [], False, 0.0
    for i, c in enumerate(closes):
        if i < n:
            out.append(False)
            continue
        w = closes[i - n:i]
        mid = (max(w) + min(w)) / 2
        if inside:
            if c < stop:
                inside = False
            else:
                stop = max(stop, mid)
        elif c > max(w):
            inside, stop = True, mid
        out.append(inside)
    return out


# ---- the signal --------------------------------------------------------------------------------------------------
def test_each_trend_model_buys_new_highs_and_sells_under_its_rising_midpoint():
    closes = [b[4] for b in crypto_days(T0, 700, seed=4, regime=90)]
    for n in cr.LOOKBACKS:
        assert cr.trend_flags(closes, n) == _naive_flags(closes, n), n
    up = [100 + i for i in range(30)] + [129 - 3 * i for i in range(15)]
    flags = cr.trend_flags(up, 10)
    assert flags[11] and flags[29]                    # in on the way up
    assert not flags[-1]                              # out after the drop under the middle of the channel


def test_volatility_is_the_annualized_spread_of_daily_moves():
    closes = [b[4] for b in crypto_days(T0, 300, seed=9)]
    v = cr.realized_vol(closes)
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    assert abs(v[-1] - statistics.stdev(rets[-90:]) * math.sqrt(365)) < 1e-9
    assert v[10] is None


def test_weights_scale_with_trend_and_calm_and_never_lever():
    assert cr.weight(0, 0.5, 10, 50) == 0
    strong_calm = cr.weight(1.0, 0.4, 10, 50)
    strong_wild = cr.weight(1.0, 1.2, 10, 50)
    weak_calm = cr.weight(3 / 9, 0.4, 10, 50)
    assert strong_calm > strong_wild and strong_calm > weak_calm
    assert cr.weight(1.0, 0.01, 10, 50) == 2.0 / 10                # capped at 2× its slice
    capped = cr.cap_gross({"A": 0.8, "B": 0.6})
    assert abs(sum(capped.values()) - 1.0) < 1e-9                 # never more than 100% in


def test_trades_only_when_far_from_target_and_never_spend_more_than_the_cash():
    book = {"cash": 1000.0, "qty": {"BTC": 0.01}}                 # $500 of BTC at 50,000
    prices = {"BTC": 50_000.0, "ETH": 2_000.0}
    eq = cr.equity_of(book, prices)
    assert cr.plan_trades(book["qty"], prices, eq, {"BTC": 0.36}) == []        # 10% off: hold
    t = cr.plan_trades(book["qty"], prices, eq, {"BTC": 0.1, "ETH": 0.5})
    assert [(x["coin"], x["side"], x["reason"]) for x in t] == [("BTC", "sell", "smaller"), ("ETH", "buy", "trend on")]
    fills = cr.execute(book, t, prices, 1.0)
    assert book["cash"] >= -1e-9 and abs(sum(f["cost"] for f in fills) - 0.01 * sum(f["value"] for f in fills)) < 1e-6
    out = cr.rebalance(book, prices, {}, 1.0)                     # every trend broke: all cash
    assert not book["qty"] and {f["reason"] for f in out} == {"trend off"}
    assert cr.execute({"cash": 10.0, "qty": {}}, [{"coin": "ETH", "side": "buy", "value": 500.0, "all": False,
                                                    "reason": "trend on"}], prices, 1.0)[0]["value"] < 10


def _market(seed=1, n=1100):
    return {"BTC": crypto_days(T0, n, 30_000, seed=seed, regime=150, drift=0.004, vol=0.03),
            "ETH": crypto_days(T0, n, 2_000, seed=seed + 1, regime=130, drift=0.005, vol=0.04),
            "SOL": crypto_days(T0 + 300 * 86400, n - 300, 40, seed=seed + 2, regime=100, drift=0.006, vol=0.055)}


def test_the_backtest_trades_the_next_day_pays_costs_and_compares_with_bitcoin():
    data = _market()
    res = cr.backtest(data, ["BTC", "ETH", "SOL"], 50, 1.0)
    free = cr.backtest(data, ["BTC", "ETH", "SOL"], 50, 0.0)
    assert res["status"] in {"proven", "promising", "negative", "thin"}
    assert res["trades"] > 0 and res["costs"] > 0 and free["strategy"]["total"] > res["strategy"]["total"]
    assert 0 < res["exposure"] <= 1 and res["years"] and res["curve"][0][1] == 100.0
    assert res["start"] >= T0 + 360 * 86400                       # the long lookbacks warm up first
    assert set(res["per_coin"]) == {"BTC", "ETH", "SOL"}
    json.dumps(res)
    assert "Trend plan" in "\n".join(cr.backtest_lines(res))


def test_the_backtest_never_trades_on_a_price_it_could_not_know():
    data = _market(seed=3)
    cut = data["BTC"][800][0]
    changed = {c: [b if b[0] < cut else (b[0], b[1] * 1.3, b[2] * 1.3, b[3] * 1.3, b[4] * 1.3, 0) for b in v]
               for c, v in data.items()}                           # everything from day 800 on is different
    a = cr.backtest(data, ["BTC", "ETH", "SOL"], 50, 1.0)
    b = cr.backtest(changed, ["BTC", "ETH", "SOL"], 50, 1.0)
    before_a = [p for p in a["curve"] if p[0] < cut]
    assert before_a and before_a == [p for p in b["curve"] if p[0] < cut]      # the past never sees the future
    assert a["curve"] != b["curve"]


def test_a_monthly_universe_sells_coins_that_drop_out():
    data = _market(seed=5)
    only_btc_later = lambda ts: {"BTC"} if ts > T0 + 700 * 86400 else {"BTC", "ETH", "SOL"}
    res = cr.backtest(data, ["BTC", "ETH", "SOL"], 50, 0.0, universe=only_btc_later)
    assert res["trades"] > 0 and res["status"]


# ---- live --------------------------------------------------------------------------------------------------------
def _crypto_app(days=900):
    now = T0 + days * 86400 + 3600                                 # an hour after a daily candle closed
    app = make_app(now=now)
    app.fake.crypto_daily = {c: v[:days] for c, v in _market(n=days + 5).items()}
    app.fake.crypto_list = ["BTC", "ETH", "SOL", "USDC"]
    app.fake.now = now
    return app, now


def test_the_live_plan_loads_tests_and_the_autopilot_trades_once_a_day():
    app, now = _crypto_app()
    asyncio.run(app.crypto.tick(now))
    c = app.crypto
    assert c.coins == ["BTC", "ETH", "SOL"] and set(c.series) == {"BTC", "ETH", "SOL"}
    assert c.bt() and c.bt()["status"] and "BACKTEST" in app.notes.kinds()
    msg = app.notes.last("CRYPTO")
    assert msg and "Crypto plan" in msg["text"] and "Crypto autopilot" in msg["text"]
    eq = c.equity()
    assert abs(eq - 1000) < 50 and int(c.db.get_meta("crypto_last_bar")) == c.latest_bar()
    n = len(app.notes.sent)
    asyncio.run(c.tick(now + 600))                                 # same candle: nothing new
    assert len(app.notes.sent) == n
    # the next day's candle closes
    later = now + 86400
    app.fake.now = later
    app.fake.crypto_daily = {k: v + [(v[-1][0] + 86400, v[-1][4], v[-1][4] * 1.01, v[-1][4] * 0.99, v[-1][4], 0)]
                             for k, v in app.fake.crypto_daily.items()}
    c._last.clear()
    asyncio.run(c.tick(later))
    assert int(c.db.get_meta("crypto_last_bar")) == c.latest_bar() > 0
    s = c.summary(later)
    json.dumps(s, default=str)
    assert s["board"] and s["autopilot"]["since"] and s["backtest"]["verdict"]
    assert "BTC" in [r["coin"] for r in s["board"]]


def test_crypto_commands_and_dashboard():
    app, now = _crypto_app()

    def say(text):
        return asyncio.run(app.commands.text(text, now))[0]["text"]
    board = say("/crypto")
    assert "Crypto trend plan" in board and "/9" in board and "Backtest" in board
    assert "Crypto backtest" in say("/crypto test")
    assert "starts over with a pretend $500" in say("/crypto reset 500") and app.crypto.start() == 500
    assert "off" in say("/crypto off") and not app.crypto.on
    assert "is off" in say("/coins")
    say("/crypto on")
    assert app.crypto.on
    from daybot.dashboard import state
    st = state(app, now)
    json.dumps(st, default=str)
    assert st["crypto"]["board"] and "Crypto" in {x["group"] for x in st["settings"]}
    assert "Crypto plan" in app.status_text(now)


def test_crypto_tables_are_in_the_export():
    import io
    import zipfile
    from daybot.export import build
    app, now = _crypto_app()
    asyncio.run(app.crypto.tick(now))
    z = zipfile.ZipFile(io.BytesIO(build(app.db, app.cfg, now)))
    assert {"crypto_trades.json", "crypto_equity.json", "updown.json"} <= set(z.namelist())
    assert json.loads(z.read("crypto_trades.json"))


# ---- BTC 15-minute Up or Down -------------------------------------------------------------------------------------
def test_the_odds_follow_the_move_the_time_left_and_the_swing():
    assert ud.prob_up(0, 0.001, 10) == 0.5
    a, b = ud.prob_up(0.002, 0.001, 10), ud.prob_up(0.002, 0.001, 2)
    assert 0.5 < a < b                                             # same lead, less time left: likelier
    assert ud.prob_up(-0.002, 0.001, 2) < 0.5 and ud.prob_up(0.002, 0.003, 10) < a
    assert ud.prob_up(0.0001, 0.001, 0) == 1.0 and ud.window_start(1_790_000_123) % 900 == 0


def test_a_random_walk_has_no_lean_and_honest_odds():
    now = 1_790_000_000 // 900 * 900
    bars = btc_minutes(now - 61 * 86400, 61 * 1440, seed=7)
    res = ud.study(bars, now)
    t = res["test"]
    assert not res["pattern"]["rule"] and t["lean"]["n"] == 0      # no made-up pattern
    assert t["favorite"]["10"]["hit"] > t["favorite"]["5"]["hit"] > 0.55
    for b in t["calibration"]:
        if b["n"] > 200:
            assert abs(b["said"] - b["happened"]) < 0.06, b


def test_a_real_pattern_becomes_a_lean():
    now = 1_790_000_000 // 900 * 900
    bars = btc_minutes(now - 61 * 86400, 61 * 1440, seed=8, momentum=0.08)
    res = ud.study(bars, now)
    assert res["pattern"]["rule"] == "momentum" and res["test"]["lean"]["hit"] > 0.5


def test_the_live_call_records_each_window_and_answers():
    now = 1_790_000_000 // 900 * 900
    app = make_app(now=now)
    app.fake.crypto_minute = {"BTC": btc_minutes(now - 61 * 86400, 61 * 1440 + 60, seed=9)}
    u = app.updown

    async def walk(t0, t1):
        t = t0
        while t <= t1:
            app.fake.now = t
            await u.tick(t)
            t += 60
    asyncio.run(walk(now + 30, now + 1830))                        # two windows
    rows = app.db.rows("select * from updown order by ts")
    assert rows and rows[0]["ts"] == now and rows[0]["outcome"] in (0, 1) and rows[0]["p10"] is not None
    t = now + 1800 + 400
    app.fake.now = t
    text = u.text(t, "62")
    assert "Most likely" in text and "Price to beat" in text and "Your price: Up 62¢" in text
    assert "76543" in u.text(t, "76543").replace(",", "")
    c = u.summary(t)
    json.dumps(c, default=str)
    assert c["ready"] and 0 <= c["p_up"] <= 1 and c["recent"]
    say = asyncio.run(app.commands.text("/btc", t))[0]["text"]
    assert "BTC 15-min Up or Down" in say


def test_the_15_minute_messages_are_off_unless_you_want_them():
    now = 1_790_000_000 // 900 * 900
    app = make_app(now=now)
    app.fake.crypto_minute = {"BTC": btc_minutes(now - 3 * 86400, 3 * 1440 + 30, seed=10)}
    app.fake.now = now + 120
    asyncio.run(app.updown.tick(now + 120))
    assert "BTC15" not in app.notes.kinds()
    app.cfg.set("BTC15_ALERTS", 1)
    app.updown._last.clear()
    asyncio.run(app.updown.tick(now + 180))
    assert "BTC15" in app.notes.kinds()
