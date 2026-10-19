"""Second leg: a coin that pumped, pulled back and built a base — and buyers are coming back.

The pattern you described: "some pump at the beginning, then drop, then pump again". In your data the coins that
rugged were the brand-new ones; a coin that already ran, survived a deep pullback and held a floor for hours is a
different animal — the insiders are mostly out, the pool is still there, and the next leg starts from a known floor
you can put a stop under. So this looks for exactly that, on 15-minute candles (GeckoTerminal, free):

  1. FIRST LEG  — the price ran 3x+ (SWING_MIN_FIRST_LEG) from its low to a peak in the last 3 days
  2. PULLBACK   — then fell 50-85% from that peak (deeper = dead, shallower = not reset)
  3. BASE       — and has held above that low for 2+ hours (no new lows): sellers are done
  4. RELOAD     — now buyers are back: price 15-100% off the base, volume over the last hour 2x+ the base's, and
                  more buys than sells in the last 5 minutes (DexScreener)
  5. ROOM       — still well under the old peak (1.6x+ to get back there)
plus the same safety checks as every alert (authorities, Token-2022 traps, bundled supply, RugCheck dangers, pool
age 6h+, real liquidity).

The alert is a trade plan, not a guarantee: entry now, stop 10% under the base low, take half at +50%, let the
rest ride toward the old peak with a trailing stop, out within 24 hours. The paper autopilot trades every one (type
"swing") so you see how the pattern really does in this market, and real money only follows once those paper trades
have made money (/edge).
"""
from __future__ import annotations

import statistics
import time

from .util import esc, mc, num, pct, usd

CANDLE_MIN = 15
MIN_CANDLES = 24                 # 6 hours of 15-minute candles
LEG1_MIN = 3.0
RETRACE = (0.50, 0.85)
BASE_HOLD_S = 2 * 3600
BOUNCE = (1.15, 2.0)
ROOM_MIN = 1.6
VOLUME_RELOAD = 2.0
CHECKS_PER_SCAN = 4              # candle fetches per scan (GeckoTerminal's free limit is ~30 a minute)
COOLDOWN = 12 * 3600             # one second-leg alert per coin per 12 hours
PLAN_HOLD = 24 * 3600


def analyze(candles: list[tuple], min_leg: float = LEG1_MIN, now: float | None = None) -> dict | None:
    """The second-leg pattern on 15-minute candles [(ts, o, h, l, c, v)], or None (with no reason) when it isn't there."""
    if len(candles) < MIN_CANDLES:
        return None
    now = now or candles[-1][0] + CANDLE_MIN * 60
    highs = [c[2] for c in candles]
    peak_i = max(range(len(candles)), key=lambda i: highs[i])
    peak = highs[peak_i]
    if peak_i == 0 or peak_i >= len(candles) - 8:
        return None                                  # no first leg in view, or the peak is too recent to have a base
    start_low = min(c[3] for c in candles[:peak_i + 1])
    after = candles[peak_i + 1:]
    base_j = min(range(len(after)), key=lambda j: after[j][3])
    base_low = after[base_j][3]
    if start_low <= 0 or base_low <= 0:
        return None
    leg1 = peak / start_low
    retrace = 1 - base_low / peak
    base_ts = after[base_j][0]
    price = candles[-1][4]
    bounce, room = price / base_low, peak / price
    base_vol = [c[5] for c in after[base_j:-4]] or [0.0]
    recent_vol = statistics.mean(c[5] for c in candles[-4:])
    typical = statistics.median(base_vol) if base_vol else 0.0
    checks = {
        "leg1": leg1 >= min_leg,
        "retrace": RETRACE[0] <= retrace <= RETRACE[1],
        "base": now - base_ts >= BASE_HOLD_S,
        "bounce": BOUNCE[0] <= bounce <= BOUNCE[1],
        "room": room >= ROOM_MIN,
        "volume": typical > 0 and recent_vol >= VOLUME_RELOAD * typical,
    }
    if not all(checks.values()):
        return None
    stop = base_low * 0.9
    return {"peak": peak, "start_low": start_low, "base_low": base_low, "price": price, "leg1": leg1,
            "retrace": retrace, "bounce": bounce, "room": room, "base_hours": (now - base_ts) / 3600,
            "volume_x": recent_vol / typical if typical else 0.0, "peak_hours_ago": (now - candles[peak_i][0]) / 3600,
            "stop": stop, "stop_pct": (stop / price - 1) * 100, "tp1": price * 1.5,
            "tp2": max(price * 1.5, peak * 0.8)}


def prefilter(info: dict, cfg, now: float) -> bool:
    """Cheap checks on DexScreener data before spending a candle fetch."""
    created = num(info.get("pair_created_ts"))
    mcap, liq = num(info.get("mc_usd")), num(info.get("liquidity_usd"), -1)
    b5, s5 = num(info.get("buys_m5")), num(info.get("sells_m5"))
    return (created > 0 and now - created >= max(6.0, cfg.get("WHALE_MIN_POOL_HOURS")) * 3600
            and cfg.get("SWING_MIN_MC_USD") <= mcap <= 50_000_000 and liq >= cfg.get("SWING_MIN_LIQ_USD")
            and liq >= 0.03 * mcap and b5 >= 8 and b5 >= 1.3 * max(s5, 1) and num(info.get("change_m5")) > 0
            and num(info.get("change_h1")) > -30)


def exit_plan(sig: dict) -> dict:
    stop_x = max(0.5, sig["stop"] / sig["price"])
    return {"tp": 1.5, "stop": stop_x, "hold": PLAN_HOLD, "half": True, "trail": 30,
            "label": f"half at +50%, the rest rides (stop {(stop_x - 1) * 100:.0f}%, trail 30%), 24h"}


