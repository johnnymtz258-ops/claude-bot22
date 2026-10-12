"""A Mac that sleeps (lid closed) leaves trades unwatched: handle the wake-up, keep those trades out of the results."""
import time

from tests.helpers import MINT


def _hype_trade(bot, opened):
    bot.cfg.set("PAPER_SLIPPAGE_PCT", "0")
    return bot.engine.paper.open(alert_id=0, mint=MINT, whale="hype", symbol="RUG", price=1.0, mc_usd=60_000, ts=opened)


def test_a_crash_is_acted_on_at_once_not_held_as_a_glitch(bot):
    now = int(time.time())
    tid = _hype_trade(bot, now - 300)
    bot.run(bot.engine.paper.tick({MINT: {"price_usd": 0.05}}, now))      # -95% in one reading: a rug, not a glitch
    t = bot.db.row("select * from paper_trades where id=?", (tid,))
    assert t["status"] == "closed" and t["close_reason"].startswith("stop")


def test_time_limit_applies_even_without_a_fresh_price(bot):
    now = int(time.time())
    tid = _hype_trade(bot, now - 2 * 3600)
    bot.run(bot.engine.paper.tick({}, now))                                # no price for the coin at all
    t = bot.db.row("select * from paper_trades where id=?", (tid,))
    assert t["status"] == "closed" and "time limit" in t["close_reason"]


def test_trades_open_while_the_mac_slept_are_marked_and_kept_out_of_the_results(bot):
    now = int(time.time())
    tid = _hype_trade(bot, now - 4 * 3600)
    bot.db.set_meta("loop_heartbeat", now - 3 * 3600)                     # asleep for 3 hours
    bot.run(bot.tracker.check_gap(now))
    assert bot.db.scalar("select interrupted from paper_trades where id=?", (tid,)) == 1
    assert any("unwatched" in m["text"] for m in bot.notes.sent)
    bot.run(bot.engine.paper.tick({MINT: {"price_usd": 0.1}}, now))
    types = {t["kind"]: t for t in bot.engine.paper.by_type()}
    assert types["hype"]["n"] == 0 and types["offline"]["n"] == 1 and types["offline"]["pnl"] < 0
