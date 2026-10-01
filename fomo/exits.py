"""Help with the hardest part: when to sell.

Three pieces, all built on a recorded price path (`price_marks`, one reading per coin per minute
while the bot is watching it — including 24h after you sell, to see what you missed):

1. Exit lab — replays every measured whale copy under different exit styles and shows which one
   would actually have made the most on *your* whales' picks:
     whale   sell when the whale sells (what copies do today)
     all2x   sell everything at 2x
     half2x  sell half at 2x, the rest when the whale sells
     trail   once it's 2x, sell if it falls 35% from its peak
     ladder  sell 1/3 at 2x, 1/3 at 3x, trail the last third 35% from its peak
2. Your habits — for each of your sells: how much higher it went in the next 24h (sold too
   early) and how far below the best price you'd seen you sold (sold too late).
3. Exit coach — for each open position: multiple now vs its peak, whales still in, and what the
   ladder / protector would say. The optional nudges (ladder levels, profit protector) are sent
   by the tracker using `coach()`.
"""
from __future__ import annotations

import statistics
import time

from .copies import return_pct
from .util import num

MARK_EVERY = 60
GLITCH = 8.0
LADDER_LEVELS = (2.0, 3.0, 5.0, 10.0)

RULES = [
    ("whale", "Sell when the whale sells"),
    ("all2x", "Sell everything at 2x"),
    ("half2x", "Sell half at 2x, rest with the whale"),
    ("trail", "After 2x, sell if it drops 35% from its peak"),
    ("ladder", "⅓ at 2x, ⅓ at 3x, trail the rest 35%"),
]


class MarkRecorder:
    """Stores at most one price per coin per minute, ignoring single-reading feed glitches."""

    def __init__(self, db):
        self.db = db
        self._last: dict[str, tuple[int, float]] = {}
        self._pending: dict[str, float] = {}

    def record(self, mint: str, price: float, ts: int) -> bool:
        if price <= 0:
            return False
        last = self._last.get(mint)
        if last is None:
            row = self.db.row("select ts, price from price_marks where mint=? order by ts desc limit 1", (mint,))
            last = (int(row["ts"]), num(row["price"])) if row else None
        if last and ts - last[0] < MARK_EVERY:
            return False
        if last and (price / last[1] >= GLITCH or last[1] / price >= GLITCH):
            pending = self._pending.get(mint)
            if not pending or not 0.8 <= price / pending <= 1.25:
                self._pending[mint] = price  # wait for a second reading that agrees
                return False
        self._pending.pop(mint, None)
        self._last[mint] = (ts, price)
        self.db.run("insert or ignore into price_marks(mint,ts,price) values(?,?,?)", (mint, ts, price))
        return True


def marks(db, mint: str, start: int, end: int) -> list[tuple[int, float]]:
    return [(int(r["ts"]), num(r["price"])) for r in db.rows(
        "select ts, price from price_marks where mint=? and ts>=? and ts<=? order by ts", (mint, start, end))]


# -- 1. exit lab -------------------------------------------------------------------------------------

def simulate(entry: float, path: list[float], whale_ret: float, fee_pct: float, trail_pct: float = 35.0) -> dict:
    """Net % return of each exit style for one copy.

    `path` is the price sequence while the copy was open; `whale_ret` is what following the whale
    returned. Whatever a style hasn't sold by the end is treated as sold with the whale.
    """
    keep = (1 - fee_pct / 100) ** 2
    whale_value = 1 + whale_ret / 100
    out = {"whale": whale_ret}

    def finish(sold_value: float, remaining: float) -> float:
        return (sold_value + remaining * whale_value - 1) * 100

    # all2x / half2x
    hit2 = next((p for p in path if p >= 2 * entry), None)
    out["all2x"] = (keep * hit2 / entry - 1) * 100 if hit2 else whale_ret
    out["half2x"] = finish(0.5 * keep * hit2 / entry, 0.5) if hit2 else whale_ret

    def trailing(pieces: list[tuple[float, float]]) -> float:
        """pieces: [(multiple, fraction)] sold at those multiples; the rest trails after 2x."""
        remaining, value, armed, peak = 1.0, 0.0, False, 0.0
        todo = sorted(pieces)
        for p in path:
            x = p / entry
            while todo and x >= todo[0][0]:
                level, frac = todo.pop(0)
                value += frac * keep * p / entry
                remaining -= frac
            if x >= 2:
                armed = True
            peak = max(peak, p)
            if armed and remaining > 1e-9 and p <= peak * (1 - trail_pct / 100):
                value += remaining * keep * p / entry
                remaining = 0.0
                break
        return finish(value, remaining)

    out["trail"] = trailing([])
    out["ladder"] = trailing([(2.0, 1 / 3), (3.0, 1 / 3)])
    return out


