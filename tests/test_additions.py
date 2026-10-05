import time

from fomo import copies
from tests.helpers import ME, MINT, WHALE, pump_buy


def test_alert_shows_the_whales_last_calls(bot):
    bot.whales.add(WHALE, "Rocket")
    now = int(time.time())
    for i, x in enumerate([2.0, 0.6, 1.5]):
        cid = copies.open_copy(bot.db, whale=WHALE, mint=f"Past{i}" + "1" * 39, swap_id=0, alert_id=0, price=1.0,
                               mc_usd=1e5, whale_price=1.0, ts=now - 9000 + i)
        copies.sell(bot.db, bot.db.row("select * from copies where id=?", (cid,)), 1.0, x, 0.0, "whale exited", now - 100 + i)
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy())
    assert "Last 3 calls: 🟩+50% 🟥-40% 🟩+100%" in bot.notes.sent[0]["text"]


def test_whale_adding_more_alerts_only_if_you_hold(bot_with_wallet):
    bot = bot_with_wallet
    bot.whales.add(WHALE, "Rocket")
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy())
    bot.feed(WHALE, pump_buy(new=False, pre_tokens=3_000_000))
    assert bot.notes.kinds() == ["BUY"]                       # you don't hold it: no add alert
    bot.feed(ME, pump_buy(wallet=ME, sol=0.2, tokens=300_000))
    bot.feed(WHALE, pump_buy(new=False, pre_tokens=6_000_000))
    assert bot.notes.kinds() == ["BUY", "ADD"] and "added $225.00 more" in bot.notes.sent[-1]["text"]
    bot.feed(WHALE, pump_buy(new=False, pre_tokens=9_000_000))
    assert bot.notes.kinds() == ["BUY", "ADD"]                # 30-minute cooldown


def test_watch_targets_up_and_down(bot):
    from tests.test_commands import FakeTelegram
    from fomo.commands import Commands
    bot.telegram = FakeTelegram()
    bot.started = time.time() - 60
    cmds = Commands(bot)
    bot.market.set_pair(mint=MINT, price=0.001, mc=1_000_000)

    def say(text):
        bot.run(cmds.handle_update({"message": {"chat": {"id": 12345}, "text": text}}))
        return bot.telegram.out[-1]["text"]

    assert "reaches $2M" in say(f"/watch {MINT} 2x")
    assert "drops to $500K" in say(f"/watch {MINT} 500k")
    assert "→ $2M" in say("/watches")
    now = int(time.time())
    bot.market.set_pair(mint=MINT, price=0.0021, mc=2_100_000)
    bot.run(bot.tracker.tick(now))
    assert bot.notes.kinds() == ["WATCH"] and "hit your $2M target" in bot.notes.sent[-1]["text"]
    bot.market.set_pair(mint=MINT, price=0.00045, mc=450_000)
    bot.run(bot.tracker.tick(now + 20))
    assert bot.notes.kinds() == ["WATCH", "WATCH"]
    bot.run(bot.tracker.tick(now + 40))
    assert bot.notes.kinds() == ["WATCH", "WATCH"]           # each target fires once


def test_wallets_buying_together_count_as_one_whale(bot):
    from tests.helpers import WHALE2, MINT2
    bot.whales.add(WHALE, "A")
    bot.whales.add(WHALE2, "B")
    now = int(time.time())
    for mint in (MINT2, "Past1" + "1" * 39):            # bought the same two coins seconds apart before
        for w in (WHALE, WHALE2):
            bot.db.run("insert into swaps(sig,wallet,ts,side,mint,usd_value) values(?,?,?,?,?,?)",
                       (f"{w[:4]}{mint[:6]}", w, now - 5000, "BUY", mint, 500))
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy(wallet=WHALE))
    bot.feed(WHALE2, pump_buy(wallet=WHALE2))
    assert "2ND WHALE IN" not in bot.notes.sent[-1]["text"]
    assert bot.db.row("select confluence from alerts order by id desc limit 1")["confluence"] == 1


def test_fast_flipper_is_flagged(bot):
    bot.whales.add(WHALE, "Rocket")
    now = int(time.time())
    for i in range(5):
        m = f"Flip{i}" + "1" * 39
        bot.db.run("insert into swaps(sig,wallet,ts,side,mint) values(?,?,?,?,?)", (f"b{i}", WHALE, now - 9000 + i, "BUY", m))
        bot.db.run("insert into swaps(sig,wallet,ts,side,mint) values(?,?,?,?,?)", (f"s{i}", WHALE, now - 8940 + i, "SELL", m))
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy())
    assert "Fast flipper: usually starts selling ~1m after buying (100% of the time within 2m)" in bot.notes.sent[0]["text"]


def test_rebuy_after_whales_left_is_flagged(bot_with_wallet):
    from tests.helpers import pump_sell
    bot = bot_with_wallet
    bot.whales.add(WHALE, "Rocket")
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy())
    bot.feed(ME, pump_buy(wallet=ME, sol=0.2, tokens=300_000))
    assert "REBUY" not in bot.notes.kinds()                   # whale still holds: fine
    bot.feed(WHALE, pump_sell(sol=1.0, tokens=3_000_000, holding=3_000_000, close=True))
    bot.feed(ME, pump_buy(wallet=ME, sol=0.2, tokens=300_000, new=False, pre_tokens=300_000))
    assert bot.notes.kinds()[-1] == "REBUY" and "has already sold" in bot.notes.sent[-1]["text"]
