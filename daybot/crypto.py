"""Crypto: a trend-following plan for the coins Robinhood sells, from "Catching Crypto Trends: A Tactical Approach
for Bitcoin and Altcoins" (Zarattini, Pagani & Barbon, 2025) — re-tested here on Alpaca's free crypto prices with
Robinhood's ~1% cost on every buy and every sell.

Why trends and not crypto day trades: studies of intraday crypto rules find an edge of a few hundredths of a percent
a day, gone after costs — and Robinhood's spread is about 1% each way. A trend plan trades a few times a month.

The rules, for each coin, once a day after the daily candle closes:
  Signal  nine Donchian-channel models with lookbacks of 5, 10, 20, 30, 60, 90, 150, 250 and 360 days. Each one buys
          when the close is above every close of its lookback, and sells when the close falls under its trailing stop:
          the middle of the lookback's highest and lowest close, which only ever moves up. The coin's trend score is
          how many of the nine are in (0-9).
  Size    each coin gets an equal slice of the money. Inside its slice it holds score/9 × (target volatility ÷ the
          coin's 90-day volatility), at most 2× the slice (the paper's cap) and never more than 100% overall (no
          leverage). A calm coin in a strong trend gets more, a wild one less; no trends anywhere = all cash.
  Trade   at the next price after the candle closes; a coin already held is only re-sized when it's 50%+ off its
          target (fewer trades: the paper's way to keep costs down).
The crypto autopilot holds that plan with pretend money, so you can see what it does before you follow it.

Tested before shipping on Coin Metrics daily prices, each month's real top-10 coins (dead ones like LUNA and FTT
included, so no hindsight), mid-2020 to May 2026, 1% cost per buy and sell, 50% target: +173% (18% a year), worst drop
-34%, Sharpe 0.95 — Bitcoin alone +767%, worst drop -77%, Sharpe 0.92. In 2022 the plan lost 15%, Bitcoin 64%. At a
0.1%-cost exchange the same plan made +268%: Robinhood's spread costs about a third of the gains.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import deque

from .bars import C, O, T
from .clock import clock_text, et
from .util import dumps, esc, loads, money, num, pct, price as fmt_price

LOOKBACKS = (5, 10, 20, 30, 60, 90, 150, 250, 360)
VOL_DAYS = 90               # realized volatility window (daily returns)
SLEEVE_CAP = 2.0            # a coin may hold at most 2× its equal slice (the paper's 200% cap)
DRIFT = 0.5                 # re-size a held coin only when it's 50%+ away from its target (tested best at 1% costs)
MIN_TRADE = 0.001           # ignore re-sizes smaller than 0.1% of the account
DAY = 86400
HISTORY_START = "2020-01-01"
BT_VERSION = "1"

# Biggest coins first (rough market size), only ones Robinhood sells in the US. The live list is this, cut to the
# coins Alpaca has prices for. Stablecoins and gold tokens are never traded.
RANKED = ("BTC", "ETH", "XRP", "SOL", "DOGE", "ADA", "LINK", "AVAX", "XLM", "BCH", "SUI", "HBAR", "LTC", "SHIB",
          "DOT", "UNI", "PEPE", "AAVE", "ETC", "ONDO", "ARB", "TRUMP", "BONK", "CRV", "WIF", "XTZ", "COMP", "GRT",
          "BAT", "MKR", "SUSHI", "YFI")
STABLE = {"USDT", "USDC", "DAI", "PYUSD", "USDG", "USDP", "TUSD", "BUSD", "FDUSD", "EURC", "PAXG", "XAUT"}

STATUS = {"proven": "✅ worth following", "promising": "🟡 made money, not steadily", "negative": "🔴 loses",
          "thin": "⚪ too little history", "untested": "⚪ not tested yet"}


# ---- the signal -------------------------------------------------------------------------------------------------
def prior_extremes(xs: list[float], n: int) -> tuple[list, list]:
    """For each day i: the highest and lowest of the n values before it (None until there are n)."""
    hi_q: deque = deque()
    lo_q: deque = deque()
    his, los = [], []
    for i, x in enumerate(xs):
        while hi_q and hi_q[0] < i - n:
            hi_q.popleft()
        while lo_q and lo_q[0] < i - n:
            lo_q.popleft()
        if i >= n:
            his.append(xs[hi_q[0]])
            los.append(xs[lo_q[0]])
        else:
            his.append(None)
            los.append(None)
        while hi_q and xs[hi_q[-1]] <= x:
            hi_q.pop()
        hi_q.append(i)
        while lo_q and xs[lo_q[-1]] >= x:
            lo_q.pop()
        lo_q.append(i)
    return his, los


def trend_flags(closes: list[float], n: int) -> list[bool]:
    """One Donchian model: in a position at each day's close? Buys a close above the prior n closes; sells a close
    under the trailing stop (the channel's middle, ratcheting up only)."""
    his, los = prior_extremes(closes, n)
    out, inside, stop = [], False, 0.0
    for c, hi, lo in zip(closes, his, los):
        if hi is None:
            out.append(False)
            continue
        mid = (hi + lo) / 2
        if inside:
            if c < stop:
                inside = False
            else:
                stop = max(stop, mid)
        elif c > hi:
            inside, stop = True, mid
        out.append(inside)
    return out


def realized_vol(closes: list[float], days: int = VOL_DAYS) -> list[float | None]:
    """Annualized volatility of the last `days` daily returns, for each day (None until there are 30)."""
    rets = [0.0] + [math.log(closes[i] / closes[i - 1]) if closes[i] > 0 and closes[i - 1] > 0 else 0.0
                    for i in range(1, len(closes))]
    out: list[float | None] = []
    s = s2 = 0.0
    for i in range(len(closes)):
        if i >= 1:
            s += rets[i]
            s2 += rets[i] ** 2
        if i - days >= 1:
            old = rets[i - days]
            s -= old
            s2 -= old ** 2
        k = min(i, days)
        if k < 30:
            out.append(None)
            continue
        var = max(0.0, (s2 - s * s / k) / (k - 1))
        out.append(math.sqrt(var * 365))
    return out


def coin_series(bars: list[tuple]) -> dict:
    """Everything the plan needs for one coin, day by day (completed daily bars only)."""
    closes = [num(b[C]) for b in bars]
    flags = {n: trend_flags(closes, n) for n in LOOKBACKS}
    on = [sum(1 for n in LOOKBACKS if flags[n][i]) for i in range(len(closes))]
    return {"ts": [int(b[T]) for b in bars], "open": [num(b[O]) or num(b[C]) for b in bars], "close": closes,
            "on": on, "score": [k / len(LOOKBACKS) for k in on], "vol": realized_vol(closes),
            "flags": {n: flags[n][-1] if flags[n] else False for n in LOOKBACKS}}


def weight(score: float, vol: float | None, n_coins: int, target_vol_pct: float) -> float:
    """Share of the account for one coin: its slice × trend score × volatility scaling (capped at 2× the slice)."""
    if score <= 0 or not vol or vol <= 0 or n_coins <= 0:
        return 0.0
    return score * min(SLEEVE_CAP, target_vol_pct / 100 / vol) / n_coins


def cap_gross(weights: dict) -> dict:
    total = sum(weights.values())
    return {k: v / total for k, v in weights.items()} if total > 1 else dict(weights)


# ---- trading ------------------------------------------------------------------------------------------------------
def plan_trades(qty: dict, prices: dict, equity: float, targets: dict, drift: float = DRIFT,
                min_trade: float = MIN_TRADE) -> list[dict]:
    """What to buy and sell to move from the coins held to the targets. Sells first, so their cash pays the buys."""
    sells, buys = [], []
    for coin in sorted(set(qty) | set(targets)):
        px = num(prices.get(coin))
        if px <= 0 or equity <= 0:
            continue
        cur = num(qty.get(coin)) * px
        tgt = num(targets.get(coin)) * equity
        if tgt <= 0:
            if cur > 0:
                sells.append({"coin": coin, "side": "sell", "value": cur, "all": True, "reason": "trend off"})
        elif cur <= 0:
            buys.append({"coin": coin, "side": "buy", "value": tgt, "all": False, "reason": "trend on"})
        elif abs(tgt - cur) > drift * max(tgt, cur) and abs(tgt - cur) >= min_trade * equity:
            side = "buy" if tgt > cur else "sell"
            row = {"coin": coin, "side": side, "value": abs(tgt - cur), "all": False,
                   "reason": "bigger" if side == "buy" else "smaller"}
            (buys if side == "buy" else sells).append(row)
    return sells + buys


def execute(book: dict, trades: list[dict], prices: dict, cost_pct: float) -> list[dict]:
    """Fill trades against a book {"cash": $, "qty": {coin: units}}, paying cost_pct on each buy and each sell.
    Buys never spend more cash than the book has."""
    cost = cost_pct / 100
    fills = []
    for t in trades:
        coin, px = t["coin"], num(prices.get(t["coin"]))
        have = num(book["qty"].get(coin))
        if px <= 0:
            continue
        if t["side"] == "sell":
            units = have if t["all"] else min(have, t["value"] / px)
            if units <= 0:
                continue
            value = units * px
            book["cash"] += value * (1 - cost)
            if have - units <= have * 1e-9:
                book["qty"].pop(coin, None)
            else:
                book["qty"][coin] = have - units
        else:
            value = min(t["value"], max(0.0, book["cash"]) / (1 + cost))
            if value <= 0:
                continue
            units = value / px
            book["cash"] -= value * (1 + cost)
            book["qty"][coin] = have + units
        fills.append({"coin": coin, "side": t["side"], "qty": units, "price": px, "value": value,
                      "cost": value * cost, "reason": t["reason"]})
    return fills


def equity_of(book: dict, prices: dict) -> float:
    return book["cash"] + sum(num(q) * num(prices.get(c)) for c, q in book["qty"].items())


def rebalance(book: dict, prices: dict, targets: dict, cost_pct: float) -> list[dict]:
    return execute(book, plan_trades(book["qty"], prices, equity_of(book, prices), targets), prices, cost_pct)


def targets_for(series: dict, index: dict, n_coins: int, target_vol: float) -> dict:
    """Target weights from each coin's signal at its index (coins without a signal today are left out)."""
    out = {}
    for coin, i in index.items():
        sr = series[coin]
        w = weight(sr["score"][i], sr["vol"][i], n_coins, target_vol)
        if w > 0:
            out[coin] = w
    return cap_gross(out)


# ---- measuring --------------------------------------------------------------------------------------------------------
def perf(points: list[tuple]) -> dict:
    """Total return, yearly growth, worst drop and Sharpe of a (ts, value) series."""
    vals = [num(v) for _, v in points]
    if len(vals) < 2 or vals[0] <= 0:
        return {"total": 0.0, "cagr": 0.0, "max_dd": 0.0, "sharpe": 0.0, "vol": 0.0, "days": 0}
    days = max(1.0, (points[-1][0] - points[0][0]) / DAY)
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1] > 0]
    mean = sum(rets) / len(rets) if rets else 0.0
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0.0
    peak, dd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1 if peak > 0 else 0.0)
    end = vals[-1] / vals[0]
    return {"total": end - 1, "cagr": (end ** (365 / days) - 1) if end > 0 else -1.0, "max_dd": dd,
            "sharpe": mean / sd * math.sqrt(365) if sd > 0 else 0.0, "vol": sd * math.sqrt(365), "days": int(days)}


