import time

import pytest

from fomo import copies, exits
from fomo.scout import qualifies
from tests.helpers import MINT, WHALE, WHALE2


# -- exit styles ---------------------------------------------------------------------------------

def test_simulate_exit_styles():
    path = [1.0, 0.7, 1.5, 2.2, 3.4, 4.0, 2.4, 1.2]      # ran to 4x, then gave most of it back
    r = exits.simulate(1.0, path, whale_ret=10.0, fee_pct=0)
    assert r["whale"] == 10.0
    assert r["all2x"] == pytest.approx(120.0)            # sold everything at the first 2x reading (2.2)
    assert r["half2x"] == pytest.approx((0.5 * 2.2 + 0.5 * 1.10 - 1) * 100)
    assert r["trail"] == pytest.approx(140.0)            # peak 4.0, sold at 2.4 (-40% from peak)
    # ladder: 1/3 at 2.2, 1/3 at 3.4, last third trailed out at 2.4
    assert r["ladder"] == pytest.approx(((2.2 + 3.4 + 2.4) / 3 - 1) * 100)


def test_simulate_without_2x_falls_back_to_the_whale():
    r = exits.simulate(1.0, [1.0, 1.3, 0.9], whale_ret=-12.0, fee_pct=1)
    assert r["all2x"] == r["trail"] == r["ladder"] == -12.0


def test_mark_recorder_throttles_and_ignores_glitches(bot):
    rec = exits.MarkRecorder(bot.db)
    t = 1_000_000
    assert rec.record(MINT, 1.0, t)
    assert not rec.record(MINT, 1.1, t + 30)             # < 60s since last mark
    assert not rec.record(MINT, 50.0, t + 70)            # one-off 50x spike
    assert rec.record(MINT, 1.2, t + 140)
    assert rec.record(MINT, 12.0, t + 210) is False and rec.record(MINT, 12.5, t + 280)  # confirmed real move
    assert [p for _, p in exits.marks(bot.db, MINT, 0, t + 999)] == [1.0, 1.2, 12.5]


def seed_marks(bot, mint, start, prices, step=120):
    for i, p in enumerate(prices):
        bot.db.run("insert into price_marks(mint,ts,price) values(?,?,?)", (mint, start + i * step, p))


def test_exit_lab_uses_recorded_paths(bot):
    now = int(time.time())
    t0 = now - 3 * 86400
    cid = copies.open_copy(bot.db, whale=WHALE, mint=MINT, swap_id=1, alert_id=1, price=1.0, mc_usd=1e5,
                           whale_price=1.0, ts=t0)
    seed_marks(bot, MINT, t0, [1.0, 2.5, 4.0, 1.5, 1.1])
    c = bot.db.row("select * from copies where id=?", (cid,))
    copies.sell(bot.db, c, 1.0, 1.1, 1.0, "whale exited", t0 + 700)
    lab = exits.exit_lab(bot.db, bot.cfg)
    assert lab["copies"] == 1 and lab["best"]["key"] in {"all2x", "trail", "ladder", "half2x"}
    assert lab["best"]["avg"] > next(r["avg"] for r in lab["rules"] if r["key"] == "whale")


def test_my_exit_habits_spot_early_and_late_sells(bot):
    now = int(time.time())
    t0 = now - 2 * 86400
    bot.db.run("insert into my_trades(ts,mint,side,usd,tokens,price_usd,source,sig) values(?,?,?,?,?,?,?,?)",
               (t0, MINT, "BUY", 10, 10, 1.0, "manual", "b1"))
    seed_marks(bot, MINT, t0, [1.0, 2.0, 3.0, 2.0])                      # peaked 3.0 while held
    sell_ts = t0 + 4 * 120
    bot.db.run("insert into my_trades(ts,mint,side,usd,tokens,price_usd,source,sig) values(?,?,?,?,?,?,?,?)",
               (sell_ts, MINT, "SELL", 15, 10, 1.5, "manual", "s1"))
    seed_marks(bot, MINT, sell_ts + 60, [1.6, 2.2, 2.4])                 # then ran to 2.4 after
    h = exits.my_exit_habits(bot.db)
    s = h["sells"][0]
    assert s["below_peak_pct"] == pytest.approx(50.0)
    assert s["after_gain_pct"] == pytest.approx(60.0)


