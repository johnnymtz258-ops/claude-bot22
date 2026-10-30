"""Synthetic market data and fakes for DayBot tests: realistic-looking days built from a price path."""
from __future__ import annotations

import random

from daybot.clock import Market, at

DAY = "2026-10-21"          # a Wednesday
PREV = ["2026-09-%02d" % d for d in (8, 9, 10, 11, 14, 15, 16, 17, 18, 21, 22, 23, 24, 25, 28, 29, 30)] + \
       ["2026-10-%02d" % d for d in (1, 2, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 19, 20)]


def daily_history(days: list[str], close: float = 100.0, atr_pct: float = 2.0, volume: float = 5e6,
                  seed: int = 1) -> list[tuple]:
    """Prior daily bars ending at `close`, with a normal daily range of about atr_pct."""
    rnd = random.Random(seed)
    out, c = [], close
    rows = []
    for _ in days:
        rows.append(c)
        c = c / (1 + rnd.uniform(-0.01, 0.01))
    rows = rows[::-1]
    rng = close * atr_pct / 100
    for day, c in zip(days, rows):
        o = c * (1 + rnd.uniform(-0.004, 0.004))
        hi = max(o, c) + rng * rnd.uniform(0.3, 0.6)
        lo = min(o, c) - rng * rnd.uniform(0.3, 0.6)
        out.append((at(day, "00:00"), o, hi, lo, c, volume * rnd.uniform(0.8, 1.2)))
    last = out[-1]
    out[-1] = (last[0], last[1], max(last[2], close), min(last[3], close), close, last[5])
    return out


def path_bars(day: str, points: list[tuple[str, float]], *, start: str = "09:30", end: str = "16:00",
              noise: float = 0.0004, vol: float = 20_000, vol_points: dict | None = None, seed: int = 7,
              step: int = 60) -> list[tuple]:
    """Minute bars following a piecewise-linear path through (HH:MM, price) points. vol_points: {HH:MM: multiplier}
    from that minute on."""
    rnd = random.Random(seed)
    t0, t1 = at(day, start), at(day, end)
    keys = [(at(day, hm), p) for hm, p in points]
    mults = sorted(((at(day, hm), m) for hm, m in (vol_points or {}).items()))
    out, prev = [], None
    for t in range(t0, t1, step):
        # price on the path at the bar's end
        p = keys[-1][1]
        for (ta, pa), (tb, pb) in zip(keys, keys[1:]):
            if ta <= t + step <= tb:
                p = pa + (pb - pa) * (t + step - ta) / (tb - ta)
                break
        if t + step < keys[0][0]:
            p = keys[0][1]
        o = prev if prev is not None else p
        c = p * (1 + rnd.uniform(-noise, noise))
        hi = max(o, c) * (1 + rnd.uniform(0, noise))
        lo = min(o, c) * (1 - rnd.uniform(0, noise))
        m = 1.0
        for tm, mm in mults:
            if t >= tm:
                m = mm
        out.append((t, o, hi, lo, c, vol * m * rnd.uniform(0.85, 1.15)))
        prev = c
    return out


def premarket(day: str, price: float, high: float | None = None, low: float | None = None, vol: float = 2000):
    hi, lo = high or price * 1.003, low or price * 0.997
    mid = (hi + lo) / 2
    return path_bars(day, [("04:00", mid), ("06:00", hi), ("07:30", lo), ("09:29", price)], start="04:00",
                     end="09:30", vol=vol, noise=0.0002)


def orb_day(day: str = DAY, base: float = 100.0, up: bool = True) -> list[tuple]:
    """Opening range 99.6-100.6 for 15 min, quiet, then a high-volume break at 10:00 that runs to +4."""
    s = 1 if up else -1
    pts = [("09:30", base), ("09:36", base + 0.5 * s), ("09:41", base - 0.4 * s), ("09:45", base + 0.3 * s),
           ("09:55", base + 0.3 * s), ("10:00", base + 0.35 * s), ("10:05", base + 1.1 * s), ("10:40", base + 2.6 * s),
           ("11:30", base + 3.8 * s), ("15:59", base + 3.9 * s)]
    return premarket(day, base) + path_bars(day, pts, vol_points={"10:00": 2.5, "10:10": 1.6, "11:30": 0.8})


