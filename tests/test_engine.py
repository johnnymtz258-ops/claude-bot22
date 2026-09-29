import time

import pytest

from fomo.engine import grade_buy
from tests.helpers import ME, MINT, WHALE, WHALE2, pump_buy, pump_sell

# 1.5 SOL at $150 = $225 for 3M tokens -> $0.000075/token; 1B supply -> $75K market cap at entry
ENTRY_PRICE = 0.000075


def setup_coin(bot, price=ENTRY_PRICE, liq=40_000.0):
    bot.market.set_pair(mint=MINT, symbol="CASHED", price=price, mc=price * 1e9, liq=liq)


def test_whale_buy_sends_graded_alert_and_opens_copy(bot):
    bot.whales.add(WHALE, "Rocket")
    setup_coin(bot)
    bot.feed(WHALE, pump_buy(sol=1.5, tokens=3_000_000))
    assert bot.notes.kinds() == ["BUY"]
    text = bot.notes.sent[0]["text"]
    assert "WHALE BUY" in text and "$CASHED" in text and "Rocket" in text
    assert "$75K</b> MC" in text            # whale's entry market cap from the swap itself
    assert "Mint &amp; freeze authority revoked" in text and "0s ago" in text
    swap = bot.db.row("select * from swaps")
    assert swap["usd_value"] == pytest.approx(225) and swap["mc_usd"] == pytest.approx(75_000)
    copy = bot.db.row("select * from copies")
    assert copy["status"] == "open" and copy["entry_price"] == pytest.approx(ENTRY_PRICE)
    alert = bot.db.row("select * from alerts")
    assert alert["grade"] in {"A", "B"}


def test_dust_buy_is_ignored(bot):
    bot.whales.add(WHALE, "Rocket")
    setup_coin(bot)
    bot.feed(WHALE, pump_buy(sol=0.3, tokens=600_000))  # $45 < $100 minimum
    assert bot.notes.sent == [] and bot.db.scalar("select count(*) from copies") == 0


def test_muted_whale_is_still_scored_but_silent(bot):
    bot.whales.add(WHALE, "Rocket")
    bot.whales.set_muted(WHALE, True)
    setup_coin(bot)
    bot.feed(WHALE, pump_buy())
    assert bot.notes.sent == []
    assert bot.db.scalar("select count(*) from copies") == 1


def test_second_whale_is_a_confluence_alert(bot):
    bot.whales.add(WHALE, "Rocket")
    bot.whales.add(WHALE2, "Dino")
    setup_coin(bot)
    bot.feed(WHALE, pump_buy(wallet=WHALE))
    bot.feed(WHALE2, pump_buy(wallet=WHALE2, sol=3.0, tokens=6_000_000))
    assert bot.notes.kinds() == ["BUY", "BUY"]
    assert "2ND WHALE IN" in bot.notes.sent[1]["text"]
    assert "Rocket @ $75K" in bot.notes.sent[1]["text"]
    assert bot.db.row("select confluence from alerts order by id desc limit 1")["confluence"] == 2


def test_same_whale_adding_does_not_re_alert(bot):
    bot.whales.add(WHALE, "Rocket")
    setup_coin(bot)
    bot.feed(WHALE, pump_buy())
    bot.feed(WHALE, pump_buy(new=False, pre_tokens=3_000_000))
    assert bot.notes.kinds() == ["BUY"]


def test_freeze_authority_coin_is_skipped(bot):
    bot.whales.add(WHALE, "Rocket")
    setup_coin(bot)
    bot.rpc.mints[MINT] = {"mint_authority": "", "freeze_authority": "Dev1111111111111111111111111111111",
                           "decimals": 6, "supply": 1e9, "program": ""}
    bot.feed(WHALE, pump_buy())
    assert bot.notes.sent == [] and bot.db.scalar("select count(*) from copies") == 0
    assert bot.db.row("select grade from alerts")["grade"] == "SKIP"


def test_late_trade_is_recorded_not_alerted_or_scored(bot):
    bot.whales.add(WHALE, "Rocket")
    setup_coin(bot)
    bot.feed(WHALE, pump_buy(block_time=int(time.time()) - 1800), source="poll")
    assert bot.notes.sent == []
    assert bot.db.scalar("select count(*) from swaps") == 1
    assert bot.db.scalar("select count(*) from copies") == 0   # you couldn't have copied it


def test_removed_whale_trades_are_ignored(bot):
    bot.whales.add(WHALE, "Rocket")
    bot.whales.remove(WHALE)
    setup_coin(bot)
    bot.feed(WHALE, pump_buy())
    assert bot.notes.sent == [] and bot.db.scalar("select count(*) from swaps") == 0


