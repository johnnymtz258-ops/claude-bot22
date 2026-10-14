"""Trade only what's proven: setups replayed on a week of coins, a forward test on paper, real money last."""
import time

from fomo.edge import CONFIRM_N, format_report
from tests.helpers import MINT
from tests.test_hype import hot_pair

NOW = int(time.time())
H = 3600


def coin(i, tag="E"):
    return f"{tag}{i:03d}".ljust(44, "x")


def seen(bot, ts, i, *, score=50, kind="shadow", wins=False, tag="E", liq=15_000.0, mc=200_000.0):
    """One coin the scanner saw (alerted or held back) and two hours of prices after it."""
    mint = coin(i, tag)
    bot.db.run("insert or replace into tokens(mint, symbol, pair_created_ts) values(?,?,?)", (mint, f"C{i}", ts - 3 * 86400))
    bot.db.insert("""insert into hype_seen(ts, mint, kind, rule, score, price, mc, h1, keys) values(?,?,?,?,?,?,?,?,?)""",
                  (ts, mint, kind, "profile" if kind == "shadow" else "", score, 1.0, mc, 10, "rush,pressure"))
    for j in range(1, 125):
        price = 1.0 + 0.01 * j if wins else 1.0 - 0.004 * j      # winners climb to 2x+, losers bleed
        bot.db.run("insert or ignore into price_marks(mint, ts, price, liq) values(?,?,?,?)",
                   (mint, ts + 60 * j, price, liq))
    return mint


def week(bot, strong_wins=True):
    """A week of coins: score-70+ coins win (if strong_wins), everything else loses."""
    for i in range(60):
        ts = NOW - 7 * 86400 + 3 * H + int(i * 2.6 * H)
        seen(bot, ts, i, score=75 if i % 2 == 0 else 50, wins=strong_wins and i % 2 == 0)


def test_with_nothing_making_money_the_autopilot_stands_aside(bot):
    bot.cfg.set("EDGE_GATE", "1")
    week(bot, strong_wins=False)
    rep = bot.run(bot.tuner.edge.evaluate(NOW))
    assert rep["active"] is None and rep["coins"] == 60
    assert "stands aside" in format_report(rep, bot.cfg)
    bot.market.set_pair(**hot_pair())
    bot.market.boosted = [MINT]
    bot.run(bot.runners.tick(NOW))
    hype = [m for m in bot.notes.sent if m["kind"] == "HYPE"]
    assert hype and hype[0]["silent"] and "isn't trading it" in hype[0]["text"]     # sent, but quiet and marked
    assert bot.db.scalar("select count(*) from paper_trades where whale='hype'") == 0


def test_a_setup_that_won_both_halves_is_tested_on_paper_with_its_own_exit(bot):
    bot.cfg.set("EDGE_GATE", "1")
    week(bot)
    rep = bot.run(bot.tuner.edge.evaluate(NOW))
    act = rep["active"]
    assert act and act["setup"] == "score70"
    passing = [t for t in rep["table"] if t["proven"]]
    assert all(t["first"]["avg"] > 1.02 and t["second"]["avg"] > 1.02 for t in passing)
    assert not any(t["setup"] == "alerts" and t["proven"] for t in rep["table"])   # the losers aren't picked
    m = bot.tuner.edge.match({"kind": "alert", "score": 80, "mc": 1, "liq": 1, "age_h": 5, "keys": set()})
    assert m and m["exit"]["tp"] > 1
    assert bot.tuner.edge.match({"kind": "alert", "score": 50, "mc": 1, "liq": 1, "age_h": 5, "keys": set()}) is None
    trade_id = bot.engine.paper.open(alert_id=1, mint=MINT, whale="hype", symbol="X", price=1.0, mc_usd=1e5,
                                     info={"liquidity_usd": 50_000}, plan=m["exit"])
    bot.tuner.edge.record(trade_id, m)
    plan = bot.engine.paper._own_plan(bot.db.row("select * from paper_trades where id=?", (trade_id,)))
    assert plan["tp"] == m["exit"]["tp"] and plan["hold"] == m["exit"]["hold"]
    ok, why = bot.tuner.edge.confirmed(trade_id)
    assert not ok and "still being tested on paper" in why                      # real money waits for paper


