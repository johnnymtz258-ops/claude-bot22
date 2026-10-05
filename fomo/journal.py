"""Your trading report: where your money is actually made and lost, from your own closed trades.

Every closed coin is sorted by a few things you control — where the idea came from (a bot alert or your
own pick), how long you held, how many times you bought it, and how big you went — and the report says
in plain words which habits are costing you. Dead coins count as losses (you really lost that), coins
whose sale the bot never saw are left out (their result is unknown).
"""
from __future__ import annotations

import time

from .util import usd

BUCKETS = (
    ("source", "Where the idea came from", ("From a bot alert", "Your own pick")),
    ("hold", "How long you held", ("Under 10 minutes", "10–60 minutes", "Over 1 hour")),
    ("buys", "How many times you bought in", ("Once or twice", "3+ times (re-buying)")),
    ("size", "Trade size", ("$40 or less", "Over $40")),
)


def closed_trades(db, portfolio, days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    out = []
    for mint in portfolio.mints():
        p = portfolio.position(mint)
        if not p or p["open"] or p["bought"] <= 0 or p["last_ts"] < since:
            continue
        if p["sale_unrecorded"] and abs(p["realized"]) < 0.01:
            continue
        trades = db.rows("select side, ts from my_trades where mint=? and source<>'unrecorded' order by ts", (mint,))
        if not trades:
            continue
        first, last = trades[0]["ts"], trades[-1]["ts"]
        result = p["realized"] + (p["value"] - p["cost"] if p["written_off"] else 0.0)
        alert = db.scalar("select 1 from alerts where mint=? and kind='BUY' and ts between ? and ?",
                          (mint, first - 6 * 3600, first + 120))
        hold_min = (last - first) / 60
        buys = sum(t["side"] == "BUY" for t in trades)
        out.append({"mint": mint, "symbol": p["symbol"], "result": result, "bought": p["bought"], "first_ts": first,
                    "dead": p["written_off"],
                    "source": "From a bot alert" if alert else "Your own pick",
                    "hold": "Under 10 minutes" if hold_min < 10 else "10–60 minutes" if hold_min < 60 else "Over 1 hour",
                    "buys": "3+ times (re-buying)" if buys >= 3 else "Once or twice",
                    "size": "Over $40" if p["bought"] > 40 else "$40 or less"})
    return out


def report(db, portfolio, days: int = 30) -> dict:
    trades = closed_trades(db, portfolio, days)
    groups = []
    for key, title, labels in BUCKETS:
        rows = []
        for label in labels:
            xs = [t["result"] for t in trades if t[key] == label]
            if xs:
                rows.append({"label": label, "n": len(xs), "total": sum(xs), "won": sum(x > 0 for x in xs) / len(xs),
                             "avg": sum(xs) / len(xs)})
        groups.append({"key": key, "title": title, "rows": rows})
    total = sum(t["result"] for t in trades)
    wins = [t["result"] for t in trades if t["result"] > 0]
    losses = [t["result"] for t in trades if t["result"] <= 0]
    return {"days": days, "n": len(trades), "total": total, "won": len(wins) / len(trades) if trades else 0.0,
            "avg_win": sum(wins) / len(wins) if wins else 0.0, "avg_loss": sum(losses) / len(losses) if losses else 0.0,
            "dead": sum(t["dead"] for t in trades), "groups": groups, "lessons": lessons(groups, trades),
            "best": sorted(trades, key=lambda t: -t["result"])[:3], "worst": sorted(trades, key=lambda t: t["result"])[:3]}


def lessons(groups: list[dict], trades: list[dict]) -> list[str]:
    """Plain-language takeaways: the habit that cost the most, and what's working."""
    out = []
    rows = [(g["title"], r) for g in groups for r in g["rows"] if r["n"] >= 5]
    if not rows:
        return ["Not enough closed trades yet — the report fills in as you trade."]
    worst = min(rows, key=lambda x: x[1]["total"])
    if worst[1]["total"] < 0:
        out.append(f"Biggest leak — {worst[1]['label'].lower()}: {usd(worst[1]['total'], signed=True)} over "
                   f"{worst[1]['n']} coins ({worst[1]['won'] * 100:.0f}% won).")
    best = max(rows, key=lambda x: x[1]["avg"])
    if best[1]["avg"] > worst[1]["avg"]:
        lead = "Works best" if best[1]["avg"] > 0 else "Costs you least"
        out.append(f"{lead} — {best[1]['label'].lower()}: {usd(best[1]['avg'], signed=True)} per coin on average "
                   f"({best[1]['won'] * 100:.0f}% won).")
    by = {g["key"]: {r["label"]: r for r in g["rows"]} for g in groups}
    alert, own = by["source"].get("From a bot alert"), by["source"].get("Your own pick")
    if alert and own and alert["n"] >= 3 and own["n"] >= 5 and alert["avg"] > own["avg"]:
        out.append(f"Bot alerts did better than your own picks ({usd(alert['avg'], signed=True)} vs "
                   f"{usd(own['avg'], signed=True)} per coin).")
    long_hold = by["hold"].get("Over 1 hour")
    if long_hold and long_hold["n"] >= 5 and long_hold["won"] < 0.3:
        out.append("Losers held past an hour rarely come back — the -40% stop exists for exactly these.")
    rebuy = by["buys"].get("3+ times (re-buying)")
    if rebuy and rebuy["n"] >= 5 and rebuy["total"] < 0:
        out.append("Re-buying the same coin again and again has cost you — one entry, one exit.")
    return out