class SwingScanner:
    def __init__(self, cfg, db, market, engine, notify, hype=None):
        self.cfg, self.db, self.market, self.engine, self.notify, self.hype = cfg, db, market, engine, notify, hype
        self.last_error = ""
        self.found: list[dict] = []

    def _recent(self, mint: str, now: int) -> bool:
        return bool(self.db.scalar("select 1 from alerts where kind='SWING' and mint=? and ts>=?", (mint, now - COOLDOWN)))

    def _sent_last_hour(self, now: int) -> int:
        return int(self.db.scalar("select count(*) from alerts where kind='SWING' and ts>=?", (now - 3600,), default=0))

    async def scan(self, infos: dict[str, dict], now: int | None = None) -> list[str]:
        if not self.cfg.flag("SWING_ALERTS"):
            return []
        now = int(now or time.time())
        cands = [i for m, i in infos.items() if prefilter({**i, "mint": m}, self.cfg, now) and not self._recent(m, now)]
        cands.sort(key=lambda i: -num(i.get("buys_m5")))
        sent = []
        for info in cands[:CHECKS_PER_SCAN]:
            if self._sent_last_hour(now) >= self.cfg.get("SWING_MAX_PER_HOUR"):
                break
            mint = info.get("mint")
            try:
                candles = await self.market.candles(info.get("pair_address", ""), CANDLE_MIN)
            except Exception as exc:
                self.last_error = f"{time.strftime('%H:%M:%S')} candles {type(exc).__name__}"
                continue
            sig = analyze(candles, self.cfg.get("SWING_MIN_FIRST_LEG"), now)
            if not sig:
                continue
            if self.hype and await self.hype._unsafe(mint, info):
                continue
            holders = await self.hype._holders(mint, info) if self.hype else None
            if num((holders or {}).get("top10_pct"), -1) > 35:
                continue                                   # bundled supply: the second leg is somebody's exit
            await self.alert(mint, info, sig, holders, now)
            sent.append(mint)
        return sent

    async def alert(self, mint: str, info: dict, sig: dict, holders: dict | None, now: int) -> int:
        plan = exit_plan(sig)
        price = num(info.get("price_usd")) or sig["price"]
        alert_id = self.db.insert("""insert into alerts(ts,kind,mint,wallet,grade,mc_usd,price_usd,reasons,status)
            values(?,?,?,?,?,?,?,?,?)""", (now, "SWING", mint, "swing", "S", num(info.get("mc_usd")), price,
                                           f"leg1 {sig['leg1']:.1f}x, pullback {sig['retrace'] * 100:.0f}%", "sent"))
        trade_id = self.engine.paper.open(alert_id=alert_id, mint=mint, whale="swing", symbol=info.get("symbol") or mint[:4],
                                          price=price, mc_usd=num(info.get("mc_usd")), ts=now, info=info,
                                          signal={"score": 70}, plan=plan)
        edge = getattr(getattr(self.hype, "tuner", None), "edge", None) if self.hype else None
        if edge and trade_id:
            edge.record(trade_id, {"setup": "s_second_leg", "plan": "swing_plan"})
        self.found.insert(0, {"mint": mint, "symbol": info.get("symbol"), "ts": now, **{k: sig[k] for k in
                                                                                       ("leg1", "retrace", "room")}})
        self.found = self.found[:20]
        if not self.cfg.flag("ALERTS_ENABLED"):
            return alert_id
        mcap = num(info.get("mc_usd"))
        scale = mcap / price if price else 0
        fw = edge.forward("s_second_leg", "swing_plan") if edge else {"n": 0}
        lines = [f"<b>♻️ SECOND LEG · ${esc(info.get('symbol'))}</b> · {mc(mcap)} MC · liq {usd(info.get('liquidity_usd'))}",
                 f"First leg {sig['leg1']:.1f}x to {mc(sig['peak'] * scale)} ({sig['peak_hours_ago']:.0f}h ago) → pulled back "
                 f"{sig['retrace'] * 100:.0f}% to {mc(sig['base_low'] * scale)} → held that floor {sig['base_hours']:.0f}h → "
                 f"now {pct((sig['bounce'] - 1) * 100)} off it with {sig['volume_x']:.1f}x the base's volume, "
                 f"{int(num(info.get('buys_m5')))} buys / {int(num(info.get('sells_m5')))} sells in 5 min",
                 f"Room back to the old peak: {sig['room']:.1f}x"]
        if holders and num(holders.get("top10_pct"), -1) >= 0:
            lines.append(f"Top 10 wallets hold {num(holders['top10_pct']):.0f}% of supply")
        lines.append(f"🎯 Plan: buy around {mc(mcap)} · stop {mc(sig['stop'] * scale)} ({sig['stop_pct']:+.0f}%, under the "
                     f"floor) · take half at {mc(mcap * 1.5)} (+50%) · rest toward {mc(sig['tp2'] * scale)} with a 30% "
                     "trailing stop · out within 24h")
        lines.append(f"Paper's second-leg record: {fw['n']} closed" + (f", {(fw['avg'] - 1) * 100:+.1f}% a trade, "
                                                                        f"{fw['won'] * 100:.0f}% won" if fw.get("n") else
                                                                        " — it's being measured, size small"))
        lines.append(f"<code>{mint}</code>")
        from . import messages
        links = [(label, url, None) for label, url in messages.token_links(mint, info.get("pair_address", ""))]
        msg_id = await self.notify("\n".join(lines), buttons=[links, [("🧾 Buy check", None, f"check:{mint}")]],
                                   mint=mint, wallet="swing", kind="SWING")
        if msg_id:
            self.db.run("update alerts set tg_message_id=? where id=?", (msg_id, alert_id))
        return alert_id
