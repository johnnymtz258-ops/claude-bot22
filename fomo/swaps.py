"""Turn a confirmed Solana transaction into "wallet X bought/sold N of token Y for Z SOL".

The parser looks only at the wallet's balance changes before and after the transaction, so it
works the same for Pump.fun, PumpSwap, Raydium, Meteora, Orca, Jupiter routes and trading
bots (Photon, Axiom, BullX, Fomo...) without decoding each program.

Accuracy details that matter for small trades:
- Opening a token account locks a ~0.002 SOL deposit that comes back when it is closed. On a
  $5 buy that is ~8% of the trade, so deposits are removed from the swap amount instead of
  being counted as a cost or a loss.
- The network fee is removed from the swap price (so a whale's entry market cap is right) but
  reported separately so your own profit/loss can include it.
- Airdrops, transfers, and token-to-token swaps are ignored: there is no clean SOL/USD price.
"""
from __future__ import annotations

from collections import defaultdict

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLES = {USDC: "USDC", USDT: "USDT"}
BASE_MINTS = {WSOL, USDC, USDT}

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# Rent-exempt deposit for a token account: 165 bytes (classic) / 170 bytes (Token-2022 ATA).
RENT_LAMPORTS = {TOKEN_PROGRAM: 2_039_280, TOKEN_2022_PROGRAM: 2_074_080}
LAMPORTS = 1_000_000_000

MIN_SOL_LEG = 0.0005     # smaller SOL movements are fees/dust, not a trade
MIN_STABLE_LEG = 0.05    # USDC/USDT

DEX_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium LaunchLab",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca",
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG": "Moonshot",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
}


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def account_keys(tx: dict) -> list[str]:
    """All account keys in index order, including address-lookup-table accounts."""
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    raw = msg.get("accountKeys") or []
    keys = [k if isinstance(k, str) else str((k or {}).get("pubkey") or "") for k in raw]
    if raw and isinstance(raw[0], str):
        # "json" encoding lists lookup-table accounts separately; "jsonParsed" already merges them.
        loaded = ((tx or {}).get("meta") or {}).get("loadedAddresses") or {}
        keys += list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    return keys


def signers(tx: dict) -> list[str]:
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    raw = msg.get("accountKeys") or []
    if raw and isinstance(raw[0], dict):
        return [str(k.get("pubkey") or "") for k in raw if k.get("signer")]
    n = _int((msg.get("header") or {}).get("numRequiredSignatures")) or 1
    return [k for k in raw[:n] if isinstance(k, str)]


def _all_instructions(tx: dict):
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    yield from msg.get("instructions") or []
    for group in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        yield from group.get("instructions") or []


def _token_rows(meta: dict, field: str, owner: str) -> dict[int, dict]:
    """Token balance rows owned by `owner`, keyed by account index."""
    out = {}
    for row in meta.get(field) or []:
        if str(row.get("owner") or "") != owner:
            continue
        amount = (row.get("uiTokenAmount") or {})
        out[_int(row.get("accountIndex"))] = {
            "mint": str(row.get("mint") or ""),
            "raw": _int(amount.get("amount")),
            "decimals": _int(amount.get("decimals")),
            "program": str(row.get("programId") or TOKEN_PROGRAM),
        }
    return out


def _deposit_adjustment(tx: dict, wallet: str, keys: list[str], pre: dict, post: dict) -> int:
    """Lamports of token-account deposits paid (+) or refunded (-) to `wallet` in this tx.

    Adding the result to the wallet's SOL change removes deposits from the swap amount.
    """
    created = {i: post[i] for i in post if i not in pre}
    closed = {i: pre[i] for i in pre if i not in post}
    if not created and not closed:
        return 0
    payer_of: dict[str, str] = {}
    refund_to: dict[str, str] = {}
    parsed_any = False
    for ix in _all_instructions(tx):
        parsed = ix.get("parsed") if isinstance(ix, dict) else None
        if not isinstance(parsed, dict):
            continue
        parsed_any = True
        kind = str(parsed.get("type") or "")
        info = parsed.get("info") or {}
        if kind in {"create", "createIdempotent", "createAccount", "createAccountWithSeed"}:
            new = str(info.get("account") or info.get("newAccount") or "")
            if new:
                payer_of[new] = str(info.get("source") or "")
        elif kind == "closeAccount":
            acct = str(info.get("account") or "")
            if acct:
                refund_to[acct] = str(info.get("destination") or "")
    fee_payer = keys[0] if keys else ""
    adjust = 0
    for idx, row in created.items():
        address = keys[idx] if idx < len(keys) else ""
        payer = payer_of.get(address) if parsed_any else None
        if payer is None:
            payer = fee_payer  # no parsed instructions: assume the fee payer funded it
        if payer == wallet:
            adjust += RENT_LAMPORTS.get(row["program"], RENT_LAMPORTS[TOKEN_PROGRAM])
    for idx, row in closed.items():
        address = keys[idx] if idx < len(keys) else ""
        dest = refund_to.get(address) if parsed_any else None
        if dest is None:
            dest = wallet
        if dest == wallet:
            adjust -= RENT_LAMPORTS.get(row["program"], RENT_LAMPORTS[TOKEN_PROGRAM])
    return adjust


def dex_label(tx: dict) -> str:
    keys = set(account_keys(tx))
    for ix in _all_instructions(tx):
        pid = ix.get("programId") if isinstance(ix, dict) else None
        if pid:
            keys.add(str(pid))
    for program, label in DEX_PROGRAMS.items():
        if program in keys:
            return label
    return "swap"


