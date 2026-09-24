import asyncio
import json
import os
import time
from collections import defaultdict, deque

import aiohttp
import websockets

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUMP_TOKEN = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
EXCLUDED_MINTS = {WSOL, USDC, PUMP_TOKEN}


class SolanaRealtime:
    """Best-effort, read-only Solana event radar.

    It never signs or sends transactions.  It listens to public RPC WebSocket logs.
    If HELIUS_API_KEY is present, it uses Helius RPC; otherwise it falls back to the
    public Solana RPC endpoint.  Public RPC is less reliable and may rate-limit.
    """

    def __init__(self):
        self.enabled = os.getenv("REALTIME_SOLANA", "true").lower() in {"1","true","yes","on"}
        self.helius_key = os.getenv("HELIUS_API_KEY", "").strip()
        if self.helius_key:
            self.wss_url = f"wss://mainnet.helius-rpc.com/?api-key={self.helius_key}"
            self.http_url = f"https://mainnet.helius-rpc.com/?api-key={self.helius_key}"
            self.source = "Helius WebSocket"
        else:
            self.wss_url = os.getenv("SOLANA_RPC_WSS", "wss://api.mainnet-beta.solana.com").strip()
            self.http_url = os.getenv("SOLANA_RPC_HTTP", "https://api.mainnet-beta.solana.com").strip()
            self.source = "public Solana WebSocket"

        self.max_watches = int(float(os.getenv("REALTIME_MAX_MINT_WATCHES", "12")))
        self.watch_ttl = int(float(os.getenv("REALTIME_MINT_WATCH_MINUTES", "15"))) * 60

        self.ws = None
        self.connected = False
        self.last_message = 0.0
        self.request_id = 100
        self.pending = {}
        self.program_subs = set()
        self.sub_to_mint = {}
        self.mint_to_sub = {}
        self.watch_touched = {}
        self.wanted_mints = {}
        self.events = defaultdict(deque)     # mint -> (ts, signature)
        self.new_mints = {}                  # mint -> first seen ts
        self.seen_signatures = {}
        self.tx_queue = asyncio.Queue(maxsize=250)
        self.stop_event = asyncio.Event()
        self._http = None

    def status(self):
        if not self.enabled:
            return "OFF"
        return f"{'CONNECTED' if self.connected else 'RECONNECTING'} via {self.source}"

    def _next_id(self):
        self.request_id += 1
        return self.request_id

    async def _subscribe(self, address, kind, mint=None):
        if not self.ws:
            return
        rid = self._next_id()
        self.pending[rid] = (kind, mint or address)
        payload = {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [address]},
                {"commitment": "processed"}
            ]
        }
        await self.ws.send(json.dumps(payload))

    async def _unsubscribe(self, sub_id):
        if not self.ws or not sub_id:
            return
        rid = self._next_id()
        self.pending[rid] = ("unsubscribe", str(sub_id))
        try:
            await self.ws.send(json.dumps({
                "jsonrpc":"2.0", "id":rid, "method":"logsUnsubscribe", "params":[sub_id]
            }))
        except Exception:
            pass

    async def watch_mint(self, mint):
        if not self.enabled or not mint or mint in EXCLUDED_MINTS:
            return False
        now = time.time()
        self.wanted_mints[mint] = now
        self.watch_touched[mint] = now

        # Expire old watches first.
        for old, touched in list(self.watch_touched.items()):
            if now - touched > self.watch_ttl:
                sid = self.mint_to_sub.pop(old, None)
                self.watch_touched.pop(old, None)
                self.wanted_mints.pop(old, None)
                if sid:
                    self.sub_to_mint.pop(sid, None)
                    await self._unsubscribe(sid)

        # Keep the hottest/recent set small.
        if mint not in self.mint_to_sub and len(self.mint_to_sub) >= self.max_watches:
            oldest = min(self.watch_touched, key=self.watch_touched.get)
            sid = self.mint_to_sub.pop(oldest, None)
            self.watch_touched.pop(oldest, None)
            self.wanted_mints.pop(oldest, None)
            if sid:
                self.sub_to_mint.pop(sid, None)
                await self._unsubscribe(sid)

        if self.connected and mint not in self.mint_to_sub:
            await self._subscribe(mint, "mint", mint)
        return True

    def _cleanup(self):
        now = time.time()
        for mint, ts in list(self.new_mints.items()):
            if now - ts > 60*60:
                self.new_mints.pop(mint, None)
                self.events.pop(mint, None)
        for sig, ts in list(self.seen_signatures.items()):
            if now - ts > 30*60:
                self.seen_signatures.pop(sig, None)

    def candidate_mints(self, max_age_seconds=15*60):
        self._cleanup()
        now = time.time()
        return {
            mint for mint, ts in self.new_mints.items()
            if now - ts <= max_age_seconds
        }

    def stats(self, mint):
        self._cleanup()
        now = time.time()
        dq = self.events[mint]
        while dq and now - dq[0][0] > 120:
            dq.popleft()
        tx10 = sum(1 for ts, _ in dq if now-ts <= 10)
        tx30 = sum(1 for ts, _ in dq if now-ts <= 30)
        tx60 = sum(1 for ts, _ in dq if now-ts <= 60)
        prior20 = max(tx30 - tx10, 0)
        expected10 = prior20 / 2.0
        acceleration = tx10 / max(expected10, 1.0)
        return {
            "available": mint in self.mint_to_sub and self.connected,
            "tx10": tx10,
            "tx30": tx30,
            "tx60": tx60,
            "acceleration": min(acceleration, 10.0),
            "newly_detected": mint in self.new_mints and now-self.new_mints[mint] <= 15*60,
        }

    async def _rpc_transaction(self, signature):
        if not self._http:
            return None
        payload = {
            "jsonrpc":"2.0", "id":1, "method":"getTransaction",
            "params":[signature, {
                "encoding":"jsonParsed",
                "commitment":"confirmed",
                "maxSupportedTransactionVersion":0
            }]
        }
        # Processed logs can arrive before getTransaction is visible at
        # confirmed commitment, especially on public RPC.
        for attempt in range(6):
            try:
                async with self._http.post(self.http_url, json=payload, timeout=8) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        result = data.get("result") if isinstance(data, dict) else None
                        if result:
                            return result
            except Exception:
                pass
            await asyncio.sleep(min(0.4 * (attempt + 1), 1.8))
        return None

    def _extract_mints(self, tx):
        meta = tx.get("meta") or {}
        mints = set()
        for key in ("preTokenBalances", "postTokenBalances"):
            for bal in meta.get(key) or []:
                mint = str(bal.get("mint") or "")
                if mint and mint not in EXCLUDED_MINTS:
                    mints.add(mint)
        return mints

    async def _tx_worker(self):
        while not self.stop_event.is_set():
            try:
                sig = await asyncio.wait_for(self.tx_queue.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            try:
                tx = await self._rpc_transaction(sig)
                if tx:
                    for mint in self._extract_mints(tx):
                        self.new_mints.setdefault(mint, time.time())
            finally:
                self.tx_queue.task_done()
            await asyncio.sleep(0.10 if self.helius_key else 0.45)

    async def _handle_message(self, message):
        self.last_message = time.time()

        # Subscription acknowledgement.
        if "id" in message and "result" in message and message.get("id") in self.pending:
            kind, value = self.pending.pop(message["id"])
            result = message.get("result")
            if kind == "program":
                self.program_subs.add(result)
            elif kind == "mint":
                self.sub_to_mint[result] = value
                self.mint_to_sub[value] = result
            return

        params = message.get("params") or {}
        sub_id = params.get("subscription")
        value = nest(params, "result", "value", default={}) or {}
        signature = str(value.get("signature") or "")
        if not signature:
            return

        # A dynamically watched mint: count activity immediately without
        # waiting for another REST indexer.
        mint = self.sub_to_mint.get(sub_id)
        if mint:
            dq = self.events[mint]
            if not dq or dq[-1][1] != signature:
                dq.append((time.time(), signature))
            return

        # Pump/PumpSwap program stream: only fetch create/initialize-like txs.
        if sub_id in self.program_subs:
            logs = " ".join(str(x).lower() for x in (value.get("logs") or []))
            create_terms = (
                "instruction: create",
                "instruction: createv2",
                "instruction: create_pool",
                "instruction: createpool",
                "instruction: initialize",
            )
            if any(term in logs for term in create_terms):
                if signature not in self.seen_signatures:
                    self.seen_signatures[signature] = time.time()
                    try:
                        self.tx_queue.put_nowait(signature)
                    except asyncio.QueueFull:
                        pass

    async def _listen_once(self):
        async with websockets.connect(
            self.wss_url,
            ping_interval=30,
            ping_timeout=20,
            close_timeout=5,
            max_queue=1000
        ) as ws:
            self.ws = ws
            self.connected = True
            self.pending.clear()
            self.program_subs.clear()
            self.sub_to_mint.clear()
            self.mint_to_sub.clear()

            await self._subscribe(PUMP_PROGRAM, "program")
            await self._subscribe(PUMP_AMM_PROGRAM, "program")
            for mint in list(self.wanted_mints)[-self.max_watches:]:
                await self._subscribe(mint, "mint", mint)

            print(f"[realtime] connected via {self.source}; Pump/PumpSwap radar active")

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    if isinstance(msg, dict):
                        await self._handle_message(msg)
                except Exception:
                    continue

    async def run(self):
        if not self.enabled:
            print("[realtime] disabled")
            return
        self._http = aiohttp.ClientSession()
        worker = asyncio.create_task(self._tx_worker())
        backoff = 2
        try:
            while not self.stop_event.is_set():
                try:
                    await self._listen_once()
                    backoff = 2
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.connected = False
                    self.ws = None
                    print(f"[realtime] reconnecting after {type(exc).__name__}: {exc}")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff*2, 30)
        finally:
            self.connected = False
            self.stop_event.set()
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            if self._http:
                await self._http.close()

    async def close(self):
        self.stop_event.set()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass


def nest(data, *keys, default=None):
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur
