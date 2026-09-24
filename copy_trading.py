"""FomoBot v16 copy-trading / smart-wallet engine.

Design goals:
- read public Solana wallet activity in real time;
- parse *final wallet balance deltas* instead of hard-coding one DEX instruction layout;
- ignore failed transactions;
- handle Jupiter/Raydium/Meteora/Pump routes, including multi-hop routes, by looking at
  the tracked wallet's net SOL/token deltas after confirmation;
- paper-copy every qualified event by default;
- optional live copying reuses the hardened v15 live_execution.LiveExecutor;
- fail closed on stale events, duplicate signatures, unresolved execution state, balance
  errors, excessive chase/impact, kill switch, daily loss, or max-position limits.

The module never stores seed phrases or private keys. Live execution can only use the local
keypair mechanism already implemented in live_execution.py and is OFF by default.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import statistics
import time
from collections import defaultdict, deque
from pathlib import Path

import aiohttp
import websockets

from live_execution import WSOL_MINT

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DEXSCREENER = "https://api.dexscreener.com"
JUPITER_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
RAYDIUM_CPMM_PROGRAM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
BASE_MINTS = {WSOL_MINT, USDC_MINT}


def _f(v, default=0.0):
    try:
        return float(v if v is not None else default)
    except Exception:
        return float(default)


def _i(v, default=0):
    try:
        return int(v if v is not None else default)
    except Exception:
        return int(default)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _valid_pubkey(value: str) -> bool:
    s = str(value or "").strip()
    if not (32 <= len(s) <= 44):
        return False
    alphabet = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return all(ch in alphabet for ch in s)


def _account_key_list(tx):
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    out = []
    for item in msg.get("accountKeys") or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            out.append(str(item.get("pubkey") or ""))
    return out


def _token_balances(meta, field, wallet):
    out = defaultdict(lambda: {"raw": 0, "ui": 0.0, "decimals": 0})
    for bal in (meta or {}).get(field) or []:
        if str(bal.get("owner") or "") != wallet:
            continue
        mint = str(bal.get("mint") or "")
        if not mint:
            continue
        ui = bal.get("uiTokenAmount") or {}
        raw = _i(ui.get("amount"), 0)
        decimals = _i(ui.get("decimals"), 0)
        ui_amt = _f(ui.get("uiAmountString"), raw / (10 ** decimals if decimals >= 0 else 1))
        out[mint]["raw"] += raw
        out[mint]["ui"] += ui_amt
        out[mint]["decimals"] = decimals
    return out


def _infer_dex(tx):
    keys = set(_account_key_list(tx))
    logs = " ".join(str(x).lower() for x in (((tx or {}).get("meta") or {}).get("logMessages") or []))
    if JUPITER_PROGRAM in keys or JUPITER_PROGRAM.lower() in logs or "jupiter" in logs:
        return "Jupiter"
    if PUMP_AMM_PROGRAM in keys or PUMP_PROGRAM in keys or "pump" in logs:
        return "Pump.fun/PumpSwap"
    if RAYDIUM_CPMM_PROGRAM in keys or "raydium" in logs:
        return "Raydium"
    if "meteora" in logs or "dlmm" in logs:
        return "Meteora"
    if "swap" in logs:
        return "DEX swap"
    return "DEX-agnostic"


def parse_wallet_swap(tx, wallet: str):
    """Return one net BUY/SELL event for a tracked wallet, or None.

    This intentionally parses post-transaction wallet deltas rather than individual swap
    instructions. That makes aggregator routes and DEX-specific multi-hop paths converge on
    the same final result. Failed transactions are never copied.
    """
    if not tx or not wallet:
        return None
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    keys = _account_key_list(tx)
    if wallet not in keys:
        return None
    wallet_idx = keys.index(wallet)
    pre_lamports = meta.get("preBalances") or []
    post_lamports = meta.get("postBalances") or []
    if wallet_idx >= len(pre_lamports) or wallet_idx >= len(post_lamports):
        return None

    fee = _i(meta.get("fee"), 0) if wallet_idx == 0 else 0
    sol_delta_raw = _i(post_lamports[wallet_idx]) - _i(pre_lamports[wallet_idx]) + fee
    sol_delta = sol_delta_raw / 1_000_000_000

    pre = _token_balances(meta, "preTokenBalances", wallet)
    post = _token_balances(meta, "postTokenBalances", wallet)
    mints = set(pre) | set(post)
    deltas = {}
    for mint in mints:
        decimals = max(_i(pre.get(mint, {}).get("decimals"), 0), _i(post.get(mint, {}).get("decimals"), 0))
        raw = _i(post.get(mint, {}).get("raw"), 0) - _i(pre.get(mint, {}).get("raw"), 0)
        ui = _f(post.get(mint, {}).get("ui"), 0) - _f(pre.get(mint, {}).get("ui"), 0)
        if raw:
            deltas[mint] = {"raw": raw, "ui": ui, "decimals": decimals,
                            "pre_raw": _i(pre.get(mint, {}).get("raw"), 0),
                            "post_raw": _i(post.get(mint, {}).get("raw"), 0)}

    # Wrapped SOL can be the route base without a persistent native SOL balance move.
    wsol_delta = _f(deltas.get(WSOL_MINT, {}).get("ui"), 0)
    usdc_delta = _f(deltas.get(USDC_MINT, {}).get("ui"), 0)
    effective_sol_delta = sol_delta + wsol_delta
    nonbase = [(m, d) for m, d in deltas.items() if m not in BASE_MINTS and abs(_f(d.get("ui"))) > 1e-12]
    if not nonbase:
        return None

    positives = [(m, d) for m, d in nonbase if _f(d.get("ui")) > 0]
    negatives = [(m, d) for m, d in nonbase if _f(d.get("ui")) < 0]

    action = token = base_mint = ""
    td = None
    base_ui = 0.0
    # Normal meme BUY: SOL/USDC leaves wallet and one non-base token arrives.
    if positives and (effective_sol_delta < -1e-7 or usdc_delta < -1e-6):
        token, td = max(positives, key=lambda x: abs(_f(x[1].get("ui"))))
        action = "BUY"
        if effective_sol_delta < -1e-7:
            base_mint, base_ui = WSOL_MINT, abs(effective_sol_delta)
        else:
            base_mint, base_ui = USDC_MINT, abs(usdc_delta)
    # Normal SELL: one non-base token leaves wallet and SOL/USDC arrives.
    elif negatives and (effective_sol_delta > 1e-7 or usdc_delta > 1e-6):
        token, td = max(negatives, key=lambda x: abs(_f(x[1].get("ui"))))
        action = "SELL"
        if effective_sol_delta > 1e-7:
            base_mint, base_ui = WSOL_MINT, abs(effective_sol_delta)
        else:
            base_mint, base_ui = USDC_MINT, abs(usdc_delta)
    else:
        # Token-to-token routes are intentionally not auto-copied because follower risk,
        # base value and direction are ambiguous without a stable base leg.
        return None

    token_ui = abs(_f(td.get("ui")))
    if token_ui <= 0 or base_ui <= 0:
        return None
    leader_price_base = base_ui / token_ui
    fraction = 0.0
    if action == "SELL":
        pre_raw = max(0, _i(td.get("pre_raw")))
        fraction = _clamp(abs(_i(td.get("raw"))) / pre_raw, 0.0, 1.0) if pre_raw else 1.0

    return {
        "action": action,
        "token": token,
        "base_mint": base_mint,
        "token_raw_delta": _i(td.get("raw")),
        "token_ui": token_ui,
        "token_decimals": _i(td.get("decimals")),
        "base_ui": base_ui,
        "leader_price_base": leader_price_base,
        "leader_sell_fraction": fraction,
        "dex": _infer_dex(tx),
        "jito_hint": "jito" in " ".join(str(x).lower() for x in (meta.get("logMessages") or [])),
        "slot": _i(tx.get("slot"), 0),
        "block_time": _i(tx.get("blockTime"), 0),
        "fee_lamports": _i(meta.get("fee"), 0),
    }


class CopyTradeEngine:
    def __init__(self, db, live_executor=None, version="v16"):
        self.db = db
        self.live_executor = live_executor
        self.version = version
        self.enabled = os.getenv("COPY_TRADING_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
        self.helius_key = os.getenv("HELIUS_API_KEY", "").strip()
        self.wss_url = (f"wss://mainnet.helius-rpc.com/?api-key={self.helius_key}" if self.helius_key
                        else os.getenv("SOLANA_RPC_WSS", "wss://api.mainnet-beta.solana.com").strip())
        self.rpc_url = (f"https://mainnet.helius-rpc.com/?api-key={self.helius_key}" if self.helius_key
                        else os.getenv("SOLANA_RPC_HTTP", "https://api.mainnet-beta.solana.com").strip())
        self.max_wallets = max(1, min(50, _i(os.getenv("COPY_MAX_TRACKED_WALLETS", "20"), 20)))
        self.paper_usd = max(1.0, _f(os.getenv("COPY_PAPER_SIZE_USD", "5"), 5))
        self.manual_test_usd = max(1.0, _f(os.getenv("COPY_MANUAL_TEST_SIZE_USD", "3"), 3))
        self.max_live_usd = max(1.0, _f(os.getenv("COPY_MAX_LIVE_POSITION_USD", "10"), 10))
        self.max_daily_loss = max(1.0, _f(os.getenv("COPY_MAX_DAILY_LOSS_USD", "15"), 15))
        self.max_open = max(1, _i(os.getenv("COPY_MAX_OPEN_POSITIONS", "5"), 5))
        self.max_event_age = max(2.0, _f(os.getenv("COPY_MAX_EVENT_AGE_SECONDS", "8"), 8))
        self.max_chase_pct = max(1.0, _f(os.getenv("COPY_MAX_CHASE_PCT", "12"), 12))
        self.min_liq = max(1000.0, _f(os.getenv("COPY_MIN_LIQUIDITY_USD", "10000"), 10000))
        self.alert_min_score = _clamp(_f(os.getenv("COPY_ALERT_MIN_WALLET_SCORE", "55"), 55), 0, 100)
        self.live_min_score = _clamp(_f(os.getenv("COPY_LIVE_MIN_WALLET_SCORE", "70"), 70), 0, 100)
        self.min_paper_hours = max(0, _f(os.getenv("COPY_LIVE_MIN_PAPER_HOURS", "48"), 48))
        self.min_closed = max(0, _i(os.getenv("COPY_LIVE_MIN_CLOSED_TRADES", "20"), 20))
        self.entry_fee_pct = max(0.0, _f(os.getenv("COPY_PAPER_ENTRY_FRICTION_PCT", "1.0"), 1.0))
        self.exit_fee_pct = max(0.0, _f(os.getenv("COPY_PAPER_EXIT_FRICTION_PCT", "1.0"), 1.0))
        self.auto_discover = os.getenv("COPY_AUTO_DISCOVERY", "true").lower() in {"1", "true", "yes", "on"}
        self.auto_track_min_buys = max(2, _i(os.getenv("COPY_AUTO_TRACK_MIN_BUYS", "3"), 3))
        self.auto_track_min_tokens = max(2, _i(os.getenv("COPY_AUTO_TRACK_MIN_TOKENS", "2"), 2))
        self.auto_track_min_usd = max(100.0, _f(os.getenv("COPY_AUTO_TRACK_MIN_USD", "1500"), 1500))
        self.auto_track_limit = max(1, min(self.max_wallets, _i(os.getenv("COPY_AUTO_TRACK_LIMIT", "10"), 10)))
        self.http_timeout = max(3.0, _f(os.getenv("COPY_HTTP_TIMEOUT_SECONDS", "10"), 10))
        self.rpc_slow_seconds = max(0.5, _f(os.getenv("COPY_RPC_SLOW_SECONDS", "3"), 3))
        self.rpc_trip_count = max(2, _i(os.getenv("COPY_RPC_CIRCUIT_TRIP_COUNT", "5"), 5))
        self.rpc_open_seconds = max(5, _i(os.getenv("COPY_RPC_CIRCUIT_OPEN_SECONDS", "15"), 15))
        self.rpc_failures = 0
        self.rpc_open_until = 0.0
        self.queue = asyncio.Queue(maxsize=1000)
        self.seen = {}
        self.connected = False
        self.last_message = 0.0
        self.last_error = ""
        self.ws = None
        self.req_id = 1000
        self.pending = {}
        self.sub_to_wallet = {}
        self.wallet_to_sub = {}
        self.stop_event = asyncio.Event()
        self.send_callback = None
        self._flow_tokens = defaultdict(set)
        self._init_schema()
        self._import_wallet_file()

    def _init_schema(self):
        with self.db.transaction():
            c = self.db.conn
            c.execute("""create table if not exists copy_wallets(
                address text primary key, label text, configured_score real default 60,
                history_score real default 0, enabled integer default 1, source text default 'manual',
                added_ts integer, last_seen_ts integer default 0, note text default '')""")
            c.execute("""create table if not exists copy_wallet_events(
                id integer primary key autoincrement, ts integer, wallet text, signature text,
                slot integer, action text, token text, base_mint text, token_ui real,
                base_ui real, leader_price_base real, leader_sell_fraction real default 0,
                dex text, jito_hint integer default 0, latency_ms real default 0,
                unique(wallet,signature,action,token))""")
            c.execute("""create table if not exists copy_signals(
                id integer primary key autoincrement, event_id integer unique, ts integer,
                wallet text, token text, symbol text, action text, wallet_score real,
                current_price real, liquidity real, leader_price_usd real, chase_pct real,
                consensus integer default 1, mode text, follower_size_usd real,
                status text, detail text)""")
            c.execute("""create table if not exists copy_paper_positions(
                id integer primary key autoincrement, wallet text, token text, symbol text,
                open_ts integer, close_ts integer default 0, entry_price real,
                amount_usd real, quantity real, remaining_quantity real, realized_pnl real default 0,
                active integer default 1, close_price real default 0, close_reason text default '',
                source_signature text, wallet_score real default 0)""")
            c.execute("""create table if not exists copy_paper_events(
                id integer primary key autoincrement, ts integer, position_id integer, side text,
                price real, quantity real, usd_value real, pnl real default 0, reason text)""")
            c.execute("""create table if not exists copy_live_positions(
                id integer primary key autoincrement, wallet text, token text, symbol text,
                open_ts integer, close_ts integer default 0, amount_usd real, cost_remaining real default 0,
                token_raw_initial text, token_raw_remaining text, active integer default 1,
                buy_signature text, sell_signature text, realized_pnl real default 0)""")
            live_cols={r[1] for r in c.execute("pragma table_info(copy_live_positions)")}
            if "cost_remaining" not in live_cols:
                c.execute("alter table copy_live_positions add column cost_remaining real default 0")
            c.execute("""create table if not exists copy_wallet_candidates(
                address text primary key, first_ts integer, last_ts integer,
                buy_count integer default 0, distinct_tokens integer default 0,
                total_buy_usd real default 0, smart_tag_count integer default 0,
                risky_tag_count integer default 0, source text default '')""")
            c.execute("""create table if not exists copy_candidate_tokens(
                address text, token text, first_ts integer, last_ts integer,
                buy_count integer default 0, buy_usd real default 0,
                primary key(address,token))""")
            c.execute("""create table if not exists copy_wallet_history(
                address text primary key, analyzed_ts integer, tx_count integer,
                completed integer, wins integer, win_rate real, median_roi real,
                mean_roi real, score real, detail text)""")
            c.execute("create index if not exists idx_copy_events_wallet_ts on copy_wallet_events(wallet,ts)")
            c.execute("create index if not exists idx_copy_events_token_ts on copy_wallet_events(token,ts)")
            c.execute("create index if not exists idx_copy_paper_active on copy_paper_positions(active,token,wallet)")
            c.execute("create index if not exists idx_copy_signals_ts on copy_signals(ts)")
            if not self.db.get_meta("copy_engine_first_ts", ""):
                self.db.set_meta_no_commit("copy_engine_first_ts", str(int(time.time())))
            if not self.db.get_meta("copy_kill_switch", ""):
                self.db.set_meta_no_commit("copy_kill_switch", "1")
            if not self.db.get_meta("copy_live_enabled", ""):
                self.db.set_meta_no_commit("copy_live_enabled", "0")

    def _import_wallet_file(self):
        p = Path(__file__).resolve().parent / "tracked_wallets.json"
        try:
            data = json.loads(p.read_text()) if p.exists() else {}
        except Exception:
            data = {}
        for row in data.get("wallets") or []:
            if isinstance(row, str):
                self.add_wallet(row, source="file")
            elif isinstance(row, dict):
                self.add_wallet(row.get("address"), row.get("label") or "", row.get("score", 60), source="file")

    def wallets(self, enabled_only=True):
        q = "select * from copy_wallets" + (" where enabled=1" if enabled_only else "") + " order by max(history_score,configured_score) desc, added_ts asc"
        return [dict(r) for r in self.db.conn.execute(q).fetchall()]

    def wallet_score(self, address):
        row = self.db.conn.execute("select * from copy_wallets where address=?", (address,)).fetchone()
        if not row:
            return 0.0
        row = dict(row)
        h = _f(row.get("history_score"))
        return _clamp(h if h > 0 else _f(row.get("configured_score"), 60), 0, 100)

    def add_wallet(self, address, label="", score=60, source="manual"):
        address = str(address or "").strip()
        if not _valid_pubkey(address):
            return False, "invalid Solana wallet address"
        existing = self.db.conn.execute("select 1 from copy_wallets where address=?", (address,)).fetchone()
        if not existing and len(self.wallets(False)) >= self.max_wallets:
            return False, f"wallet limit reached ({self.max_wallets})"
        now = int(time.time())
        with self.db.transaction():
            self.db.conn.execute("""insert into copy_wallets(address,label,configured_score,enabled,source,added_ts)
                values(?,?,?,?,?,?) on conflict(address) do update set
                label=case when excluded.label<>'' then excluded.label else copy_wallets.label end,
                configured_score=excluded.configured_score, enabled=1, source=excluded.source""",
                (address, str(label or "")[:80], _clamp(_f(score, 60), 0, 100), 1, str(source or "manual"), now))
        return True, "tracked"

    def remove_wallet(self, address):
        with self.db.transaction():
            self.db.conn.execute("update copy_wallets set enabled=0 where address=?", (str(address or "").strip(),))
        return True

    def status(self):
        wallets = self.wallets()
        closed = self.db.conn.execute("select count(*) from copy_paper_positions where active=0").fetchone()[0]
        pnl = _f(self.db.conn.execute("select coalesce(sum(realized_pnl),0) from copy_paper_positions where active=0").fetchone()[0])
        open_n = self.db.conn.execute("select count(*) from copy_paper_positions where active=1").fetchone()[0]
        live = self.db.get_meta("copy_live_enabled", "0") == "1"
        kill = self.db.get_meta("copy_kill_switch", "1") == "1"
        return {"connected": self.connected, "wallets": len(wallets), "paper_closed": int(closed), "paper_open": int(open_n),
                "paper_pnl": pnl, "live": live, "kill": kill, "last_error": self.last_error,
                "rpc_circuit_open": bool(time.time() < self.rpc_open_until),
                "source": "Helius WSS" if self.helius_key else "Solana public WSS"}

    async def _rpc(self, http, method, params):
        if time.time() < self.rpc_open_until:
            return None
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        timeout = aiohttp.ClientTimeout(total=self.http_timeout)
        started=time.monotonic()
        try:
            async with http.session.post(self.rpc_url, json=payload, timeout=timeout) as r:
                data = await r.json(content_type=None)
                elapsed=time.monotonic()-started
                bad=(r.status == 429 or r.status >= 500 or elapsed >= self.rpc_slow_seconds or not isinstance(data,dict) or bool(data.get("error")))
                if bad:
                    self.rpc_failures += 1
                    if self.rpc_failures >= self.rpc_trip_count:
                        self.rpc_open_until=time.time()+self.rpc_open_seconds
                        self.last_error=f"copy RPC circuit open {self.rpc_open_seconds}s after slow/failing calls"
                    if r.status == 429:
                        await asyncio.sleep(0.8)
                else:
                    self.rpc_failures=0
                if r.status != 200 or not isinstance(data, dict) or data.get("error"):
                    return None
                return data.get("result")
        except Exception as exc:
            self.rpc_failures += 1
            if self.rpc_failures >= self.rpc_trip_count:
                self.rpc_open_until=time.time()+self.rpc_open_seconds
            self.last_error = f"RPC {method}: {type(exc).__name__}: {exc}"
            return None

    async def _get_tx(self, http, sig):
        for attempt in range(6):
            tx = await self._rpc(http, "getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])
            if tx:
                return tx
            await asyncio.sleep(min(0.25 * (attempt + 1), 1.25))
        return None

    async def _price_context(self, http, token):
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with http.session.get(f"{DEXSCREENER}/token-pairs/v1/solana/{token}", timeout=timeout) as r:
                data = await r.json(content_type=None)
                if r.status != 200 or not isinstance(data, list) or not data:
                    return {}
            pairs = [x for x in data if isinstance(x, dict)]
            pairs.sort(key=lambda x: _f(((x.get("liquidity") or {}).get("usd"))), reverse=True)
            p = pairs[0]
            return {"price": _f(p.get("priceUsd")), "liq": _f((p.get("liquidity") or {}).get("usd")),
                    "symbol": str((p.get("baseToken") or {}).get("symbol") or "?"),
                    "name": str((p.get("baseToken") or {}).get("name") or ""),
                    "mcap": _f(p.get("marketCap") or p.get("fdv")),
                    "url": str(p.get("url") or "")}
        except Exception:
            return {}

    async def _sol_usd(self, http):
        if self.live_executor:
            try:
                v = await self.live_executor.sol_usd(http)
                if v:
                    return float(v)
            except Exception:
                pass
        try:
            async with http.session.get(f"{DEXSCREENER}/token-pairs/v1/solana/{WSOL_MINT}", timeout=aiohttp.ClientTimeout(total=6)) as r:
                data = await r.json(content_type=None)
            if isinstance(data, list) and data:
                return max((_f(x.get("priceUsd")) for x in data if isinstance(x, dict)), default=0)
        except Exception:
            pass
        return 0.0

    def _consensus(self, token, now=None, window=90):
        now = int(now or time.time())
        rows = self.db.conn.execute("""select distinct wallet from copy_wallet_events
            where token=? and action='BUY' and ts>=?""", (token, now - int(window))).fetchall()
        return len(rows)

    def _paper_position(self, wallet, token):
        r = self.db.conn.execute("select * from copy_paper_positions where wallet=? and token=? and active=1 order by id desc limit 1", (wallet, token)).fetchone()
        return dict(r) if r else None

    def _open_paper(self, wallet, token, symbol, price, score, signature):
        if price <= 0 or self._paper_position(wallet, token):
            return None
        amount = self.paper_usd
        invest = amount * (1 - self.entry_fee_pct / 100)
        qty = invest / price
        with self.db.transaction():
            cur = self.db.conn.execute("""insert into copy_paper_positions(wallet,token,symbol,open_ts,entry_price,amount_usd,quantity,remaining_quantity,source_signature,wallet_score)
                values(?,?,?,?,?,?,?,?,?,?)""", (wallet, token, symbol, int(time.time()), price, amount, qty, qty, signature, score))
            pid = cur.lastrowid
            self.db.conn.execute("insert into copy_paper_events(ts,position_id,side,price,quantity,usd_value,reason) values(?,?,?,?,?,?,?)",
                                 (int(time.time()), pid, "BUY", price, qty, amount, "leader wallet buy"))
        return pid

    def _close_paper(self, pos, price, fraction, reason):
        if not pos or price <= 0:
            return None
        fraction = _clamp(_f(fraction, 1.0), 0.01, 1.0)
        remaining = _f(pos.get("remaining_quantity"), _f(pos.get("quantity")))
        qty = min(remaining, remaining * fraction)
        gross = qty * price
        net = gross * (1 - self.exit_fee_pct / 100)
        avg_cost = _f(pos.get("amount_usd")) / max(_f(pos.get("quantity")), 1e-18)
        cost = qty * avg_cost
        pnl = net - cost
        new_remaining = max(0.0, remaining - qty)
        final = new_remaining <= max(_f(pos.get("quantity")) * 0.01, 1e-18) or fraction >= 0.99
        total_realized = _f(pos.get("realized_pnl")) + pnl
        with self.db.transaction():
            self.db.conn.execute("insert into copy_paper_events(ts,position_id,side,price,quantity,usd_value,pnl,reason) values(?,?,?,?,?,?,?,?)",
                                 (int(time.time()), int(pos["id"]), "SELL", price, qty, net, pnl, reason))
            if final:
                self.db.conn.execute("""update copy_paper_positions set active=0,close_ts=?,close_price=?,remaining_quantity=0,realized_pnl=?,close_reason=? where id=?""",
                                     (int(time.time()), price, total_realized, reason, int(pos["id"])))
            else:
                self.db.conn.execute("update copy_paper_positions set remaining_quantity=?,realized_pnl=? where id=?",
                                     (new_remaining, total_realized, int(pos["id"])))
        return {"pnl": pnl, "final": final, "remaining": new_remaining}

    def observe_flow_trades(self, token, trades, pair=None):
        """Feed Birdeye wallet-flow observations into automatic smart-wallet discovery."""
        if not self.auto_discover:
            return
        now = int(time.time())
        price = _f((pair or {}).get("priceUsd"))
        for t in trades or []:
            if str(t.get("side")) != "buy":
                continue
            addr = str(t.get("owner") or "")
            if not _valid_pubkey(addr):
                continue
            usd = max(0.0, _f(t.get("usd")))
            with self.db.transaction():
                self.db.conn.execute("""insert into copy_candidate_tokens(address,token,first_ts,last_ts,buy_count,buy_usd)
                    values(?,?,?,?,1,?) on conflict(address,token) do update set
                    last_ts=excluded.last_ts,buy_count=copy_candidate_tokens.buy_count+1,buy_usd=copy_candidate_tokens.buy_usd+excluded.buy_usd""",
                    (addr, token, now, now, usd))
                row = self.db.conn.execute("select count(*) n,coalesce(sum(buy_count),0) buys,coalesce(sum(buy_usd),0) usd from copy_candidate_tokens where address=?", (addr,)).fetchone()
                self.db.conn.execute("""insert into copy_wallet_candidates(address,first_ts,last_ts,buy_count,distinct_tokens,total_buy_usd,source)
                    values(?,?,?,?,?,?,?) on conflict(address) do update set
                    last_ts=excluded.last_ts,buy_count=excluded.buy_count,distinct_tokens=excluded.distinct_tokens,total_buy_usd=excluded.total_buy_usd""",
                    (addr, now, now, int(row["buys"]), int(row["n"]), _f(row["usd"]), "Birdeye flow"))
            self._maybe_auto_track(addr)

    def observe_tagged_wallet(self, address, smart=True, risky=False, source="Birdeye top trader"):
        if not self.auto_discover or not _valid_pubkey(address):
            return
        now = int(time.time())
        with self.db.transaction():
            self.db.conn.execute("""insert into copy_wallet_candidates(address,first_ts,last_ts,buy_count,distinct_tokens,total_buy_usd,smart_tag_count,risky_tag_count,source)
                values(?,?,?,0,0,0,?,?,?) on conflict(address) do update set
                last_ts=excluded.last_ts,smart_tag_count=copy_wallet_candidates.smart_tag_count+excluded.smart_tag_count,
                risky_tag_count=copy_wallet_candidates.risky_tag_count+excluded.risky_tag_count,source=excluded.source""",
                (address, now, now, 1 if smart else 0, 1 if risky else 0, source))
        self._maybe_auto_track(address)

    def _maybe_auto_track(self, address):
        row = self.db.conn.execute("select * from copy_wallet_candidates where address=?", (address,)).fetchone()
        if not row:
            return
        r = dict(row)
        if _i(r.get("risky_tag_count")) > 0:
            return
        smart = _i(r.get("smart_tag_count")) >= 1
        flow = (_i(r.get("buy_count")) >= self.auto_track_min_buys and
                _i(r.get("distinct_tokens")) >= self.auto_track_min_tokens and
                _f(r.get("total_buy_usd")) >= self.auto_track_min_usd)
        auto_n = self.db.conn.execute("select count(*) from copy_wallets where enabled=1 and source like 'auto%'").fetchone()[0]
        if (smart or flow) and auto_n < self.auto_track_limit:
            score = 62 if smart else 55
            self.add_wallet(address, "auto-smart" if smart else "auto-flow", score, source="auto-smart" if smart else "auto-flow")

    def discovered(self, limit=20):
        rows = self.db.conn.execute("""select c.*,w.enabled,w.history_score,w.configured_score from copy_wallet_candidates c
            left join copy_wallets w on w.address=c.address
            order by c.risky_tag_count asc,c.smart_tag_count desc,c.distinct_tokens desc,c.total_buy_usd desc limit ?""", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    async def analyze_wallet_history(self, http, address, limit=40):
        if not _valid_pubkey(address):
            return {"ok": False, "error": "invalid wallet"}
        sigs = await self._rpc(http, "getSignaturesForAddress", [address, {"limit": max(10, min(100, int(limit))) }])
        if not isinstance(sigs, list):
            return {"ok": False, "error": "could not fetch wallet history"}
        events = []
        for s in reversed(sigs):
            if s.get("err") is not None:
                continue
            sig = str(s.get("signature") or "")
            if not sig:
                continue
            tx = await self._get_tx(http, sig)
            ev = parse_wallet_swap(tx, address) if tx else None
            if ev:
                ev["signature"] = sig
                ev["ts"] = _i((tx or {}).get("blockTime"), _i(s.get("blockTime"), 0))
                events.append(ev)
            await asyncio.sleep(0.05 if self.helius_key else 0.18)
        inv = {}
        rois = []
        for ev in sorted(events, key=lambda x: x.get("ts", 0)):
            key = (ev["token"], ev["base_mint"])
            qty = _f(ev.get("token_ui")); base = _f(ev.get("base_ui"))
            if ev["action"] == "BUY":
                cur = inv.setdefault(key, {"qty": 0.0, "cost": 0.0})
                cur["qty"] += qty; cur["cost"] += base
            elif ev["action"] == "SELL" and key in inv and inv[key]["qty"] > 0:
                cur = inv[key]
                sold = min(qty, cur["qty"])
                cost = cur["cost"] * (sold / cur["qty"])
                proceeds = base * (sold / max(qty, 1e-18))
                if cost > 0:
                    rois.append((proceeds / cost - 1) * 100)
                cur["qty"] -= sold; cur["cost"] -= cost
        completed = len(rois); wins = sum(1 for x in rois if x > 0)
        win_rate = wins / completed if completed else 0.0
        med = statistics.median(rois) if rois else 0.0
        mean = statistics.mean(rois) if rois else 0.0
        score = _clamp(45 + (win_rate - 0.5) * 45 + _clamp(med * 0.6, -18, 18) + min(completed, 10), 0, 100) if completed else 50
        detail = f"{completed} reconstructed exits; win {win_rate*100:.0f}%; median ROI {med:+.1f}%"
        with self.db.transaction():
            self.db.conn.execute("""insert into copy_wallet_history(address,analyzed_ts,tx_count,completed,wins,win_rate,median_roi,mean_roi,score,detail)
                values(?,?,?,?,?,?,?,?,?,?) on conflict(address) do update set
                analyzed_ts=excluded.analyzed_ts,tx_count=excluded.tx_count,completed=excluded.completed,wins=excluded.wins,
                win_rate=excluded.win_rate,median_roi=excluded.median_roi,mean_roi=excluded.mean_roi,score=excluded.score,detail=excluded.detail""",
                (address, int(time.time()), len(events), completed, wins, win_rate, med, mean, score, detail))
            self.db.conn.execute("update copy_wallets set history_score=? where address=?", (score, address))
        return {"ok": True, "events": len(events), "completed": completed, "wins": wins, "win_rate": win_rate,
                "median_roi": med, "mean_roi": mean, "score": score, "detail": detail}

    async def _record_event(self, wallet, signature, ev, received_ts):
        now = int(time.time())
        latency = max(0.0, (time.time() - received_ts) * 1000)
        try:
            with self.db.transaction():
                cur = self.db.conn.execute("""insert or ignore into copy_wallet_events(ts,wallet,signature,slot,action,token,base_mint,token_ui,base_ui,leader_price_base,leader_sell_fraction,dex,jito_hint,latency_ms)
                    values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (now, wallet, signature, _i(ev.get("slot")), ev.get("action"), ev.get("token"), ev.get("base_mint"),
                     _f(ev.get("token_ui")), _f(ev.get("base_ui")), _f(ev.get("leader_price_base")), _f(ev.get("leader_sell_fraction")),
                     ev.get("dex"), 1 if ev.get("jito_hint") else 0, latency))
                # sqlite's cursor.lastrowid is sticky after INSERT OR IGNORE. rowcount is
                # the reliable signal that this event was newly inserted, which prevents
                # a duplicate websocket notification from being processed twice.
                eid = int(cur.lastrowid or 0) if cur.rowcount == 1 else 0
                if eid:
                    self.db.conn.execute("update copy_wallets set last_seen_ts=? where address=?", (now, wallet))
            return eid or None
        except Exception:
            return None

    def _live_position(self, wallet, token):
        r = self.db.conn.execute("select * from copy_live_positions where wallet=? and token=? and active=1 order by id desc limit 1", (wallet, token)).fetchone()
        return dict(r) if r else None

    def _daily_live_pnl(self):
        cutoff=int(time.time()//86400*86400)
        return _f(self.db.conn.execute("select coalesce(sum(realized_pnl),0) from copy_live_positions where active=0 and close_ts>=?", (cutoff,)).fetchone()[0])

    async def _maybe_live_buy(self, http, wallet, signature, ev, ctx, score, chase, hard_live_block):
        if self.db.get_meta("copy_live_enabled", "0") != "1":
            return None
        gate=self.live_gate()
        if not gate.get("allowed"):
            return {"ok":False,"blocked":True,"reason":"; ".join(gate.get("reasons") or [])}
        if hard_live_block:
            return {"ok":False,"blocked":True,"reason":"; ".join(hard_live_block)}
        if self._live_position(wallet, ev["token"]):
            return {"ok":False,"blocked":True,"reason":"copy position already open"}
        open_n=self.db.conn.execute("select count(*) from copy_live_positions where active=1").fetchone()[0]
        if int(open_n)>=self.max_open:
            return {"ok":False,"blocked":True,"reason":"max copy positions reached"}
        daily=self._daily_live_pnl()
        if daily <= -self.max_daily_loss:
            return {"ok":False,"blocked":True,"reason":f"daily copy loss lock {daily:+.2f}"}
        if score < self.live_min_score:
            return {"ok":False,"blocked":True,"reason":f"wallet score {score:.0f} below live minimum"}
        solusd=await self._sol_usd(http)
        if solusd<=0:
            return {"ok":False,"blocked":True,"reason":"SOL/USD unavailable"}
        size=min(self.max_live_usd, max(1.0, _f(os.getenv("COPY_LIVE_SIZE_USD", str(self.manual_test_usd)), self.manual_test_usd)))
        amount_raw=int(size/solusd*1_000_000_000)
        idem=f"copybuy:{wallet}:{signature}"
        result=await self.live_executor.swap(http, WSOL_MINT, ev["token"], amount_raw,
            db=self.db,idempotency_key=idem,side="COPY_BUY",token=ev["token"],symbol=ctx.get("symbol") or "?",
            tier="COPY EDGE",amount_usd=size,context={"leader_wallet":wallet,"leader_signature":signature,"wallet_score":score})
        if result.get("ok"):
            raw=int(result.get("output_raw") or 0)
            with self.db.transaction():
                self.db.conn.execute("""insert into copy_live_positions(wallet,token,symbol,open_ts,amount_usd,cost_remaining,token_raw_initial,token_raw_remaining,buy_signature)
                    values(?,?,?,?,?,?,?,?,?)""",(wallet,ev["token"],ctx.get("symbol") or "?",int(time.time()),size,size,str(raw),str(raw),result.get("signature") or ""))
        return result

    async def _maybe_live_sell(self, http, wallet, signature, ev, ctx, fraction):
        if self.db.get_meta("copy_live_enabled", "0") != "1" or not self.live_executor:
            return None
        pos=self._live_position(wallet,ev["token"])
        if not pos:
            return None
        # Exits remain allowed when the strategy is kill-switched; the kill switch only blocks new buys.
        raw_remaining=max(0,_i(pos.get("token_raw_remaining")))
        if raw_remaining<=0:
            return None
        actual=await self.live_executor.token_raw_balance(http,ev["token"])
        if actual is None:
            return {"ok":False,"blocked":True,"reason":"token balance unavailable"}
        raw_remaining=min(raw_remaining,int(actual))
        sell_raw=max(1,int(raw_remaining*_clamp(fraction,0.05,1.0)))
        idem=f"copysell:{wallet}:{signature}:{pos['id']}"
        result=await self.live_executor.swap(http,ev["token"],WSOL_MINT,sell_raw,
            db=self.db,idempotency_key=idem,side="COPY_SELL",token=ev["token"],symbol=ctx.get("symbol") or pos.get("symbol") or "?",
            tier="COPY EDGE",position_id=int(pos["id"]),context={"leader_wallet":wallet,"leader_signature":signature,"copy_position_id":int(pos["id"])})
        if result.get("ok"):
            out_raw=int(result.get("output_raw") or 0); solusd=await self._sol_usd(http)
            proceeds=(out_raw/1_000_000_000)*solusd if solusd>0 else 0.0
            frac=sell_raw/max(raw_remaining,1)
            cost_sold=_f(pos.get("cost_remaining"),_f(pos.get("amount_usd")))*frac
            pnl=proceeds-cost_sold
            new_raw=max(0,raw_remaining-sell_raw); new_cost=max(0.0,_f(pos.get("cost_remaining"),_f(pos.get("amount_usd")))-cost_sold)
            final=new_raw<=max(1,int(_i(pos.get("token_raw_initial"))*0.01)) or fraction>=0.99
            with self.db.transaction():
                if final:
                    self.db.conn.execute("update copy_live_positions set active=0,close_ts=?,token_raw_remaining='0',cost_remaining=0,sell_signature=?,realized_pnl=realized_pnl+? where id=?",
                        (int(time.time()),result.get("signature") or "",pnl,int(pos["id"])))
                else:
                    self.db.conn.execute("update copy_live_positions set token_raw_remaining=?,cost_remaining=?,sell_signature=?,realized_pnl=realized_pnl+? where id=?",
                        (str(new_raw),new_cost,result.get("signature") or "",pnl,int(pos["id"])))
            result["pnl_usd"]=pnl
        return result

    async def _handle_buy(self, http, wallet, signature, ev, event_id):
        ctx = await self._price_context(http, ev["token"])
        if not ctx.get("price"):
            return
        score = self.wallet_score(wallet)
        solusd = await self._sol_usd(http) if ev.get("base_mint") == WSOL_MINT else 1.0
        leader_price_usd = _f(ev.get("leader_price_base")) * (solusd if ev.get("base_mint") == WSOL_MINT else 1.0)
        chase = ((ctx["price"] / leader_price_usd - 1) * 100) if leader_price_usd > 0 else 0.0
        consensus = self._consensus(ev["token"])
        stale = max(0, int(time.time()) - _i(ev.get("block_time"), int(time.time())))
        hard_live_block = []
        if stale > self.max_event_age: hard_live_block.append(f"event age {stale}s")
        if _f(ctx.get("liq")) < self.min_liq: hard_live_block.append(f"liquidity ${_f(ctx.get('liq')):,.0f}")
        if chase > self.max_chase_pct: hard_live_block.append(f"chase {chase:+.1f}%")
        if score < self.live_min_score: hard_live_block.append(f"wallet score {score:.0f}")

        self._open_paper(wallet, ev["token"], ctx.get("symbol") or "?", ctx["price"], score, signature)
        mode = "COPY BUY NOW — PAPER/MANUAL TEST"
        size = self.manual_test_usd
        status = "PAPER"
        if score < self.alert_min_score:
            mode = "COPY WATCH — WALLET NOT PROVEN"
            size = 0.0
        detail = "; ".join(hard_live_block)
        with self.db.transaction():
            self.db.conn.execute("""insert or ignore into copy_signals(event_id,ts,wallet,token,symbol,action,wallet_score,current_price,liquidity,leader_price_usd,chase_pct,consensus,mode,follower_size_usd,status,detail)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, int(time.time()), wallet, ev["token"], ctx.get("symbol"), "BUY", score, ctx["price"], _f(ctx.get("liq")),
                 leader_price_usd, chase, consensus, mode, size, status, detail))
        live_result = await self._maybe_live_buy(http, wallet, signature, ev, ctx, score, chase, hard_live_block)
        if live_result and live_result.get("ok"):
            mode = "COPY BUY NOW — LIVE EXECUTED"
            status = "LIVE"
            with self.db.transaction():
                self.db.conn.execute("update copy_signals set mode=?,status=?,detail=? where event_id=?",
                    (mode,status,str(live_result.get("signature") or ""),event_id))
        elif live_result and live_result.get("blocked") and not detail:
            detail=str(live_result.get("reason") or "")
        if self.send_callback and score >= self.alert_min_score:
            label = self.db.conn.execute("select label from copy_wallets where address=?", (wallet,)).fetchone()
            label = str(label[0] if label and label[0] else wallet[:6] + "…" + wallet[-4:])
            base_name = "SOL" if ev.get("base_mint") == WSOL_MINT else "USDC"
            msg = (f"🐋 {mode}\n"
                   f"{ctx.get('symbol') or '?'} | {ev['token']}\n"
                   f"Leader: {label} | wallet score {score:.0f}/100 | {ev.get('dex')}\n"
                   f"Leader bought ~{_f(ev.get('base_ui')):.3f} {base_name} | consensus {consensus} tracked wallet(s)\n"
                   f"Current price ${ctx['price']:.8g} | liquidity ${_f(ctx.get('liq')):,.0f} | follower chase {chase:+.1f}%\n"
                   f"Suggested TEST size: ${size:.2f}. Paper copy opened automatically.\n"
                   + (f"Live blockers: {detail}\n" if detail else "")
                   + (f"Follower tx: {live_result.get('signature')}\n" if live_result and live_result.get("ok") else "")
                   + "This is a public on-chain copy signal, not guaranteed profit.")
            await self.send_callback(msg)

    async def _handle_sell(self, http, wallet, signature, ev, event_id):
        ctx = await self._price_context(http, ev["token"])
        price = _f(ctx.get("price"))
        pos = self._paper_position(wallet, ev["token"])
        fraction = _clamp(_f(ev.get("leader_sell_fraction"), 1.0), 0.05, 1.0)
        result = self._close_paper(pos, price, fraction, "leader wallet sell") if pos and price > 0 else None
        live_result = await self._maybe_live_sell(http,wallet,signature,ev,ctx,fraction)
        if self.send_callback:
            symbol = ctx.get("symbol") or (pos or {}).get("symbol") or "?"
            pnl_line = f" | paper leg P/L {result['pnl']:+.2f}" if result else ""
            live_line = f" | live tx {live_result.get('signature','')[:10]}…" if live_result and live_result.get("ok") else ""
            await self.send_callback(
                f"🐋 COPY EXIT — leader sold {fraction*100:.0f}%\n{symbol} | {ev['token']}\n"
                f"Wallet {wallet[:6]}…{wallet[-4:]} | {ev.get('dex')}{pnl_line}{live_line}\n"
                "If you manually copied this leader, review/scale your exit now.")

    async def _process_signature(self, http, wallet, signature, received_ts):
        key = (wallet, signature)
        if key in self.seen:
            return
        self.seen[key] = time.time()
        tx = await self._get_tx(http, signature)
        if not tx:
            return
        ev = parse_wallet_swap(tx, wallet)
        if not ev:
            return
        ev["block_time"] = _i(tx.get("blockTime"), int(time.time()))
        event_id = await self._record_event(wallet, signature, ev, received_ts)
        if not event_id:
            return
        if ev["action"] == "BUY":
            await self._handle_buy(http, wallet, signature, ev, event_id)
        elif ev["action"] == "SELL":
            await self._handle_sell(http, wallet, signature, ev, event_id)

    async def _worker(self, http):
        while not self.stop_event.is_set():
            try:
                wallet, sig, received = await asyncio.wait_for(self.queue.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            try:
                await self._process_signature(http, wallet, sig, received)
            except Exception as exc:
                self.last_error = f"worker {type(exc).__name__}: {exc}"
            finally:
                self.queue.task_done()
            await asyncio.sleep(0.03 if self.helius_key else 0.18)

    async def _subscribe_wallet(self, wallet):
        if not self.ws or wallet in self.wallet_to_sub:
            return
        self.req_id += 1
        rid = self.req_id
        self.pending[rid] = wallet
        await self.ws.send(json.dumps({"jsonrpc":"2.0","id":rid,"method":"logsSubscribe",
                                      "params":[{"mentions":[wallet]},{"commitment":"confirmed"}]}))

    async def _ws_loop(self):
        while not self.stop_event.is_set():
            try:
                async with websockets.connect(self.wss_url, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=8_000_000) as ws:
                    self.ws = ws; self.connected = True; self.last_error = ""
                    self.pending.clear(); self.sub_to_wallet.clear(); self.wallet_to_sub.clear()
                    for row in self.wallets()[:self.max_wallets]:
                        await self._subscribe_wallet(row["address"])
                    while not self.stop_event.is_set():
                        # Subscribe newly auto-discovered/manual-added wallets without reconnecting.
                        for row in self.wallets()[:self.max_wallets]:
                            if row["address"] not in self.wallet_to_sub and row["address"] not in self.pending.values():
                                await self._subscribe_wallet(row["address"])
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=8)
                        except asyncio.TimeoutError:
                            continue
                        self.last_message = time.time()
                        msg = json.loads(raw)
                        if msg.get("id") in self.pending and isinstance(msg.get("result"), int):
                            wallet = self.pending.pop(msg["id"])
                            sub = msg["result"]; self.sub_to_wallet[sub] = wallet; self.wallet_to_sub[wallet] = sub
                            continue
                        p = msg.get("params") or {}; sub = p.get("subscription")
                        wallet = self.sub_to_wallet.get(sub)
                        value = ((p.get("result") or {}).get("value") or {})
                        sig = str(value.get("signature") or "")
                        if wallet and sig and value.get("err") is None:
                            try:
                                self.queue.put_nowait((wallet, sig, time.time()))
                            except asyncio.QueueFull:
                                self.last_error = "copy transaction queue full"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False; self.ws = None
                self.last_error = f"WSS {type(exc).__name__}: {exc}"
                await asyncio.sleep(2.0)
            finally:
                self.connected = False; self.ws = None

    async def _paper_timeout_loop(self, http):
        max_age = max(30, _i(os.getenv("COPY_PAPER_MAX_HOLD_MINUTES", "360"), 360)) * 60
        while not self.stop_event.is_set():
            try:
                rows = self.db.conn.execute("select * from copy_paper_positions where active=1 and open_ts<? limit 20", (int(time.time()) - max_age,)).fetchall()
                for row in rows:
                    pos = dict(row); ctx = await self._price_context(http, pos["token"]); price = _f(ctx.get("price"))
                    if price > 0:
                        self._close_paper(pos, price, 1.0, "paper max-hold timeout")
            except Exception as exc:
                self.last_error = f"paper monitor {type(exc).__name__}: {exc}"
            await asyncio.sleep(30)

    async def run(self, http, send_callback=None):
        if not self.enabled:
            return
        self.send_callback = send_callback
        workers = [asyncio.create_task(self._worker(http)) for _ in range(max(1, min(3, _i(os.getenv("COPY_TX_WORKERS", "2"), 2))))]
        paper_task = asyncio.create_task(self._paper_timeout_loop(http))
        try:
            await self._ws_loop()
        finally:
            self.stop_event.set()
            for t in workers + [paper_task]:
                t.cancel()
            await asyncio.gather(*workers, paper_task, return_exceptions=True)

    def report(self):
        rows = self.db.conn.execute("select * from copy_paper_positions where active=0").fetchall()
        pnls = [_f(r["realized_pnl"]) for r in rows]
        wins = sum(1 for x in pnls if x > 0)
        gross_wins = sum(x for x in pnls if x > 0)
        gross_losses = -sum(x for x in pnls if x < 0)
        return {"closed": len(rows), "wins": wins, "win_rate": wins / len(rows) if rows else 0.0,
                "pnl": sum(pnls), "pf": gross_wins / gross_losses if gross_losses > 0 else (999.0 if gross_wins > 0 else 0.0),
                "open": self.db.conn.execute("select count(*) from copy_paper_positions where active=1").fetchone()[0]}

    def live_gate(self):
        reasons = []
        first = _i(self.db.get_meta("copy_engine_first_ts", "0"), 0)
        hours = (time.time() - first) / 3600 if first else 0
        rep = self.report()
        if self.db.get_meta("copy_kill_switch", "1") == "1": reasons.append("copy kill switch ON")
        if hours < self.min_paper_hours: reasons.append(f"paper age {hours:.1f}h < {self.min_paper_hours:.0f}h")
        if rep["closed"] < self.min_closed: reasons.append(f"closed copy trades {rep['closed']} < {self.min_closed}")
        if rep["pnl"] <= 0: reasons.append(f"copy paper net {rep['pnl']:+.2f} not positive")
        if not self.live_executor or not self.live_executor.ready(): reasons.append("live executor not ready")
        return {"allowed": not reasons, "reasons": reasons, "hours": hours, "report": rep}
