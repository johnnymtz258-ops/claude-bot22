"""Minimal Telegram Bot API client + the `notify` function every part of the bot uses."""
from __future__ import annotations

import asyncio
import html
import re
import time
from collections import deque

import aiohttp

MAX_LEN = 4000


def split_message(text: str, limit: int = MAX_LEN) -> list[str]:
    """Split on line breaks so long lists stay readable (Telegram caps messages at 4096 chars)."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def keyboard(buttons) -> dict | None:
    """[[(label, url, callback_data), ...], ...] -> Telegram inline keyboard."""
    rows = []
    for row in buttons or []:
        out = []
        for label, url, data in row:
            if url:
                out.append({"text": label, "url": url})
            elif data:
                out.append({"text": label, "callback_data": str(data)[:64]})
        if out:
            rows.append(out)
    return {"inline_keyboard": rows} if rows else None


class Telegram:
    def __init__(self, session: aiohttp.ClientSession, token: str, chat_id: str):
        self.session = session
        self.token = token
        self.chat_id = str(chat_id or "")
        self.base = f"https://api.telegram.org/bot{token}"
        self.ok = bool(token)
        self.last_error = ""
        self.sent = 0
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0

    async def send_document(self, path, caption: str = "", chat_id: str | None = None) -> bool:
        """Upload a file into the chat (e.g. the /export review zip). Telegram allows up to 50 MB."""
        if not self.ok:
            return False
        from pathlib import Path
        path = Path(path)
        for attempt in range(3):
            form = aiohttp.FormData()
            form.add_field("chat_id", str(chat_id or self.chat_id))
            if caption:
                form.add_field("caption", caption)
                form.add_field("parse_mode", "HTML")
            form.add_field("document", path.read_bytes(), filename=path.name, content_type="application/zip")
            try:
                async with self.session.post(f"{self.base}/sendDocument", data=form,
                                             timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                self.last_error = f"sendDocument: {type(exc).__name__}"
                await asyncio.sleep(1 + attempt)
                continue
            if data.get("ok"):
                return True
            self.last_error = f"sendDocument: {data.get('description', 'error')}"
            return False
        return False

    async def api(self, method: str, http_timeout: float = 20, **params):
        if not self.ok:
            return None
        payload = {k: v for k, v in params.items() if v is not None}
        for attempt in range(3):
            try:
                async with self.session.post(f"{self.base}/{method}", json=payload,
                                             timeout=aiohttp.ClientTimeout(total=http_timeout)) as resp:
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                self.last_error = f"{method}: {type(exc).__name__}"
                await asyncio.sleep(1 + attempt)
                continue
            if data.get("ok"):
                return data.get("result")
            retry = ((data.get("parameters") or {}).get("retry_after"))
            if retry:
                await asyncio.sleep(min(float(retry), 30))
                continue
            self.last_error = f"{method}: {data.get('description', 'error')}"
            if "can't parse entities" in str(data.get("description", "")) and params.get("parse_mode"):
                params = {**params, "parse_mode": None, "text": re.sub(r"<[^>]+>", "", params.get("text", ""))}
                payload = {k: v for k, v in params.items() if v is not None}
                continue
            return None
        return None

    async def send(self, text: str, *, buttons=None, silent: bool = False, reply_to: int | None = None,
                   chat_id: str | None = None) -> int:
        """Send (splitting if long). Returns the message id of the last part (0 on failure)."""
        target = chat_id or self.chat_id
        if not (self.ok and target):
            return 0
        msg_id = 0
        parts = split_message(text)
        async with self._send_lock:
            for i, part in enumerate(parts):
                wait = 0.35 - (time.monotonic() - self._last_send)  # stay under Telegram's per-chat limits
                if wait > 0:
                    await asyncio.sleep(wait)
                result = await self.api("sendMessage", chat_id=target, text=part, parse_mode="HTML",
                                        disable_web_page_preview=True, disable_notification=silent or None,
                                        reply_to_message_id=reply_to if i == 0 else None,
                                        reply_markup=keyboard(buttons) if i == len(parts) - 1 else None)
                self._last_send = time.monotonic()
                if result:
                    msg_id = int(result.get("message_id") or 0)
                    self.sent += 1
        return msg_id

    async def edit(self, message_id: int, text: str, buttons=None) -> bool:
        """True if the message now shows `text` (including "not modified": it already did)."""
        if not message_id:
            return False
        result = await self.api("editMessageText", chat_id=self.chat_id, message_id=message_id, text=text[:MAX_LEN],
                                parse_mode="HTML", disable_web_page_preview=True, reply_markup=keyboard(buttons))
        return bool(result) or "not modified" in self.last_error

    async def pin(self, message_id: int) -> bool:
        return bool(await self.api("pinChatMessage", chat_id=self.chat_id, message_id=message_id,
                                   disable_notification=True))

    async def answer(self, callback_id: str, text: str = "") -> None:
        await self.api("answerCallbackQuery", callback_query_id=callback_id, text=text[:180] or None)

    async def updates(self, offset: int) -> list:
        # long poll: Telegram holds the request up to 25s until something arrives
        result = await self.api("getUpdates", http_timeout=35, timeout=25, offset=offset,
                                allowed_updates=["message", "callback_query"])
        return result if isinstance(result, list) else []


class Notifier:
    """What the engine calls to tell you something. Also keeps a feed for the dashboard."""

    def __init__(self, telegram: Telegram | None, db):
        self.telegram = telegram
        self.db = db
        self.feed: deque = deque(maxlen=200)

    async def __call__(self, text: str, *, buttons=None, silent: bool = False, mint: str = "", wallet: str = "",
                       kind: str = "", reply_to: int | None = None) -> int:
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        self.feed.appendleft({"ts": int(time.time()), "kind": kind, "mint": mint, "wallet": wallet, "text": plain})
        msg_id = await self.telegram.send(text, buttons=buttons, silent=silent, reply_to=reply_to) if self.telegram else 0
        if not msg_id:
            print(plain + "\n")
        if msg_id and (mint or wallet):
            self.db.run("insert or replace into tg_messages(message_id,mint,wallet,kind,ts) values(?,?,?,?,?)",
                        (msg_id, mint, wallet, kind, int(time.time())))
        return msg_id
