"""Telegram message text. Few message types, same layout every time.

  🟢 WHALE BUY / 🐋🐋 2ND WHALE IN   a tracked whale bought a coin (graded A/B/C)
  🔴 WHALE SOLD x% / WHALE EXITED    only for coins you hold (or were alerted on)
  🚨 LIQUIDITY PULLED                confirmed twice; the only price-based alarm
  📊 DAILY SUMMARY

The only "price dropped" message is one stop-loss warning per coin you hold (STOP_LOSS_PCT).
"""
from __future__ import annotations

from .util import ago, dur, esc, mc, mult, num, pct, usd
from .whales import dumps

GRADE_ICON = {"A": "🟢", "B": "🟡", "C": "⚪️"}
STATUS_TEXT = {"HOT": "🔥 HOT", "OK": "✅ OK", "NEW": "🆕 NEW", "WEAK": "〰️ WEAK", "COLD": "🧊 COLD"}


def token_links(mint: str, pair: str = "") -> list[tuple[str, str]]:
    links = [("DexScreener", f"https://dexscreener.com/solana/{pair or mint}"),
             ("GMGN", f"https://gmgn.ai/sol/token/{mint}")]
    if pair:
        links.append(("Photon", f"https://photon-sol.tinyastro.io/en/lp/{pair}"))
    return links


def whale_link(address: str) -> str:
    return f"https://gmgn.ai/sol/address/{address}"


def whale_record(stats: dict) -> str:
    status = STATUS_TEXT.get(stats.get("status", "NEW"), stats.get("status", ""))
    if stats.get("n", 0) == 0:
        return f"{status} · no copies measured yet"
    text = (f"{status} · {stats['n']} copies · {stats['win_rate'] * 100:.0f}% won · "
            f"median {pct(stats.get('median', stats['avg']))}")
    if abs(stats["avg"] - stats.get("median", stats["avg"])) >= 25:
        text += f" (avg {pct(stats['avg'])})"
    if stats.get("typical_dip", 0) <= -10:
        text += f" · winners dipped {stats['typical_dip']:.0f}% first"
    return text


def form_line(form: list[float]) -> str:
    if not form:
        return ""
    return f"Last {len(form)} calls: " + " · ".join(("🟩" if r > 0 else "🟥") + pct(r) for r in form)


def buy_alert(*, symbol: str, mint: str, whale_name: str, whale_addr: str, stats: dict, grade: str,
              reasons: list, usd_value: float, base_amount: float, base: str, entry_mc: float,
              now_mc: float, chase: float | None, confluence: list[dict], latency_s: int,
              info: dict, late_detect: bool, hold_line: str = "", form: list | None = None,
              scalp: bool = False) -> str:
    n = len(confluence)
    if n >= 2:
        head = f"🐋🐋 {'2ND' if n == 2 else f'{n}TH' if n > 3 else '3RD'} WHALE IN · ${esc(symbol)}"
    else:
        head = f"{GRADE_ICON.get(grade, '🟢')} WHALE BUY · ${esc(symbol)}"
    if scalp:
        head = "⚡ SCALP · " + head
    lines = [f"<b>{head}</b>  <i>grade {grade}</i>",
             f"🐋 <a href=\"{whale_link(whale_addr)}\">{esc(whale_name)}</a> — {whale_record(stats)}"]
    if form:
        lines.append(form_line(form))
    paid = f"{base_amount:,.2f} {base}" if base == "SOL" else usd(base_amount)
    timing = dur(latency_s) + " ago"
    lines.append(f"Bought {usd(usd_value)} ({paid}) at <b>{mc(entry_mc)}</b> MC · {timing}"
                 + (" · <i>seen late</i>" if late_detect else ""))
    if now_mc > 0 and chase is not None:
        lines.append(f"Now {mc(now_mc)} MC ({pct(chase)} since whale)")
    if n >= 2:
        lines.append("Whales in: " + ", ".join(f"{esc(c['name'])} @ {mc(c['entry_mc'])}" for c in confluence[:5]))
    facts = []
    liq = num(info.get("liquidity_usd"), -1)
    if liq >= 0:
        facts.append(f"liq {usd(liq)}")
    if info.get("pair_created_ts"):
        facts.append(f"age {ago(info['pair_created_ts'])}")
    if info.get("buys_h1") or info.get("sells_h1"):
        facts.append(f"1h {info.get('buys_h1', 0)} buys / {info.get('sells_h1', 0)} sells")
    if facts:
        lines.append(" · ".join(facts))
    for ok, text in reasons:
        lines.append(("✅ " if ok else "⚠️ " if ok is False else "• ") + esc(text))
    if hold_line:
        lines.append(esc(hold_line))
    lines.append(f"<code>{mint}</code>")
    return "\n".join(lines)