def exit_lab(db, cfg, days: int = 30) -> dict:
    """Average return of each exit style over recent whale copies that have a recorded path."""
    now = int(time.time())
    fee = cfg.get("COPY_FEE_PCT")
    results = {key: [] for key, _ in RULES}
    used = 0
    for c in db.rows("""select * from copies where whale<>'runner' and open_ts>=? and
            (status='closed' or open_ts<=?)""", (now - days * 86400, now - 86400)):
        end = int(c["close_ts"]) if c["status"] == "closed" and c["close_ts"] else now
        path = [p for _, p in marks(db, c["mint"], int(c["open_ts"]), end)]
        if len(path) < 3 or num(c["entry_price"]) <= 0:
            continue
        whale_ret = num(c["return_pct"]) if c["status"] == "closed" else return_pct(c, num(c["last_price"]), fee)
        for key, value in simulate(num(c["entry_price"]), path, min(whale_ret, 1000), fee).items():
            results[key].append(min(value, 1000))
        used += 1
    rules = []
    for key, label in RULES:
        vals = results[key]
        rules.append({"key": key, "label": label, "n": len(vals),
                      "avg": sum(vals) / len(vals) if vals else 0.0,
                      "median": statistics.median(vals) if vals else 0.0,
                      "win_rate": sum(v > 0 for v in vals) / len(vals) if vals else 0.0})
    best = max(rules, key=lambda r: r["avg"]) if used else None
    return {"copies": used, "days": days, "rules": rules, "best": best}


# -- 2. your habits ------------------------------------------------------------------------------

def my_exit_habits(db, days: int = 30) -> dict:
    """How much higher your coins went after you sold, and how far below the peak you sold."""
    now = int(time.time())
    sells = []
    for t in db.rows("select * from my_trades where side='SELL' and ts>=? and price_usd>0 order by ts desc",
                     (now - days * 86400,)):
        start = int(db.scalar("""select max(ts) from my_trades where mint=? and side='BUY' and ts<=?""",
                              (t["mint"], t["ts"]), default=0) or 0)
        first_buy = int(db.scalar("select min(ts) from my_trades where mint=? and side='BUY'", (t["mint"],),
                                  default=start) or start)
        before = marks(db, t["mint"], first_buy, int(t["ts"]))
        after = marks(db, t["mint"], int(t["ts"]) + 1, int(t["ts"]) + 86400)
        price = num(t["price_usd"])
        row = {"mint": t["mint"], "ts": t["ts"], "usd": num(t["usd"]), "price": price, "mc": num(t["mc_usd"])}
        if len(before) >= 3:
            peak_before = max(p for _, p in before)
            row["below_peak_pct"] = max(0.0, (1 - price / peak_before) * 100) if peak_before > 0 else 0.0
        if len(after) >= 3 and (now - int(t["ts"]) >= 6 * 3600 or len(after) >= 60):
            row["after_gain_pct"] = (max(p for _, p in after) / price - 1) * 100
            row["after_low_pct"] = (min(p for _, p in after) / price - 1) * 100
        sells.append(row)
    early = [s["after_gain_pct"] for s in sells if "after_gain_pct" in s]
    late = [s["below_peak_pct"] for s in sells if "below_peak_pct" in s]
    result = {
        "sells": sells[:50], "measured_after": len(early), "measured_before": len(late),
        "median_after_gain": statistics.median(early) if early else 0.0,
        "median_below_peak": statistics.median(late) if late else 0.0,
        "too_early": sum(g >= 50 for g in early), "too_late": sum(b >= 35 for b in late),
    }
    result["advice"] = habit_advice(result)
    return result


def habit_advice(h: dict) -> list[str]:
    tips = []
    if h["measured_after"] >= 3 and h["median_after_gain"] >= 40:
        tips.append(f"You tend to sell too early: after you sell, your coins go another "
                    f"+{h['median_after_gain']:.0f}% (median) within a day. Sell in thirds instead of all at "
                    f"once and let the last third ride while the whales hold.")
    if h["measured_before"] >= 3 and h["median_below_peak"] >= 30:
        tips.append(f"You tend to sell too late: you sell {h['median_below_peak']:.0f}% below the best price "
                    f"you had (median). The profit protector warns you when a 2x+ coin gives back that much.")
    if not tips and h["measured_after"] + h["measured_before"] >= 5:
        tips.append("Your exit timing is balanced — no strong early or late pattern yet.")
    if not tips:
        tips.append("Not enough sells with a recorded price path yet. This fills in as the bot watches your coins.")
    return tips


# -- 3. exit coach -----------------------------------------------------------------------------------

