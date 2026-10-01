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

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
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
        await srv.close()
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
