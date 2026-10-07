import asyncio
import json
import sqlite3

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from fomo import config
from fomo.db import Database, import_legacy_whales
from fomo.rpc import SolanaRPC, WalletStream
from tests.helpers import WHALE, WHALE2


def test_rpc_fails_over_and_waits_for_new_transactions():
    hits = {"a": 0, "b": 0}

    async def a(request):
        hits["a"] += 1
        return web.Response(status=429)

    async def b(request):
        hits["b"] += 1
        body = await request.json()
        if body["method"] == "getTransaction":
            # first poll: not indexed yet (null), second poll: found
            return web.json_response({"jsonrpc": "2.0", "id": 1, "result": {"slot": 1} if hits["b"] > 1 else None})
        if body["method"] == "bad":
            return web.json_response({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Invalid params"}})
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": 123})

    async def run():
        apps = []
        for fn in (a, b):
            app = web.Application()
            app.router.add_post("/", fn)
            srv = TestServer(app)
            await srv.start_server()
            apps.append(srv)
        async with aiohttp.ClientSession() as session:
            rpc = SolanaRPC(session, [str(apps[0].make_url("/")), str(apps[1].make_url("/"))])
            assert await rpc.call("getSlot", []) == 123
            assert rpc.rate_limited == 1
            assert await rpc.transaction("sig", wait=3) == {"slot": 1}
            assert await rpc.call("bad", []) is None          # a definite error isn't retried forever
        for srv in apps:
            await srv.close()
        return hits

    hits = asyncio.run(run())
    assert hits["a"] == 1  # the rate-limited node was cooled down and skipped afterwards


def test_wallet_stream_subscribes_and_reports_signatures():
    got = []
    seen_methods = []

    sockets = []

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.append(ws)
        sub = 100
        async for msg in ws:
            data = json.loads(msg.data)
            seen_methods.append((data["method"], data["params"][0]))
            if data["method"] == "logsSubscribe":
                sub += 1
                await ws.send_json({"jsonrpc": "2.0", "id": data["id"], "result": sub})
                wallet = data["params"][0]["mentions"][0]
                await ws.send_json({"jsonrpc": "2.0", "method": "logsNotification", "params": {
                    "subscription": sub, "result": {"context": {"slot": 5},
                                                    "value": {"signature": f"sig-{wallet[:4]}", "err": None}}}})
                await ws.send_json({"jsonrpc": "2.0", "method": "logsNotification", "params": {
                    "subscription": sub, "result": {"value": {"signature": "failed", "err": {"x": 1}}}}})
            elif data["method"] == "logsUnsubscribe":
                await ws.send_json({"jsonrpc": "2.0", "id": data["id"], "result": True})
        return ws

    async def run():
        app = web.Application()
        app.router.add_get("/", ws_handler)
        srv = TestServer(app)
        await srv.start_server()

        async def on_sig(wallet, sig):
            got.append((wallet, sig))

        stream = WalletStream(str(srv.make_url("/")).replace("http", "ws"), on_sig)
        stream.set_wallets([WHALE, WHALE2])
        task = asyncio.create_task(stream.run())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if len(got) >= 2:
                break
        assert stream.connected and stream.subscribed == 2
        stream.set_wallets([WHALE])  # removing a whale unsubscribes it without reconnecting
        for _ in range(100):
            await asyncio.sleep(0.05)
            if stream.subscribed == 1 and any(m == "logsUnsubscribe" for m, _ in seen_methods):
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for ws in sockets:  # close server-side sockets first, or the test server can wait on them forever
            await ws.close()
        await asyncio.wait_for(srv.close(), timeout=5)
        return stream

    stream = asyncio.run(run())
    assert sorted(got) == sorted([(WHALE, f"sig-{WHALE[:4]}"), (WHALE2, f"sig-{WHALE2[:4]}")])  # failed tx dropped
    assert stream.subscribed == 1


def test_config_loads_env_and_validates(tmp_path, monkeypatch):
    for key in ("MY_WALLETS", "PUBLIC_SOLANA_WALLET_ADDRESS", "HELIUS_API_KEY", "MIN_WHALE_BUY_USD", "SOLANA_RPC_HTTP"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"PUBLIC_SOLANA_WALLET_ADDRESS={WHALE}\nHELIUS_API_KEY=abc\nMIN_WHALE_BUY_USD=250\n"
                   f"FOMO_STATE_DIR={tmp_path}\n")
    cfg = config.load(env)
    assert cfg.my_wallets == [WHALE]                     # old key still works
    assert cfg.uses_helius and cfg.rpc_http[0].endswith("api-key=abc") and "helius" in cfg.rpc_wss
    assert cfg.get("MIN_WHALE_BUY_USD") == 250
    assert cfg.db_path == tmp_path / "whales.db"
    assert cfg.set("quiet_low_grade", "off") == 0 and not cfg.flag("QUIET_LOW_GRADE")
    with pytest.raises(ValueError):
        cfg.set("LATE_CHASE_PCT", "abc")
    with pytest.raises(ValueError):
        cfg.set("NOPE", 1)


def test_old_bot_whales_are_imported_once(tmp_path):
    old = tmp_path / "fomo_master.db"
    con = sqlite3.connect(old)
    con.execute("create table copy_wallets(address text, label text, enabled integer)")
    con.execute("insert into copy_wallets values(?,?,1)", (WHALE, "Rocket"))
    con.execute("insert into copy_wallets values(?,?,1)", (WHALE2, "auto-flow"))
    con.execute("insert into copy_wallets values(?,?,0)", ("GjJyeC1rB1p4d6k1Mzw5Y6vYGZyLr8N8zQJ7XU4yzF1G", "off"))
    con.commit()
    con.close()
    wallets_file = tmp_path / "tracked_wallets.json"
    wallets_file.write_text(json.dumps({"wallets": [{"address": WHALE2, "label": "Dino"}]}))
    db = Database(tmp_path / "whales.db")
    assert import_legacy_whales(db, old, [wallets_file]) == 2
    names = {r["address"]: r["name"] for r in db.rows("select * from whales")}
    assert names[WHALE] == "Rocket" and names[WHALE2] == "Dino"  # your name beats the old "auto-flow" tag
    assert import_legacy_whales(db, old, [wallets_file]) == 0


def test_autopilot_whales_are_dropped_once_and_yours_kept(tmp_path):
    from fomo.db import drop_autopilot_whales
    db = Database(tmp_path / "w.db")
    db.run("insert into whales(address,name,added_ts,source) values(?,?,?,?)", (WHALE, "auto-1", 0, "auto"))
    db.run("insert into whales(address,name,added_ts,source) values(?,?,?,?)", (WHALE2, "Mine", 0, "manual"))
    assert drop_autopilot_whales(db) == 1
    assert {r["address"] for r in db.rows("select address from whales where active=1")} == {WHALE2}
    db.run("update whales set active=1 where address=?", (WHALE,))  # if you re-add one later, it stays
    assert drop_autopilot_whales(db) == 0


def test_rpc_asks_for_newer_transaction_versions():
    seen = []

    async def node(request):
        body = await request.json()
        v = body["params"][1]["maxSupportedTransactionVersion"]
        seen.append(v)
        if v < 2:
            return web.json_response({"jsonrpc": "2.0", "id": 1, "error": {"code": -32015, "message":
                f"Transaction version ({v + 1}) is not supported by the requesting client. Please try the request again "
                f"with the following configuration parameter: \"maxSupportedTransactionVersion\": {v + 1}"}})
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": {"slot": 7}})

    async def run():
        app = web.Application()
        app.router.add_post("/", node)
        srv = TestServer(app)
        await srv.start_server()
        async with aiohttp.ClientSession() as session:
            rpc = SolanaRPC(session, [str(srv.make_url("/"))])
            assert await rpc.transaction("sig", wait=3) == {"slot": 7}
            assert await rpc.transaction("sig2", wait=3) == {"slot": 7}
        await srv.close()

    asyncio.run(run())
    assert seen == [1, 2, 2]     # the bot remembers the version, so later fetches work first time


def test_live_trade_fetch_is_retried_in_seconds(bot, monkeypatch):
    from fomo import engine as eng
    from tests.helpers import MINT, pump_buy
    monkeypatch.setattr(eng, "RETRY_DELAYS", (0.05, 0.05))
    bot.whales.add(WHALE, "Rocket")
    bot.market.set_pair(mint=MINT, price=0.000075, mc=75_000)
    tx = pump_buy()
    sig = "late-index-sig"
    real = bot.rpc.txs
    bot.rpc.txs = {}                                     # not indexed yet when the stream fires
    worker = asyncio.get_event_loop().create_task(bot.engine.worker())
    bot.run(bot.engine.enqueue(WHALE, sig, "stream"))
    bot.run(asyncio.sleep(0.02))
    assert bot.db.scalar("select count(*) from swaps") == 0
    bot.rpc.txs = {**real, sig: tx}                      # indexed a moment later
    bot.run(asyncio.sleep(0.3))
    assert bot.db.scalar("select count(*) from swaps") == 1
    assert bot.db.scalar("select source from swaps") == "stream"
    worker.cancel()


def test_rate_limits_never_show_as_a_health_issue():
    calls = {"n": 0}

    async def node(request):
        calls["n"] += 1
        if calls["n"] % 2:
            return web.Response(status=429)
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": 7})

    async def run():
        app = web.Application()
        app.router.add_post("/", node)
        srv = TestServer(app)
        await srv.start_server()
        async with aiohttp.ClientSession() as session:
            rpc = SolanaRPC(session, [str(srv.make_url("/"))])
            assert await rpc.call("getSlot", [], attempts=1) is None     # throttled, background call gave up
            assert await rpc.call("getSlot", []) == 7
            assert rpc.last_error == "" and "rate limited" in rpc.last_transient and rpc.rate_limited >= 1
        await srv.close()

    asyncio.run(run())
