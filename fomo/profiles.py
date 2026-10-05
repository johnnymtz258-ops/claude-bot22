"""Whale profiles: what kind of trader a wallet is, and what copying it at YOUR speed returns.

A whale can be very profitable for itself and still lose money for everyone who copies it: it buys,
copy-trade bots pump the coin in seconds, it sells into them within minutes, and by the time you see
the alert and buy you are the exit. Your own data showed it: 7yuq made +$40K while its coins were at
0.27x an hour after it bought. So every whale gets two numbers:

  style       how fast it starts selling after a buy (first sell, median over its coins)
                🤖 BOT under 1 minute · 🟠 FLIPPER under 10 minutes (or 40%+ of coins within 5)
                🔵 SWING 10 minutes to 2 hours · 🟢 HOLDER 2 hours or more
  copy score  what YOU would have made: buy COPY_DELAY_SECONDS after the whale (the price then),
              then the exit plan — half at 2x, sell the rest if it falls 35% from its top or 40% below
              entry, or ~90s after the whale has sold half its bag — with 1% fee each way.

Tracked whales are profiled from the trades and prices the bot recorded; wallets the scanner finds
are profiled from their on-chain history plus GeckoTerminal minute candles.
"""
from __future__ import annotations

import json
import statistics
import time

from .util import dur, num

STYLE_LABEL = {"HOLDER": "🟢 Holder", "SWING": "🔵 Swing", "FLIPPER": "🟠 Flipper", "BOT": "🤖 Bot",
               "NEW": "🆕 New"}
MIN_TRIPS = 4               # coins needed before a style is given
REACT_SECONDS = 90          # from the whale's sell to yours
FEE = 0.01                  # per side
WIN_CAP = 5.0               # one 13x can't make a whale look great: each copy counts at most 5x
MAX_HOLD = 24 * 3600


def style_of(first_sell_holds: list[float]) -> str:
    if len(first_sell_holds) < MIN_TRIPS:
        return "NEW"
    med = statistics.median(first_sell_holds)
    fast = sum(h <= 300 for h in first_sell_holds) / len(first_sell_holds)
    if med < 60:
        return "BOT"
    if med < 600 or fast >= 0.4:
        return "FLIPPER"
    return "HOLDER" if med >= 7200 else "SWING"


def simulate_copy(path: list[tuple[int, float]], buy_ts: int, whale_out_ts: int | None, *, delay: float = 60,
                  tp: float = 2.0, trail_pct: float = 35, arm: float = 1.5, stop_pct: float = 40) -> float | None:
    """Multiple you'd end with copying one buy (after fees), or None if there's no price near your entry."""
    pts = [(t, p) for t, p in path if t >= buy_ts + delay and p > 0]
    if not pts or pts[0][0] > buy_ts + delay + 240:
        return None
    t0, entry = pts[0]
    peak, got, rem, half, last = 1.0, 0.0, 1.0, False, 1.0
    for t, p in pts:
        if t > t0 + MAX_HOLD:
            break
        x = last = p / entry
        peak = max(peak, x)
        if whale_out_ts and t >= whale_out_ts + REACT_SECONDS:
            got, rem = got + rem * x, 0.0
            break
        if not half and x >= tp:
            got, rem, half = got + 0.5 * x, 0.5, True
        if x <= 1 - stop_pct / 100 or (peak >= arm and x <= peak * (1 - trail_pct / 100)):
            got, rem = got + rem * x, 0.0
            break
    got += rem * last
    return got * (1 - FEE) / (1 + FEE)


def summarize(results: list[float]) -> dict:
    xs = [r for r in results if r is not None]
    if not xs:
        return {"copy_n": 0, "copy_avg": 0.0, "copy_med": 0.0, "copy_win": 0.0}
    return {"copy_n": len(xs), "copy_avg": statistics.mean(min(x, WIN_CAP) for x in xs),
            "copy_med": statistics.median(xs), "copy_win": sum(x > 1 for x in xs) / len(xs)}


def build_profile(trips: list[dict], paths: dict, delay: float, source: str) -> dict:
    """trips: [{mint, buy_ts, first_sell_ts, half_out_ts, bought_usd, sold_usd, closed}]."""
    holds = [t["first_sell_ts"] - t["buy_ts"] for t in trips if t.get("first_sell_ts") and t["first_sell_ts"] >= t["buy_ts"]]
    closed = [t for t in trips if t.get("closed") and t.get("bought_usd", 0) > 0]
    copies = [simulate_copy(paths[t["mint"]], t["buy_ts"], t.get("half_out_ts"), delay=delay)
              for t in trips if paths.get(t["mint"])]
    prof = {"style": style_of(holds), "trips": len(trips), "median_hold_s": statistics.median(holds) if holds else 0.0,
            "within5m": sum(h <= 300 for h in holds) / len(holds) if holds else 0.0,
            "own_pnl_usd": sum(t["sold_usd"] - t["bought_usd"] for t in closed),
            "own_win": sum(t["sold_usd"] > t["bought_usd"] for t in closed) / len(closed) if closed else 0.0,
            "closed": len(closed),
            "source": source, "updated_ts": int(time.time())}
    prof.update(summarize(copies))
    return prof


