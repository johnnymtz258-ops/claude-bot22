"""Ingestion: pump.fun buys read straight from the stream's logs, and a stream that heals itself."""
import asyncio
import hashlib
import json
import time

from aiohttp import web
from aiohttp.test_utils import TestServer

from fomo import pumplog
from fomo.rpc import WalletStream
from tests.helpers import WHALE, WHALE2

COIN = pumplog.b58encode(hashlib.sha256(b"a pump.fun coin").digest())
OTHER_PROGRAM = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"


def logs_for(*events, program=pumplog.PUMP_PROGRAM):
    return (["Program ComputeBudget111111111111111111111111111111 invoke [1]",
             "Program ComputeBudget111111111111111111111111111111 success",
             f"Program {program} invoke [1]", "Program log: Instruction: Buy"]
            + list(events) + [f"Program {program} consumed 40000 of 200000 compute units", f"Program {program} success"])


def buy_event(user=WHALE, sol=1.5, tokens=3_000_000, ts=None, is_buy=True, mint=COIN):
    return pumplog.encode_trade(mint=mint, sol_lamports=int(sol * 1e9), token_raw=int(tokens * 1e6), is_buy=is_buy,
                                user=user, ts=int(ts or time.time()))


def test_trade_events_are_decoded_only_from_the_pump_program():
    ev = pumplog.trades(logs_for(buy_event()))
    assert len(ev) == 1 and ev[0]["mint"] == COIN and ev[0]["user"] == WHALE and ev[0]["is_buy"]
    assert ev[0]["sol_lamports"] == 1_500_000_000 and ev[0]["token_raw"] == 3_000_000_000_000
    assert pumplog.trades(logs_for(buy_event(), program=OTHER_PROGRAM)) == []      # a fork's look-alike event
    assert pumplog.trades(["Program data: not base64!!"]) == []


def test_buy_by_takes_only_this_wallets_buys():
    logs = logs_for(buy_event(user=WHALE2), buy_event(sol=0.5, tokens=1_000_000), buy_event(sol=1.0, tokens=2_000_000))
    b = pumplog.buy_by(logs, WHALE)
    assert b["sol_lamports"] == 1_500_000_000 and b["token_raw"] == 3_000_000_000_000   # two fills added
    assert pumplog.buy_by(logs_for(buy_event(is_buy=False)), WHALE) is None          # sells read the full tx
    assert pumplog.buy_by(logs_for(buy_event(user=WHALE2)), WHALE) is None
    swap = pumplog.as_swap(b, prior_holding=0)
    assert swap["side"] == "BUY" and swap["new_position"] and abs(swap["price_base"] - 5e-7) < 1e-12


def test_a_whale_pump_buy_is_alerted_from_the_logs_without_an_rpc_call(bot):
    bot.whales.add(WHALE, "Rocket")
    bot.market.set_pair(mint=COIN, price=0.000075, mc=75_000)
    sig = "fastsig" + "x" * 40
    bot.run(bot.engine.process(WHALE, sig, "stream", logs_for(buy_event(ts=time.time()))))
    assert sig not in bot.rpc.txs                                   # never fetched
    assert bot.engine.fast_buys == 1 and bot.notes.kinds() == ["BUY"]
    row = bot.db.row("select * from swaps")
    assert row["dex"] == "Pump.fun" and row["mint"] == COIN and abs(row["token_amount"] - 3_000_000) < 1e-6
    assert bot.db.scalar("select 1 from processed where sig=?", (sig,))   # the poller won't redo it


def test_a_trade_the_poller_finds_twice_makes_the_stream_resubscribe_that_wallet(bot):
    from tests.helpers import pump_buy
    asked = []
    bot.engine.on_stream_miss = asked.append
    bot.whales.add(WHALE, "Rocket")
    bot.market.set_pair(mint=COIN, price=0.000075, mc=75_000)
    bot.feed(WHALE, pump_buy(mint=COIN, block_time=int(time.time())), source="poll")
    assert asked == []
    bot.feed(WHALE, pump_buy(mint=COIN, block_time=int(time.time()), new=False, pre_tokens=3_000_000), source="poll")
    assert asked == [WHALE]
    assert bot.tracker.poll_every() in (15, 45, 120)


def test_failed_subscriptions_are_retried_and_a_wallet_can_be_resubscribed(monkeypatch):
    import fomo.rpc as rpcmod
    seen, sockets = [], []
    refused = {"left": 1}

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.append(ws)
        sub = 200
        async for msg in ws:
            data = json.loads(msg.data)
            seen.append(data["method"])
            if data["method"] == "logsSubscribe":
                if refused["left"]:
                    refused["left"] -= 1
                    await ws.send_json({"jsonrpc": "2.0", "id": data["id"], "error": {"code": -32005, "message": "busy"}})
                    continue
                sub += 1
                await ws.send_json({"jsonrpc": "2.0", "id": data["id"], "result": sub})
            else:
                await ws.send_json({"jsonrpc": "2.0", "id": data["id"], "result": True})
        return ws

    async def run():
        app = web.Application()
        app.router.add_get("/", ws_handler)
        srv = TestServer(app)
        await srv.start_server()

        async def on_sig(wallet, sig, logs=None):
            pass

        stream = WalletStream(str(srv.make_url("/")).replace("http", "ws"), on_sig)
        stream.set_wallets([WHALE])
        task = asyncio.create_task(stream.run())
        await asyncio.sleep(0.3)
        assert stream.failed_subs == 1 and stream.subscribed == 0
        stream._last_sync -= 31                                      # 30 seconds later: tried again
        for _ in range(60):
            await asyncio.sleep(0.05)
            if stream.subscribed == 1:
                break
        assert stream.subscribed == 1
        stream.resubscribe(WHALE)
        for _ in range(60):
            await asyncio.sleep(0.05)
            if seen.count("logsSubscribe") >= 3 and stream.subscribed == 1:
                break
        assert "logsUnsubscribe" in seen and seen.count("logsSubscribe") == 3 and stream.resubscribed == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for ws in sockets:
            await ws.close()
        await asyncio.wait_for(srv.close(), timeout=5)

    asyncio.run(run())


def test_the_poller_speeds_up_only_while_the_stream_is_missing_trades(bot):
    class Stream:
        connected = True
    bot.tracker.stream = Stream()
    assert bot.tracker.poll_every() in (45, 120)
    bot.engine.stream_misses = {WHALE: [int(time.time()) - 60]}
    assert bot.tracker.poll_every() == 15
    bot.engine.stream_misses = {WHALE: [int(time.time()) - 3600]}           # an old miss: back to normal
    assert bot.tracker.poll_every() in (45, 120)
    Stream.connected = False
    assert bot.tracker.poll_every() == 15
