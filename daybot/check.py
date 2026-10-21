"""Trade check: everything that says "careful" about a stock or options trade, in one verdict with a plan.

It runs the moment one of your fills shows up (Robinhood / Chase through SnapTrade) and when you ask (/check NVDA,
/check NVDA 140c 10/24, or just paste a ticker / contract).

🔴 AVOID   breaks one of your own rules or has taken money in the data: daily loss limit hit, a revenge trade,
           sized far over your risk rule, a dilution headline, a spread or open interest you can't get out through,
           a same-day option late in the day
🟡 CAREFUL manageable with size and the plan: chasing far above VWAP, fighting VWAP, first minutes, lunch,
           light volume, a weak market, a lottery-ticket option, a move bigger than the market expects
🟢 CLEAN   none of the above — not a promise it works, just none of the traps the bot can see
"""
from __future__ import annotations

import datetime as dt
import time

from .accounts import account_size, fmt_qty, size_shares, spendable
from .alpaca import snap_price, snap_spread_pct
from .backtest import describe
from .bars import H, L, T, nearest_levels, resample, split_day
from .clock import clock_text, day_of, et
from .news import catalysts, warning
from .options import breakeven, dte, expected_move, occ, parse_occ, quote, value_change
from .setups import label
from .util import esc, money, num, pct, price as fmt_price

RED, YELLOW, GREEN = "🔴", "🟡", "🟢"
WORD = {RED: "AVOID", YELLOW: "CAREFUL", GREEN: "CLEAN"}


def add(items: list, level: str, text: str, flag: str = "") -> None:
    """A check line, with a short flag the journal uses to find patterns (chasing, revenge, 0dte…)."""
    items.append((level, text, flag))


def stock_plan(direction: str, entry: float, lv: dict, bars5: list, atr: float) -> dict:
    """Stop under the nearest real level (VWAP, the last 5-minute swing, the opening range, the premarket level),
    targets at 1R and 2R. Falls back to 0.35 ATR when no level is in a sensible range."""
    s = 1 if direction == "long" else -1
    cands = []
    for name, key in (("VWAP", "vwap"), ("opening-range low" if s > 0 else "opening-range high", "orl" if s > 0 else "orh"),
                      ("premarket low" if s > 0 else "premarket high", "pml" if s > 0 else "pmh"),
                      ("yesterday's close", "pdc")):
        v = num(lv.get(key))
        if v:
            cands.append((name, v))
    recent = bars5[-3:]
    if recent:
        swing = min(b[L] for b in recent) if s > 0 else max(b[H] for b in recent)
        cands.append(("the last 15-min swing " + ("low" if s > 0 else "high"), swing))
    best = None
    for name, level in cands:
        dist = s * (entry - level)
        if atr and 0.1 * atr <= dist <= 0.6 * atr and (best is None or dist < best[2]):
            best = (name, level, dist)
    if best:
        stop = best[1] - s * 0.02 * atr
        why = f"just past {best[0]} {fmt_price(best[1])}"
    else:
        stop = entry - s * 0.35 * (atr or entry * 0.02)
        why = "0.35× the normal daily range (no clean level nearby)"
    risk = abs(entry - stop)
    nxt = nearest_levels(entry, lv, above=s > 0)
    return {"stop": stop, "t1": entry + s * risk, "t2": entry + 2 * s * risk, "risk_ps": risk, "why": why,
            "next_level": nxt[0] if nxt else None}


