"""The outside services, against local test servers: SnapTrade's signed requests, Alpaca's paging and free-plan
rules, and the exact request DayBot sends to the Claude API."""
import asyncio
import json
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from daybot.alpaca import SIP_DELAY, Alpaca, parse_ts
from daybot.snaptrade import SnapTrade, normalize_order, sign


def serve(handler_map):
    """Run a coroutine against a local server built from {(method, path): handler}."""
    app = web.Application()
    for (method, path), fn in handler_map.items():
        app.router.add_route(method, path, fn)
    return TestServer(app)


def test_snaptrade_signs_every_request_like_its_sdk():
    seen = []

    async def recent(req):
        qs = req.rel_url.raw_query_string
        expected = sign("secret-key", req.path, qs, None)
        seen.append((req.path, dict(req.query), req.headers.get("Signature") == expected))
        return web.json_response({"orders": [
            {"brokerage_order_id": "9", "status": "EXECUTED", "universal_symbol": {"symbol": "AMD"}, "action": "SELL",
             "filled_quantity": "5", "execution_price": "150.5", "time_executed": "2026-10-21T15:00:00Z"},
            {"brokerage_order_id": "10", "status": "CANCELED", "action": "BUY", "filled_quantity": "0"}]})

    async def login(req):
        body = await req.json()
        ok = req.headers.get("Signature") == sign("secret-key", req.path, req.rel_url.raw_query_string, body)
        seen.append((req.path, body, ok))
        return web.json_response({"redirectURI": "https://connect.example/x", "sessionId": "s"})

    async def go():
        server = serve({("GET", "/accounts/acc1/recentOrders"): recent, ("POST", "/snapTrade/login"): login})
        await server.start_server()
        async with aiohttp.ClientSession() as session:
            st = SnapTrade(session, "client-1", "secret-key")
            st.base = str(server.make_url("")).rstrip("/")
            fills = await st.recent_fills("acc1")
            link = await st.connect_link()
        await server.close()
        return fills, link
    fills, link = asyncio.run(go())
    assert [s[2] for s in seen] == [True, True]
    path, query, _ = seen[0]
    assert query["clientId"] == "client-1" and query["only_executed"] == "true" and abs(int(query["timestamp"]) - time.time()) < 60
    assert seen[1][1] == {"connectionType": "read"}                # read-only connections, always
    assert len(fills) == 1 and fills[0]["side"] == "SELL" and fills[0]["symbol"] == "AMD" and fills[0]["qty"] == 5
    assert link == "https://connect.example/x"


def test_snaptrade_option_order_and_unknown_shapes():
    o = {"brokerage_order_id": "2", "status": "EXECUTED", "action": "SELL_TO_CLOSE", "filled_quantity": "2",
         "execution_price": "3.4", "time_executed": "2026-10-21T15:00:00Z",
         "option_symbol": {"ticker": "NVDA  261023C00140000", "option_type": "CALL", "strike_price": 140,
                           "expiration_date": "2026-10-23", "underlying_symbol": {"symbol": "NVDA"}}}
    f = normalize_order(o, "a")
    assert f["kind"] == "option" and f["effect"] == "close" and f["symbol"] == "NVDA261023C00140000" and f["multiplier"] == 100
    assert normalize_order({"status": "EXECUTED", "action": "BUY"}, "a") is None


