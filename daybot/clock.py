"""New York market time: sessions, holidays, early closes.

Alpaca's market calendar is the source of truth when it's reachable; the built-in NYSE holiday list keeps the bot
right when it isn't.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# NYSE full-day closures and 1pm early closes (fallback when Alpaca's calendar can't be read)
HOLIDAYS = {
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04",
    "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06",
    "2027-11-25", "2027-12-24",
}
EARLY_CLOSES = {"2025-07-03", "2025-11-28", "2025-12-24", "2026-11-27", "2026-12-24", "2027-11-26"}

PRE_OPEN = "04:00"
OPEN = "09:30"
CLOSE = "16:00"
POST_CLOSE = "20:00"


def et(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, ET)


def day_of(ts: float) -> str:
    return et(ts).strftime("%Y-%m-%d")


def at(day: str, hhmm: str) -> int:
    """Unix time of a New York wall-clock time on a day ('2026-10-21', '09:30')."""
    d = dt.date.fromisoformat(day)
    h, m = (int(x) for x in hhmm.split(":"))
    return int(dt.datetime(d.year, d.month, d.day, h, m, tzinfo=ET).timestamp())


def hhmm(ts: float) -> str:
    return et(ts).strftime("%H:%M")


def clock_text(ts: float) -> str:
    """'9:41am ET' (New York time — what market hours are quoted in)."""
    t = et(ts)
    return t.strftime("%I:%M%p").lstrip("0").lower() + " ET"


def minute_of_day(ts: float) -> int:
    t = et(ts)
    return t.hour * 60 + t.minute


class Market:
    """Trading days and session times. `days` maps 'YYYY-MM-DD' -> (open 'HH:MM', close 'HH:MM') from Alpaca."""

    def __init__(self):
        self.days: dict[str, tuple[str, str]] = {}

    def load_calendar(self, rows: list[dict]) -> None:
        for r in rows or []:
            day = str(r.get("date") or "")[:10]
            if day:
                self.days[day] = (str(r.get("open") or OPEN)[:5], str(r.get("close") or CLOSE)[:5])

    def is_trading_day(self, day: str) -> bool:
        if day in self.days:
            return True
        if self.days and self._covered(day):
            return False                       # Alpaca's calendar covers this date and it isn't in it
        d = dt.date.fromisoformat(day)
        return d.weekday() < 5 and day not in HOLIDAYS

    def _covered(self, day: str) -> bool:
        keys = sorted(self.days)
        return bool(keys) and keys[0] <= day <= keys[-1]

    def hours(self, day: str) -> tuple[str, str]:
        if day in self.days:
            return self.days[day]
        return OPEN, ("13:00" if day in EARLY_CLOSES else CLOSE)

    def open_ts(self, day: str) -> int:
        return at(day, self.hours(day)[0])

    def close_ts(self, day: str) -> int:
        return at(day, self.hours(day)[1])

    def session(self, ts: float) -> str:
        """closed | pre | open | post for a moment in time."""
        day = day_of(ts)
        if not self.is_trading_day(day):
            return "closed"
        if ts < at(day, PRE_OPEN):
            return "closed"
        if ts < self.open_ts(day):
            return "pre"
        if ts < self.close_ts(day):
            return "open"
        if ts < at(day, POST_CLOSE):
            return "post"
        return "closed"

    def prev_day(self, day: str, n: int = 1) -> str:
        d = dt.date.fromisoformat(day)
        while n > 0:
            d -= dt.timedelta(days=1)
            if self.is_trading_day(d.isoformat()):
                n -= 1
        return d.isoformat()

    def next_day(self, day: str) -> str:
        d = dt.date.fromisoformat(day) + dt.timedelta(days=1)
        while not self.is_trading_day(d.isoformat()):
            d += dt.timedelta(days=1)
        return d.isoformat()

    def trading_days_back(self, day: str, n: int) -> list[str]:
        """The n trading days before `day` (oldest first), not including it."""
        out, d = [], dt.date.fromisoformat(day)
        while len(out) < n:
            d -= dt.timedelta(days=1)
            if self.is_trading_day(d.isoformat()):
                out.append(d.isoformat())
        return out[::-1]

    def minutes_left(self, ts: float) -> float:
        return (self.close_ts(day_of(ts)) - ts) / 60