class TradeCheck:
    def __init__(self, cfg, db, data, market, ledger, edge, earnings=None):
        self.cfg, self.db, self.data, self.market, self.ledger, self.edge = cfg, db, data, market, ledger, edge
        self.earnings = earnings

    def account_size(self, account_id: str | None = None) -> float:
        return account_size(self.db, self.cfg, account_id)

    def matching_alert(self, symbol: str, direction: str, ts: int, minutes: int = 20) -> dict | None:
        return self.db.row("""select * from alerts where symbol=? and direction=? and ts between ? and ?
                              order by ts desc limit 1""", (symbol, direction, ts - minutes * 60, ts + 60))

    async def _prepare(self, symbols: list[str], now: float) -> None:
        await self.data.load_daily(symbols, now)
        stale = [s for s in symbols if not self.data.bars1(s) or now - self.data.bars1(s)[-1][T] > 90]
        if stale:
            await self.data.refresh(stale, now)

    async def check(self, req: dict, now: float | None = None) -> dict:
        """req: symbol (underlying), kind stock|option, contract (OCC, options), direction long|short, qty, price
        (0 = now), account_id, source, adding (bool)."""
        now = now or time.time()
        sym = req["symbol"].upper()
        kind = req.get("kind", "stock")
        direction = req.get("direction", "long")
        s = 1 if direction == "long" else -1
        await self._prepare([sym, "SPY"], now)
        snap = await self.data.snapshot(sym)
        und = num(req.get("und_price")) or self.data.last_price(sym) or snap_price(snap)
        if kind == "stock" and num(req.get("price")) > 0:
            und = num(req.get("price"))                 # judge (and plan) the price you actually got
        lv = self.data.levels(sym, now)
        day = day_of(now)
        _, rth, _ = split_day(self.data.bars1(sym), day, self.market)
        bars5 = [b for b in resample(rth, 5)]
        atr = num(lv.get("atr"))
        items: list[tuple[str, str]] = []
        info: dict = {"symbol": sym, "kind": kind, "direction": direction, "und_price": und, "levels": lv}
        if und <= 0:
            return {**info, "verdict": RED, "items": [(RED, f"No price for {sym} — check the ticker", "no_price")],
                    "plan": {}, "flags": ["no_price"]}
        session = self.market.session(now)
        mins = (now - self.market.open_ts(day)) / 60 if session == "open" else None
        # ---- time of day ----
        if session in {"pre", "post"}:
            add(items, YELLOW, "Extended hours: thin trading and wide spreads — limit orders only, smaller size", "extended_hours")
        elif session == "closed":
            add(items, YELLOW, "Market closed — prices here are from the last session", "closed")
        elif mins is not None and mins < 5:
            add(items, YELLOW, "First 5 minutes: the opening swings stop people out — waiting for the opening "
                               "range usually beats this", "first5")
        elif mins is not None and self.market.minutes_left(now) < 15:
            add(items, YELLOW, "Less than 15 minutes to the close", "late")
        elif mins is not None and 120 <= mins <= 240:
            add(items, YELLOW, "Lunch hours: volume dries up and breakouts fail more often", "lunch")
        # ---- the stock itself ----
        spread = snap_spread_pct(snap) if snap else None
        if kind == "stock" and spread is not None:
            if spread > 1.5:
                add(items, RED, f"Spread {spread:.1f}%: you lose that on the way in and out", "spread")
            elif spread > 0.5:
                add(items, YELLOW, f"Wide spread ({spread:.2f}%) — use limit orders", "spread")
        if und < self.cfg.get("MIN_PRICE"):
            add(items, YELLOW, f"Under ${self.cfg.get('MIN_PRICE'):.0f}: thin, gappy, easy to push around", "penny")
        avg_vol = num(lv.get("avg_vol"))
        if avg_vol and avg_vol < 500_000:
            add(items, YELLOW, f"Thinly traded ({avg_vol / 1e3:.0f}K shares a day)", "thin")
        vw = num(lv.get("vwap"))
        if vw and session == "open":
            if s * (und - vw) < 0:
                add(items, YELLOW, f"{'Below' if s > 0 else 'Above'} VWAP {fmt_price(vw)} — "
                                   f"{'buyers' if s > 0 else 'sellers'} aren't in control yet", "against_vwap")
            elif atr and s * (und - vw) > 0.8 * atr:
                add(items, YELLOW, f"Chasing: {fmt_price(abs(und - vw))} ({abs(und - vw) / atr:.1f}× its daily range) "
                                   f"{'above' if s > 0 else 'below'} VWAP — snap-backs to VWAP are common", "chasing")
            else:
                add(items, GREEN, f"{'Above' if s > 0 else 'Below'} VWAP {fmt_price(vw)}, not stretched")
        if atr and lv.get("open") and s * (und - lv["open"]) > 1.5 * atr:
            add(items, YELLOW, f"Already moved {abs(und - lv['open']) / atr:.1f}× its normal daily range today", "extended")
        rv = num(lv.get("rvol"))
        if session == "open" and lv.get("avg_vol_live") and mins and mins >= 10:
            if rv < 0.7:
                add(items, YELLOW, f"Volume {rv:.1f}× normal — not in play, moves fade", "low_volume")
            elif rv >= 2:
                add(items, GREEN, f"In play: {rv:.1f}× normal volume")
        nxt = nearest_levels(und, lv, above=s > 0)
        if nxt and atr and abs(nxt[0][1] - und) < 0.15 * atr:
            add(items, YELLOW, f"Right {'under' if s > 0 else 'above'} {nxt[0][0]} {fmt_price(nxt[0][1])} — "
                               f"the first test often {'rejects' if s > 0 else 'bounces'}", "at_level")
        # ---- the news ----
        news = catalysts(await self.data.news(sym), sym)
        bad = warning(news) if s > 0 else ""
        if bad:
            add(items, RED if ("offering" in bad or "bankruptcy" in bad) else YELLOW, f"News: {bad}", "bad_news")
        info["news"] = news[:3]
        if self.earnings:
            when = await self.earnings.next_report(sym, now)
            if when and when.get("days") is not None and when["days"] <= 1:
                add(items, YELLOW, f"Earnings {when['text']} — don't hold through it unless you mean to", "earnings")
                info["earnings"] = when
        # ---- the market ----
        spy = self.data.levels("SPY", now) if sym != "SPY" else {}
        if spy.get("vwap") and spy.get("last") and spy.get("pdc") and session == "open":
            spy_chg = (spy["last"] / spy["pdc"] - 1) * 100
            weak = spy["last"] < spy["vwap"] and spy_chg < -0.3
            strong = spy["last"] > spy["vwap"] and spy_chg > 0.3
            if (s > 0 and weak) or (s < 0 and strong):
                add(items, YELLOW, f"Market against you: SPY {pct(spy_chg)} and {'below' if weak else 'above'} VWAP", "weak_market")
            info["spy_chg"] = spy_chg
        # ---- the bot's own setups ----
        alert = self.matching_alert(sym, direction, int(now))
        if alert:
            st = self.edge.status(alert["setup"], alert["direction"])
            add(items, YELLOW if st["status"] in {"negative", "benched"} else GREEN,
                       f"Matches the bot's {label(alert['setup'], alert['direction'])} alert from "
                       f"{clock_text(alert['ts'])} ({describe(st)})")
            info["alert"] = alert
        # ---- plan, size and your rules ----
        entry_und = und
        plan = stock_plan(direction, entry_und, lv, bars5, atr)
        if alert and abs(num(alert["entry"]) - und) < (atr or und) * 0.3 and s * (und - num(alert["stop"])) > 0:
            plan.update(stop=num(alert["stop"]), t1=num(alert["t1"]), t2=num(alert["t2"]),
                        risk_ps=abs(und - num(alert["stop"])), why="the alert's stop")
        plan["und_price"] = und
        account = self.account_size(req.get("account_id"))
        info["account"] = account
        qty = num(req.get("qty"))
        if kind == "option":
            await self._option_items(req, items, plan, info, now, und, account)
        else:
            px = num(req.get("price")) or und
            plan["entry"] = px
            plan["risk_usd"] = qty * abs(px - plan["stop"]) if qty else 0.0
            before = req.get("source") != "fill" and not req.get("closing")      # asking before you buy
            cash = spendable(self.db, req.get("account_id")) if before else None
            if account and plan["risk_ps"] > 0:
                sz = size_shares(account * self.cfg.get("RISK_PER_TRADE_PCT") / 100, plan["risk_ps"], px, cash)
                plan.update(max_qty=sz["shares"], max_by_risk=sz["by_risk"], cash=cash, capped=sz["capped"],
                            max_risk=sz["risk_usd"])
            if before and qty and cash is not None and direction == "long" and qty * px > cash * 1.01 + 1:
                add(items, RED, f"{fmt_qty(qty)} shares cost {money(qty * px)} — you have {money(cash)} to buy with",
                    "no_cash")
        if qty and account and plan.get("risk_usd"):
            budget = account * self.cfg.get("RISK_PER_TRADE_PCT") / 100
            share = plan["risk_usd"] / account * 100
            if plan["risk_usd"] > 2 * budget:
                add(items, RED, f"Size: {money(plan['risk_usd'])} at risk if the stop hits — {share:.1f}% of the "
                                f"account; your rule is {self.cfg.get('RISK_PER_TRADE_PCT'):g}%", "oversized")
            elif plan["risk_usd"] > 1.15 * budget:
                add(items, YELLOW, f"Size a bit big: {money(plan['risk_usd'])} at risk ({share:.1f}%, rule "
                                   f"{self.cfg.get('RISK_PER_TRADE_PCT'):g}%)", "oversized")
            else:
                add(items, GREEN, f"Size OK: {money(plan['risk_usd'])} at risk ({share:.1f}% of the account)")
        if qty and not account:
            plan["no_account"] = True
        if not req.get("closing"):
            self._discipline(items, req, now, account, sym)
        if req.get("adding") and req.get("avg_price"):
            mark = num(req.get("mark")) or num(req.get("price"))
            if s * (mark - num(req["avg_price"])) < 0:
                add(items, YELLOW, "Averaging down: adding to a position that's losing", "averaging_down")
        reds = [it[1] for it in items if it[0] == RED]
        yellows = [it[1] for it in items if it[0] == YELLOW]
        flags = sorted({it[2] for it in items if it[2] and it[0] != GREEN})
        verdict = RED if reds else YELLOW if yellows else GREEN
        return {**info, "verdict": verdict, "items": items, "plan": plan, "reds": len(reds), "yellows": len(yellows),
                "qty": qty, "price": num(req.get("price")), "contract": req.get("contract", ""), "ts": int(now),
                "flags": flags}

    def _discipline(self, items: list, req: dict, now: float, account: float, sym: str) -> None:
        state = self.ledger.day_state(day_of(now), req.get("account_id"))
        if account and state["realized"] <= -account * self.cfg.get("DAILY_MAX_LOSS_PCT") / 100:
            add(items, RED, f"Daily loss limit hit ({money(state['realized'])} today). Your rule: stop for today", "over_daily_limit")
        streak, last = state["streak"], state["last_closed_ts"]
        if streak >= self.cfg.get("LOSS_STREAK_BREAK") and now - last < self.cfg.get("BREAK_MINUTES") * 60:
            add(items, RED, f"{streak} losses in a row, the last {int((now - last) / 60)} min ago — take the "
                            f"{self.cfg.get('BREAK_MINUTES'):.0f}-minute break first", "after_streak")
        lost = state["last_loss"].get(sym)
        if lost and now - lost < self.cfg.get("REVENGE_MINUTES") * 60:
            add(items, RED, f"Revenge trade? You took a loss on {sym} {int((now - lost) / 60)} min ago", "revenge")
        if state["trades"] >= self.cfg.get("MAX_TRADES_PER_DAY"):
            add(items, YELLOW, f"Trade #{state['trades'] + 1} today — past your {self.cfg.get('MAX_TRADES_PER_DAY'):.0f}"
                               "-trade limit (overtrading costs more than it makes)", "overtrading")

    async def _option_items(self, req: dict, items: list, plan: dict, info: dict, now: float, und: float,
                            account: float) -> None:
        contract = parse_occ(req.get("contract", "")) or {}
        today = et(now).date()
        snap = {}
        if contract:
            snaps = await self.data.alpaca.option_snapshots([contract["symbol"]]) if self.data.ok else {}
            snap = snaps.get(contract["symbol"]) or {}
            if not snap:                                     # e.g. a weekend date: use the nearest real expiry
                fixed = await self._nearest_contract(contract, today, und)
                if fixed:
                    contract = fixed
                    snaps = await self.data.alpaca.option_snapshots([contract["symbol"]])
                    snap = snaps.get(contract["symbol"]) or {}
        info["contract_info"] = contract
        if not contract:
            add(items, YELLOW, "Couldn't read the contract (try: NVDA 140c 10/24)")
            return
        q = quote(snap) if snap else {}
        info["quote"] = q
        days = dte(contract["expiry"], today)
        info["dte"] = days
        mid = num(q.get("mid")) or num(req.get("price"))
        if not snap:
            add(items, YELLOW, "No live quote for this contract — check the strike and expiry")
        else:
            sp = q.get("spread_pct")
            cap = self.cfg.get("OPTION_MAX_SPREAD_PCT")
            if sp is not None and sp > 2 * cap:
                add(items, RED, f"Option spread {sp:.0f}% — you give that up getting in and out", "option_spread")
            elif sp is not None and sp > cap:
                add(items, YELLOW, f"Option spread {sp:.0f}% (wide) — use limit orders at the mid", "option_spread")
            elif sp is not None:
                add(items, GREEN, f"Option spread {sp:.1f}%")
        oi = (await self.data.open_interest(contract["underlying"], contract["expiry"], contract["expiry"], und)
              ).get(contract["symbol"], -1)
        info["oi"] = oi
        if 0 <= oi < 100:
            add(items, RED, f"Open interest {oi:.0f}: few traders in this contract — hard to sell", "low_oi")
        elif 0 <= oi < self.cfg.get("OPTION_MIN_OI"):
            add(items, YELLOW, f"Open interest only {oi:.0f}", "low_oi")
        hhmm = et(now).hour * 60 + et(now).minute
        if days == 0 and not self.cfg.flag("ALLOW_0DTE"):
            if hhmm >= 14 * 60 + 30:
                add(items, RED, "Same-day expiry after 2:30pm: time decay eats the premium by the minute", "zero_dte_late")
            else:
                add(items, YELLOW, "Same-day expiry: decay speeds up all day — it needs a fast move", "zero_dte")
        elif days < 0:
            add(items, RED, "This contract has expired", "expired")
        delta = abs(num(q.get("delta")))
        if q and 0 < delta < 0.2:
            add(items, YELLOW, f"Lottery ticket (delta {delta:.2f}): needs a big move, usually expires worthless", "lottery")
        iv = num(q.get("iv"))
        if iv > 1.0:
            add(items, YELLOW, f"Implied volatility {iv * 100:.0f}%: expensive options — a move can still lose "
                               "when IV drops", "high_iv")
        if mid > 0 and contract.get("strike"):
            be = breakeven(contract["type"], contract["strike"], mid)
            need = abs(be - und) if (contract["type"] == "call" and be > und) or (contract["type"] == "put" and be < und) else 0.0
            em = expected_move(und, iv, max(days, 0.25)) if iv else 0.0
            info["breakeven"] = be
            if need and em and need > em:
                add(items, YELLOW, f"Needs {fmt_price(need)} ({need / und * 100:.1f}%) to break even by expiry — more "
                                   f"than the market expects ({fmt_price(em)})", "big_move_needed")
        if self.earnings and days >= 0:
            when = await self.earnings.next_report(contract["underlying"], now)
            if when and when.get("days") is not None and when["days"] <= days:
                add(items, YELLOW, f"Earnings {when['text']}, before this expires — IV usually collapses after", "earnings")
        # plan in option terms
        qty = num(req.get("qty"))
        if snap and mid > 0:
            d, g = num(q.get("delta")), num(q.get("gamma"))
            stop_move = plan["stop"] - und
            opt_at_stop = max(0.0, mid + value_change(d, g, stop_move))
            opt_t1 = max(0.0, mid + value_change(d, g, plan["t1"] - und))
            paid = num(req.get("price")) or mid
            loss_each = (opt_at_stop - paid) if req.get("short_option") else (paid - opt_at_stop)
            plan.update(opt_mid=mid, opt_at_stop=opt_at_stop, opt_t1=opt_t1, theta=num(q.get("theta")),
                        loss_each=loss_each * 100, mult=100)
            if qty:
                plan["risk_usd"] = max(loss_each, 0) * 100 * qty
        premium = (num(req.get("price")) or mid) * 100 * qty
        if req.get("source") != "fill" and not req.get("closing") and mid > 0:
            cash = spendable(self.db, req.get("account_id"))
            each = (num(req.get("price")) or num(q.get("ask")) or mid) * 100
            if cash is not None and each > cash:
                add(items, RED, f"One contract costs {money(each)} — you have {money(cash)} to buy with", "no_cash")
            elif cash is not None and qty and each * qty > cash * 1.01 + 1:
                add(items, RED, f"{qty:g} contracts cost {money(each * qty)} — you have {money(cash)} to buy with",
                    "no_cash")
        if qty and account and premium:
            share = premium / account * 100
            cap = self.cfg.get("OPTIONS_MAX_PREMIUM_PCT")
            if share > 2 * cap:
                add(items, RED, f"{money(premium)} of premium is {share:.0f}% of the account (rule {cap:g}%)", "premium_big")
            elif share > cap:
                add(items, YELLOW, f"{money(premium)} of premium — {share:.1f}% of the account (rule {cap:g}%)", "premium_big")

    async def _nearest_contract(self, contract: dict, today: dt.date, und: float) -> dict | None:
        try:
            exps = await self.data.expiries(contract["underlying"], today, und, 60)
        except Exception:
            return None
        later = [e for e in exps if e >= contract["expiry"]] or exps
        if not later:
            return None
        exp = min(later, key=lambda e: abs(dt.date.fromisoformat(e) - dt.date.fromisoformat(contract["expiry"])))
        sym = occ(contract["underlying"], exp, contract["type"][0], contract["strike"])
        return parse_occ(sym)