def verdict(p: dict | None, min_avg: float = 0.95, min_n: int = 5) -> tuple[bool, str]:
    """(send entry alerts?, reason). Flippers and bots never; holders/swing unless copying them loses."""
    if not p:
        return True, ""
    if p["style"] in ("FLIPPER", "BOT"):
        return False, (f"{STYLE_LABEL[p['style']]}: first sells ~{dur(p['median_hold_s'])} after buying "
                       f"({p['within5m'] * 100:.0f}% within 5m) — you'd be buying their exit")
    if p.get("closed", 0) >= 6 and p["own_pnl_usd"] < 0 and p["own_win"] < 0.35:
        return False, (f"Losing whale: {p['own_win'] * 100:.0f}% of its last {p['closed']} trades won, "
                       f"${p['own_pnl_usd']:,.0f} overall")
    if p.get("copy_n", 0) >= min_n and p["copy_avg"] < min_avg:
        return False, f"Copying them at your speed lost: {p['copy_avg']:.2f}x avg over {p['copy_n']} coins"
    return True, ""


def describe(p: dict | None) -> str:
    if not p:
        return ""
    text = STYLE_LABEL.get(p["style"], p["style"])
    if p["style"] != "NEW":
        text += f" · first sells ~{dur(p['median_hold_s'])} after buying"
    if p.get("copy_n", 0) >= 3:
        text += (f" · copying at your speed: {p['copy_avg']:.2f}x avg, {p['copy_win'] * 100:.0f}% won "
                 f"({p['copy_n']} coins)")
    return text


# -- tracked whales: from the bot's own records ---------------------------------------------------

def local_trips(db, wallet: str, days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    trips = []
    for r in db.rows("""select mint, min(case when side='BUY' then ts end) buy_ts,
            min(case when side='SELL' then ts end) first_sell_ts,
            sum(case when side='BUY' then usd_value else 0 end) bought_usd,
            sum(case when side='SELL' then usd_value else 0 end) sold_usd,
            max(max(holding_after), max(pre_holding)) peak_hold,
            min(case when side='SELL' and holding_after<=0 then ts end) out_ts
            from swaps where wallet=? and ts>=? group by mint""", (wallet, since)):
        if not r["buy_ts"]:
            continue
        half = db.scalar("""select min(ts) from swaps where wallet=? and mint=? and side='SELL'
            and holding_after<=?""", (wallet, r["mint"], 0.5 * num(r["peak_hold"])))
        trips.append({"mint": r["mint"], "buy_ts": int(r["buy_ts"]), "first_sell_ts": r["first_sell_ts"],
                      "half_out_ts": half or r["out_ts"], "bought_usd": num(r["bought_usd"]),
                      "sold_usd": num(r["sold_usd"]), "closed": bool(r["out_ts"])})
    return trips


def local_paths(db, trips: list[dict]) -> dict:
    out = {}
    for t in trips:
        rows = db.rows("select ts, price from price_marks where mint=? and ts between ? and ? order by ts",
                       (t["mint"], t["buy_ts"], t["buy_ts"] + MAX_HOLD))
        if rows:
            out[t["mint"]] = [(int(r["ts"]), num(r["price"])) for r in rows]
    return out


class Profiler:
    """Keeps a profile per whale in the database; tracked whales are re-profiled every 30 minutes."""

    def __init__(self, db, cfg, market=None, discovery=None):
        self.db = db
        self.cfg = cfg
        self.market = market
        self.discovery = discovery
        self._cache: dict[str, tuple[float, dict | None]] = {}

    def get(self, address: str) -> dict | None:
        hit = self._cache.get(address)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        row = self.db.row("select data from whale_profiles where address=?", (address,))
        prof = json.loads(row["data"]) if row else None
        self._cache[address] = (time.time(), prof)
        return prof

    def save(self, address: str, prof: dict) -> dict:
        self.db.run("""insert into whale_profiles(address,data,updated_ts) values(?,?,?)
            on conflict(address) do update set data=excluded.data, updated_ts=excluded.updated_ts""",
                    (address, json.dumps(prof), int(time.time())))
        self._cache.pop(address, None)
        return prof

    def profile_local(self, address: str) -> dict:
        trips = local_trips(self.db, address)
        prof = build_profile(trips, local_paths(self.db, trips), self.cfg.get("COPY_DELAY_SECONDS"), "recorded")
        old = self.get(address)
        if old and old.get("source") == "history" and old.get("trips", 0) > prof["trips"]:
            # a freshly followed whale: its on-chain history says more than our few records yet
            return old
        return self.save(address, prof)

    def refresh_tracked(self) -> int:
        n = 0
        for w in self.db.rows("select address from whales where active=1"):
            self.profile_local(w["address"])
            n += 1
        return n

    async def profile_history(self, address: str, analysis: dict | None = None, max_coins: int = 12) -> dict | None:
        """Profile any wallet from its recent on-chain trades + minute candles (used by the scanner)."""
        if not (self.discovery and self.market):
            return None
        analysis = analysis or await self.discovery.analyze_wallet(address)
        trips = (analysis or {}).get("trip_list") or []
        if not trips:
            return None
        trips = sorted(trips, key=lambda t: -t["buy_ts"])[:max_coins]
        paths = {}
        for t in trips:
            path = await self.market.price_path(t["mint"], t["buy_ts"] - 60, t["buy_ts"] + 16 * 3600)
            if path:
                paths[t["mint"]] = path
        prof = build_profile(trips, paths, self.cfg.get("COPY_DELAY_SECONDS"), "history")
        prof["own_pnl_sol"] = num(analysis.get("pnl_sol"))
        return prof
