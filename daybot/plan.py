"""The morning game plan (default 9:00am New York, or /plan any time).

The market's tone, which setups the data backs right now, the gappers with their levels and a plan for each,
your overnight positions, and your rules for the day in dollars.
"""
from __future__ import annotations

from .backtest import NO_EDGE
from .bars import C, sma
from .clock import day_of, et
from .util import big, esc, money, num, pct, price as fmt_price


def market_tone(data, now: float) -> dict:
    """SPY / QQQ: premarket move, trend vs the 20- and 50-day averages, yesterday's range."""
    out = {}
    for sym in ("SPY", "QQQ"):
        daily = data.daily(sym)
        if len(daily) < 20:
            continue
        pdc = daily[-1][C]
        px = data.last_price(sym) or pdc
        closes = [d[C] for d in daily]
        s20, s50 = sma(closes, 20), sma(closes, 50)
        out[sym] = {"chg": (px / pdc - 1) * 100, "price": px, "above20": pdc > s20, "above50": pdc > s50,
                    "s20": s20, "s50": s50}
    spy = out.get("SPY")
    if spy:
        if spy["above20"] and spy["above50"]:
            trend = "uptrend (above its 20- and 50-day averages)"
        elif not spy["above20"] and not spy["above50"]:
            trend = "downtrend (below its 20- and 50-day averages)"
        else:
            trend = "mixed (between its 20- and 50-day averages)"
        out["trend"] = trend
        chg = spy["chg"]
        out["tone"] = ("strong open expected — longs have the wind" if chg > 0.6 else
                       "weak open expected — be pickier with longs" if chg < -0.6 else "flat open")
    return out


def gapper_plan(g: dict) -> str:
    """A simple if-then for a gapper."""
    if g["gap"] > 0:
        a = f"long over the premarket high {fmt_price(g['pmh'])} if it holds VWAP" if g.get("pmh") else "long on a VWAP hold"
        b = f"short only under {fmt_price(g['pml'])} (gap failing)" if g.get("pml") else ""
    else:
        a = f"short under the premarket low {fmt_price(g['pml'])} if it stays under VWAP" if g.get("pml") else "short on a VWAP rejection"
        b = f"long only over {fmt_price(g['pmh'])} (gap filling)" if g.get("pmh") else ""
    return a + (f"; {b}" if b else "")


def build(cfg, db, data, edge, scanner, now: float, account: float = 0.0, positions: list[dict] | None = None,
          earnings_map: dict | None = None) -> str:
    day = day_of(now)
    t = et(now)
    lines = [f"<b>🌅 Game plan · {t.strftime('%a %b %d')} · {t.strftime('%I:%M%p').lstrip('0').lower()} ET</b>"]
    tone = market_tone(data, now)
    if tone.get("SPY"):
        spy, qqq = tone["SPY"], tone.get("QQQ")
        line = f"Market: SPY {pct(spy['chg'])}" + (f", QQQ {pct(qqq['chg'])}" if qqq else "") + f" · {tone['tone']}"
        lines.append(line)
        lines.append(f"Trend: SPY in a {tone['trend']}")
    table = edge.table()
    proven = [r for r in table if r["status"] == "proven"]
    losing = [r for r in table if r["status"] in NO_EDGE]
    if proven:
        lines.append("✅ Setups the data backs: " + ", ".join(esc(r["label"]) for r in proven))
    elif any(r["backtest"] for r in table):
        promising = [r for r in table if r["status"] == "promising" and r["direction"] == "long"]
        lines.append("⚪ No setup is proven yet" + (": the only ones with a probable edge are "
                                                  + ", ".join(esc(r["label"]) for r in promising)
                                                  + " — small, or paper first" if promising else
                                                  " — nothing here has a measurable edge, so sitting out is the plan"))
    else:
        lines.append("⚪ Backtest not run yet — /backtest (takes a few minutes)")
    if losing:
        longs = [r for r in losing if r["direction"] == "long"]
        lines.append("🔴 No edge in the test (paper only): " + ", ".join(esc(r["label"]) for r in longs))
    inplay = next((r for r in table if r["setup"] == "orb5" and r["direction"] == "long"), None)
    if inplay and inplay["status"] not in NO_EDGE and cfg.get("ORB5_TOP") > 0:
        lines.append("🔥 Today's stocks in play come at 9:35am, each with a buy-stop and a stop")
    gappers = scanner.gappers(day)
    if gappers:
        lines.append("\n<b>Gappers</b>")
        for i, g in enumerate(gappers[:8], 1):
            head = (f"{i}. <b>{esc(g['symbol'])}</b> {pct(g['gap'])} {fmt_price(g['price'])} · PM vol {big(g['pm_vol'])}"
                    + (f" ({g['pm_rvol']:.1f}× usual)" if g.get("pm_rvol") else ""))
            lines.append(head)
            cat = g.get("catalyst")
            if cat:
                tag = f" [{', '.join(cat['tags'])}]" if cat.get("tags") else ""
                lines.append(f"   📰 {esc(cat['headline'][:110])}{esc(tag)}")
            if g.get("warning"):
                lines.append(f"   🔴 {esc(g['warning'][:140])}")
            lv = [f"PM high {fmt_price(g['pmh'])}" if g.get("pmh") else "", f"PM low {fmt_price(g['pml'])}" if g.get("pml") else "",
                  f"Y high {fmt_price(g['pdh'])}", f"Y close {fmt_price(g['pdc'])}"]
            lines.append("   Levels: " + " · ".join(x for x in lv if x))
            lines.append(f"   Plan: {esc(gapper_plan(g))}")
            if earnings_map and earnings_map.get(g["symbol"]):
                lines.append(f"   📅 Earnings {esc(earnings_map[g['symbol']])}")
    else:
        lines.append("\nNo gappers past your filters yet (GAP_MIN_PCT, MIN_PRICE, MIN_AVG_VOLUME).")
    if positions:
        lines.append("\n<b>Your open positions</b>")
        for p in positions[:8]:
            what = (f"{p['underlying']} {num(p['strike']):g}{(p['option_type'] or 'C')[0]} {p['expiry'][5:]}"
                    if p["kind"] == "option" else p["symbol"])
            note = ""
            if earnings_map and earnings_map.get(p["underlying"]):
                note = f" · 📅 earnings {earnings_map[p['underlying']]}"
            lines.append(f"• {esc(what)} × {num(p['qty']):g} @ {fmt_price(p['avg_price'])}{esc(note)}")
    if account:
        risk = account * cfg.get("RISK_PER_TRADE_PCT") / 100
        stop_day = account * cfg.get("DAILY_MAX_LOSS_PCT") / 100
        lines.append(f"\n📏 Today's rules: risk {money(risk)} a trade ({cfg.get('RISK_PER_TRADE_PCT'):g}%), done for the day "
                     f"at {money(-stop_day)} ({cfg.get('DAILY_MAX_LOSS_PCT'):g}%), max {cfg.get('MAX_TRADES_PER_DAY'):.0f} trades, "
                     f"break after {cfg.get('LOSS_STREAK_BREAK'):.0f} losses in a row.")
    lines.append("First 5 minutes are the most expensive minutes of the day — let the opening range form.")
    return "\n".join(lines)