def test_real_money_follows_only_after_paper_confirms_and_a_losing_candidate_is_benched(bot):
    bot.cfg.set("EDGE_GATE", "1")
    week(bot)
    edge = bot.tuner.edge
    bot.run(edge.evaluate(NOW))
    bot.db.set_meta("edge:active_since", NOW - 10)
    m = edge.match({"kind": "alert", "score": 80, "mc": 1, "liq": 1, "age_h": 5, "keys": set()})
    ids = []
    for k in range(CONFIRM_N):
        tid = bot.db.insert("""insert into paper_trades(alert_id,mint,whale,symbol,open_ts,entry_price,size_usd,tokens,
            remaining,proceeds_usd,status,close_ts) values(0,?,?,?,?,1,50,50,0,?,'closed',?)""",
                            (coin(k, "P"), "hype", "P", NOW, 50 * (1.10 if k % 3 else 0.97), NOW + 1))
        edge.record(tid, m)
        ids.append(tid)
    assert edge.confirmed(ids[0])[0]                                            # 15 paper trades, +6% a trade
    # the same setup losing on paper: benched at the next check, and the autopilot stands aside again
    bot.db.run("update paper_trades set proceeds_usd=35")
    bot.db.set_meta("edge:last_run", 0)
    bot.run(edge.tick(NOW + 60))
    assert f"score70:{m['plan']}" in edge.report["benched"]
    act = edge.report.get("active") or {}
    assert (act.get("setup"), act.get("plan")) != ("score70", m["plan"])         # another setup, or none
    assert any("autopilot" in n["text"] or "paper trades it" in n["text"] for n in bot.notes.sent if n["kind"] == "INFO")


def test_a_held_back_coin_that_fits_the_setup_is_traded_on_paper(bot):
    bot.cfg.set("EDGE_GATE", "1")
    edge = bot.tuner.edge
    bot.db.set_meta("edge:last_run", NOW)                          # keep this report for the scan below
    edge.report = {"table": [{}], "coins": 1, "active": {
        "setup": "liq_heavy", "plan": "ladder", "label": "x", "plan_label": "y", "avg": 1.1, "n": 30, "worst_half": 1.05}}
    bot.market.set_pair(**hot_pair(mc=60_000, liq=35_000))         # the $30K-100K zone holds it back from an alert…
    bot.market.boosted = [MINT]
    bot.run(bot.runners.tick(NOW))
    assert bot.notes.kinds() == []
    assert bot.db.row("select * from hype_seen")["rule"] == "zone"
    assert bot.db.scalar("select count(*) from paper_trades where whale='hype'") == 1   # …but it fits the setup
    assert bot.db.scalar("select setup from edge_trades") == "liq_heavy"
    plan = bot.engine.paper._own_plan(bot.db.row("select * from paper_trades"))
    assert plan["half"] and plan["tp"] == 1.3                     # traded with the setup's own exit (the ladder)


def test_edge_command_and_dashboard(chat):
    out = chat.say("/edge")
    assert "Edge" in out


def test_live_buys_a_hype_coin_only_once_its_setup_is_confirmed_on_paper(bot):
    from tests.test_live import live_bot
    bot.cfg.set("EDGE_GATE", "1")
    live, chain, quotes, kp, msg = live_bot(bot, dry=False)
    live.edge = bot.tuner.edge
    assert bot.run(live.buy(0, "Hyped" + "1" * 39, "HYPED", "hype")) == 0      # fits no setup being tested
    assert any("being tested" in m["text"] or "fits no setup" in m["text"] for m in bot.notes.sent)


def test_the_review_export_carries_the_edge_paper_period_and_health_history(tmp_path):
    import sqlite3
    import zipfile
    from fomo.db import Database
    from fomo.edge import Edge
    from fomo.config import Config
    from fomo.export import build_review_zip
    src = tmp_path / "whales.db"
    db = Database(src)
    Edge(Config(), db)
    db.set_meta("paper_period_ts", 123)
    db.set_meta("edge:report", "{}")
    db.set_meta("tg_offset", 55)                                                  # not part of a review
    db.run("insert into health_log(ts, data) values(1, '{}')")
    db.close()
    out = build_review_zip(src, tmp_path / "r.zip")
    with zipfile.ZipFile(out) as z:
        z.extractall(tmp_path / "x")
    con = sqlite3.connect(tmp_path / "x" / "fomo_review.db")
    keys = {k for (k,) in con.execute("select key from settings")}
    assert {"paper_period_ts", "edge:report"} <= keys and "tg_offset" not in keys
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert {"health_log", "edge_trades"} <= tables
