"""Scalp setups: micro-caps and whales whose coins usually die are never graded A."""
import time

from fomo import copies, exits
from fomo.db import backfill_alert_outcomes
from fomo.engine import grade_buy
from fomo.whales import Whales
from tests.helpers import MINT, WHALE, pump_buy
from tests.test_exits_scout import hold, tick_at

BASE = dict(stats={"avg": 40, "median": 20, "win_rate": 0.7, "n": 12}, confluence=1, chase=5.0,
            safety={"ok": True}, rug=None, liquidity=50_000, usd_value=500, usual_usd=0, late_chase_pct=50)


def add_alert(db, mint, *, wallet=WHALE, ts, price=1.0, mc_usd=100_000, later=None, peak=0.0, scalp=0, grade="B"):
    return db.insert("""insert into alerts(ts,kind,mint,wallet,grade,mc_usd,price_usd,p6h,peak_price,scalp,status)
        values(?,?,?,?,?,?,?,?,?,?,?)""", (ts, "BUY", mint, wallet, grade, mc_usd, price, later, peak, scalp, "sent"))


def make_hot(bot, wallet=WHALE):
    now = int(time.time())
    for i, x in enumerate([1.6, 1.4, 2.0, 1.3, 1.5, 0.8]):
        cid = copies.open_copy(bot.db, whale=wallet, mint=f"Hot{i}" + "1" * 40, swap_id=0, alert_id=0, price=1.0,
                               mc_usd=1e6, whale_price=1.0, ts=now - 30 * 3600 + i)
        copies.sell(bot.db, bot.db.row("select * from copies where id=?", (cid,)), 1.0, x, 0.0, "whale exited",
                    now - 29 * 3600 + i)
    assert bot.whales.stats(wallet, fresh=True)["status"] == "HOT"


def test_scalp_setups_are_never_grade_a():
    assert grade_buy(status="HOT", **BASE)[0] == "A"
    dying = {"n": 6, "dead": 5, "median_x": 0.2, "hit_2x": 3}
    g, reasons = grade_buy(status="HOT", **BASE, after=dying)
    assert g == "B" and any("5 of 6 were down 50%+" in r for _, r in reasons)
    g, reasons = grade_buy(status="HOT", **BASE, micro={"n": 0, "dead": 0, "median_x": 0, "hit_2x": 0})
    assert g == "B" and any("Micro-cap" in r for _, r in reasons)
    healthy = {"n": 6, "dead": 1, "median_x": 1.4, "hit_2x": 3}
    assert grade_buy(status="HOT", **BASE, after=healthy)[0] == "A"


def test_two_lottery_wins_dont_make_a_whale_hot(bot):
    now = int(time.time())
    # like 7yuq: +1184% and +572% outliers, but the typical copy lost money
    for i, x in enumerate([12.8, 6.7, 0.98, 0.35, 0.52, 0.38, 0.44, 0.98]):
        cid = copies.open_copy(bot.db, whale=WHALE, mint=f"Lot{i}" + "1" * 40, swap_id=0, alert_id=0, price=1.0,
                               mc_usd=1e5, whale_price=1.0, ts=now - 30 * 3600 + i)
        copies.sell(bot.db, bot.db.row("select * from copies where id=?", (cid,)), 1.0, x, 0.0, "whale exited",
                    now - 29 * 3600 + i)
    s = bot.whales.stats(WHALE, fresh=True)
    assert s["avg"] > 100 and s["median"] < 0
    assert s["status"] == "OK"


def test_micro_caps_are_not_sent_by_default(bot):
    bot.whales.add(WHALE, "Flipper")
    bot.market.set_pair(mint=MINT, price=0.00002, mc=20_000, liq=8_000)
    bot.feed(WHALE, pump_buy(tokens=11_250_000))             # $225 at $20K market cap
    assert bot.notes.sent == []
    assert bot.db.scalar("select status from alerts where kind='BUY'") == "micro"
    assert bot.db.scalar("select count(*) from copies") == 1      # still scored


def test_micro_cap_alert_when_turned_on_is_a_scalp_with_a_plan(bot):
    bot.whales.add(WHALE, "Flipper")
    bot.cfg.set("MICRO_ALERTS", "on")
    make_hot(bot)
    bot.market.set_pair(mint=MINT, price=0.00002, mc=20_000, liq=8_000)
    bot.feed(WHALE, pump_buy(tokens=11_250_000))
    msg = bot.notes.sent[0]
    assert msg["kind"] == "BUY" and msg["text"].startswith("<b>⚡ SCALP · ")
    assert "grade B" in msg["text"] and "Micro-cap" in msg["text"]
    assert "sell half at 2x (~$40K MC)" in msg["text"]
    assert ("🎯 Ping me at 2x", None, f"x2:{MINT}") in msg["buttons"][-1]
    assert bot.db.scalar("select scalp from alerts where kind='BUY'") == 1


