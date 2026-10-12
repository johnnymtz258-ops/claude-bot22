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
            assert await rpc.call("getSlot", [], priority=True) == 123   # live work: first node, then the next
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

        async def on_sig(wallet, sig, logs=None):
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
    assert cfg.fast_rpc and cfg.rpc_name == "Helius"
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


def test_scanner_pauses_while_helius_is_out(bot, monkeypatch):
    calls = []

    async def fake_scout(now=None):
        calls.append(1)
        return {}

    monkeypatch.setattr(bot.scout, "scout", fake_scout)
    bot.scout.discovery.rpc.primary_down = lambda: True
    sleeps = []

    async def no_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with pytest.raises(asyncio.CancelledError):
        bot.run(bot.scout.run())
    assert calls == [] and "scanner paused" in bot.scout.paused


def test_out_of_credits_rests_the_node_without_slowing_the_others():
    hits = {"helius": 0, "backup": 0}

    async def helius(request):
        hits["helius"] += 1
        return web.Response(status=429, text="max usage reached")

    async def backup(request):
        hits["backup"] += 1
        return web.json_response({"jsonrpc": "2.0", "id": 1, "result": 5})

    async def run():
        servers = []
        for fn in (helius, backup):
            app = web.Application()
            app.router.add_post("/", fn)
            srv = TestServer(app)
            await srv.start_server()
            servers.append(srv)
        async with aiohttp.ClientSession() as session:
            rpc = SolanaRPC(session, [str(servers[0].make_url("/")), str(servers[1].make_url("/"))], min_interval=0.01)
            for _ in range(5):
                assert await rpc.call("getSlot", [], priority=True) == 5
            assert hits["helius"] == 1                      # asked once, then rested for 30 minutes
            assert rpc.rate_limited == 0 and rpc.min_interval == 0.01 and not rpc.strained()
            assert rpc.primary_down()
        for srv in servers:
            await srv.close()

    asyncio.run(run())


def test_daily_database_backup_keeps_the_last_three(tmp_path):
    from fomo.db import Database, backup
    path = tmp_path / "whales.db"
    db = Database(path)
    db.set_meta("x", "1")
    for day in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"):
        out = backup(path, tmp_path / "backups", day=day)
    assert out.name == "whales-2026-10-04.db"
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == [
        "whales-2026-10-02.db", "whales-2026-10-03.db", "whales-2026-10-04.db"]
    assert Database(out).get_meta("x") == "1"
    assert backup(":memory:", tmp_path / "b2") is None


def test_state_folder_is_per_platform(monkeypatch):
    from fomo import config
    monkeypatch.setattr(config.sys, "platform", "darwin")
    assert config.default_state_dir() == "~/Library/Application Support/FomoBot"
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert config.default_state_dir() == "~/.local/share/FomoBot"


def test_two_running_copies_are_reported_and_real_buys_held(bot):
    import time as _t
    from fomo.app import App
    app = App.__new__(App)
    app.db, app.notify = bot.db, bot.notes

    class TG:
        conflict_ts = 0.0
    app.telegram = TG()

    class Live:
        hold_until = 0.0
    app.live = Live()
    assert not bot.run(app.check_conflict())
    TG.conflict_ts = _t.time()
    assert bot.run(app.check_conflict())
    assert app.live.hold_until > _t.time() + 500
    assert "Two copies of FomoBot are running" in bot.notes.sent[-1]["text"]
    bot.run(app.check_conflict())
    assert len(bot.notes.sent) == 1                                     # warned once an hour, not every poll


