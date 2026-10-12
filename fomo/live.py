"""Live autopilot — OFF unless you turn it on. Makes the paper autopilot's trades for real.

How it trades
  It does exactly what the paper autopilot decides (same alerts, same exit plan): when paper buys, it
  buys LIVE_TRADE_SOL of the coin; when paper sells half or all, it sells the same share of what it holds.
  Swaps go through Jupiter (best route, including pump.fun) and are signed on your Mac.

Safety rails
  • A separate wallet: put ONLY that wallet's private key in .env as TRADING_PRIVATE_KEY. Fund it with
    what you're willing to lose. The key never leaves your Mac, is never shown, logged, exported or sent.
  • LIVE_DRY_RUN (on by default): gets real Jupiter quotes and signs the transaction, but doesn't send it —
    you see exactly what it would have done. Turn it off only when the dry runs look right.
  • LIVE_MAX_OPEN coins at once · LIVE_DAILY_LOSS_SOL: once today's realized loss hits it, no new buys
    until tomorrow · it keeps a SOL reserve for fees · /sellall sells everything · /live off stops buying.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from datetime import datetime

import aiohttp

from collections import deque

from .scam import roundtrip
from .swaps import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, WSOL, account_keys
from .util import esc, num

JUPITER = "https://lite-api.jup.ag/swap/v1"            # free, no key — Jupiter is phasing it out…
JUPITER_KEYED = "https://api.jup.ag/swap/v1"           # …for this one: free key at developers.jup.ag (or keyless, slower)
FEE_RESERVE_SOL = 0.02
CONFIRM_TIMEOUT = 60
JITO_URL = "https://mainnet.block-engine.jito.wtf/api/v1/transactions"   # private: no public mempool, no sandwich
JITO_WAIT = 20            # not landed through Jito in this long: the same signed swap goes out through the RPC
AUDIT_EVERY = 300         # how often open real positions are checked against the wallet's actual token balance
RETRY_EVERY = 60          # a live coin the paper autopilot already sold is retried at most once a minute…
MAX_RETRIES = 15          # …and after this many failed tries you're told to sell it yourself
TOKEN_PROGRAMS = (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)
PROFIT_DAYS = 7
PROFIT_MIN_TRADES = 15
FILL_DAYS = 30
CALIBRATE_MIN = 10        # real trades before paper's slippage is set from them


def load_keypair():
    """(keypair, problem). Reads TRADING_PRIVATE_KEY (base58, or a [64 numbers] array) or TRADING_KEYPAIR_PATH."""
    secret = os.environ.get("TRADING_PRIVATE_KEY", "").strip()
    path = os.environ.get("TRADING_KEYPAIR_PATH", "").strip()
    if not (secret or path):
        return None, "no TRADING_PRIVATE_KEY in .env"
    try:
        from solders.keypair import Keypair
    except ImportError:
        return None, "live-trading support isn't installed yet — restart the bot (start_mac.command / server/install.sh)"
    try:
        if path:
            with open(os.path.expanduser(path)) as f:
                return Keypair.from_bytes(bytes(json.load(f))), ""
        if secret.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(secret))), ""
        return Keypair.from_base58_string(secret), ""
    except Exception:
        return None, "TRADING_PRIVATE_KEY isn't a valid Solana private key"


def sol_delta(tx: dict | None, wallet: str) -> float | None:
    """SOL the wallet gained (or lost) in a transaction, from its own pre/post balances."""
    if not tx:
        return None
    keys, meta = account_keys(tx), tx.get("meta") or {}
    if wallet not in keys:
        return None
    i = keys.index(wallet)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if i >= len(pre) or i >= len(post):
        return None
    return (int(post[i]) - int(pre[i])) / 1e9


class LiveTrader:
    def __init__(self, cfg, db, rpc, session, notify, keypair=None, key_problem: str = ""):
        self.cfg = cfg
        self.db = db
        self.rpc = rpc
        self.session = session
        self.notify = notify
        if keypair is None and not key_problem:
            keypair, key_problem = load_keypair()
        self.keypair = keypair
        self.key_problem = key_problem
        self.jupiter_key = os.environ.get("JUPITER_API_KEY", "").strip()
        self.jupiter = (os.environ.get("JUPITER_API", "").strip().rstrip("/")
                        or (JUPITER_KEYED if self.jupiter_key else JUPITER))
        self.last_error = ""
        self.set_setting = None   # App.set_setting: lets calibrate() store the measured slippage
        self.hold_until = 0.0     # set while a second copy of the bot is running: no real buys (they'd double)
        self.market = None        # set by the app: SOL price for sizing by pool liquidity
        self.stream = None        # set by the app: no real buys while the live wallet stream is down
        self.fail_streak = 0      # failed transactions in a row (the kill switch)
        self.halted_until = 0.0
        self.halted_why = ""
        self._last_audit = 0
        self.exec = {"sent": 0, "jito": 0, "rpc": 0, "rpc_fallback": 0, "failed": 0, "skipped": 0,
                     "confirm_s": deque(maxlen=50)}   # telemetry: how swaps land
        self._lock = asyncio.Lock()
        self._tasks: set = set()

    # -- state ----------------------------------------------------------------------------------
    @property
    def wallet(self) -> str:
        return str(self.keypair.pubkey()) if self.keypair else ""

    @property
    def dry_run(self) -> bool:
        return self.cfg.flag("LIVE_DRY_RUN")

    def ready(self) -> tuple[bool, str]:
        if not self.cfg.flag("LIVE_TRADING"):
            return False, "live trading is off"
        if not self.keypair:
            return False, self.key_problem
        return True, ""

    def open_trades(self) -> list[dict]:
        return self.db.rows("select * from live_trades where status='open' order by open_ts")

    def today_realized_sol(self) -> float:
        midnight = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        return num(self.db.scalar("""select coalesce(sum(sol_out - sol_in),0) from live_trades
            where status='closed' and close_ts>=? and dry=0""", (midnight,)))

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- Jupiter ------------------------------------------------------------------------------------
    def _headers(self) -> dict:
        return {"accept": "application/json", **({"x-api-key": self.jupiter_key} if self.jupiter_key else {})}

    def _bases(self) -> list[str]:
        # the free lite-api is being phased out: if it stops answering, the same call goes to api.jup.ag
        return [self.jupiter] + ([JUPITER_KEYED] if self.jupiter == JUPITER else [])

    async def _jup(self, verb: str, path: str, **kw):
        problem = None
        for base in self._bases():
            try:
                async with self.session.request(verb, f"{base}{path}", headers=self._headers(),
                                                timeout=aiohttp.ClientTimeout(total=15 if verb == "POST" else 10),
                                                **kw) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 200:
                        return data
                    problem = RuntimeError(f"Jupiter {path} HTTP {resp.status}: {str(data)[:160]}")
                    if resp.status == 400:   # a real "no" about this swap (bad amount, no route): don't retry it
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                problem = RuntimeError(f"Jupiter {path}: {type(exc).__name__}")
        raise problem or RuntimeError(f"Jupiter {path} failed")

    async def _jup_get(self, path: str, params: dict):
        return await self._jup("GET", path, params=params)

    async def _jup_post(self, path: str, body: dict):
        return await self._jup("POST", path, json=body)

    async def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int | None = None) -> dict:
        bps = int(slippage_bps or self.cfg.get("LIVE_SLIPPAGE_BPS"))
        q = await self._jup_get("/quote", {"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount)),
                                           "slippageBps": str(bps)})
        if not isinstance(q, dict) or not q.get("outAmount"):
            raise RuntimeError(f"no route: {str(q)[:160]}")
        return q

    def sign(self, tx_b64: str) -> tuple[str, str]:
        """Sign Jupiter's transaction with the trading key. Returns (signed base64, signature)."""
        from solders.transaction import VersionedTransaction
        raw = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(raw.message, [self.keypair])
        return base64.b64encode(bytes(signed)).decode(), str(signed.signatures[0])

    async def execute(self, quote: dict) -> tuple[str, bool]:
        """Build, sign and (unless dry run) send a swap. Returns (signature, confirmed).

        With LIVE_JITO the swap carries a Jito tip and goes to Jito's block engine: it never sits where sandwich bots
        can see it. If it hasn't landed in JITO_WAIT seconds, the SAME signed transaction is sent through the RPC —
        same signature, so it can never execute twice."""
        jito = self.cfg.flag("LIVE_JITO")
        fee = ({"jitoTipLamports": int(self.cfg.get("LIVE_JITO_TIP_LAMPORTS"))} if jito else
               {"priorityLevelWithMaxLamports": {"maxLamports": int(self.cfg.get("LIVE_PRIORITY_LAMPORTS")),
                                                 "priorityLevel": "veryHigh"}})
        body = {"quoteResponse": quote, "userPublicKey": self.wallet, "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": fee}
        swap = await self._jup_post("/swap", body)
        tx_b64 = (swap or {}).get("swapTransaction")
        if not tx_b64:
            raise RuntimeError(f"no transaction from Jupiter: {str(swap)[:160]}")
        signed_b64, sig = self.sign(tx_b64)
        if self.dry_run:
            return sig, False
        started = time.monotonic()
        self.exec["sent"] += 1
        if jito and await self._send_jito(signed_b64):
            if await self._confirm(sig, JITO_WAIT):
                self.exec["jito"] += 1
                self.exec["confirm_s"].append(time.monotonic() - started)
                return sig, True
            self.exec["rpc_fallback"] += 1
        sent = await self.rpc.call("sendTransaction", [signed_b64, {"encoding": "base64", "skipPreflight": True,
                                                                    "maxRetries": 3}], attempts=1)
        if not sent:
            raise RuntimeError(f"RPC didn't accept the transaction ({self.rpc.last_error})")
        ok = await self._confirm(sig)
        if ok:
            self.exec["rpc"] += 1
            self.exec["confirm_s"].append(time.monotonic() - started)
        return sig, ok

    async def _send_jito(self, signed_b64: str) -> bool:
        """Hand the signed swap to Jito's block engine. False (→ the RPC sends it) on any problem."""
        try:
            async with self.session.post(JITO_URL, json={"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                                                         "params": [signed_b64, {"encoding": "base64"}]},
                                         timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json(content_type=None)
            return resp.status == 200 and isinstance(data, dict) and bool(data.get("result"))
        except Exception:
            return False

    async def _confirm(self, sig: str, timeout: float = CONFIRM_TIMEOUT) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            res = await self.rpc.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}], attempts=1)
            status = ((res or {}).get("value") or [None])[0] if isinstance(res, dict) else None
            if status:
                if status.get("err"):
                    return False
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            await asyncio.sleep(2)
        return False

    # -- trading --------------------------------------------------------------------------------
    def paper_record(self, kind: str, days: int = PROFIT_DAYS) -> dict:
        """The paper autopilot's closed trades for one alert type ('hype' or 'whales') over the last `days`."""
        cond = "whale='hype'" if kind == "hype" else "whale<>'hype'"
        rows = self.db.rows(f"""select size_usd, proceeds_usd from paper_trades where status='closed' and {cond}
            and close_ts>=? and coalesce(interrupted,0)=0""", (int(time.time()) - days * 86400,))
        pnl = [num(r["proceeds_usd"]) - num(r["size_usd"]) for r in rows]
        return {"n": len(pnl), "pnl": sum(pnl), "won": sum(p > 0 for p in pnl)}

    def type_allowed(self, whale: str) -> tuple[bool, str]:
        """LIVE_PROFITABLE_ONLY: real money only follows alert types that are making money on paper right now."""
        if not self.cfg.flag("LIVE_PROFITABLE_ONLY"):
            return True, ""
        kind = "hype" if whale == "hype" else "whales"
        rec = self.paper_record(kind)
        if rec["n"] >= PROFIT_MIN_TRADES and rec["pnl"] <= 0:
            return False, (f"{'hype' if kind == 'hype' else 'whale'} alerts lost ${-rec['pnl']:.2f} on paper over the last "
                           f"{PROFIT_DAYS} days ({rec['n']} trades) — the paper autopilot keeps testing them")
        return True, ""

    # -- kill switch, sizing, slippage -------------------------------------------------------------
    def halt_reason(self, now: float | None = None) -> str:
        """Why new real buys are halted right now ('' = they aren't). Selling and the exit plan never stop."""
        now = now or time.time()
        if now < self.hold_until:
            return "two copies of the bot are running"
        if now < self.halted_until:
            return self.halted_why
        down = getattr(self.rpc, "primary_down", None)
        if callable(down) and down():
            return "every private RPC is down — not trading on the slow public one"
        if self.stream is not None and not getattr(self.stream, "connected", True):
            return "the live wallet stream is reconnecting"
        return ""

    async def _record_tx(self, ok: bool, why: str = "") -> None:
        """Count failed transactions in a row; LIVE_KILL_FAILS of them halt new buys for LIVE_KILL_MINUTES."""
        if ok:
            self.fail_streak = 0
            return
        self.fail_streak += 1
        self.exec["failed"] += 1
        if self.fail_streak >= int(self.cfg.get("LIVE_KILL_FAILS")) and time.time() >= self.halted_until:
            minutes = self.cfg.get("LIVE_KILL_MINUTES")
            self.halted_until = time.time() + minutes * 60
            self.halted_why = f"{self.fail_streak} failed transactions in a row (last: {why[:80]})"
            await self.notify(f"🛑 <b>Live autopilot: new buys halted for {minutes:.0f} min</b>\n{esc(self.halted_why)}.\n"
                              "Open positions are still watched and sold by the exit plan. /live shows the state.",
                              kind="LIVE")

    def loss_streak(self) -> int:
        n = 0
        for t in self.db.rows("""select sol_in, sol_out from live_trades where status='closed' and dry=0
                order by close_ts desc limit 10"""):
            if num(t["sol_out"]) >= num(t["sol_in"]):
                break
            n += 1
        return n

    async def size_for(self, info: dict | None, signal: dict | None) -> tuple[float, str]:
        """SOL for this buy: LIVE_TRADE_SOL, x1.5 on a strong signal (grade A / hype 70+), x0.5 on a weak one (grade C),
        x0.5 after 3 losses in a row, and never more than LIVE_MAX_LIQ_PCT of the pool."""
        base = self.cfg.get("LIVE_TRADE_SOL")
        if not self.cfg.flag("LIVE_DYNAMIC_SIZE"):
            return base, ""
        signal, info = signal or {}, info or {}
        grade, score = signal.get("grade"), signal.get("score")
        mult, why = 1.0, []
        if grade == "A" or num(score) >= 70:
            mult, why = 1.5, ["strong signal ×1.5"]
        elif grade == "C":
            mult, why = 0.5, ["grade C ×0.5"]
        streak = self.loss_streak()
        if streak >= 3:
            mult *= 0.5
            why.append(f"{streak} losses in a row ×0.5")
        size = base * mult
        liq_usd = num(info.get("liquidity_usd"))
        sol_usd = await self.market.sol_usd() if self.market else 0.0
        if liq_usd > 0 and sol_usd > 0:
            cap = liq_usd / sol_usd * self.cfg.get("LIVE_MAX_LIQ_PCT") / 100
            if size > cap:
                size = cap
                why.append(f"capped at {self.cfg.get('LIVE_MAX_LIQ_PCT'):g}% of the pool")
        return round(size, 4), ", ".join(why)

    async def buy_slippage(self, info: dict | None, size_sol: float) -> int:
        """Slippage for a buy: the floor, plus twice the expected price impact (trade vs pool depth), plus how fast the
        coin is moving — capped at LIVE_SLIPPAGE_BPS. Tight where it can be, so a bad fill can't take 15%."""
        info = info or {}
        lo, hi = self.cfg.get("LIVE_MIN_SLIPPAGE_BPS"), self.cfg.get("LIVE_SLIPPAGE_BPS")
        sol_usd = await self.market.sol_usd() if self.market else 0.0
        liq = num(info.get("liquidity_usd"))
        impact_bps = (2 * size_sol * sol_usd / liq * 10_000) if liq > 0 and sol_usd > 0 else hi / 2
        moving_bps = 25 * abs(num(info.get("change_m5")))
        return int(min(hi, max(lo, lo + 2 * impact_bps + moving_bps)))

    async def _skip(self, symbol: str, mint: str, why: str) -> None:
        """A buy the checks refused: protective, not a failure (doesn't count toward the kill switch)."""
        self.exec["skipped"] += 1
        self.last_error = f"{time.strftime('%H:%M:%S')} {symbol}: skipped — {why}"
        await self.notify(f"🛡 <b>Live autopilot skipped ${esc(symbol)}</b>\n{esc(why)}\n<code>{mint}</code>", mint=mint,
                          silent=True, kind="LIVE")

    async def buy(self, paper_id: int, mint: str, symbol: str, whale: str = "", info: dict | None = None,
                  signal: dict | None = None) -> int:
        ok, why = self.ready()
        if not ok:
            return 0
        halted = self.halt_reason()
        if halted:
            hour = time.strftime("%Y-%m-%d %H")
            if self.db.get_meta("live_halt_note") != hour:
                self.db.set_meta("live_halt_note", hour)
                await self.notify(f"⏸ <b>Live autopilot: not buying ${esc(symbol)}</b>\n{esc(halted)}. Paper keeps trading; "
                                  "open real positions are still managed.", silent=True, kind="LIVE")
            return 0
        allowed, why = self.type_allowed(whale)
        if not allowed:
            day = datetime.now().strftime("%Y-%m-%d")
            key = f"live_type_skip:{'hype' if whale == 'hype' else 'whales'}"
            if self.db.get_meta(key) != day:
                self.db.set_meta(key, day)
                await self.notify(f"⏸ <b>Live autopilot skipping these for now</b>\n{esc(why)}. Real buys resume "
                                  "automatically once they're profitable on paper again.", silent=True, kind="LIVE")
            return 0
        async with self._lock:
            if self.db.scalar("select 1 from live_trades where mint=? and status='open'", (mint,)):
                return 0
            if len(self.open_trades()) >= int(self.cfg.get("LIVE_MAX_OPEN")):
                return 0
            limit = self.cfg.get("LIVE_DAILY_LOSS_SOL")
            if limit > 0 and self.today_realized_sol() <= -limit and not self.dry_run:
                if self.db.get_meta("live_loss_stop") != datetime.now().strftime("%Y-%m-%d"):
                    self.db.set_meta("live_loss_stop", datetime.now().strftime("%Y-%m-%d"))
                    await self.notify(f"🛑 <b>Live autopilot paused for today</b>\nRealized loss today reached "
                                      f"{limit:g} SOL (LIVE_DAILY_LOSS_SOL). It starts again tomorrow.", kind="LIVE")
                return 0
            from .hype import seeded_pool
            if info and seeded_pool(info, self.cfg):
                await self._skip(symbol, mint, f"pool only {seeded_pool(info, self.cfg)} min old and seeded like a "
                                               "pull-the-liquidity setup — real money never buys these")
                return 0
            size, size_note = await self.size_for(info, signal)
            if size < 0.25 * self.cfg.get("LIVE_TRADE_SOL"):
                await self._skip(symbol, mint, f"the pool is too thin for a real buy ({size:.4f} SOL would be "
                                               f"{self.cfg.get('LIVE_MAX_LIQ_PCT'):g}% of it)")
                return 0
            sol_before = await self.rpc.sol_balance(self.wallet)
            if not self.dry_run and sol_before is not None and size > 0.25 * (sol_before - FEE_RESERVE_SOL) \
                    and sol_before - FEE_RESERVE_SOL > 0:
                size = round(min(size, max(self.cfg.get("LIVE_TRADE_SOL"), 0.25 * (sol_before - FEE_RESERVE_SOL))), 4)
            if not self.dry_run and (sol_before is None or sol_before < size + FEE_RESERVE_SOL):
                await self._fail(symbol, mint, f"not enough SOL in the trading wallet ({num(sol_before):.3f} SOL)",
                                 count=False)
                return 0
            tokens_before = (await self.rpc.token_balance_raw(self.wallet, mint)) or 0
            lamports = int(size * 1e9)
            slip = await self.buy_slippage(info, size)
            # pre-trade checks, at once: the buy quote and the coin's mint account (Token-2022 traps)
            mint_info = getattr(self.rpc, "mint_info", None)
            q, minted = await asyncio.gather(self.quote(WSOL, mint, lamports, slip),
                                             mint_info(mint) if callable(mint_info) else asyncio.sleep(0),
                                             return_exceptions=True)
            if isinstance(q, Exception):
                await self._fail(symbol, mint, str(q))
                return 0
            if isinstance(minted, dict) and minted.get("traps"):
                await self._skip(symbol, mint, "token trap: " + "; ".join(minted["traps"]))
                return 0
            try:   # honeypot / hidden tax / no liquidity: what would selling straight back return?
                back = await self.quote(mint, WSOL, int(q["outAmount"]))
                rt = roundtrip(lamports, int(back["outAmount"]))
            except Exception as exc:
                await self._skip(symbol, mint, f"can't get a quote to sell it back ({exc}) — possible honeypot")
                return 0
            max_loss = self.cfg.get("LIVE_MAX_ROUNDTRIP_LOSS_PCT")
            if rt < 1 - max_loss / 100:
                await self._skip(symbol, mint, f"selling straight back would return only {rt * 100:.0f}% of the SOL "
                                               f"(more than {max_loss:g}% lost) — honeypot, hidden tax or no liquidity")
                return 0
            plan = (f"{size:g} SOL{f' ({size_note})' if size_note else ''} · slippage {slip / 100:g}% · "
                    f"sell-back check {rt * 100:.0f}%")
            try:
                sig, confirmed = await self.execute(q)
            except Exception as exc:
                await self._fail(symbol, mint, str(exc))
                return 0
            if self.dry_run:
                trade_id = self.db.insert("""insert into live_trades(paper_id,mint,symbol,open_ts,sol_in,tokens_raw,
                        buy_sig,status,dry,size_note) values(?,?,?,?,?,?,?,?,1,?)""",
                                          (paper_id, mint, symbol, int(time.time()), size, int(q["outAmount"]), sig, "open",
                                           plan))
                await self.notify(f"🧪 <b>Dry run: would buy {size:g} SOL of ${esc(symbol)}</b>\nJupiter quote: "
                                  f"{int(q['outAmount']):,} tokens · price impact {num(q.get('priceImpactPct')) * 100:.1f}% · "
                                  f"{esc(plan)} · {'Jito (private)' if self.cfg.flag('LIVE_JITO') else 'RPC'} · "
                                  "signed but not sent. /live dry off to trade for real.\n"
                                  f"<code>{mint}</code>", mint=mint, silent=True, kind="LIVE")
                return trade_id
            tokens_after = (await self.rpc.token_balance_raw(self.wallet, mint)) or 0
            got = tokens_after - tokens_before
            if not confirmed and got <= 0:
                await self._fail(symbol, mint, f"swap not confirmed ({sig[:12]}…)")
                return 0
            await self._record_tx(True)
            sol_after = await self.rpc.sol_balance(self.wallet)
            spent = (sol_before - sol_after) if sol_before is not None and sol_after is not None else size
            trade_id = self.db.insert("""insert into live_trades(paper_id,mint,symbol,open_ts,sol_in,tokens_raw,buy_sig,
                    status,dry,size_note) values(?,?,?,?,?,?,?,?,0,?)""",
                                      (paper_id, mint, symbol, int(time.time()), spent, got, sig, "open", plan))
            await self.notify(f"🟢 <b>Live: bought ${esc(symbol)} for {spent:.3f} SOL</b>\n{esc(plan)}\n"
                              f"<a href=\"https://solscan.io/tx/{sig}\">transaction</a> · the exit plan runs automatically"
                              f"\n<code>{mint}</code>", buttons=[[("🔴 Sell now", None, f"livesell:{mint}")]],
                              mint=mint, kind="LIVE")
            return trade_id

    async def sell(self, mint: str, share: float, reason: str) -> bool:
        trade = self.db.row("select * from live_trades where mint=? and status='open'", (mint,))
        if not trade or not self.keypair:
            return False
        async with self._lock:
            if trade["dry"]:
                amount = int(trade["tokens_raw"] * share)
            else:
                held = await self.rpc.token_balance_raw(self.wallet, mint)
                if held is None:
                    await self._fail(trade["symbol"], mint, "couldn't read the token balance")
                    return False
                amount = held if share >= 0.999 else int(held * share)
            closing = share >= 0.999 or amount <= 0
            if amount > 0:
                sol_before = await self.rpc.sol_balance(self.wallet) if not trade["dry"] else 0.0
                try:
                    q = await self.quote(mint, WSOL, amount)
                    sig, confirmed = await self.execute(q)
                except Exception as exc:
                    await self._fail(trade["symbol"], mint, f"sell failed: {exc}")
                    return False
                if trade["dry"]:
                    got = int(q["outAmount"]) / 1e9
                else:
                    if not confirmed:
                        # it may still land: remember it, so the catch-up checks it before selling again
                        self.db.run("update live_trades set pending_sig=? where id=?", (f"{sig}|{int(time.time())}",
                                                                                       trade["id"]))
                        await self._fail(trade["symbol"], mint, f"sell not confirmed ({sig[:12]}…) — will retry")
                        return False
                    await self._record_tx(True)
                    sol_after = await self.rpc.sol_balance(self.wallet)
                    got = (sol_after - sol_before) if sol_after is not None and sol_before is not None else int(q["outAmount"]) / 1e9
            else:
                sig, got = "", 0.0
            rent = 0.0
            if closing and not trade["dry"]:
                rent = await self.close_empty_accounts(mint)   # get the ~0.002 SOL token-account rent back
            self.db.run("""update live_trades set sol_out=sol_out+?, tokens_raw=tokens_raw-?, status=?, close_ts=?,
                    close_reason=?, sell_sigs=trim(coalesce(sell_sigs,'')||' '||?), rent_back=rent_back+? where id=?""",
                        (got + rent, amount, "closed" if closing else "open", int(time.time()) if closing else None,
                         reason if closing else trade["close_reason"], sig, rent, trade["id"]))
            t = self.db.row("select * from live_trades where id=?", (trade["id"],))
            head = "🧪 Dry run: would sell" if trade["dry"] else "🔴 Live: sold"
            line = f"<b>{head} {'all' if closing else f'{share * 100:.0f}%'} of ${esc(trade['symbol'])}</b>\n{esc(reason)} · {got:.3f} SOL"
            if closing:
                line += f" · trade {t['sol_out'] - t['sol_in']:+.3f} SOL"
            if sig and not trade["dry"]:
                line += f' · <a href="https://solscan.io/tx/{sig}">transaction</a>'
            await self.notify(line + f"\n<code>{mint}</code>", mint=mint, silent=bool(trade["dry"]), kind="LIVE")
            return True

    async def sell_token(self, mint: str, share: float, symbol: str = "", reason: str = "you sold it") -> tuple[bool, str]:
        """Sell `share` of whatever the trading wallet holds of `mint` — autopilot trade or not (dashboard / /sellnow)."""
        if not self.keypair:
            return False, f"selling needs the trading wallet: {self.key_problem}"
        share = min(max(share, 0.01), 1.0)
        if self.db.scalar("select 1 from live_trades where mint=? and status='open'", (mint,)):
            ok = await self.sell(mint, share, reason)
            return ok, "sold" if ok else (self.last_error or "sell failed")
        async with self._lock:
            held = await self.rpc.token_balance_raw(self.wallet, mint)
            if not held:
                return False, ("the trading wallet doesn't hold this coin — if it's in your Fomo wallet, sell it in Fomo"
                               if held == 0 else "couldn't read the trading wallet's balance")
            amount = held if share >= 0.999 else int(held * share)
            sol_before = await self.rpc.sol_balance(self.wallet)
            try:
                q = await self.quote(mint, WSOL, amount)
                sig, confirmed = await self.execute(q)
            except Exception as exc:
                await self._fail(symbol or mint[:4], mint, f"sell failed: {exc}")
                return False, str(exc)
            if self.dry_run:
                return True, f"dry run: would get {int(q['outAmount']) / 1e9:.3f} SOL (nothing sent)"
            if not confirmed:
                await self._fail(symbol or mint[:4], mint, f"sell not confirmed ({sig[:12]}…)")
                return False, "not confirmed — check Solscan and try again"
            sol_after = await self.rpc.sol_balance(self.wallet)
            got = (sol_after - sol_before) if sol_after is not None and sol_before is not None else int(q["outAmount"]) / 1e9
            await self.notify(f"<b>🔴 Sold {share * 100:.0f}% of ${esc(symbol or mint[:4])}</b>\n{esc(reason)} · {got:.3f} SOL · "
                              f'<a href="https://solscan.io/tx/{sig}">transaction</a>\n<code>{mint}</code>', mint=mint, kind="LIVE")
            return True, f"sold for {got:.3f} SOL"

    async def close_empty_accounts(self, mint: str) -> float:
        """Close the wallet's now-empty token account(s) for `mint` and get the rent back (~0.002 SOL each). Every buy
        of a new coin opens one; left open, that's ~4% of a 0.05 SOL trade lost on every coin."""
        try:
            res = await self.rpc.call("getTokenAccountsByOwner", [self.wallet, {"mint": mint}, {"encoding": "jsonParsed"}])
            empty = []
            for item in (res or {}).get("value") or []:
                acct = item.get("account") or {}
                info = (((acct.get("data") or {}).get("parsed") or {}).get("info") or {})
                if int((info.get("tokenAmount") or {}).get("amount") or 0) == 0 and acct.get("owner") in TOKEN_PROGRAMS:
                    empty.append((item["pubkey"], acct["owner"], int(acct.get("lamports") or 0)))
            if not empty:
                return 0.0
            bh = await self.rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])
            blockhash = ((bh or {}).get("value") or {}).get("blockhash")
            if not blockhash:
                return 0.0
            tx_b64 = self.close_tx(empty, blockhash)
            sent = await self.rpc.call("sendTransaction", [tx_b64, {"encoding": "base64", "maxRetries": 3}], attempts=1)
            return sum(lamports for _, _, lamports in empty) / 1e9 if sent else 0.0
        except Exception as exc:   # never let rent recovery break a sell
            self.last_error = f"{time.strftime('%H:%M:%S')} rent recovery: {type(exc).__name__}: {exc}"
            return 0.0

    def close_tx(self, accounts: list[tuple[str, str, int]], blockhash: str) -> str:
        """A signed transaction closing token accounts (SPL Token CloseAccount = instruction 9), rent to the wallet."""
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.transaction import VersionedTransaction
        owner = self.keypair.pubkey()
        ixs = [Instruction(Pubkey.from_string(program), bytes([9]),
                           [AccountMeta(Pubkey.from_string(acct), is_signer=False, is_writable=True),
                            AccountMeta(owner, is_signer=False, is_writable=True),
                            AccountMeta(owner, is_signer=True, is_writable=False)])
               for acct, program, _ in accounts]
        msg = MessageV0.try_compile(owner, ixs, [], Hash.from_string(blockhash))
        return base64.b64encode(bytes(VersionedTransaction(msg, [self.keypair]))).decode()

    async def _landed(self, sig: str) -> str:
        """'landed', 'failed' or '' (unknown yet) for a transaction signature."""
        res = await self.rpc.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}], attempts=1)
        status = ((res or {}).get("value") or [None])[0] if isinstance(res, dict) else None
        if not status:
            return ""
        if status.get("err"):
            return "failed"
        return "landed" if status.get("confirmationStatus") in ("confirmed", "finalized") else ""

    async def _settle_pending(self, t: dict, now: int) -> bool:
        """A sell that didn't confirm in time: did it land after all? Book what it really got (from the transaction's
        own balances) instead of selling again or losing track. True when the trade is settled."""
        sig, _, since = str(t["pending_sig"]).partition("|")
        state = await self._landed(sig)
        if state == "landed":
            tx = await self.rpc.transaction(sig) if hasattr(self.rpc, "transaction") else None
            got = max(0.0, sol_delta(tx, self.wallet) or 0.0)
            held = await self.rpc.token_balance_raw(self.wallet, t["mint"])
            closing = held == 0
            self.db.run("""update live_trades set sol_out=sol_out+?, pending_sig='', status=?, close_ts=?, close_reason=?,
                    sell_sigs=trim(coalesce(sell_sigs,'')||' '||?) where id=?""",
                        (got, "closed" if closing else "open", now if closing else None,
                         "the sell that didn't confirm in time landed" if closing else t["close_reason"], sig, t["id"]))
            await self._record_tx(True)
            if closing:
                rec = self.db.row("select * from live_trades where id=?", (t["id"],))
                await self.notify(f"✅ <b>Live: the earlier sell of ${esc(t['symbol'])} did land</b>\n{got:.3f} SOL · trade "
                                  f"{rec['sol_out'] - rec['sol_in']:+.3f} SOL\n<code>{t['mint']}</code>", mint=t["mint"],
                                  silent=True, kind="LIVE")
            return closing
        if state == "failed" or now - int(num(since)) > 120:   # failed, or expired (blockhash) without landing
            self.db.run("update live_trades set pending_sig='' where id=?", (t["id"],))
        return False

    async def audit_balances(self, now: int | None = None) -> list[str]:
        """Every 5 minutes: does the wallet still hold each open real position? A position with no tokens left (sold in
        another app, or by hand) is closed here instead of being 'open' forever."""
        now = int(now or time.time())
        if now - self._last_audit < AUDIT_EVERY or not self.keypair:
            return []
        self._last_audit = now
        closed = []
        for t in self.open_trades():
            if t["dry"] or t["pending_sig"] or now - int(t["open_ts"]) < 60:
                continue
            held = await self.rpc.token_balance_raw(self.wallet, t["mint"])
            if held == 0:
                self.db.run("""update live_trades set status='closed', close_ts=?, close_reason=? where id=?""",
                            (now, "no tokens left in the trading wallet (sold outside the bot?)", t["id"]))
                closed.append(t["mint"])
                await self.notify(f"🔎 <b>Live: ${esc(t['symbol'])} is no longer in the trading wallet</b>\nClosed it here "
                                  "(sold in another app or by hand?) — what it sold for isn't known to the bot.\n"
                                  f"<code>{t['mint']}</code>", mint=t["mint"], silent=True, kind="LIVE")
        return closed

    async def reconcile(self, now: int | None = None) -> list[str]:
        """Catch-up selling: a live coin whose paper trade already closed (a sell that didn't confirm, no route for a
        moment) is sold again, at most once a minute — so nothing sits in your wallet unwatched overnight.
        A sell that didn't confirm in time is checked first: if it landed late, it's booked, not sold twice."""
        now = int(now or time.time())
        done = []
        for t in self.open_trades():
            if t.get("pending_sig") and not t["dry"]:
                if await self._settle_pending(t, now):
                    done.append(t["mint"])
                continue
            paper = self.db.row("select status, close_reason from paper_trades where id=?", (t["paper_id"],))
            if paper and paper["status"] != "closed":
                continue
            if int(t["retries"] or 0) >= MAX_RETRIES or now - int(t["retry_ts"] or 0) < RETRY_EVERY:
                continue
            self.db.run("update live_trades set retries=retries+1, retry_ts=? where id=?", (now, t["id"]))
            reason = f"catching up: the plan already sold it ({(paper or {}).get('close_reason') or 'paper trade closed'})"
            if await self.sell(t["mint"], 1.0, reason):
                done.append(t["mint"])
            elif int(t["retries"] or 0) + 1 >= MAX_RETRIES:
                await self.notify(f"🆘 <b>Couldn't sell ${esc(t['symbol'])} after {MAX_RETRIES} tries</b>\n"
                                  "Probably no liquidity left (rugged) or no route. Check it and sell it yourself — "
                                  "/sellnow or the dashboard.\n"
                                  f"<code>{t['mint']}</code>", mint=t["mint"], kind="LIVE")
        await self.audit_balances(now)
        return done

    async def sell_all(self, reason: str = "you sold everything (/sellall)") -> int:
        n = 0
        for t in self.open_trades():
            n += bool(await self.sell(t["mint"], 1.0, reason))
        return n

    async def _fail(self, symbol: str, mint: str, why: str, count: bool = True) -> None:
        self.last_error = f"{time.strftime('%H:%M:%S')} {symbol}: {why}"
        # only transactions that failed count toward the kill switch — "no route" for a rugged coin is the coin's
        # problem, not the bot's, and its catch-up retries mustn't keep halting new buys
        if count and "no route" not in why and "/quote" not in why:
            await self._record_tx(False, why)
        await self.notify(f"⚠️ <b>Live autopilot: ${esc(symbol)}</b>\n{esc(why)}\n<code>{mint}</code>", mint=mint,
                          silent=True, kind="LIVE")

    # -- real fills vs paper ------------------------------------------------------------------------
    def fills(self, days: int = FILL_DAYS) -> dict:
        """Real trades next to the paper trades they copied: what real swaps cost on top of what paper assumes.
        paper_x / live_x are the multiples of the money put in (fees, slippage, priority fees and rent all included)."""
        rows = self.db.rows("""select l.id, l.symbol, l.mint, l.open_ts, l.sol_in, l.sol_out, p.open_ts as paper_ts,
                p.size_usd, p.proceeds_usd from live_trades l join paper_trades p on p.id=l.paper_id
            where l.status='closed' and l.dry=0 and p.status='closed' and l.sol_in>0 and p.size_usd>0 and l.close_ts>=?
            order by l.close_ts desc""", (int(time.time()) - days * 86400,))
        slip = self.cfg.get("PAPER_SLIPPAGE_PCT") / 100
        ratios = []
        for r in rows:
            r["live_x"], r["paper_x"] = r["sol_out"] / r["sol_in"], num(r["proceeds_usd"]) / r["size_usd"]
            r["delay_s"] = max(0, int(r["open_ts"]) - int(r["paper_ts"]))
            market_x = r["paper_x"] * (1 + slip) / (1 - slip)      # the price move itself, before paper's costs
            if market_x > 0:
                ratios.append(r["live_x"] / market_x)
        out = {"n": len(rows), "rows": rows[:10], "paper_slip_pct": slip * 100}
        if rows:
            out.update(live_avg=sum(r["live_x"] for r in rows) / len(rows),
                       paper_avg=sum(r["paper_x"] for r in rows) / len(rows),
                       delay_s=sorted(r["delay_s"] for r in rows)[len(rows) // 2])
            out["gap"] = out["live_avg"] - out["paper_avg"]
        if ratios:
            k = sorted(ratios)[len(ratios) // 2]                      # median: one rug doesn't swing it
            out["real_slip_pct"] = max(0.0, (1 - k) / (1 + k) * 100)  # cost per side that explains the real fills
        return out

    async def calibrate(self, now: int | None = None) -> str:
        """Once a day: with 10+ real trades, set the paper autopilot's slippage to what real swaps actually cost, so
        paper results (and the self-tuner, which replays with it) predict real money. A value you set yourself is
        left alone for a week."""
        now = int(now or time.time())
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if self.db.get_meta("live_calibrated") == today or not self.set_setting:
            return ""
        self.db.set_meta("live_calibrated", today)
        f = self.fills()
        if f["n"] < CALIBRATE_MIN or "real_slip_pct" not in f:
            return ""
        if now - int(num(self.db.get_meta("tune:manual:PAPER_SLIPPAGE_PCT", "0"))) < 7 * 86400:
            return ""
        new = round(min(15.0, max(1.0, f["real_slip_pct"])), 1)
        if abs(new - f["paper_slip_pct"]) < 1:
            return ""
        self.set_setting("PAPER_SLIPPAGE_PCT", new)
        text = (f"📏 <b>Paper autopilot calibrated to your real fills</b>\nOn your last {f['n']} real trades, swaps cost about "
                f"{f['real_slip_pct']:.1f}% each way (paper assumed {f['paper_slip_pct']:g}%): real {f['live_avg']:.2f}x vs paper "
                f"{f['paper_avg']:.2f}x a trade. PAPER_SLIPPAGE_PCT → {new:g}, so paper results match what real money gets.")
        await self.notify(text, silent=True, kind="LIVE")
        return text

    def summary(self) -> dict:
        trades = self.db.rows("select * from live_trades where dry=? order by open_ts desc", (int(self.dry_run),))
        closed = [t for t in trades if t["status"] == "closed"]
        pnl = [t["sol_out"] - t["sol_in"] for t in closed]
        return {"enabled": self.cfg.flag("LIVE_TRADING"), "dry_run": self.dry_run, "wallet": self.wallet,
                "problem": self.ready()[1], "open": [t for t in trades if t["status"] == "open"],
                "closed": len(closed), "won": sum(p > 0 for p in pnl), "realized_sol": sum(pnl),
                "today_sol": self.today_realized_sol(), "recent": self._with_paper(trades[:20]), "error": self.last_error,
                "fills": {k: v for k, v in self.fills().items() if k != "rows"}, "halt": self.halt_reason(),
                "exec": self.exec_stats()}

    def exec_stats(self) -> dict:
        times = sorted(self.exec["confirm_s"])
        return {**{k: v for k, v in self.exec.items() if k != "confirm_s"}, "fail_streak": self.fail_streak,
                "median_confirm_s": round(times[len(times) // 2], 1) if times else None,
                "jito_on": self.cfg.flag("LIVE_JITO")}

    def _with_paper(self, trades: list[dict]) -> list[dict]:
        for t in trades:
            p = self.db.row("select status, size_usd, proceeds_usd from paper_trades where id=?", (t["paper_id"],))
            t["paper_x"] = (num(p["proceeds_usd"]) / num(p["size_usd"]) if p and p["status"] == "closed"
                            and num(p["size_usd"]) > 0 else None)
        return trades
