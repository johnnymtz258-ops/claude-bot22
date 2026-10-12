"""Read pump.fun trades straight from the transaction logs the wallet stream already delivers.

Every pump.fun bonding-curve trade logs its TradeEvent ("Program data: vdt/007m…"): the coin, SOL in, tokens out,
buy or sell, the trader, the time and the curve's reserves. For a whale BUY that is everything an alert needs, so the
alert doesn't wait on a getTransaction round trip — which can take seconds to index, fail under a rate limit, or not
come at all when the RPC plan is used up. (About a quarter of your whales' trades are on the curve.)
Sells still load the full transaction: the share of the bag sold needs the wallet's balances.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import struct

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TRADE_EVENT = hashlib.sha256(b"event:TradeEvent").digest()[:8]   # Anchor event discriminator
TRADE_MIN_LEN = 113            # discriminator + mint + sol + tokens + is_buy + user + timestamp + 2 reserves
PUMP_DECIMALS = 6
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        n = n * 58 + _B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def encode_trade(*, mint: str, sol_lamports: int, token_raw: int, is_buy: bool, user: str, ts: int,
                 virtual_sol: int = 30_000_000_000, virtual_token: int = 1_073_000_000_000_000) -> str:
    """A TradeEvent as pump.fun logs it (used by tests and to check the decoder)."""
    raw = (TRADE_EVENT + b58decode(mint).rjust(32, b"\0") + struct.pack("<QQ?", sol_lamports, token_raw, is_buy)
           + b58decode(user).rjust(32, b"\0") + struct.pack("<qQQ", ts, virtual_sol, virtual_token))
    return "Program data: " + base64.b64encode(raw).decode()


def trades(logs: list | None) -> list[dict]:
    """Every pump.fun TradeEvent in a transaction's logs. Only events logged BY the pump.fun program count (a fork
    with an event of the same name, called in the same transaction, is ignored)."""
    out: list[dict] = []
    stack: list[str] = []
    for line in logs or []:
        if not isinstance(line, str):
            continue
        if line.startswith("Program ") and " invoke [" in line:
            stack.append(line.split(" ", 2)[1])
        elif line.startswith("Program ") and (line.endswith(" success") or " failed" in line):
            program = line.split(" ", 2)[1]
            if stack and stack[-1] == program:
                stack.pop()
        elif line.startswith("Program data: ") and stack and stack[-1] == PUMP_PROGRAM:
            try:
                raw = base64.b64decode(line[len("Program data: "):].strip(), validate=True)
            except (binascii.Error, ValueError):
                continue
            if raw[:8] != TRADE_EVENT or len(raw) < TRADE_MIN_LEN:
                continue
            sol, tokens, is_buy = struct.unpack_from("<QQ?", raw, 40)
            ts, v_sol, v_token = struct.unpack_from("<qQQ", raw, 89)
            out.append({"mint": b58encode(raw[8:40]), "sol_lamports": sol, "token_raw": tokens, "is_buy": bool(is_buy),
                        "user": b58encode(raw[57:89]), "ts": ts, "virtual_sol": v_sol, "virtual_token": v_token})
    return out


def buy_by(logs: list | None, wallet: str) -> dict | None:
    """The wallet's pump.fun BUY in these logs (several fills of one coin added together), or None."""
    mine = [t for t in trades(logs) if t["user"] == wallet]
    if not mine or not all(t["is_buy"] for t in mine) or len({t["mint"] for t in mine}) != 1:
        return None   # a sell, or something mixed: the full transaction is read instead
    first = mine[0]
    return {**first, "sol_lamports": sum(t["sol_lamports"] for t in mine), "token_raw": sum(t["token_raw"] for t in mine)}


def as_swap(event: dict, prior_holding: float) -> dict:
    """The event in the shape parse_swap() returns, so the rest of the engine treats it like any whale buy."""
    tokens = event["token_raw"] / 10 ** PUMP_DECIMALS
    sol = event["sol_lamports"] / 1e9
    return {"side": "BUY", "mint": event["mint"], "token_amount": tokens, "decimals": PUMP_DECIMALS, "base": "SOL",
            "base_amount": sol, "price_base": sol / tokens if tokens > 0 else 0.0, "pre_holding": prior_holding,
            "holding_after": prior_holding + tokens, "sell_fraction": 0.0, "new_position": prior_holding <= 0,
            "fee_sol": 0.0, "deposit_sol": 0.0, "ts": int(event["ts"]), "slot": 0, "dex": "Pump.fun"}
