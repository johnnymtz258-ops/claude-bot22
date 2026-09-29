"""Simulated follower copies: "what if I bought when the alert arrived and sold when the whale sold".

This is how the bot scores whales. A copy:
- enters at the live price when the alert is sent (not the whale's better price),
- pays COPY_FEE_PCT on the buy and on every sell,
- sells the same fraction the whale sells, and nothing else — there is no tight stop-loss,
  because meme coins often dip hard before the real run and a stop would sell the bottom,
- closes when the whale is out, after COPY_MAX_HOLD_HOURS, or if the coin is rugged.

It also records the price path (peak, the dip before the peak, time to 2x, checkpoints), which
is what lets the bot tell you "this whale's winners typically dip -30% first".
"""
from __future__ import annotations

import time

from .util import num

CHECKPOINTS = (("p5m", 300), ("p15m", 900), ("p1h", 3600), ("p4h", 14400), ("p24h", 86400))
DUST_REMAINING = 0.02
# A single price reading this far from the previous one is held until a second reading agrees.
GLITCH_JUMP = 8.0


def return_pct(copy: dict, price: float, fee_pct: float) -> float:
    """Net % return if the rest of the copy were sold at `price` now."""
    entry = num(copy.get("entry_price"))
    if entry <= 0:
        return 0.0
    keep = 1 - fee_pct / 100
    value = num(copy.get("proceeds")) + num(copy.get("remaining"), 1.0) * keep * keep * max(price, 0.0) / entry
    return (value - 1) * 100


def open_copy(db, *, whale: str, mint: str, swap_id: int, alert_id: int, price: float, mc_usd: float,
              whale_price: float, ts: int | None = None) -> int:
    if price <= 0 or db.scalar("select id from copies where whale=? and mint=? and status='open'", (whale, mint)):
        return 0
    ts = int(ts or time.time())
    return db.insert("""insert into copies(whale,mint,swap_id,alert_id,open_ts,entry_price,entry_mc,whale_price,
            last_price,last_ts,peak_price,peak_ts,low_price) values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (whale, mint, swap_id, alert_id, ts, price, mc_usd, whale_price, price, ts, price, ts, price))


def sell(db, copy: dict, fraction: float, price: float, fee_pct: float, reason: str, ts: int | None = None) -> dict:
    """Sell `fraction` of what is left. Closes the copy when (almost) nothing remains."""
    entry = num(copy.get("entry_price"))
    remaining = num(copy.get("remaining"), 1.0)
    if entry <= 0 or remaining <= 0 or price < 0:
        return copy
    fraction = min(1.0, max(0.0, fraction))
    keep = 1 - fee_pct / 100
    sold = remaining * fraction
    copy = dict(copy)
    copy["proceeds"] = num(copy.get("proceeds")) + sold * keep * keep * price / entry
    copy["remaining"] = remaining - sold
    ts = int(ts or time.time())
    if copy["remaining"] <= DUST_REMAINING:
        # the dust left over is valued at the same price
        copy["proceeds"] += copy["remaining"] * keep * keep * price / entry
        copy["remaining"] = 0.0
        copy.update(status="closed", close_ts=ts, close_reason=reason,
                    return_pct=(copy["proceeds"] - 1) * 100)
    db.run("""update copies set remaining=?,proceeds=?,status=?,close_ts=?,close_reason=?,return_pct=? where id=?""",
           (copy["remaining"], copy["proceeds"], copy.get("status", "open"), copy.get("close_ts", 0),
            copy.get("close_reason", ""), copy.get("return_pct"), copy["id"]))
    return copy


def accept_price(copy: dict, price: float, ts: int) -> float | None:
    """Glitch filter: a >8x jump from the last reading must be seen twice before it counts."""
    last = num(copy.get("last_price"))
    if price <= 0:
        return None
    if last > 0 and (price / last >= GLITCH_JUMP or last / price >= GLITCH_JUMP):
        pending = num(copy.get("pending_price"))
        if pending > 0 and 0.8 <= price / pending <= 1.25 and ts - int(num(copy.get("pending_ts"))) >= 15:
            return price
        copy["pending_price"], copy["pending_ts"] = price, ts
        return None
    return price


def update_path(db, copy: dict, price: float, ts: int | None = None, trusted: bool = False) -> dict:
    """Record a new price reading for an open copy (peak, dip-before-peak, checkpoints, 2x).

    `trusted` skips the glitch filter for prices confirmed by an actual on-chain trade.
    """
    ts = int(ts or time.time())
    copy = dict(copy)
    accepted = price if trusted and price > 0 else accept_price(copy, price, ts)
    if accepted is None:
        db.run("update copies set pending_price=?,pending_ts=? where id=?",
               (copy.get("pending_price"), copy.get("pending_ts"), copy["id"]))
        return copy
    entry = num(copy.get("entry_price"))
    copy["last_price"], copy["last_ts"] = accepted, ts
    copy["pending_price"], copy["pending_ts"] = None, None
    low = num(copy.get("low_price"), entry) or entry
    if accepted < low:
        copy["low_price"] = low = accepted
    if accepted > num(copy.get("peak_price")):
        copy["peak_price"], copy["peak_ts"] = accepted, ts
        copy["dip_before_peak_pct"] = (low / entry - 1) * 100 if entry > 0 else 0.0
    if entry > 0 and accepted >= 2 * entry and not copy.get("hit_2x_ts"):
        copy["hit_2x_ts"] = ts
    age = ts - int(num(copy.get("open_ts"), ts))
    for column, seconds in CHECKPOINTS:
        if copy.get(column) is None and age >= seconds:
            copy[column] = accepted
    db.run("""update copies set last_price=?,last_ts=?,low_price=?,peak_price=?,peak_ts=?,dip_before_peak_pct=?,
            hit_2x_ts=?,p5m=?,p15m=?,p1h=?,p4h=?,p24h=?,pending_price=null,pending_ts=null where id=?""",
           (copy["last_price"], copy["last_ts"], copy.get("low_price"), copy.get("peak_price"), copy.get("peak_ts"),
            copy.get("dip_before_peak_pct", 0), copy.get("hit_2x_ts", 0), copy.get("p5m"), copy.get("p15m"),
            copy.get("p1h"), copy.get("p4h"), copy.get("p24h"), copy["id"]))
    return copy
