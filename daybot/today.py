"""What to do today: every part of DayBot boiled down to actions, or an honest "nothing to do, and here's why".

Each sleeve says one of
  act   there's something to do now (buy / sell / place)
  hold  you're already where the plan wants you
  wait  nothing yet, but something is coming (a list at 9:35, a window ending)
  skip  the evidence says don't bother
and carries a trust level taken from its own test, so a loud message can't hide a weak one. The scorecard puts the
test result next to what it would be worth in dollars on the money you actually put in each sleeve.
"""
from __future__ import annotations

from .clock import day_of
from .invest import VERDICT_TEXT as INVEST_VERDICT
from .util import esc, money, num, pct

TRUST = {"proven": "✅", "beat": "✅", "smoother": "🟢", "promising": "🟡", "aggressive": "🟠", "flat": "⚪",
         "no_better": "⚪", "thin": "⚪", "untested": "⚪", "negative": "🔴", "benchmark": "▫️", "benched": "⏸"}


def _invest_item(inv: dict) -> dict:
    if not inv.get("on"):
        return {"sleeve": "invest", "icon": "🧺", "title": "Invest plan", "status": "off", "lines": ["Off (/set INVEST 1)."]}
    if not inv.get("ready"):
        return {"sleeve": "invest", "icon": "🧺", "title": "Invest plan", "status": "wait",
                "lines": ["Loading 10 years of ETF prices from Alpaca…" + (f" {inv['error']}" if inv.get("error") else "")]}
    plan = next(r for r in inv["strategies"] if r["key"] == inv["plan"])
    acts = inv.get("actions") or []
    lines = [plan.get("explain", "")] if plan.get("explain") else []
    if acts:
        status = "act"
        lines += [f"{'Sell' if a['side'] == 'sell' else 'Buy'} {money(a['dollars'])} of {a['ticker']} ({a['name']})"
                  for a in acts]
        lines.append("Place them in the app — fractional shares, no commission. Orders placed after the close fill at "
                     "tomorrow's open.")
    elif not inv.get("manage"):
        status = "hold"
        lines.append(f"The plan holds {inv['plan_now']}. Nothing for you to do unless you choose to follow it "
                     "(/set INVEST_MANAGE 1 for exact dollars).")
    elif inv.get("budget"):
        status = "hold"
        lines.append(f"You already hold the plan ({inv['plan_now']}). Nothing to do — keep adding money to it.")
    else:
        status = "wait"
        lines.append("Tell it how much this plan manages: /set INVEST_BUDGET 500.")
    return {"sleeve": "invest", "icon": "🧺", "title": f"Invest plan: {plan['label']}", "status": status, "lines": lines,
            "trust": plan["verdict"]}


def _crypto_item(c: dict) -> dict:
    if not c.get("on"):
        return {"sleeve": "crypto", "icon": "🪙", "title": "Crypto trend plan", "status": "off", "lines": ["Off (/crypto on)."]}
    board = c.get("board") or []
    if not board or not c.get("backtest"):
        return {"sleeve": "crypto", "icon": "🪙", "title": "Crypto trend plan", "status": "wait",
                "lines": ["Loading crypto prices…" + (f" {c['error']}" if c.get("error") else "")]}
    budget = c.get("budget") or 0
    held = [r for r in board if r.get("target_w", 0) > 0]
    status = "hold"
    if held:
        lines = ["Trending up now: " + ", ".join(f"{r['coin']} {money(r['plan_usd'])}" for r in held[:6])
                 + f" (plan for {money(budget)}; the rest in cash)"]
    else:
        lines = [f"No coin is trending up: the plan for {money(budget)} is all cash."]
    recent = (c.get("autopilot") or {}).get("recent") or []
    latest = max((t["ts"] for t in recent), default=0)
    if latest:
        changes = [t for t in recent if t["ts"] == latest]
        lines.append("Last re-plan: " + ", ".join(f"{'bought' if t['side'] == 'buy' else 'sold'} {t['coin']}"
                                                  for t in changes[:6]) + " (the autopilot, pretend money)")
    lines.append("Costs 1% each way on Robinhood: only worth acting on if you plan to hold for weeks.")
    return {"sleeve": "crypto", "icon": "🪙", "title": "Crypto trend plan", "status": status, "lines": lines,
            "trust": c["backtest"].get("status", "untested")}


