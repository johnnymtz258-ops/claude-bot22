"""The research strategies: the in-play 5-minute breakout and the index noise band — their rules, entries and exits."""
import asyncio

from daybot.backtest import band_moves, band_table, rank_in_play, run
from daybot.bars import T, resample, vwap
from daybot.clock import Market, at
from daybot.setups import Day, detect, simulate
from tests.day_helpers import DAY, PREV, FakeAlpaca, daily_history, path_bars, premarket

CFG = {"ORB_MINUTES": 15, "GAP_MIN_PCT": 3}
M = Market()


def _day(bars1, *, in_play=1, rvol5=3.0, band=None, pdc=100.0, atr=2.0):
    rth = [b for b in resample(bars1, 5) if b[T] >= M.open_ts(DAY)]
    return Day("TEST", rth, vwap(rth), M.open_ts(DAY), M.close_ts(DAY), atr, 5e6, pdc, 0.0, 0.0, 1, 0.0,
               in_play, rvol5, band), rth


def _runner(up=True, base=100.0):
    """Green (red) first five minutes, then it breaks the first candle's high (low) and trends all day."""
    s = 1 if up else -1
    pts = [("09:30", base), ("09:35", base + 0.6 * s), ("09:40", base + 0.52 * s), ("09:50", base + 1.2 * s),
           ("11:00", base + 2.5 * s), ("15:59", base + 4.0 * s)]
    return path_bars(DAY, pts, noise=0.0002)


def test_in_play_breakout_waits_for_the_first_candle_and_holds_to_the_close():
    for up in (True, False):
        bars1 = _runner(up)
        d, rth = _day(bars1)
        sigs = detect(d.upto(0), 0, CFG, set())
        orb5 = [x for x in sigs if x.setup == "orb5"]
        assert [(x.direction, x.plan, x.entry_type) for x in orb5] == [("long" if up else "short", "close", "stop")]
        sig = orb5[0]
        first = rth[0]
        assert abs(sig.entry - (first[2] if up else first[3])) < 1e-9          # its high (low)
        assert abs(abs(sig.entry - sig.stop) - 0.2) < 1e-9                      # 10% of a $2 daily range
        assert sig.t1 == 0 and sig.expires == M.close_ts(DAY) - 3600
        res = simulate(sig, bars1, M.close_ts(DAY), 0.05, bar_seconds=60)
        assert res["reason"] == "time" and res["r"] > 10                        # one runner pays for ten losers
        assert not [x for x in detect(_day(bars1, in_play=0)[0].upto(0), 0, CFG, set()) if x.setup == "orb5"]


def test_in_play_breakout_never_filled_or_stopped_in_the_fill_minute():
    pts = [("09:30", 100.0), ("09:35", 100.6), ("10:30", 100.1), ("15:59", 99.0)]      # never takes out the high
    bars1 = path_bars(DAY, pts, noise=0.0002)
    d, _ = _day(bars1)
    sig = next(x for x in detect(d.upto(0), 0, CFG, set()) if x.setup == "orb5")
    assert simulate(sig, bars1, M.close_ts(DAY), 0.05, bar_seconds=60) is None
    spike = list(_runner())
    k = next(i for i, b in enumerate(spike) if b[T] == at(DAY, "09:40"))
    t, o, h, l, c, v = spike[k]
    spike[k] = (t, o, sig.entry + 0.5, sig.stop - 0.5, c, v)                     # through the level and the stop
    res = simulate(sig, spike, M.close_ts(DAY), 0.05, bar_seconds=60)
    assert res["reason"] == "stop" and -1.6 < res["r"] < -0.9


def _band_day():
    pts = [("09:30", 100.0), ("10:00", 100.6), ("10:30", 100.9), ("11:00", 101.0), ("11:30", 100.2),
           ("15:59", 99.8)]
    return path_bars(DAY, pts, noise=0.0001)