def test_two_buys_arriving_together_alert_once(bot):
    import asyncio
    bot.whales.add(WHALE, "Rocket")
    setup_coin(bot)
    bot.rpc.txs["sigA" + "x" * 30] = pump_buy()
    bot.rpc.txs["sigB" + "x" * 30] = pump_buy(new=False, pre_tokens=3_000_000)
    bot.run(asyncio.gather(bot.engine.process(WHALE, "sigA" + "x" * 30), bot.engine.process(WHALE, "sigB" + "x" * 30)))
    assert bot.notes.kinds() == ["BUY"]


def test_whale_sells_move_the_copy_and_notify_when_relevant(bot):
    bot.whales.add(WHALE, "Rocket")
    setup_coin(bot)
    bot.feed(WHALE, pump_buy(sol=1.5, tokens=3_000_000))
    setup_coin(bot, price=ENTRY_PRICE * 3)  # coin ran 3x
    bot.feed(WHALE, pump_sell(sol=1.5 * 3 * 0.5, tokens=1_500_000, holding=3_000_000))
    assert bot.notes.kinds() == ["BUY", "SELL"]
    assert "WHALE SOLD 50%" in bot.notes.sent[1]["text"]
    assert "3.0x" in bot.notes.sent[1]["text"] and "still holds 50%" in bot.notes.sent[1]["text"]
    copy = bot.db.row("select * from copies")
    assert copy["remaining"] == pytest.approx(0.5) and copy["status"] == "open"
    assert copy["peak_price"] == pytest.approx(ENTRY_PRICE * 3)   # the sale price counts as a real reading
    bot.feed(WHALE, pump_sell(sol=2.25, tokens=1_500_000, holding=1_500_000, close=True))
    assert bot.notes.kinds()[-1] == "SELL" and "WHALE EXITED" in bot.notes.sent[-1]["text"]
    copy = bot.db.row("select * from copies")
    assert copy["status"] == "closed"
    # bought at alert price, sold both halves at 3x, 1% fee per side
    assert copy["return_pct"] == pytest.approx((3 * 0.99 * 0.99 - 1) * 100, rel=1e-6)


def test_small_trim_is_silent(bot):
    bot.whales.add(WHALE, "Rocket")
    setup_coin(bot)
    bot.feed(WHALE, pump_buy(tokens=3_000_000))
    bot.feed(WHALE, pump_sell(sol=0.2, tokens=300_000, holding=3_000_000))  # 10% trim
    assert bot.notes.kinds() == ["BUY"]
    assert bot.db.row("select remaining from copies")["remaining"] == pytest.approx(0.9)


def test_with_wallet_sync_sells_only_notify_for_coins_you_hold(bot_with_wallet):
    bot = bot_with_wallet
    bot.whales.add(WHALE, "Rocket")
    setup_coin(bot)
    bot.feed(WHALE, pump_buy())
    bot.feed(WHALE, pump_sell(sol=1.0, tokens=1_500_000, holding=3_000_000))
    assert bot.notes.kinds() == ["BUY"]            # you don't hold it: no sell message
    bot.feed(ME, pump_buy(wallet=ME, sol=0.2, tokens=400_000))
    bot.feed(WHALE, pump_sell(sol=1.0, tokens=1_500_000, holding=1_500_000, close=True))
    assert bot.notes.kinds() == ["BUY", "SELL"]
    assert "You:" in bot.notes.sent[-1]["text"]


def test_my_wallet_trades_are_exact(bot_with_wallet):
    bot = bot_with_wallet
    setup_coin(bot)
    bot.feed(ME, pump_buy(wallet=ME, sol=0.2, tokens=400_000, fee=5000))
    p = bot.portfolio.position(MINT)
    # 0.2 SOL at $150 = $30 + $0.00075 network fee; the 0.002 SOL deposit is NOT a cost
    assert p["cost"] == pytest.approx(30.00075)
    assert p["tokens"] == pytest.approx(400_000)
    assert bot.notes.sent == []


def test_grade_rules():
    base = dict(stats={"avg": 0, "n": 0}, confluence=1, chase=5.0, safety={"ok": True}, rug=None,
                liquidity=50_000, usd_value=500, usual_usd=0, late_chase_pct=50)
    assert grade_buy(status="NEW", **base)[0] == "B"
    assert grade_buy(status="HOT", **{**base, "stats": {"avg": 40, "n": 12}})[0] == "A"
    assert grade_buy(status="NEW", **{**base, "confluence": 2})[0] == "B"
    assert grade_buy(status="OK", **{**base, "confluence": 2})[0] == "A"
    assert grade_buy(status="NEW", **{**base, "chase": 80.0})[0] == "C"
    assert grade_buy(status="COLD", **base)[0] == "C"
    g, reasons = grade_buy(status="NEW", **{**base, "safety": {"ok": True, "freeze_authority": "x"}})
    assert g == "SKIP"
    g, reasons = grade_buy(status="NEW", **{**base, "usual_usd": 100})
    assert g == "B" and any("Big buy" in r for _, r in reasons)
