"""Token traps: things a coin's own program can do to the people who buy it.

Most memecoins are plain SPL tokens. Token-2022 coins can carry extensions, and a few of them exist only to trap
buyers. These are read from the mint account the bot already loads (one getAccountInfo, cached for hours):
  permanent delegate   — the dev can move or burn tokens out of ANY wallet, yours included
  transfer hook        — every transfer runs the dev's program, which can refuse your sell
  non-transferable     — you can buy it but never sell it
  frozen by default    — new token accounts start frozen (you hold it, you can't move it)
  pausable             — the dev can stop all transfers at any moment
  transfer tax         — a cut of every buy and sell goes to the dev (shown as a %; 1%+ is treated as a trap)
A coin with any of these is never alerted and never bought by the live autopilot.

Before a real buy the live autopilot also asks Jupiter for the SELL quote of the tokens it is about to get: if selling
straight back would return much less than it pays (a honeypot, a hidden tax, no liquidity to sell into), it skips.
"""
from __future__ import annotations

from .util import num

TAX_TRAP_BPS = 100   # 1%


def token_traps(info: dict) -> list[str]:
    """Trap descriptions for a parsed mint account (`getAccountInfo` jsonParsed `info`). Empty for a plain token."""
    traps = []
    for ext in info.get("extensions") or []:
        if not isinstance(ext, dict):
            continue
        name = str(ext.get("extension") or "")
        state = ext.get("state") if isinstance(ext.get("state"), dict) else {}
        if name == "permanentDelegate" and state.get("delegate"):
            traps.append("permanent delegate: the dev can take tokens out of any wallet")
        elif name == "transferHook" and state.get("programId"):
            traps.append("transfer hook: every transfer runs the dev's program (it can block your sell)")
        elif name == "nonTransferable":
            traps.append("non-transferable: you could buy it but never sell it")
        elif name == "defaultAccountState" and str(state.get("accountState") or "").lower() == "frozen":
            traps.append("new token accounts start frozen")
        elif name in ("pausableConfig", "pausable") and state.get("authority"):
            traps.append("pausable: the dev can stop all transfers")
        elif name == "transferFeeConfig":
            bps = max(int(num(((state.get(k) or {}).get("transferFeeBasisPoints")))) for k in
                      ("newerTransferFee", "olderTransferFee"))
            if bps >= TAX_TRAP_BPS:
                traps.append(f"transfer tax {bps / 100:g}% on every buy and sell")
    return traps


def roundtrip(spend_lamports: int, sell_back_lamports: int) -> float:
    """Share of the SOL you'd get back selling straight away (1.0 = all of it)."""
    return sell_back_lamports / spend_lamports if spend_lamports > 0 else 0.0
