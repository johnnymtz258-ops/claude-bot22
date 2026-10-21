"""Options: contract symbols, reading a contract you type, and the numbers that decide whether a contract is
tradeable for a day trade (spread, open interest, delta, time decay, the move it needs)."""
from __future__ import annotations

import datetime as dt
import math
import re

from .util import num

_OCC = re.compile(r"^([A-Z][A-Z0-9.]{0,5})(\d{6})([CP])(\d{8})$")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"], start=1)}


def occ(underlying: str, expiry: str, cp: str, strike: float) -> str:
    """'NVDA', '2026-10-23', 'C', 140 -> 'NVDA261023C00140000'."""
    d = dt.date.fromisoformat(expiry[:10])
    return f"{underlying.upper()}{d:%y%m%d}{cp.upper()[:1]}{int(round(strike * 1000)):08d}"


def parse_occ(symbol: str) -> dict | None:
    s = str(symbol or "").replace(" ", "").upper()
    m = _OCC.match(s)
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    try:
        expiry = dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:])).isoformat()
    except ValueError:
        return None
    return {"symbol": s, "underlying": root, "expiry": expiry, "type": "call" if cp == "C" else "put",
            "strike": int(strike) / 1000}


def _next_weekday(d: dt.date, weekday: int) -> dt.date:
    return d + dt.timedelta(days=(weekday - d.weekday()) % 7)


