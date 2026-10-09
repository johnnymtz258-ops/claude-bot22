"""Live autopilot: mirrors the paper autopilot with real (or dry-run) Jupiter swaps, inside hard limits."""
import asyncio
import base64

import pytest

from fomo.live import LiveTrader, load_keypair
from fomo.swaps import WSOL
from tests.helpers import MINT, WHALE, pump_buy

solders = pytest.importorskip("solders")
from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0, to_bytes_versioned  # noqa: E402
from solders.null_signer import NullSigner  # noqa: E402
from solders.system_program import TransferParams, transfer  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

P0 = 0.000075


def unsigned_tx(kp) -> tuple[str, MessageV0]:
    """What Jupiter returns: a v0 transaction for our wallet, not signed yet."""
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1))
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    tx = VersionedTransaction(msg, [NullSigner(kp.pubkey())])
    return base64.b64encode(bytes(tx)).decode(), msg


class ChainStub:
    """RPC for the trading wallet: SOL and token balances move when a swap is 'confirmed'."""

    def __init__(self, sol=1.0):
        self.sol, self.tokens, self.sent, self.pending = sol, 0, [], None
        self.last_error = ""
        self.accounts = []   # token accounts getTokenAccountsByOwner returns (for rent recovery)

    async def sol_balance(self, owner):
        return self.sol

    async def token_balance_raw(self, owner, mint):
        return self.tokens

    async def call(self, method, params, **kw):
        if method == "sendTransaction":
            self.sent.append(params[0])
            if self.pending:   # a swap; a close-account transaction has no pending swap
                self.sol, self.tokens = self.pending(self.sol, self.tokens)
                self.pending = None
            return "sig"
        if method == "getTokenAccountsByOwner":
            return {"value": self.accounts}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.default())}}
        if method == "getSignatureStatuses":
            return {"value": [{"confirmationStatus": "confirmed", "err": None}]}
        return None


def live_bot(bot, dry=True, sol=1.0):
    kp = Keypair()
    chain = ChainStub(sol)
    live = LiveTrader(bot.cfg, bot.db, chain, None, bot.notes, keypair=kp)
    tx_b64, msg = unsigned_tx(kp)
    quotes = []

    async def jup_get(path, params):
        quotes.append(params)
        amount = int(params["amount"])
        if params["inputMint"] == WSOL:   # buying: 1 SOL buys 1e12 raw tokens
            chain.pending = lambda s, t: (s - amount / 1e9 - 0.001, t + amount * 1000)
            return {"outAmount": str(amount * 1000), "priceImpactPct": "0.02"}
        chain.pending = lambda s, t: (s + amount / 1000 / 1e9 * 2.0, t - amount)   # selling at 2x
        return {"outAmount": str(int(amount / 1000 * 2.0)), "priceImpactPct": "0.01"}

    async def jup_post(path, body):
        assert body["userPublicKey"] == str(kp.pubkey()) and body["wrapAndUnwrapSol"]
        return {"swapTransaction": tx_b64}

    live._jup_get, live._jup_post = jup_get, jup_post
    bot.engine.paper.live = live
    bot.cfg.set("LIVE_TRADING", "on")
    bot.cfg.set("LIVE_DRY_RUN", "on" if dry else "off")
    bot.cfg.set("PAPER_SLIPPAGE_PCT", "0")
    return live, chain, quotes, kp, msg


def alert(bot):
    bot.whales.add(WHALE, "Holder")
    bot.market.set_pair(mint=MINT, price=P0, mc=75_000)
    bot.feed(WHALE, pump_buy())
    bot.run(asyncio.sleep(0.05))   # let the live buy task run


def test_signing_uses_the_trading_key(bot):
    live, chain, quotes, kp, msg = live_bot(bot)
    signed_b64, sig = live.sign(unsigned_tx(kp)[0])
    signed = VersionedTransaction.from_bytes(base64.b64decode(signed_b64))
    assert signed.signatures[0] == kp.sign_message(to_bytes_versioned(signed.message))
    assert sig == str(signed.signatures[0])


