"""The coach: what your own trades say about where the money goes.

Every closed round trip carries tags from the moment you opened it (time of day, what the trade check flagged,
whether it was one of the bot's setups) and from how it ended (held past the stop, sold a runner early, how long
you held). The coach groups them and ranks the leaks by dollars — the biggest leak is usually one habit.
"""
from __future__ import annotations

import time

from .bars import H, L, T
from .clock import at, et, hhmm
from .setups import label
from .util import esc, loads, money, num

FLAG_TEXT = {
    "chasing": "chasing extended moves", "against_vwap": "fighting VWAP", "first5": "the first 5 minutes",
    "lunch": "lunch-hour trades", "late": "the last 15 minutes", "extended_hours": "pre/after-hours trades",
    "revenge": "revenge trades", "after_streak": "trades right after a losing streak", "oversized": "oversized trades",
    "over_daily_limit": "trades after the daily loss limit", "overtrading": "trades past your daily trade limit",
    "lottery": "lottery-ticket options", "zero_dte": "same-day-expiry options", "zero_dte_late": "0DTE after 2:30pm",
    "bad_news": "buying into bad news", "weak_market": "trading against the market", "low_volume": "low-volume stocks",
    "averaging_down": "averaging down", "spread": "wide-spread stocks", "option_spread": "wide-spread options",
    "low_oi": "thin options", "big_move_needed": "options needing a bigger move than expected", "penny": "sub-$5 stocks",
    "at_level": "entries right into a level", "earnings": "trades into earnings", "high_iv": "expensive (high IV) options",
    "extended": "stocks already far up/down on the day", "thin": "thinly traded stocks",
}
BUCKETS = (("premarket", "04:00", "09:30"), ("the open (9:30-10)", "09:30", "10:00"), ("the morning (10-11:30)", "10:00", "11:30"),
           ("midday (11:30-2)", "11:30", "14:00"), ("the afternoon (2-4)", "14:00", "16:00"), ("after hours", "16:00", "20:00"))


def bucket(ts: float) -> str:
    t = hhmm(ts)
    for name, a, b in BUCKETS:
        if a <= t < b:
            return name
    return "overnight"


def hold_bucket(seconds: float) -> str:
    m = seconds / 60
    return ("held < 5 min" if m < 5 else "held 5-30 min" if m < 30 else "held 30 min-2h" if m < 120
            else "held 2h+ (same day)" if m < 600 else "held overnight+")


async def excursion(data, market, t: dict) -> dict | None:
    """How far the stock went for and against you while you held, and what it did in the hour after you left."""
    und = t["underlying"]
    start, end = int(num(t["opened_ts"])), int(num(t["closed_ts"]) or time.time())
    bars = [b for b in data.bars1(und) if start - 60 <= b[T] <= end + 3600]
    if (not bars or bars[0][T] > start) and data.ok:
        try:
            got = await data.alpaca.bars([und], "1Min", start - 60, min(end + 3600, time.time()))
            bars = got.get(und) or []
        except Exception:
            bars = []
    held = [b for b in bars if start - 60 <= b[T] <= end]
    after = [b for b in bars if end < b[T] <= end + 3600]
    if not held:
        return None
    bullish = t["direction"] == "long"
    if t["kind"] == "option":
        bullish = (t["option_type"] or "C").upper().startswith("C") == (t["direction"] == "long")
    s = 1 if bullish else -1
    entry = num(t["und_entry"]) or (num(t["entry_avg"]) if t["kind"] == "stock" else held[0][1])
    stop = num(t["plan_stop"])
    risk = abs(entry - stop) if stop else entry * 0.005
    if risk <= 0:
        return None
    fav = (max(b[H] for b in held) - entry) if s > 0 else (entry - min(b[L] for b in held))
    adv = (entry - min(b[L] for b in held)) if s > 0 else (max(b[H] for b in held) - entry)
    exit_und = held[-1][4] if t["kind"] == "option" else num(t["exit_avg"])
    done_r = s * (exit_und - entry) / risk
    mfe_r, mae_r = fav / risk, adv / risk
    notes, tags = [], {}
    pnl = num(t["pnl"])
    if stop and done_r < -1.25 and mae_r > 1.25:
        notes.append(f"⚠️ You held past your stop: it got to −{mae_r:.1f}R against you, the plan was −1R. That's where "
                     "small losses turn big.")
        tags["held_past_stop"] = 1
    elif pnl < 0 and done_r > -0.9 and stop:
        notes.append("👍 Cut it before the stop — losses like this are the cost of doing business.")
    if pnl > 0 and mfe_r >= 2 and done_r < 1:
        notes.append(f"It reached +{mfe_r:.1f}R while you held; you took +{max(done_r, 0):.1f}R. The plan's half-at-1R "
                     "and trail would have kept more.")
        tags["left_money"] = 1
    if after:
        post = (max(b[H] for b in after) - exit_und) if s > 0 else (exit_und - min(b[L] for b in after))
        post_bad = (exit_und - min(b[L] for b in after)) if s > 0 else (max(b[H] for b in after) - exit_und)
        if pnl > 0 and post / risk >= 1.5:
            notes.append(f"After you sold it went another +{post / risk:.1f}R within the hour.")
        elif post_bad / risk >= 1.0 and pnl >= 0:
            notes.append(f"Good exit: it dropped {post_bad / risk:.1f}R within the hour after you left.")
    return {"mfe_r": mfe_r, "mae_r": mae_r, "done_r": done_r, "notes": notes, "tags": tags}


