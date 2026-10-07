"""Hype scanner: find coins at the START of a push, from what hype actually does to a coin.

Posts on X and calls in Telegram groups are easy to fake (paid shills, bots). What real attention does to a coin is
harder to fake and shows up within minutes, for free:
  🚀 buy rush       — buys in the last 5 minutes far above the hour's pace
  🟢 buy pressure   — buyers clearly outnumber sellers right now
  💰 volume surge   — 5-minute volume far above the hour's pace
  📈 momentum       — rising, but not already parabolic
  🐋 your whales    — tracked whales bought it in the last hours (the whale alert system feeds this)
  📣 paid boost     — the project is paying DexScreener for promotion: a push is on
  🧾 profile / CTO  — new DexScreener token profile or community takeover, with X / Telegram / site
  🔥 trending       — trending on GeckoTerminal
  🌱 early          — young pair, small market cap: most of the move can still be ahead
  🧲 loading        — buys and volume picking up while the price hasn't moved yet: the earliest pre-pump sign
  ♻️ second leg     — a coin that ran recently, pulled back hard, and has buyers coming back

Coins that were hot recently stay on the scanner's watch list for 48 hours, even after they drop off the trending
lists — that's where second legs come from. Coins already up more than HYPE_MAX_H1_PCT in the hour are not alerted:
by then you're the exit liquidity, not early.

Each coin gets a 0–100 score from these. An alert goes out when the score clears HYPE_MIN_SCORE with at least three
signals, one of them real buying (rush, volume or whales), and the coin passes the safety gates (liquidity, not
dumping, not already parabolic, freeze authority revoked, not a whale-only coin). Whale entry alerts carry the same
score, so the two systems work together.

Every hype alert is recorded and traded by the paper autopilot with the exit plan, and /hype and the dashboard show
which signals actually led to runs — the scanner is measured, not trusted.
(The old "community runner" alerts averaged -71%: they fired after coins had run and held for a day. This scanner
looks for acceleration at the start and skips coins that already went parabolic.)
"""
from __future__ import annotations

import time

from . import copies, messages
from .util import ago, esc, mc, num, pct, usd

HYPE = "hype"              # pseudo-wallet for hype alerts' copies and paper trades
COOLDOWN = 6 * 3600        # one hype alert per coin per 6 hours
ACTIVITY = {"rush", "volume", "whales", "loading"}
WATCH_HOURS = 48
LABELS = {"rush": "🚀 buy rush", "pressure": "🟢 buy pressure", "volume": "💰 volume surge", "momentum": "📈 momentum",
          "whales": "🐋 your whales", "boost": "📣 paid boost", "profile": "🧾 profile/socials", "trending": "🔥 trending",
          "early": "🌱 early", "loading": "🧲 loading", "reload": "♻️ second leg"}


