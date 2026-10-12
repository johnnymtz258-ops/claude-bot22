"""Solana RPC over HTTP (with failover and pacing) and a WebSocket wallet watcher."""
from __future__ import annotations

import asyncio
import re
import json
import time

import aiohttp

from .scam import token_traps
from .util import is_address

# JSON-RPC errors that mean "this node can't answer right now" -> try the next node / later.
RETRYABLE_RPC_CODES = {-32004, -32005, -32007, -32009, -32014, -32016, 429}


OUT_OF_CREDITS = ("max usage", "credit", "quota", "exceeded your", "upgrade your", "capacity limit", "monthly",
                  "limit exceeded", "payment required", "insufficient balance", "plan limit")
# the same, said inside a normal JSON-RPC answer (stricter: "credit" alone also appears in transaction errors)
QUOTA_WORDS = ("max usage", "quota", "exceeded your", "upgrade your", "capacity limit", "monthly", "limit exceeded",
               "payment required", "plan limit", "out of credits", "credits exhausted", "insufficient credits")
REFUSED = (401, 402, 403)   # key wrong, plan used up or not allowed: that node can't help — rest it, use the next


def _public(url: str) -> bool:
    return "api.mainnet-beta.solana.com" in url


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
        self.node = {u: {"calls": 0, "errors": 0, "ms": 0.0, "ok": 0} for u in self.urls}   # telemetry per RPC
        self.calls = 0
        self.errors = 0
        self.rate_limited = 0
        self.last_error = ""
        self.last_rpc_error: dict = {}
        self.last_transient = ""
        self._last_429 = 0.0
        self._priority = 0   # live whale-trade fetches waiting: background calls hold back until they're done
        self.tx_version = 1   # newest transaction format the bot asks for; adjusted if a node names another
        self.last_ok_ts = 0.0

    def _ordered(self, background: bool = False) -> list[str]:
        now = time.monotonic()
        healthy = [u for u in self.urls if self._cool_until[u] <= now]
        private = [u for u in healthy if not _public(u)]
        if background and len(private) >= 2:
            # two private RPCs (e.g. two free plans): background work (scanner, coin checks) uses the second, so the
            # first one's monthly allowance is kept for your live whale trades
            healthy = [private[1]] + [u for u in healthy if u != private[1]]
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
        # temporary (rate limit, timeout, network blip): retried, counted, never an "issue" on its own —
        # a whale trade that is still lost after every retry is reported by the engine instead
        self.errors += 1
        self.last_transient = f"{time.strftime('%H:%M:%S')} {text}"
        self._cool_until[url] = time.monotonic() + cool
        if url in self.node:
            self.node[url]["errors"] += 1

    async def call(self, method: str, params: list, *, timeout: float = 12, attempts: int = 2,
                   priority: bool = False):
        """Return the JSON-RPC `result`, or None when every node failed."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        before = self.last_error
        for attempt in range(attempts):
            for url in self._ordered(background=not priority):
                waited = 0
                while not priority and self._priority > 0 and waited < 100:   # live trades go first
                    await asyncio.sleep(0.1)
                    waited += 1
                await self._pace()
                async with self.sem:
                    self.calls += 1
                    started = time.monotonic()
                    if url in self.node:
                        self.node[url]["calls"] += 1
                    try:
                        async with self.session.post(url, json=payload,
                                                     timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                            if resp.status == 429:
                                body = (await resp.text())[:300].lower()
                                if any(w in body for w in OUT_OF_CREDITS):
                                    # the plan's monthly credits are used up: not a rate limit — rest this node for
                                    # 30 minutes and let the others work at full speed
                                    self._note_error(url, f"{method}: out of credits (max usage reached)", 1800.0)
                                    continue
                                self.rate_limited += 1
                                self._slow_down()
                                self._note_error(url, f"{method}: rate limited", 2.0)
                                continue
                            if resp.status in REFUSED:
                                body = (await resp.text())[:200]
                                self._note_error(url, f"{method}: HTTP {resp.status} — wrong key, or the free plan's "
                                                      f"limit is used up ({body.strip()[:80]!r})", 1800.0)
                                continue
                            if resp.status >= 500:
                                self._note_error(url, f"{method}: HTTP {resp.status}", 5.0)
                                continue
                            data = await resp.json(content_type=None)
                    except ValueError:
                        # not JSON: a credit-limit / outage page. Rest this node for a minute; the fallback answers
                        self._note_error(url, f"{method}: the node answered with a non-JSON page (credits used up or outage)",
                                         60.0)
                        continue
                    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
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
                    text = str(err.get("message") if isinstance(err, dict) else err).lower()
                    if method not in ("sendTransaction", "simulateTransaction") and (
                            any(w in text for w in QUOTA_WORDS) or "rate limit" in text or "too many requests" in text):
                        # a provider saying "limit reached" inside a normal answer: not about this call — next node
                        out = not ("rate limit" in text or "too many requests" in text)
                        self._note_error(url, f"{method}: {text[:80]}", 1800.0 if out else 2.0)
                        continue
                    self.last_error = f"{time.strftime('%H:%M:%S')} {method}: {err}"
                    self.last_rpc_error = err if isinstance(err, dict) else {"message": str(err)}
                    return None  # a definite answer (bad params etc.) — other nodes would agree
                self.last_ok_ts = time.time()
                if url in self.node:
                    self.node[url]["ok"] += 1
                    self.node[url]["ms"] += (time.monotonic() - started) * 1000
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
            "traps": token_traps(info),   # Token-2022 extensions that exist to trap buyers
        }

    async def token_balance(self, owner: str, mint: str) -> float | None:
        """Total balance of `mint` held by `owner` (None if the RPC failed)."""
        if not (is_address(owner) and is_address(mint)):
            return None   # never send the RPC something that isn't an address
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
        if not (is_address(owner) and is_address(mint)):
            return None   # never send the RPC something that isn't an address
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
        from .config import provider_name   # names only: an RPC link carries its key
        now = time.monotonic()
        return {"calls": self.calls, "errors": self.errors, "rate_limited": self.rate_limited,
                "last_error": self.last_error, "last_ok_ts": int(self.last_ok_ts),
                "primary_down": self.primary_down(),
                "nodes": [{"name": provider_name(u), "calls": n["calls"], "errors": n["errors"],
                           "avg_ms": round(n["ms"] / n["ok"]) if n["ok"] else None,
                           "resting_s": max(0, int(self._cool_until.get(u, 0) - now))}
                          for u, n in self.node.items()]}

    def primary_down(self) -> bool:
        """Every private RPC is cooling off after failures (monthly limit used up, outage): calls go to the slow
        public one."""
        now = time.monotonic()
        private = [u for u in self.urls if not _public(u)]
        if not private or len(private) == len(self.urls):
            return len(self.urls) > 1 and self._cool_until.get(self.urls[0], 0) > now
        return all(self._cool_until.get(u, 0) > now for u in private)


class WalletStream:
    """Subscribes to every watched wallet with `logsSubscribe` and reports new signatures.

    `on_signature(wallet, signature, logs=[...])` must be quick (it should just enqueue work). The logs come with
    every notification: pump.fun buys are read straight from them (no RPC call).
    """

    def __init__(self, url: str, on_signature, fallback: str = ""):
        self.url = url
        self.primary = url
        self.fallback = fallback
        self._failures = 0
        self._fallback_until = 0.0
        self.on_signature = on_signature
        self.wanted: set[str] = set()
        self._stale: list[int] = []
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
        self._last_sync = 0.0
        self.notifications = 0
        self.failed_subs = 0
        self.resubscribed = 0
        self.connected_since = 0.0

    def set_wallets(self, wallets) -> None:
        self.wanted = {w for w in wallets if w}
        self._changed.set()

    def resubscribe(self, wallet: str) -> None:
        """Drop and redo one wallet's subscription (a provider can silently stop delivering one)."""
        sub = self.sub_of.pop(wallet, None)
        if sub is not None:
            self.wallet_of.pop(sub, None)
            self._stale.append(sub)
        self.resubscribed += 1
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
        self._last_sync = time.time()
        while self._stale:
            await self._send("logsUnsubscribe", [self._stale.pop()], "", "unsub")
        in_flight = {w for w, kind in self.pending.values() if kind == "sub"}
        for i, wallet in enumerate(sorted(self.wanted - set(self.sub_of) - in_flight)):
            if i:
                await asyncio.sleep(0.05)   # paced: a burst of 50 subscriptions can trip a provider's limit
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
                else:   # retried by the 30-second check in run()
                    self.failed_subs += 1
                    self.last_error = f"subscribe {wallet[:6]}: {msg.get('error')}"
            return
        if msg.get("method") != "logsNotification":
            return
        params = msg.get("params") or {}
        wallet = self.wallet_of.get(params.get("subscription"))
        value = ((params.get("result") or {}).get("value") or {})
        signature = str(value.get("signature") or "")
        if wallet and signature and value.get("err") is None:
            self.notifications += 1
            await self.on_signature(wallet, signature, logs=value.get("logs"))

    async def run(self) -> None:
        import websockets  # imported here so the rest of the package works without it in tests

        backoff = 1.0
        while True:
            # Helius refusing connections (credits used up, outage): use the public stream for 10 minutes, then retry
            if self.fallback and self._failures >= 3 and self.url == self.primary:
                self.url, self._fallback_until, backoff = self.fallback, time.time() + 600, 1.0
            elif self.url != self.primary and time.time() >= self._fallback_until:
                self.url, self._failures = self.primary, 0
            try:
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20,
                                              open_timeout=15, close_timeout=5, max_size=2 ** 23) as ws:
                    self._ws = ws
                    self.connected = True
                    if self.url == self.primary:
                        self._failures = 0
                    self.sub_of.clear(); self.wallet_of.clear(); self.pending.clear()
                    backoff = 1.0
                    await self._sync()
                    self.connected_since = time.time()
                    while True:
                        missing = self.wanted - set(self.sub_of) - {w for w, k in self.pending.values() if k == "sub"}
                        if self._changed.is_set() or (missing and time.time() - self._last_sync >= 30):
                            await self._sync()   # new wallets, or subscriptions that failed: (re)subscribe
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
                if self.url == self.primary:
                    self._failures += 1
            finally:
                self.connected = False
                self._ws = None
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
