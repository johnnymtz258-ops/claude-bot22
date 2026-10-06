"""Per-whale playbooks: a flipper whose coins run after it sells gets its own exit plan instead of a block."""
import time

from fomo import playbook
from tests.helpers import MINT, WHALE, pump_buy


def _copies(bot, whale, shapes):
    now = int(time.time()) - 2 * 86400
    for i, shape in enumerate(shapes):
        mint = f"Mint{i:02d}" + "1" * 36
        t0 = now + i * 3000
        bot.db.run("insert into copies(whale,mint,open_ts,entry_price,status) values(?,?,?,?,'closed')",
                   (whale, mint, t0, 1.0))
        for k, price in enumerate(shape):
            bot.db.run("insert into price_marks(mint,ts,price) values(?,?,?)", (mint, t0 + 60 * k, price))


SPIKE = [1.0, 1.3, 1.7, 2.1, 1.2, 0.4, 0.3]     # runs to 2x within minutes, then dumps (a flipper's coin)
DUD = [1.0, 0.9, 0.8, 0.6, 0.5]


def test_simulate_quick_plan_takes_2x_before_the_dump():
    assert round(playbook.simulate(1.0, list(enumerate(SPIKE)), playbook.PLANS["quick"]), 2) == 1.98
    assert round(playbook.simulate(1.0, list(enumerate(DUD)), playbook.PLANS["quick"]), 2) == 0.58


def test_flipper_with_a_winning_playbook_is_sent_with_its_plan(bot):
    _copies(bot, WHALE, [SPIKE] * 7 + [DUD] * 3)
    bot.whales.add(WHALE, "Flippy")
    bot.engine.profiles.save(WHALE, {"style": "FLIPPER", "trips": 40, "median_hold_s": 200, "within5m": 0.7,
                                     "copy_n": 10, "copy_avg": 0.8, "copy_win": 0.2})
    prof = bot.engine.profiles.profile_local(WHALE)
    assert prof["playbook"]["ok"] and prof["playbook"]["plan"] == "quick"
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy())
    assert bot.notes.kinds() == ["BUY"]
    text = bot.notes.sent[0]["text"]
    assert "Playbook for this whale" in text and "Exit plan for this whale: sell everything at 2x" in text


def test_paper_follows_the_whales_playbook(bot):
    _copies(bot, WHALE, [SPIKE] * 7 + [DUD] * 3)
    bot.whales.add(WHALE, "Flippy")
    bot.engine.profiles.profile_local(WHALE)
    bot.cfg.set("PAPER_SLIPPAGE_PCT", "0")
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy())
    bot.market.set_pair(mint=MINT, price=0.000075 * 2.05, mc=154_000)
    bot.run(bot.tracker.tick(int(time.time())))
    t = bot.db.row("select * from paper_trades")
    assert t["status"] == "closed" and "playbook" in t["close_reason"]


def test_whale_without_enough_copies_keeps_the_default_rules(bot):
    _copies(bot, WHALE, [SPIKE] * 3)
    assert playbook.evaluate(bot.db, WHALE)["ok"] is False
    assert playbook.plan_for(bot.db, WHALE) is None
