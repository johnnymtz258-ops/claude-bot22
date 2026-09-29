import time

import pytest

from fomo.discovery import Discovery, verdict
from tests.helpers import MINT, MINT2, WHALE, WHALE2, pump_buy, pump_sell

EARLY = "EarLyWhaLe111111111111111111111111111111111"
SNIPER = "Sniper1111111111111111111111111111111111111"
LATE = "LateBuyer11111111111111111111111111111111111"
DEV = "Dev111111111111111111111111111111111111111111"[:44]


def coin_history(bot, mint, t0):
    """A coin that launches, gets bought early by EARLY, runs 10x, and gets chased by LATE."""
    events = [  # (seconds after launch, wallet, side, sol, tokens)  price = sol/tokens
        (0, DEV, "BUY", 1.0, 30_000_000),
        (1, SNIPER, "BUY", 2.0, 50_000_000),
        (600, EARLY, "BUY", 5.0, 80_000_000),
        (900, WHALE, "BUY", 2.0, 30_000_000),
        (3600, LATE, "BUY", 20.0, 40_000_000),
        (5400, LATE, "BUY", 10.0, 12_000_000),     # near the peak
        (7200, EARLY, "SELL", 40.0, 60_000_000),
        (9000, WHALE, "SELL", 8.0, 20_000_000),
    ]
    rows = []
    for i, (dt, wallet, side, sol, tokens) in enumerate(events):
        if side == "BUY":
            tx = pump_buy(wallet=wallet, mint=mint, sol=sol, tokens=tokens, block_time=t0 + dt)
        else:
            tx = pump_sell(wallet=wallet, mint=mint, sol=sol, tokens=tokens, holding=tokens * 1.5, block_time=t0 + dt)
        tx["slot"] = 1000 + dt
        sig = f"{mint[:6]}sig{i:03d}" + "x" * 20
        bot.rpc.txs[sig] = tx
        rows.append({"signature": sig, "err": None, "blockTime": t0 + dt, "slot": 1000 + dt})
    bot.rpc.sigs[mint] = list(reversed(rows))
    bot.rpc.balances[(EARLY, mint)] = 20_000_000
    bot.rpc.balances[(WHALE, mint)] = 10_000_000


def test_find_ranks_early_buyers_and_flags_snipers(bot):
    t0 = int(time.time()) - 86400
    coin_history(bot, MINT, t0)
    bot.market.set_pair(mint=MINT, price=0.00012, mc=120_000)
    result = bot.run(Discovery(bot.rpc, bot.market, bot.db, bot.cfg).find([MINT]))
    coin = result["coins"][0]
    assert coin["ok"] and coin["from_launch"]
    wallets = [c["wallet"] for c in coin["candidates"]]
    assert LATE not in wallets                  # chased near the peak: not early
    assert wallets[0] == EARLY                  # biggest early buyer, not flagged
    sniper = next(c for c in coin["candidates"] if c["wallet"] == SNIPER)
    assert "launch sniper" in sniper["flags"]
    dev = next(c for c in coin["candidates"] if c["wallet"] == DEV)
    assert "creator" in dev["flags"]
    early = coin["candidates"][0]
    assert early["to_peak"] == pytest.approx((10 / 12_000_000) / (5 / 80_000_000), rel=1e-6)
    assert early["held_pct"] == pytest.approx(25.0)


def test_find_puts_repeat_early_buyers_first(bot):
    t0 = int(time.time()) - 86400
    coin_history(bot, MINT, t0)
    coin_history(bot, MINT2, t0)
    bot.market.set_pair(mint=MINT, price=0.00012, mc=120_000)
    bot.market.set_pair(mint=MINT2, symbol="MOIN", price=0.00012, mc=120_000)
    result = bot.run(Discovery(bot.rpc, bot.market, bot.db, bot.cfg).find([MINT, MINT2]))
    top = result["wallets"][0]
    assert top["wallet"] == EARLY and len(top["coins"]) == 2


def test_analyze_wallet_round_trips(bot):
    t0 = int(time.time()) - 5 * 86400
    rows = []
    trades = []
    for i in range(6):
        mint = f"Coin{i}" + "1" * 39
        win = i % 3 != 0
        trades.append(pump_buy(wallet=WHALE2, mint=mint, sol=1.0, tokens=1_000_000, block_time=t0 + i * 7200))
        trades.append(pump_sell(wallet=WHALE2, mint=mint, sol=2.0 if win else 0.5, tokens=1_000_000,
                                holding=1_000_000, close=True, block_time=t0 + i * 7200 + 1800))
    for i, tx in enumerate(trades):
        sig = f"w{i:03d}" + "x" * 30
        bot.rpc.txs[sig] = tx
        rows.append({"signature": sig, "err": None, "blockTime": tx["blockTime"]})
    bot.rpc.sigs[WHALE2] = list(reversed(rows))
    r = bot.run(Discovery(bot.rpc, bot.market, bot.db, bot.cfg).analyze_wallet(WHALE2))
    assert r["ok"] and r["trips"] == 6
    assert r["win_rate"] == pytest.approx(4 / 6)
    assert r["pnl_sol"] == pytest.approx(4 * 1.0 - 2 * 0.5)
    assert r["median_hold_s"] == 1800
    assert r["verdict"].startswith("✅")


def test_verdicts():
    base = dict(trips=10, median_hold_s=600, buys_per_day=10, pnl_sol=5, win_rate=0.5, median_entry_mc=400_000)
    assert verdict(base)[0].startswith("✅")
    assert verdict({**base, "median_hold_s": 20})[0].startswith("🤖")
    assert verdict({**base, "pnl_sol": -1})[0].startswith("❌")
    assert verdict({**base, "trips": 2})[0].startswith("🆕")