def test_alpaca_pages_and_respects_the_free_plan():
    calls = []

    async def bars(req):
        q = dict(req.query)
        calls.append(q)
        if q.get("feed") == "sip" and parse_ts(q["end"]) > time.time() - SIP_DELAY + 5:
            return web.json_response({"message": "subscription does not permit querying recent SIP data"}, status=403)
        if not q.get("page_token"):
            return web.json_response({"bars": {"AAA": [{"t": "2026-10-21T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]},
                                      "next_page_token": "p2"})
        return web.json_response({"bars": {"AAA": [{"t": "2026-10-21T13:31:00.123456789Z", "o": 1.5, "h": 2, "l": 1, "c": 1.8, "v": 5}]},
                                  "next_page_token": None})

    async def go():
        server = serve({("GET", "/v2/stocks/bars"): bars})
        await server.start_server()
        async with aiohttp.ClientSession() as session:
            a = Alpaca(session, "k", "s", "iex")
            a.data_base = str(server.make_url("")).rstrip("/")
            got = await a.bars(["AAA"], "1Min", time.time() - 3600, time.time())
            sip = await a.bars(["AAA"], "1Min", time.time() - 3600, time.time(), feed="sip")
        await server.close()
        return got, sip
    got, sip = asyncio.run(go())
    assert [b[4] for b in got["AAA"]] == [1.5, 1.8] and got["AAA"][1][0] == parse_ts("2026-10-21T13:31:00Z")
    sip_calls = [c for c in calls if c.get("feed") == "sip"]
    assert sip_calls and all(parse_ts(c["end"]) <= time.time() - SIP_DELAY + 5 for c in sip_calls)
    assert len(sip["AAA"]) == 2


def test_claude_requests_use_the_current_model_fallbacks_and_structured_output():
    anthropic = pytest.importorskip("anthropic")
    from daybot.ai import AI, CHART_SCHEMA
    from daybot.config import Config
    from daybot.db import Database
    seen = []

    async def messages(req):
        body = await req.json()
        seen.append((dict(req.headers), body))
        text = json.dumps({"ticker": "NVDA", "timeframe": "5-min", "trend": "up", "pattern": "bull flag", "bias": "long",
                           "confidence": "medium", "levels": [{"price": 141.2, "kind": "resistance"}],
                           "entry_idea": "over 141.20", "stop_idea": "139.80", "targets": "143", "invalidation": "below VWAP",
                           "risks": ["extended"], "answer": ""}) if "output_config" in body and "format" in body["output_config"] \
            else "Hold half; your stop is 139.80."
        return web.json_response({"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                                  "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                                  "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 150}})

    async def go():
        server = serve({("POST", "/v1/messages"): messages})
        await server.start_server()
        ai = AI(Config(anthropic_key="sk-test"), Database(":memory:"))
        ai.client = anthropic.AsyncAnthropic(api_key="sk-test", base_url=str(server.make_url("")).rstrip("/"),
                                             max_retries=0)
        read = await ai.read_chart(b"\x89PNG fake", "image/png", "should I buy?")
        answer = await ai.ask("hold NVDA?", {"open_positions": []})
        await server.close()
        return ai, read, answer
    ai, read, answer = asyncio.run(go())
    assert read["ticker"] == "NVDA" and answer.startswith("Hold half")
    headers, body = seen[0]
    assert body["model"] == "claude-opus-5-5"
    assert "server-side-fallback-2026-07-01" in headers.get("anthropic-beta", "") and body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": CHART_SCHEMA}
    image = body["messages"][0]["content"][0]
    assert image["type"] == "image" and image["source"]["type"] == "base64" and image["source"]["media_type"] == "image/png"
    assert "thinking" not in body and "budget_tokens" not in json.dumps(body)
    assert ai.usage_today()["calls"] == 2


def test_a_refusal_is_reported_not_crashed():
    anthropic = pytest.importorskip("anthropic")
    from daybot.ai import AI
    from daybot.config import Config
    from daybot.db import Database

    async def messages(req):
        return web.json_response({"id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                                  "content": [], "stop_reason": "refusal", "stop_sequence": None,
                                  "usage": {"input_tokens": 10, "output_tokens": 0}})

    async def go():
        server = serve({("POST", "/v1/messages"): messages})
        await server.start_server()
        ai = AI(Config(anthropic_key="sk-test"), Database(":memory:"))
        ai.client = anthropic.AsyncAnthropic(api_key="sk-test", base_url=str(server.make_url("")).rstrip("/"),
                                             max_retries=0)
        out = await ai.ask("?", {})
        await server.close()
        return ai, out
    ai, out = asyncio.run(go())
    assert out is None and "declined" in ai.last_error
