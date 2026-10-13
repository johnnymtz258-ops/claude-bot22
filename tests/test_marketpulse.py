"""Market pulse: is the memecoin market hot or cold, what's trending, what to look out for."""
import json
import time

from fomo.marketpulse import format_report, launchpad_of, theme_of

NOW = int(time.time())
H = 3600


def coin(i, tag="C"):
    return f"{tag}{i:03d}".ljust(44, "x")


def hype_alert(bot, ts, i, doubled, *, tag="C", symbol="X", name="x coin", dex="pumpswap", rug=False, fake=False):
    """One hype alert and 24 minutes of recorded prices. doubled: the price really climbs to 2.5x with a pool behind it.
    rug: the pool is emptied after 3 minutes. fake: a 3x price is printed AFTER the pool was emptied (not a pump)."""
    mint = coin(i, tag)
    bot.db.run("insert or replace into tokens(mint, symbol, name, dex) values(?,?,?,?)", (mint, symbol, name, dex))
    bot.db.insert("insert into alerts(ts,kind,mint,wallet,price_usd,peak_price) values(?,?,?,?,?,?)",
                  (ts, "HYPE", mint, "hype", 1.0, 3.0 if fake else 2.5 if doubled else 1.1))
    bot.db.insert("insert into hype_seen(ts,mint,kind,price) values(?,?,?,?)", (ts, mint, "alert", 1.0))
    for j in range(1, 25):
        price = 1.0 + (0.0625 if doubled else 0.01) * j
        liq = 20_000.0
        if rug and j > 3:
            price, liq = (3.0 if fake and j > 20 else 1.2), 3.0
        bot.db.run("insert or ignore into price_marks(mint,ts,price,liq) values(?,?,?,?)", (mint, ts + 60 * j, price, liq))


def running(bot, start, end):
    for ts in range(start, end, 300):
        bot.db.run("insert or ignore into paper_equity(ts, equity) values(?, 1000)", (ts,))


def test_themes_and_launchpads():
    assert theme_of("大闸蟹", "") == "chinese" and theme_of("파랭이", "") == "korean"
    assert theme_of("AGENT", "Claude agent") == "ai" and theme_of("DOGE2", "") == "dog" and theme_of("XYZ", "zzz") == "other"
    assert launchpad_of("pumpswap") == "pump.fun" and launchpad_of("meteora") == "Meteora (bags / believe)"


def test_a_cold_market_is_called_cold_with_reasons_and_lookouts(bot):
    running(bot, NOW - 8 * 24 * H, NOW)
    for i in range(70):                                   # last week: 10 hype coins a day, 4 doubled, few rugs
        hype_alert(bot, NOW - 2 * 24 * H - i * 2 * H, i, doubled=i % 10 < 4, rug=i % 10 == 9, tag="W")
    for i in range(12):                                   # today: 12 coins, 1 doubled, most rugs, Chinese names
        hype_alert(bot, NOW - 20 * H + i * H, i, doubled=i == 0, rug=i % 4 != 0, tag="T", symbol="大闸蟹" if i % 2 else "CN",
                   name="螃蟹" if i % 3 else "crab", dex="meteora")
    snap = {"ts": NOW - 600, "sol_usd": 112.8, "sol_24h": -7.5, "fng": 22, "fng_label": "Extreme Fear", "board_up": 0.22}
    bot.db.run("insert into market_pulse(ts, data) values(?, ?)", (NOW - 600, json.dumps(snap)))
    r = bot.pulse.report(NOW, fresh=True)
    assert r["regime"] == "cold" and r["score"] <= -2
    text = " ".join(r["lookouts"])
    assert "Fewer real pumps" in text and "SOL is down" in text and "Extreme fear" in text and "More rugs" in text
    assert "Chinese-name coins" in text
    assert any(p["launchpad"] == "Meteora (bags / believe)" and p["rug_rate"] >= 0.6 for p in r["launchpads"])
    out = format_report(r)
    assert "Market: 🧊 Cold" in out and "What to look out for" in out
    days = bot.pulse.daily(3, NOW)
    assert len(days) == 3 and days[-1]["up_h"] > 0


def whale_buys(bot, start, every, n):
    for k in range(n):
        bot.db.insert("""insert into swaps(sig,wallet,is_me,ts,seen_ts,side,mint,token_amount,base,base_amount,usd_value)
            values(?,?,0,?,?,'BUY',?,1,'SOL',1,100)""", (f"s{start}{k}", "W" * 44, start + k * every, start + k * every, coin(k, "B")))