def score(info: dict, cfg, *, whales: list | None = None, lists: dict | None = None,
          now: float | None = None, peak_mc: float = 0.0) -> dict:
    """{"score", "signals": [(key, text, points)], "blocked": reason or ""} for one coin. Pure: no network."""
    now = now or time.time()
    lists = lists or {}
    whales = whales or []
    mint = info.get("mint", "")
    mcap, liq = num(info.get("mc_usd")), num(info.get("liquidity_usd"), -1)
    m5, h1 = num(info.get("change_m5")), num(info.get("change_h1"))
    created = num(info.get("pair_created_ts"))
    age_h = (now - created) / 3600 if created else 999.0
    out = {"score": 0, "signals": [], "blocked": ""}
    if mcap <= 0 or num(info.get("price_usd")) <= 0:
        out["blocked"] = "no price"
        return out
    if mcap > cfg.get("HYPE_MAX_MC_USD"):
        out["blocked"] = f"market cap {mc(mcap)} — past early"
    elif liq < cfg.get("HYPE_MIN_LIQ_USD") or liq < 0.03 * mcap:
        out["blocked"] = f"liquidity too thin ({usd(max(liq, 0))})"
    elif m5 < -15 or h1 < -35:
        out["blocked"] = f"dumping ({pct(m5)} 5m, {pct(h1)} 1h)"
    elif m5 > 40 or h1 > cfg.get("HYPE_MAX_H1_PCT"):
        out["blocked"] = f"late — already {pct(h1)} this hour ({pct(m5)} in 5 min)"

    sig = []
    b5, s5 = num(info.get("buys_m5")), num(info.get("sells_m5"))
    pace = max(num(info.get("buys_h1")) / 12, 1.0)
    if b5 >= 25 and b5 >= 2 * pace:
        sig.append(("rush", f"🚀 buy rush: {int(b5)} buys in 5 min, {b5 / pace:.1f}x the hour's pace", 25))
    elif b5 >= 15 and b5 >= 1.5 * pace:
        sig.append(("rush", f"🚀 buying picking up: {int(b5)} buys in 5 min ({b5 / pace:.1f}x the hour's pace)", 12))
    if b5 >= 15 and b5 >= 1.6 * max(s5, 1):
        sig.append(("pressure", f"🟢 buyers outnumber sellers {int(b5)}/{int(s5)} in 5 min", 15))
    v5, vpace = num(info.get("volume_m5")), max(num(info.get("volume_h1")) / 12, 1.0)
    if v5 >= 5_000 and v5 >= 2.5 * vpace:
        sig.append(("volume", f"💰 volume surge: {usd(v5)} in 5 min, {v5 / vpace:.1f}x the hour's pace", 20))
    if 5 <= m5 <= 40 and h1 <= 150:
        sig.append(("momentum", f"📈 up {pct(m5)} in 5 min, {pct(h1)} in the hour — not parabolic yet", 10))
    if len(whales) >= 2:
        sig.append(("whales", f"🐋 {len(whales)} of your whales in: " + ", ".join(w["name"] for w in whales[:4]), 30))
    elif whales:
        sig.append(("whales", f"🐋 your whale {whales[0]['name']} bought it", 15))
    if num(info.get("boosts")) > 0 or mint in lists.get("boosts", ()) or mint in lists.get("top_boosts", ()):
        sig.append(("boost", "📣 paid DexScreener boost — the team is pushing it", 10))
    if mint in lists.get("takeovers", ()):
        sig.append(("profile", "🧾 community takeover on DexScreener", 8))
    elif mint in lists.get("profiles", ()) or (num(info.get("socials_count")) >= 2 and info.get("x_url")):
        sig.append(("profile", "🧾 X / Telegram / site listed", 8))
    if mint in lists.get("trending", ()):
        sig.append(("trending", "🔥 trending on GeckoTerminal", 8))
    if age_h <= 24 and mcap <= 1_500_000:
        sig.append(("early", f"🌱 early: pair {_age(age_h)} old at {mc(mcap)} MC", 10))
    if b5 >= 10 and b5 >= 1.5 * pace and v5 >= 2 * vpace and v5 >= 2_000 and -5 <= m5 <= 8:
        sig.append(("loading", f"🧲 buyers loading before the price moves: {int(b5)} buys, {usd(v5)} in 5 min "
                               f"({v5 / vpace:.1f}x the hour's volume pace), price only {pct(m5)}", 20))
    keys = {k for k, _, _ in sig}
    if peak_mc >= 2.5 * mcap and keys & {"loading", "rush", "volume"}:
        sig.append(("reload", f"♻️ second leg: peaked at {mc(peak_mc)} in the last 2 days, now {mc(mcap)} — "
                              "buyers coming back", 15))
    out["signals"] = sig
    out["score"] = min(100, sum(p for _, _, p in sig))
    return out


def alertable(result: dict, cfg) -> bool:
    keys = {k for k, _, _ in result["signals"]}
    return (not result["blocked"] and result["score"] >= cfg.get("HYPE_MIN_SCORE")
            and len(keys) >= 3 and bool(keys & ACTIVITY))


def _age(hours: float) -> str:
    return f"{int(hours * 60)}m" if hours < 1 else f"{hours:.0f}h"