def flag_day(day: str = DAY, base: float = 100.0, up: bool = True) -> list[tuple]:
    """First leg 100 -> 103 by 10:15 on volume, a 40% pullback on light volume above VWAP, the flag breaks 10:50."""
    s = 1 if up else -1
    pts = [("09:30", base), ("09:45", base + 1.0 * s), ("10:15", base + 3.0 * s), ("10:25", base + 2.4 * s),
           ("10:40", base + 1.85 * s), ("10:45", base + 1.9 * s), ("10:50", base + 2.1 * s), ("10:55", base + 2.6 * s),
           ("11:30", base + 3.4 * s), ("12:30", base + 4.6 * s), ("15:59", base + 4.8 * s)]
    return premarket(day, base) + path_bars(day, pts, vol_points={"09:30": 2.0, "10:15": 0.5, "10:50": 1.4})


def gap_day(day: str = DAY, pdc: float = 100.0, up: bool = True) -> list[tuple]:
    """Gaps 5% on news, premarket high 105.6; opens 105, holds, breaks the premarket high at 9:45 on volume."""
    s = 1 if up else -1
    o = pdc * (1 + 0.05 * s)
    pmh = pdc * (1 + 0.056 * s)
    pts = [("09:30", o), ("09:35", o + 0.2 * s), ("09:40", o + 0.25 * s), ("09:45", o + 0.35 * s),
           ("09:50", pmh + 0.35 * s), ("10:10", pmh + 1.4 * s), ("10:40", pmh + 2.6 * s), ("15:59", pmh + 2.8 * s)]
    pre = premarket(day, o, high=max(pmh, o * 0.99) if up else None, low=None if up else min(pmh, o * 1.01),
                    vol=30_000)
    return pre + path_bars(day, pts, vol=60_000, vol_points={"09:45": 2.0, "10:10": 1.2})


def vwap_day(day: str = DAY, base: float = 100.0, up: bool = True) -> list[tuple]:
    """Green day trending up, a 15-minute dip under VWAP around 11:00, reclaimed on volume, then higher."""
    s = 1 if up else -1
    pts = [("09:30", base + 0.5 * s), ("10:00", base + 1.2 * s), ("10:30", base + 1.6 * s), ("10:45", base + 1.4 * s),
           ("10:55", base + 0.95 * s), ("11:05", base + 0.9 * s), ("11:10", base + 1.0 * s), ("11:15", base + 1.4 * s),
           ("12:00", base + 2.3 * s), ("13:30", base + 3.1 * s), ("15:59", base + 3.3 * s)]
    return premarket(day, base + 0.5 * s) + path_bars(day, pts, vol_points={"10:45": 0.9, "11:10": 1.8, "11:20": 1.0})


def chop_day(day: str = DAY, base: float = 100.0, seed: int = 3) -> list[tuple]:
    rnd = random.Random(seed)
    pts = [("09:30", base)]
    for hm in ("10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30", "14:00", "14:30", "15:00",
               "15:30", "15:59"):
        pts.append((hm, base + rnd.uniform(-0.3, 0.3)))
    return premarket(day, base) + path_bars(day, pts, noise=0.0008)


def market() -> Market:
    m = Market()
    return m


def five(bars: list[tuple]) -> list[tuple]:
    from daybot.bars import resample
    return resample(bars, 5)


# ---- fakes ---------------------------------------------------------------------------------------------------------
import datetime as _dt