def _day_item(table: list[dict], in_play: list[dict], session: str) -> dict:
    longs = [r for r in table if r["direction"] == "long"]
    good = [r for r in longs if r["status"] in ("proven", "promising")]
    if not any(r.get("backtest") for r in longs):
        return {"sleeve": "day", "icon": "⚡", "title": "Day-trade setups", "status": "wait",
                "lines": ["Not tested yet — the first backtest runs by itself after the close."]}
    if not good:
        return {"sleeve": "day", "icon": "⚡", "title": "Day-trade setups", "status": "skip",
                "lines": ["Nothing has a measurable edge after costs, so there's nothing to take today. "
                          "The bot keeps paper-testing them and tells you the day one earns it."],
                "trust": "flat"}
    names = ", ".join(r["label"] for r in good)
    lines = [f"Only {names} has a probable edge — and it's small: most trades lose a little, a few win big."]
    if session == "open" and in_play:
        lines.append(f"{len(in_play)} stocks in play today — the alert has each one's buy-stop.")
    else:
        lines.append("Stocks in play come at 9:35am ET with a buy-stop for each.")
    best = min(good, key=lambda r: {"proven": 0, "promising": 1}[r["status"]])
    return {"sleeve": "day", "icon": "⚡", "title": "Day-trade setups", "status": "wait", "lines": lines,
            "trust": best["status"]}


def _btc_item(u: dict) -> dict:
    if not u.get("on"):
        return {"sleeve": "btc15", "icon": "⏱", "title": "BTC 15-minute call", "status": "off", "lines": ["Off."]}
    if not u.get("ready"):
        return {"sleeve": "btc15", "icon": "⏱", "title": "BTC 15-minute call", "status": "wait",
                "lines": ["Loading Bitcoin's minute prices…"]}
    up = u["p_up"] >= 0.5
    lines = [f"This window: {'UP' if up else 'DOWN'} {max(u['p_up'], 1 - u['p_up']) * 100:.0f}% "
             f"(price to beat ${u['beat']:,.0f}, now ${u['price']:,.0f})"]
    for q in (u.get("quotes") or [])[:2]:
        paper = (u.get("paper") or {}).get(q["venue"], {})
        proven = paper.get("proven")
        lines.append(f"{q['name']}: Up {q['up']:.0f}¢ / Down {q['down']:.0f}¢ — "
                     + (f"{q['side']} is {q['edge']:.0f}¢ cheap" + ("" if proven else " (signal not proven yet — paper only)")
                        if q["edge"] >= 4 else "no real edge"))
    return {"sleeve": "btc15", "icon": "⏱", "title": "BTC 15-minute call", "status": "wait", "lines": lines,
            "trust": "benchmark"}