class HypeScanner:
    def __init__(self, cfg, db, market, engine, whales, notify):
        self.cfg = cfg
        self.db = db
        self.market = market
        self.engine = engine
        self.whales = whales
        self.notify = notify
        self.lists: dict[str, set] = {}
        self.board: list[dict] = []      # this minute's top coins, for the dashboard and /hype
        self.last_scan = 0
        self.last_error = ""
        db.run("""create table if not exists hype_watch(mint text primary key, hot_ts integer, peak_mc real)""")

    def whales_in(self, mint: str, now: float) -> list[dict]:
        try:
            return self.engine._confluence(mint, int(now))
        except Exception:
            return []

    def watch_mints(self, now: float, limit: int = 150) -> list[str]:
        """Coins that were hot in the last 48h: rescanned every minute so a second leg is caught at the start."""
        return [r["mint"] for r in self.db.rows("select mint from hype_watch where hot_ts>=? order by hot_ts desc limit ?",
                                                (int(now) - WATCH_HOURS * 3600, limit))]

    def peak_of(self, mint: str) -> float:
        return num(self.db.scalar("select peak_mc from hype_watch where mint=?", (mint,)))

    def _remember(self, mint: str, info: dict, r: dict, now: int) -> None:
        hot = r["score"] >= 40 or num(info.get("change_h24")) >= 200
        if hot:
            self.db.run("""insert into hype_watch(mint,hot_ts,peak_mc) values(?,?,?) on conflict(mint) do update set
                hot_ts=excluded.hot_ts, peak_mc=max(hype_watch.peak_mc, excluded.peak_mc)""",
                        (mint, now, num(info.get("mc_usd"))))
        else:
            self.db.run("update hype_watch set peak_mc=max(peak_mc, ?) where mint=?", (num(info.get("mc_usd")), mint))

    def quick(self, mint: str, info: dict, now: float | None = None) -> dict:
        """The score for one coin right now (used by whale entry alerts)."""
        return score({**info, "mint": mint}, self.cfg, whales=self.whales_in(mint, now or time.time()),
                     lists=self.lists, now=now, peak_mc=self.peak_of(mint))

    async def scan(self, infos: dict[str, dict], lists: dict[str, list], now: int | None = None) -> list[str]:
        now = int(now or time.time())
        self.last_scan = now
        self.lists = {k: set(v) for k, v in lists.items()}
        scored = []
        for mint, info in infos.items():
            r = score({**info, "mint": mint}, self.cfg, whales=self.whales_in(mint, now), lists=self.lists, now=now,
                      peak_mc=self.peak_of(mint))
            self._remember(mint, info, r, now)
            if r["signals"]:
                scored.append((mint, info, r))
        scored.sort(key=lambda x: (not x[2]["blocked"], x[2]["score"]), reverse=True)
        self.board = [{"mint": m, "symbol": i.get("symbol"), "image": i.get("image"), "mc_usd": i.get("mc_usd"),
                       "liquidity_usd": i.get("liquidity_usd"), "change_m5": i.get("change_m5"),
                       "change_h1": i.get("change_h1"), "age_ts": i.get("pair_created_ts"), "x_url": i.get("x_url", ""),
                       "score": r["score"], "blocked": r["blocked"],
                       "signals": [t for _, t, _ in r["signals"]], "keys": [k for k, _, _ in r["signals"]]}
                      for m, i, r in scored[:30]]
        if not self.cfg.flag("HYPE_ALERTS"):
            return []
        sent = []
        for mint, info, r in scored:
            if not alertable(r, self.cfg):
                continue
            if self._sent_last_hour(now) >= self.cfg.get("HYPE_MAX_PER_HOUR"):
                break
            if self.recently_alerted(mint, now):
                continue
            if await self._unsafe(mint, info):
                continue
            await self.alert(mint, info, r, now)
            sent.append(mint)
        return sent

    def recently_alerted(self, mint: str, now: float) -> bool:
        return bool(self.db.scalar("""select 1 from alerts where mint=? and ts>=? and (kind='HYPE'
            or (kind='BUY' and status in ('sent','silent') and reasons like '%Hype score%'))""",
                                   (mint, int(now) - COOLDOWN)))

    def _sent_last_hour(self, now: int) -> int:
        return int(self.db.scalar("select count(*) from alerts where kind='HYPE' and ts>=?", (now - 3600,), default=0))

    async def _unsafe(self, mint: str, info: dict) -> bool:
        safety = await self.market.safety(mint)
        if not safety.get("ok", True) or safety.get("freeze_authority"):
            return True
        community = await self.engine.community.check(mint, info)
        return community.get("label") == "WHALE-ONLY"

    def record(self) -> dict:
        return self.whales.stats(HYPE, fresh=True)

    async def alert(self, mint: str, info: dict, r: dict, now: int) -> None:
        keys = ",".join(k for k, _, _ in r["signals"])
        alert_id = self.db.insert("""insert into alerts(ts,kind,mint,wallet,grade,mc_usd,price_usd,reasons,status)
            values(?,?,?,?,?,?,?,?,?)""", (now, "HYPE", mint, HYPE, "H", num(info.get("mc_usd")),
                                           num(info.get("price_usd")), f"signals:{keys}", "sent"))
        copies.open_copy(self.db, whale=HYPE, mint=mint, swap_id=0, alert_id=alert_id, price=num(info["price_usd"]),
                         mc_usd=num(info.get("mc_usd")), whale_price=num(info["price_usd"]), ts=now)
        self.engine.paper.open(alert_id=alert_id, mint=mint, whale=HYPE, symbol=info.get("symbol") or mint[:4],
                               price=num(info["price_usd"]), mc_usd=num(info.get("mc_usd")), ts=now)
        if not self.cfg.flag("ALERTS_ENABLED"):
            return
        created = num(info.get("pair_created_ts"))
        mcap = num(info.get("mc_usd"))
        lines = [f"<b>🔥 HYPE BUILDING · ${esc(info.get('symbol'))} · score {r['score']}/100</b>",
                 f"{mc(mcap)} MC · liq {usd(info.get('liquidity_usd'))} · pair {ago(created) if created else '?'} · "
                 f"5m {pct(info.get('change_m5'))} · 1h {pct(info.get('change_h1'))}"]
        lines += [esc(t) for _, t, _ in r["signals"]]
        rec = self.record()
        if rec["n"] >= 3:
            lines.append(f"Hype alerts so far: {rec['n']} · {rec['win_rate'] * 100:.0f}% won · avg {pct(rec['avg'])} · "
                         f"{rec['hit_2x'] * 100:.0f}% reached 2x")
        else:
            lines.append("Hype alerts are new — their record is being measured (/hype). Start small.")
        lines.append(f"Plan: take half at 2x ({mc(mcap * 2)} MC), rest out if it falls 35% from its top, "
                     f"-{self.cfg.get('STOP_LOSS_PCT'):.0f}% stop.")
        lines.append(f"<code>{mint}</code>")
        links = [(label, url, None) for label, url in messages.token_links(mint, info.get("pair_address", ""))]
        if info.get("x_url"):
            links.append(("𝕏", info["x_url"], None))
        msg_id = await self.notify("\n".join(lines), buttons=[links, [("🔎 Find its early whales", None, f"find:{mint}")]],
                                   mint=mint, wallet=HYPE, kind="HYPE")
        if msg_id:
            self.db.run("update alerts set tg_message_id=? where id=?", (msg_id, alert_id))

    def signal_results(self, days: int = 14) -> list[dict]:
        """For each signal: how the hype alerts it was part of did (peak, and where they were 1h later)."""
        rows = self.db.rows("""select reasons, price_usd, peak_price, p1h from alerts where kind='HYPE'
            and price_usd>0 and ts>=?""", (int(time.time()) - days * 86400,))
        by: dict[str, list] = {}
        for r in rows:
            for key in str(r["reasons"] or "").replace("signals:", "").split(","):
                if key:
                    by.setdefault(key, []).append(r)
        out = []
        for key, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
            peaks = [num(r["peak_price"]) / num(r["price_usd"]) for r in rs if num(r["peak_price"]) > 0]
            later = [num(r["p1h"]) / num(r["price_usd"]) for r in rs if r["p1h"]]
            out.append({"signal": key, "n": len(rs), "hit_2x": sum(p >= 2 for p in peaks) / len(peaks) if peaks else 0.0,
                        "hit_1_5x": sum(p >= 1.5 for p in peaks) / len(peaks) if peaks else 0.0,
                        "avg_1h": (sum(later) / len(later) - 1) * 100 if later else None})
        return out