from daybot.bars import resample as _resample


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeAlpaca:
    """Serves synthetic bars as if it were Alpaca, as of `self.now` (set by the test)."""

    def __init__(self, minute: dict | None = None, daily: dict | None = None, *, chain=None, contracts=None,
                 news=None, movers=None, actives=None, now: float | None = None, assets=None):
        self.minute = minute or {}
        self.daily_store = daily or {}
        self.feed = "iex"
        self.realtime_sip = False
        self.session = object()
        self.last_error = ""
        self.requests = 0
        self.chain_store = chain or {}
        self.contracts_store = contracts or {}
        self.news_store = news or {}
        self.movers_data = movers or {"gainers": [], "losers": []}
        self.actives = actives or []
        self.now = now or at(DAY, "16:00")
        self.calls: list[tuple] = []
        self.assets_list = [{"symbol": a, "exchange": "NASDAQ", "tradable": True, "status": "active",
                             "name": f"{a} Inc"} for a in (assets or [])]

    @property
    def ok(self) -> bool:
        return True

    async def bars(self, symbols, timeframe, start, end=None, feed=None, adjustment=None):
        end = min(end or self.now, self.now)
        self.calls.append(("bars", tuple(symbols), timeframe, feed))
        out = {}
        for s in symbols:
            if timeframe == "1Day":
                rows = [b for b in self.daily_store.get(s, []) if start <= b[0] < end]
            else:
                rows = [b for b in self.minute.get(s, []) if start <= b[0] and b[0] + 60 <= end]
                step = {"1Min": 1, "5Min": 5, "15Min": 15, "30Min": 30}[timeframe]
                if step > 1:
                    rows = _resample(rows, step)
            if rows:
                out[s] = rows
        return out

    def _last(self, s):
        rows = [b for b in self.minute.get(s, []) if b[0] + 60 <= self.now]
        return rows[-1] if rows else None

    async def snapshots(self, symbols, feed=None):
        out = {}
        for s in symbols:
            b = self._last(s)
            if not b:
                continue
            vol = sum(x[5] for x in self.minute.get(s, []) if x[0] + 60 <= self.now and x[0] >= at(DAY, "09:30"))
            out[s] = {"latestTrade": {"p": b[4], "t": _iso(b[0] + 59)},
                      "latestQuote": {"bp": round(b[4] * 0.9998, 2), "ap": round(b[4] * 1.0002, 2)},
                      "dailyBar": {"v": vol, "c": b[4]}, "minuteBar": {"c": b[4]}}
        return out

    async def movers(self, top=50):
        return self.movers_data

    async def most_actives(self, top=50):
        return self.actives

    async def news(self, symbols, start, limit=50):
        rows = []
        for s in symbols:
            rows.extend(self.news_store.get(s, []))
        return rows

    async def option_chain(self, underlying, expiry=None, kind=None, strike_lo=None, strike_hi=None, exp_to=None):
        return dict(self.chain_store.get(underlying, {}))

    async def option_snapshots(self, symbols):
        out = {}
        for chain in self.chain_store.values():
            for s in symbols:
                if s in chain:
                    out[s] = chain[s]
        return out

    async def option_contracts(self, underlying, exp_from=None, exp_to=None, kind=None, strike_lo=None, strike_hi=None):
        return list(self.contracts_store.get(underlying, []))

    async def calendar(self, start, end):
        return []

    # ---- crypto (set crypto_daily / crypto_minute: {coin: bars}) --------------------------------------------------
    crypto_daily: dict = {}
    crypto_minute: dict = {}
    crypto_list: list = []

    async def crypto_bars(self, coins, timeframe, start, end=None):
        end = min(end or self.now, self.now)
        store = self.crypto_daily if timeframe == "1Day" else self.crypto_minute
        self.calls.append(("crypto_bars", tuple(coins), timeframe))
        out = {}
        for c in coins:
            rows = [b for b in store.get(c, []) if start <= b[0] <= end]
            if rows:
                out[c] = rows
        return out

    async def crypto_snapshots(self, coins):
        out = {}
        for c in coins:
            rows = [b for b in self.crypto_minute.get(c, []) if b[0] + 60 <= self.now] or \
                [b for b in self.crypto_daily.get(c, []) if b[0] <= self.now]
            if rows:
                out[c] = {"latestTrade": {"p": rows[-1][4]}, "prevDailyBar": {"c": rows[-2][4] if len(rows) > 1 else rows[-1][4]}}
        return out

    async def crypto_assets(self):
        return list(self.crypto_list or self.crypto_daily)

    async def assets(self):
        return list(self.assets_list)

    async def clock(self):
        return {}


