"""Earnings dates (optional: FINNHUB_API_KEY, free at finnhub.io). Without a key the bot simply doesn't warn about
earnings — everything else works."""
from __future__ import annotations

import datetime as dt
import time

import aiohttp

from .clock import et


class Earnings:
    URL = "https://finnhub.io/api/v1/calendar/earnings"

    def __init__(self, session: aiohttp.ClientSession | None, key: str):
        self.session, self.key = session, key
        self.cal: dict[str, dict] = {}
        self.loaded = 0.0
        self.last_error = ""

    @property
    def ok(self) -> bool:
        return bool(self.key and self.session)

    async def load(self, now: float) -> None:
        if not self.ok or now - self.loaded < 6 * 3600:
            return
        start = et(now).date()
        params = {"from": start.isoformat(), "to": (start + dt.timedelta(days=21)).isoformat(), "token": self.key}
        try:
            async with self.session.get(self.URL, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    self.last_error = f"finnhub {resp.status}"
                    self.loaded = now
                    return
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            self.last_error = f"finnhub {type(exc).__name__}"
            return
        cal = {}
        for row in (data or {}).get("earningsCalendar") or []:
            sym = str(row.get("symbol") or "").upper()
            day = str(row.get("date") or "")
            if sym and day and (sym not in cal or day < cal[sym]["date"]):
                cal[sym] = {"date": day, "hour": str(row.get("hour") or "")}
        self.cal, self.loaded = cal, now

    async def next_report(self, symbol: str, now: float | None = None) -> dict | None:
        now = now or time.time()
        await self.load(now)
        row = self.cal.get(symbol.upper())
        if not row:
            return None
        today = et(now).date()
        try:
            d = dt.date.fromisoformat(row["date"])
        except ValueError:
            return None
        days = (d - today).days
        when = {"bmo": "before the open", "amc": "after the close", "dmh": "during the day"}.get(row["hour"], "")
        label = "today" if days == 0 else "tomorrow" if days == 1 else d.strftime("%a %b %-d")
        return {"date": row["date"], "hour": row["hour"], "days": days, "text": f"{label} {when}".strip()}