def format_check(res: dict, header: str = "") -> str:
    plan = res.get("plan") or {}
    sym = res["symbol"]
    s = 1 if res.get("direction") == "long" else -1
    title = header or f"<b>🧾 Check · {esc(sym)} {res.get('direction')} · {res['verdict']} {WORD[res['verdict']]}</b>"
    lines = [title]
    lv = res.get("levels") or {}
    bits = [f"{esc(sym)} {fmt_price(res.get('und_price'))}"]
    if lv.get("pdc"):
        bits.append(f"{pct((res['und_price'] / lv['pdc'] - 1) * 100)} today")
    if lv.get("vwap"):
        bits.append(f"VWAP {fmt_price(lv['vwap'])}")
    if lv.get("rvol"):
        bits.append(f"vol {lv['rvol']:.1f}×")
    lines.append(" · ".join(bits))
    if res.get("kind") == "option" and res.get("contract_info"):
        c = res["contract_info"]
        q = res.get("quote") or {}
        opt = f"{c['underlying']} {c['strike']:g}{c['type'][0].upper()} {c['expiry']} ({res.get('dte', '?')}d)"
        if q.get("mid"):
            opt += f" · {fmt_price(q['bid'])}/{fmt_price(q['ask'])} · Δ {q.get('delta', 0):.2f} · θ {money(q.get('theta', 0) * 100)}/day"
        if res.get("oi", -1) >= 0:
            opt += f" · OI {res['oi']:.0f}"
        lines.append(esc(opt))
    order = {RED: 0, YELLOW: 1, GREEN: 2}
    for it in sorted(res["items"], key=lambda it: order[it[0]]):
        lines.append(f"{it[0]} {esc(it[1])}")
    if plan.get("stop"):
        ln = (f"🎯 Plan: stop {fmt_price(plan['stop'])} on {esc(sym)} ({esc(plan.get('why', ''))}) · take half "
              f"{fmt_price(plan['t1'])} · rest {fmt_price(plan['t2'])} · out by 3:55pm")
        if res.get("kind") == "option" and plan.get("opt_at_stop") is not None:
            ln += (f"\n   Option ≈ {fmt_price(plan['opt_at_stop'])} at the stop, ≈ {fmt_price(plan['opt_t1'])} at the "
                   f"first target — or cut it at −35% of the premium, whichever comes first")
        if plan.get("risk_usd"):
            ln += f"\n   At risk if the stop hits: {money(plan['risk_usd'])}"
        elif plan.get("capped") and plan.get("cash") is not None:
            ln += (f"\n   Size: your {money(plan['cash'])} cash buys {fmt_qty(plan['max_qty'])} shares"
                   + (f" ({money(plan['max_risk'])} at risk at the stop)" if plan["max_qty"] else "")
                   + f" — your risk rule would allow {fmt_qty(plan['max_by_risk'])}")
        elif plan.get("max_qty"):
            ln += (f"\n   Size: up to {fmt_qty(plan['max_qty'])} shares at your risk rule "
                   f"({money(plan.get('max_risk', 0))} at risk at the stop)")
        if plan.get("next_level") and res.get("kind") == "stock":
            name, lvl = plan["next_level"]
            if s * (lvl - res["und_price"]) > 0:
                ln += f"\n   Next level: {esc(name)} {fmt_price(lvl)}"
        lines.append(ln)
    if plan.get("no_account"):
        lines.append("💼 Account size unknown, so size wasn't checked — /connect your broker or /set ACCOUNT_SIZE 25000")
    news = res.get("news") or []
    if news:
        n = news[0]
        tag = f" [{n['tags'][0][0]}]" if n["tags"] else ""
        lines.append(f"📰 {esc(n['headline'][:140])}{tag}")
    return "\n".join(lines)

