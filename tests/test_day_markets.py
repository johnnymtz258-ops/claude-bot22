"""The BTC 15-minute call against the market's real prices: parsing, fees, edges, the saved quotes and the paper record."""
import asyncio
import json

from daybot import markets as mk
from daybot import updown as ud
from daybot.alpaca import parse_ts
from tests.day_helpers import btc_minutes, make_app

START = 1_790_000_000 // 900 * 900


# ---- parsing what the venues send -------------------------------------------------------------------------------
def test_polymarket_prices_and_the_price_to_beat_are_read_whichever_shape_they_come_in():
    event = [{"slug": f"btc-updown-15m-{START}", "eventMetadata": {"priceToBeat": 76543.21},
              "markets": [{"outcomes": "[\"Up\", \"Down\"]", "outcomePrices": "[\"0.62\", \"0.38\"]",
                           "bestBid": 0.61, "bestAsk": 0.63}]}]
    q = mk.parse_polymarket(event, START)
    assert (q["up_ask"], q["up_bid"], q["dn_ask"], q["dn_bid"]) == (63.0, 61.0, 39.0, 37.0)
    assert q["strike"] == 76543.21 and not q["approx"]
    mid_only = mk.parse_polymarket([{"slug": "x", "markets": [{"outcomes": ["Up", "Down"], "outcomePrices": ["0.5", "0.5"]}]}], 1)
    assert mid_only["approx"] and mid_only["up_ask"] == 50.5 and mid_only["dn_ask"] == 50.5
    assert mk.parse_polymarket([], START) is None and mk.parse_polymarket([{"slug": "x", "markets": [{}]}], 1) is None


def test_kalshi_prices_come_as_cents_or_as_dollar_text_and_one_cent_is_not_one_dollar():
    end = parse_ts("2026-10-30T16:30:00Z")
    cents = {"markets": [{"close_time": "2026-10-30T16:15:00Z", "yes_ask": 1}, {
        "close_time": "2026-10-30T16:30:00Z", "yes_bid": 55, "yes_ask": 57, "no_bid": 43, "no_ask": 45, "floor_strike": 76000.5}]}
    q = mk.parse_kalshi(cents, end)
    assert (q["up_ask"], q["dn_ask"], q["strike"]) == (57.0, 45.0, 76000.5)
    dollars = {"markets": [{"close_time": "2026-10-30T16:30:00Z", "yes_bid_dollars": "0.0100", "yes_ask_dollars": "0.0200",
                            "no_bid_dollars": "0.9800", "no_ask_dollars": "0.9900"}]}
    q2 = mk.parse_kalshi(dollars, end)
    assert (q2["up_ask"], q2["up_bid"], q2["dn_ask"]) == (2.0, 1.0, 99.0)       # 2 cents, not 2 dollars
    assert mk.parse_kalshi({"markets": []}, end) is None and mk.parse_kalshi(None, end) is None
    assert mk.parse_kalshi(cents, end + 900) is None                               # a different window isn't this one


def test_fees_shrink_toward_the_ends_and_an_edge_is_what_is_left_after_them():
    assert abs(mk.fee_cents(50, 1.75) - 1.75) < 1e-9 and mk.fee_cents(95, 1.75) < 0.4 and mk.fee_cents(5, 1.75) < 0.4
    e = mk.edges(0.80, {"up_ask": 70, "dn_ask": 32}, 1.75)
    assert e["side"] == "Up" and abs(e["edge"] - (80 - 70 - mk.fee_cents(70, 1.75))) < 1e-9
    d = mk.edges(0.30, {"up_ask": 40, "dn_ask": 55}, 1.75)
    assert d["side"] == "Down" and d["edge"] > 10                                  # 70¢ of fair value for a 55¢ ask


def test_the_paper_record_counts_only_signals_pays_the_fee_and_compares_with_the_favorite():
    rows = [{"day": "d1", "p10": 0.85, "outcome": 1, "up_ask": 60, "dn_ask": 42},      # edge ~24¢ → trade, wins
            {"day": "d1", "p10": 0.85, "outcome": 0, "up_ask": 60, "dn_ask": 42},      # same signal, loses
            {"day": "d2", "p10": 0.55, "outcome": 1, "up_ask": 54, "dn_ask": 48},      # no edge → no trade
            ]
    r = mk.paper_trades(rows, 4, 1.75)
    sig = r["signal"]
    f = mk.fee_cents(60, 1.75)
    assert sig["n"] == 2 and abs(sig["total_c"] - ((100 - 60 - f) + (0 - 60 - f))) < 1e-9 and sig["win"] == 0.5
    assert r["favorite"]["n"] == 3 and r["windows"] == 3


# ---- the live flow ----------------------------------------------------------------------------------------------
def _app_with_quotes(up=45.0, dn=58.0):
    app = make_app(now=START)
    app.fake.crypto_minute = {"BTC": btc_minutes(START - 61 * 86400, 61 * 1440 + 400, seed=9)}
    calls = []

    async def poly(session, start):
        calls.append(("polymarket", start))
        return mk.quote("polymarket", up, up - 1, dn, dn - 1, 80000.0)

    async def kalshi(session, end):
        calls.append(("kalshi", end))
        return None
    app.updown.fetchers = {"polymarket": poly, "kalshi": kalshi}
    app.fake.session = object()
    return app, calls


def _walk(app, t0, t1, step=20):
    async def go():
        t = t0
        while t <= t1:
            app.fake.now = t
            await app.updown.tick(t)
            t += step
    asyncio.run(go())


