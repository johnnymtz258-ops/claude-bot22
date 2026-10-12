"""Self-tuning hype filter: each rule is judged on what it blocked, and moves toward what made money."""
import time

from fomo.tuner import TRACK, Tuner, format_report, replay
from tests.helpers import MINT
from tests.test_hype import hot_pair

NOW = int(time.time())
DAY = 86400
PLAN = {"tp": 1.3, "stop": 0.75, "hold": 3600}


def rising(j):            # +2% a minute: through +30% after the 3% slippage in ~17 minutes
    return 1 + 0.02 * j


def falling(j):           # -1% a minute: through the -25% stop in ~23 minutes
    return 1 - 0.01 * j


def seen(bot, kind, n, path, *, rule="", start=NOW - 2 * DAY, mc=60_000, score=50, h1=20, keys="rush,pressure",
         tag="A"):
    for i in range(n):
        mint = f"{tag}{i:03d}".ljust(44, "x")
        ts = start - i * 900
        bot.db.insert("""insert into hype_seen(ts,mint,kind,rule,score,price,mc,h1,keys) values(?,?,?,?,?,?,?,?,?)""",
                      (ts, mint, kind, rule, score, 1.0, mc, h1, keys))
        for j in range(1, TRACK // 60 + 1):
            bot.db.run("insert into price_marks(mint,ts,price) values(?,?,?)", (mint, ts + 60 * j, path(j)))


def test_replay_follows_the_hype_exit_plan():
    up = [(60 * j, rising(j)) for j in range(1, 60)]
    assert abs(replay(1.0, up, 0, PLAN, 0.03) - 1.30 * 0.97) < 0.03         # take-profit, minus the sell slippage
    down = [(60 * j, falling(j)) for j in range(1, 60)]
    assert replay(1.0, down, 0, PLAN, 0.03) < 0.76                           # stop
    flat = [(60 * j, 1.0 + 0.001 * (j % 2)) for j in range(1, 70)]
    assert abs(replay(1.0, flat, 0, PLAN, 0.0) - 1.0) < 0.002                  # time limit
    assert replay(1.0, flat[:20], 0, PLAN, 0.0) is None                       # price data stops early: not counted
    rug = [(60 * j, 1.2) for j in range(1, 70)]                               # frozen: liquidity pulled
    assert replay(1.0, rug, 0, PLAN, 0.0) == 0.0                              # nothing to sell into
    emptied = [(60, 1.1, 20_000.0), (120, 1.15, 15_000.0), (180, 1.2, 4.0), (240, 1.2, 3.0)]
    assert replay(1.0, emptied, 0, PLAN, 0.0) == 0.0


def test_a_coin_held_back_by_the_zone_is_shadowed_and_priced(bot):
    bot.market.set_pair(**hot_pair(mc=60_000, liq=15_000))
    bot.market.boosted = [MINT]
    bot.run(bot.runners.tick(NOW))
    assert bot.notes.kinds() == []                                            # no alert, no trade…
    row = bot.db.row("select * from hype_seen")
    assert row["kind"] == "shadow" and row["rule"] == "zone"                  # …but it's measured
    assert MINT in bot.tuner.tracked_mints(NOW + 60)
    bot.run(bot.tracker.tick(NOW + 60))
    assert bot.db.scalar("select count(*) from price_marks where mint=?", (MINT,)) == 1
    assert MINT not in bot.tuner.tracked_mints(NOW + TRACK + 60)


def test_alerts_are_recorded_for_the_tuner(bot):
    bot.market.set_pair(**hot_pair())
    bot.market.boosted = [MINT]
    bot.run(bot.runners.tick(NOW))
    assert bot.notes.kinds() == ["HYPE"]
    row = bot.db.row("select * from hype_seen")
    assert row["kind"] == "alert" and row["score"] >= 45 and "rush" in row["keys"]


def test_a_rule_whose_blocked_coins_would_have_won_is_loosened(bot):
    seen(bot, "shadow", 12, rising, rule="zone")
    notes = bot.run(bot.tuner.tick(NOW))
    assert bot.cfg.get("HYPE_AVOID_MC_HI") == bot.cfg.get("HYPE_AVOID_MC_LO")   # zone off
    assert float(bot.db.get_meta("tune:base:HYPE_AVOID_MC_HI")) == 100000
    assert notes and "would have made +26" in notes[0]
    text = bot.notes.sent[-1]["text"]
    assert "Self-tuning" in text and "HYPE_AUTOTUNE 0" in text
    assert bot.run(bot.tuner.tick(NOW + 60)) == []                            # measured every 6 hours, not every minute


def test_a_rule_whose_blocked_coins_lost_stays(bot):
    seen(bot, "shadow", 15, falling, rule="zone")
    assert bot.run(bot.tuner.tick(NOW)) == []
    assert bot.cfg.get("HYPE_AVOID_MC_HI") == 100000
    rule = next(r for r in bot.tuner.report["rules"] if r["rule"] == "zone")
    assert rule["blocked"]["n"] == 15 and rule["blocked"]["avg"] < 0.8


def test_a_loosened_rule_goes_back_when_the_coins_it_lets_in_lose(bot):
    bot.db.set_meta("tune:base:HYPE_AVOID_MC_HI", 100000)
    bot.db.set_meta("tune:changed:HYPE_AVOID_MC_HI", NOW - 5 * DAY)
    bot.cfg.set("HYPE_AVOID_MC_HI", 30000)
    seen(bot, "alert", 12, falling, start=NOW - 2 * DAY, mc=60_000)
    seen(bot, "alert", 6, rising, start=NOW - 2 * DAY, mc=200_000, tag="B")  # outside the zone: not the rule's doing
    notes = bot.run(bot.tuner.tick(NOW))
    assert bot.cfg.get("HYPE_AVOID_MC_HI") == 100000
    assert "let in lost" in notes[0]


def test_one_change_per_rule_every_three_days_and_your_own_settings_are_left_alone(bot):
    seen(bot, "shadow", 12, rising, rule="profile")
    bot.db.set_meta("tune:manual:HYPE_SKIP_PROFILE", NOW - DAY)
    assert bot.run(bot.tuner.tick(NOW)) == [] and bot.cfg.flag("HYPE_SKIP_PROFILE")
    bot.db.set_meta("tune:manual:HYPE_SKIP_PROFILE", 0)
    bot.db.set_meta("tune:changed:HYPE_SKIP_PROFILE", NOW - DAY)
    bot.db.set_meta("tune:last_run", 0)
    assert bot.run(bot.tuner.tick(NOW)) == [] and bot.cfg.flag("HYPE_SKIP_PROFILE")
    bot.db.set_meta("tune:changed:HYPE_SKIP_PROFILE", NOW - 4 * DAY)
    bot.db.set_meta("tune:last_run", 0)
    assert bot.run(bot.tuner.tick(NOW)) and not bot.cfg.flag("HYPE_SKIP_PROFILE")


def test_autotune_off_measures_but_changes_nothing(bot):
    bot.cfg.set("HYPE_AUTOTUNE", "0")
    seen(bot, "shadow", 12, rising, rule="zone")
    assert bot.run(bot.tuner.tick(NOW)) == []
    assert bot.cfg.get("HYPE_AVOID_MC_HI") == 100000
    assert bot.tuner.report["rules"][0]["change"][0] == "loosen"
    assert "measuring only" in format_report(bot.tuner.report)


def test_score_threshold_follows_the_coins_around_it(bot):
    seen(bot, "shadow", 12, rising, rule="score", score=42)                   # just under 45 would have won
    bot.run(bot.tuner.tick(NOW))
    assert bot.cfg.get("HYPE_MIN_SCORE") == 40
    other = Tuner(bot.cfg, bot.db, bot.notes)                                  # and alerts just over it losing:
    bot.db.run("delete from hype_seen")
    bot.db.run("delete from meta where key like 'tune:%'")
    bot.cfg.set("HYPE_MIN_SCORE", "45")
    seen(bot, "alert", 15, falling, score=46, tag="C")
    bot.run(other.tick(NOW))
    assert bot.cfg.get("HYPE_MIN_SCORE") == 50


def test_the_exit_plan_moves_to_what_worked_on_your_alerts(bot):
    def run_then_fade(j):                       # runs to +65% in 45 min, then gives it all back
        return 1 + 0.0145 * j if j <= 45 else max(0.9, 1.6525 - 0.05 * (j - 45))
    seen(bot, "alert", 25, run_then_fade, mc=200_000)
    notes = bot.run(bot.tuner.tick(NOW))
    assert (bot.cfg.get("HYPE_TP_PCT"), bot.cfg.get("HYPE_STOP_PCT"), bot.cfg.get("HYPE_MAX_MINUTES")) == (50, 25, 60)
    assert any("Hype exit plan: all out at +50%" in n for n in notes)


def test_tuning_command_and_dashboard(chat):
    out = chat.say("/tuning")
    assert "Self-tuning hype filter" in out and "What each rule blocked" in out


def test_your_own_set_marks_a_setting_as_yours(bot):
    from fomo.app import App
    app = App.__new__(App)
    app.cfg, app.db = bot.cfg, bot.db
    app.set_setting("HYPE_MIN_SCORE", "50")
    assert int(float(bot.db.get_meta("tune:manual:HYPE_MIN_SCORE"))) >= NOW - 5
    app.set_setting("HYPE_MAX_H1_PCT", "150", by_tuner=True)
    assert not bot.db.get_meta("tune:manual:HYPE_MAX_H1_PCT")


def test_coins_just_under_the_score_and_paid_profile_coins_are_shadowed(bot):
    bot.cfg.set("HYPE_MIN_SCORE", "60")                                       # this push scores 55
    bot.market.set_pair(**hot_pair())
    bot.market.boosted = [MINT]                                                # how the scanner finds it
    bot.run(bot.runners.tick(NOW))
    assert bot.notes.kinds() == []
    assert bot.db.row("select rule, kind from hype_seen") == {"rule": "score", "kind": "shadow"}
    bot.db.run("delete from hype_seen")
    bot.cfg.set("HYPE_MIN_SCORE", "45")
    bot.market.watchlist, bot.market.boosted = [MINT], []                      # on DexScreener's paid profile list
    bot.market._cache.clear()
    bot.run(bot.runners.tick(NOW + 60))
    assert bot.notes.kinds() == []
    assert bot.db.row("select rule from hype_seen")["rule"] == "profile"


def test_past_hype_alerts_are_brought_in_once_if_todays_filter_would_send_them(bot):
    for i, (mc, reasons) in enumerate([(200_000, "signals:rush,pressure"), (60_000, "signals:rush,volume"),
                                       (200_000, "signals:rush,profile"), (60_000, "signals:whales,rush")]):
        bot.db.insert("""insert into alerts(ts,kind,mint,wallet,mc_usd,price_usd,reasons) values(?,?,?,?,?,?,?)""",
                      (NOW - DAY - i * 60, "HYPE", f"P{i}".ljust(44, "x"), "hype", mc, 1.0, reasons))
    assert bot.tuner.backfill() == 0                                           # done once, at the fixture's start
    bot.db.set_meta("tune:backfilled", "0")
    t = Tuner(bot.cfg, bot.db, bot.notes)                                      # a first start brings in 2 of the 4:
    keys = sorted(r["keys"] for r in bot.db.rows("select keys from hype_seen"))
    assert keys == ["rush,pressure", "whales,rush"]                            # zone and profile coins weren't
    bot.db.set_meta("tune:backfilled", "0")
    assert t.backfill() == 0                                                   # never twice


def test_a_half_and_ride_ladder_wins_when_coins_keep_running():
    from fomo.tuner import replay
    path = [(60 * j, 1 + 0.02 * j) for j in range(1, 121)]          # +2%/min for two hours: up to 3.4x
    all_out = replay(1.0, path, 0, {"tp": 1.3, "stop": 0.75, "hold": 7200}, 0.0)
    ladder = replay(1.0, path, 0, {"tp": 1.3, "stop": 0.75, "hold": 7200, "half": True}, 0.0)
    assert abs(all_out - 1.3) < 0.03 and ladder > 2.0                 # half at 1.3x, the rest rode to the time limit
    dump = [(60, 1.32), (120, 1.0), (180, 0.7)]
    assert abs(replay(1.0, dump, 0, {"tp": 1.3, "stop": 0.75, "hold": 7200, "half": True}, 0.0) - (0.66 + 0.35)) < 0.01


def test_paper_hype_ladder_and_liquidity_pull_exit(bot):
    from tests.test_hype import hot_pair
    bot.cfg.set("PAPER_SLIPPAGE_PCT", "0")
    bot.cfg.set("HYPE_HALF_AT_TP", "1")
    bot.market.set_pair(**hot_pair())
    bot.market.boosted = [MINT]
    bot.run(bot.runners.tick(NOW))
    assert "sell half at +30%" in bot.notes.sent[0]["text"] and "ride the rest" in bot.notes.sent[0]["text"]
    bot.market.set_pair(**hot_pair(price=0.0002 * 1.35, mc=270_000))
    bot.market._cache.clear()
    bot.run(bot.tracker.tick(NOW + 60))
    t = bot.db.row("select * from paper_trades where whale='hype'")
    assert t["status"] == "open" and t["half_taken"] == 1 and t["remaining"] == 0.5
    bot.market.set_pair(**hot_pair(price=0.0002 * 1.4, mc=280_000, liq=6_000))      # pool emptied 85%
    bot.market._cache.clear()
    bot.run(bot.tracker.tick(NOW + 120))
    assert bot.db.row("select status from paper_trades where whale='hype'")["status"] == "open"   # seen once: wait
    bot.market._cache.clear()
    bot.run(bot.tracker.tick(NOW + 135))
    t = bot.db.row("select * from paper_trades where whale='hype'")
    assert t["status"] == "closed" and "liquidity pulled" in t["close_reason"]