def parse_contract_text(text: str, today: dt.date) -> dict | None:
    """What you'd type: 'NVDA 140c 10/24', 'nvda 10/24 140 call', 'SPY 580p 0dte', 'AAPL 230 calls friday',
    or an OCC symbol. Returns {underlying, type, strike, expiry ('' when not given)} or None."""
    raw = str(text or "").strip()
    if not raw:
        return None
    direct = parse_occ(raw.split()[0]) if raw.split() else None
    if direct:
        return direct
    low = raw.lower().replace("$", " ")
    tokens = low.replace(",", " ").split()
    ticker = next((t.upper() for t in tokens if re.fullmatch(r"[a-z]{1,5}(\.[a-z])?", t)
                   and t not in {"c", "p", "call", "calls", "put", "puts", "dte", "today", "tomorrow", "friday",
                                 "fri", "exp", "expiry", "weekly", "monthly", *(_MONTHS.keys())}), "")
    if not ticker:
        return None
    kind, strike = "", 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(calls?|puts?|c|p)\b", low)
    if m:
        strike, kind = float(m.group(1)), "call" if m.group(2).startswith("c") else "put"
    else:
        word = re.search(r"\b(calls?|puts?)\b", low)
        nums = [float(x) for x in re.findall(r"(?<![/\d-])(\d+(?:\.\d+)?)(?![/\d-])", low)]
        if word and nums:
            kind, strike = ("call" if word.group(1).startswith("c") else "put"), nums[0]
    if not kind or strike <= 0:
        return None
    expiry = ""
    if re.search(r"\b0\s*dte\b|\btoday\b", low):
        expiry = today.isoformat()
    elif re.search(r"\b1\s*dte\b|\btomorrow\b", low):
        nxt = today + dt.timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += dt.timedelta(days=1)
        expiry = nxt.isoformat()
    elif re.search(r"\b(this\s+)?fri(day)?\b|\bweekly\b", low):
        expiry = _next_weekday(today, 4).isoformat()
    else:
        iso_m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", low)
        md = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", low)
        mon = re.search(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s*(\d{1,2})\b", low)
        try:
            if iso_m:
                expiry = dt.date(int(iso_m.group(1)), int(iso_m.group(2)), int(iso_m.group(3))).isoformat()
            elif md or mon:
                month, day = (int(md.group(1)), int(md.group(2))) if md else (_MONTHS[mon.group(1)[:3]], int(mon.group(2)))
                year = today.year
                if md and md.group(3):
                    year = int(md.group(3)) + (2000 if len(md.group(3)) == 2 else 0)
                d = dt.date(year, month, day)
                if not (md and md.group(3)) and d < today:
                    d = dt.date(year + 1, month, day)
                expiry = d.isoformat()
        except ValueError:
            expiry = ""
    return {"underlying": ticker, "type": kind, "strike": strike, "expiry": expiry,
            "symbol": occ(ticker, expiry, kind[0], strike) if expiry else ""}


def dte(expiry: str, today: dt.date) -> int:
    try:
        return (dt.date.fromisoformat(expiry[:10]) - today).days
    except ValueError:
        return -1


def quote(snap: dict) -> dict:
    """bid / ask / mid / spread % / greeks / IV from an Alpaca option snapshot."""
    q = snap.get("latestQuote") or {}
    bid, ask = num(q.get("bp")), num(q.get("ap"))
    last = num((snap.get("latestTrade") or {}).get("p"))
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else (ask or last)
    spread = (ask - bid) / mid * 100 if bid > 0 and ask >= bid and mid > 0 else None
    g = snap.get("greeks") or {}
    return {"bid": bid, "ask": ask, "mid": mid, "last": last, "spread_pct": spread, "delta": num(g.get("delta")),
            "gamma": num(g.get("gamma")), "theta": num(g.get("theta")), "vega": num(g.get("vega")),
            "iv": num(snap.get("impliedVolatility")),
            "volume": num((snap.get("dailyBar") or {}).get("v"))}


def expected_move(price: float, iv: float, days: float) -> float:
    """One standard deviation of price movement over `days` calendar days implied by the option market."""
    if price <= 0 or iv <= 0 or days <= 0:
        return 0.0
    return price * iv * math.sqrt(days / 365)


def value_change(delta: float, gamma: float, move: float) -> float:
    """Approximate change in an option's price (per share) for a stock move (delta-gamma)."""
    return delta * move + 0.5 * gamma * move * move


def breakeven(kind: str, strike: float, premium: float) -> float:
    return strike + premium if kind == "call" else strike - premium


def pick_idea(chain: dict[str, dict], open_interest: dict[str, float], direction: str, spot: float,
              today: dt.date, min_dte: int, max_spread: float, min_oi: float) -> dict | None:
    """The cleanest contract for a day trade in `direction`: nearest expiry at least `min_dte` days out (max 3
    weeks), delta 0.40-0.65, tight spread, real open interest. None when nothing qualifies."""
    want = "call" if direction == "long" else "put"
    best, best_key = None, None
    for sym, snap in chain.items():
        info = parse_occ(sym)
        if not info or info["type"] != want:
            continue
        days = dte(info["expiry"], today)
        if days < min_dte or days > 21:
            continue
        q = quote(snap)
        d = abs(q["delta"])
        if not (0.40 <= d <= 0.65) or q["mid"] <= 0 or q["spread_pct"] is None or q["spread_pct"] > max_spread:
            continue
        oi = num(open_interest.get(sym), -1)
        if 0 <= oi < min_oi:
            continue
        key = (days, abs(d - 0.5), q["spread_pct"])
        if best_key is None or key < best_key:
            best_key = key
            best = {**info, **q, "dte": days, "oi": oi}
    return best


def size_contracts(idea: dict, stock_risk: float, risk_budget: float, account: float, max_premium_pct: float) -> dict:
    """How many contracts so that the stop on the stock costs about `risk_budget`, capped by premium."""
    per_share_loss = abs(value_change(abs(idea.get("delta", 0)), idea.get("gamma", 0), -abs(stock_risk)))
    spread_cost = max(0.0, (idea.get("ask", 0) - idea.get("bid", 0)) / 2)
    loss_per_contract = (per_share_loss + spread_cost) * 100
    cap = account * max_premium_pct / 100 if account > 0 else 0
    premium = idea.get("ask") or idea.get("mid", 0)
    n = int(risk_budget // loss_per_contract) if loss_per_contract > 0 else 0
    if cap and premium > 0:
        n = min(n, int(cap // (premium * 100)))
    return {"contracts": max(n, 0), "loss_at_stop": loss_per_contract * max(n, 0),
            "loss_per_contract": loss_per_contract, "premium_each": premium * 100}