# -- nudges on your positions --------------------------------------------------------------------

def hold(bot, price=0.001):
    bot.market.set_pair(mint=MINT, price=price, mc=price * 1e9, liq=100_000)
    bot.run(bot.portfolio.manual_buy(MINT, 50))


def tick_at(bot, price, t):
    bot.market.set_pair(mint=MINT, price=price, mc=price * 1e9, liq=100_000 * (price / 0.001) ** 0.5)
    bot.run(bot.tracker.tick(t))


def test_ladder_nudges_once_per_level(bot):
    hold(bot)
    t = int(time.time())
    tick_at(bot, 0.0015, t)
    assert bot.notes.kinds() == []
    tick_at(bot, 0.0021, t + 20)
    tick_at(bot, 0.0023, t + 40)
    assert bot.notes.kinds() == ["LADDER"] and "hit 2x" in bot.notes.sent[0]["text"]
    tick_at(bot, 0.0055, t + 60)                        # jumps past 3x and 5x: one message for 5x
    assert bot.notes.kinds() == ["LADDER", "LADDER"] and "hit 5x" in bot.notes.sent[1]["text"]


def test_profit_protector_after_2x_confirmed(bot):
    bot.cfg.set("PROFIT_LADDER", "off")
    hold(bot)
    t = int(time.time())
    for i, price in enumerate([0.002, 0.003, 0.0032]):
        tick_at(bot, price, t + i * 70)                  # peak 3.2x, recorded as marks
    tick_at(bot, 0.0019, t + 300)                        # -41% from peak: first sighting
    assert bot.notes.kinds() == []
    tick_at(bot, 0.0019, t + 330)
    assert bot.notes.kinds() == ["PROTECT"] and "gave back" in bot.notes.sent[0]["text"]
    tick_at(bot, 0.0015, t + 400)
    assert bot.notes.kinds() == ["PROTECT"]             # once per holding period


def test_protector_never_fires_on_an_early_dip(bot):
    bot.cfg.set("PROFIT_LADDER", "off")
    hold(bot)
    t = int(time.time())
    for i, price in enumerate([0.0015, 0.0006, 0.0005, 0.0004]):   # up 1.5x then -73%: never reached 2x
        tick_at(bot, price, t + i * 70)
    assert bot.notes.kinds() == []


# -- autopilot ---------------------------------------------------------------------------------------

GOOD = {"ok": True, "verdict": "✅ Looks copyable", "trips": 9, "pnl_sol": 4.2, "win_rate": 0.6,
        "median_hold_s": 900, "last_trade_ts": 0}


def test_qualifies_rules():
    now = time.time()
    good = {**GOOD, "last_trade_ts": now - 3600}
    assert qualifies(good, now)[0]
    assert not qualifies({**good, "pnl_sol": 0.3}, now)[0]
    assert not qualifies({**good, "win_rate": 0.3}, now)[0]
    assert not qualifies({**good, "last_trade_ts": now - 5 * 86400}, now)[0]
    assert not qualifies({**good, "verdict": "🤖 Too fast to copy"}, now)[0]


def test_scout_follows_profitable_early_buyers(bot, monkeypatch):
    now = int(time.time())
    coin = {"ok": True, "symbol": "RUN", "candidates": [
        {"wallet": WHALE, "tracked": False, "flags": [], "entry_mc": 90_000, "to_peak": 12},
        {"wallet": WHALE2, "tracked": False, "flags": [], "entry_mc": 95_000, "to_peak": 11},
        {"wallet": "GjJyeC1rB1p4d6k1Mzw5Y6vYGZyLr8N8zQJ7XU4yzF1G", "tracked": False, "flags": ["launch sniper"],
         "entry_mc": 5_000, "to_peak": 200}]}

    async def research(mint, progress=None):
        return coin

    async def analyze(wallet, progress=None):
        return {**GOOD, "pnl_sol": 4.2 if wallet == WHALE else -2.0}

    async def last_trade(wallet):
        return now - 600

    monkeypatch.setattr(bot.scout.discovery, "research_coin", research)
    monkeypatch.setattr(bot.scout.discovery, "analyze_wallet", analyze)
    monkeypatch.setattr(bot.scout, "_last_trade_ts", last_trade)
    monkeypatch.setattr(bot.scout, "pick_coins", lambda limit=3: [MINT])
    s = bot.run(bot.scout.scout(now))
    # default: suggest only — you decide who to follow
    assert s == {"ts": now, "coins": [MINT], "checked": 2, "followed": 0, "picked": 1}
    assert bot.whales.get(WHALE) is None
    assert bot.notes.kinds() == ["PICKS"] and WHALE in bot.notes.sent[0]["text"]
    assert bot.notes.sent[0]["buttons"][0][0] == ("➕ Follow #1", None, f"track:{WHALE}")
    assert bot.db.row("select status from whale_candidates where address=?", (WHALE2,))["status"] == "rejected"
    # opt-in auto-follow still works
    bot.cfg.set("AUTO_WHALES", "on")
    bot.db.run("delete from whale_candidates")
    bot.run(bot.scout.scout(now))
    w = bot.whales.get(WHALE)
    assert w["source"] == "auto" and w["active"] == 1 and bot.notes.kinds()[-1] == "AUTO"


