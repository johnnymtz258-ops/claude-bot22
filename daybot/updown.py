"""BTC 15-minute Up or Down: which way Bitcoin most likely finishes the current quarter hour (the windows Polymarket,
Kalshi and Robinhood's prediction markets trade: 3:00-3:15, 3:15-3:30 … New York time; Up wins when the price at the
end is at or above the price at the start — the "price to beat").

What it can and can't know:
  • At the start of a window it's close to a coin flip. Studies of hourly and shorter Bitcoin moves find no model
    that reliably beats a random walk. DayBot measures, every 6 hours, whether the last 30 days had a pattern from
    one window to the next (did a rise keep rising?). It only calls a "lean" when that pattern is statistically
    clear, and it shows how often the lean was right.
  • During a window the odds are real: they follow from how far the price is from the price to beat, how much time
    is left and how much Bitcoin is swinging (the last 2 hours of 1-minute moves). Its one setting — how fat the
    tails are — is fitted on the 30 days before the 30 it's checked on, so the track record is measured on windows it
    never saw.
Prices come from Alpaca's free BTC/USD feed. The markets settle on their own feeds (Polymarket: Chainlink), which can
differ by a few dollars — so when the price is right at the line, it really is 50/50.
"""
from __future__ import annotations

import math
import time

from . import markets
from .clock import clock_text, et
from .stats import clustered_mean_ci
from .util import dumps, esc, loads, num, price as fmt_price

WINDOW = 900
PROVEN_N = 40               # live paper trades before the edge signal may ping you
CHECKS = (5, 10)            # minutes into the window the track record is scored at
VOL_MINUTES = 120           # 1-minute moves used for volatility
STUDY_DAYS = 30
HISTORY_DAYS = 61
K_GRID = [round(0.7 + 0.05 * i, 2) for i in range(27)]     # tail factor: 0.70 … 2.00


def window_start(ts: float) -> int:
    return int(ts) // WINDOW * WINDOW


def phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def prob_up(move: float, sigma: float, minutes_left: float, k: float = 1.0) -> float:
    """Chance the window ends at or above the price to beat, from the log move so far and per-minute volatility."""
    if minutes_left <= 0:
        return 1.0 if move >= 0 else 0.0
    if sigma <= 0:
        return 0.5 if move == 0 else (0.99 if move > 0 else 0.01)
    return min(0.999, max(0.001, phi(move / (k * sigma * math.sqrt(minutes_left)))))


def _closes(bars: list) -> dict:
    return {int(b[0]): num(b[4]) for b in bars if num(b[4]) > 0}


def sigma_before(closes: dict, ts: int, minutes: int = VOL_MINUTES) -> float:
    """Per-minute volatility of the `minutes` 1-minute log moves before ts."""
    rets = []
    for m in range(minutes, 0, -1):
        a, b = closes.get(ts - 60 * (m + 1)), closes.get(ts - 60 * m)
        if a and b:
            rets.append(math.log(b / a))
    if len(rets) < 20:
        return 0.0
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))


def windows(bars: list, end: float | None = None) -> list[dict]:
    """Every finished window in the bars: its start price (the minute before it began), end price, the price at each
    checkpoint, the volatility going in, and how the window before it went."""
    closes = _closes(bars)
    if not closes:
        return []
    first, last = min(closes), max(closes)
    out = []
    s = window_start(first) + WINDOW
    stop = min(last + 60, end or last + 60)
    while s + WINDOW <= stop:
        p0, p1 = closes.get(s - 60), closes.get(s + WINDOW - 60)
        if p0 and p1:
            prev0 = closes.get(s - WINDOW - 60)
            row = {"ts": s, "open": p0, "close": p1, "up": 1 if p1 >= p0 else 0,
                   "prev": (p0 / prev0 - 1) if prev0 else None, "sigma": sigma_before(closes, s)}
            for m in CHECKS:
                row[f"p{m}"] = closes.get(s + 60 * m - 60)
            out.append(row)
        s += WINDOW
    return out