def test_dry_run_quotes_and_signs_but_sends_nothing(bot):
    live, chain, quotes, kp, msg = live_bot(bot, dry=True)
    alert(bot)
    assert chain.sent == []
    t = bot.db.row("select * from live_trades")
    assert t["dry"] == 1 and t["status"] == "open" and t["sol_in"] == 0.05
    assert quotes[0]["inputMint"] == WSOL and quotes[0]["amount"] == str(int(0.05 * 1e9))
    assert any(m["kind"] == "LIVE" and "Dry run: would buy 0.05 SOL" in m["text"] for m in bot.notes.sent)


def test_real_trades_follow_the_paper_exit_plan(bot):
    live, chain, quotes, kp, msg = live_bot(bot, dry=False)
    alert(bot)
    t = bot.db.row("select * from live_trades")
    assert len(chain.sent) == 1 and t["dry"] == 0 and t["tokens_raw"] == 50_000_000_000
    assert t["sol_in"] == pytest.approx(0.051)                       # measured from the wallet: swap + fees
    assert any("Live: bought $CASHED for 0.051 SOL" in m["text"] for m in bot.notes.sent)
    # paper hits 2x -> sells half -> live sells half of what it holds
    bot.market.set_pair(mint=MINT, price=P0 * 2.1, mc=157_500)
    bot.run(bot.tracker.tick(int(__import__("time").time())))
    bot.run(asyncio.sleep(0.05))
    assert quotes[-1]["inputMint"] == MINT and quotes[-1]["amount"] == str(25_000_000_000)
    t = bot.db.row("select * from live_trades")
    assert t["status"] == "open" and t["sol_out"] == pytest.approx(0.05)
    bot.run(live.sell_all())
    t = bot.db.row("select * from live_trades")
    assert t["status"] == "closed" and t["sol_out"] == pytest.approx(0.1) and chain.tokens == 0


def test_limits_not_enough_sol_and_daily_loss(bot):
    live, chain, quotes, kp, msg = live_bot(bot, dry=False, sol=0.03)
    alert(bot)
    assert chain.sent == [] and bot.db.scalar("select count(*) from live_trades") == 0
    assert any("not enough SOL" in m["text"] for m in bot.notes.sent)
    bot.db.run("""insert into live_trades(mint,symbol,open_ts,sol_in,sol_out,status,close_ts,dry)
        values('X','X',0,0.5,0.1,'closed',strftime('%s','now'),0)""")
    chain.sol = 5.0
    assert bot.run(live.buy(0, "Other" + "1" * 39, "OTHER")) == 0
    assert any("paused for today" in m["text"] for m in bot.notes.sent)