def test_whale_whose_coins_die_is_a_scalp(bot):
    bot.whales.add(WHALE, "Dumper")
    make_hot(bot)
    t = int(time.time()) - 20 * 3600
    for i in range(6):
        add_alert(bot.db, f"Dead{i}" + "1" * 39, ts=t + i, later=0.1 if i < 5 else 1.5, peak=2.5)
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy())
    text = bot.notes.sent[0]["text"]
    assert "⚡ SCALP" in text and "grade B" in text and "5 of 6 were down 50%+" in text
    assert "don't hold overnight" in text


def test_big_coins_from_healthy_whales_are_not_scalps(bot):
    bot.whales.add(WHALE, "Steady")
    make_hot(bot)
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy())
    text = bot.notes.sent[0]["text"]
    assert "SCALP" not in text and "grade A" in text


def test_tracker_records_what_alerted_coins_did_later(bot):
    t0 = int(time.time()) - 7 * 3600
    aid = add_alert(bot.db, MINT, ts=t0, price=0.001, later=None)
    bot.market.set_pair(mint=MINT, price=0.0004, mc=400_000)
    bot.run(bot.tracker.tick(t0 + 6 * 3600 + 60))
    a = bot.db.row("select * from alerts where id=?", (aid,))
    assert a["p6h"] == 0.0004 and a["p1h"] is None and a["p24h"] is None   # 1h reading was missed, not faked


def test_alert_peak_ignores_one_tick_glitches(bot):
    t0 = int(time.time())
    aid = add_alert(bot.db, MINT, ts=t0, price=0.001)
    for dt, price in ((15, 0.0015), (30, 0.05), (45, 0.0016)):    # a 33x one-tick spike is ignored
        bot.market.set_pair(mint=MINT, price=price, mc=price * 1e9)
        bot.run(bot.tracker.tick(t0 + dt))
    assert bot.db.scalar("select peak_price from alerts where id=?", (aid,)) == 0.0016


def test_backfill_uses_recorded_prices(bot):
    now = int(time.time())
    t0 = now - 30 * 3600
    aid = add_alert(bot.db, MINT, ts=t0, price=0.001)
    bot.db.run("insert into price_marks(mint,ts,price) values(?,?,?)", (MINT, t0 + 600, 0.004))
    bot.db.run("insert into price_marks(mint,ts,price) values(?,?,?)", (MINT, t0 + 3700, 0.002))
    bot.db.run("insert into price_marks(mint,ts,price) values(?,?,?)", (MINT, t0 + 6 * 3600 + 100, 0.0003))
    assert backfill_alert_outcomes(bot.db, now) == 1
    a = bot.db.row("select * from alerts where id=?", (aid,))
    assert (a["p1h"], a["p6h"], a["peak_price"]) == (0.002, 0.0003, 0.004)
    w = Whales(bot.db, bot.cfg).aftermath(WHALE)
    assert w == {"n": 1, "dead": 1, "median_x": 0.3, "hit_2x": 1}


def test_scalp_coins_get_protected_earlier(bot):
    bot.cfg.set("PROFIT_LADDER", "off")
    hold(bot)
    t = int(time.time())
    add_alert(bot.db, MINT, ts=t - 600, price=0.001, scalp=1)
    for i, price in enumerate([0.0012, 0.0016, 0.0017]):          # peak 1.7x — the normal protector waits for 2x
        tick_at(bot, price, t + i * 70)
    tick_at(bot, 0.0012, t + 300)                                  # -29% from the top
    tick_at(bot, 0.0012, t + 330)
    assert bot.notes.kinds() == ["PROTECT"]
    c = exits.coach(bot.db, bot.cfg, bot.portfolio.position(MINT, 0.0012), [], t + 330)
    assert c["scalp"] and c["protect_after"] == 1.5 and c["trail"] == 25


def test_ping_me_at_2x_button(bot):
    from fomo.commands import Commands
    from tests.test_commands import FakeTelegram
    bot.telegram = FakeTelegram()
    bot.started = time.time() - 60
    cmds = Commands(bot)
    bot.market.set_pair(mint=MINT, price=0.00002, mc=20_000)
    bot.run(cmds.on_button({"id": "q1", "data": f"x2:{MINT}"}))
    assert bot.db.scalar("select target_mc from watches") == 40_000
    assert "reaches $40K" in bot.telegram.out[-1]["text"]


def test_stats_report_where_alerts_were_later(bot):
    from fomo import reports
    t = int(time.time()) - 10 * 3600
    add_alert(bot.db, "Aaa" + "1" * 41, ts=t, later=1.8, peak=2.4, grade="A")
    add_alert(bot.db, "Sss" + "1" * 41, ts=t, later=0.1, peak=3.0, scalp=1)
    out = reports.grade_outcomes(bot.db, 14)
    assert out["A"] == {"n": 1, "dead": 0, "median_x": 1.8, "hit_2x": 1}
    assert out["scalp"]["dead"] == 1 and out["B"]["n"] == 0
