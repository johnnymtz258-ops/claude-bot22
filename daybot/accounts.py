"""How big your account is, and how much cash you can actually buy with, from the accounts SnapTrade synced.

Only accounts you day-trade in count (the default; /accounts off N marks one as investing and leaves it out).
"""
from __future__ import annotations

from .util import num


def _checked(db, account_id: str | None = None) -> list[dict]:
    if account_id:
        return db.rows("select * from accounts where id=?", (account_id,))
    rows = db.rows("select * from accounts where coalesce(checks, 1)=1")
    return rows or db.rows("select * from accounts")


def account_size(db, cfg, account_id: str | None = None) -> float:
    """ACCOUNT_SIZE if you set it, else the account the trade is in, else your biggest day-trading account."""
    fixed = cfg.get("ACCOUNT_SIZE")
    if fixed > 0:
        return fixed
    if account_id:
        v = num(db.scalar("select total from accounts where id=?", (account_id,)))
        if v > 0:
            return v
    return max((num(a["total"]) for a in _checked(db)), default=0.0)


def spendable(db, account_id: str | None = None) -> float | None:
    """Cash you can buy with right now (the smaller of cash and buying power, so margin is never counted as money
    you have). None = not synced yet, so nothing is capped."""
    best = None
    for a in _checked(db, account_id):
        if a.get("cash") is None and a.get("buying_power") is None:
            continue
        vals = [v for v in (num(a.get("cash")), num(a.get("buying_power"))) if v > 0]
        v = min(vals) if vals else 0.0
        best = v if best is None else max(best, v)
    return best


def fmt_qty(q: float) -> str:
    """Shares for people: 12, 0.37, 1.5 — never 0.37000000001."""
    q = float(q or 0)
    if abs(q - round(q)) < 1e-6:
        return f"{int(round(q))}"
    return f"{q:.4f}".rstrip("0").rstrip(".")


def _round_down(q: float) -> float:
    """Whole shares when at least one fits, else a fraction (two decimals)."""
    return float(int(q)) if q >= 1 else int(q * 100) / 100


def size_shares(budget: float, risk_ps: float, price: float, cash: float | None) -> dict:
    """Shares so the stop costs about `budget`, never more than your cash buys. Fractional when a whole share
    doesn't fit (Robinhood and Chase both sell fractional shares of most stocks)."""
    by_risk = _round_down(budget / risk_ps) if risk_ps > 0 and budget > 0 else 0.0
    out = {"by_risk": by_risk, "shares": by_risk, "cash": cash, "capped": False}
    if cash is not None and price > 0 and cash / price < by_risk:
        out.update(shares=_round_down(cash / price), capped=True)
    out["cost"] = out["shares"] * price
    out["risk_usd"] = out["shares"] * risk_ps
    return out
