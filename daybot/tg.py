"""Telegram for DayBot: FomoBot's client plus photos (charts out, screenshots in)."""
from __future__ import annotations

import asyncio
import html
import re
import time
from collections import deque

import aiohttp

from fomo.telegram import Telegram as _Base
from fomo.telegram import keyboard

CAPTION_MAX = 1000


class Telegram(_Base):
    async def send_photo(self, photo: bytes, caption: str = "", *, buttons=None, silent: bool = False,
                         chat_id: str | None = None) -> int:
        if not (self.ok and (chat_id or self.chat_id)):
            return 0
        for attempt in range(3):
            form = aiohttp.FormData()
            form.add_field("chat_id", str(chat_id or self.chat_id))
            if caption:
                form.add_field("caption", caption[:1024])
                form.add_field("parse_mode", "HTML")
            if silent:
                form.add_field("disable_notification", "true")
            kb = keyboard(buttons)
            if kb:
                import json
                form.add_field("reply_markup", json.dumps(kb))
            form.add_field("photo", photo, filename="chart.png", content_type="image/png")
            try:
                async with self._send_lock:
                    async with self.session.post(f"{self.base}/sendPhoto", data=form,
                                                 timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        data = await resp.json(content_type=None)
                    self._last_send = time.monotonic()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                self.last_error = f"sendPhoto: {type(exc).__name__}"
                await asyncio.sleep(1 + attempt)
                continue
            if data.get("ok"):
                self.sent += 1
                return int((data.get("result") or {}).get("message_id") or 0)
            retry = (data.get("parameters") or {}).get("retry_after")
            if retry:
                await asyncio.sleep(min(float(retry), 30))
                continue
            self.last_error = f"sendPhoto: {data.get('description', 'error')}"
            return 0
        return 0

    async def download(self, file_id: str, max_bytes: int = 10_000_000) -> bytes | None:
        """A file someone sent the bot (a chart screenshot)."""
        info = await self.api("getFile", file_id=file_id)
        path = (info or {}).get("file_path")
        if not path:
            return None
        try:
            async with self.session.get(f"https://api.telegram.org/file/bot{self.token}/{path}",
                                        timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.content.read(max_bytes + 1)
                return data if len(data) <= max_bytes else None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None


class Notifier:
    """What every part of DayBot calls to tell you something. Keeps a feed for the dashboard."""

    def __init__(self, telegram: Telegram | None, db):
        self.telegram = telegram
        self.db = db
        self.feed: deque = deque(maxlen=300)

    async def __call__(self, text: str, *, buttons=None, silent: bool = False, kind: str = "", symbol: str = "",
                       photo: bytes | None = None, reply_to: int | None = None) -> int:
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        now = int(time.time())
        self.feed.appendleft({"ts": now, "kind": kind, "symbol": symbol, "text": plain})
        self.db.event(now, kind or "MSG", plain, symbol)
        msg_id = 0
        tg = self.telegram
        if tg and tg.ok:
            if photo and len(text) <= CAPTION_MAX:
                msg_id = await tg.send_photo(photo, text, buttons=buttons, silent=silent)
            else:
                if photo:
                    first = text.split("\n", 1)[0]
                    await tg.send_photo(photo, first[:CAPTION_MAX], silent=True)
                msg_id = await tg.send(text, buttons=buttons, silent=silent, reply_to=reply_to)
        if not msg_id:
            print(plain + "\n")
        return msg_id
