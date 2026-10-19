"""The assistant: second legs, the newborn radar, buy checks — and paper as the market gauge."""
import time

from fomo.buycheck import GREEN, RED, format_check
from fomo.swing import analyze
from tests.helpers import MINT
from tests.test_hype import hot_pair

NOW = int(time.time())
STEP = 900


def second_leg(now=NOW, *, peak=4.0, base=1.2, bounce=1.25, spike=5.0, n=288):
    """Three days of 15-minute candles: flat at 1.0, a run to `peak`, a slide to `base`, a long floor, buyers back."""
    out = []
    t0 = now - n * STEP
    for i in range(n):
        if i < 32:
            p = 1.0
        elif i < 56:
            p = 1.0 + (peak - 1.0) * (i - 32) / 24
        elif i < 160:
            p = peak - (peak - base) * (i - 56) / 104
        else:
            p = base * 1.08
        lo = base if i == 160 else p * 0.98 if i >= 160 else p * 0.99
        hi = peak if i == 56 else p * 1.01
        vol = 1000.0
        if i >= n - 4:
            p, lo, vol = base * bounce, base * 1.1, 1000.0 * spike
        out.append((t0 + i * STEP, p, max(hi, p), min(lo, p), p, vol))
    return out


def test_the_second_leg_pattern():
    now = NOW
    sig = analyze(second_leg(), 3.0, now)
    assert sig and abs(sig["leg1"] - 4.0) < 0.1 and 0.65 < sig["retrace"] < 0.75 and sig["room"] > 2.5
    assert sig["stop"] < sig["base_low"] < sig["price"] < sig["tp1"] < sig["tp2"] <= sig["peak"]
    assert analyze(second_leg(base=0.4, bounce=1.25), 3.0, now) is None      # fell 90%: dead, not a base
    assert analyze(second_leg(spike=1.0), 3.0, now) is None                   # no buyers coming back
    assert analyze(second_leg(bounce=2.6), 3.0, now) is None                  # already ran off the floor: late
    assert analyze(second_leg(peak=2.0), 3.0, now) is None                    # first leg too small
    assert analyze(second_leg()[:20], 3.0, now) is None                       # not enough history


def test_a_second_leg_is_alerted_with_a_plan_and_paper_trades_it(bot):
    bot.market.set_pair(mint=MINT, price=1.5, mc=300_000, liq=60_000, created_ms=(NOW - 3 * 86400) * 1000,
                        buys_m5=30, sells_m5=10, change_m5=5, change_h1=10)
    infos = bot.run(bot.market.tokens([MINT]))
    bot.market.candle_data[infos[MINT]["pair_address"]] = second_leg()
    sent = bot.run(bot.swing.scan(infos, NOW))
    assert sent == [MINT]
    msg = bot.notes.sent[-1]
    assert msg["kind"] == "SWING" and "SECOND LEG" in msg["text"] and "stop" in msg["text"] and not msg["silent"]
    t = bot.db.row("select * from paper_trades where whale='swing'")
    plan = bot.engine.paper._own_plan(t)
    assert plan["half"] and plan["tp"] == 1.5 and 0.6 < plan["stop"] < 0.8
    assert bot.db.scalar("select setup from edge_trades") == "s_second_leg"
    assert bot.run(bot.swing.scan(infos, NOW + 60)) == []                     # one alert per coin per 12h


def test_a_young_or_quiet_coin_is_never_a_second_leg_candidate(bot):
    bot.market.set_pair(mint=MINT, price=1.5, mc=300_000, liq=60_000, created_ms=(NOW - 2 * 3600) * 1000,
                        buys_m5=30, sells_m5=10, change_m5=5)
    infos = bot.run(bot.market.tokens([MINT]))
    bot.market.candle_data[infos[MINT]["pair_address"]] = second_leg()
    assert bot.run(bot.swing.scan(infos, NOW)) == []                          # 2 hours old: not a second leg


