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

from .swaps import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, WSOL
from .util import esc, num

JUPITER = "https://lite-api.jup.ag/swap/v1"
FEE_RESERVE_SOL = 0.02
CONFIRM_TIMEOUT = 60
RETRY_EVERY = 60          # a live coin the paper autopilot already sold is retried at most once a minute…
MAX_RETRIES = 15          # …and after this many failed tries you're told to sell it yourself
TOKEN_PROGRAMS = (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)
PROFIT_DAYS = 7
PROFIT_MIN_TRADES = 15


def load_keypair():
    """(keypair, problem). Reads TRADING_PRIVATE_KEY (base58, or a [64 numbers] array) or TRADING_KEYPAIR_PATH."""
    secret = os.environ.get("TRADING_PRIVATE_KEY", "").strip()
    path = os.environ.get("TRADING_KEYPAIR_PATH", "").strip()
    if not (secret or path):
        return None, "no TRADING_PRIVATE_KEY in .env"
    try:
        from solders.keypair import Keypair
    except ImportError:
        return None, "live-trading support isn't installed yet — restart with start_mac.command"
    try:
        if path:
            with open(os.path.expanduser(path)) as f:
                return Keypair.from_bytes(bytes(json.load(f))), ""
        if secret.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(secret))), ""
        return Keypair.from_base58_string(secret), ""
    except Exception:
        return None, "TRADING_PRIVATE_KEY isn't a valid Solana private key"


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
        self.jupiter = os.environ.get("JUPITER_API", "").strip().rstrip("/") or JUPITER
        self.jupiter_key = os.environ.get("JUPITER_API_KEY", "").strip()
        self.last_error = ""
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

    async def _jup_get(self, path: str, params: dict):
        async with self.session.get(f"{self.jupiter}{path}", params=params, headers=self._headers(),
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(f"Jupiter {path} HTTP {resp.status}: {str(data)[:160]}")
            return data

    async def _jup_post(self, path: str, body: dict):
        async with self.session.post(f"{self.jupiter}{path}", json=body, headers=self._headers(),
                                     timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(f"Jupiter {path} HTTP {resp.status}: {str(data)[:160]}")
            return data

    async def quote(self, input_mint: str, output_mint: str, amount: int) -> dict:
        q = await self._jup_get("/quote", {"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount)),
                                           "slippageBps": str(int(self.cfg.get("LIVE_SLIPPAGE_BPS")))})
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
        """Build, sign and (unless dry run) send a swap. Returns (signature, confirmed)."""
        body = {"quoteResponse": quote, "userPublicKey": self.wallet, "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                    "maxLamports": int(self.cfg.get("LIVE_PRIORITY_LAMPORTS")), "priorityLevel": "veryHigh"}}}
        swap = await self._jup_post("/swap", body)
        tx_b64 = (swap or {}).get("swapTransaction")
        if not tx_b64:
            raise RuntimeError(f"no transaction from Jupiter: {str(swap)[:160]}")
        signed_b64, sig = self.sign(tx_b64)
        if self.dry_run:
            return sig, False
        sent = await self.rpc.call("sendTransaction", [signed_b64, {"encoding": "base64", "skipPreflight": True,
                                                                    "maxRetries": 3}], attempts=1)
        if not sent:
            raise RuntimeError(f"RPC didn't accept the transaction ({self.rpc.last_error})")
        return sig, await self._confirm(sig)

    async def _confirm(self, sig: str) -> bool:
        deadline = time.monotonic() + CONFIRM_TIMEOUT
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
            and close_ts>=?""", (int(time.time()) - days * 86400,))
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

    async def buy(self, paper_id: int, mint: str, symbol: str, whale: str = "") -> int:
        ok, why = self.ready()
        if not ok:
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
            size = self.cfg.get("LIVE_TRADE_SOL")
            sol_before = await self.rpc.sol_balance(self.wallet)
            if not self.dry_run and (sol_before is None or sol_before < size + FEE_RESERVE_SOL):
                await self._fail(symbol, mint, f"not enough SOL in the trading wallet ({num(sol_before):.3f} SOL)")
                return 0
            tokens_before = (await self.rpc.token_balance_raw(self.wallet, mint)) or 0
            try:
                q = await self.quote(WSOL, mint, int(size * 1e9))
                sig, confirmed = await self.execute(q)
            except Exception as exc:
                await self._fail(symbol, mint, str(exc))
                return 0
            if self.dry_run:
                trade_id = self.db.insert("""insert into live_trades(paper_id,mint,symbol,open_ts,sol_in,tokens_raw,
                        buy_sig,status,dry) values(?,?,?,?,?,?,?,?,1)""",
                                          (paper_id, mint, symbol, int(time.time()), size, int(q["outAmount"]), sig, "open"))
                await self.notify(f"🧪 <b>Dry run: would buy {size:g} SOL of ${esc(symbol)}</b>\nJupiter quote: "
                                  f"{int(q['outAmount']):,} tokens · price impact {num(q.get('priceImpactPct')) * 100:.1f}% · "
                                  "signed but not sent. /live dry off to trade for real.\n"
                                  f"<code>{mint}</code>", mint=mint, silent=True, kind="LIVE")
                return trade_id
            tokens_after = (await self.rpc.token_balance_raw(self.wallet, mint)) or 0
            got = tokens_after - tokens_before
            if not confirmed and got <= 0:
                await self._fail(symbol, mint, f"swap not confirmed ({sig[:12]}…)")
                return 0
            sol_after = await self.rpc.sol_balance(self.wallet)
            spent = (sol_before - sol_after) if sol_before is not None and sol_after is not None else size
            trade_id = self.db.insert("""insert into live_trades(paper_id,mint,symbol,open_ts,sol_in,tokens_raw,buy_sig,
                    status,dry) values(?,?,?,?,?,?,?,?,0)""", (paper_id, mint, symbol, int(time.time()), spent, got, sig, "open"))
            await self.notify(f"🟢 <b>Live: bought ${esc(symbol)} for {spent:.3f} SOL</b>\n"
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
                        await self._fail(trade["symbol"], mint, f"sell not confirmed ({sig[:12]}…) — will retry")
                        return False
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

    async def reconcile(self, now: int | None = None) -> list[str]:
        """Catch-up selling: a live coin whose paper trade already closed (a sell that didn't confirm, no route for a
        moment) is sold again, at most once a minute — so nothing sits in your wallet unwatched overnight."""
        now = int(now or time.time())
        done = []
        for t in self.open_trades():
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
        return done

    async def sell_all(self, reason: str = "you sold everything (/sellall)") -> int:
        n = 0
        for t in self.open_trades():
            n += bool(await self.sell(t["mint"], 1.0, reason))
        return n

    async def _fail(self, symbol: str, mint: str, why: str) -> None:
        self.last_error = f"{time.strftime('%H:%M:%S')} {symbol}: {why}"
        await self.notify(f"⚠️ <b>Live autopilot: ${esc(symbol)}</b>\n{esc(why)}\n<code>{mint}</code>", mint=mint,
                          silent=True, kind="LIVE")

    def summary(self) -> dict:
        trades = self.db.rows("select * from live_trades where dry=? order by open_ts desc", (int(self.dry_run),))
        closed = [t for t in trades if t["status"] == "closed"]
        pnl = [t["sol_out"] - t["sol_in"] for t in closed]
        return {"enabled": self.cfg.flag("LIVE_TRADING"), "dry_run": self.dry_run, "wallet": self.wallet,
                "problem": self.ready()[1], "open": [t for t in trades if t["status"] == "open"],
                "closed": len(closed), "won": sum(p > 0 for p in pnl), "realized_sol": sum(pnl),
                "today_sol": self.today_realized_sol(), "recent": trades[:20], "error": self.last_error}