def test_a_mac_env_on_a_linux_server_uses_the_servers_folder(monkeypatch, tmp_path):
    from fomo import config
    missing = tmp_path / "no.env"
    monkeypatch.setenv("FOMO_STATE_DIR", "~/Library/Application Support/FomoBot")   # what a Mac .env says
    monkeypatch.delenv("WHALE_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(config.sys, "platform", "linux")
    assert str(config.load(missing).db_path).endswith(".local/share/FomoBot/whales.db")
    monkeypatch.setattr(config.sys, "platform", "darwin")
    assert "Library/Application Support/FomoBot" in str(config.load(missing).db_path)



def test_one_free_rpc_link_replaces_helius(monkeypatch, tmp_path):
    for key in ("HELIUS_API_KEY", "SOLANA_RPC_HTTP", "SOLANA_RPC_WSS", "SOLANA_RPC_FALLBACKS"):
        monkeypatch.delenv(key, raising=False)
    missing = tmp_path / "no.env"
    monkeypatch.setenv("HELIUS_API_KEY", "used-up")
    monkeypatch.setenv("SOLANA_RPC_HTTP", "https://solana-mainnet.core.chainstack.com/KEY")
    cfg = config.load(missing)
    assert cfg.rpc_http[0] == "https://solana-mainnet.core.chainstack.com/KEY"      # yours first…
    assert "helius" in cfg.rpc_http[1] and cfg.rpc_http[-1] == config.PUBLIC_RPC    # …Helius as a backup, public last
    assert cfg.rpc_wss == "wss://solana-mainnet.core.chainstack.com/KEY"            # websocket worked out from the link
    assert cfg.rpc_wss_fallback == config.PUBLIC_WSS
    assert cfg.fast_rpc and cfg.rpc_name == "Chainstack"
    monkeypatch.delenv("HELIUS_API_KEY")
    monkeypatch.setenv("SOLANA_RPC_HTTP", "https://lb.drpc.org/ogrpc?network=solana&dkey=K")
    assert config.load(missing).rpc_wss == "wss://lb.drpc.org/ogws?network=solana&dkey=K"
    monkeypatch.delenv("SOLANA_RPC_HTTP")
    cfg = config.load(missing)
    assert not cfg.fast_rpc and cfg.rpc_http == [config.PUBLIC_RPC] and cfg.rpc_wss == config.PUBLIC_WSS


def test_background_calls_use_the_second_free_rpc_and_refused_or_used_up_nodes_are_skipped():
    from aiohttp.test_utils import TestServer
    hits = {"a": [], "b": [], "c": []}

    def node(name, reply):
        async def handler(request):
            body = await request.json()
            hits[name].append(body["method"])
            return reply(body)
        return handler

    used_up = node("a", lambda b: web.json_response(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "Monthly capacity limit exceeded"}})
        if b["method"] == "getBalance" else web.json_response({"jsonrpc": "2.0", "id": 1, "result": "a"}))
    refused = node("b", lambda b: web.Response(status=403, text="forbidden: invalid api key")
                   if b["method"] == "getSlot" else web.json_response({"jsonrpc": "2.0", "id": 1, "result": "b"}))
    public = node("c", lambda b: web.json_response({"jsonrpc": "2.0", "id": 1, "result": "c"}))

    async def run():
        servers = []
        for fn in (used_up, refused, public):
            app = web.Application()
            app.router.add_post("/", fn)
            srv = TestServer(app)
            await srv.start_server()
            servers.append(srv)
        async with aiohttp.ClientSession() as session:
            urls = [str(s.make_url("/")) for s in servers]
            rpc = SolanaRPC(session, urls)
            rpc_public = rpc.urls[2]
            import fomo.rpc as rpcmod
            rpcmod_public = rpcmod._public
            rpcmod._public = lambda u: u == rpc_public          # the third test node plays the public RPC
            try:
                assert await rpc.call("getHealth", [], priority=True) == "a"      # live work: your first RPC
                assert await rpc.call("getHealth", []) == "b"                     # background: the second one
                assert await rpc.call("getSlot", []) == "a"                       # 403 there: rested, next node
                assert not rpc.primary_down()
                assert await rpc.call("getBalance", [], priority=True) == "c"     # "limit exceeded": next node
                assert rpc.primary_down()                                          # both private ones resting now
            finally:
                rpcmod._public = rpcmod_public
        for srv in servers:
            await srv.close()

    asyncio.run(run())


def test_a_crashing_loop_is_restarted_and_the_rest_keeps_running():
    from fomo.app import App
    app = App.__new__(App)
    app.crashes, app.last_crash = {}, ""
    runs = {"n": 0}

    async def flaky():
        runs["n"] += 1
        if runs["n"] < 3:
            raise ValueError("bad answer from somewhere")

    async def go():
        import fomo.app as appmod
        real_sleep = asyncio.sleep

        async def fast_sleep(s):
            await real_sleep(0)
        appmod.asyncio.sleep = fast_sleep
        try:
            await asyncio.wait_for(app.supervise("hype scanner", flaky), 2)
        finally:
            appmod.asyncio.sleep = real_sleep
    asyncio.run(go())
    assert runs["n"] == 3 and app.crashes == {"hype scanner": 2} and "ValueError" in app.last_crash


def test_caches_stay_bounded_on_a_long_run():
    import time as _t
    from fomo.util import prune_cache
    now = _t.time()
    cache = {f"old{i}": (now - 7200, i) for i in range(15)} | {f"new{i}": (now, i) for i in range(10)}
    prune_cache(cache, max_items=20)
    assert len(cache) == 10 and all(k.startswith("new") for k in cache)
    cache = {f"k{i}": (now - i, i) for i in range(30)}
    prune_cache(cache, max_items=20)
    assert len(cache) == 15 and "k0" in cache and "k29" not in cache


def test_health_report_shows_latency_by_source_and_no_secrets(chat):
    import time as _t
    now = int(_t.time())
    chat.db.run("""insert into swaps(sig,wallet,is_me,ts,seen_ts,side,mint,source) values
        ('h1','W',0,?,?,'BUY','M','stream'), ('h2','W',0,?,?,'BUY','M2','poll')""", (now - 100, now - 98, now - 900, now - 60))
    out = chat.say("/health")
    assert "Health" in out and "straight from the stream" in out and "api-key" not in out


def test_doctor_online_checks_run_to_the_end(capsys):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("doctor", Path(__file__).resolve().parents[1] / "doctor.py")
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)
    cfg = config.Config(rpc_http=["http://127.0.0.1:9/rpc", config.PUBLIC_RPC], rpc_wss="ws://127.0.0.1:9")
    asyncio.run(asyncio.wait_for(doctor.online_checks(cfg), 30))      # no NameError: every check reports
    out = capsys.readouterr().out
    assert "127.0.0.1:9 unreachable" in out and "websocket" in out
