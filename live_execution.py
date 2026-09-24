"""Hardened optional local Solana execution for FomoBot v15.2.

The execution path is deliberately fail-closed:
- wallet secrets stay in a local Solana CLI-style JSON keypair file;
- the keypair fingerprint is revalidated before every signature;
- balances and RPC responses carry explicit success/error state;
- a signed transaction signature is durably journaled before network submission;
- Solana confirmation, not an HTTP response, is the source of execution truth;
- post-trade wallet deltas are used to reconcile actual received amounts;
- ambiguous executions remain locked for startup/runtime reconciliation instead of retrying blindly.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from pathlib import Path

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JUPITER_BASE = "https://api.jup.ag/swap/v2"
DEX = "https://api.dexscreener.com"

_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    if not raw:
        return ""
    zeros = 0
    for b in raw:
        if b == 0:
            zeros += 1
        else:
            break
    n = int.from_bytes(raw, "big")
    chars = bytearray()
    while n:
        n, r = divmod(n, 58)
        chars.append(_B58_ALPHABET[r])
    return (b"1" * zeros + bytes(reversed(chars or b""))).decode()


def _read_shortvec(buf: bytes, offset: int):
    value = 0
    shift = 0
    start = offset
    while True:
        if offset >= len(buf):
            raise ValueError("shortvec truncated")
        b = buf[offset]
        offset += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, offset, offset - start
        shift += 7
        if shift > 28:
            raise ValueError("shortvec too large")


def load_keypair(path: str | Path):
    p = Path(os.path.expanduser(str(path)))
    data = json.loads(p.read_text())
    if not isinstance(data, list) or len(data) not in (32, 64):
        raise ValueError("keypair file must contain a JSON array of 32 or 64 bytes")
    raw = bytes(int(x) & 0xFF for x in data)
    seed = raw[:32]
    key = Ed25519PrivateKey.from_private_bytes(seed)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if len(raw) == 64 and raw[32:] != pub:
        raise ValueError("keypair public key does not match private seed")
    return key, pub


def wallet_address(path: str | Path) -> str:
    _, pub = load_keypair(path)
    return b58encode(pub)


def generate_keypair(path: str | Path) -> str:
    p = Path(os.path.expanduser(str(path)))
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        raise FileExistsError(str(p))
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    p.write_text(json.dumps(list(seed + pub)))
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return b58encode(pub)


def keypair_fingerprint(path: str | Path) -> str:
    p = Path(os.path.expanduser(str(path)))
    # Validate structure as part of fingerprinting; never hash an arbitrary malformed file.
    load_keypair(p)
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _wallet_signer_index(tx_raw: bytes, pub: bytes):
    sig_count, after_count, _ = _read_shortvec(tx_raw, 0)
    sigs_start = after_count
    message_start = sigs_start + sig_count * 64
    if message_start >= len(tx_raw):
        raise ValueError("transaction message missing")
    message = bytes(tx_raw[message_start:])
    header_start = 1 if (message[0] & 0x80) else 0
    if len(message) < header_start + 4:
        raise ValueError("transaction header truncated")
    required_signers = message[header_start]
    account_count, keys_start, _ = _read_shortvec(message, header_start + 3)
    if account_count < required_signers:
        raise ValueError("invalid signer/account count")
    keys_end = keys_start + account_count * 32
    if keys_end > len(message):
        raise ValueError("account key list truncated")
    for i in range(required_signers):
        start = keys_start + i * 32
        if message[start:start + 32] == pub:
            if i >= sig_count:
                raise ValueError("signature array is shorter than required signers")
            return i, sigs_start, message_start, message
    raise ValueError("wallet is not a required signer in Jupiter transaction")


def sign_solana_transaction(tx_b64: str, keypair_path: str | Path) -> str:
    """Partially sign a legacy/v0 transaction while preserving other signatures."""
    raw = bytearray(base64.b64decode(tx_b64))
    key, pub = load_keypair(keypair_path)
    signer_index, sigs_start, _message_start, message = _wallet_signer_index(bytes(raw), pub)
    signature = key.sign(message)
    s0 = sigs_start + signer_index * 64
    raw[s0:s0 + 64] = signature
    return base64.b64encode(bytes(raw)).decode()


def transaction_signature(tx_b64: str, keypair_path: str | Path) -> str:
    raw = base64.b64decode(tx_b64)
    _, pub = load_keypair(keypair_path)
    signer_index, sigs_start, _message_start, _message = _wallet_signer_index(raw, pub)
    s0 = sigs_start + signer_index * 64
    sig = raw[s0:s0 + 64]
    if len(sig) != 64 or not any(sig):
        raise ValueError("wallet signature is missing")
    return b58encode(sig)


def classify_network_error(exc) -> tuple[str, bool]:
    if isinstance(exc, asyncio.TimeoutError):
        return "TIMEOUT", True
    if isinstance(exc, (aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError)):
        return "CONNECTION", True
    if isinstance(exc, aiohttp.ClientResponseError):
        retryable = int(getattr(exc, "status", 0) or 0) in {408, 409, 425, 429, 500, 502, 503, 504}
        return f"HTTP_{getattr(exc, 'status', 0)}", retryable
    return type(exc).__name__.upper(), False


class LiveExecutor:
    def __init__(self):
        self.capable = os.getenv("LIVE_TRADING_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
        self.api_key = os.getenv("JUPITER_API_KEY", "").strip()
        self.keypair_path = os.path.expanduser(os.getenv("LIVE_WALLET_KEYPAIR_PATH", "").strip())
        primary = os.getenv("SOLANA_RPC_HTTP", "https://api.mainnet-beta.solana.com").strip()
        fallbacks = [x.strip() for x in os.getenv("SOLANA_RPC_FALLBACKS", "").replace(";", ",").split(",") if x.strip()]
        self.rpc_urls = []
        for url in [primary, *fallbacks, "https://api.mainnet-beta.solana.com"]:
            if url and url not in self.rpc_urls:
                self.rpc_urls.append(url)
        self.rpc = self.rpc_urls[0]
        try:
            self.max_price_impact = float(os.getenv("AUTO_LIVE_MAX_PRICE_IMPACT_PCT", "4"))
        except Exception:
            self.max_price_impact = 4.0
        try:
            self.max_fill_slippage = float(os.getenv("AUTO_LIVE_MAX_FILL_SLIPPAGE_PCT", "6"))
        except Exception:
            self.max_fill_slippage = 6.0
        try:
            self.http_timeout = max(3.0, float(os.getenv("LIVE_HTTP_TIMEOUT_SECONDS", "12")))
        except Exception:
            self.http_timeout = 12.0
        try:
            self.confirm_timeout = max(5.0, float(os.getenv("LIVE_CONFIRM_TIMEOUT_SECONDS", "35")))
        except Exception:
            self.confirm_timeout = 35.0
        try:
            self.min_sol_reserve = max(0.0, float(os.getenv("AUTO_LIVE_MIN_SOL_RESERVE", "0.03")))
        except Exception:
            self.min_sol_reserve = 0.03
        self._startup_keypair_fingerprint = ""
        if self.keypair_path and Path(self.keypair_path).exists():
            try:
                self._startup_keypair_fingerprint = keypair_fingerprint(self.keypair_path)
            except Exception:
                pass
        self.last_rpc_url = self.rpc
        self.last_rpc_error = ""

    def keypair_integrity(self):
        if not self.keypair_path:
            return False, "LIVE_WALLET_KEYPAIR_PATH is missing"
        p = Path(self.keypair_path)
        if not p.exists():
            return False, "live wallet keypair file is missing"
        try:
            current = keypair_fingerprint(p)
        except Exception as exc:
            return False, f"wallet keypair invalid: {exc}"
        if self._startup_keypair_fingerprint and current != self._startup_keypair_fingerprint:
            return False, "wallet keypair changed since bot startup; restart required before signing"
        if not self._startup_keypair_fingerprint:
            self._startup_keypair_fingerprint = current
        return True, ""

    def readiness(self):
        issues = []
        if not self.capable:
            issues.append("LIVE_TRADING_ENABLED is off")
        if not self.api_key:
            issues.append("JUPITER_API_KEY is missing")
        ok, msg = self.keypair_integrity()
        if not ok:
            issues.append(msg)
        return issues

    def ready(self) -> bool:
        return not self.readiness()

    def address(self) -> str:
        if not self.keypair_path:
            return ""
        try:
            return wallet_address(self.keypair_path)
        except Exception:
            return ""

    async def _rpc_checked(self, http, method, params):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        errors = []
        timeout = aiohttp.ClientTimeout(total=self.http_timeout)
        for url in self.rpc_urls:
            try:
                async with http.session.post(url, json=payload, timeout=timeout) as resp:
                    data = await resp.json(content_type=None)
                if resp.status == 200 and isinstance(data, dict) and not data.get("error"):
                    self.last_rpc_url = url
                    self.last_rpc_error = ""
                    return {"ok": True, "value": data.get("result"), "rpc": url}
                detail = (data or {}).get("error") if isinstance(data, dict) else data
                errors.append(f"{url}: HTTP {resp.status}: {detail}")
            except Exception as exc:
                kind, _ = classify_network_error(exc)
                errors.append(f"{url}: {kind}: {exc}")
        self.last_rpc_error = " | ".join(errors)[-2000:]
        return {"ok": False, "error": self.last_rpc_error or "all RPC endpoints failed", "value": None}

    async def _rpc(self, http, method, params):
        result = await self._rpc_checked(http, method, params)
        return result.get("value") if result.get("ok") else None

    async def sol_balance_raw_checked(self, http):
        addr = self.address()
        if not addr:
            return {"ok": False, "error": "wallet address unavailable", "raw": None, "value": None}
        result = await self._rpc_checked(http, "getBalance", [addr, {"commitment": "confirmed"}])
        if not result.get("ok") or not isinstance(result.get("value"), dict):
            return {"ok": False, "error": result.get("error", "balance RPC failed"), "raw": None, "value": None}
        try:
            raw = int(result["value"].get("value"))
            return {"ok": True, "raw": raw, "value": raw / 1_000_000_000, "rpc": result.get("rpc")}
        except Exception as exc:
            return {"ok": False, "error": f"invalid SOL balance response: {exc}", "raw": None, "value": None}

    async def sol_balance(self, http) -> float | None:
        x = await self.sol_balance_raw_checked(http)
        return x.get("value") if x.get("ok") else None

    async def token_raw_balance_checked(self, http, mint: str):
        addr = self.address()
        if not addr:
            return {"ok": False, "error": "wallet address unavailable", "raw": None}
        result = await self._rpc_checked(http, "getTokenAccountsByOwner", [
            addr, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}
        ])
        if not result.get("ok") or not isinstance(result.get("value"), dict):
            return {"ok": False, "error": result.get("error", "token balance RPC failed"), "raw": None}
        total = 0
        try:
            for item in result["value"].get("value", []):
                amount = item["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
                total += int(amount)
            return {"ok": True, "raw": total, "rpc": result.get("rpc")}
        except Exception as exc:
            return {"ok": False, "error": f"invalid token balance response: {exc}", "raw": None}

    async def token_raw_balance(self, http, mint: str) -> int | None:
        x = await self.token_raw_balance_checked(http, mint)
        return x.get("raw") if x.get("ok") else None

    async def _mint_balance_checked(self, http, mint):
        if mint == WSOL_MINT:
            return await self.sol_balance_raw_checked(http)
        return await self.token_raw_balance_checked(http, mint)

    async def sol_usd(self, http) -> float | None:
        timeout = aiohttp.ClientTimeout(total=self.http_timeout)
        try:
            async with http.session.get(f"{DEX}/tokens/v1/solana/{WSOL_MINT}", timeout=timeout) as resp:
                data = await resp.json(content_type=None)
            if isinstance(data, list):
                rows = [x for x in data if str(x.get("chainId", "")).lower() == "solana"
                        and str((x.get("baseToken") or {}).get("address", "")) == WSOL_MINT]
                rows.sort(key=lambda x: float(((x.get("liquidity") or {}).get("usd") or 0)), reverse=True)
                for row in rows:
                    price = float(row.get("priceUsd") or 0)
                    if price > 0:
                        return price
        except Exception:
            pass
        try:
            async with http.session.get("https://api.coingecko.com/api/v3/simple/price",
                                        params={"ids": "solana", "vs_currencies": "usd"}, timeout=timeout) as resp:
                data = await resp.json(content_type=None)
            price = float(((data or {}).get("solana") or {}).get("usd") or 0)
            return price if price > 0 else None
        except Exception:
            return None

    async def quote_test(self, http):
        params = {"inputMint": WSOL_MINT, "outputMint": USDC_MINT, "amount": "1000000"}
        timeout = aiohttp.ClientTimeout(total=self.http_timeout)
        try:
            async with http.session.get(f"{JUPITER_BASE}/order", params=params,
                                        headers={"x-api-key": self.api_key}, timeout=timeout) as resp:
                data = await resp.json(content_type=None)
                return resp.status, data
        except Exception as exc:
            return 0, {"error": str(exc)}

    async def confirm_signature(self, http, signature: str, timeout_seconds=None):
        deadline = time.monotonic() + float(timeout_seconds or self.confirm_timeout)
        last = None
        while time.monotonic() < deadline:
            result = await self._rpc_checked(http, "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
            if result.get("ok") and isinstance(result.get("value"), dict):
                vals = result["value"].get("value") or []
                row = vals[0] if vals else None
                if row:
                    last = row
                    if row.get("err") is not None:
                        return {"confirmed": False, "failed": True, "err": row.get("err"), "slot": row.get("slot"), "status": row}
                    status = str(row.get("confirmationStatus") or "")
                    if status in {"confirmed", "finalized"}:
                        return {"confirmed": True, "failed": False, "err": None, "slot": row.get("slot"), "status": row}
            await asyncio.sleep(0.8)
        return {"confirmed": False, "failed": False, "err": None, "slot": (last or {}).get("slot") if isinstance(last, dict) else None, "status": last, "timeout": True}

    async def _stable_post_balance(self, http, mint, pre_raw, attempts=5):
        last = None
        for i in range(max(1, attempts)):
            last = await self._mint_balance_checked(http, mint)
            if last.get("ok"):
                raw = int(last.get("raw") or 0)
                if pre_raw is None or raw != int(pre_raw):
                    return last
            if i + 1 < attempts:
                await asyncio.sleep(0.7 + 0.2 * i)
        return last or {"ok": False, "error": "balance unavailable", "raw": None}

    async def swap(self, http, input_mint: str, output_mint: str, amount_raw: int, *,
                   db=None, idempotency_key="", side="", token="", symbol="", tier="",
                   signal_id=None, position_id=None, amount_usd=0.0, context=None):
        issues = self.readiness()
        if issues:
            return {"ok": False, "error": "; ".join(issues), "error_kind": "NOT_READY", "retryable": False}
        try:
            amount_raw = int(amount_raw)
        except Exception:
            amount_raw = 0
        if amount_raw <= 0:
            return {"ok": False, "error": "order amount is zero", "error_kind": "INVALID_AMOUNT", "retryable": False}

        idem = str(idempotency_key or f"adhoc:{side}:{token}:{input_mint}:{output_mint}:{amount_raw}:{int(time.time()//30)}")
        if db is not None:
            existing = db.execution_intent(idem)
            if existing:
                state = str(existing.get("state") or "")
                if state in {"ACCOUNTED", "CONFIRMED"}:
                    return {"ok": False, "executed": True, "duplicate": True, "error_kind": "IDEMPOTENT_REPLAY",
                            "error": f"execution already completed for {idem}", "signature": existing.get("signature") or ""}
                if state in {"SIGNED", "SUBMITTING", "AMBIGUOUS", "CONFIRMED_UNRECONCILED"}:
                    return {"ok": False, "executed": state.startswith("CONFIRMED"), "ambiguous": True,
                            "error_kind": "PENDING_EXECUTION", "error": f"execution {idem} is still unresolved; refusing duplicate send",
                            "signature": existing.get("signature") or ""}
                # FAILED intents are deliberately not resent under the same key. A later
                # retry uses a new 30-second attempt bucket after the execution lease releases.
                return {"ok": False, "executed": False, "error_kind": "PRIOR_FAILED_ATTEMPT",
                        "retryable": True, "error": f"prior failed execution attempt exists for {idem}"}

        # Fail closed on balance-read errors and amount > available balance.
        pre_in = await self._mint_balance_checked(http, input_mint)
        pre_out = await self._mint_balance_checked(http, output_mint)
        if not pre_in.get("ok") or not pre_out.get("ok"):
            detail = f"input={pre_in.get('error','ok')} output={pre_out.get('error','ok')}"
            if db is not None:
                db.log_trade_failure("PRE_BALANCE", "BALANCE_UNAVAILABLE", detail, retryable=True, side=side, token=token, idempotency_key=idem)
            return {"ok": False, "error": f"wallet balance preflight failed: {detail}", "error_kind": "BALANCE_UNAVAILABLE", "retryable": True}
        available = int(pre_in.get("raw") or 0)
        reserve_raw = int(self.min_sol_reserve * 1_000_000_000) if input_mint == WSOL_MINT else 0
        if amount_raw + reserve_raw > available:
            return {"ok": False, "error": f"insufficient input balance: need {amount_raw + reserve_raw}, have {available}",
                    "error_kind": "INSUFFICIENT_BALANCE", "retryable": False}

        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(amount_raw), "taker": self.address()}
        headers = {"x-api-key": self.api_key}
        timeout = aiohttp.ClientTimeout(total=self.http_timeout)
        try:
            async with http.session.get(f"{JUPITER_BASE}/order", params=params, headers=headers, timeout=timeout) as resp:
                order = await resp.json(content_type=None)
                if resp.status != 200:
                    category = f"JUPITER_ORDER_HTTP_{resp.status}"
                    if db is not None:
                        db.log_trade_failure("ORDER", category, str(order), retryable=resp.status in {408,425,429,500,502,503,504}, side=side, token=token, idempotency_key=idem)
                    return {"ok": False, "error": f"Jupiter order HTTP {resp.status}: {order}", "error_kind": category,
                            "retryable": resp.status in {408,425,429,500,502,503,504}}
        except Exception as exc:
            kind, retryable = classify_network_error(exc)
            if db is not None:
                db.log_trade_failure("ORDER", kind, str(exc), retryable=retryable, side=side, token=token, idempotency_key=idem)
            return {"ok": False, "error": f"Jupiter order network error: {exc}", "error_kind": kind, "retryable": retryable}

        if not isinstance(order, dict) or not order.get("transaction"):
            msg = order.get("errorMessage") if isinstance(order, dict) else order
            return {"ok": False, "error": f"Jupiter could not build order: {msg}", "error_kind": "INVALID_ORDER", "retryable": False}
        try:
            impact_raw = float(order.get("priceImpactPct") or 0)
            impact = abs(impact_raw) * (100 if abs(impact_raw) <= 1 else 1)
        except Exception:
            impact = 0.0
        if impact and impact > self.max_price_impact:
            return {"ok": False, "error": f"price impact {impact:.2f}% exceeds {self.max_price_impact:.2f}%", "error_kind": "PRICE_IMPACT", "retryable": False}

        ok_key, key_msg = self.keypair_integrity()
        if not ok_key:
            return {"ok": False, "error": key_msg, "error_kind": "KEYPAIR_CHANGED", "retryable": False}
        try:
            signed = sign_solana_transaction(order["transaction"], self.keypair_path)
            signature = transaction_signature(signed, self.keypair_path)
        except Exception as exc:
            return {"ok": False, "error": f"local transaction signing failed: {exc}", "error_kind": "SIGNING", "retryable": False}

        expected_output = int(order.get("outAmount") or order.get("outputAmount") or 0)
        if db is not None:
            intent, created = db.create_execution_intent(
                idempotency_key=idem, side=side, token=token, symbol=symbol, tier=tier,
                input_mint=input_mint, output_mint=output_mint, amount_raw=amount_raw, amount_usd=amount_usd,
                signal_id=signal_id, position_id=position_id, wallet_address=self.address(),
                request_id=order.get("requestId") or "", signature=signature, expected_output_raw=expected_output,
                pre_input_raw=pre_in.get("raw"), pre_output_raw=pre_out.get("raw"), context=context or {})
            if not created:
                return {"ok": False, "error": "execution intent already exists; refusing duplicate send", "error_kind": "IDEMPOTENT_REPLAY",
                        "duplicate": True, "signature": intent.get("signature") or signature}
            db.update_execution_intent(idem, state="SUBMITTING")

        body = {"signedTransaction": signed, "requestId": order.get("requestId")}
        execute_result = None
        execute_http = 0
        execute_error = ""
        try:
            async with http.session.post(f"{JUPITER_BASE}/execute", json=body,
                                         headers={"Content-Type": "application/json", "x-api-key": self.api_key},
                                         timeout=timeout) as resp:
                execute_http = resp.status
                execute_result = await resp.json(content_type=None)
                if resp.status != 200:
                    execute_error = f"Jupiter execute HTTP {resp.status}: {execute_result}"
        except Exception as exc:
            kind, retryable = classify_network_error(exc)
            execute_error = f"Jupiter execute network error: {exc}"
            if db is not None:
                db.log_trade_failure("SUBMIT", kind, execute_error, retryable=retryable, side=side, token=token,
                                     signature=signature, idempotency_key=idem)

        # Jupiter's HTTP result is advisory. The signed Solana transaction signature is
        # authoritative, including the case where the HTTP response is lost or contradictory.
        chain = await self.confirm_signature(http, signature)
        if chain.get("failed"):
            detail = f"on-chain failure: {chain.get('err')} | jupiter={execute_result or execute_error}"
            if db is not None:
                db.update_execution_intent(idem, state="FAILED", error_kind="CHAIN_FAILED", detail=detail, confirmed_slot=chain.get("slot"))
                db.log_trade_failure("CONFIRM", "CHAIN_FAILED", detail, retryable=False, side=side, token=token,
                                     signature=signature, idempotency_key=idem)
            return {"ok": False, "executed": False, "error": detail, "error_kind": "CHAIN_FAILED", "retryable": False,
                    "signature": signature, "order": order, "execute": execute_result}
        if not chain.get("confirmed"):
            detail = execute_error or f"transaction {signature} not confirmed before timeout"
            if db is not None:
                db.update_execution_intent(idem, state="AMBIGUOUS", error_kind="CONFIRM_TIMEOUT", detail=detail, confirmed_slot=chain.get("slot"))
                db.log_trade_failure("CONFIRM", "CONFIRM_TIMEOUT", detail, retryable=True, side=side, token=token,
                                     signature=signature, idempotency_key=idem)
            return {"ok": False, "executed": False, "ambiguous": True, "error": detail, "error_kind": "CONFIRM_TIMEOUT",
                    "retryable": True, "signature": signature, "order": order, "execute": execute_result}

        post_out = await self._stable_post_balance(http, output_mint, pre_out.get("raw"))
        post_in = await self._mint_balance_checked(http, input_mint)
        actual_output = None
        if post_out.get("ok"):
            actual_output = max(0, int(post_out.get("raw") or 0) - int(pre_out.get("raw") or 0))
        if actual_output is None or actual_output <= 0:
            detail = "Solana confirmed execution but output-wallet delta is not yet verifiable"
            if db is not None:
                db.update_execution_intent(idem, state="CONFIRMED_UNRECONCILED", error_kind="BALANCE_UNRECONCILED", detail=detail,
                                           post_input_raw=(post_in.get("raw") if post_in.get("ok") else None),
                                           post_output_raw=(post_out.get("raw") if post_out.get("ok") else None), confirmed_slot=chain.get("slot"))
                db.log_trade_failure("POST_BALANCE", "BALANCE_UNRECONCILED", detail, retryable=True, side=side, token=token,
                                     signature=signature, idempotency_key=idem)
            return {"ok": False, "executed": True, "reconciled": False, "error": detail, "error_kind": "BALANCE_UNRECONCILED",
                    "retryable": True, "signature": signature, "order": order, "execute": execute_result}

        slippage_pct = None
        if expected_output > 0:
            slippage_pct = max(-100.0, (1.0 - actual_output / expected_output) * 100.0)
        warning = ""
        if slippage_pct is not None and slippage_pct > self.max_fill_slippage:
            warning = f"actual wallet output was {slippage_pct:.2f}% below quoted output"
            if db is not None:
                db.log_trade_failure("POST_TRADE", "EXCESS_FILL_SLIPPAGE", warning, retryable=False, side=side, token=token,
                                     signature=signature, idempotency_key=idem)

        if db is not None:
            db.update_execution_intent(idem, state="CONFIRMED", actual_output_raw=str(actual_output),
                                       post_input_raw=(post_in.get("raw") if post_in.get("ok") else None),
                                       post_output_raw=post_out.get("raw"), slippage_pct=slippage_pct,
                                       confirmed_slot=chain.get("slot"), error_kind=("EXCESS_FILL_SLIPPAGE" if warning else ""), detail=warning)
        return {
            "ok": True, "executed": True, "reconciled": True, "warning": warning,
            "signature": signature, "input_raw": amount_raw, "output_raw": actual_output,
            "expected_output_raw": expected_output, "slippage_pct": slippage_pct,
            "order": order, "execute": execute_result, "chain": chain, "idempotency_key": idem,
            "jupiter_http": execute_http,
        }

    async def reconcile_intent(self, http, intent, db=None):
        """Re-check one unresolved execution without submitting anything new."""
        ok_key, key_msg = self.keypair_integrity()
        if not ok_key:
            return {"resolved": False, "state": str(intent.get("state") or "AMBIGUOUS"), "error": key_msg}
        stored_wallet=str(intent.get("wallet_address") or "")
        if stored_wallet and stored_wallet != self.address():
            return {"resolved": False, "state": str(intent.get("state") or "AMBIGUOUS"),
                    "error": "configured wallet no longer matches execution intent wallet"}
        signature = str(intent.get("signature") or "")
        idem = str(intent.get("idempotency_key") or "")
        if not signature:
            if db is not None:
                db.update_execution_intent(idem, state="FAILED", error_kind="NO_SIGNATURE", detail="unresolved intent has no signature")
            return {"resolved": True, "state": "FAILED", "error": "missing signature"}
        chain = await self.confirm_signature(http, signature, timeout_seconds=min(8.0, self.confirm_timeout))
        if chain.get("failed"):
            if db is not None:
                db.update_execution_intent(idem, state="FAILED", error_kind="CHAIN_FAILED", detail=str(chain.get("err")), confirmed_slot=chain.get("slot"))
            return {"resolved": True, "state": "FAILED", "chain": chain}
        if not chain.get("confirmed"):
            return {"resolved": False, "state": "AMBIGUOUS", "chain": chain}
        pre_out = intent.get("pre_output_raw")
        try:
            pre_out = int(pre_out) if pre_out is not None else None
        except Exception:
            pre_out = None
        post = await self._mint_balance_checked(http, str(intent.get("output_mint") or ""))
        actual = None
        if post.get("ok") and pre_out is not None:
            actual = max(0, int(post.get("raw") or 0) - pre_out)
        if not actual:
            if db is not None:
                db.update_execution_intent(idem, state="CONFIRMED_UNRECONCILED", error_kind="BALANCE_UNRECONCILED",
                                           post_output_raw=(post.get("raw") if post.get("ok") else None), confirmed_slot=chain.get("slot"))
            return {"resolved": False, "state": "CONFIRMED_UNRECONCILED", "chain": chain}
        expected = int(intent.get("expected_output_raw") or 0)
        slip = (1.0 - actual / expected) * 100.0 if expected > 0 else None
        if db is not None:
            db.update_execution_intent(idem, state="CONFIRMED", actual_output_raw=str(actual), post_output_raw=post.get("raw"),
                                       slippage_pct=slip, confirmed_slot=chain.get("slot"), error_kind="", detail="startup/runtime reconciliation")
        return {"resolved": True, "state": "CONFIRMED", "actual_output_raw": actual, "slippage_pct": slip, "chain": chain}