def exit_plan_line(now_mc: float, cfg, scalp: bool, plan: dict | None = None) -> str:
    """The exit plan that backtested best on your alerts: half at 2x, trail the rest, cut a big loss."""
    trail, stop = cfg.get("PROTECT_TRAIL_PCT"), cfg.get("STOP_LOSS_PCT")
    line = "📋 Plan: sell half at 2x" + (f" (~{mc(now_mc * 2)} MC)" if now_mc > 0 else "")
    line += f". Sell the rest if it falls {trail:.0f}% from its top"
    line += f" or {stop:.0f}% below your entry." if stop > 0 else "."
    if scalp:
        line += " ⚡ This whale's coins usually die within hours — don't hold overnight."
    if plan and plan.get("time_to_peak_s"):
        line += f" Winners peaked ~{dur(plan['time_to_peak_s'])} after the buy."
    return line


def sell_alert(*, symbol: str, mint: str, whale_name: str, whale_addr: str, fraction: float, usd_value: float,
               exit_mc: float, whale_multiple: float, whale_entry_mc: float, left_pct: float,
               others_in: list[dict], position: dict | None, all_out: bool) -> str:
    full = fraction >= 0.9 or left_pct < 5
    head = f"🔴 WHALE EXITED · ${esc(symbol)}" if full else f"🟠 WHALE SOLD {fraction * 100:.0f}% · ${esc(symbol)}"
    lines = [f"<b>{head}</b>",
             f"🐋 <a href=\"{whale_link(whale_addr)}\">{esc(whale_name)}</a> sold {usd(usd_value)} at {mc(exit_mc)} MC"]
    if whale_entry_mc > 0:
        lines.append(f"Their entry {mc(whale_entry_mc)} → {mult(whale_multiple)}"
                     + ("" if full else f" · still holds {left_pct:.0f}% of their bag"))
    elif not full:
        lines.append(f"Still holds {left_pct:.0f}% of their bag")
    if others_in:
        lines.append("Still in: " + ", ".join(f"{esc(o['name'])} (entry {mc(o['entry_mc'])})" for o in others_in[:4]))
    elif all_out:
        lines.append("<b>Every tracked whale in this coin is now out.</b>")
    if position and position.get("open"):
        lines.append(f"You: {usd(position['value'])} now · {usd(position['pnl'], signed=True)} "
                     f"({pct(position['pnl_pct'])}) on this coin")
    lines.append("Partial sell — whales often trim and keep riding." if not full
                 else "Copy-exit signal: this whale is out.")
    lines.append(f"<code>{mint}</code>")
    return "\n".join(lines)


def rug_alert(*, symbol: str, mint: str, liq_before: float, liq_now: float, position: dict | None) -> str:
    drop = (1 - liq_now / liq_before) * 100 if liq_before > 0 else 100
    lines = [f"<b>🚨 LIQUIDITY PULLED · ${esc(symbol)}</b>",
             f"Liquidity {usd(liq_before)} → {usd(liq_now)} ({drop:.0f}% gone, confirmed twice)"]
    if position and position.get("open"):
        lines.append(f"You hold {usd(position['value'])} ({pct(position['pnl_pct'])}). Selling may already be hard.")
    lines.append(f"<code>{mint}</code>")
    return "\n".join(lines)


def take_initial_note(*, symbol: str, mint: str, position: dict, multiple: float) -> str:
    return (f"<b>💰 ${esc(symbol)} is {mult(multiple)} on your cost</b>\n"
            f"Optional: selling {usd(position['cost'])} takes your initial out and lets the rest ride "
            f"for free while the whales hold.\n<code>{mint}</code>")


