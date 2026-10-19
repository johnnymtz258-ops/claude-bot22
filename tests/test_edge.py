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


def test_with_nothing_making_money_paper_still_trades_but_real_money_stands_aside(bot):
    """Paper is the market gauge: it trades every alert. Only real money waits for a proven setup."""
    bot.cfg.set("EDGE_GATE", "1")
    week(bot, strong_wins=False)
    rep = bot.run(bot.tuner.edge.evaluate(NOW))
    assert rep["active"] is None and rep["coins"] == 60
    assert "stands aside" in format_report(rep, bot.cfg)
    bot.market.set_pair(**hot_pair())
    bot.market.boosted = [MINT]
    bot.run(bot.runners.tick(NOW))
    hype = [m for m in bot.notes.sent if m["kind"] == "HYPE"]
    assert hype and hype[0]["silent"] and "paper only, no real money" in hype[0]["text"]   # sent quietly, marked
    tid = bot.db.scalar("select id from paper_trades where whale='hype'")
    assert tid                                                                     # paper trades it (the gauge)…
    assert not bot.tuner.edge.confirmed(tid)[0]                                    # …real money would not follow
    bot.cfg.set("HYPE_ALERTS_NEED_EDGE", "1")                                      # opt in: only proven setups ping
    bot.db.run("delete from alerts")
    bot.market._cache.clear()
    before = len(bot.notes.sent)
    bot.run(bot.runners.tick(NOW + 7 * 3600))
    assert not [m for m in bot.notes.sent[before:] if m["kind"] == "HYPE"]
    assert bot.db.scalar("select status from alerts where kind='HYPE'") == "no_edge"


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


def whale_buy(bot, ts, i, *, wins, age_h=48.0, mc=300_000.0, usd=1_500.0, wallet="W" * 44, confluence=1):
    mint = coin(i, "W")
    bot.db.run("insert or replace into tokens(mint, symbol, pair_created_ts) values(?,?,?)", (mint, f"W{i}", ts - age_h * 3600))
    sid = bot.db.insert("""insert into swaps(sig,wallet,is_me,ts,seen_ts,side,mint,token_amount,base,base_amount,usd_value)
        values(?,?,0,?,?,'BUY',?,1,'SOL',1,?)""", (f"ws{i}", wallet, ts - 50, ts - 49, mint, usd))
    bot.db.insert("""insert into alerts(ts,kind,mint,wallet,swap_id,grade,mc_usd,price_usd,confluence,status)
        values(?,?,?,?,?,?,?,?,?,?)""", (ts, "BUY", mint, wallet, sid, "B", mc, 1.0, confluence, "late"))
    for j in range(1, 380):
        price = 1.0 + 0.01 * j if wins else max(0.3, 1.0 - 0.004 * j)
        bot.db.run("insert or ignore into price_marks(mint, ts, price, liq) values(?,?,?,?)", (mint, ts + 60 * j, price, 60_000.0))
    return mint


def test_whale_buys_are_measured_too_and_fresh_launches_never_count(bot):
    bot.cfg.set("EDGE_GATE", "1")
    bot.cfg.set("WHALE_MIN_POOL_HOURS", "6")
    for i in range(30):                          # a week of whale buys into settled $100K+ pools: they work
        whale_buy(bot, NOW - 7 * 86400 + 8 * H + int(i * 5 * H), i, wins=True)
    for i in range(30, 40):                      # buys into 10-minute-old pools: never tradable, never counted
        whale_buy(bot, NOW - 6 * 86400 + int((i - 30) * 9 * H), i, wins=False, age_h=0.2)
    rep = bot.run(bot.tuner.edge.evaluate(NOW))
    w = rep["whale"]
    assert w["coins"] == 30 and w["active"] and w["active"]["setup"].startswith("w_")
    f = bot.tuner.edge.whale_features("W" * 44, {"mc_usd": 300_000, "liquidity_usd": 60_000,
                                                 "pair_created_ts": NOW - 2 * 86400}, 1500, 1, NOW)
    m = bot.tuner.edge.match(f)
    assert m and m["exit"]["tp"] > 1
    assert "Whale buys" in format_report(rep, bot.cfg)


def test_a_whale_copy_gets_the_tested_setups_exit_and_real_money_only_follows_that(bot):
    import time as _t
    from tests.helpers import WHALE, pump_buy
    bot.cfg.set("EDGE_GATE", "1")
    bot.whales.add(WHALE, "Rocket")
    bot.market.set_pair(mint=MINT, price=0.000075, mc=150_000, created_ms=int((_t.time() - 3 * 86400) * 1000))
    bot.feed(WHALE, pump_buy())                                            # nothing proven: paper copies it as the gauge
    assert bot.notes.kinds() == ["BUY"] and "paper only, no real money" in bot.notes.sent[0]["text"]
    first = bot.db.scalar("select id from paper_trades")
    assert first and not bot.tuner.edge.confirmed(first)[0]
    bot.db.run("delete from paper_trades")
    bot.tuner.edge.report = {"table": [], "active": None, "whale": {"table": [{}], "active": {
        "setup": "w_all", "plan": "swing", "label": "Whale buys into settled pools", "plan_label": "x", "avg": 1.08,
        "n": 30, "worst_half": 1.04}}}
    other = "Other" + "1" * 39
    bot.market.set_pair(mint=other, price=0.000075, mc=150_000, created_ms=int((_t.time() - 3 * 86400) * 1000))
    bot.feed(WHALE, pump_buy(mint=other))                                  # a setup is being tested: paper copies it
    assert "Fits the whale setup paper is testing" in bot.notes.sent[-1]["text"]
    assert bot.db.scalar("select setup from edge_trades") == "w_all"
    plan = bot.engine.paper._own_plan(bot.db.row("select * from paper_trades"))
    assert plan["half"] and plan["tp"] == 2.0 and plan["hold"] == 21600


def test_a_coins_age_is_its_oldest_pool_not_its_newest():
    """$Pluto: days old, but a new pool made it look minutes old — and v30 blocked the whale buy as a launch."""
    from fomo.market import best_pairs
    from tests.helpers import pair
    old = pair(mint=MINT, liq=10_000, created_ms=(NOW - 5 * 86400) * 1000)
    new = pair(mint=MINT, liq=40_000, created_ms=(NOW - 600) * 1000, pair_addr="NewPool" + "1" * 37)
    info = best_pairs([old, new])[MINT]
    assert info["liquidity_usd"] == 40_000                      # prices from the deepest pool…
    assert NOW - info["pair_created_ts"] >= 5 * 86400 - 5        # …age from the oldest


def test_the_backup_stream_prefers_a_provider_that_still_has_credits(monkeypatch):
    from fomo import config
    monkeypatch.setenv("SOLANA_RPC_HTTP", "https://solana-mainnet.core.chainstack.com/abc")
    monkeypatch.setenv("HELIUS_API_KEY", "k")
    monkeypatch.setenv("SOLANA_RPC_FALLBACKS", "https://solana-mainnet.g.alchemy.com/v2/k")
    monkeypatch.setenv("SOLANA_RPC_WSS", "")
    cfg = config.load(env_file=None)
    assert cfg.rpc_wss_extra[0].startswith("wss://solana-mainnet.g.alchemy.com")
