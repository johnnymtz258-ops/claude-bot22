"""Accurate positions (dead coins, sales the bot didn't see) and the trading report."""
import time

import pytest

from tests.helpers import ME, MINT, pump_buy, pump_sell


def held(bot, mint=MINT, price=0.000075, sol=0.2, tokens=400_000):
    bot.market.set_pair(mint=mint, price=price, mc=price * 1e9)
    bot.feed(ME, pump_buy(wallet=ME, mint=mint, sol=sol, tokens=tokens))


def test_dead_coins_are_written_off_not_positions(bot_with_wallet):
    bot = bot_with_wallet
    held(bot)                                                   # $30 position
    bot.market.set_pair(mint=MINT, price=0.000002, mc=2_000)    # coin died: worth $0.80 now
    bot.run(bot.market.tokens([MINT], max_age=0))
    p = bot.portfolio.position(MINT)
    assert p["written_off"] and not p["open"] and bot.portfolio.open_positions() == []
    s = bot.portfolio.summary()
    assert s["closed"] == 1 and s["win_rate"] == 0 and s["realized"] == pytest.approx(0.8 - 30.0, abs=0.05)   # cost includes the swap fee
    t = int(time.time())
    for dt in (0, 30, 60):
        bot.run(bot.tracker.tick(t + dt))
    assert "STOP" not in bot.notes.kinds()                     # no "sell it" nagging about a dead coin


def test_sale_the_bot_missed_is_closed_without_a_fake_loss(bot_with_wallet):
    bot = bot_with_wallet
    held(bot)
    bot.rpc.balances[(ME, MINT)] = 0.0                          # you sold it somewhere the bot didn't see
    fixed = bot.run(bot.portfolio.reconcile(bot.rpc, [ME]))
    assert fixed == ["CASHED"] and bot.portfolio.open_positions() == []
    s = bot.portfolio.summary()
    assert s["realized"] == pytest.approx(0.0) and s["closed"] == 0
    assert bot.portfolio.pnl_curve() == []


def test_reconcile_keeps_what_you_still_hold(bot_with_wallet):
    bot = bot_with_wallet
    held(bot)
    bot.rpc.balances[(ME, MINT)] = 400_000.0
    assert bot.run(bot.portfolio.reconcile(bot.rpc, [ME])) == []
    assert len(bot.portfolio.open_positions()) == 1
    bot.feed(ME, pump_sell(wallet=ME, sol=0.3, tokens=400_000, holding=400_000, close=True))
    assert bot.portfolio.open_positions() == []


ROBIN_PAIR = "0xa8270528100cf1b8935e10400644ebe2d5737c3983e12bddfb8a162a218315f4"
ROBIN_TOKEN = "0x1111111111111111111111111111111111111111"
ROBIN_URL = f"https://dexscreener.com/robinhood/{ROBIN_PAIR}"


def robin_pair(price, mcap):
    return {"chainId": "robinhood", "dexId": "uniswap", "pairAddress": ROBIN_PAIR, "url": ROBIN_URL,
            "baseToken": {"address": ROBIN_TOKEN, "symbol": "HOOD", "name": "Hood"}, "priceUsd": str(price),
            "marketCap": mcap, "fdv": mcap, "liquidity": {"usd": 200_000}, "txns": {}, "priceChange": {}, "info": {}}


def test_links_and_other_chains_are_read_correctly():
    from fomo.util import parse_coin_ref
    assert parse_coin_ref(ROBIN_URL) == ("robinhood", ROBIN_PAIR)          # not a Solana-looking slice of the hex
    assert parse_coin_ref(ROBIN_TOKEN) == ("", ROBIN_TOKEN)
    assert parse_coin_ref(f"https://gmgn.ai/base/token/{ROBIN_TOKEN}") == ("base", ROBIN_TOKEN)
    sol = "DvNcJZTiSMD1RBCtZ2J31mh7s42CVCwrzGapv1Lypump"
    assert parse_coin_ref(f"https://dexscreener.com/solana/{sol}") == ("solana", sol)
    assert parse_coin_ref(sol) == ("solana", sol)


