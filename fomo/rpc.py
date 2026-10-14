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
# temporary node-side failures: another node (or a second try) usually answers. -32019 is "failed to query long-term
# storage" (a node without the archive for that range): it must not empty the backup poller's answer for a wallet
RETRYABLE_RPC_CODES = {-32004, -32005, -32007, -32009, -32014, -32016, -32019, 429}

# "the plan's monthly allowance is used up": rest that node 30 minutes. Only clear words — "upgrade your plan" alone
# is also how providers word a requests-per-second limit (Chainstack's free plan: 3 a second), which is NOT this.
OUT_OF_CREDITS = ("max usage", "quota", "monthly", "credits", "out of credit", "exhausted", "insufficient balance",
                  "payment required", "request units")
RATE_WORDS = ("rate limit", "too many requests", "rps", "requests per second", "per second")
# a node that won't serve one METHOD on its plan (Chainstack's free plan disables getTokenLargestAccounts and
# getTokenAccountsByOwner): that method goes to the other nodes for an hour, everything else stays on it
METHOD_REFUSED = ("method not found", "not available", "not allowed", "not supported on", "paid plan", "upgrade your plan",
                  "dedicated node", "is disabled", "not enabled", "unsupported method")
KEY_REFUSED = ("api key", "apikey", "api-key", "unauthorized", "invalid key", "authenticat", "token is invalid")

# requests a second each provider's FREE plan allows, as seconds between calls (just under the limit; a node that
# still answers 429 is slowed further by itself, without slowing the others)
PROVIDER_GAP = (("chainstack", 0.36), ("helius", 0.11), ("alchemy", 0.06), ("drpc", 0.1), ("quiknode", 0.07),
                ("ankr", 0.1), ("syndica", 0.1), ("mainnet-beta.solana.com", 0.15))


def _public(url: str) -> bool:
    return "api.mainnet-beta.solana.com" in url


def _gap_for(url: str) -> float:
    return next((gap for key, gap in PROVIDER_GAP if key in url), 0.0)