def crypto_days(start_ts: int, n: int, start: float = 100.0, seed: int = 1, regime: int = 120, drift: float = 0.004,
                vol: float = 0.03) -> list[tuple]:
    """Daily crypto candles: alternating up and down regimes with noise."""
    import math
    rnd = random.Random(seed)
    out, c = [], start
    for i in range(n):
        o = c
        c = c * math.exp((drift if (i // regime) % 2 == 0 else -drift) + rnd.gauss(0, vol))
        out.append((start_ts + i * 86400, o, max(o, c), min(o, c), c, 0.0))
    return out


def btc_minutes(start_ts: int, minutes: int, start: float = 70_000.0, seed: int = 2, vol: float = 0.0006,
                momentum: float = 0.0) -> list[tuple]:
    """1-minute Bitcoin candles; momentum > 0 makes each 15-minute window lean the way the last one went."""
    import math
    rnd = random.Random(seed)
    out, c, last_dir, w_start, w_open = [], start, 0, None, start
    for i in range(minutes):
        t = start_ts + i * 60
        if t % 900 == 0:
            if w_start is not None:
                last_dir = 1 if c >= w_open else -1
            w_start, w_open = t, c
        o = c
        c = c * math.exp(rnd.gauss(momentum * last_dir * vol, vol))
        out.append((t, o, max(o, c), min(o, c), c, 1.0))
    return out


def etf_world(end_day: str = "2026-10-23", years: float = 10.8, seed: int = 3) -> dict[str, list[tuple]]:
    """Daily bars for the Invest lab's ETFs: a market with bull runs, a crash and a bear market; bonds and gold that
    behave differently, T-bills that creep up, and 3× Nasdaq that compounds the daily moves."""
    import datetime as dt
    import math
    rnd = random.Random(seed)
    end = dt.date.fromisoformat(end_day)
    days, d = [], end - dt.timedelta(days=int(years * 365.25))
    while d <= end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    n = len(days)

    def regime(i):
        f = i / n
        if f < 0.22:
            return 0.0006, 0.007                         # bull
        if f < 0.27:
            return -0.006, 0.02                          # crash
        if f < 0.55:
            return 0.0008, 0.008                         # bull
        if f < 0.68:
            return -0.0012, 0.012                        # bear
        return 0.0007, 0.0075                            # bull
    mkt = []
    for i in range(n):
        mu, sd = regime(i)
        mkt.append(rnd.gauss(mu, sd))
    spec = {"SPY": (1.0, 0.0, 0.002), "QQQ": (1.25, 0.0001, 0.004), "IWM": (1.1, 0.0, 0.006), "EFA": (0.85, -0.0001, 0.005),
            "EEM": (0.9, -0.0002, 0.007), "VNQ": (0.8, 0.0, 0.006), "AGG": (-0.05, 0.0002, 0.002),
            "TLT": (-0.15, 0.0002, 0.006), "GLD": (0.05, 0.0002, 0.008)}
    for sec, beta in zip(("XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB"),
                         (1.2, 1.1, 1.0, 0.7, 1.05, 0.5, 1.0, 0.45, 0.95)):
        spec[sec] = (beta, rnd.uniform(-0.0001, 0.0002), 0.005)
    rets: dict[str, list[float]] = {}
    for sym, (beta, drift, idio) in spec.items():
        rets[sym] = [beta * mkt[i] + drift + rnd.gauss(0, idio) for i in range(n)]
    rets["TQQQ"] = [max(-0.9, 3 * r) - 0.00008 for r in rets["QQQ"]]
    rets["BIL"] = [0.00008 for _ in range(n)]
    out = {}
    for sym, rs in rets.items():
        px, rows = 100.0, []
        for i, day in enumerate(days):
            o = px * (1 + rnd.gauss(0, 0.001))
            px = px * math.exp(rs[i]) if sym != "BIL" else px * (1 + rs[i])
            rows.append((at(day, "00:00"), o, max(o, px) * 1.001, min(o, px) * 0.999, px, 1e6))
        out[sym] = rows
    return out


class FakeSnap:
    def __init__(self, total: float = 25_000.0):
        self.fills: dict[str, list] = {"acc1": []}
        self.accounts_list = [{"id": "acc1", "name": "Individual", "institution": "Robinhood", "number": "****1234",
                               "margin": "margin", "total": total}]
        self.positions_list: list = []
        self.history_list: list = []
        self.last_error = ""
        self.requests = 0

    @property
    def ok(self) -> bool:
        return True

    async def accounts(self):
        return self.accounts_list

    async def balances(self, account_id):
        return {"cash": 10_000.0, "buying_power": 50_000.0}

    async def connections(self):
        return []

    async def positions(self, account_id):
        return list(self.positions_list)

    async def recent_fills(self, account_id):
        return list(self.fills.get(account_id, []))

    async def history(self, account_id, start_day, end_day):
        return list(self.history_list)

    async def connect_link(self, reconnect="", broker=""):
        return "https://connect.snaptrade.example/abc"


class Notes:
    """Captures what the bot would send to Telegram."""

    def __init__(self):
        self.sent: list[dict] = []
        self.feed = []

    async def __call__(self, text, *, buttons=None, silent=False, kind="", symbol="", photo=None, reply_to=None):
        self.sent.append({"text": text, "kind": kind, "silent": silent, "symbol": symbol, "photo": photo,
                          "buttons": buttons})
        return len(self.sent)

    def kinds(self):
        return [m["kind"] for m in self.sent]

    def last(self, kind):
        return next((m for m in reversed(self.sent) if m["kind"] == kind), None)


def stock_fill(order_id, side, qty, px, ts, symbol="TEST", account="acc1"):
    return {"account_id": account, "order_id": order_id, "ts": int(ts), "side": side, "effect": "", "qty": qty,
            "price": px, "fee": 0.0, "source": "order", "kind": "stock", "symbol": symbol, "underlying": symbol,
            "option_type": "", "strike": 0.0, "expiry": "", "multiplier": 1.0}


def option_snapshot(bid, ask, delta, gamma=0.05, theta=-0.08, iv=0.45):
    return {"latestQuote": {"bp": bid, "ap": ask}, "latestTrade": {"p": (bid + ask) / 2},
            "greeks": {"delta": delta, "gamma": gamma, "theta": theta, "vega": 0.1}, "impliedVolatility": iv,
            "dailyBar": {"v": 1200}}


def make_app(minute=None, daily=None, *, now=None, total=25_000.0, charts=None, **fake_kw):
    from daybot.app import App
    from daybot.config import Config
    from daybot.db import Database
    import tempfile
    from pathlib import Path
    cfg = Config(alpaca_key="k", alpaca_secret="s", snaptrade_client_id="c", snaptrade_consumer_key="ck",
                 telegram_chat_id="1", dashboard_enabled=False)
    cfg.state_dir = Path(tempfile.mkdtemp(prefix="daybot-test-"))
    fake = FakeAlpaca(minute, daily, now=now, **fake_kw)
    snap = FakeSnap(total)
    notes = Notes()
    app = App(cfg, None, db=Database(":memory:"), telegram=None, alpaca=fake, snap=snap, charts=charts or False,
              notify=notes)
    app.fake, app.notes = fake, notes
    return app