def test_each_window_saves_what_the_market_charged_at_5_and_10_minutes():
    app, calls = _app_with_quotes()
    _walk(app, START + 20, START + 1000)
    assert ("polymarket", START) in calls and ("kalshi", START + 900) in calls          # asked for THIS window
    row = app.db.row("select * from updown_quotes where ts=? and venue='polymarket'", (START,))
    assert row and row["m5_up"] == 45.0 and row["m10_dn"] == 58.0 and row["strike"] == 80000.0
    assert not app.db.rows("select * from updown_quotes where venue='kalshi'")           # no price, no row


def test_a_stale_quote_from_an_earlier_window_is_never_used():
    app, _ = _app_with_quotes()
    _walk(app, START + 20, START + 400)
    assert app.updown.live_quotes(START + 400)
    assert not app.updown.live_quotes(START + 1000)                                      # next window: nothing yet
    assert app.updown.card(START + 1000)["quotes"] == []


def test_the_text_shows_the_markets_prices_the_edge_and_the_paper_record():
    app, _ = _app_with_quotes(up=40.0, dn=62.0)
    _walk(app, START + 20, START + 1200)                                                 # into the second window
    now = START + 1200
    text = app.updown.text(now)
    assert "Polymarket: Up 40¢ / Down 62¢" in text
    assert "paper record" in text or "Measuring whether" in text
    c = app.updown.summary(now)
    json.dumps(c, default=str)
    assert c["quotes"] and c["quotes"][0]["venue"] == "polymarket" and "paper" in c
    assert app.db.row("select * from updown_quotes where ts=? and venue='polymarket'", (START,))   # window 1 was kept


def _fill_history(app, n_windows, win_rate_signal):
    """Windows with saved quotes where the model strongly favors Up (p10 0.9) and the market asks only 60¢ for Up."""
    for i in range(n_windows):
        ts = START - 86400 * (1 + i % 12) - 900 * (i // 12 + 1)           # spread over 12 different days
        won = 1 if i < int(n_windows * win_rate_signal) else 0
        app.db.run("insert into updown(ts, open, close, lean, p_lean, k, sigma, p5, p10, outcome) values(?,?,?,?,?,?,?,?,?,?)",
                   (ts, 80000, 80050 if won else 79950, 0, 0.5, 1.0, 0.0006, 0.8, 0.9, won))
        app.db.run("insert into updown_quotes(ts, venue, strike, m5_up, m5_dn, m10_up, m10_dn) values(?,?,?,?,?,?,?)",
                   (ts, "polymarket", 80000, 62, 40, 60, 42))


def test_the_signal_is_only_called_proven_when_it_really_paid():
    app, _ = _app_with_quotes()
    _fill_history(app, 60, 0.85)                                  # it won 85% of the time at a 60¢ price
    rec = app.updown.paper_record(30, START)["polymarket"]
    assert rec["signal"]["n"] == 60 and rec["signal"]["avg_c"] > 15 and rec["proven"]
    app2, _ = _app_with_quotes()
    _fill_history(app2, 60, 0.40)                                 # it won 40% at 60¢: it loses money
    rec2 = app2.updown.paper_record(30, START)["polymarket"]
    assert rec2["signal"]["avg_c"] < 0 and not rec2["proven"]
    app3, _ = _app_with_quotes()
    _fill_history(app3, 12, 1.0)                                  # a perfect streak, too short to trust
    assert not app3.updown.paper_record(30, START)["polymarket"]["proven"]


def _lead(app, pct=0.004, from_ts=START):
    """BTC jumps `pct` right at the window start and holds: a clear lead the model should price at ~95% Up."""
    bars = []
    for b in app.fake.crypto_minute["BTC"]:
        f = 1 + pct if b[0] >= from_ts else 1.0
        bars.append((b[0], b[1] * f, b[2] * f, b[3] * f, b[4] * f, b[5]))
    app.fake.crypto_minute["BTC"] = bars


def test_an_edge_message_only_comes_when_the_signal_is_proven_and_only_once_per_window():
    for wins, expect in ((0.85, 1), (0.40, 0)):
        app, _ = _app_with_quotes(up=45.0, dn=58.0)
        _lead(app)
        _fill_history(app, 60, wins)
        _walk(app, START + 20, START + 660)
        card = app.updown.card(START + 600)
        assert card["p_up"] > 0.85 and card["quotes"][0]["edge"] >= 4              # the cheap side is clearly Up
        notes = [m for m in app.notes.sent if m["kind"] == "BTC15"]
        assert len(notes) == expect, (wins, len(notes))
        if notes:
            assert "Polymarket: Up is" in notes[0]["text"] and "cheap" in notes[0]["text"] and "paper record" in notes[0]["text"]
        _walk(app, START + 680, START + 780)
        assert len([m for m in app.notes.sent if m["kind"] == "BTC15"]) == expect      # never twice for one window


def test_an_unreachable_market_is_just_no_quote_and_manual_prices_still_work():
    app = make_app(now=START)
    app.fake.crypto_minute = {"BTC": btc_minutes(START - 61 * 86400, 61 * 1440 + 400, seed=9)}
    app.fake.session = object()                                    # no real session: every fetch raises

    async def boom(session, arg):
        raise RuntimeError("Polymarket 503")
    app.updown.fetchers = {"polymarket": boom, "kalshi": boom}
    _walk(app, START + 20, START + 400, step=60)
    assert app.updown.quote_errors.get("polymarket") and not app.updown.live_quotes(START + 400)
    text = app.updown.text(START + 400, "62")
    assert "Your price: Up 62¢" in text and "no prices read yet" in text
    assert app.updown.summary(START + 400)["quote_errors"]["polymarket"].startswith("RuntimeError")