def test_index_band_breakout_and_its_half_hour_exit():
    table = {m: 0.002 + 0.0001 * (m // 30) for m in range(30, 361, 30)}
    bars1 = _band_day()
    d, rth = _day(bars1, in_play=0, band=table)
    fired, sigs = set(), []
    for i in range(len(rth)):
        sigs += [x for x in detect(d.upto(i), i, CFG, fired) if x.setup == "band"]
    assert [(x.direction, x.plan, x.ts) for x in sigs] == [("long", "band", at(DAY, "10:00"))]
    res = simulate(sigs[0], rth, M.close_ts(DAY), 0.05)
    assert res["reason"] == "trail" and res["exit_ts"] == at(DAY, "11:30")      # closed back under the band
    assert abs(res["r"]) < 3
    down = [(t, 200 - o, 200 - l, 200 - h, 200 - c, v) for t, o, h, l, c, v in bars1]
    d2, rth2 = _day(down, in_play=0, band=table, pdc=100.0)
    fired = set()
    got = [x for i in range(len(rth2)) for x in detect(d2.upto(i), i, CFG, fired) if x.setup == "band"]
    assert [x.direction for x in got] == ["short"]


def test_band_table_and_ranking_helpers():
    moves = [band_moves(path_bars(day, [("09:30", 100), ("16:00", 101)], noise=0), day, M) for day in PREV[-14:]]
    table = band_table(moves)
    assert set(table) == set(range(30, 361, 30)) and table[60] < table[360]
    daily = {"AAA": daily_history(PREV, 50.0, 3.0), "BBB": daily_history(PREV, 50.0, 3.0, seed=2),
             "PENNY": daily_history(PREV, 2.0, 5.0), "TQQQ": daily_history(PREV, 80.0, 3.0)}
    first5 = {s: (M.open_ts(DAY), 1, 1, 1, 1, vol) for s, vol in
              (("AAA", 90_000), ("BBB", 30_000), ("PENNY", 900_000), ("TQQQ", 900_000))}
    hist = {s: [30_000.0] * 14 for s in daily}
    got = rank_in_play(first5, hist, daily, DAY, _Cfg(), 20)
    assert got == [("AAA", 1, 3.0), ("BBB", 2, 1.0)]                          # no penny stock, no leveraged fund


class _Cfg:
    def get(self, name):
        return {"MIN_PRICE": 5, "MIN_AVG_VOLUME": 1e6}[name]


def test_backtest_finds_the_stock_in_play_and_trades_it_on_minute_bars():
    days = PREV[-16:] + [DAY]
    minute = {"HOT": [], "SPY": [], "QQQ": []}
    for day in days[:-1]:
        for s in minute:
            minute[s] += path_bars(day, [("09:30", 100.0), ("16:00", 100.3)], noise=0.0003, vol=20_000)
    minute["HOT"] += premarket(DAY, 100.0) + path_bars(DAY, [("09:30", 100.0), ("09:35", 100.6), ("09:40", 100.52),
                                                             ("09:50", 101.2), ("15:59", 104.0)],
                                                       noise=0.0002, vol=80_000)
    for s in ("SPY", "QQQ"):
        minute[s] += path_bars(DAY, [("09:30", 100.0), ("16:00", 100.2)], noise=0.0003)
    daily = {s: daily_history(PREV, 100.0, 2.0) for s in minute}
    fake = FakeAlpaca(minute, daily, now=at(DAY, "20:00"))
    from daybot.config import Config
    res = asyncio.run(run(fake, M, Config(), "2026-10-22", days=1, core=["SPY", "QQQ"], universe=["HOT", "SPY", "QQQ"]))
    assert res["in_play"] == 1
    hot = [t for t in res["list"] if t["symbol"] == "HOT" and t["setup"] == "orb5"]
    assert len(hot) == 1 and hot[0]["r"] > 5
    assert ("1Min", ("HOT",)) in {(c[2], c[1]) for c in fake.calls if c[0] == "bars"}
    st = res["stats"]["orb5_long"]
    assert st["list"] and st["list"][0][1] == "HOT"


def test_live_day_ranks_stocks_in_play_sends_one_message_and_the_autopilot_follows():
    from tests.day_helpers import make_app
    prior = PREV[-15:]
    minute = {}
    for sym in ("HOT", "SPY", "QQQ"):
        minute[sym] = [b for d in prior for b in path_bars(d, [("09:30", 100.0), ("16:00", 100.3)], noise=0.0003,
                                                             vol=20_000)]
    minute["HOT"] += premarket(DAY, 100.0) + path_bars(DAY, [("09:30", 100.0), ("09:35", 100.6), ("09:40", 100.52),
                                                             ("09:50", 101.2), ("15:59", 104.0)],
                                                       noise=0.0002, vol=80_000)
    for sym in ("SPY", "QQQ"):
        minute[sym] += path_bars(DAY, [("09:30", 100.0), ("16:00", 100.1)], noise=0.0003)
    daily = {s: daily_history(PREV, 100.0, 2.0) for s in minute}
    app = make_app(minute, daily, now=at(DAY, "06:00"))
    app.cfg.watchlist = ["HOT"]

    async def walk():
        t = at(DAY, "08:55")
        while t <= at(DAY, "16:20"):
            app.fake.now = t
            await app.market_tick(t)
            t += 60
    asyncio.run(walk())
    assert app.scanner.in_play_today.get("HOT", (0,))[0] == 1
    msgs = [m for m in app.notes.sent if m["kind"] == "SETUP" and "Stocks in play" in m["text"]]
    assert len(msgs) == 1 and "HOT" in msgs[0]["text"] and "buy-stop" in msgs[0]["text"]
    row = app.db.row("select * from paper where symbol='HOT' and setup='orb5'")
    assert row["auto"] == 1 and row["status"] == "closed" and row["reason"] == "time" and row["r_mult"] > 5
    auto = [m["text"] for m in app.notes.sent if m["kind"] == "AUTO"]
    assert any("Autopilot bought" in t and "HOT" in t and "first 5-minute high" in t for t in auto)
    assert any("Autopilot closed HOT" in t and "held to the close" in t for t in auto)
    assert app.autopilot.summary(at(DAY, "16:20"))["balance"] > 1000


def test_live_spy_band_alert_and_its_half_hour_exit():
    from tests.day_helpers import make_app
    prior = PREV[-15:]
    minute = {sym: [b for d in prior for b in path_bars(d, [("09:30", 100.0), ("16:00", 100.3)], noise=0.0003)]
              for sym in ("SPY", "QQQ")}
    minute["SPY"] += path_bars(DAY, [("09:30", 100.0), ("10:00", 100.6), ("10:30", 100.9), ("11:00", 101.0),
                                     ("11:30", 100.0), ("16:00", 99.9)], noise=0.0002)
    minute["QQQ"] += path_bars(DAY, [("09:30", 100.0), ("16:00", 100.0)], noise=0.0002)
    app = make_app(minute, {s: daily_history(PREV, 100.0, 2.0) for s in minute}, now=at(DAY, "06:00"))

    async def walk():
        t = at(DAY, "08:55")
        while t <= at(DAY, "16:20"):
            app.fake.now = t
            await app.market_tick(t)
            t += 60
    asyncio.run(walk())
    msg = next(m for m in app.notes.sent if m["kind"] == "SETUP" and m["symbol"] == "SPY")
    assert "noise band" in msg["text"] and "half-hour close" in msg["text"]
    row = app.db.row("select * from paper where symbol='SPY' and setup='band'")
    assert row["auto"] == 1 and row["status"] == "closed" and row["reason"] == "trail"
    assert at(DAY, "11:00") < row["closed_ts"] <= at(DAY, "11:30")
    assert any("Autopilot closed SPY" in m["text"] and "noise band" in m["text"]
               for m in app.notes.sent if m["kind"] == "AUTO")


def test_end_of_day_bounce_ranks_losers_and_holds_to_the_close():
    from daybot.setups import eod_rank, eod_signal
    moves = {f"S{i:02d}": (i - 50) / 1000 for i in range(100)}             # -5% … +4.9%
    losers, winners = eod_rank(moves)
    assert losers[:2] == ["S00", "S01"] and winners[0] == "S99" and len(losers) == 10
    close = M.close_ts(DAY)
    sig = eod_signal("S00", "long", 50.0, close - 1800, close, 2.0, -0.05, 1)
    assert sig.plan == "eod" and abs(sig.risk - 0.5) < 1e-9 and "biggest loser" in sig.reason
    bar = (close - 1800, 50.0, 50.6, 49.9, 50.4, 9e5)                       # bounced 0.8% into the close
    res = simulate(sig, [bar], close, 0.05, bar_seconds=1800)
    assert res["reason"] == "close" and 0.6 < res["r"] < 0.8                 # costs both ways come off


def test_live_end_of_day_bounce_message_and_autopilot():
    from tests.day_helpers import make_app
    from daybot.backtest import UNIVERSE
    names = [s for s in UNIVERSE if s not in ("SPY", "QQQ")][:40]
    minute, daily = {}, {}
    for i, sym in enumerate(names):
        end = 100.0 * (1 + (i - 20) / 400)                                    # -5% … +4.75% by 3:30
        minute[sym] = path_bars(DAY, [("09:30", 100.0), ("15:30", end), ("15:59", end * (1.006 if i < 4 else 1.0))],
                                noise=0.0001)
        daily[sym] = daily_history(PREV, 100.0, 2.0)
    for sym in ("SPY", "QQQ"):
        minute[sym] = path_bars(DAY, [("09:30", 100.0), ("16:00", 100.0)], noise=0.0001)
        daily[sym] = daily_history(PREV, 100.0, 2.0)
    app = make_app(minute, daily, now=at(DAY, "15:00"))
    app.cfg.set("ORB5_TOP", "0")

    async def walk():
        t = at(DAY, "15:20")
        while t <= at(DAY, "16:20"):
            app.fake.now = t
            await app.market_tick(t)
            t += 60
    asyncio.run(walk())
    msg = next(m for m in app.notes.sent if m["kind"] == "SETUP" and "End-of-day bounce" in m["text"])
    assert names[0] in msg["text"]
    rows = app.db.rows("select * from paper where setup='eod' and direction='long' order by id")
    assert len(rows) == 5 and rows[0]["symbol"] == names[0]
    assert all(r["status"] == "closed" and r["reason"] == "close" for r in rows)
    auto = [r for r in rows if r["auto"]]
    assert len(auto) == 3 and all(r["r_mult"] > 0 for r in auto[:3])         # the 3 worst bounced