def test_a_clean_newborn_with_traction_is_on_the_radar_but_never_a_hype_alert(bot):
    bot.market.set_pair(**hot_pair(mc=100_000, liq=20_000, created_ms=(NOW - 10 * 60) * 1000))
    bot.market.boosted = [MINT]
    bot.run(bot.runners.tick(NOW))
    kinds = bot.notes.kinds()
    assert "RADAR" in kinds and "HYPE" not in kinds
    msg = next(m for m in bot.notes.sent if m["kind"] == "RADAR")
    assert msg["silent"] and "10 min old" in msg["text"] and "Brand-new coin" in msg["text"]
    assert bot.db.scalar("select count(*) from paper_trades where whale='radar'") == 1
    assert bot.db.scalar("select kind from hype_seen where mint=?", (MINT,)) == "radar"


def test_a_seeded_newborn_is_not_on_the_radar(bot):
    bot.market.set_pair(**hot_pair(mc=40_000, liq=36_000, created_ms=(NOW - 10 * 60) * 1000))   # liquidity ≈ market cap
    bot.market.boosted = [MINT]
    bot.run(bot.runners.tick(NOW))
    assert "RADAR" not in bot.notes.kinds()


def test_buy_check_flags_a_brand_new_launch_and_a_freeze_authority(bot):
    bot.market.set_pair(mint=MINT, price=0.0001, mc=50_000, liq=20_000, created_ms=(NOW - 15 * 60) * 1000)
    res = bot.run(bot.buycheck.check(MINT))
    assert res["verdict"] == RED and any("Brand-new launch" in t for _, t in res["items"])
    bot.rpc.mints[MINT] = {"mint_authority": "", "freeze_authority": "Dev111", "decimals": 6, "supply": 1e9,
                           "program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}
    bot.db.run("update tokens set safety_ts=0")
    res = bot.run(bot.buycheck.check(MINT))
    assert any("Freeze authority" in t for _, t in res["items"])


def test_buy_check_on_a_settled_coin_and_a_failed_sell_back(bot):
    bot.market.set_pair(mint=MINT, price=0.0004, mc=400_000, liq=80_000, created_ms=(NOW - 3 * 86400) * 1000)
    res = bot.run(bot.buycheck.check(MINT))
    assert res["verdict"] != RED and any(lvl == GREEN and "days old" in t for lvl, t in res["items"])
    assert "Buy check" in format_check(res) and "Size:" in bot.buycheck.plan(res)

    class Taxed:                                     # Jupiter quotes: you get 70% back selling straight away
        async def quote(self, input_mint, output_mint, amount, bps=None):
            return {"outAmount": str(1000 if output_mint == MINT else int(self.spent * 0.7))}
    taxed = Taxed()
    bot.buycheck.live = taxed
    taxed.spent = int(bot.cfg.get("PAPER_TRADE_USD") / 150 * 1e9)
    res = bot.run(bot.buycheck.check(MINT))
    assert res["verdict"] == RED and any("Sell-back test" in t and "30%" in t for _, t in res["items"])


def test_your_own_buy_is_checked_at_once(bot):
    bot.market.set_pair(mint=MINT, price=0.0001, mc=50_000, liq=20_000, created_ms=(NOW - 15 * 60) * 1000)
    bot.run(bot.engine._own_buy_check(MINT, 120.0, NOW))
    msg = bot.notes.sent[-1]
    assert msg["kind"] == "CHECK" and "You bought $120" in msg["text"] and "AVOID" in msg["text"]
    assert not msg["silent"] and "Today:" in msg["text"]
    bot.run(bot.engine._own_buy_check(MINT, 30.0, NOW))                        # another fill of the same buy
    assert sum(1 for m in bot.notes.sent if m["kind"] == "CHECK") == 1


def test_paper_reports_each_alert_type(bot):
    kinds = {r["kind"] for r in bot.engine.paper.by_type()}
    assert {"hype", "whales", "swing", "radar"} <= kinds


def test_check_command(chat):
    from tests.demo_app import COINS
    out = chat.say(f"/check {COINS['CASHED'][0]}")
    assert "Buy check" in out and ("CLEAN" in out or "CAREFUL" in out or "AVOID" in out)
