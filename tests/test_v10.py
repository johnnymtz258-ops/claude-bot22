"""v10: gap warning, whale profiles, copy-score gate, paper autopilot, live status card."""
import time


def test_sleep_gap_is_reported_once(bot):
    t = time.time()
    assert bot.run(bot.tracker.check_gap(t)) == 0
    bot.run(bot.tracker.check_gap(t + 15))
    assert bot.notes.sent == []
    bot.run(bot.tracker.check_gap(t + 15 + 3 * 3600))
    assert bot.notes.kinds() == ["GAP"] and "paused for 3h" in bot.notes.sent[0]["text"]
    bot.run(bot.tracker.check_gap(t + 30 + 3 * 3600))
    assert bot.notes.kinds() == ["GAP"]


# -- whale profiles ------------------------------------------------------------------------------
from fomo import profiles  # noqa: E402
from tests.helpers import MINT, WHALE, pump_buy, pump_sell  # noqa: E402


def test_style_from_how_fast_they_first_sell():
    assert profiles.style_of([200, 250, 400]) == "NEW"
    assert profiles.style_of([20, 30, 45, 50]) == "BOT"
    assert profiles.style_of([120, 200, 260, 900, 4000]) == "FLIPPER"          # usually out in ~4m
    assert profiles.style_of([200, 250, 3000, 4000, 5000]) == "FLIPPER"        # 40% within 5 minutes
    assert profiles.style_of([1800, 3600, 5400, 2400]) == "SWING"
    assert profiles.style_of([9000, 20000, 40000, 7300]) == "HOLDER"


def path(points, start=1000, step=60):
    return [(start + i * step, p) for i, p in enumerate(points)]


def test_copy_sim_buys_late_and_follows_the_plan():
    # the whale buys at t=1000 at 1.0; copy bots spike it to 1.8 in the first minute, then it dumps
    pump_dump = path([1.0, 1.8, 1.3, 0.9, 0.7, 0.5])
    assert profiles.simulate_copy(pump_dump, 1000, None) < 0.6               # you bought 1.8, stopped at -40%+
    runner = path([1.0, 1.05, 1.3, 1.7, 2.2, 2.6, 3.1, 2.4, 1.9])
    x = profiles.simulate_copy(runner, 1000, None)
    # entry 1.05; half at 2.2 (2.10x); rest trailed out at 1.9 (35% off the 3.1 top -> 1.81x)
    assert abs(x - (0.5 * 2.2 / 1.05 + 0.5 * 1.9 / 1.05) * 0.99 / 1.01) < 1e-9
    held = path([1.0, 1.0, 1.1, 1.2, 1.4, 1.3, 1.2])
    # the whale is out at t=1240; you react 90s later and sell at the next price (1.2)
    assert abs(profiles.simulate_copy(held, 1000, 1000 + 4 * 60) - 1.2 * 0.99 / 1.01) < 1e-9
    assert profiles.simulate_copy([(1000, 1.0), (1600, 2.0)], 1000, None) is None             # no price near entry


def test_verdict_blocks_flippers_and_losers_only():
    base = {"style": "SWING", "median_hold_s": 3000, "within5m": 0.1, "copy_n": 8, "copy_avg": 1.2,
            "copy_win": 0.5, "closed": 10, "own_pnl_usd": 900, "own_win": 0.6}
    assert profiles.verdict(base)[0]
    assert not profiles.verdict({**base, "style": "FLIPPER", "median_hold_s": 240, "within5m": 0.53})[0]
    assert not profiles.verdict({**base, "own_pnl_usd": -12000, "own_win": 0.27})[0]
    assert not profiles.verdict({**base, "copy_avg": 0.8})[0]
    assert profiles.verdict({**base, "copy_n": 3, "copy_avg": 0.5})[0]          # too few coins to judge
    assert profiles.verdict(None)[0]


def seed_flips(bot, wallet, n=6, hold=120):
    t = int(time.time()) - 6 * 3600
    for i in range(n):
        mint = f"Flip{i}" + "1" * 39
        bot.feed(wallet, pump_buy(wallet=wallet, mint=mint, block_time=t + i * 600), source="backfill")
        bot.feed(wallet, pump_sell(wallet=wallet, mint=mint, tokens=3_000_000, holding=3_000_000, close=True,
                                   block_time=t + i * 600 + hold), source="backfill")


def test_flipper_buys_are_tracked_not_sent(bot):
    bot.whales.add(WHALE, "Flipper")
    seed_flips(bot, WHALE)
    prof = bot.engine.profiles.profile_local(WHALE)
    assert prof["style"] == "FLIPPER" and prof["trips"] == 6
    bot.notes.sent.clear()
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy())
    assert bot.notes.sent == []
    assert bot.db.scalar("select status from alerts where kind='BUY' and mint=?", (MINT,)) == "flipper"
    bot.cfg.set("BLOCK_FLIPPERS", "off")
    bot.feed(WHALE, pump_buy(mint="Other" + "1" * 39))
    assert bot.notes.kinds() == ["BUY"]


