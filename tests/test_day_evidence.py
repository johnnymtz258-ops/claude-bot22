"""The evidence rules: a setup must beat luck, counted by whole days, to be called promising or proven."""
import random

from daybot.backtest import NO_EDGE, _stats, grade
from daybot.stats import clustered_mean_ci, sharpe_ci


def _trades(per_day, mean, sd, days=60, seed=1, pct_scale=0.3):
    rnd = random.Random(seed)
    out = []
    for d in range(days):
        for k in range(per_day):
            r = rnd.gauss(mean, sd)
            out.append({"setup": "x", "direction": "long", "symbol": "A", "day": "2026-01-%02d" % (d % 28 + 1) + f"-{d}",
                        "ts": d * 1000 + k, "r": r, "pct": r * pct_scale})
    return out


def test_a_real_edge_has_a_confidence_range_above_zero():
    ci = clustered_mean_ci([(t["day"], t["r"]) for t in _trades(8, 0.25, 1.0)])
    assert ci["lo"] > 0 and ci["p_pos"] > 0.95 and ci["lo"] < ci["mean"] < ci["hi"]


def test_noise_is_not_called_an_edge():
    wrong = 0
    for seed in range(60):
        ci = clustered_mean_ci([(t["day"], t["r"]) for t in _trades(8, 0.0, 1.0, seed=seed)], boot=300)
        wrong += ci["lo"] > 0
    assert wrong <= 12                                   # a 10% one-sided tail: about 6 of 60 by chance


def test_trades_on_the_same_day_count_as_one_piece_of_evidence():
    # 400 trades that all win or lose together by day: 20 independent days, so the range must be wide
    rows = []
    rnd = random.Random(3)
    for d in range(20):
        shock = rnd.gauss(0.05, 1.0)
        rows += [(f"d{d}", shock + rnd.gauss(0, 0.05)) for _ in range(20)]
    wide = clustered_mean_ci(rows)
    naive_half_width = 1.28 * (sum((v - sum(x for _, x in rows) / 400) ** 2 for _, v in rows) / 400) ** 0.5 / 20
    assert wide["hi"] - wide["lo"] > 3 * naive_half_width


def test_grade_wants_a_real_average_not_a_lucky_one():
    base = {"n": 500, "half1": 0.1, "half2": 0.1, "no_best": 0.05}
    assert grade({**base, "avg_r": 0.12, "ci_lo": 0.03, "p_pos": 0.97}) == "proven"
    assert grade({**base, "avg_r": 0.12, "ci_lo": -0.02, "p_pos": 0.9}) == "promising"
    assert grade({**base, "avg_r": 0.02, "ci_lo": -0.05, "p_pos": 0.65}) == "flat"      # +0.02R is zero to the eye
    assert grade({**base, "avg_r": -0.04, "ci_lo": -0.1, "p_pos": 0.1}) == "negative"
    assert grade({**base, "n": 10, "avg_r": 0.5}) == "thin"
    lottery = {**base, "avg_r": 0.14, "no_best": -0.05, "ci_lo": 0.01, "p_pos": 0.9}     # lives on a few big winners
    assert grade(lottery) == "promising"
    assert {"flat", "negative", "benched"} <= NO_EDGE and "promising" not in NO_EDGE


def test_stats_report_what_a_trade_earns_in_percent_of_the_money():
    st = _stats(_trades(8, 0.2, 1.0, days=80, seed=4))
    assert abs(st["avg_pct"] - 0.3 * st["avg_r"]) < 1e-9
    assert st["pct_lo"] < st["avg_pct"] < st["pct_hi"] and st["list"][0][3] == round(st["list"][0][2] * 0.3, 3)
    assert st["ci_lo"] < st["avg_r"] < st["ci_hi"]


def test_sharpe_ci_brackets_the_estimate():
    rnd = random.Random(2)
    xs = [rnd.gauss(0.001, 0.01) for _ in range(750)]
    s = sharpe_ci(xs)
    assert s["lo"] < s["sharpe"] < s["hi"]