def test_prune_drops_idle_or_cold_auto_whales_but_never_yours(bot):
    now = int(time.time())
    bot.whales.add(WHALE, "auto-1", source="auto")
    bot.whales.add(WHALE2, "Mine")
    bot.db.run("update whales set added_ts=?, last_trade_ts=?", (now - 10 * 86400, now - 6 * 86400))
    dropped = bot.run(bot.scout.prune(now))
    assert [w["address"] for w, _ in dropped] == [WHALE]
    assert bot.whales.get(WHALE)["active"] == 0 and bot.whales.get(WHALE2)["active"] == 1
    assert bot.run(bot.scout.prune(now + 60)) == []        # once a day


def test_adding_an_auto_whale_yourself_makes_it_yours(bot):
    bot.whales.add(WHALE, "auto-1", source="auto")
    bot.whales.add(WHALE, "Keeper")
    assert bot.whales.get(WHALE)["source"] == "manual"
    now = int(time.time())
    bot.db.run("update whales set added_ts=?, last_trade_ts=?", (now - 10 * 86400, now - 6 * 86400))
    assert bot.run(bot.scout.prune(now)) == []


# -- hold plan & alert funnel ----------------------------------------------------------------------

def seed_copies(bot, whale, n, peak_x=2.5, peak_after=4800, at15=1.2):
    now = int(time.time())
    for i in range(n):
        t = now - (i + 1) * 20000
        bot.db.run("""insert into copies(whale,mint,swap_id,open_ts,entry_price,peak_price,peak_ts,p15m,status,
            return_pct,last_price) values(?,?,?,?,?,?,?,?,?,?,?)""",
                   (whale, f"Coin{i}", 0, t, 1.0, peak_x, t + peak_after, at15, "closed", 40.0, 1.4))


def test_hold_plan_uses_this_whales_history(bot):
    seed_copies(bot, WHALE, 6)
    plan = exits.hold_plan(bot.db, WHALE)
    assert plan["scope"] == "this whale" and plan["time_to_peak_s"] == 4800 and plan["peak_x"] == pytest.approx(2.5)
    line = exits.hold_plan_line(plan)
    assert "peaked ~1h20m after the buy at ~2.5x" in line and "at least ~40m" in line and "+20%" in line


def test_hold_plan_falls_back_to_all_whales_then_to_a_note(bot):
    assert "not enough history" in exits.hold_plan_line(exits.hold_plan(bot.db, WHALE2))
    seed_copies(bot, WHALE, 6)
    assert exits.hold_plan(bot.db, WHALE2)["scope"] == "your whales overall"


def test_buy_alert_includes_hold_plan_and_funnel_counts_outcomes(bot):
    from fomo.reports import alert_funnel
    from tests.helpers import pump_buy
    seed_copies(bot, WHALE, 6)
    bot.whales.add(WHALE, "Rocket")
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy())
    assert "⏱ Hold plan (this whale, 6 picks)" in bot.notes.sent[0]["text"]
    bot.feed(WHALE, pump_buy(mint="MoiNmemeTokenMint22222222222222222222222pump"[:44], sol=0.2, tokens=100_000))
    f = alert_funnel(bot.db)
    assert f["counts"] == {"sent": 1, "too_small": 1}