def test_holder_profile_shows_on_the_alert(bot):
    bot.whales.add(WHALE, "Holder")
    bot.engine.profiles.save(WHALE, {"style": "HOLDER", "trips": 12, "median_hold_s": 11 * 3600, "within5m": 0,
                                     "copy_n": 6, "copy_avg": 1.3, "copy_med": 1.1, "copy_win": 0.6, "closed": 8,
                                     "own_pnl_usd": 9000, "own_win": 0.6, "source": "recorded"})
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy())
    text = bot.notes.sent[0]["text"]
    assert "🟢 Holder · first sells ~11h after buying · copying at your speed: 1.30x avg, 60% won (6 coins)" in text


def test_gecko_candles_are_read_from_the_pool_that_covers_the_buy():
    import asyncio
    from fomo.market import Market
    m = Market(session=None, rpc=None, db=None)
    m.gecko_every = 0
    pool_a, pool_b = "A" * 43, "B" * 43
    calls = []

    async def fake_get(url, timeout=8.0):
        calls.append(url)
        if "/tokens/" in url:
            return {"data": [{"id": f"solana_{pool_a}", "attributes": {"address": pool_a}},
                             {"id": f"solana_{pool_b}", "attributes": {"address": pool_b}}]}
        if pool_a in url:   # migrated pool: only has candles from after the buy
            return {"data": {"attributes": {"ohlcv_list": [[5000, 1, 1, 1, 2.0, 9], [4000, 1, 1, 1, 1.5, 9]]}}}
        return {"data": {"attributes": {"ohlcv_list": [[1100, 1, 1, 1, 1.1, 9], [1040, 1, 1, 1, 1.0, 9]]}}}

    m._get = fake_get
    path = asyncio.new_event_loop().run_until_complete(m.price_path("M" * 43, 1000, 6000))
    assert path == [(1040, 1.0), (1100, 1.1)]                     # sorted, from the bonding-curve pool
    assert "token=" + "M" * 43 in calls[1] and "before_timestamp=6000" in calls[1]


# -- paper autopilot ------------------------------------------------------------------------------
import pytest  # noqa: E402

P0 = 0.000075  # the whale's price in pump_buy()


def tick(bot, price, t):
    bot.market.set_pair(mint=MINT, price=price, mc=price * 1e9)
    bot.run(bot.tracker.tick(t))


def paper_after_alert(bot):
    bot.cfg.set("PAPER_SLIPPAGE_PCT", "0")
    bot.whales.add(WHALE, "Holder")
    bot.market.set_pair(mint=MINT, price=P0, mc=75_000)
    bot.feed(WHALE, pump_buy())
    trade = bot.db.row("select * from paper_trades")
    assert trade and trade["size_usd"] == 50 and trade["entry_price"] == pytest.approx(P0)
    bot.notes.sent.clear()
    return trade


def test_paper_takes_half_at_2x_and_trails_the_rest(bot):
    paper_after_alert(bot)
    t = int(time.time())
    tick(bot, P0 * 2.1, t)
    tr = bot.db.row("select * from paper_trades")
    assert tr["half_taken"] == 1 and tr["remaining"] == pytest.approx(0.5) and tr["proceeds_usd"] == pytest.approx(52.5)
    assert bot.notes.kinds() == ["PAPER"] and "sold half" in bot.notes.sent[0]["text"]
    tick(bot, P0 * 3.0, t + 15)
    tick(bot, P0 * 1.9, t + 30)                                   # -37% from the 3x top
    tr = bot.db.row("select * from paper_trades")
    assert tr["status"] == "closed" and "fell 35%" in tr["close_reason"]
    assert tr["proceeds_usd"] == pytest.approx(52.5 + 25 * 1.9)
    s = bot.engine.paper.summary()
    assert s["equity"] == pytest.approx(1000 + 52.5 + 47.5 - 50) and s["won"] == 1


def test_paper_stops_out_and_ignores_a_one_tick_glitch(bot):
    paper_after_alert(bot)
    t = int(time.time())
    tick(bot, P0 * 20, t)                                         # a 20x spike seen once: ignored
    assert bot.db.row("select * from paper_trades")["half_taken"] == 0
    tick(bot, P0 * 0.55, t + 15)                                  # -45%
    tr = bot.db.row("select * from paper_trades")
    assert tr["status"] == "closed" and tr["close_reason"] == "stop -40%"
    assert bot.engine.paper.summary()["equity"] == pytest.approx(1000 - 50 + 50 * 0.55)


def test_paper_exits_when_the_whale_sells_half(bot):
    paper_after_alert(bot)
    t = int(time.time())
    tick(bot, P0 * 1.2, t)
    bot.feed(WHALE, pump_sell(tokens=2_000_000, holding=3_000_000))   # whale keeps 1/3 of its bag
    tick(bot, P0 * 1.2, t + 15)
    tr = bot.db.row("select * from paper_trades")
    assert tr["status"] == "closed" and tr["close_reason"] == "whale sold half its bag"