def test_track_a_robinhood_chain_coin_from_its_dexscreener_link(bot):
    bot.market.other_pairs["robinhood"] = [robin_pair(0.003006, 3_006_000)]
    ok, text = bot.run(bot.engine.coins.add(ROBIN_URL, entry_mc=3_006_000))
    assert ok and "$HOOD (robinhood) from your entry $3M" in text
    coin = bot.db.row("select * from coins")
    assert coin["mint"] == ROBIN_TOKEN and coin["chain"] == "robinhood" and coin["pair"] == ROBIN_PAIR
    bot.market.other_pairs["robinhood"] = [robin_pair(0.0065, 6_500_000)]
    bot.run(bot.tracker.tick(int(time.time())))
    assert bot.notes.kinds() == ["COIN"] and "📈 $HOOD is 2.2x (your entry)" in bot.notes.sent[-1]["text"]


def test_add_command_and_dashboard_take_the_link(chat):
    chat.market.other_pairs["robinhood"] = [robin_pair(0.003006, 3_006_000)]
    assert "Tracking $HOOD (robinhood) from your entry $3M" in chat.say(f"/add {ROBIN_URL} 3,006,000")


def test_alert_gets_a_one_hour_report_card(bot):
    from tests.helpers import WHALE
    bot.whales.add(WHALE, "Holder")
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy())
    alert_msg = bot.notes.sent[0]["id"]
    t0 = bot.db.scalar("select ts from alerts where kind='BUY'")
    for dt, price in ((300, 0.00016), (3700, 0.00012)):        # topped at 2.1x, 1.6x an hour later
        bot.market.set_pair(mint=MINT, price=price, mc=price * 1e9)
        bot.run(bot.tracker.tick(t0 + dt))
    reports = [m for m in bot.notes.sent if m["kind"] == "REPORT"]
    assert len(reports) == 1 and reports[0]["reply_to"] == alert_msg
    assert "1.6x from the alert (top 2.1x) — the plan sold half at 2x" in reports[0]["text"]
    bot.run(bot.tracker.tick(t0 + 3800))
    assert len([m for m in bot.notes.sent if m["kind"] == "REPORT"]) == 1


def test_trading_report(bot_with_wallet):
    from fomo import journal
    bot = bot_with_wallet
    for i in range(6):                                          # six quick small losers you picked yourself
        mint = f"Lose{i}" + "1" * 39
        held(bot, mint=mint)
        bot.feed(ME, pump_sell(wallet=ME, mint=mint, sol=0.1, tokens=400_000, holding=400_000, close=True))
    r = journal.report(bot.db, bot.portfolio, 30)
    assert r["n"] == 6 and r["won"] == 0 and r["total"] < 0
    assert r["lessons"][0].startswith("Biggest leak — your own pick")


def test_quiet_update_says_what_was_seen_and_skipped(bot):
    from tests.helpers import WHALE
    bot.cfg.set("QUIET_UPDATE_MINUTES", "15")
    bot.whales.add(WHALE, "Rocket")
    t = int(time.time())
    assert bot.run(bot.tracker.quiet_update(t)) is False          # first call just starts the clock
    bot.market.set_pair(mint=MINT, price=0.000075 * 1.8, mc=135_000)
    bot.feed(WHALE, pump_buy())                                     # ran 80% before the confirm: skipped
    assert bot.run(bot.tracker.quiet_update(t + 60)) is False
    assert bot.run(bot.tracker.quiet_update(t + 16 * 60)) is True
    msg = bot.notes.sent[-1]
    assert msg["kind"] == "QUIET" and not msg["silent"]           # comes with a normal notification
    assert "skipped $CASHED from Rocket: already ran (+80% already)" in msg["text"]
    assert bot.run(bot.tracker.quiet_update(t + 17 * 60)) is False


def test_old_alerts_never_get_a_report_card(bot):
    t = int(time.time())
    bot.db.run("""insert into alerts(ts,kind,mint,wallet,grade,price_usd,p1h,peak_price,status,tg_message_id)
        values(?,?,?,?,?,?,?,?,?,?)""", (t - 3 * 86400, "BUY", MINT, "w", "A", 1.0, 2.0, 3.0, "sent", 55))
    assert bot.run(bot.tracker._alert_reports()) == 0 and bot.notes.sent == []