class SolanaRPC:
    def __init__(self, session: aiohttp.ClientSession, urls: list[str], *, concurrency: int = 6,
                 min_interval: float = 0.0):
        self.session = session
        self.urls = list(urls)
        self.sem = asyncio.Semaphore(max(1, concurrency))
        self.min_interval = max(0.0, min_interval)     # all nodes together
        self.base_interval = self.min_interval
        self._next = 0.0
        self._pace_lock = asyncio.Lock()
        self._cool_until = {u: 0.0 for u in self.urls}
        self.base_gap = {u: _gap_for(u) for u in self.urls}   # each node on its own
        self.gap = dict(self.base_gap)
        self._node_next = {u: 0.0 for u in self.urls}
        self._blocked: dict[tuple[str, str], float] = {}     # (node, method) -> refused until
        self.node = {u: {"calls": 0, "errors": 0, "ms": 0.0, "ok": 0, "last": ""} for u in self.urls}   # telemetry
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

    def _ordered(self, background: bool = False, method: str = "") -> list[str]:
        now = time.monotonic()
        usable = [u for u in self.urls if self._blocked.get((u, method), 0) <= now]
        healthy = [u for u in usable if self._cool_until[u] <= now]
        private = [u for u in healthy if not _public(u)]
        if background and len(private) >= 2:
            # two private RPCs (e.g. two free plans): background work (scanner, coin checks) uses the second, so the
            # first one's allowance is kept for your live whale trades
            healthy = [private[1]] + [u for u in healthy if u != private[1]]
        # a node already busy at its own speed limit goes behind one that is free now (keeps preference otherwise)
        healthy.sort(key=lambda u: self._node_next[u] - now > 0.25)
        return healthy + [u for u in usable if u not in healthy] + [u for u in self.urls if u not in usable]

    async def _pace(self, url: str = "") -> None:
        async with self._pace_lock:
            now = time.monotonic()
            start = max(now, self._next if self.min_interval else now, self._node_next.get(url, 0.0))
            if self.min_interval:
                self._next = start + self.min_interval
            if url in self._node_next:
                self._node_next[url] = start + self.gap.get(url, 0.0)
        if start > now:
            await asyncio.sleep(start - now)

    def strained(self) -> bool:
        """Rate limited in the last 10 seconds: background work (the whale scanner) should wait."""
        return time.monotonic() - self._last_429 < 10

    def _slow_down(self, url: str) -> None:
        """This node said "too many requests": space ITS calls out more (the other nodes keep their speed)."""
        self._last_429 = time.monotonic()
        self.gap[url] = min(2.0, max(self.gap.get(url, 0.0), 0.05) * 1.5)
        self._node_next[url] = max(self._node_next.get(url, 0.0), time.monotonic() + 1.0)

    def _note_error(self, url: str, text: str, cool: float) -> None:
        # temporary (rate limit, timeout, network blip): retried, counted, never an "issue" on its own —
        # a whale trade that is still lost after every retry is reported by the engine instead
        self.errors += 1
        self.last_transient = f"{time.strftime('%H:%M:%S')} {text}"
        self._cool_until[url] = max(self._cool_until.get(url, 0.0), time.monotonic() + cool)
        if url in self.node:
            self.node[url]["errors"] += 1
            self.node[url]["last"] = f"{time.strftime('%H:%M')} {text}"[:140]

    def _refuse_method(self, url: str, method: str, why: str) -> None:
        self._blocked[(url, method)] = time.monotonic() + 3600
        self.errors += 1
        if url in self.node:
            self.node[url]["errors"] += 1
            self.node[url]["last"] = f"{time.strftime('%H:%M')} {method} not on this plan: {why}"[:140]
        recent = {m for (u, m), until in self._blocked.items() if u == url and until > time.monotonic()}
        if len(recent) >= 4:   # refuses almost everything: more likely a bad key than a few paid-only methods
            self._note_error(url, f"refused {len(recent)} different methods ({why})", 1800.0)

    def _rate_limited(self, url: str, method: str) -> None:
        self.rate_limited += 1
        self._slow_down(url)
        self._note_error(url, f"{method}: rate limited (this provider's requests-per-second limit)", 1.0)

    async def call(self, method: str, params: list, *, timeout: float = 12, attempts: int = 2,
                   priority: bool = False):
        """Return the JSON-RPC `result`, or None when every node failed."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        before = self.last_error
        sending = method in ("sendTransaction", "simulateTransaction")
        for attempt in range(attempts):
            for url in self._ordered(background=not priority, method=method):
                waited = 0
                while not priority and self._priority > 0 and waited < 100:   # live trades go first
                    await asyncio.sleep(0.1)
                    waited += 1
                await self._pace(url)
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
                                if any(w in body for w in OUT_OF_CREDITS) and not any(w in body for w in RATE_WORDS):
                                    # the plan's monthly allowance is used up: rest this node 30 minutes, the others
                                    # keep working at full speed
                                    self._note_error(url, f"{method}: monthly allowance used up ({body.strip()[:60]!r})",
                                                     1800.0)
                                else:
                                    self._rate_limited(url, method)
                                continue
                            if resp.status in (401, 402, 403):
                                body = (await resp.text())[:200].lower()
                                if resp.status == 403 and not any(w in body for w in KEY_REFUSED) \
                                        and not any(w in body for w in OUT_OF_CREDITS):
                                    self._refuse_method(url, method, f"HTTP 403 {body.strip()[:60]!r}")
                                else:
                                    self._note_error(url, f"{method}: HTTP {resp.status} — wrong key or plan used up "
                                                          f"({body.strip()[:60]!r})", 1800.0)
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
                    text = str(err.get("message") if isinstance(err, dict) else err).lower()
                    if not sending:
                        if any(w in text for w in OUT_OF_CREDITS) and not any(w in text for w in RATE_WORDS):
                            self._note_error(url, f"{method}: {text[:80]}", 1800.0)   # allowance used up, said in JSON
                            continue
                        if code == 429 or code == -32005 or any(w in text for w in RATE_WORDS):
                            self._rate_limited(url, method)
                            continue
                        if (code == -32601 or any(w in text for w in METHOD_REFUSED)) and "version" not in text:
                            self._refuse_method(url, method, text[:60])
                            continue
                    if code in RETRYABLE_RPC_CODES:
                        self._note_error(url, f"{method}: {err.get('message', code)}", 1.0)
                        continue
                    self.last_error = f"{time.strftime('%H:%M:%S')} {method}: {err}"
                    self.last_rpc_error = err if isinstance(err, dict) else {"message": str(err)}
                    return None  # a definite answer (bad params etc.) — other nodes would agree
                self.last_ok_ts = time.time()
                if url in self.node:
                    self.node[url]["ok"] += 1
                    self.node[url]["ms"] += (time.monotonic() - started) * 1000
                if self.gap.get(url, 0.0) > self.base_gap.get(url, 0.0):   # recover this node's speed gradually
                    self.gap[url] = max(self.base_gap[url], self.gap[url] * 0.97)
                if self.last_error != before:
                    self.last_error = before   # a retry got through (rate limit, network blip): nothing was lost
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
                "nodes": [{"name": provider_name(u), "calls": n["calls"], "errors": n["errors"], "ok": n["ok"],
                           "avg_ms": round(n["ms"] / n["ok"]) if n["ok"] else None,
                           "resting_s": max(0, int(self._cool_until.get(u, 0) - now)),
                           "per_second": round(1 / self.gap[u], 1) if self.gap.get(u) else None,
                           "not_on_plan": sorted(m for (v, m), until in self._blocked.items() if v == u and until > now),
                           "last": n["last"]}
                          for u, n in self.node.items()]}

    def primary_down(self) -> bool:
        """Every private RPC is cooling off after failures (monthly limit used up, outage): calls go to the slow
        public one."""
        now = time.monotonic()
        private = [u for u in self.urls if not _public(u)]
        if not private or len(private) == len(self.urls):
            return len(self.urls) > 1 and self._cool_until.get(self.urls[0], 0) > now
        return all(self._cool_until.get(u, 0) > now for u in private)


SUB_ANSWER_WAIT = 20      # seconds a provider has to answer a subscribe request before it's sent again


def _stream_name(url: str) -> str:
    from .config import provider_name
    return provider_name(url.replace("wss://", "https://").replace("ws://", "http://").replace("/ogws", "/ogrpc"))


class WalletStream:
    """Subscribes to every watched wallet with `logsSubscribe` and reports new signatures.

    `on_signature(wallet, signature, logs=[...])` must be quick (it should just enqueue work). The logs come with
    every notification: pump.fun buys are read straight from them (no RPC call).
    """

    def __init__(self, url: str, on_signature, fallback: str = "", name: str = ""):
        self.url = url
        self.primary = url
        self.name = name or _stream_name(url)
        self.first = 0              # trades this stream delivered before any other (with several streams)
        self.fallback = fallback
        self._failures = 0
        self._fallback_until = 0.0
        self.on_signature = on_signature
        self.wanted: set[str] = set()
        self._stale: list[int] = []
        self.sub_of: dict[str, int] = {}
        self.wallet_of: dict[int, str] = {}
        self.pending: dict[int, tuple[str, str]] = {}
        self._sent_at: dict[int, float] = {}
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
        self._sent_at[self._req] = time.time()
        await self._ws.send(json.dumps({"jsonrpc": "2.0", "id": self._req, "method": method, "params": params}))

    def _expire_pending(self) -> None:
        """A request the provider never answered (dropped under its rate limit) must not block that wallet forever:
        after SUB_ANSWER_WAIT seconds it counts as failed and is sent again on the next check."""
        now = time.time()
        for req, sent in list(self._sent_at.items()):
            if now - sent >= SUB_ANSWER_WAIT:
                self._sent_at.pop(req, None)
                if self.pending.pop(req, (None, ""))[1] == "sub":
                    self.failed_subs += 1
                    self.last_error = f"{time.strftime('%H:%M:%S')} a subscription got no answer — retrying"

    def _in_flight(self) -> set[str]:
        return {w for w, kind in self.pending.values() if kind == "sub"}

    async def _sync(self) -> None:
        self._changed.clear()
        self._last_sync = time.time()
        self._expire_pending()
        gap = max(0.05, _gap_for(self.url) * 1.2)   # under the plan's requests-a-second (Chainstack free: 3 a second)
        while self._stale:
            await self._send("logsUnsubscribe", [self._stale.pop()], "", "unsub")
            await asyncio.sleep(gap)
        for i, wallet in enumerate(sorted(self.wanted - set(self.sub_of) - self._in_flight())):
            if i:
                await asyncio.sleep(gap)
            await self._send("logsSubscribe", [{"mentions": [wallet]}, {"commitment": "confirmed"}], wallet, "sub")
        for wallet in list(set(self.sub_of) - self.wanted):
            sub = self.sub_of.pop(wallet)
            self.wallet_of.pop(sub, None)
            await self._send("logsUnsubscribe", [sub], wallet, "unsub")

    async def _handle(self, raw: str) -> None:
        msg = json.loads(raw)
        if "id" in msg and msg["id"] in self.pending:
            self._sent_at.pop(msg["id"], None)
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
                    self.sub_of.clear(); self.wallet_of.clear(); self.pending.clear(); self._sent_at.clear()
                    backoff = 1.0
                    await self._sync()
                    self.connected_since = time.time()
                    while True:
                        self._expire_pending()
                        missing = self.wanted - set(self.sub_of) - self._in_flight()
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


class MultiStream:
    """The same wallets subscribed on several providers at once (e.g. Chainstack + Alchemy): a subscription one
    provider drops, or a reconnect, is covered by the other. Every signature still goes through the engine once (it
    skips one it already has). Looks like one WalletStream to the rest of the bot."""

    def __init__(self, streams: list[WalletStream], on_signature):
        self.streams = streams
        self.on_signature = on_signature
        self._seen: dict[str, str] = {}
        self._unique = 0
        self._resubscribed = 0
        for s in streams:
            s.on_signature = self._tap(s)

    def _tap(self, stream: WalletStream):
        async def on_signature(wallet: str, signature: str, logs=None):
            if signature not in self._seen:
                self._seen[signature] = stream.name
                self._unique += 1
                stream.first += 1
                if len(self._seen) > 5000:   # bounded: keep the most recent half
                    self._seen = dict(list(self._seen.items())[-2500:])
            await self.on_signature(wallet, signature, logs=logs)
        return on_signature

    # -- what the rest of the bot reads ------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return any(s.connected for s in self.streams)

    @property
    def subscribed(self) -> int:
        """Wallets covered by at least one live subscription."""
        return len(set().union(*(set(s.sub_of) for s in self.streams if s.connected)))

    @property
    def wanted(self) -> set[str]:
        return self.streams[0].wanted if self.streams else set()

    @property
    def notifications(self) -> int:
        return self._unique if len(self.streams) > 1 else sum(s.notifications for s in self.streams)

    @property
    def reconnects(self) -> int:
        return sum(s.reconnects for s in self.streams)

    @property
    def failed_subs(self) -> int:
        return sum(s.failed_subs for s in self.streams)

    @property
    def resubscribed(self) -> int:
        return self._resubscribed

    @property
    def last_error(self) -> str:
        return next((f"{s.name}: {s.last_error}" for s in self.streams if s.last_error and not s.connected), "") or \
            next((f"{s.name}: {s.last_error}" for s in self.streams if s.last_error), "")

    @property
    def last_message_ts(self) -> float:
        return max((s.last_message_ts for s in self.streams), default=0.0)

    def detail(self) -> list[dict]:
        return [{"name": s.name, "connected": s.connected, "subscribed": s.subscribed, "wanted": len(s.wanted),
                 "notifications": s.notifications, "first": s.first, "reconnects": s.reconnects,
                 "failed_subs": s.failed_subs} for s in self.streams]

    def set_wallets(self, wallets) -> None:
        for s in self.streams:
            s.set_wallets(wallets)

    def resubscribe(self, wallet: str) -> None:
        self._resubscribed += 1
        for s in self.streams:
            s.resubscribe(wallet)

    async def run(self) -> None:
        await asyncio.gather(*(s.run() for s in self.streams))