def by_year(points: list[tuple]) -> dict:
    """Return per calendar year (UTC), each year measured from the last value of the year before."""
    out, base, cur_year, last = {}, None, None, None
    for ts, v in points:
        y = dt.datetime.fromtimestamp(ts, dt.timezone.utc).year
        if base is None:
            base, cur_year = v, y
        if y != cur_year:
            out[str(cur_year)] = last / base - 1 if base else 0.0
            base, cur_year = last, y
        last = v
    if cur_year is not None and base:
        out[str(cur_year)] = last / base - 1
    return out


def thin_out(points: list[tuple], n: int = 160) -> list[tuple]:
    step = max(1, -(-len(points) // n))
    out = points[::step]
    if points and out[-1] != points[-1]:
        out.append(points[-1])
    return out


def verdict(res: dict) -> str:
    s, b = res["strategy"], res["btc"]
    if s["days"] < 365:
        return "thin"
    if s["total"] > 0 and res["half1"] > 0 and res["half2"] > 0 and s["sharpe"] >= max(0.8, b["sharpe"] - 0.1) \
            and s["max_dd"] > b["max_dd"]:
        return "proven"
    return "promising" if s["total"] > 0 else "negative"


def backtest(bars_by_coin: dict, coins: list[str], target_vol: float, cost_pct: float, drift: float = DRIFT,
             min_trade: float = MIN_TRADE, start: int = 0, resize_days: int = 1, universe=None) -> dict:
    """Replay the plan day by day over all the history there is: decide after each daily close, trade at the next
    day's open, pay cost_pct on every buy and sell. Compared with simply holding Bitcoin over the same days.
    universe: optional ts -> set of coins tradable that day (research: each month's real top coins)."""
    coins = [c for c in coins if len(bars_by_coin.get(c) or []) > 40]
    if "BTC" not in coins:
        return {"error": "no Bitcoin history"}
    series = {c: coin_series(bars_by_coin[c]) for c in coins}
    index = {c: {ts: i for i, ts in enumerate(series[c]["ts"])} for c in coins}
    btc = series["BTC"]
    warm = 360 if len(btc["ts"]) >= 360 + 365 else VOL_DAYS
    if len(btc["ts"]) <= warm + 30:
        return {"error": "not enough Bitcoin history"}
    start_ts = max(btc["ts"][warm], start)
    all_ts = sorted({ts for c in coins for ts in series[c]["ts"] if ts >= start_ts})
    n = len(coins)
    book = {"cash": 1.0, "qty": {}}
    last_px: dict = {}
    flows = {c: 0.0 for c in coins}
    curve, btc_curve, gross, fills_n, costs, turnover = [], [], [], 0, 0.0, 0.0
    pending: dict | None = None
    for k, ts in enumerate(all_ts):
        if pending is not None:                                   # yesterday's decision, filled at today's open
            opens = {c: series[c]["open"][index[c][ts]] for c in set(pending) | set(book["qty"]) if ts in index[c]}
            marks = {**last_px, **opens}
            trades = [t for t in plan_trades(book["qty"], marks, equity_of(book, marks), pending, drift, min_trade)
                      if t["coin"] in opens and (k % resize_days == 0 or t["reason"] in ("trend on", "trend off"))]
            for f in execute(book, trades, opens, cost_pct):
                fills_n += 1
                costs += f["cost"]
                turnover += f["value"]
                flows[f["coin"]] += (f["value"] - f["cost"]) if f["side"] == "sell" else -(f["value"] + f["cost"])
            pending = None
        for c in coins:
            if ts in index[c]:
                last_px[c] = series[c]["close"][index[c][ts]]
        eq = equity_of(book, last_px)
        curve.append((ts, eq))
        btc_curve.append((ts, last_px.get("BTC") or (btc_curve[-1][1] if btc_curve else 0)))
        gross.append(sum(q * last_px.get(c, 0) for c, q in book["qty"].items()) / eq if eq > 0 else 0)
        members = universe(ts) if universe else None
        today = {c: index[c][ts] for c in coins if ts in index[c] and (members is None or c in members)}
        tg = targets_for(series, today, len(members) if members else n, target_vol)
        for c, q in book["qty"].items():                            # no candle today: leave it as it is
            if c not in today and ts not in index[c] and eq > 0:
                tg[c] = q * last_px.get(c, 0) / eq
        pending = tg
    s = perf(curve)
    b = perf(btc_curve)
    mid = len(curve) // 2
    per_coin = {c: flows[c] + num(book["qty"].get(c)) * last_px.get(c, 0) for c in coins}
    years_s, years_b = by_year(curve), by_year(btc_curve)
    b0 = btc_curve[0][1] or 1
    res = {"version": BT_VERSION, "coins": coins, "start": curve[0][0], "end": curve[-1][0], "strategy": s, "btc": b,
           "half1": curve[mid][1] / curve[0][1] - 1, "half2": curve[-1][1] / curve[mid][1] - 1,
           "trades": fills_n, "costs": costs, "turnover": turnover, "exposure": sum(gross) / len(gross),
           "per_coin": dict(sorted(per_coin.items(), key=lambda kv: -kv[1])),
           "years": {y: {"plan": years_s[y], "btc": years_b.get(y, 0.0)} for y in years_s},
           "curve": [(ts, round(v * 100, 2)) for ts, v in thin_out(curve)],
           "btc_curve": [(ts, round(v / b0 * 100, 2)) for ts, v in thin_out(btc_curve)],
           "target_vol": target_vol, "cost_pct": cost_pct}
    res["status"] = verdict(res)
    return res


# ---- words ----------------------------------------------------------------------------------------------------------
VERDICT = {
    "proven": "✅ Worth following: it made money in both halves of the test, earned as much per unit of risk as "
              "Bitcoin, and fell far less. Expect it to lag Bitcoin in big bull runs — it's built to dodge crashes.",
    "promising": "🟡 It made money overall, but not steadily enough to trust yet. Follow it small, or watch the "
                 "autopilot first.",
    "negative": "🔴 It lost money after costs in this test. Keep real money out; the autopilot keeps measuring it.",
    "thin": "⚪ Less than a year of prices — too short to judge.",
}


def _day(ts: float) -> str:
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y-%m-%d")


def _month(ts: float) -> str:
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%b %Y")


def _p(x: float, digits: int = 0) -> str:
    return pct(num(x) * 100, digits)


def backtest_lines(res: dict | None) -> list[str]:
    if not res or res.get("error"):
        return [f"Not tested yet{(' (' + esc(res['error']) + ')') if res and res.get('error') else ''}."]
    s, b = res["strategy"], res["btc"]
    lines = [f"Test: {_month(res['start'])} – {_month(res['end'])} · {len(res['coins'])} coins · "
             f"{res['cost_pct']:g}% cost on every buy and sell included",
             f"Trend plan: <b>{_p(s['total'])}</b> ({_p(s['cagr'])} a year) · worst drop {_p(s['max_dd'])} · "
             f"Sharpe {s['sharpe']:.2f}",
             f"Just holding Bitcoin: {_p(b['total'])} ({_p(b['cagr'])} a year) · worst drop {_p(b['max_dd'])} · "
             f"Sharpe {b['sharpe']:.2f}"]
    if res.get("years"):
        lines.append("Each year: " + " · ".join(f"{y} {_p(v['plan'])} (BTC {_p(v['btc'])})"
                                                for y, v in res["years"].items()))
    lines.append(f"On average {res['exposure'] * 100:.0f}% of the money was in coins; the rest waited in cash. "
                 f"{res['trades']:,} trades.")
    return lines


# ---- live --------------------------------------------------------------------------------------------------------------
class Crypto:
    """The live crypto plan: prices, the daily signal, the crypto autopilot and the backtest. Runs around the clock."""

    def __init__(self, cfg, db, alpaca, notify):
        self.cfg, self.db, self.alpaca, self.notify = cfg, db, alpaca, notify
        self.coins: list[str] = []
        self.bars: dict[str, list] = {}
        self.snaps: dict[str, dict] = {}
        self.series: dict[str, dict] = {}
        self._series_key = None
        self._coins_for = 0
        self._last: dict = {}
        self.last_error = ""
        self.last_tick = 0.0

    @property
    def on(self) -> bool:
        return self.cfg.flag("CRYPTO")

    def every(self, name: str, seconds: float, now: float) -> bool:
        if now - self._last.get(name, 0) >= seconds:
            self._last[name] = now
            return True
        return False

    # ---- data ----------------------------------------------------------------------------------------------------
    async def load_coins(self, now: float) -> None:
        avail: set = set()
        try:
            avail = set(await self.alpaca.crypto_assets())
        except Exception as exc:                   # the fixed list still works without it
            self.last_error = f"coin list: {exc}"
        want = int(self.cfg.get("CRYPTO_COINS"))
        coins = [c for c in RANKED if c not in STABLE and (not avail or c in avail)][:want]
        if "BTC" not in coins:
            coins = ["BTC"] + coins[:max(0, want - 1)]
        self.coins, self._coins_for = coins, want

    @staticmethod
    def _merge(old: list, new: list) -> list:
        rows = {b[T]: b for b in old}
        rows.update({b[T]: b for b in new})
        return [rows[k] for k in sorted(rows)]

    async def load_bars(self, now: float) -> None:
        need = [c for c in self.coins if c not in self.bars]
        have = [c for c in self.coins if c in self.bars]
        try:
            if need:
                start = dt.datetime.fromisoformat(HISTORY_START).replace(tzinfo=dt.timezone.utc).timestamp()
                got = await self.alpaca.crypto_bars(need, "1Day", start, now)
                for c in need:
                    self.bars[c] = sorted(got.get(c, []))
            if have:
                got = await self.alpaca.crypto_bars(have, "1Day", now - 6 * DAY, now)
                for c, rows in got.items():
                    if c in self.bars:
                        self.bars[c] = self._merge(self.bars[c], rows)
        except Exception as exc:
            self.last_error = f"prices: {exc}"

    async def load_snaps(self, now: float) -> None:
        try:
            self.snaps = await self.alpaca.crypto_snapshots(self.coins) or self.snaps
        except Exception as exc:
            self.last_error = f"live prices: {exc}"

    def completed(self, coin: str, now: float) -> list:
        return [b for b in self.bars.get(coin, []) if b[T] + DAY <= now]

    def compute(self, now: float) -> None:
        done = {c: self.completed(c, now) for c in self.coins}
        key = tuple((c, len(v), v[-1][T] if v else 0) for c, v in done.items())
        if key == self._series_key:
            return
        self.series = {c: coin_series(v) for c, v in done.items() if len(v) > 40}
        self._series_key = key

    def latest_bar(self) -> int:
        if "BTC" in self.series:
            return self.series["BTC"]["ts"][-1]
        return max((sr["ts"][-1] for sr in self.series.values()), default=0)

    def price(self, coin: str) -> float:
        s = self.snaps.get(coin) or {}
        p = num((s.get("latestTrade") or {}).get("p"))
        q = s.get("latestQuote") or {}
        if not p and num(q.get("bp")) > 0 and num(q.get("ap")) > 0:
            p = (num(q["bp"]) + num(q["ap"])) / 2
        if not p and self.bars.get(coin):
            p = num(self.bars[coin][-1][C])
        return p

    def change(self, coin: str) -> float | None:
        """% since the last daily close."""
        px = self.price(coin)
        prev = num(((self.snaps.get(coin) or {}).get("prevDailyBar") or {}).get("c"))
        if not prev and coin in self.series:
            prev = self.series[coin]["close"][-1]
        return (px / prev - 1) * 100 if px and prev else None

    def targets(self) -> dict:
        latest = self.latest_bar()
        index = {c: len(sr["ts"]) - 1 for c, sr in self.series.items() if sr["ts"][-1] >= latest - 2 * DAY}
        return targets_for(self.series, index, len(self.series), self.cfg.get("CRYPTO_TARGET_VOL"))

    # ---- the crypto autopilot's book ----------------------------------------------------------------------------
    def since(self) -> int:
        return int(num(self.db.get_meta("crypto_since", "0")))

    def start(self) -> float:
        return num(self.db.get_meta("crypto_start", "0")) or self.cfg.get("CRYPTO_AUTOPILOT_START")

    def book(self) -> dict:
        cash = self.db.get_meta("crypto_cash", "")
        return {"cash": num(cash) if cash != "" else self.start(),
                "qty": {r["coin"]: num(r["qty"]) for r in self.db.rows("select coin, qty from crypto_pos")}}

    def _save(self, book: dict, now: float) -> None:
        self.db.run("delete from crypto_pos")
        for coin, q in book["qty"].items():
            self.db.run("insert into crypto_pos(coin, qty, updated) values(?,?,?)", (coin, q, int(now)))
        self.db.set_meta("crypto_cash", book["cash"])

    def _begin(self, now: float) -> None:
        if not self.db.get_meta("crypto_since"):
            self.db.set_meta("crypto_since", int(now))
            self.db.set_meta("crypto_start", self.cfg.get("CRYPTO_AUTOPILOT_START"))
            self.db.set_meta("crypto_cash", self.cfg.get("CRYPTO_AUTOPILOT_START"))
            self.db.set_meta("crypto_btc0", self.price("BTC"))

    def reset(self, now: float, amount: float = 0.0) -> float:
        amount = amount if amount > 0 else self.cfg.get("CRYPTO_AUTOPILOT_START")
        self.db.run("delete from crypto_pos")
        self.db.run("delete from crypto_equity")
        for k, v in (("crypto_since", int(now)), ("crypto_start", amount), ("crypto_cash", amount),
                     ("crypto_btc0", self.price("BTC")), ("crypto_last_bar", 0)):
            self.db.set_meta(k, v)
        return amount

    def prices(self) -> dict:
        return {c: self.price(c) for c in set(self.series) | set(self.book()["qty"])}

    def equity(self) -> float:
        return equity_of(self.book(), self.prices())

    def btc_since(self) -> float | None:
        b0 = num(self.db.get_meta("crypto_btc0", "0"))
        px = self.price("BTC")
        return (px / b0 - 1) * 100 if b0 and px else None

    # ---- the daily step ---------------------------------------------------------------------------------------------
    async def step(self, now: float, bar_ts: int) -> list[dict]:
        """A daily candle closed: re-plan, and the autopilot trades the plan at the current price."""
        self._begin(now)
        first = not self.db.get_meta("crypto_step_ts")
        prices = self.prices()
        book = self.book()
        before = equity_of(book, prices)
        fills = rebalance(book, prices, self.targets(), self.cfg.get("CRYPTO_COST_PCT"))
        day = _day(bar_ts)
        for f in fills:
            on = self.series[f["coin"]]["on"][-1] if f["coin"] in self.series else 0
            self.db.insert("""insert into crypto_trades(ts, day, coin, side, qty, price, value, cost, reason, trend)
                              values(?,?,?,?,?,?,?,?,?,?)""",
                           (int(now), day, f["coin"], f["side"], f["qty"], f["price"], f["value"], f["cost"],
                            f["reason"], on))
        self._save(book, now)
        after = equity_of(book, prices)
        self.db.run("insert or replace into crypto_equity(day, ts, equity, btc) values(?,?,?,?)",
                    (day, int(now), after, prices.get("BTC") or 0))
        self.db.set_meta("crypto_last_bar", bar_ts)
        self.db.set_meta("crypto_step_ts", int(now))
        if self.cfg.flag("CRYPTO_ALERTS") and (fills or first):
            status = (self.bt() or {}).get("status", "untested")
            hour = et(now).hour
            await self.notify(self.update_text(fills, before, now, first), kind="CRYPTO",
                              silent=status == "negative" or not 8 <= hour < 22)
        return fills

    def update_text(self, fills: list[dict], before: float, now: float, first: bool = False) -> str:
        budget = self.cfg.get("CRYPTO_BUDGET")
        scale = budget / before if before > 0 else 0
        lines = [f"<b>🪙 Crypto plan</b> · {et(now).strftime('%a %b %-d')}, {clock_text(now)}"]
        if first:
            lines.append("The trend plan is on: it holds coins that are trending up and keeps the rest in cash. "
                         f"Dollar amounts are for {money(budget)} (CRYPTO_BUDGET).")
        small = 0
        for f in fills:
            usd = f["value"] * scale
            on = self.series[f["coin"]]["on"][-1] if f["coin"] in self.series else 0
            if usd < 1 and f["reason"] in ("bigger", "smaller"):
                small += 1
                continue
            if f["reason"] == "trend on":
                lines.append(f"🟢 <b>Buy {f['coin']}</b> — trending up ({on}/9) · {money(usd)}")
            elif f["reason"] == "trend off":
                lines.append(f"🔴 <b>Sell all {f['coin']}</b> — the trend broke · about {money(usd)}")
            elif f["reason"] == "bigger":
                lines.append(f"↗️ Add {money(usd)} of {f['coin']} — stronger trend or calmer price ({on}/9)")
            else:
                lines.append(f"↘️ Sell {money(usd)} of {f['coin']} — weaker trend or wilder price ({on}/9)")
        if small:
            lines.append(f"<i>{small} small re-size{'s' if small > 1 else ''} under $1 skipped — not worth it.</i>")
        if not fills and first:
            lines.append("Nothing is trending up right now, so the plan is 100% cash. You'll get a message the day "
                         "that changes.")
        held = self.board_rows()
        hold = [f"{r['coin']} {money(r['held_w'] * budget)}" for r in held if r["held_w"] > 0.0005]
        cash_w = max(0.0, 1 - sum(r["held_w"] for r in held))
        lines.append("Now: " + (" · ".join(hold) + " · " if hold else "") + f"cash {money(cash_w * budget)} "
                     f"({cash_w * 100:.0f}%)")
        lines.append(self.autopilot_line())
        res = self.bt()
        if res and res.get("status") == "negative":
            lines.append("🔴 This plan lost money in the backtest — pretend money only for now.")
        lines.append("/crypto for the full board")
        return "\n".join(lines)

    def autopilot_line(self) -> str:
        eq, start = self.equity(), self.start()
        btc = self.btc_since()
        since = self.since()
        return (f"🤖 Crypto autopilot: {money(eq)} ({pct((eq / start - 1) * 100 if start else 0)})"
                + (f" · Bitcoin {pct(btc)}" if btc is not None else "")
                + (f" since {et(since).strftime('%b %-d')}" if since else ""))

    # ---- the backtest ----------------------------------------------------------------------------------------------
    def bt(self) -> dict | None:
        return loads(self.db.get_meta("crypto_backtest"), None)

    def bt_key(self) -> str:
        return (f"{BT_VERSION}:{','.join(sorted(self.series))}:{self.cfg.get('CRYPTO_TARGET_VOL'):g}:"
                f"{self.cfg.get('CRYPTO_COST_PCT'):g}")

    def bt_stale(self, now: float) -> bool:
        res = self.bt()
        return not res or res.get("key") != self.bt_key() or now - num(res.get("ts")) > 7 * DAY

    async def run_backtest(self, now: float, announce: bool | None = None) -> dict:
        res = backtest({c: self.completed(c, now) for c in self.series}, list(self.series),
                       self.cfg.get("CRYPTO_TARGET_VOL"), self.cfg.get("CRYPTO_COST_PCT"))
        res["key"], res["ts"] = self.bt_key(), int(now)
        self.db.set_meta("crypto_backtest", dumps(res))
        if announce is None:
            announce = self.db.get_meta("crypto_bt_announced") != res["key"]
        if announce and not res.get("error"):
            self.db.set_meta("crypto_bt_announced", res["key"])
            await self.notify(self.backtest_text(res), kind="BACKTEST")
        return res

    def backtest_text(self, res: dict) -> str:
        return "\n".join(["<b>🪙 Crypto backtest</b> — the trend plan vs just holding Bitcoin", *backtest_lines(res),
                          "", VERDICT.get(res.get("status", ""), ""),
                          "<i>Coins were picked by today's size, so the test flatters them a little.</i>"])

    # ---- the loop -------------------------------------------------------------------------------------------------
    async def tick(self, now: float) -> None:
        if not self.on or not getattr(self.alpaca, "ok", False):
            return
        self.last_tick = now
        if not self.coins or self._coins_for != int(self.cfg.get("CRYPTO_COINS")) or self.every("coins", DAY, now):
            await self.load_coins(now)
        if any(c not in self.bars for c in self.coins) or self.every("bars", 600, now):
            await self.load_bars(now)
        if self.every("snaps", 120, now):
            await self.load_snaps(now)
        self.compute(now)
        if not self.series:
            return
        if self.bt_stale(now) and self.every("crypto_bt", 1800, now):
            await self.run_backtest(now)
        latest = self.latest_bar()
        if latest and latest > int(num(self.db.get_meta("crypto_last_bar", "0"))):
            await self.step(now, latest)

    # ---- views -------------------------------------------------------------------------------------------------------
    def board_rows(self) -> list[dict]:
        tg = self.targets()
        book = self.book()
        prices = self.prices()
        eq = equity_of(book, prices)
        budget = self.cfg.get("CRYPTO_BUDGET")
        rows = []
        for c in self.coins:
            sr = self.series.get(c)
            px = prices.get(c) or self.price(c)
            held = num(book["qty"].get(c)) * px
            rows.append({"coin": c, "price": px, "change": self.change(c), "on": sr["on"][-1] if sr else None,
                         "lookbacks": [n for n in LOOKBACKS if sr and sr["flags"].get(n)],
                         "vol": (sr["vol"][-1] or 0) * 100 if sr else None, "target_w": tg.get(c, 0.0),
                         "plan_usd": tg.get(c, 0.0) * budget, "held": held, "held_w": held / eq if eq > 0 else 0.0,
                         "history": len(sr["ts"]) if sr else 0})
        rank = {c: i for i, c in enumerate(self.coins)}
        return sorted(rows, key=lambda r: (-(r["on"] or 0), rank[r["coin"]]))

    def summary(self, now: float) -> dict:
        res = self.bt() or {}
        start, eq = self.start(), self.equity()
        b0 = num(self.db.get_meta("crypto_btc0", "0"))
        curve = [[r["ts"], round(r["equity"], 2), round(start * r["btc"] / b0, 2) if b0 and r["btc"] else None]
                 for r in self.db.rows("select ts, equity, btc from crypto_equity order by day")]
        if self.since():
            btc_px = self.price("BTC")
            curve.append([int(now), round(eq, 2), round(start * btc_px / b0, 2) if b0 and btc_px else None])
        recent = self.db.rows("select * from crypto_trades where ts>=? order by id desc limit 30", (self.since(),))
        rows = self.board_rows()
        latest = self.latest_bar()
        return {
            "on": self.on, "coins": len(self.coins), "budget": self.cfg.get("CRYPTO_BUDGET"), "board": rows,
            "trending": sum(1 for r in rows if r["target_w"] > 0), "in_coins": sum(r["target_w"] for r in rows),
            "last_bar": latest, "next_update": latest + 2 * DAY if latest else 0,
            "cost_pct": self.cfg.get("CRYPTO_COST_PCT"), "target_vol": self.cfg.get("CRYPTO_TARGET_VOL"),
            "autopilot": {"start": start, "equity": eq, "return_pct": (eq / start - 1) * 100 if start else 0.0,
                          "btc_pct": self.btc_since(), "since": self.since(), "cash": self.book()["cash"],
                          "trades": int(self.db.scalar("select count(*) from crypto_trades where ts>=?",
                                                       (self.since(),), default=0)),
                          "costs": num(self.db.scalar("select sum(cost) from crypto_trades where ts>=?",
                                                      (self.since(),), default=0)),
                          "curve": curve, "recent": recent},
            "backtest": {**{k: v for k, v in res.items() if k != "key"},
                         "verdict": VERDICT.get(res.get("status", ""), "") if res else ""},
            "error": self.last_error,
        }

    def text(self, now: float) -> str:
        if not self.on:
            return "🪙 The crypto plan is off. /crypto on turns it on."
        if not self.series:
            return ("🪙 Loading crypto prices… (it needs Alpaca keys; crypto prices are free)"
                    + (f"\n{esc(self.last_error)}" if self.last_error else ""))
        budget = self.cfg.get("CRYPTO_BUDGET")
        rows = self.board_rows()
        btc = next((r for r in rows if r["coin"] == "BTC"), None)
        lines = ["<b>🪙 Crypto trend plan</b>" + (f" · BTC {fmt_price(btc['price'])}"
                                                + (f" ({pct(btc['change'])} today)" if btc["change"] is not None else "")
                                                if btc else "")]
        res = self.bt()
        if res and not res.get("error"):
            lines.append(VERDICT.get(res.get("status", ""), ""))
        lines.append(f"\n<b>The plan for {money(budget)}</b> (trend score out of 9)")
        for r in rows:
            if r["on"] is None:
                lines.append(f"⚪ {r['coin']} — not enough price history yet")
                continue
            icon = "🟢" if r["target_w"] > 0 and r["on"] >= 5 else "🟡" if r["target_w"] > 0 else "⚪"
            lines.append(f"{icon} {r['coin']} {r['on']}/9 · " + (f"<b>{money(r['plan_usd'])}</b>" if r["target_w"] > 0
                                                               else "none"))
        cash_w = max(0.0, 1 - sum(r["target_w"] for r in rows))
        lines.append(f"💵 Cash {money(cash_w * budget)} ({cash_w * 100:.0f}%) — waiting in cash is how it sidesteps "
                     "crashes")
        lines.append("\n" + self.autopilot_line())
        if res and not res.get("error"):
            lines.append("\n<b>Backtest</b>\n" + "\n".join(backtest_lines(res)))
        nxt = self.latest_bar() + 2 * DAY
        lines.append(f"\nIt re-plans once a day when the daily candle closes (next: {et(nxt).strftime('%a')} "
                     f"{clock_text(nxt)}). Costs assumed: "
                     f"{self.cfg.get('CRYPTO_COST_PCT'):g}% per buy or sell (Robinhood's spread).\n"
                     "/crypto test · /crypto reset 1000 · /crypto off · /set CRYPTO_BUDGET 50")
        return "\n".join(lines)

    def week_line(self, now: float) -> str:
        if not self.on or not self.since():
            return ""
        return self.autopilot_line()

    def status_line(self, now: float) -> str:
        if not self.on:
            return "⚪ Crypto plan: off (/crypto on)"
        if not self.series:
            return "⚪ Crypto plan: loading prices" + (f" ({esc(self.last_error)})" if self.last_error else "")
        res = self.bt() or {}
        step = int(num(self.db.get_meta("crypto_step_ts", "0")))
        return (f"✅ Crypto plan: {len(self.series)} coins · last re-plan "
                + (f"{et(step).strftime('%b %-d')} {clock_text(step)}" if step else "soon")
                + (f" · backtest {STATUS.get(res.get('status', ''), '')}" if res else ""))
