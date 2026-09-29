"""Optional PumpPortal discovery radar for Fomo Bot v12.

Read-only. Uses a single WebSocket connection and only the free token-creation /
migration streams by default. It never signs transactions and never subscribes to
metered token/account trade streams unless a future explicit opt-in feature is added.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
from collections import deque

import certifi
import websockets


def _b(name: str, default: bool = True) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


class PumpPortalRadar:
    def __init__(self):
        self.key = os.getenv("PUMPPORTAL_API_KEY", "").strip()
        self.enabled = _b("PUMPPORTAL_ENABLED", True) and bool(self.key)
        self.url = f"wss://pumpportal.fun/api/data?api-key={self.key}" if self.key else ""
        self.connected = False
        self.last_message = 0.0
        self.stop_event = asyncio.Event()
        self.events = deque(maxlen=5000)  # (ts, mint, kind)
        self.first_seen: dict[str, float] = {}
        self.last_seen: dict[str, float] = {}
        self.migrations: dict[str, float] = {}
        self._last_error_log = 0.0
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    def status(self) -> str:
        if not self.key:
            return "OFF/not configured"
        if not self.enabled:
            return "OFF"
        return "CONNECTED" if self.connected else "RECONNECTING"

    @staticmethod
    def _mint_from(message: dict) -> str:
        for key in ("mint", "tokenAddress", "token_address", "mintAddress", "address"):
            value = message.get(key)
            if isinstance(value, str) and 32 <= len(value.strip()) <= 44:
                return value.strip()
        token = message.get("token")
        if isinstance(token, dict):
            for key in ("mint", "address", "tokenAddress"):
                value = token.get(key)
                if isinstance(value, str) and 32 <= len(value.strip()) <= 44:
                    return value.strip()
        return ""

    @staticmethod
    def _kind_from(message: dict) -> str:
        text = " ".join(str(message.get(k) or "") for k in ("txType", "type", "event", "eventType", "method")).lower()
        if "migrat" in text or "complete" in text:
            return "MIGRATION"
        return "NEW_TOKEN"

    def _record(self, mint: str, kind: str) -> None:
        if not mint:
            return
        now = time.time()
        self.first_seen.setdefault(mint, now)
        self.last_seen[mint] = now
        if kind == "MIGRATION":
            self.migrations[mint] = now
        self.events.append((now, mint, kind))

    def candidate_mints(self, max_age_seconds: int = 20 * 60) -> set[str]:
        now = time.time()
        for mint, ts in list(self.last_seen.items()):
            if now - ts > max_age_seconds * 4:
                self.last_seen.pop(mint, None)
                self.first_seen.pop(mint, None)
                self.migrations.pop(mint, None)
        return {mint for mint, ts in self.last_seen.items() if now - ts <= max_age_seconds}

    def is_recent(self, mint: str, max_age_seconds: int = 20 * 60) -> bool:
        ts = self.last_seen.get(str(mint or ""), 0.0)
        return bool(ts and time.time() - ts <= max_age_seconds)

    def is_migration(self, mint: str, max_age_seconds: int = 30 * 60) -> bool:
        ts = self.migrations.get(str(mint or ""), 0.0)
        return bool(ts and time.time() - ts <= max_age_seconds)

    def provenance(self, mint: str) -> list[str]:
        out = []
        if self.is_recent(mint):
            out.append("PumpPortal new-token")
        if self.is_migration(mint):
            out.append("PumpPortal migration")
        return out

    async def _listen_once(self) -> None:
        async with websockets.connect(
            self.url,
            ssl=self.ssl_context,
            ping_interval=25,
            ping_timeout=20,
            close_timeout=5,
            max_queue=2000,
        ) as ws:
            self.connected = True
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            await ws.send(json.dumps({"method": "subscribeMigration"}))
            print("[pumpportal] connected; free new-token + migration discovery active")
            async for raw in ws:
                self.last_message = time.time()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                mint = self._mint_from(msg)
                if mint:
                    self._record(mint, self._kind_from(msg))

    async def run(self) -> None:
        if not self.enabled:
            if self.key:
                print("[pumpportal] disabled")
            else:
                print("[pumpportal] not configured; existing Solana realtime radar remains active")
            return
        backoff = 2
        while not self.stop_event.is_set():
            try:
                await self._listen_once()
                backoff = 2
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                now = time.time()
                if now - self._last_error_log >= 300:
                    print(f"[pumpportal] reconnecting after {type(exc).__name__}: {exc}")
                    self._last_error_log = now
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        self.connected = False

    async def close(self) -> None:
        self.stop_event.set()