def test_off_by_default_and_never_without_a_key(bot, monkeypatch):
    monkeypatch.delenv("TRADING_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("TRADING_KEYPAIR_PATH", raising=False)
    assert bot.cfg.flag("LIVE_TRADING") is False
    live = LiveTrader(bot.cfg, bot.db, ChainStub(), None, bot.notes)
    assert live.keypair is None and live.ready() == (False, "live trading is off")
    bot.cfg.set("LIVE_TRADING", "on")
    assert live.ready() == (False, "no TRADING_PRIVATE_KEY in .env")


def test_key_formats(monkeypatch):
    kp = Keypair()
    monkeypatch.setenv("TRADING_PRIVATE_KEY", str(kp))
    assert load_keypair()[0].pubkey() == kp.pubkey()
    monkeypatch.setenv("TRADING_PRIVATE_KEY", str(list(bytes(kp))))
    assert load_keypair()[0].pubkey() == kp.pubkey()
    monkeypatch.setenv("TRADING_PRIVATE_KEY", "not-a-key")
    assert load_keypair() == (None, "TRADING_PRIVATE_KEY isn't a valid Solana private key")


def test_live_command(chat, monkeypatch):
    from fomo.live import LiveTrader as LT
    monkeypatch.delenv("TRADING_PRIVATE_KEY", raising=False)
    chat.live = LT(chat.cfg, chat.db, ChainStub(), None, chat.notify)
    assert "OFF" in chat.say("/live")
    assert "can't turn on" in chat.say("/live on")
    assert chat.cfg.flag("LIVE_TRADING") is False


def test_sell_any_coin_the_trading_wallet_holds(bot):
    live, chain, quotes, kp, msg = live_bot(bot, dry=False)
    coin = "Held" + "1" * 36 + "pump"
    assert bot.run(live.sell_token(coin, 0.5, "HELD"))[1].startswith("the trading wallet doesn't hold this coin")
    chain.tokens = 1_000_000
    ok, message = bot.run(live.sell_token(coin, 0.5, "HELD", "you sold from the dashboard"))
    assert ok and message.startswith("sold for") and quotes[-1]["amount"] == "500000" and chain.tokens == 500_000
    assert any("Sold 50% of $HELD" in m["text"] for m in bot.notes.sent)


def test_paper_trade_can_be_closed_by_hand_and_live_follows(bot):
    live, chain, quotes, kp, msg = live_bot(bot, dry=False)
    alert(bot)
    t = bot.db.row("select * from paper_trades")
    trade = bot.run(bot.engine.paper.close(t["id"]))
    bot.run(asyncio.sleep(0.05))
    assert trade["status"] == "closed" and trade["close_reason"] == "closed by you"
    assert bot.db.row("select status from live_trades")["status"] == "closed"
    assert bot.run(bot.engine.paper.close(t["id"])) is None


EMPTY_ATA = "EmptyAta111111111111111111111111111111111111"[:44]


def test_close_account_transaction_is_signed_and_correct(bot):
    live, chain, quotes, kp, msg = live_bot(bot, dry=False)
    from fomo.swaps import TOKEN_PROGRAM
    from solders.message import to_bytes_versioned
    tx = VersionedTransaction.from_bytes(base64.b64decode(live.close_tx([(EMPTY_ATA, TOKEN_PROGRAM, 2039280)],
                                                                       str(Hash.default()))))
    keys = [str(k) for k in tx.message.account_keys]
    ix = tx.message.instructions[0]
    assert keys[ix.program_id_index] == TOKEN_PROGRAM and bytes(ix.data) == bytes([9])          # CloseAccount
    assert [keys[i] for i in ix.accounts] == [EMPTY_ATA, str(kp.pubkey()), str(kp.pubkey())]   # account, rent to, owner
    assert tx.signatures[0] == kp.sign_message(to_bytes_versioned(tx.message))


def test_selling_out_gets_the_token_account_rent_back(bot):
    from fomo.swaps import TOKEN_PROGRAM
    live, chain, quotes, kp, msg = live_bot(bot, dry=False)
    alert(bot)
    chain.accounts = [{"pubkey": EMPTY_ATA, "account": {"owner": TOKEN_PROGRAM, "lamports": 2039280,
                       "data": {"parsed": {"info": {"tokenAmount": {"amount": "0", "decimals": 6}}}}}}]
    bot.run(live.sell_all())
    t = bot.db.row("select * from live_trades")
    assert t["status"] == "closed" and t["rent_back"] == pytest.approx(0.00203928)
    assert t["sol_out"] == pytest.approx(0.1 + 0.00203928)
    assert len(chain.sent) == 3                                      # buy, sell, close account


def test_a_failed_live_sell_is_caught_up_after_paper_closes(bot):
    live, chain, quotes, kp, msg = live_bot(bot, dry=False)
    alert(bot)
    real_execute = live.execute
    calls = {"n": 0}

    async def flaky(q):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("blockhash expired")                  # the plan's sell doesn't land
        return await real_execute(q)

    live.execute = flaky
    t = bot.db.row("select * from paper_trades")
    bot.run(bot.engine.paper.close(t["id"]))
    bot.run(asyncio.sleep(0.05))
    assert bot.db.row("select status from live_trades")["status"] == "open"   # still holding for real
    done = bot.run(live.reconcile())
    assert done and bot.db.row("select status, close_reason from live_trades")["status"] == "closed"
    assert bot.run(live.reconcile()) == []                                   # nothing left to catch up


def test_live_only_follows_alert_types_that_make_money_on_paper(bot):
    import time as _t
    live, chain, quotes, kp, msg = live_bot(bot, dry=False)
    now = int(_t.time())
    for i in range(16):
        bot.db.run("""insert into paper_trades(mint,whale,symbol,open_ts,entry_price,size_usd,tokens,remaining,proceeds_usd,
            status,close_ts) values(?,?,?,?,1,50,50,0,30,'closed',?)""", (f"L{i}" + "1" * 40, "hype", "L", now - 3600, now - 60))
    assert bot.run(live.buy(0, "Hyped" + "1" * 39, "HYPED", "hype")) == 0
    assert any("skipping" in m["text"] for m in bot.notes.sent)
    assert live.type_allowed("SomeWhale")[0]                                 # whale alerts aren't losing: allowed