def _trades(db, since: int, until: int | None = None) -> list[dict]:
    until = until or int(time.time()) + 86400
    rows = db.rows("""select * from trades where status='closed' and closed_ts>=? and closed_ts<?
                      and account_id not in (select id from accounts where checks=0) order by closed_ts""", (since, until))
    for r in rows:
        r["tagd"] = loads(r["tags"], {}) or {}
    # exits DayBot never saw (no real P&L) and holdings from before it (investing, not day trading) teach nothing here
    return [r for r in rows if not r["tagd"].get("gap") and not r["tagd"].get("holding")]


def _groups(rows: list[dict]) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = {}

    def put(name, r):
        g.setdefault(name, []).append(r)
    for r in rows:
        tg = r["tagd"]
        put(f"Trades in {tg.get('bucket') or bucket(r['opened_ts'])}", r)
        if r["kind"] == "option":
            kind = "calls" if (r["option_type"] or "C").upper().startswith("C") else "puts"
            put(f"Options: {kind}", r)
            dte = tg.get("dte")
            if dte is not None:
                put("Options: same-day expiry (0DTE)" if num(dte) == 0 else "Options: 1-3 days to expiry"
                    if num(dte) <= 3 else "Options: 4+ days to expiry", r)
        else:
            put("Stocks: long" if r["direction"] == "long" else "Stocks: short", r)
        for flag in tg.get("flags") or []:
            if flag in FLAG_TEXT:
                put(f"Flagged: {FLAG_TEXT[flag]}", r)
        setup = tg.get("setup") or r.get("setup") or "own"
        if setup == "own":
            put("Your own ideas (no bot setup)", r)
        else:
            put("Taken on a bot setup alert", r)
            st, _, dr = setup.partition("_")
            put(f"Bot setup: {label(st, dr)}", r)
        v = tg.get("verdict")
        if v:
            put({"avoid": "Trades the check marked 🔴 AVOID", "careful": "Trades the check marked 🟡 CAREFUL",
                 "clean": "Trades the check marked 🟢 CLEAN"}.get(v, v), r)
        put(hold_bucket(num(r["closed_ts"]) - num(r["opened_ts"])).capitalize(), r)
        if tg.get("held_past_stop"):
            put("Held past the stop", r)
        if tg.get("left_money"):
            put("Sold a runner early", r)
        put(f"Ticker: {r['underlying']}", r)
    return g


def summary(rows: list[dict]) -> dict:
    pnl = [num(r["pnl"]) for r in rows]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    return {"n": len(pnl), "pnl": sum(pnl), "won": (len(wins) / len(pnl)) if pnl else 0.0,
            "avg_win": (sum(wins) / len(wins)) if wins else 0.0, "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
            "pf": (sum(wins) / abs(sum(losses))) if losses else (99.0 if wins else 0.0),
            "per_trade": (sum(pnl) / len(pnl)) if pnl else 0.0}


def leaks(db, days: int = 30, now: float | None = None, min_n: int = 3) -> dict:
    now = now or time.time()
    rows = _trades(db, int(now - days * 86400))
    total = summary(rows)
    out = []
    for name, rs in _groups(rows).items():
        if len(rs) < min_n or name.startswith("Ticker:") and len(rs) < 4:
            continue
        st = summary(rs)
        out.append({"name": name, **st})
    bad = sorted([g for g in out if g["pnl"] < 0], key=lambda g: g["pnl"])
    good = sorted([g for g in out if g["pnl"] > 0], key=lambda g: -g["pnl"])
    red = [r for r in rows if (r["tagd"].get("verdict") == "avoid")]
    without_red = total["pnl"] - sum(num(r["pnl"]) for r in red)
    return {"total": total, "leaks": bad[:8], "edges": good[:6], "days": days, "red_n": len(red),
            "without_red": without_red}


def format_leaks(res: dict) -> str:
    t = res["total"]
    if not t["n"]:
        return ("📒 No closed trades in the journal yet. Once your account is connected (/connect), every trade you "
                "close lands here and the coach starts finding patterns after a handful.")
    lines = [f"<b>📒 Your last {res['days']} days: {t['n']} trades, {money(t['pnl'], True)}</b>",
             f"Won {t['won'] * 100:.0f}% · avg win {money(t['avg_win'])} · avg loss {money(t['avg_loss'])} · "
             f"profit factor {t['pf']:.2f} · {money(t['per_trade'], True)} a trade"]
    if res["leaks"]:
        lines.append("\n<b>Where the money leaks</b>")
        for g in res["leaks"]:
            lines.append(f"🔻 {esc(g['name'])}: {g['n']} trades, {money(g['pnl'], True)} ({g['won'] * 100:.0f}% won)")
    if res["edges"]:
        lines.append("\n<b>What works for you</b>")
        for g in res["edges"]:
            lines.append(f"🔹 {esc(g['name'])}: {g['n']} trades, {money(g['pnl'], True)} ({g['won'] * 100:.0f}% won)")
    if res["red_n"]:
        lines.append(f"\nWithout the {res['red_n']} trades the check marked 🔴 you'd be at "
                     f"{money(res['without_red'], True)} instead of {money(t['pnl'], True)}.")
    if res["leaks"]:
        lines.append(f"\n🎯 One thing to fix: {esc(res['leaks'][0]['name'].lower())}.")
    return "\n".join(lines)


