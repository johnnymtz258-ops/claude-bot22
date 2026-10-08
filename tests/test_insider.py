"""The insider framework: funding paths, a watch period for new wallets, and smart money stacking on older coins."""
import time

from tests.helpers import MINT, WHALE, WHALE2, pump_buy

FRESH = "FreshSpLit111111111111111111111111111111111"[:44]
OLD = "EstabLished11111111111111111111111111111111"[:44]


def transfer_tx(src, dest, sol):
    return {"blockTime": int(time.time()), "slot": 1, "version": 0,
            "meta": {"err": None, "fee": 5000, "preBalances": [100 * 10 ** 9, 0], "postBalances": [(100 - sol) * 10 ** 9, sol * 10 ** 9],
                     "preTokenBalances": [], "postTokenBalances": [], "innerInstructions": [], "logMessages": []},
            "transaction": {"signatures": ["t"], "message": {
                "accountKeys": [{"pubkey": src, "signer": True, "writable": True}, {"pubkey": dest, "signer": False, "writable": True}],
                "instructions": [{"program": "system", "programId": "11111111111111111111111111111111",
                                  "parsed": {"type": "transfer", "info": {"source": src, "destination": dest,
                                                                          "lamports": int(sol * 10 ** 9)}}}]}}}


def test_whale_funding_a_fresh_wallet_gets_that_wallet_watched(bot):
    bot.whales.add(WHALE, "Rocket")
    bot.rpc.sigs[FRESH] = [{"signature": "a", "blockTime": 1}]                     # brand new
    bot.rpc.sigs[OLD] = [{"signature": str(i), "blockTime": 1} for i in range(40)]  # an exchange / old wallet
    bot.feed(WHALE, transfer_tx(WHALE, OLD, 20))
    bot.feed(WHALE, transfer_tx(WHALE, FRESH, 1))       # too small to matter
    assert bot.whales.get(FRESH) is None and bot.whales.get(OLD) is None
    bot.feed(WHALE, transfer_tx(WHALE, FRESH, 12))
    w = bot.whales.get(FRESH)
    assert w and w["source"] == "linked" and w["name"].startswith("Rocket→")
    assert any("Funding path" in m["text"] and "12.0 SOL" in m["text"] for m in bot.notes.sent)


def test_new_wallets_are_watched_before_their_buys_are_sent(bot):
    bot.whales.add(WHALE, "auto-new", source="auto")
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy())
    assert bot.notes.kinds() == []
    assert bot.db.scalar("select status from alerts where kind='BUY'") == "watching"
    # it proves itself: 4 winning copies since it was followed -> graduates early
    now = int(time.time())
    for i in range(4):
        bot.db.run("""insert into copies(whale,mint,open_ts,entry_price,last_price,status,return_pct,close_ts)
            values(?,?,?,1.0,1.5,'closed',50,?)""", (WHALE, f"Win{i}" + "1" * 39, now, now))
    from tests.helpers import MINT2
    bot.market.set_pair(mint=MINT2, symbol="MOIN", price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy(mint=MINT2))
    assert bot.notes.kinds() == ["BUY"]


def test_smart_money_stacking_on_an_older_coin(bot):
    bot.whales.add(WHALE, "Rocket")
    bot.whales.add(WHALE2, "auto-new", source="auto")           # watch-only, but it still counts for stacking
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000, created_ms=int((time.time() - 20 * 86400) * 1000))
    bot.feed(WHALE2, pump_buy(wallet=WHALE2))
    assert bot.notes.kinds() == []                               # the watched wallet alone: not sent
    bot.feed(WHALE, pump_buy())
    buy = [m["text"] for m in bot.notes.sent if m["kind"] == "BUY"]
    assert buy and "SMART MONEY STACKING · 2 wallets · coin 20d old" in buy[-1] and "Size: normal" in buy[-1]


def test_auto_wallets_that_never_exit_are_dropped(bot):
    now = int(time.time())
    bot.whales.add(WHALE, "auto-bags", source="auto")
    for i in range(10):
        bot.db.run("""insert into swaps(sig,wallet,is_me,ts,seen_ts,side,mint,usd_value) values(?,?,0,?,?,'BUY',?,500)""",
                   (f"b{i}", WHALE, now - 3600, now - 3600, f"Bag{i}" + "1" * 39))
    dropped = bot.run(bot.scout.prune(now))
    assert dropped and "no exits seen" in dropped[0][1]
