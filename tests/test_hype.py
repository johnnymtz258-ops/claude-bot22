"""Hype scanner: coins at the start of a push, scored on what real attention does to a coin."""
import time

from fomo.hype import alertable, score
from tests.helpers import MINT, WHALE, WHALE2, pair, pump_buy
from fomo.market import pair_to_info

NOW = time.time()


def hot_pair(**over):
    """A young coin with a buy rush, buyers beating sellers and a volume surge."""
    base = dict(mint=MINT, price=0.0002, mc=200_000, liq=40_000, created_ms=int((NOW - 2 * 3600) * 1000),
                buys_h1=240, buys_m5=90, sells_m5=30, volume_m5=30_000, change_m5=12, change_h1=60)
    base.update(over)
    return base


def info_of(**over):
    return {**pair_to_info(pair(**hot_pair(**over))), "mint": MINT}


def test_a_coin_at_the_start_of_a_push_scores_high(bot):
    r = score(info_of(), bot.cfg, lists={"boosts": {MINT}}, now=NOW)
    keys = [k for k, _, _ in r["signals"]]
    assert {"rush", "pressure", "volume", "momentum", "boost", "early"} <= set(keys)
    assert r["score"] >= 60 and not r["blocked"] and alertable(r, bot.cfg)


def test_quiet_coins_and_unsafe_ones_are_not_alerted(bot):
    quiet = score(info_of(buys_m5=5, sells_m5=5, volume_m5=500, change_m5=0), bot.cfg, now=NOW)
    assert not alertable(quiet, bot.cfg)
    assert "late" in score(info_of(change_h1=225), bot.cfg, now=NOW)["blocked"]     # the $A1 alert at +225%: a chase
    assert "dumping" in score(info_of(change_m5=-25), bot.cfg, now=NOW)["blocked"]
    assert "liquidity" in score(info_of(liq=3_000), bot.cfg, now=NOW)["blocked"]
    assert "past early" in score(info_of(mc=40_000_000, liq=2_000_000), bot.cfg, now=NOW)["blocked"]


def test_scanner_sends_one_hype_alert_and_trades_it_on_paper(bot):
    bot.market.set_pair(**hot_pair())
    bot.market.boosted = [MINT]
    bot.run(bot.runners.tick(int(NOW)))
    assert bot.notes.kinds() == ["HYPE"]
    text = bot.notes.sent[0]["text"]
    assert "HYPE BUILDING" in text and "buy rush" in text and "Plan: take half at 2x" in text
    assert bot.db.scalar("select count(*) from paper_trades where whale='hype'") == 1
    assert bot.hype.board and bot.hype.board[0]["mint"] == MINT
    bot.run(bot.runners.tick(int(NOW) + 60))                    # same coin a minute later: no repeat
    assert bot.notes.kinds() == ["HYPE"]


def test_whale_buy_into_a_hyped_coin_says_so(bot):
    bot.whales.add(WHALE, "Rocket")
    bot.whales.add(WHALE2, "Moon")
    bot.market.set_pair(**hot_pair(price=0.000075, mc=75_000, liq=20_000))
    bot.feed(WHALE2, pump_buy(wallet=WHALE2))                   # first whale
    bot.feed(WHALE, pump_buy())                                  # second whale: confluence + hype
    texts = [m["text"] for m in bot.notes.sent if m["kind"] == "BUY"]
    assert any("WHALE + HYPE" in t and "Hype score" in t for t in texts)


def test_whale_alerts_without_hype_are_unchanged(bot):
    bot.whales.add(WHALE, "Rocket")
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy())
    assert bot.notes.kinds() == ["BUY"]
    assert "Hype score" not in bot.notes.sent[0]["text"] and "WHALE + HYPE" not in bot.notes.sent[0]["text"]


def test_second_leg_is_caught_while_buyers_load_before_the_price_moves(bot):
    """$A1: ran to $1.5M, fell to $340K, went quiet — then buys and volume picked up before the pump."""
    old = int((NOW - 30 * 3600) * 1000)
    bot.market.set_pair(**hot_pair(mc=1_500_000, liq=120_000, created_ms=old, change_h1=40))
    bot.market.trending = [MINT]
    bot.run(bot.runners.tick(int(NOW) - 20 * 3600))              # yesterday: hot, goes on the watch list
    bot.notes.sent.clear()
    bot.db.run("delete from alerts")
    bot.market.trending = []                                       # dropped off trending since
    bot.market.set_pair(mint=MINT, price=0.00034, mc=340_000, liq=60_000, created_ms=old, buys_h1=120,
                        buys_m5=30, sells_m5=12, volume_m5=25_000, change_m5=3, change_h1=5, boosts=1)
    bot.market._cache.clear()
    bot.run(bot.runners.tick(int(NOW)))
    assert bot.notes.kinds() == ["HYPE"]
    text = bot.notes.sent[0]["text"]
    assert "buyers loading before the price moves" in text and "second leg: peaked at $1.5M" in text
