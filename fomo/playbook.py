"""Per-whale playbook: the exit plan that actually made money copying THIS whale.

One exit rule doesn't fit every whale. "Leave when the whale leaves" is right for holders, but a flipper leaves
within minutes — before most of its coins run — so following its exit throws away the winners. For each whale the
bot replays its past copies (entered at the price you could get, fees included) on the recorded price path under a
few plans and keeps the one that won. A whale blocked under the default rules is sent again when one of these plans
is clearly profitable on it, and its alerts carry that plan.

From your data: copying 7yuq (a flipper, blocked) and selling everything at 2x with a -30% stop, IGNORING its own
sells, averaged 1.26x over 46 coins (median 1.98x, 52% won, 1.25x even without its best coin).
"""
from __future__ import annotations

import json
import statistics
import time

FEE = 0.02            # 1% each way
MIN_COPIES = 8        # fewer recorded copies than this: not enough evidence to unblock a whale
MIN_HISTORY_COPIES = 6   # a scanner candidate's on-chain buys replayed on minute candles
MIN_AVG_WO_BEST = 1.10
MIN_MEDIAN = 1.0
REFRESH = 3 * 3600

PLANS = {
    "ride": {"label": "sell half at 2x, trail the rest (out if it falls back below 1.4x), -40% stop, ignore the whale's sells",
             "tp": 2.0, "half": True, "stop": 0.6, "hold": 24 * 3600, "follow_whale": False},
    "quick": {"label": "sell everything at 2x, -30% stop, out after 6h, ignore the whale's sells",
              "tp": 2.0, "half": False, "stop": 0.7, "hold": 6 * 3600, "follow_whale": False},
}


def simulate(entry: float, path: list[tuple[int, float]], plan: dict) -> float:
    """Multiple of your money after fees for one copy that bought at `entry`, walking the price path."""
    if entry <= 0 or not path:
        return 0.0
    t0, got, left = path[0][0], 0.0, 1.0
    tp, stop = plan["tp"], plan["stop"]
    for ts, price in path:
        x = price / entry
        if ts - t0 > plan["hold"]:
            return got + left * x - FEE
        if left == 1.0 and x >= tp:
            if not plan["half"]:
                return tp - FEE
            got, left = 0.5 * tp, 0.5
            continue
        if left == 0.5 and x <= tp * 0.7:          # the rest gives back 30% from the take-profit level
            return got + 0.5 * x - FEE
        if x <= stop:
            return got + left * x - FEE
    return got + left * path[-1][1] / entry - FEE


def _paths(db, whale: str) -> list[tuple[float, list]]:
    out = []
    for c in db.rows("select mint, open_ts, entry_price from copies where whale=? and entry_price>0 "
                     "and open_ts<=? order by open_ts desc limit 80", (whale, int(time.time()) - 3600)):
        pts = [(r["ts"], r["price"]) for r in db.rows(
            "select ts, price from price_marks where mint=? and ts>=? and ts<=? order by ts",
            (c["mint"], c["open_ts"], c["open_ts"] + 24 * 3600)) if r["price"] > 0]
        if len(pts) >= 5:
            out.append((c["entry_price"], pts))
    return out


def evaluate_trades(trades: list[tuple[float, list]], min_copies: int = MIN_COPIES) -> dict:
    """Best plan over [(entry price, price path)]: {plan, label, n, avg, median, avg_wo_best, won, ok}."""
    if len(trades) < min_copies:
        return {"plan": "", "n": len(trades), "ok": False}
    best = None
    for name, plan in PLANS.items():
        r = sorted(simulate(e, p, plan) for e, p in trades)
        res = {"plan": name, "label": plan["label"], "n": len(r), "avg": statistics.mean(r),
               "median": statistics.median(r), "avg_wo_best": statistics.mean(r[:-1]),
               "won": sum(x > 1 for x in r) / len(r)}
        res["ok"] = res["avg_wo_best"] >= MIN_AVG_WO_BEST and res["median"] >= MIN_MEDIAN
        key = (res["ok"], min(res["avg_wo_best"], res["median"] + 0.2))
        if best is None or key > best[0]:
            best = (key, res)
    return best[1]


def evaluate(db, whale: str) -> dict:
    """Best plan for this whale from the copies the bot recorded."""
    return evaluate_trades(_paths(db, whale))


def from_trips(trips: list[dict], paths: dict, delay: float) -> list[tuple[float, list]]:
    """A wallet's on-chain buys as copies you could have made `delay` seconds later (used by the scanner)."""
    out = []
    for t in trips:
        pts = [(ts, p) for ts, p in paths.get(t["mint"]) or [] if ts >= t["buy_ts"] + delay and p > 0]
        if len(pts) >= 5 and pts[0][0] <= t["buy_ts"] + delay + 240:
            out.append((pts[0][1], pts))
    return out


def get(db, whale: str, fresh: bool = False) -> dict:
    """Cached for a few hours (replaying every copy is a few thousand price points)."""
    raw = db.get_meta(f"playbook:{whale}")
    if raw and not fresh:
        try:
            saved = json.loads(raw)
            if time.time() - saved.get("ts", 0) < REFRESH:
                return saved
        except ValueError:
            pass
    res = {**evaluate(db, whale), "ts": int(time.time())}
    db.set_meta(f"playbook:{whale}", json.dumps(res))
    return res


def plan_for(db, whale: str) -> dict | None:
    """The plan to trade this whale with, or None for the default (follow the whale's exit)."""
    raw = db.get_meta(f"playbook:{whale}")
    try:
        pb = json.loads(raw) if raw else {}
    except ValueError:
        return None
    return PLANS.get(pb.get("plan", "")) if pb.get("ok") else None


def line(pb: dict) -> str:
    return (f"📘 Playbook for this whale: {pb['label']} — on its last {pb['n']} copies that averaged "
            f"{pb['avg']:.2f}x (median {pb['median']:.2f}x, {pb['won'] * 100:.0f}% won)")