def day_report(db, ledger, edge, day: str, account: float = 0.0, cfg=None) -> str:
    start = at(day, "00:00")
    rows = _trades(db, start, start + 86400)
    d = et(start).strftime("%a %b %d")
    lines = [f"<b>📒 Day report · {d}</b>"]
    opened = db.rows("select * from trades where day=? and tags not like '%\"holding\":1%'", (day,))
    for r in opened:
        r["tagd"] = loads(r["tags"], {}) or {}
    if rows:
        st = summary(rows)
        best = max(rows, key=lambda r: num(r["pnl"]))
        worst = min(rows, key=lambda r: num(r["pnl"]))
        lines.append(f"{st['n']} trades closed: {money(st['pnl'], True)} ({int(round(st['won'] * st['n']))} won, "
                     f"{st['n'] - int(round(st['won'] * st['n']))} lost)")
        lines.append(f"Best {esc(best['underlying'])} {money(best['pnl'], True)} · worst {esc(worst['underlying'])} "
                     f"{money(worst['pnl'], True)}")
        if account and cfg and st["pnl"] <= -account * cfg.get("DAILY_MAX_LOSS_PCT") / 100:
            lines.append(f"🛑 Daily loss limit ({money(-account * cfg.get('DAILY_MAX_LOSS_PCT') / 100)}) was hit.")
    else:
        lines.append("No trades closed today. Sitting out is a position too.")
    still = [r for r in opened if r["status"] == "open"]
    if still:
        lines.append("Still open: " + ", ".join(f"{esc(r['symbol'] if r['kind'] == 'stock' else r['underlying'] + ' option')} "
                                                f"{r['direction']} {num(r['open_qty']):g}" for r in still))
    if opened or rows:
        broken = {}
        for r in {x["id"]: x for x in opened + rows}.values():
            for f in r["tagd"].get("flags") or []:
                broken.setdefault(f, []).append(r)
        rules = []
        for flag in ("over_daily_limit", "revenge", "after_streak", "oversized", "overtrading", "zero_dte_late",
                     "chasing", "averaging_down"):
            if flag in broken:
                rs = broken[flag]
                closed_pnl = sum(num(r["pnl"]) for r in rs if r["status"] == "closed")
                rules.append(f"{FLAG_TEXT[flag]} ×{len(rs)}" + (f" ({money(closed_pnl, True)})" if any(
                    r["status"] == "closed" for r in rs) else ""))
        held = [r for r in rows if r["tagd"].get("held_past_stop")]
        if held:
            rules.append(f"held past the stop ×{len(held)} ({money(sum(num(r['pnl']) for r in held), True)})")
        lines.append("Rules: " + ("✅ none broken" if not rules else "⚠️ " + "; ".join(rules)))
    # the market gauge: what the bot's setups did today on paper
    paper = db.rows("select * from paper where day=? and status='closed'", (day,))
    if paper:
        rs = [num(p["r_mult"]) for p in paper]
        by = {}
        for p in paper:
            by.setdefault(label(p["setup"], p["direction"]), []).append(num(p["r_mult"]))
        lines.append(f"\n📊 Market gauge — the bot's setups on paper today: {len(rs)} trades, {sum(rs):+.1f}R, "
                     f"{sum(1 for r in rs if r > 0)} won")
        for name, v in sorted(by.items(), key=lambda kv: -sum(kv[1]))[:5]:
            lines.append(f"   {esc(name)}: {len(v)} · {sum(v):+.1f}R")
    lk = leaks(db, 30, start + 86400)
    if lk["leaks"]:
        lines.append(f"\n🎯 Tomorrow's focus (your 30-day leak): {esc(lk['leaks'][0]['name'].lower())} "
                     f"({money(lk['leaks'][0]['pnl'], True)})")
    return "\n".join(lines)


def week_report(db, days: int = 7, now: float | None = None) -> str:
    now = now or time.time()
    lk = leaks(db, days, now, min_n=2)
    text = format_leaks(lk).replace(f"Your last {days} days", "Your week")
    paper = db.rows("select * from paper where status='closed' and closed_ts>=?", (int(now - days * 86400),))
    if paper:
        rs = [num(p["r_mult"]) for p in paper]
        text += f"\n\n📊 The bot's setups on paper this week: {len(rs)} trades, {sum(rs):+.1f}R"
    return text