def persistence(wins: list[dict]) -> dict:
    """How often a window went the same way as the one before it, and whether that's more than chance."""
    same = n = 0
    for w in wins:
        if w["prev"] is None or w["prev"] == 0:
            continue
        n += 1
        same += 1 if (w["prev"] > 0) == bool(w["up"]) else 0
    p = same / n if n else 0.5
    z = (p - 0.5) / math.sqrt(0.25 / n) if n else 0.0
    return {"p": p, "n": n, "z": z, "rule": "momentum" if z >= 2 else "reversal" if z <= -2 else ""}


def lean_for(prev: float | None, pers: dict) -> tuple[int, float]:
    """(direction 1 up / -1 down / 0 none, its chance) for a window, given the last window's move."""
    if not pers.get("rule") or prev is None or prev == 0:
        return 0, 0.5
    d = 1 if prev > 0 else -1
    if pers["rule"] == "reversal":
        d = -d
    return d, max(pers["p"], 1 - pers["p"])


def _checkpoint_probs(w: dict, k: float) -> dict:
    out = {}
    for m in CHECKS:
        px = w.get(f"p{m}")
        if px and w["sigma"] > 0:
            out[m] = prob_up(math.log(px / w["open"]), w["sigma"], 15 - m, k)
    return out


def fit_k(wins: list[dict]) -> float:
    """The tail factor that best fits these windows (lowest log loss at the checkpoints)."""
    best, best_loss = 1.0, float("inf")
    for k in K_GRID:
        loss = n = 0
        for w in wins:
            for p in _checkpoint_probs(w, k).values():
                y = w["up"]
                loss -= math.log(p if y else 1 - p)
                n += 1
        if n and loss / n < best_loss:
            best, best_loss = k, loss / n
    return best


