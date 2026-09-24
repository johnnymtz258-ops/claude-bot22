import time
from adaptive_engine import position_state


def pair(price=0.0001072, liq=106000, pc5=0.5, pc1=8.0, buys=60, sells=45):
    return {
        "priceUsd": str(price), "marketCap": 1120000,
        "liquidity": {"usd": liq}, "volume": {"m5": 12000},
        "txns": {"m5": {"buys": buys, "sells": sells}},
        "priceChange": {"m5": pc5, "h1": pc1, "h6": 0, "h24": 0},
    }


def pos(amount=6.37, entry=0.00012, peak=0.00013, liq=112000):
    return {"entry_price":entry,"peak_price":peak,"entry_liquidity":liq,
            "amount_usd":amount,"open_ts":int(time.time())-600}

# v14 capital-first: even a small -10.7% dip is beyond the capped 8% hard line.
s=position_state(pair(),pos(),risk_line=10,round_trip_friction=5,exit_friction=2.5,
                 small_position_usd=15,small_hard_stop=20,mid_hard_stop=16)
assert s["state"] == "EXIT_REVIEW", s
assert s["price_hard_stop"] == 8
assert s["net_ret_est"] < s["ret"]

# Same small position, but true liquidity failure must still be urgent.
s=position_state(pair(liq=65000),pos(),risk_line=10,liquidity_exit=30,
                 small_position_usd=15,small_hard_stop=20)
assert s["state"] == "EXIT_REVIEW", s

# Deeper small-position loss still exits even without liquidity failure.
s=position_state(pair(price=0.000095, liq=106000),pos(),risk_line=10,
                 small_position_usd=15,small_hard_stop=20)
assert s["state"] == "EXIT_REVIEW", s

# A larger position is also capped by the capital-first price risk line.
s=position_state(pair(),pos(amount=100),risk_line=10,small_position_usd=15,
                 small_hard_stop=20,mid_hard_stop=16)
assert s["state"] == "EXIT_REVIEW", s
print('v11.1 fee-aware guardian tests passed')
