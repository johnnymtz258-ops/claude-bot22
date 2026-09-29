"""Optional read-only Solana wallet synchronization for Fomo Bot v11.

Only a PUBLIC wallet address is required. No seed phrase/private key is read here.
The sync layer is intended to reconcile positions the bot already knows about; it
never places a transaction.
"""
from __future__ import annotations

import os
from typing import Any

BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
DEFAULT_RPC = os.getenv("SOLANA_RPC_HTTP", "https://api.mainnet-beta.solana.com").strip()


def valid_solana_address(value: str) -> bool:
    value = str(value or "").strip()
    return 32 <= len(value) <= 44 and all(c in BASE58 for c in value)


class PublicSolanaWalletSync:
    def __init__(self, address: str = "", rpc: str = ""):
        self.address = str(address or os.getenv("PUBLIC_SOLANA_WALLET_ADDRESS", "")).strip()
        self.rpc = str(rpc or DEFAULT_RPC).strip()
        self.enabled = valid_solana_address(self.address)

    async def _rpc(self, http, method: str, params: list[Any]):
        payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
        try:
            async with http.session.post(self.rpc, json=payload, timeout=12) as response:
                data = await response.json(content_type=None)
                if response.status != 200 or not isinstance(data, dict) or data.get("error"):
                    return None
                return data.get("result")
        except Exception:
            return None

    async def token_balance(self, http, mint: str) -> dict | None:
        if not self.enabled or not valid_solana_address(mint):
            return None
        result = await self._rpc(http, "getTokenAccountsByOwner", [
            self.address,
            {"mint": mint},
            {"encoding":"jsonParsed","commitment":"confirmed"},
        ])
        if not isinstance(result, dict):
            return None
        raw_total = 0
        ui_total = 0.0
        decimals = None
        try:
            for item in result.get("value", []):
                amount = item["account"]["data"]["parsed"]["info"]["tokenAmount"]
                raw_total += int(amount.get("amount") or 0)
                dec = int(amount.get("decimals") or 0)
                decimals = dec if decimals is None else decimals
                ui = amount.get("uiAmountString")
                if ui is None:
                    ui_total += int(amount.get("amount") or 0) / (10 ** dec)
                else:
                    ui_total += float(ui)
            return {"raw": raw_total, "ui": ui_total, "decimals": int(decimals or 0)}
        except Exception:
            return None

    async def sol_balance(self, http) -> float | None:
        if not self.enabled:
            return None
        result = await self._rpc(http, "getBalance", [self.address, {"commitment":"confirmed"}])
        if not isinstance(result, dict):
            return None
        try:
            return float(result.get("value", 0)) / 1_000_000_000
        except Exception:
            return None


def reconcile_balance(last_ui: float | None, current_ui: float | None, min_change_pct: float = 3.0) -> dict:
    """Describe a material balance change relative to the previous wallet snapshot."""
    if current_ui is None:
        return {"event":"unavailable"}
    current = max(float(current_ui), 0.0)
    if last_ui is None:
        return {"event":"baseline", "current":current}
    last = max(float(last_ui), 0.0)
    if last <= 0:
        if current > 0:
            return {"event":"increase", "current":current, "change_pct":100.0}
        return {"event":"unchanged", "current":current, "change_pct":0.0}
    pct = (current / last - 1.0) * 100.0
    if abs(pct) < max(float(min_change_pct), 0.1):
        return {"event":"unchanged", "current":current, "change_pct":pct}
    if current <= max(last * 0.01, 1e-12):
        return {"event":"closed", "current":current, "change_pct":pct, "sold_fraction":1.0}
    if current < last:
        return {"event":"decrease", "current":current, "change_pct":pct,
                "sold_fraction":min(1.0, max(0.0, (last-current)/last))}
    return {"event":"increase", "current":current, "change_pct":pct}

USDC_MINT='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'


def _account_key(item):
    if isinstance(item,str): return item
    if isinstance(item,dict): return str(item.get('pubkey') or '')
    return ''


def _owner_token_amounts(meta, side, owner):
    out={}
    rows=(meta or {}).get(side+'TokenBalances') or []
    for row in rows:
        if str(row.get('owner') or '') != owner: continue
        mint=str(row.get('mint') or '')
        amt=((row.get('uiTokenAmount') or {}).get('uiAmountString'))
        try:
            value=float(amt) if amt is not None else float((row.get('uiTokenAmount') or {}).get('uiAmount') or 0)
        except Exception:
            value=0.0
        out[mint]=out.get(mint,0.0)+value
    return out


async def recent_sale_evidence(sync: PublicSolanaWalletSync, http, mint: str, since_ts: int = 0, limit: int = 8) -> dict | None:
    """Look for a recent on-chain swap-like event before auto-recording a sale.

    A mere token balance decrease might be a transfer. We only call it a sale when
    the target token decreased and the wallet simultaneously received SOL or a known
    stablecoin in the same transaction.
    """
    if not sync.enabled or not valid_solana_address(mint):
        return None
    sigs=await sync._rpc(http,'getSignaturesForAddress',[sync.address,{'limit':max(1,min(int(limit),20)),'commitment':'confirmed'}])
    if not isinstance(sigs,list): return None
    for item in sigs:
        bt=int(item.get('blockTime') or 0)
        if since_ts and bt and bt < int(since_ts)-30: continue
        sig=str(item.get('signature') or '')
        if not sig: continue
        tx=await sync._rpc(http,'getTransaction',[sig,{'encoding':'jsonParsed','commitment':'confirmed','maxSupportedTransactionVersion':0}])
        if not isinstance(tx,dict): continue
        meta=tx.get('meta') or {}; message=((tx.get('transaction') or {}).get('message') or {})
        keys=[_account_key(x) for x in (message.get('accountKeys') or [])]
        try: idx=keys.index(sync.address)
        except ValueError: idx=-1
        pre=_owner_token_amounts(meta,'pre',sync.address); post=_owner_token_amounts(meta,'post',sync.address)
        target_delta=post.get(mint,0.0)-pre.get(mint,0.0)
        if target_delta >= -1e-12: continue
        # USDC is the only stable mint hard-coded here. SOL receipts are also accepted.
        # Avoid treating an uncertain/stale stablecoin mint as sale evidence.
        stable_delta=(post.get(USDC_MINT,0.0)-pre.get(USDC_MINT,0.0))
        sol_delta=0.0
        if idx>=0:
            try:
                sol_delta=(float((meta.get('postBalances') or [])[idx])-float((meta.get('preBalances') or [])[idx]))/1_000_000_000
            except Exception:
                sol_delta=0.0
        if stable_delta>0.01 or sol_delta>0.00001:
            return {'signature':sig,'block_time':bt,'sold_ui':abs(target_delta),'sol_received':max(sol_delta,0.0),'stable_received':max(stable_delta,0.0)}
    return None
