import time

import pytest

from fomo import copies
from tests.helpers import ME, MINT, WHALE, WHALE2, pump_buy


def open_copy(bot, whale=WHALE, price=1.0, ts=None, mint=MINT):
    cid = copies.open_copy(bot.db, whale=whale, mint=mint, swap_id=1, alert_id=1, price=price, mc_usd=1e6,
                           whale_price=price, ts=ts)
    return bot.db.row("select * from copies where id=?", (cid,))


# -- copies: hold through the dip, record the path ------------------------------------------------

def test_copy_rides_the_dip_and_records_it(bot):
    t0 = int(time.time()) - 7200
    c = open_copy(bot, price=1.0, ts=t0)
    for dt, price in ((60, 0.8), (240, 0.6), (400, 0.9), (1000, 2.5), (4000, 1.9)):
        c = copies.update_path(bot.db, c, price, t0 + dt)
    c = bot.db.row("select * from copies where id=?", (c["id"],))
    assert c["status"] == "open"                         # no stop-loss sold the -40% dip
    assert c["low_price"] == pytest.approx(0.6)
    assert c["peak_price"] == pytest.approx(2.5)
    assert c["dip_before_peak_pct"] == pytest.approx(-40)
    assert c["hit_2x_ts"] == t0 + 1000
    assert c["p5m"] == pytest.approx(0.9) and c["p15m"] == pytest.approx(2.5) and c["p1h"] == pytest.approx(1.9)


def test_price_glitch_needs_confirmation(bot):
    t0 = int(time.time())
    c = open_copy(bot, price=1.0, ts=t0)
    c = copies.update_path(bot.db, c, 60.0, t0 + 10)       # feed glitch: ignored
    assert c["last_price"] == pytest.approx(1.0)
    c = copies.update_path(bot.db, c, 1.05, t0 + 20)       # normal reading clears it
    assert c["last_price"] == pytest.approx(1.05) and c["peak_price"] == pytest.approx(1.05)
    c = copies.update_path(bot.db, c, 12.0, t0 + 30)       # a real 10x run...
    assert c["last_price"] == pytest.approx(1.05)
    c = copies.update_path(bot.db, c, 12.5, t0 + 50)       # ...confirmed by the next reading
    assert c["last_price"] == pytest.approx(12.5)


def test_tracker_closes_copies_at_max_hold_and_on_rug(bot):
    now = int(time.time())
    old = open_copy(bot, price=1.0, ts=now - 80 * 3600)
    bot.market.set_pair(mint=MINT, price=1.3, mc=1.3e9)
    bot.run(bot.tracker.tick(now))
    old = bot.db.row("select * from copies where id=?", (old["id"],))
    assert old["status"] == "closed" and old["close_reason"] == "max hold reached"
    assert old["return_pct"] == pytest.approx((1.3 * 0.99 * 0.99 - 1) * 100)


def test_whale_status_from_measured_copies(bot):
    bot.whales.add(WHALE, "Rocket")
    now = int(time.time())
    for i in range(4):
        c = open_copy(bot, ts=now - 3600 * (i + 1), mint=f"Mint{i}" + "1" * 38)
        copies.sell(bot.db, c, 1.0, 1.6, 1.0, "whale exited", now)
    assert bot.whales.stats(WHALE, fresh=True)["status"] == "NEW"
    c = open_copy(bot, ts=now - 100, mint="Mint9" + "1" * 38)
    copies.sell(bot.db, c, 1.0, 1.6, 1.0, "whale exited", now)
    s = bot.whales.stats(WHALE, fresh=True)
    assert s["n"] == 5 and s["status"] == "HOT" and s["win_rate"] == 1.0


def test_losing_whale_is_auto_muted_and_told(bot):
    bot.whales.add(WHALE2, "Bagholder")
    now = int(time.time())
    for i in range(9):
        c = open_copy(bot, whale=WHALE2, ts=now - 3600 * (i + 1), mint=f"Loss{i}" + "1" * 38)
        copies.sell(bot.db, c, 1.0, 0.55, 1.0, "whale exited", now)
    assert bot.whales.stats(WHALE2, fresh=True)["status"] == "COLD"
    bot.run(bot.tracker._auto_mutes())
    assert bot.whales.get(WHALE2)["auto_muted"] == 1
    assert "Auto-muted" in bot.notes.sent[-1]["text"]
    bot.whales.set_muted(WHALE2, False)  # your choice sticks: the bot won't auto-mute them again
    bot.run(bot.tracker._auto_mutes())
    w = bot.whales.get(WHALE2)
    assert bot.whales.is_alerting(w) and w["auto_muted"] == -1


# -- your positions: no dip alarms, rug alarm only when liquidity is really pulled -------------

def hold_coin(bot):
    bot.market.set_pair(mint=MINT, price=0.001, mc=1e6, liq=100_000)
    bot.run(bot.portfolio.manual_buy(MINT, 50))


def test_dips_never_send_sell_messages(bot):
    hold_coin(bot)
    now = int(time.time())
    for i, (price, liq) in enumerate(((0.0007, 83_000), (0.0004, 63_000), (0.0003, 55_000), (0.0009, 95_000))):
        bot.market.set_pair(mint=MINT, price=price, mc=price * 1e9, liq=liq)
        bot.run(bot.tracker.tick(now + 30 * i))
    assert bot.notes.kinds() == ["STOP"]                 # only the one -40% stop warning, never per-dip alarms