def test_downtime_does_not_look_like_a_crash(bot):
    for i in range(70):                                   # a running week: 40% of hype coins really doubled
        hype_alert(bot, NOW - 2 * 24 * H - i * 2 * H, i, doubled=i % 10 < 4, tag="W")
    running(bot, NOW - 8 * 24 * H, NOW - 24 * H)
    whale_buys(bot, NOW - 8 * 24 * H, 3 * H, 50)          # whales: a buy every 3 running hours…
    running(bot, NOW - 3 * H, NOW)                        # today the bot only ran 3 hours…
    for i in range(10):
        hype_alert(bot, NOW - 3 * H + i * 600, i, doubled=i < 5, tag="T")   # …and half its hype coins doubled
    whale_buys(bot, NOW - 2 * H, 1, 1)                    # …and the whales bought once: the same pace
    r = bot.pulse.report(NOW, fresh=True)
    assert r["today"]["up_h"] <= 3.1 and r["regime"] in ("hot", "healthy")
    assert not any("sitting out" in x for x in r["lookouts"])


def test_a_pump_printed_into_an_emptied_pool_is_not_a_pump(bot):
    running(bot, NOW - 3 * H, NOW)
    hype_alert(bot, NOW - 2 * H, 1, doubled=False, rug=True, fake=True)    # 3x printed after the liquidity was pulled
    hype_alert(bot, NOW - 2 * H, 2, doubled=True)                          # a real 2.5x
    w = bot.pulse.window(NOW - 3 * H, NOW, NOW)
    assert w["checked"] == 2 and w["x2"] == 1 and w["rug_rate"] == 0.5
    fates = {a["mint"]: a["fate"] for a in w["alerts"]}
    assert fates[coin(1)]["best"] < 2 and fates[coin(2)]["best"] >= 2


def test_a_whale_feed_outage_is_not_read_as_whales_sitting_out(bot):
    running(bot, NOW - 8 * 24 * H, NOW)
    whale_buys(bot, NOW - 8 * 24 * H, H, 150)             # last week: a buy an hour; today: none
    for k in range(8):                                    # …but the feed was down most of today
        bot.db.run("insert into market_pulse(ts, data) values(?, ?)",
                   (NOW - k * H, json.dumps({"ts": NOW - k * H, "feed_ok": k == 0})))
    r = bot.pulse.report(NOW, fresh=True)
    text = " ".join(r["lookouts"])
    assert "whale feed was down" in text and "sitting out" not in text


def test_snapshot_reads_majors_mood_board_and_the_scanner(bot):
    async def fake_get(url, timeout=8.0):
        if "coingecko" in url:
            return {"solana": {"usd": 112.8, "usd_24h_change": -7.5}, "bitcoin": {"usd": 81500, "usd_24h_change": -4.1}}
        if "alternative.me" in url:
            return {"data": [{"value": "22", "value_classification": "Extreme Fear"}]}
        return None
    bot.market._get = fake_get
    bot.market.jup.board_data = [{"mint": coin(i), "symbol": f"M{i}", "name": "meme", "launchpad": "pump.fun",
                                   "mcap": 1e6, "chg24": -20 if i < 7 else 15, "vol24": 1e5, "organic": 50,
                                   "verified": False} for i in range(10)]
    bot.runners.universe = {coin(i): {"change_h24": 150 if i < 2 else 5, "change_h1": 60 if i < 3 else 1} for i in range(20)}
    s = bot.run(bot.pulse.snapshot(NOW))
    assert s["sol_24h"] == -7.5 and s["btc_24h"] == -4.1 and s["fng"] == 22
    assert abs(s["board_up"] - 0.3) < 1e-9 and s["runners_h24"] == 2 and s["pumping_h1"] == 3
    assert bot.pulse.snapshots(NOW - 60)[0]["fng"] == 22


def test_market_guard_holds_real_buys_in_a_cold_market(bot):
    from fomo.live import LiveTrader
    live = LiveTrader(bot.cfg, bot.db, bot.rpc, None, bot.notes, keypair=None, key_problem="x")
    live.pulse = bot.pulse
    bot.pulse._report = (NOW, {"regime": "cold"})
    assert "cold market" in live.halt_reason()
    bot.cfg.set("MARKET_GUARD", "0")
    assert "cold market" not in live.halt_reason()


def test_market_command_and_brief(chat):
    out = chat.say("/market")
    assert "Market:" in out
    pulse = chat.pulse
    chat.cfg.set("MARKET_BRIEF_HOUR", str(time.localtime().tm_hour))
    before = len(chat.notify.feed)
    chat.loop.run_until_complete(pulse.tick())
    assert len(chat.notify.feed) > before                         # the morning brief went out
