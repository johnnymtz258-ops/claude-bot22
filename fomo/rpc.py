"""Solana RPC over HTTP (with failover and pacing) and a WebSocket wallet watcher."""
from __future__ import annotations

import asyncio
import re
import json
import time

import aiohttp

# JSON-RPC errors that mean "this node can't answer right now" -> try the next node / later.
RETRYABLE_RPC_CODES = {-32004, -32005, -32007, -32009, -32014, -32016, 429}


class SolanaRPC:
    def __init__(self, session: aiohttp.ClientSession, urls: list[str], *, concurrency: int = 6,
                 min_interval: float = 0.0):
        self.session = session
        self.urls = list(urls)
        self.sem = asyncio.Semaphore(max(1, concurrency))
        self.min_interval = max(0.0, min_interval)
        self.base_interval = self.min_interval
        self._next = 0.0
        self._pace_lock = asyncio.Lock()
        self._cool_until = {u: 0.0 for u in self.urls}
        self.calls = 0
        self.errors = 0
        self.rate_limited = 0
        self.last_error = ""
        self.last_rpc_error: dict = {}
        self._last_429 = 0.0
        self._priority = 0   # live whale-trade fetches waiting: background calls hold back until they're done
        self.tx_version = 1   # newest transaction format the bot asks for; adjusted if a node names another
        self.last_ok_ts = 0.0

    def _ordered(self) -> list[str]:
        now = time.monotonic()
        healthy = [u for u in self.urls if self._cool_until[u] <= now]
        return healthy + [u for u in self.urls if u not in healthy]

    async def _pace(self) -> None:
        if not self.min_interval:
            return
        async with self._pace_lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.min_interval
        if start > now:
            await asyncio.sleep(start - now)

    def strained(self) -> bool:
        """Rate limited in the last 10 seconds: background work (the whale scanner) should wait."""
        return time.monotonic() - self._last_429 < 10

    def _slow_down(self) -> None:
        """Rate limited: pause everyone briefly and space calls out more, instead of hammering the plan's limit."""
        self._last_429 = time.monotonic()
        self.min_interval = min(0.5, max(self.min_interval, 0.05) * 1.5)
        self._next = max(self._next, time.monotonic() + 1.0)

    def _note_error(self, url: str, text: str, cool: float) -> None:
        self.errors += 1
        self.last_error = f"{time.strftime('%H:%M:%S')} {text}"
        self._cool_until[url] = time.monotonic() + cool

    async def call(self, method: str, params: list, *, timeout: float = 12, attempts: int = 2,
                   priority: bool = False):
        """Return the JSON-RPC `result`, or None when every node failed."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        before = self.last_error
        for attempt in range(attempts):
            for url in self._ordered():
                waited = 0
                while not priority and self._priority > 0 and waited < 100:   # live trades go first
                    await asyncio.sleep(0.1)
                    waited += 1
                await self._pace()
                async with self.sem:
                    self.calls += 1
                    try:
                        async with self.session.post(url, json=payload,
                                                     timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                            if resp.status == 429:
                                self.rate_limited += 1
                                self._slow_down()
                                self._note_error(url, f"{method}: rate limited", 2.0)
                                continue
                            if resp.status >= 500:
                                self._note_error(url, f"{method}: HTTP {resp.status}", 5.0)
                                continue
                            data = await resp.json(content_type=None)
                    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                        self._note_error(url, f"{method}: {type(exc).__name__}", 5.0)
                        continue
                if not isinstance(data, dict):
                    self._note_error(url, f"{method}: bad response", 2.0)
                    continue
                err = data.get("error")
                if err:
                    code = err.get("code") if isinstance(err, dict) else None
                    if code in RETRYABLE_RPC_CODES:
                        self._note_error(url, f"{method}: {err.get('message', code)}", 1.0)
                        continue
                    self.last_error = f"{time.strftime('%H:%M:%S')} {method}: {err}"
                    self.last_rpc_error = err if isinstance(err, dict) else {"message": str(err)}
                    return None  # a definite answer (bad params etc.) — other nodes would agree
                self.last_ok_ts = time.time()
                if self.last_error != before:
                    self.last_error = before   # a retry got through (rate limit, network blip): nothing was lost
                if self.min_interval > self.base_interval:   # recover speed gradually after a rate limit
                    self.min_interval = max(self.base_interval, self.min_interval * 0.98)
                return data.get("result")
            await asyncio.sleep(0.4 * (attempt + 1))
        return None

    # -- typed helpers ----------------------------------------------------------------------
    async def transaction(self, signature: str, *, wait: float = 6.0, priority: bool = False):
        if not priority:
            return await self._transaction(signature, wait, False)
        self._priority += 1
        try:
            return await self._transaction(signature, wait, True)
        finally:
            self._priority -= 1

    async def _transaction(self, signature: str, wait: float, priority: bool):
        """Fetch a confirmed transaction. A just-notified signature can take a moment to index."""
        deadline = time.monotonic() + wait
        delay = 0.35
        before = self.last_error
        while True:
            self.last_rpc_error = {}
            tx = await self.call("getTransaction", [signature, {
                "encoding": "jsonParsed", "commitment": "confirmed",
                "maxSupportedTransactionVersion": self.tx_version}], attempts=1, priority=priority)
            if tx and self.last_error != before:
                self.last_error = before   # a retry got it: nothing was lost
            if tx or time.monotonic() >= deadline:
                return tx
            if self._adjust_tx_version(self.last_rpc_error):
                continue   # newer transaction format: ask again at once with the version the node named
            await asyncio.sleep(delay)
            delay = min(delay * 1.6, 1.5)

    def _adjust_tx_version(self, err: dict) -> bool:
        """Solana keeps adding transaction versions; a node rejecting one names the version to ask for."""
        msg = str((err or {}).get("message", ""))
        if "maxSupportedTransactionVersion" not in msg:
            return False
        found = re.search(r'maxSupportedTransactionVersion"?\s*:\s*(\d+)', msg)
        wanted = int(found.group(1)) if found else None
        if wanted is not None and wanted != self.tx_version:
            self.tx_version = wanted
            return True
        if wanted is None and self.tx_version > 0:   # an older node that doesn't know the newer version
            self.tx_version = 0
            return True
        return False

    async def signatures(self, address: str, *, limit: int = 100, before: str | None = None,
                         priority: bool = False) -> list[dict]:
        opts = {"limit": max(1, min(1000, int(limit))), "commitment": "confirmed"}
        if before:
            opts["before"] = before
        result = await self.call("getSignaturesForAddress", [address, opts], timeout=20, priority=priority)
        return result if isinstance(result, list) else []

    async def mint_info(self, mint: str) -> dict | None:
        result = await self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        value = (result or {}).get("value") if isinstance(result, dict) else None
        if not isinstance(value, dict):
            return None
        data = value.get("data") or {}
        parsed = data.get("parsed") if isinstance(data, dict) else None
        if not isinstance(parsed, dict) or parsed.get("type") != "mint":
            return None
        info = parsed.get("info") or {}
        decimals = int(info.get("decimals") or 0)
        return {
            "mint_authority": info.get("mintAuthority") or "",
            "freeze_authority": info.get("freezeAuthority") or "",
            "decimals": decimals,
            "supply": int(info.get("supply") or 0) / 10 ** decimals,
            "program": str(value.get("owner") or ""),
        }

    async def token_balance(self, owner: str, mint: str) -> float | None:
        """Total balance of `mint` held by `owner` (None if the RPC failed)."""
        result = await self.call("getTokenAccountsByOwner", [owner, {"mint": mint},
                                                            {"encoding": "jsonParsed", "commitment": "confirmed"}])
        if not isinstance(result, dict):
            return None
        total = 0.0
        for item in result.get("value") or []:
            try:
                amount = item["account"]["data"]["parsed"]["info"]["tokenAmount"]
                total += int(amount["amount"]) / 10 ** int(amount["decimals"])
            except (KeyError, TypeError, ValueError):
                continue
        return total

    async def token_balance_raw(self, owner: str, mint: str) -> int | None:
        """Balance of `mint` held by `owner` in the token's smallest unit (what swaps are priced in)."""
        result = await self.call("getTokenAccountsByOwner", [owner, {"mint": mint},
                                                            {"encoding": "jsonParsed", "commitment": "confirmed"}])
        if not isinstance(result, dict):
            return None
        total = 0
        for item in result.get("value") or []:
            try:
                total += int(item["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            except (KeyError, TypeError, ValueError):
                continue
        return total

    async def sol_balance(self, owner: str) -> float | None:
        result = await self.call("getBalance", [owner, {"commitment": "confirmed"}])
        if isinstance(result, dict) and "value" in result:
            return int(result["value"]) / 1e9
        return None

    def health(self) -> dict:
        return {"calls": self.calls, "errors": self.errors, "rate_limited": self.rate_limited,
                "last_error": self.last_error, "last_ok_ts": int(self.last_ok_ts)}


class WalletStream:
    """Subscribes to every watched wallet with `logsSubscribe` and reports new signatures.

    `on_signature(wallet, signature)` must be quick (it should just enqueue work).
    """

    def __init__(self, url: str, on_signature):
        self.url = url
        self.on_signature = on_signature
        self.wanted: set[str] = set()
        self.sub_of: dict[str, int] = {}
        self.wallet_of: dict[int, str] = {}
        self.pending: dict[int, tuple[str, str]] = {}
        self.connected = False
        self.last_message_ts = 0.0
        self.reconnects = 0
        self.last_error = ""
        self._ws = None
        self._req = 0
        self._changed = asyncio.Event()

    def set_wallets(self, wallets) -> None:
        self.wanted = {w for w in wallets if w}
        self._changed.set()

    @property
    def subscribed(self) -> int:
        return len(self.sub_of)

    async def _send(self, method: str, params: list, wallet: str, kind: str) -> None:
        self._req += 1
        self.pending[self._req] = (wallet, kind)
        await self._ws.send(json.dumps({"jsonrpc": "2.0", "id": self._req, "method": method, "params": params}))

    async def _sync(self) -> None:
        self._changed.clear()
        in_flight = {w for w, kind in self.pending.values() if kind == "sub"}
        for wallet in sorted(self.wanted - set(self.sub_of) - in_flight):
            await self._send("logsSubscribe", [{"mentions": [wallet]}, {"commitment": "confirmed"}], wallet, "sub")
        for wallet in list(set(self.sub_of) - self.wanted):
            sub = self.sub_of.pop(wallet)
            self.wallet_of.pop(sub, None)
            await self._send("logsUnsubscribe", [sub], wallet, "unsub")

    async def _handle(self, raw: str) -> None:
        msg = json.loads(raw)
        if "id" in msg and msg["id"] in self.pending:
            wallet, kind = self.pending.pop(msg["id"])
            if kind == "sub":
                if isinstance(msg.get("result"), int):
                    self.sub_of[wallet] = msg["result"]
                    self.wallet_of[msg["result"]] = wallet
                else:
                    self.last_error = f"subscribe {wallet[:6]}: {msg.get('error')}"
            return
        if msg.get("method") != "logsNotification":
            return
        params = msg.get("params") or {}
        wallet = self.wallet_of.get(params.get("subscription"))
        value = ((params.get("result") or {}).get("value") or {})
        signature = str(value.get("signature") or "")
        if wallet and signature and value.get("err") is None:
            await self.on_signature(wallet, signature)

    async def run(self) -> None:
        import websockets  # imported here so the rest of the package works without it in tests

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20,
                                              open_timeout=15, close_timeout=5, max_size=2 ** 23) as ws:
                    self._ws = ws
                    self.connected = True
                    self.sub_of.clear(); self.wallet_of.clear(); self.pending.clear()
                    backoff = 1.0
                    await self._sync()
                    while True:
                        if self._changed.is_set():
                            await self._sync()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2)
                        except asyncio.TimeoutError:
                            continue
                        self.last_message_ts = time.time()
                        await self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network drops are expected; reconnect with backoff
                self.last_error = f"{time.strftime('%H:%M:%S')} {type(exc).__name__}: {str(exc)[:120]}"
            finally:
                self.connected = False
                self._ws = None
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