def test_paper_respects_max_open_and_slippage(bot):
    bot.cfg.set("PAPER_MAX_OPEN", "1")
    bot.whales.add(WHALE, "Holder")
    bot.market.set_pair(mint=MINT, price=P0, mc=75_000)
    bot.feed(WHALE, pump_buy())
    other = "Other" + "1" * 39
    bot.market.set_pair(mint=other, price=P0, mc=75_000)
    bot.feed(WHALE, pump_buy(mint=other))
    trades = bot.db.rows("select * from paper_trades")
    assert len(trades) == 1 and trades[0]["entry_price"] == pytest.approx(P0 * 1.03)
    bot.cfg.set("PAPER_TRADING", "off")
    assert bot.engine.paper.open(alert_id=0, mint="X" * 40, whale=WHALE, symbol="X", price=1, mc_usd=1) == 0


# -- tracked coins ---------------------------------------------------------------------------------
COIN = "DvNcJZ" + "1" * 34 + "pump"


def test_coin_followed_as_a_whale_becomes_a_tracked_coin(bot):
    import types
    from fomo.app import App
    ok, text = bot.whales.add(COIN, "oops")
    assert not ok and "coin address" in text
    bot.db.run("insert into whales(address,name,added_ts,source,active) values(?,?,?,?,1)", (COIN, "old", 0, "manual"))
    bot.whales.add(WHALE, "Real")
    bot.market.set_pair(mint=COIN, symbol="DVNC", price=0.0001, mc=100_000)
    app = types.SimpleNamespace(db=bot.db, engine=bot.engine, refresh_wallets=lambda: None)
    assert bot.run(App.move_coin_whales(app)) == ["DVNC"]
    assert bot.whales.get(COIN)["active"] == 0 and bot.whales.get(WHALE)["active"] == 1
    assert bot.engine.coins.is_tracked(COIN)


def test_tracked_coin_take_profit_messages(bot):
    bot.market.set_pair(mint=COIN, symbol="DVNC", price=0.0001, mc=100_000)
    ok, text = bot.run(bot.engine.coins.add(COIN, entry_mc=80_000))
    assert ok and "from your entry $80K" in text and "2x ($160K MC)" in text
    t = int(time.time())

    def at(mcap, dt):
        bot.market.set_pair(mint=COIN, symbol="DVNC", price=mcap / 1e9, mc=mcap)
        bot.run(bot.tracker.tick(t + dt))

    at(170_000, 0)                                                # 2.1x from your $80K entry
    assert bot.notes.kinds() == ["COIN"] and "📈 $DVNC is 2.1x (your entry)" in bot.notes.sent[-1]["text"]
    assert "Take half" in bot.notes.sent[-1]["text"]
    at(260_000, 70)                                               # 3.25x: next slice
    assert bot.notes.kinds() == ["COIN", "COIN"]
    at(150_000, 140)                                              # -42% from the top: first sighting
    at(150_000, 170)
    assert bot.notes.kinds() == ["COIN"] * 3 and "gave back 42%" in bot.notes.sent[-1]["text"]
    at(150_000, 200)
    assert bot.notes.kinds() == ["COIN"] * 3                      # each message once


def test_tracked_coin_stop_and_held_coins_are_not_nudged_twice(bot_with_wallet):
    from tests.helpers import ME
    bot = bot_with_wallet
    bot.market.set_pair(mint=COIN, symbol="DVNC", price=0.0001, mc=100_000)
    bot.run(bot.engine.coins.add(COIN))
    t = int(time.time())
    for dt in (0, 30):
        bot.market.set_pair(mint=COIN, symbol="DVNC", price=0.000055, mc=55_000)
        bot.run(bot.tracker.tick(t + dt))
    assert bot.notes.kinds() == ["COIN"] and "down 45% (when tracked)" in bot.notes.sent[-1]["text"]
    other = "Held" + "1" * 36 + "pump"
    bot.market.set_pair(mint=other, symbol="HELD", price=0.000075, mc=75_000)
    bot.feed(ME, pump_buy(wallet=ME, mint=other, sol=0.2, tokens=400_000))
    bot.run(bot.engine.coins.add(other))
    bot.notes.sent.clear()
    bot.market.set_pair(mint=other, symbol="HELD", price=0.0002, mc=200_000)
    bot.run(bot.tracker.tick(t + 60))
    assert "COIN" not in bot.notes.kinds()                         # your position already sends the ladder


def test_add_command_tracks_coins_and_follows_wallets(chat):
    chat.market.set_pair(mint=COIN, symbol="DVNC", price=0.0001, mc=100_000)
    reply = chat.say(f"/add {COIN} at 80k")
    assert "Tracking $DVNC from your entry $80K" in reply and chat.engine.coins.is_tracked(COIN)
    assert "$DVNC" in chat.say("/coins")
    assert "Stopped tracking $DVNC" in chat.say("/drop DVNC")
    assert not chat.engine.coins.is_tracked(COIN)
    reply = chat.say("/add GjJyeC1rB1p4d6k1Mzw5Y6vYGZyLr8N8zQJ7XU4yzF1G Newbie")
    assert "Following Newbie" in reply and not chat.engine.coins.is_tracked("GjJyeC1rB1p4d6k1Mzw5Y6vYGZyLr8N8zQJ7XU4yzF1G")