def parse_swap(tx: dict, wallet: str, sol_usd: float = 0.0) -> dict | None:
    """Return the wallet's net BUY or SELL in this transaction, or None.

    Result keys: side, mint, token_amount, decimals, base ("SOL"/"USDC"/"USDT"), base_amount,
    price_base (base per token), pre_holding, holding_after, sell_fraction, new_position,
    fee_sol (network fee paid by this wallet), deposit_sol, ts, slot, dex.
    """
    if not tx or not wallet:
        return None
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    keys = account_keys(tx)
    if wallet not in keys:
        return None
    idx = keys.index(wallet)
    pre_l, post_l = meta.get("preBalances") or [], meta.get("postBalances") or []
    if idx >= len(pre_l) or idx >= len(post_l):
        return None

    fee = _int(meta.get("fee")) if idx == 0 else 0
    pre = _token_rows(meta, "preTokenBalances", wallet)
    post = _token_rows(meta, "postTokenBalances", wallet)
    deposit = _deposit_adjustment(tx, wallet, keys, pre, post)
    native = _int(post_l[idx]) - _int(pre_l[idx]) + fee + deposit

    totals: dict[str, dict] = defaultdict(lambda: {"pre": 0, "post": 0, "decimals": 0})
    for field, rows in (("pre", pre), ("post", post)):
        for row in rows.values():
            t = totals[row["mint"]]
            t[field] += row["raw"]
            t["decimals"] = max(t["decimals"], row["decimals"])

    def ui(mint: str, field: str) -> float:
        t = totals.get(mint)
        return (t[field] / 10 ** t["decimals"]) if t else 0.0

    sol = native / LAMPORTS + (ui(WSOL, "post") - ui(WSOL, "pre"))
    stables = {m: ui(m, "post") - ui(m, "pre") for m in STABLES}
    moved = {m: t for m, t in totals.items() if m not in BASE_MINTS and t["post"] != t["pre"]}
    up = [m for m, t in moved.items() if t["post"] > t["pre"]]
    down = [m for m, t in moved.items() if t["post"] < t["pre"]]
    if not moved or (up and down) or len(up) > 1 or len(down) > 1:
        return None  # nothing traded, token-to-token, or an ambiguous multi-token transaction

    side = "BUY" if up else "SELL"
    mint = (up or down)[0]
    direction = -1 if side == "BUY" else 1  # buys pay base (negative), sells receive it
    legs = []
    if sol * direction >= MIN_SOL_LEG:
        legs.append(("SOL", abs(sol), abs(sol) * sol_usd if sol_usd > 0 else None))
    for m, delta in stables.items():
        if delta * direction >= MIN_STABLE_LEG:
            legs.append((STABLES[m], abs(delta), abs(delta)))
    if not legs:
        return None  # airdrop, transfer, or a trade paid in something other than SOL/stables
    if len(legs) > 1:
        # Prefer the leg worth the most; without a SOL price prefer a $1+ stable leg.
        legs.sort(key=lambda leg: leg[2] if leg[2] is not None else (0.5 if leg[0] == "SOL" else -1), reverse=True)
    base, base_amount, _ = legs[0]

    t = totals[mint]
    pre_ui, post_ui = ui(mint, "pre"), ui(mint, "post")
    amount = abs(post_ui - pre_ui)
    if amount <= 0 or base_amount <= 0:
        return None
    return {
        "side": side,
        "mint": mint,
        "token_amount": amount,
        "decimals": t["decimals"],
        "base": base,
        "base_amount": base_amount,
        "price_base": base_amount / amount,
        "pre_holding": pre_ui,
        "holding_after": post_ui,
        "sell_fraction": min(1.0, amount / pre_ui) if side == "SELL" and pre_ui > 0 else 0.0,
        "new_position": side == "BUY" and pre_ui <= 0,
        "fee_sol": fee / LAMPORTS,
        "deposit_sol": deposit / LAMPORTS,
        "ts": _int(tx.get("blockTime")),
        "slot": _int(tx.get("slot")),
        "dex": dex_label(tx),
    }


def trader_swap(tx: dict, mint: str, sol_usd: float = 0.0) -> tuple[str, dict] | None:
    """For coin research: find which signer traded `mint` in this tx and parse their swap."""
    meta = (tx or {}).get("meta") or {}
    changed = set()
    for field in ("preTokenBalances", "postTokenBalances"):
        for row in meta.get(field) or []:
            if row.get("mint") == mint and row.get("owner"):
                changed.add(str(row["owner"]))
    for signer in signers(tx):
        if signer in changed:
            swap = parse_swap(tx, signer, sol_usd)
            if swap and swap["mint"] == mint:
                return signer, swap
    return None


def sol_transfers_out(tx: dict, wallet: str) -> list[tuple[str, int]]:
    """[(destination, lamports)] for plain SOL transfers sent by `wallet` in this transaction (system program
    transfers, top-level or inner). Used to follow a whale's funding path: whale -> fresh wallet -> next buy."""
    out = []
    if not tx or ((tx.get("meta") or {}).get("err") is not None):
        return out
    for ix in _all_instructions(tx):
        parsed = ix.get("parsed") if isinstance(ix, dict) else None
        if not isinstance(parsed, dict) or ix.get("program") != "system" or parsed.get("type") not in ("transfer",
                                                                                                    "transferWithSeed"):
            continue
        info = parsed.get("info") or {}
        if str(info.get("source") or "") == wallet and info.get("destination"):
            out.append((str(info["destination"]), _int(info.get("lamports"))))
    return out