def _score(inv: dict, c: dict, u: dict, table: list[dict], auto: dict) -> list[dict]:
    rows = []
    if inv.get("ready"):
        plan = next(r for r in inv["strategies"] if r["key"] == inv["plan"])
        p = plan["perf"]
        budget = inv.get("budget") or 0
        rows.append({"sleeve": "invest", "name": "🧺 Invest plan", "trust": plan["verdict"],
                     "verdict": INVEST_VERDICT.get(plan["verdict"], ""), "test": f"{p['cagr'] * 100:+.1f}% a year, worst drop "
                     f"{p['max_dd'] * 100:.0f}% over {p['years']:.1f} years", "money": budget,
                     "year": budget * p["cagr"], "drop": budget * p["max_dd"],
                     "live": (f"since {inv['since']['since']}: {pct(inv['since']['plan'] * 100)} vs SPY "
                              f"{pct((inv['since']['spy'] or 0) * 100)}") if inv.get("since") and inv["since"].get("plan") is not None else ""})
    bt = c.get("backtest") or {}
    if bt.get("strategy"):
        s, budget = bt["strategy"], c.get("budget") or 0
        ap = c.get("autopilot") or {}
        rows.append({"sleeve": "crypto", "name": "🪙 Crypto plan", "trust": bt.get("status", "untested"),
                     "verdict": bt.get("verdict", ""), "test": f"{s['cagr'] * 100:+.1f}% a year, worst drop "
                     f"{s['max_dd'] * 100:.0f}% (Bitcoin alone: {bt['btc']['max_dd'] * 100:.0f}%)", "money": budget,
                     "year": budget * s["cagr"], "drop": budget * s["max_dd"],
                     "live": (f"autopilot {pct(ap['return_pct'])}" + (f" vs Bitcoin {pct(ap['btc_pct'])}" if ap.get("btc_pct") is not None else ""))
                     if ap.get("since") else ""})
    best = max((r for r in table if r["direction"] == "long" and r.get("backtest")),
               key=lambda r: num((r["backtest"] or {}).get("avg_r")), default=None)
    if best:
        bt2 = best["backtest"]
        live = (f"autopilot {money(auto['equity'] - auto['start'], True)} on {auto['n']} trades" if auto.get("n") else "")
        rows.append({"sleeve": "day", "name": "⚡ Day trading", "trust": best["status"], "verdict": f"Best setup: {best['label']}",
                     "test": f"{num(bt2['avg_r']):+.2f}R a trade, {num(bt2.get('avg_pct')):+.2f}% of the money (typical trade "
                             f"{num(bt2.get('med_pct')):+.2f}%), {int(bt2['n'])} trades",
                     "money": auto.get("start") or 0, "year": None, "drop": None, "live": live})
    lv = (u.get("live") or {}) if u.get("ready") else {}
    paper = (u.get("paper") or {}) if u.get("ready") else {}
    if u.get("ready"):
        t = (u.get("test") or {}).get("favorite", {}).get("10", {}) if u.get("test") else {}
        rows.append({"sleeve": "btc15", "name": "⏱ BTC 15-minute", "trust": "benchmark",
                     "verdict": ("Edge vs market prices: " + "; ".join(
                         f"{v.title()} {r['signal']['avg_c']:+.1f}¢/contract over {r['signal']['n']} signals"
                         for v, r in paper.items() if r["signal"]["n"])) if any(r["signal"]["n"] for r in paper.values())
                     else "Odds are honest; whether they beat the market's price is being measured",
                     "test": (f"favorite won {t['hit'] * 100:.0f}% at 10 min in (30-day test)" if t.get("hit") else ""),
                     "money": None, "year": None, "drop": None,
                     "live": (f"live 7 days: {lv['fav10'] * 100:.0f}% of {lv['n10']}" if lv.get("fav10") is not None else "")})
    return rows


def build(app, now: float, parts: dict) -> dict:
    """parts: the dashboard's already-computed pieces {"invest", "crypto", "updown", "autopilot", "in_play"}."""
    table = app.edge.table()
    session = app.market.session(now)
    items = [_invest_item(parts["invest"]), _crypto_item(parts["crypto"]),
             _day_item(table, parts.get("in_play") or [], session), _btc_item(parts["updown"])]
    acting = [i for i in items if i["status"] == "act"]
    headline = ("👉 " + "; ".join(f"{i['icon']} {i['title']}" for i in acting)) if acting else \
        "Nothing needs doing right now — the plans are in place. Quiet is the system working."
    return {"headline": headline, "items": items, "score": _score(parts["invest"], parts["crypto"], parts["updown"], table,
                                                                  parts["autopilot"]),
            "day": day_of(now)}


def text(t: dict) -> str:
    """The Telegram version: a few lines per sleeve."""
    mark = {"act": "👉", "hold": "✅", "wait": "⏳", "skip": "🚫", "off": "⚪"}
    lines = ["<b>📋 What to do today</b>", esc(t["headline"]), ""]
    for i in t["items"]:
        lines.append(f"{mark.get(i['status'], '•')} <b>{i['icon']} {esc(i['title'])}</b> {TRUST.get(i.get('trust', ''), '')}")
        lines += [f"   {esc(x)}" for x in i["lines"][:4]]
    sc = [r for r in t["score"] if r.get("year") is not None and r.get("money")]
    if sc:
        lines.append("\n<b>What it's worth at your size</b> (from each test — not a promise)")
        for r in sc:
            lines.append(f"   {r['name']}: {money(r['money'])} → about {money(r['year'], True)} a year, "
                         f"worst drop about {money(r['drop'])}")
    return "\n".join(lines)


def short(t: dict) -> str:
    """Two lines for the top of the morning plan."""
    acting = [i for i in t["items"] if i["status"] == "act"]
    if not acting:
        return "📋 Today: nothing needs doing — the plans are in place. (/today for each one)"
    return "📋 Today: " + " · ".join(f"{i['icon']} {i['title']}: " + i["lines"][1 if len(i["lines"]) > 1 else 0]
                                     for i in acting[:3]) + " (/today)"