def test_missing_liquidity_is_not_a_rug(bot):
    hold_coin(bot)
    now = int(time.time())
    bot.run(bot.tracker.tick(now))
    for i in range(1, 4):
        bot.market.set_pair(mint=MINT, price=0.001, mc=1e6, liq=None)  # API omitted liquidity
        bot.run(bot.tracker.tick(now + 30 * i))
    assert bot.notes.sent == []


def test_pulled_liquidity_alarms_once_after_confirmation(bot):
    hold_coin(bot)
    now = int(time.time())
    bot.run(bot.tracker.tick(now))
    bot.market.set_pair(mint=MINT, price=0.00095, mc=0.95e6, liq=4_000)
    bot.run(bot.tracker.tick(now + 15))
    assert bot.notes.sent == []                       # first sighting: wait for confirmation
    bot.run(bot.tracker.tick(now + 40))
    assert bot.notes.kinds() == ["RUG"] and "LIQUIDITY PULLED" in bot.notes.sent[0]["text"]
    bot.run(bot.tracker.tick(now + 80))
    assert bot.notes.kinds() == ["RUG"]               # not repeated


def test_take_initial_note_is_optional_and_once(bot):
    bot.cfg.set("PROFIT_LADDER", "off")
    hold_coin(bot)
    now = int(time.time())
    bot.market.set_pair(mint=MINT, price=0.0025, mc=2.5e6, liq=150_000)
    bot.run(bot.tracker.tick(now))
    assert bot.notes.sent == []                       # off by default
    bot.cfg.set("TAKE_INITIAL_AT_X", 2)
    bot.run(bot.tracker.tick(now + 20))
    bot.run(bot.tracker.tick(now + 40))
    assert bot.notes.kinds() == ["INFO"]


# -- profit/loss accuracy -------------------------------------------------------------------------

def test_manual_buy_at_typed_market_cap_matches_the_app(bot):
    bot.market.set_pair(mint=MINT, price=0.001, mc=1e6)
    r = bot.run(bot.portfolio.manual_buy(MINT, 20, mc_usd=850_000))
    assert r["price"] == pytest.approx(0.00085)
    p = bot.portfolio.position(MINT)
    assert p["value"] == pytest.approx(20 * 1e6 / 850_000)       # $23.53 — same as the app's MC ratio
    assert p["unrealized"] == pytest.approx(20 * 1e6 / 850_000 - 20)


def test_flat_coin_shows_zero_not_a_fee_loss(bot):
    bot.market.set_pair(mint=MINT, price=0.001, mc=1e6)
    bot.run(bot.portfolio.manual_buy(MINT, 20))
    assert bot.portfolio.position(MINT)["unrealized"] == pytest.approx(0.0)


def test_partial_and_full_manual_sells(bot):
    bot.market.set_pair(mint=MINT, price=0.001, mc=1e6)
    bot.run(bot.portfolio.manual_buy(MINT, 20))
    bot.market.set_pair(mint=MINT, price=0.002, mc=2e6)
    bot.run(bot.portfolio.manual_sell(MINT, usd=10))               # $10 at 2x = a quarter of the bag
    p = bot.portfolio.position(MINT)
    assert p["realized"] == pytest.approx(5.0) and p["tokens"] == pytest.approx(15_000)
    bot.run(bot.portfolio.manual_sell(MINT, fraction=1.0))
    p = bot.portfolio.position(MINT)
    assert p["realized"] == pytest.approx(20.0) and not p["open"]
    assert bot.portfolio.summary()["realized"] == pytest.approx(20.0)


def test_undo_removes_last_manual_entry(bot):
    bot.market.set_pair(mint=MINT, price=0.001, mc=1e6)
    bot.run(bot.portfolio.manual_buy(MINT, 20))
    bot.run(bot.portfolio.manual_buy(MINT, 5))
    bot.portfolio.undo_last_manual()
    assert bot.portfolio.position(MINT)["cost"] == pytest.approx(20)


def test_sell_without_a_recorded_buy_is_not_profit(bot_with_wallet):
    bot = bot_with_wallet
    from tests.helpers import pump_sell
    bot.market.set_pair(mint=MINT, price=0.001, mc=1e6)
    bot.feed(ME, pump_sell(wallet=ME, sol=1.0, tokens=500_000, holding=500_000, close=True))
    assert bot.portfolio.summary()["realized"] == 0.0
    assert bot.portfolio.position(MINT)["unmatched_sells"] == 1


def test_wallet_round_trip_is_exact(bot_with_wallet):
    bot = bot_with_wallet
    from tests.helpers import pump_sell
    bot.market.set_pair(mint=MINT, price=0.0001, mc=1e5)
    bot.feed(ME, pump_buy(wallet=ME, sol=0.2, tokens=300_000, fee=5000))
    bot.feed(ME, pump_sell(wallet=ME, sol=0.5, tokens=300_000, holding=300_000, close=True, fee=5000))
    p = bot.portfolio.position(MINT)
    fee_usd = 5000 / 1e9 * 150
    assert p["realized"] == pytest.approx(0.5 * 150 - fee_usd - (0.2 * 150 + fee_usd))
    assert not p["open"]
