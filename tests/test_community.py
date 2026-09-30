import time

import pytest

from fomo.community import assess, holder_concentration, is_on_curve
from fomo.runners import prefilter
from tests.helpers import MINT, MINT2, ON_CURVE, POOL_PDA, WHALE, pump_buy

SUPPLY = 1_000_000_000


def spread_holders(bot, mint=MINT, top_share=0.015):
    """Pool holds 40% (excluded), 12 wallets hold `top_share` each."""
    bot.rpc.holders[mint] = [(POOL_PDA, 0.4 * SUPPLY)] + [(w, top_share * SUPPLY) for w in ON_CURVE]


def test_curve_check():
    assert all(is_on_curve(w) for w in ON_CURVE)
    assert not is_on_curve(POOL_PDA)


def test_pool_is_not_counted_as_a_holder(bot):
    spread_holders(bot)
    conc = bot.run(holder_concentration(bot.rpc, MINT, SUPPLY))
    assert conc["ok"] and conc["top10_pct"] == pytest.approx(15.0) and conc["top1_pct"] == pytest.approx(1.5)


def test_community_labels():
    busy = {"buys_h24": 3000, "sells_h24": 2500, "buys_h1": 200, "socials_count": 3}
    assert assess(busy, {"ok": True, "top10_pct": 18})["label"] == "STRONG"
    assert assess(busy, {"ok": True, "top10_pct": 70})["label"] == "WHALE-ONLY"
    assert assess({**busy, "buys_h24": 100, "sells_h24": 50}, {"ok": True, "top10_pct": 50})["label"] == "WHALE-ONLY"
    assert assess({**busy, "buys_h24": 100, "sells_h24": 50}, {"ok": True, "top10_pct": 20})["label"] == "THIN"
    assert assess(busy, {"ok": False})["label"] == "OK"  # unknown holders never makes it STRONG


def test_whale_only_coin_is_tracked_but_not_sent(bot):
    bot.whales.add(WHALE, "Rocket")
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000, trades_h24=120)
    bot.rpc.holders[MINT] = [(ON_CURVE[0], 0.5 * SUPPLY), (ON_CURVE[1], 0.2 * SUPPLY)]
    bot.feed(WHALE, pump_buy())
    assert bot.notes.sent == []
    assert bot.db.scalar("select count(*) from copies") == 1
    assert "WHALE-ONLY" in bot.db.row("select reasons from alerts")["reasons"]
    bot.cfg.set("HIDE_WHALE_ONLY", "off")
    bot.market.set_pair(mint=MINT2, price=0.000075, mc=75_000, trades_h24=120)
    bot.rpc.holders[MINT2] = bot.rpc.holders[MINT]
    bot.feed(WHALE, pump_buy(mint=MINT2))
    assert bot.notes.kinds() == ["BUY"] and "Community WHALE-ONLY" in bot.notes.sent[0]["text"]


def test_strong_community_lifts_the_grade(bot):
    bot.whales.add(WHALE, "Rocket")
    bot.market.set_pair(mint=MINT, price=0.0005, mc=500_000)
    spread_holders(bot)
    bot.feed(WHALE, pump_buy(sol=1.5, tokens=450_000))
    assert "Community STRONG" in bot.notes.sent[0]["text"]
    assert bot.db.row("select grade from alerts")["grade"] == "B"  # NEW whale (0) + strong community (+1)


# -- community runners -------------------------------------------------------------------------

def runner_coin(bot, mint=MINT, **over):
    bot.market.set_pair(mint=mint, symbol="RUN", price=0.0012, mc=1_200_000, liq=150_000, **over)
    bot.market.watchlist.append(mint)
    spread_holders(bot, mint)


def test_prefilter_explains_rejections(bot):
    now = time.time()
    good = {"mc_usd": 1e6, "liquidity_usd": 1e5, "buys_h1": 300, "sells_h1": 200, "buys_h24": 3000,
            "sells_h24": 2000, "pair_created_ts": now - 7200, "socials_count": 2, "change_h1": 30, "change_m5": 3}
    assert prefilter(good, bot.cfg, now) == (True, "")
    assert prefilter({**good, "socials_count": 0}, bot.cfg, now)[1] == "fewer than 2 social links"
    assert prefilter({**good, "change_h1": 400}, bot.cfg, now)[1] == "dumping or already parabolic"
    assert prefilter({**good, "liquidity_usd": -1}, bot.cfg, now)[1] == "liquidity too thin"


def test_runner_alert_once_with_its_own_record(bot):
    runner_coin(bot)
    assert bot.run(bot.runners.tick()) == [MINT]
    assert bot.notes.kinds() == ["RUNNER"] and "COMMUNITY RUNNER · $RUN" in bot.notes.sent[0]["text"]
    assert bot.db.row("select whale from copies")["whale"] == "runner"
    assert bot.run(bot.runners.tick()) == []          # 24h cooldown
    assert bot.whales.get("runner") is None           # not a followed wallet


def test_runner_skips_concentrated_coins_and_respects_hourly_cap(bot):
    runner_coin(bot, MINT)
    bot.rpc.holders[MINT] = [(ON_CURVE[0], 0.4 * SUPPLY)] + [(w, 0.01 * SUPPLY) for w in ON_CURVE[1:]]
    assert bot.run(bot.runners.tick()) == []          # top wallet holds 40%: not a community coin
    bot.cfg.set("RUNNER_MAX_PER_HOUR", 1)
    runner_coin(bot, MINT2)
    extra = ON_CURVE[5]  # any valid address works as a coin id for the fake market
    runner_coin(bot, extra)
    assert len(bot.run(bot.runners.tick())) == 1


def test_runner_record_is_separate_in_stats(bot):
    from fomo.reports import copy_report
    runner_coin(bot)
    bot.run(bot.runners.tick())
    r = copy_report(bot.db, bot.cfg)
    assert r["runners"]["n"] == 1 and r["all"]["n"] == 0


def test_early_coin_with_normal_concentration_is_alerted_loud(bot):
    bot.whales.add(WHALE, "Rocket")
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000, trades_h24=90, socials=0)
    bot.rpc.holders[MINT] = [(POOL_PDA, 0.6 * SUPPLY)] + [(w, 0.045 * SUPPLY) for w in ON_CURVE[:10]]  # top-10 = 45%
    bot.feed(WHALE, pump_buy())
    assert bot.notes.kinds() == ["BUY"] and not bot.notes.sent[0]["silent"]
    assert "Community EARLY" in bot.notes.sent[0]["text"]
    assert bot.db.row("select grade from alerts")["grade"] == "B"


def test_early_coin_with_one_giant_holder_is_still_whale_only():
    info = {"mc_usd": 50_000, "buys_h24": 40, "sells_h24": 10}
    assert assess(info, {"ok": True, "top10_pct": 40, "top1_pct": 30})["label"] == "WHALE-ONLY"