def coach(db, cfg, position: dict, holders: list[dict], now: int | None = None) -> dict:
    """Where an open position stands and what the ladder / protector would say."""
    now = int(now or time.time())
    cost, tokens, price = num(position.get("cost")), num(position.get("tokens")), num(position.get("price"))
    if cost <= 0 or tokens <= 0 or price <= 0:
        return {"multiple": 0.0, "peak_multiple": 0.0, "from_peak_pct": 0.0, "hint": "", "whales_in": 0,
                "scalp": False, "protect_after": 0, "trail": 100}
    entry = cost / tokens
    since = int(position.get("episode_ts") or position.get("first_ts") or now)
    path = marks(db, position["mint"], since, now)
    peak = max([p for _, p in path] + [price])
    multiple, peak_multiple = price / entry, peak / entry
    from_peak = (1 - price / peak) * 100 if peak > 0 else 0.0
    whales_in = sum(1 for h in holders if h["still_in"])
    protect_after, trail = cfg.get("PROTECT_AFTER_X"), cfg.get("PROTECT_TRAIL_PCT")
    # SCALP coins (micro-caps, whales whose coins usually die) pump fast and give it all back: protect sooner
    scalp = bool(db.scalar("select 1 from alerts where mint=? and kind='BUY' and scalp=1 and ts>=?",
                           (position["mint"], now - 2 * 86400)))
    if scalp:
        protect_after = min(protect_after, cfg.get("SCALP_PROTECT_AFTER_X")) if protect_after > 0 else 0
        trail = min(trail, cfg.get("SCALP_TRAIL_PCT"))
    if protect_after > 0 and peak_multiple >= protect_after and from_peak >= trail:
        hint = f"Gave back {from_peak:.0f}% from its {peak_multiple:.1f}x peak — protect what's left"
    elif scalp and multiple >= 1.5:
        hint = f"{multiple:.1f}x on a scalp coin — these usually fade, take some"
    elif multiple >= 2:
        hit = [lv for lv in LADDER_LEVELS if multiple >= lv][-1]
        hint = f"{multiple:.1f}x — ladder says a slice at {hit:g}x" + (", whales still in" if whales_in else "")
    elif multiple < 1 and whales_in:
        hint = f"Down {100 - multiple * 100:.0f}% but {whales_in} whale{'s' if whales_in > 1 else ''} still in — dips are normal"
    elif holders and not whales_in:
        hint = "All tracked whales are out"
    else:
        hint = "Hold while the whales hold"
    return {"multiple": multiple, "peak_multiple": peak_multiple, "from_peak_pct": from_peak,
            "whales_in": whales_in, "hint": hint, "entry_price": entry, "peak_price": peak,
            "scalp": scalp, "protect_after": protect_after, "trail": trail}


# -- 4. hold plan (shown on buy alerts) ------------------------------------------------------------

def _whale_hold_seconds(db, whale: str) -> float:
    """Median time from a whale's first buy of a coin to when it had fully sold (observed trades)."""
    holds = []
    for r in db.rows("""select mint, min(case when side='BUY' then ts end) first_buy,
            min(case when side='SELL' and holding_after<=0 then ts end) out_ts
            from swaps where wallet=? group by mint""", (whale,)):
        if r["first_buy"] and r["out_ts"] and r["out_ts"] > r["first_buy"]:
            holds.append(r["out_ts"] - r["first_buy"])
    return statistics.median(holds) if len(holds) >= 3 else 0.0


def hold_plan(db, whale: str) -> dict | None:
    """How long this whale's picks take to play out, from measured copies (pooled if the whale is new)."""
    def profile(rows):
        winners = [r for r in rows if num(r["entry_price"]) > 0 and num(r["peak_price"]) >= 1.2 * num(r["entry_price"])
                   and num(r["peak_ts"]) > num(r["open_ts"])]
        if len(rows) < 4 or len(winners) < 3:
            return None
        p15 = [num(r["p15m"]) / num(r["entry_price"]) for r in rows if r["p15m"] and num(r["entry_price"]) > 0]
        return {"n": len(rows), "winners": len(winners),
                "time_to_peak_s": statistics.median(num(r["peak_ts"]) - num(r["open_ts"]) for r in winners),
                "peak_x": statistics.median(num(r["peak_price"]) / num(r["entry_price"]) for r in winners),
                "at_15m_pct": (statistics.median(p15) - 1) * 100 if len(p15) >= 3 else None}

    # picks younger than an hour haven't played out yet
    query = """select * from copies where {} and open_ts>=? and open_ts<=? and entry_price>0
        order by open_ts desc limit 60"""
    now = int(time.time())
    window = (now - 60 * 86400, now - 3600)
    plan = profile(db.rows(query.format("whale=?"), (whale, *window)))
    scope = "this whale" if plan else ""
    if not plan and whale != "runner":
        plan = profile(db.rows(query.format("whale<>'runner'"), window))
        scope = "your whales overall" if plan else ""
    if not plan:
        return None
    plan["scope"] = scope
    plan["whale_hold_s"] = _whale_hold_seconds(db, whale) if whale != "runner" else 0.0
    return plan


def hold_plan_line(plan: dict | None) -> str:
    from .util import dur
    if not plan:
        return "⏱ Hold plan: not enough history yet — the bot learns this whale's timing as it trades."
    t = plan["time_to_peak_s"]
    line = (f"⏱ Hold plan ({plan['scope']}, {plan['n']} picks): winners peaked ~{dur(t)} after the buy at "
            f"~{plan['peak_x']:.1f}x. Try to hold at least ~{dur(t / 2)}")
    if plan.get("at_15m_pct") is not None:
        line += f"; if you must leave early, at 15m picks were typically {plan['at_15m_pct']:+.0f}%"
    line += "."
    if plan.get("whale_hold_s"):
        line += f" This whale usually holds ~{dur(plan['whale_hold_s'])}."
    return line
