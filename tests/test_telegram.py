import asyncio

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from fomo.telegram import Telegram, keyboard, split_message


def test_split_message_keeps_lines_whole():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = split_message(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts) == text


def test_keyboard_shapes():
    kb = keyboard([[("Chart", "https://x", None), ("Mute", None, "mute:abc")], []])
    assert kb == {"inline_keyboard": [[{"text": "Chart", "url": "https://x"}, {"text": "Mute", "callback_data": "mute:abc"}]]}
    assert keyboard(None) is None


def test_client_long_polls_and_falls_back_to_plain_text():
    calls = []

    async def handler(request):
        method = request.match_info["method"]
        body = await request.json()
        calls.append((method, body))
        if method == "sendMessage" and body.get("parse_mode") == "HTML" and "<broken" in body["text"]:
            return web.json_response({"ok": False, "description": "Bad Request: can't parse entities"})
        if method == "getUpdates":
            return web.json_response({"ok": True, "result": [{"update_id": 7}]})
        return web.json_response({"ok": True, "result": {"message_id": 42}})

    async def run():
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", handler)
        server = TestServer(app)
        await server.start_server()
        async with aiohttp.ClientSession() as session:
            tg = Telegram(session, "TOKEN", "1")
            tg.base = str(server.make_url("/botTOKEN"))
            assert await tg.send("<b>hi</b>") == 42
            assert await tg.send("<broken tag") == 42          # retried without HTML
            assert await tg.updates(5) == [{"update_id": 7}]
        await server.close()

    asyncio.run(run())
    updates = [b for m, b in calls if m == "getUpdates"][0]
    assert updates["timeout"] == 25 and updates["offset"] == 5
    retried = [b for m, b in calls if m == "sendMessage" and "broken" in b["text"]]
    assert retried[0]["parse_mode"] == "HTML" and "parse_mode" not in retried[1]
