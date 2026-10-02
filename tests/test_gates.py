"""Whale buys are judged at the price you could get ~45s later, not at the whale's fill."""
import asyncio

from tests.helpers import MINT, WHALE, pump_buy

WHALE_PRICE = 0.000075  # pump_buy(): $225 for 3M tokens


def test_alert_waits_for_the_confirm_price(bot):
    bot.cfg.set("CONFIRM_SECONDS", "1")
    bot.whales.add(WHALE, "Rocket")
    bot.market.set_pair(mint=MINT, price=WHALE_PRICE * 1.4, mc=105_000)   # copy bots spike it first
    bot.feed(WHALE, pump_buy())
    assert bot.notes.sent == [] and bot.db.scalar("select count(*) from alerts") == 0
    bot.feed(WHALE, pump_buy(new=False, pre_tokens=3_000_000))          # an add while waiting: no second alert
    bot.market.set_pair(mint=MINT, price=WHALE_PRICE * 1.1, mc=82_500)   # settles a bit later
    bot.run(asyncio.sleep(1.3))
    assert bot.notes.kinds() == ["BUY"] and "+10% since whale" in bot.notes.sent[0]["text"]
    assert bot.db.scalar("select round(entry_price/whale_price,2) from copies") == 1.1


def test_coin_that_already_ran_is_not_sent(bot):
    bot.whales.add(WHALE, "Rocket")
    bot.market.set_pair(mint=MINT, price=WHALE_PRICE * 1.6, mc=120_000)
    bot.feed(WHALE, pump_buy())
    assert bot.notes.sent == []
    assert bot.db.scalar("select status from alerts where kind='BUY'") == "chased"
    assert bot.db.scalar("select count(*) from copies") == 1                 # still scored, at the price you'd pay


def test_coin_already_dumping_is_not_sent(bot):
    bot.whales.add(WHALE, "Rocket")
    bot.market.set_pair(mint=MINT, price=WHALE_PRICE * 0.7, mc=52_500)
    bot.feed(WHALE, pump_buy())
    assert bot.notes.sent == []
    assert bot.db.scalar("select status from alerts where kind='BUY'") == "dumping"


def test_gates_report_in_the_funnel(bot):
    from fomo.reports import FUNNEL_LABELS
    assert {"chased", "dumping", "micro"} <= set(FUNNEL_LABELS)
