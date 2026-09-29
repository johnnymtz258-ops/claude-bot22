import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from fomo.dashboard import build_app
from tests.demo_app import COINS, WHALES, DemoApp


@pytest.fixture
def client():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    demo = DemoApp()
    loop.run_until_complete(demo.seed())
    application, dash = build_app(demo)
    dash.allowed_hosts.add("127.0.0.1")  # the test client connects without a port-qualified host header
    server = TestServer(application, host="127.0.0.1", port=0)
    c = TestClient(server, loop=loop)
    loop.run_until_complete(c.start_server())
    dash.allowed_hosts.add(f"127.0.0.1:{server.port}")
    c.demo, c.dash, c.loop = demo, dash, loop
    yield c
    loop.run_until_complete(c.close())
    loop.close()


def call(c, method, path, **kw):
    async def go():
        resp = await c.request(method, path, **kw)
        body = await resp.json() if resp.content_type == "application/json" else await resp.text()
        return resp.status, body
    return c.loop.run_until_complete(go())


def test_read_endpoints_work(client):
    for path in ("/api/overview", "/api/feed", "/api/hot", "/api/whales", "/api/positions", "/api/stats",
                 "/api/settings", "/api/find", f"/api/coin/{COINS['CASHED'][0]}", f"/api/whale/{WHALES['Rocket']}"):
        status, body = call(client, "GET", path)
        assert status == 200, (path, body)
    status, page = call(client, "GET", "/")
    assert status == 200 and client.dash.token in page and "__TOKEN__" not in page


def test_whales_endpoint_shape(client):
    _, whales = call(client, "GET", "/api/whales")
    rocket = next(w for w in whales if w["name"] == "Rocket")
    assert rocket["status"] == "HOT" and rocket["n"] == 8
    cold = next(w for w in whales if w["name"] == "Bagholder")
    assert cold["status"] == "COLD" and cold["auto_muted"] == 1


def test_lookup_for_extension(client):
    _, coin = call(client, "GET", f"/api/lookup/{COINS['CASHED'][0]}")
    assert coin["type"] == "coin" and {h["name"] for h in coin["holders"]} == {"Rocket", "Dino"}
    _, whale = call(client, "GET", f"/api/lookup/{WHALES['Dino']}")
    assert whale["type"] == "whale" and whale["whale"]["name"] == "Dino"
    _, url = call(client, "GET", f"/api/lookup/https%3A%2F%2Fgmgn.ai%2Fsol%2Ftoken%2F{COINS['MOIN'][0]}")
    assert url["type"] == "coin"


def test_actions_need_the_session_token(client):
    body = {"address": "GjJyeC1rB1p4d6k1Mzw5Y6vYGZyLr8N8zQJ7XU4yzF1G", "name": "X"}
    status, _ = call(client, "POST", "/api/whales", json=body)
    assert status == 403
    status, _ = call(client, "POST", "/api/whales", json=body, headers={"X-Fomo-Token": "wrong"})
    assert status == 403
    status, r = call(client, "POST", "/api/whales", json=body, headers={"X-Fomo-Token": client.dash.token})
    assert status == 200 and r["ok"]


def test_foreign_host_header_is_refused(client):
    status, _ = call(client, "GET", "/api/overview", headers={"Host": "evil.example:8787"})
    assert status == 403


def test_settings_and_manual_trade_via_api(client):
    h = {"X-Fomo-Token": client.dash.token}
    status, r = call(client, "POST", "/api/settings", json={"name": "min_whale_buy_usd", "value": "250"}, headers=h)
    assert status == 200 and client.demo.cfg.get("MIN_WHALE_BUY_USD") == 250
    status, r = call(client, "POST", "/api/settings", json={"name": "MIN_WHALE_BUY_USD", "value": "-5"}, headers=h)
    assert status == 400
    status, r = call(client, "POST", "/api/trades", json={"mint": COINS["BAGSPAY"][0], "side": "BUY", "usd": 20,
                                                          "mc": "500k"}, headers=h)
    assert status == 200 and r["mc"] == pytest.approx(500_000)
    status, r = call(client, "POST", "/api/trades/undo", headers=h)
    assert r["ok"]