def ladder_note(*, symbol: str, mint: str, level: float, position: dict, whales_in: int, typical_peak: float,
                best_rule: str, scalp: bool = False) -> str:
    lines = [f"<b>📈 ${esc(symbol)} hit {level:g}x on your cost</b>"]
    if scalp:
        lines.append(f"{usd(position['cost'])} in → {usd(position['value'])} now. ⚡ Scalp coin: sell at least half "
                     "here — coins like this usually give the whole pump back.")
    else:
        lines.append(f"{usd(position['cost'])} in → {usd(position['value'])} now. Ladder: sell about a third, let the rest ride"
                     + (f" while {whales_in} whale{'s' if whales_in != 1 else ''} hold." if whales_in else "."))
    if typical_peak > 0:
        lines.append(f"Your whales' picks peak around {mult(typical_peak)} (median).")
    if best_rule:
        lines.append(f"Best exit style on your whales lately: {esc(best_rule)}.")
    lines.append(f"<code>{mint}</code>")
    return "\n".join(lines)


def protect_note(*, symbol: str, mint: str, coach: dict, position: dict) -> str:
    return (f"<b>🛡 ${esc(symbol)} gave back {coach['from_peak_pct']:.0f}% from its peak</b>\n"
            f"It reached {mult(coach['peak_multiple'])} on your cost and is {mult(coach['multiple'])} now "
            f"({usd(position['value'])}). "
            + (f"{coach['whales_in']} whale{'s' if coach['whales_in'] != 1 else ''} still in. " if coach.get("whales_in")
               else "No tracked whale is in anymore. ")
            + "If you usually sell too late, this is the moment to lock some in.\n"
            f"<code>{mint}</code>")


def stop_note(*, symbol: str, mint: str, coach: dict, position: dict, stop_pct: float) -> str:
    return (f"<b>✂️ ${esc(symbol)} is down {100 - coach['multiple'] * 100:.0f}% on your cost</b>\n"
            f"{usd(position['cost'])} in → {usd(position['value'])} now. Your plan was to cut it at -{stop_pct:.0f}%. "
            + (f"{coach['whales_in']} whale{'s' if coach['whales_in'] != 1 else ''} still in. " if coach.get("whales_in")
               else "No tracked whale is in anymore. ")
            + f"\n<code>{mint}</code>")


def position_line(p: dict, holders: list[dict]) -> str:
    whales_in = [h for h in holders if h["still_in"]]
    line = (f"<b>${esc(p['symbol'])}</b> {usd(p['value'])} · {usd(p['unrealized'], signed=True)} "
            f"({pct((p['multiple'] - 1) * 100 if p['multiple'] else 0)})")
    if p.get("entry_mc"):
        line += f" · in @ {mc(p['entry_mc'])} → {mc(p['mc_now'])}"
    if holders:
        line += f"\n   🐋 {len(whales_in)}/{len(holders)} whales still in"
        if whales_in:
            line += ": " + ", ".join(esc(h["name"]) for h in whales_in[:3])
    return line


def whale_line(w: dict, i: int) -> str:
    s = w["stats"]
    muted = " 🔕 muted" if w.get("muted") else " · auto-muted" if w.get("auto_muted") == 1 else ""
    last = f" · last trade {ago(w['last_trade_ts'])} ago" if w.get("last_trade_ts") else ""
    body = (f"{i}. <b>{esc(w['name'])}</b> {STATUS_TEXT[s['status']]}{muted}\n"
            f"   {s['n']} copies · {s['win_rate'] * 100:.0f}% won · median {pct(s['median'])} · avg {pct(s['avg'])} · "
            f"2x rate {s['hit_2x'] * 100:.0f}%{last}")
    after = w.get("after") or {}
    if after.get("n", 0) >= 3:
        body += (f"\n   Their coins 6h later: {after['dead']}/{after['n']} down 50%+"
                 + (" · ⚡ scalp only" if dumps(after) else ""))
    return body + f"\n   <code>{w['address']}</code>"