def score(wins: list[dict], k: float, pers: dict) -> dict:
    """How the calls did on these windows: the lean, the favorite at each checkpoint, and calibration."""
    lean_hits = lean_n = 0
    fav = {m: [0, 0] for m in CHECKS}
    buckets = {b: [0, 0, 0.0] for b in (50, 60, 70, 80, 90)}     # n, hits, sum of predicted
    brier = bn = 0.0
    for w in wins:
        d, _ = lean_for(w["prev"], pers)
        if d:
            lean_n += 1
            lean_hits += 1 if (d > 0) == bool(w["up"]) else 0
        for m, p in _checkpoint_probs(w, k).items():
            favorite_up = p >= 0.5
            hit = favorite_up == bool(w["up"])
            fav[m][0] += 1
            fav[m][1] += 1 if hit else 0
            q = max(p, 1 - p)
            b = min(90, int(q * 100) // 10 * 10)
            buckets[b][0] += 1
            buckets[b][1] += 1 if hit else 0
            buckets[b][2] += q
            brier += (p - w["up"]) ** 2
            bn += 1
    return {"windows": len(wins), "up_share": sum(w["up"] for w in wins) / len(wins) if wins else 0.0,
            "lean": {"n": lean_n, "hit": lean_hits / lean_n if lean_n else None},
            "favorite": {str(m): {"n": v[0], "hit": v[1] / v[0] if v[0] else None} for m, v in fav.items()},
            "calibration": [{"from": b, "n": v[0], "said": v[2] / v[0] if v[0] else None,
                             "happened": v[1] / v[0] if v[0] else None} for b, v in buckets.items()],
            "brier": brier / bn if bn else None}


def study(bars: list, now: float) -> dict:
    """Walk-forward: fit on days 60-30 ago, score on the last 30 days. Then refit on the last 30 for live use."""
    wins = windows(bars, now)
    cut = now - STUDY_DAYS * 86400
    fit_part = [w for w in wins if cut - STUDY_DAYS * 86400 <= w["ts"] < cut]
    test_part = [w for w in wins if w["ts"] >= cut]
    out = {"ts": int(now), "windows": len(wins)}
    if len(fit_part) >= 200 and len(test_part) >= 100:
        k0, p0 = fit_k(fit_part), persistence(fit_part)
        out["test"] = {**score(test_part, k0, p0), "k": k0, "pattern": p0}
    recent = test_part if len(test_part) >= 200 else wins[-STUDY_DAYS * 96:]
    out["k"] = fit_k(recent) if recent else 1.0
    out["pattern"] = persistence(recent)
    return out


# ---- live ----------------------------------------------------------------------------------------------------------
class UpDown:
    def __init__(self, cfg, db, alpaca, notify):
        self.cfg, self.db, self.alpaca, self.notify = cfg, db, alpaca, notify
        self.bars: list = []
        self.live_px = 0.0
        self.live_ts = 0.0
        self._last: dict = {}
        self.last_error = ""
        self.quotes: dict[str, dict] = {}              # venue -> the latest quote for the current window
        self.fetchers = {"polymarket": markets.fetch_polymarket, "kalshi": markets.fetch_kalshi}
        self.quote_errors: dict[str, str] = {}

    @property
    def on(self) -> bool:
        return self.cfg.flag("BTC15")

    def every(self, name: str, seconds: float, now: float) -> bool:
        if now - self._last.get(name, 0) >= seconds:
            self._last[name] = now
            return True
        return False

    def model(self) -> dict:
        return loads(self.db.get_meta("updown_study"), {}) or {}

    async def load(self, now: float) -> None:
        start = (self.bars[-1][0] + 60) if self.bars else now - HISTORY_DAYS * 86400
        try:
            got = await self.alpaca.crypto_bars(["BTC"], "1Min", start, now)
        except Exception as exc:
            self.last_error = f"BTC minutes: {exc}"
            return
        rows = got.get("BTC") or []
        if rows:
            merged = {b[0]: b for b in self.bars}
            merged.update({b[0]: b for b in rows})
            keep = now - HISTORY_DAYS * 86400
            self.bars = [merged[t] for t in sorted(merged) if t >= keep]

    async def refresh_price(self, now: float) -> None:
        try:
            snap = (await self.alpaca.crypto_snapshots(["BTC"])).get("BTC") or {}
        except Exception as exc:
            self.last_error = f"BTC price: {exc}"
            return
        p = num((snap.get("latestTrade") or {}).get("p"))
        q = snap.get("latestQuote") or {}
        if num(q.get("bp")) > 0 and num(q.get("ap")) > 0:
            p = (num(q["bp"]) + num(q["ap"])) / 2 if not p else p
        if p:
            self.live_px, self.live_ts = p, now

    def price(self) -> float:
        if self.live_px:
            return self.live_px
        return num(self.bars[-1][4]) if self.bars else 0.0

    def start_price(self, start: int) -> float:
        """Price to beat: BTC at the moment the window began (the close of the minute before it)."""
        closes = _closes(self.bars[-40:])
        return closes.get(start - 60) or 0.0

    async def tick(self, now: float) -> None:
        if not self.on or not getattr(self.alpaca, "ok", False):
            return
        if self.every("bars", 60, now) or not self.bars:
            await self.load(now)
        if self.every("price", 15, now):
            await self.refresh_price(now)
        if not self.bars:
            return
        m = self.model()
        if not m or now - num(m.get("ts")) > 6 * 3600:
            self.db.set_meta("updown_study", dumps(study(self.bars, now)))
        if self.every("quotes", 20, now):
            await self.refresh_quotes(now)
        self.save_quotes(now)
        self.record(now)
        await self.edge_alert(now)
        start = window_start(now)
        if self.cfg.flag("BTC15_ALERTS") and now - start >= 60 and self.db.once(f"btc15:{start}", int(now)):
            card = self.card(now)
            if card.get("beat"):
                await self.notify(self.short_text(card), kind="BTC15", silent=True)

    def record(self, now: float) -> None:
        """The live track record: the lean is written when a window starts, the result when it ends."""
        m = self.model()
        start = window_start(now)
        if not self.db.scalar("select 1 from updown where ts=?", (start,)):
            p0 = self.start_price(start)
            prev0 = _closes(self.bars[-40:]).get(start - WINDOW - 60)
            if p0 and now - start < 300:                             # only windows it saw begin
                d, pl = lean_for((p0 / prev0 - 1) if prev0 else None, m.get("pattern") or {})
                self.db.run("insert or ignore into updown(ts, open, lean, p_lean, k, sigma) values(?,?,?,?,?,?)",
                            (start, p0, d, pl, num(m.get("k"), 1.0), sigma_before(_closes(self.bars[-200:]), start)))
        closes = _closes(self.bars[-120:])
        for r in self.db.rows("select * from updown where outcome is null and ts<=?", (int(now) - WINDOW,)):
            end_px = closes.get(r["ts"] + WINDOW - 60)
            if not end_px:
                if now - r["ts"] > 3 * WINDOW:
                    self.db.run("delete from updown where ts=?", (r["ts"],))
                continue
            probs = _checkpoint_probs({"open": r["open"], "sigma": num(r["sigma"]),
                                       **{f"p{m_}": closes.get(r["ts"] + 60 * m_ - 60) for m_ in CHECKS}},
                                      num(r["k"], 1.0))
            self.db.run("update updown set close=?, outcome=?, p5=?, p10=? where ts=?",
                        (end_px, 1 if end_px >= r["open"] else 0, probs.get(5), probs.get(10), r["ts"]))

    # ---- the market's prices -----------------------------------------------------------------------------------------
    def venues(self) -> list[str]:
        v = int(self.cfg.get("BTC15_VENUE"))
        return list(markets.VENUES) if v == 0 else [markets.VENUES[v - 1]]

    async def refresh_quotes(self, now: float) -> None:
        session = getattr(self.alpaca, "session", None)
        if session is None:
            return
        start = window_start(now)
        for venue in self.venues():
            arg = start if venue == "polymarket" else start + WINDOW
            try:
                q = await self.fetchers[venue](session, arg)
            except Exception as exc:
                self.quote_errors[venue] = f"{type(exc).__name__}: {str(exc)[:80]}"
                q = None
            if q:
                q["window"], q["ts"] = start, now
                self.quotes[venue] = q
                self.quote_errors.pop(venue, None)
            elif self.quotes.get(venue, {}).get("window") != start:
                self.quotes.pop(venue, None)                # an old window's price is no price

    def live_quotes(self, now: float) -> dict[str, dict]:
        start = window_start(now)
        return {v: q for v, q in self.quotes.items() if q.get("window") == start and now - q["ts"] < 60}

    def save_quotes(self, now: float) -> None:
        """At 5 and 10 minutes into a window, keep what each venue charged: that's what the paper record scores."""
        start = window_start(now)
        into = now - start
        for tag, lo in (("m5", 5 * 60), ("m10", 10 * 60)):
            if not lo <= into < lo + 75:
                continue
            for venue, q in self.live_quotes(now).items():
                self.db.run("insert or ignore into updown_quotes(ts, venue, strike) values(?,?,?)",
                            (start, venue, q.get("strike")))
                self.db.run(f"update updown_quotes set {tag}_up=?, {tag}_dn=?, strike=coalesce(strike, ?) "
                            f"where ts=? and venue=? and {tag}_up is null",
                            (q["up_ask"], q["dn_ask"], q.get("strike"), start, venue))

    def paper_record(self, days: int = 30, now: float | None = None) -> dict:
        """Per venue: what following the cheap-side signal (and, for comparison, always buying the favorite) would
        have made per contract over the windows we saw prices for."""
        since = int((now or time.time()) - days * 86400)
        fee50, min_edge = self.cfg.get("BTC15_FEE_CENTS"), self.cfg.get("BTC15_MIN_EDGE")
        out = {}
        for venue in markets.VENUES:
            rows = self.db.rows("""select u.ts, u.p10, u.outcome, q.m10_up, q.m10_dn from updown u join updown_quotes q
                                  on q.ts=u.ts where q.venue=? and u.outcome is not null and u.p10 is not null
                                  and q.m10_up is not null and q.m10_dn is not null and u.ts>=?""", (venue, since))
            if not rows:
                continue
            data = [{"day": et(r["ts"]).strftime("%Y-%m-%d"), "p10": r["p10"], "outcome": r["outcome"],
                     "up_ask": r["m10_up"], "dn_ask": r["m10_dn"]} for r in rows]
            rec = markets.paper_trades(data, min_edge, fee50)
            sig = rec["signal"]
            ci = clustered_mean_ci(sig["pnls"]) if sig["n"] >= 10 else None
            rec["proven"] = bool(sig["n"] >= PROVEN_N and sig["avg_c"] is not None and sig["avg_c"] >= 1.0 and ci and ci["lo"] > 0)
            rec["ci"] = ci
            for k in ("signal", "favorite"):
                rec[k] = {x: v for x, v in rec[k].items() if x not in ("pnls", "days")} | {"days": len(rec[k]["days"])}
            out[venue] = rec
        return out

    async def edge_alert(self, now: float) -> None:
        """One message per window and venue, ten minutes in, when a side is cheap AND the live paper record says this
        signal has paid (so a signal that loses money never pings you)."""
        if not (self.cfg.flag("BTC15_EDGE_ALERTS") and 10 * 60 <= now - window_start(now) < 10 * 60 + 60):
            return
        quotes = self.live_quotes(now)
        if not quotes:
            return
        card = self.card(now)
        if not card.get("beat"):
            return
        record = self.paper_record(30, now)
        for venue, q in quotes.items():
            e = markets.edges(card["p_up"], q, self.cfg.get("BTC15_FEE_CENTS"))
            if e["edge"] >= self.cfg.get("BTC15_MIN_EDGE") and record.get(venue, {}).get("proven") \
                    and self.db.once(f"btc15edge:{window_start(now)}:{venue}", int(now)):
                sig = record[venue]["signal"]
                await self.notify(f"🟢 <b>BTC 15-min · {markets.VENUE_NAME[venue]}: {e['side']} is {e['edge']:.0f}¢ cheap</b>\n"
                                  f"Ask {e['ask']:.0f}¢ vs fair {(card['p_up'] if e['side'] == 'Up' else 1 - card['p_up']) * 100:.0f}¢ "
                                  f"· {int(card['left_min'])} min left · price to beat {fmt_price(card['beat'])}, now "
                                  f"{fmt_price(card['price'])}\nThis signal's live paper record: {sig['n']} trades, "
                                  f"{sig['avg_c']:+.1f}¢ per contract after fees.", kind="BTC15")

    def live_record(self, days: int = 7, now: float | None = None) -> dict:
        since = int((now or time.time()) - days * 86400)
        rows = self.db.rows("select * from updown where outcome is not null and ts>=?", (since,))
        lean = [r for r in rows if r["lean"]]
        out = {"windows": len(rows), "lean_n": len(lean),
               "lean_hit": sum(1 for r in lean if (r["lean"] > 0) == bool(r["outcome"])) / len(lean) if lean else None}
        for m in CHECKS:
            ps = [(num(r[f"p{m}"]), r["outcome"]) for r in rows if r[f"p{m}"] is not None]
            out[f"fav{m}"] = (sum(1 for p, y in ps if (p >= 0.5) == bool(y)) / len(ps)) if ps else None
            out[f"n{m}"] = len(ps)
        return out

    def card(self, now: float, up_cents: float | None = None, beat: float | None = None) -> dict:
        m = self.model()
        start = window_start(now)
        p0 = beat or self.start_price(start)
        px = self.price()
        left = max(0.0, (start + WINDOW - now) / 60)
        sig = sigma_before(_closes(self.bars[-200:]), start) if self.bars else 0.0     # as in the track record
        k = num(m.get("k"), 1.0)
        prob = prob_up(math.log(px / p0), sig, left, k) if px and p0 else 0.5
        closes = _closes(self.bars[-40:])
        prev0 = closes.get(start - WINDOW - 60)
        d, pl = lean_for((p0 / prev0 - 1) if p0 and prev0 else None, m.get("pattern") or {})
        out = {"start": start, "end": start + WINDOW, "beat": p0, "price": px, "left_min": left,
               "move": (px - p0) if px and p0 else None, "move_pct": (px / p0 - 1) * 100 if px and p0 else None,
               "p_up": prob, "lean": d, "lean_p": pl, "pattern": m.get("pattern"), "k": k,
               "sigma_15m_pct": sig * math.sqrt(15) * 100, "test": m.get("test"), "live": self.live_record(7, now),
               "fresh": bool(self.live_ts and now - self.live_ts < 120)}
        out["quotes"] = []
        for venue, q in self.live_quotes(now).items():
            e = markets.edges(prob, q, self.cfg.get("BTC15_FEE_CENTS"))
            out["quotes"].append({"venue": venue, "name": markets.VENUE_NAME[venue], "up": q["up_ask"], "down": q["dn_ask"],
                                  "strike": q.get("strike"), "approx": q.get("approx", False), **e})
        out["quote_errors"] = dict(self.quote_errors)
        if up_cents is not None and 0 < up_cents < 100:
            fair = prob * 100
            out["market"] = {"up": up_cents, "down": 100 - up_cents, "fair_up": fair,
                             "edge_up": fair - up_cents, "edge_down": (100 - fair) - (100 - up_cents)}
        return out

    def short_text(self, c: dict) -> str:
        side = "UP" if c["p_up"] >= 0.5 else "DOWN"
        lean = ("lean " + ("UP" if c["lean"] > 0 else "DOWN") + f" {c['lean_p'] * 100:.0f}%") if c["lean"] \
            else "no lean (coin flip at the start)"
        return (f"⏱ BTC {clock_text(c['start']).replace(' ET', '')}–{clock_text(c['end'])}: beat "
                f"{fmt_price(c['beat'])} · now {fmt_price(c['price'])} → {side} {max(c['p_up'], 1 - c['p_up']) * 100:.0f}% "
                f"· {lean}")

    def text(self, now: float, args: str = "") -> str:
        if not self.on:
            return "⏱ The BTC 15-minute call is off. /set BTC15 1 turns it on."
        if not self.bars:
            return "⏱ Loading Bitcoin's 1-minute prices… try again in a minute." + (
                f"\n{esc(self.last_error)}" if self.last_error else "")
        up_cents = beat = None
        for tok in args.replace("¢", " ").replace("$", " ").replace(",", "").split():
            v = num(tok, -1)
            if 1000 <= v:
                beat = v
            elif 0 < v < 1:
                up_cents = v * 100
            elif 1 <= v < 100:
                up_cents = v
        c = self.card(now, up_cents, beat)
        if not c["beat"]:
            return "⏱ Waiting for this window's first minute of prices — try again in a minute."
        mins, secs = int(c["left_min"]), int((c["left_min"] % 1) * 60)
        side = "UP" if c["p_up"] >= 0.5 else "DOWN"
        sure = max(c["p_up"], 1 - c["p_up"]) * 100
        lines = [f"<b>⏱ BTC 15-min Up or Down</b> · {clock_text(c['start']).replace(' ET', '')}–{clock_text(c['end'])}",
                 f"Price to beat: <b>{fmt_price(c['beat'])}</b>" + ("" if beat else " (Alpaca's price at the start — "
                                                                   "the market's own can differ by a few $)"),
                 f"Now: {fmt_price(c['price'])} ({'+' if c['move'] >= 0 else '-'}${abs(c['move']):,.0f}, "
                 f"{c['move_pct']:+.2f}%) · {mins}m {secs:02d}s left",
                 f"➡️ Most likely: <b>{side} — {sure:.0f}%</b>"]
        if sure < 55:
            lines.append("That's close to a coin flip — the price is near the line.")
        if c["lean"]:
            pat = c["pattern"]
            lines.append(f"At the start the data leaned {'UP' if c['lean'] > 0 else 'DOWN'} ({c['lean_p'] * 100:.0f}%): "
                         f"over 30 days a window {'kept going' if pat['rule'] == 'momentum' else 'turned around'} "
                         f"{max(pat['p'], 1 - pat['p']) * 100:.1f}% of {pat['n']:,} times.")
        elif c["pattern"]:
            lines.append(f"At the start it's a coin flip: over 30 days one window predicted the next only "
                         f"{c['pattern']['p'] * 100:.1f}% of {c['pattern']['n']:,} times (50% = no pattern).")
        t = c.get("test")
        if t:
            f5, f10 = t["favorite"].get("5", {}), t["favorite"].get("10", {})
            lines.append(f"Track record (last 30 days, odds it never saw): its favorite won "
                         f"{(f5.get('hit') or 0) * 100:.0f}% of the time at 5 min in and "
                         f"{(f10.get('hit') or 0) * 100:.0f}% at 10 min in ({t['windows']:,} windows).")
        min_edge = self.cfg.get("BTC15_MIN_EDGE")
        for q in c.get("quotes") or []:
            lines.append(f"{q['name']}: Up {q['up']:.0f}¢ / Down {q['down']:.0f}¢ → "
                         + (f"<b>{q['side']} is {q['edge']:.0f}¢ cheap</b> after fees" if q["edge"] >= min_edge
                            else "no real edge after fees"))
        mk = c.get("market")
        if mk:
            best = "Up" if mk["edge_up"] >= mk["edge_down"] else "Down"
            edge = max(mk["edge_up"], mk["edge_down"])
            lines.append(f"Your price: Up {mk['up']:.0f}¢ / Down {mk['down']:.0f}¢ vs fair Up {mk['fair_up']:.0f}¢ → "
                         + (f"<b>{best} is {edge:.0f}¢ cheap</b>" if edge >= 4 else "no real edge (fees eat a few ¢)"))
        lines += self.record_lines(now, bool(c.get("quotes")))
        lines.append("\n/btc15 62 — compare with a market price of 62¢ for Up · /btc15 76543 — use the market's "
                     "price to beat")
        return "\n".join(lines)

    def record_lines(self, now: float, have_quotes: bool) -> list[str]:
        """The proof: what following the cheap-side signal would have made at the market's real prices."""
        rec = self.paper_record(30, now)
        if not rec:
            return ["📒 Measuring whether the cheap-side signal pays at real market prices: it saves each venue's price "
                    "5 and 10 minutes into every window" + ("" if have_quotes else " (no prices read yet — "
                                                           + (esc("; ".join(f"{k}: {v}" for k, v in self.quote_errors.items()))
                                                              or "waiting for the first window") + ")")]
        out = []
        for venue, r in rec.items():
            sig, fav = r["signal"], r["favorite"]
            head = f"📒 {markets.VENUE_NAME[venue]} paper record, 30 days:"
            if sig["n"]:
                tail = (" ✅ proven — it will ping you" if r["proven"] else
                        f" (needs {PROVEN_N}+ signals, +1¢ and a positive 80% range to ping you)")
                out.append(f"{head} the cheap-side signal: {sig['n']} trades, won {sig['win'] * 100:.0f}%, "
                           f"<b>{sig['avg_c']:+.1f}¢ per contract</b> after fees{tail}")
            else:
                out.append(f"{head} no window had a side cheap enough yet ({r['windows']} windows watched)")
            if fav["n"]:
                out.append(f"   always buying the favorite instead: {fav['avg_c']:+.1f}¢ per contract ({fav['n']} windows)")
        return out

    def summary(self, now: float) -> dict:
        if not self.on or not self.bars:
            return {"on": self.on, "ready": False, "error": self.last_error}
        c = self.card(now)
        recent = self.db.rows("select ts, open, close, lean, outcome, p5, p10 from updown where outcome is not null "
                              "order by ts desc limit 12")
        return {"on": True, "ready": bool(c["beat"]), **c, "recent": recent, "paper": self.paper_record(30, now),
                "alerts": self.cfg.flag("BTC15_ALERTS"), "error": self.last_error, "min_edge": self.cfg.get("BTC15_MIN_EDGE"),
                "label": f"{et(c['start']).strftime('%-I:%M')}–{clock_text(c['end'])}"}

    def status_line(self, now: float) -> str:
        if not self.on:
            return ""
        m = self.model()
        t = m.get("test") or {}
        f10 = (t.get("favorite") or {}).get("10", {}).get("hit")
        live = self.live_quotes(now)
        if live:
            mk = " · market prices: " + " + ".join(markets.VENUE_NAME[v] for v in live)
        elif self.quote_errors:
            mk = " · ⚠️ market prices: " + "; ".join(f"{markets.VENUE_NAME[v]} {e}" for v, e in self.quote_errors.items())
        else:
            mk = " · market prices: not read yet"
        return ("✅ BTC 15-min call: " + ("ready" if self.bars else "loading")
                + (f" · favorite at 10 min won {f10 * 100:.0f}% (30-day test)" if f10 else "") + mk)
