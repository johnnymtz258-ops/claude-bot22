"""The scanner: finds what's in play and watches it for setups.

  • Premarket: gappers from a universe of ~250 liquid names + the day's movers and most-active stocks + your
    watchlist, ranked by gap and premarket volume, with the headline behind each move.
  • 9:35am: the day's stocks in play — the ones trading far more than usual in their first 5 minutes — for the
    in-play breakout, in one message with a buy-stop and a stop for each.
  • During the session: every completed 5-minute bar of every watched stock goes through the setup detectors
    (SPY / QQQ also through the noise band at each half hour). A setup with a proven record pings you; an unproven
    one is sent silently (or not at all — ALERT_UNPROVEN) and paper-traded either way, so the record builds.
  • 3:30pm: the day's biggest losers among liquid stocks, for the end-of-day bounce, in one message.
  • Every 10 minutes: new stocks coming into play (big move on heavy volume) join the watchlist, and you hear
    about them — the "found it before I did" part.
"""
from __future__ import annotations

import datetime as dt
import statistics

from .accounts import account_size, fmt_qty, size_shares, spendable
from .alpaca import AlpacaError, parse_ts, snap_price
from .backtest import (BAND_SYMBOLS, INPLAY_LOOKBACK, NO_EDGE, UNIVERSE, Edge, band_moves, band_table, describe,
                       rank_in_play)
from .universe import LEVERED, LEVERED_NAME, all_stocks, liquid
from .bars import C, H, L, O, T, V, atr, rvol
from .clock import Market, at, clock_text, day_of, et
from .news import catalysts, main_catalyst, warning
from .options import pick_idea, size_contracts
from .setups import EOD_START, Signal, detect, eod_rank, eod_signal, label
from .util import big, dumps, esc, loads, money, num, pct, price as fmt_price

ALWAYS = ["SPY", "QQQ"]
DISCOVER_PER_DAY = 8


class Scanner:
    def __init__(self, cfg, db, data, market: Market, edge: Edge, paper, notify, charts=None):
        self.cfg, self.db, self.data, self.market, self.edge, self.paper, self.notify = cfg, db, data, market, edge, paper, notify
        self.charts = charts
        self.done: dict[str, int] = {}          # symbol -> number of 5-min bars already judged today
        self.fired: dict[str, set] = {}         # symbol -> setups that already fired today
        self.day = ""
        self.extra: list[str] = []              # symbols other parts want watched (open positions)
        self.last_error = ""
        self.last_scan = 0.0
        self.in_play_today: dict[str, tuple[int, float]] = {}    # symbol -> (rank, first-5-minute relative volume)
        self.in_play_day = ""
        self.first5_hist: dict[str, list[float]] = {}            # usual first-5-minute volume, last 14 days
        self.first5_day = ""
        self.bands: dict[str, dict] = {}                          # SPY / QQQ noise band for today
        self.bands_day = ""
        self._prepare_try = 0.0
        self.eod_day = ""
        self.broad: list[str] = []                                 # every liquid listed stock (refreshed daily)
        self.broad_day = ""

    # ---- the watchlist -----------------------------------------------------------------------------------------
    def watchlist(self, day: str) -> list[str]:
        rows = self.db.rows("select symbol from watch where day=? order by score desc", (day,))
        syms = list(dict.fromkeys(ALWAYS + list(self.cfg.watchlist) + self.extra + [r["symbol"] for r in rows]))
        return syms[: max(int(self.cfg.get("WATCH_MAX")), len(ALWAYS) + len(self.extra))]

    async def levered(self, symbol: str) -> bool:
        """A leveraged or inverse fund? Known list first, then the fund's name from Alpaca (asked once, kept)."""
        if symbol in LEVERED:
            return True
        if symbol in self.cfg.watchlist:
            return False                                    # you asked for it: keep it
        key = f"asset_name:{symbol}"
        name = self.db.get_meta(key, None)
        if name is None:
            try:
                name = str((await self.data.alpaca.asset(symbol)).get("name") or "")
            except (AlpacaError, AttributeError):
                return False                                # couldn't ask: keep it, ask again next time
            self.db.set_meta(key, name)
        return bool(LEVERED_NAME.search(name))

    async def market_wide(self, now: float) -> list[str]:
        """Once a day: every listed stock that trades enough to day-trade (about 2,000), so gappers and stocks in
        play come from the whole market — like the research — not only the big names."""
        day = day_of(now)
        if self.broad_day == day:
            return self.broad
        self.broad_day = day                               # once a day, even if Alpaca can't be asked
        try:
            names = await all_stocks(self.data.alpaca, self.cfg.state_dir / "stocks.json", now)
            got = await liquid(self.data.alpaca, names, now - 40 * 86400, at(day, "00:00"), self.cfg, keep_bars=False)
            self.broad = sorted(x for x in got if x not in LEVERED)
        except (AlpacaError, AttributeError) as exc:
            self.last_error = f"market-wide list: {exc}"
        return self.broad

    def _in_play_candidates(self, day: str) -> list[str]:
        rows = self.db.rows("select symbol from watch where day=? and source in ('gapper','in-play')", (day,))
        return sorted((set(UNIVERSE) | set(self.cfg.watchlist) | {r["symbol"] for r in rows} | set(self.broad))
                      - LEVERED)

    async def prepare(self, now: float) -> None:
        """Once a day: the SPY / QQQ noise band (from the last 14 days) and everyone's usual first 5 minutes."""
        day = day_of(now)
        if (self.bands_day == day and (self.first5_day == day or self.cfg.get("ORB5_TOP") <= 0)) \
                or now - self._prepare_try < 300:
            return                                         # done today, or failed less than 5 minutes ago
        self._prepare_try = now
        await self.market_wide(now)
        if self.bands_day != day:
            days = self.market.trading_days_back(day, INPLAY_LOOKBACK + 4)
            try:
                got = await self.data.alpaca.bars(list(BAND_SYMBOLS), "5Min", at(days[0], "04:00"), at(day, "00:00"),
                                                  feed="sip")
                for sym in BAND_SYMBOLS:
                    hist = [band_moves([b for b in got.get(sym) or [] if day_of(b[T]) == d], d, self.market)
                            for d in days]
                    table = band_table(hist)
                    if table:
                        self.bands[sym] = table
                self.bands_day = day
            except AlpacaError as exc:
                self.last_error = f"band: {exc}"
        if self.first5_day != day and self.cfg.get("ORB5_TOP") > 0:
            cands = self._in_play_candidates(day)
            hist: dict[str, list[float]] = {}
            try:
                for d in self.market.trading_days_back(day, INPLAY_LOOKBACK):
                    o = self.market.open_ts(d)
                    got = await self.data.alpaca.bars(cands, "5Min", o, o + 300)     # the live feed, like today's
                    for sym, rows in got.items():
                        if rows and rows[0][T] == o:
                            hist.setdefault(sym, []).append(rows[0][V])
                self.first5_hist, self.first5_day = hist, day
            except AlpacaError as exc:
                self.last_error = f"in play: {exc}"
        if self.bands_day == day and (self.first5_day == day or self.cfg.get("ORB5_TOP") <= 0):
            self._prepare_try = 0.0

    async def rank_in_play(self, now: float) -> list[tuple[str, int, float]]:
        """At 9:35: the day's stocks in play, best first — [(symbol, rank, rvol5)]. Stored, so a restart keeps it."""
        day = day_of(now)
        top = int(self.cfg.get("ORB5_TOP"))
        if self.in_play_day == day or top <= 0:
            return []
        saved = self.db.rows("select symbol, data from watch where day=? and source='inplay'", (day,))
        if saved:
            self.in_play_today = {r["symbol"]: ((loads(r["data"], {}) or {}).get("rank", 0),
                                                (loads(r["data"], {}) or {}).get("rvol5", 0.0)) for r in saved}
            self.in_play_day = day
            return []
        open_ts = self.market.open_ts(day)
        if now < open_ts + 300 + 15:
            return []
        if now > open_ts + 3600:
            self.in_play_day = day                         # started too late to rank today
            return []
        give_up = now > open_ts + 420                      # couldn't rank by 9:37: today goes on without it
        await self.prepare(now)
        if self.first5_day != day:
            if give_up:
                self.in_play_day = day
            return []
        cands = self._in_play_candidates(day)
        try:
            await self.data.load_daily(cands, now)
            got = await self.data.alpaca.bars(cands, "1Min", open_ts, open_ts + 300)
        except AlpacaError as exc:
            self.last_error = f"in play: {exc}"
            if give_up:
                self.in_play_day = day
            return []
        first5 = {}
        for sym, rows in got.items():
            rows = [b for b in rows if open_ts <= b[T] < open_ts + 300]
            if rows:
                first5[sym] = (open_ts, rows[0][O], max(b[H] for b in rows), min(b[L] for b in rows), rows[-1][C],
                               sum(b[V] for b in rows))
        daily = {sym: self.data.daily(sym) for sym in first5}
        ranked = []
        for sym, _, rv in rank_in_play(first5, self.first5_hist, daily, day, self.cfg, top + 5):
            if await self.levered(sym):
                continue
            ranked.append((sym, len(ranked) + 1, rv))
            if len(ranked) >= top:
                break
        for sym, rank, rv in ranked:
            self.db.run("""insert into watch(day, symbol, source, score, data, added) values(?,?,?,?,?,?)
                           on conflict(day, symbol) do update set source='inplay', score=excluded.score,
                           data=excluded.data""",
                        (day, sym, "inplay", 3000 - rank, dumps({"rank": rank, "rvol5": rv, "first5": first5[sym][1:]}),
                         int(now)))
        self.in_play_today = {sym: (rank, rv) for sym, rank, rv in ranked}
        self.in_play_day = day
        return ranked

    def _reset(self, day: str) -> None:
        if self.day != day:
            self.day = day
            self.done, self.fired = {}, {}
            for r in self.db.rows("select symbol, setup, direction from alerts where day=?", (day,)):
                self.fired.setdefault(r["symbol"], set()).add(f"{r['setup']}_{r['direction']}")

    # ---- premarket ---------------------------------------------------------------------------------------------
    async def premarket(self, now: float) -> list[dict]:
        """Rank today's gappers. Stored in the watch table; returns them best first."""
        day = day_of(now)
        cands = set(UNIVERSE) | set(self.cfg.watchlist) | set(await self.market_wide(now))
        try:
            movers = await self.data.alpaca.movers(50)
            for side in ("gainers", "losers"):
                cands |= {str(m.get("symbol")) for m in movers.get(side) or [] if m.get("symbol")}
            cands |= {str(m.get("symbol")) for m in await self.data.alpaca.most_actives(50) if m.get("symbol")}
        except AlpacaError as exc:
            self.last_error = str(exc)
        cands = sorted(s for s in cands if s and s.isascii() and len(s) <= 5 and s not in LEVERED)
        await self.data.load_daily(cands, now)
        open_ts = self.market.open_ts(day)
        end = min(now, open_ts)
        try:
            pre = await self.data.alpaca.bars(cands, "5Min", at(day, "04:00"), end, feed="sip")
            snaps = await self.data.alpaca.snapshots(cands) if now >= at(day, "08:00") else {}
        except AlpacaError as exc:
            self.last_error = str(exc)
            return []
        out = []
        for s in cands:
            daily = self.data.daily(s)
            if len(daily) < 15:
                continue
            pdc = daily[-1][C]
            avg_vol = sum(b[V] for b in daily[-20:]) / len(daily[-20:])
            rows = [b for b in pre.get(s) or [] if b[T] < open_ts]
            pm_vol = sum(b[V] for b in rows)
            last = rows[-1][C] if rows else 0.0
            snap = snaps.get(s) or {}
            trade = snap.get("latestTrade") or {}
            if parse_ts(trade.get("t")) >= at(day, "04:00") and snap_price(snap) > 0:
                last = snap_price(snap)
            session_open = now >= open_ts
            if session_open:
                last = self.data.last_price(s) or last
            if not last or not pdc:
                continue
            gap = (last / pdc - 1) * 100
            if abs(gap) < self.cfg.get("GAP_MIN_PCT") or last < self.cfg.get("MIN_PRICE"):
                continue
            pm_rvol = pm_vol / (avg_vol * 0.02) if avg_vol else 0.0    # a normal premarket trades ~2% of the day
            if avg_vol < self.cfg.get("MIN_AVG_VOLUME") and pm_vol < 300_000:
                continue
            score = abs(gap) * min(max(pm_rvol, 0.2), 6.0) + (3 if s in self.cfg.watchlist else 0)
            out.append({"symbol": s, "gap": gap, "price": last, "pdc": pdc, "pm_vol": pm_vol, "pm_rvol": pm_rvol,
                        "avg_vol": avg_vol, "pmh": max((b[2] for b in rows), default=0.0),
                        "pml": min((b[3] for b in rows), default=0.0), "pdh": daily[-1][2], "pdl": daily[-1][3],
                        "score": score, "open": session_open})
        out.sort(key=lambda g: -g["score"])
        top = []
        for g in out:
            if len(top) >= 15:
                break
            if g["symbol"] not in self.cfg.watchlist and await self.levered(g["symbol"]):
                continue
            top.append(g)
        for g in top:
            try:
                news = catalysts(await self.data.news(g["symbol"], 30), g["symbol"])
            except Exception:
                news = []
            cat = main_catalyst(news)
            g["catalyst"] = {"headline": cat["headline"], "tags": [t[0] for t in cat["tags"]]} if cat else None
            g["warning"] = warning(news)
            self.db.run("""insert into watch(day, symbol, source, score, data, added) values(?,?,?,?,?,?)
                           on conflict(day, symbol) do update set score=excluded.score, data=excluded.data""",
                        (day, g["symbol"], "gapper", g["score"], dumps(g), int(now)))
        self.last_scan = now
        return top

    def gappers(self, day: str) -> list[dict]:
        rows = self.db.rows("select * from watch where day=? and source='gapper' order by score desc", (day,))
        return [loads(r["data"], {}) or {} for r in rows]

    # ---- in-play discovery during the day ----------------------------------------------------------------------
    async def discover(self, now: float) -> list[dict]:
        day = day_of(now)
        if int(self.db.scalar("select count(*) from watch where day=? and source='in-play'", (day,), default=0)) >= DISCOVER_PER_DAY:
            return []
        try:
            movers = await self.data.alpaca.movers(30)
            actives = await self.data.alpaca.most_actives(30)
        except AlpacaError as exc:
            self.last_error = str(exc)
            return []
        have = set(self.watchlist(day))
        cands = {str(m.get("symbol")) for side in ("gainers", "losers") for m in movers.get(side) or []}
        cands |= {str(m.get("symbol")) for m in actives}
        cands = sorted(s for s in cands - have if s and s.isascii() and len(s) <= 5)
        if not cands:
            return []
        await self.data.load_daily(cands, now)
        snaps = await self.data.snapshots(cands)
        found = []
        minutes = (now - self.market.open_ts(day)) / 60
        for s in cands:
            daily = self.data.daily(s)
            snap = snaps.get(s) or {}
            px = snap_price(snap)
            if len(daily) < 15 or px < self.cfg.get("MIN_PRICE"):
                continue
            pdc = daily[-1][C]
            chg = (px / pdc - 1) * 100 if pdc else 0.0
            live_avg = self.data.live_avg.get(s, (0, 0.0))[1]
            vol_today = num((snap.get("dailyBar") or {}).get("v"))
            rv = rvol(vol_today, minutes, live_avg) if live_avg else 0.0
            avg_full = sum(b[V] for b in daily[-20:]) / len(daily[-20:])
            if abs(chg) < 5 or rv < 3 or avg_full < self.cfg.get("MIN_AVG_VOLUME") * 0.5:
                continue
            found.append({"symbol": s, "chg": chg, "price": px, "rvol": rv, "score": abs(chg) * min(rv, 8)})
        found.sort(key=lambda f: -f["score"])
        found = [f for f in found if not await self.levered(f["symbol"])]
        for f in found[:3]:
            news = catalysts(await self.data.news(f["symbol"], 24), f["symbol"])
            cat = main_catalyst(news)
            f["catalyst"] = {"headline": cat["headline"], "tags": [t[0] for t in cat["tags"]]} if cat else None
            self.db.run("""insert or ignore into watch(day, symbol, source, score, data, added) values(?,?,?,?,?,?)""",
                        (day, f["symbol"], "in-play", 1000 + f["score"], dumps(f), int(now)))
            line = (f"🆕 <b>In play: {esc(f['symbol'])} {pct(f['chg'])}</b> at {fmt_price(f['price'])} on "
                    f"{f['rvol']:.1f}× normal volume — now on the watchlist for setups")
            if f["catalyst"]:
                line += f"\n📰 {esc(f['catalyst']['headline'][:150])}"
            bad = warning(news)
            if bad:
                line += f"\n🔴 {esc(bad)}"
            await self.notify(line, silent=True, kind="INPLAY", symbol=f["symbol"],
                              buttons=[[("📈 Chart", None, f"chart:{f['symbol']}"), ("🧾 Check", None, f"check:{f['symbol']}")]])
        return found[:3]

    # ---- live setups -------------------------------------------------------------------------------------------
    async def tick(self, now: float) -> list[Signal]:
        """Judge every newly completed 5-minute bar of every watched stock. Returns the signals sent."""
        day = day_of(now)
        self._reset(day)
        await self.prepare(now)
        await self.rank_in_play(now)
        syms = self.watchlist(day)
        await self.data.load_daily(syms, now)
        await self.data.refresh(syms, now)
        sent, in_play = [], []
        for s in syms:
            d = self.data.day_obj(s, now)
            if not d:
                continue
            if d.bars[-1][C] < self.cfg.get("MIN_PRICE"):
                continue
            d.in_play, d.rvol5 = self.in_play_today.get(s, (0, 0.0))
            d.band = self.bands.get(s)
            start = self.done.get(s, 0)
            fired = self.fired.setdefault(s, set())
            values = {"ORB_MINUTES": self.cfg.get("ORB_MINUTES"), "GAP_MIN_PCT": self.cfg.get("GAP_MIN_PCT")}
            ranked = self.in_play_day == day or self.cfg.get("ORB5_TOP") <= 0
            upto = len(d.bars) if ranked else 0             # the first candle waits for the in-play ranking
            for i in range(start, upto):
                sigs = [g for g in detect(d.upto(i), i, values, fired) if now - g.ts <= 10 * 60]   # old bars: too late
                in_play += [g for g in sigs if g.setup == "orb5"]
                sigs = [g for g in sigs if g.setup != "orb5"]
                for direction in ("long", "short"):
                    group = [g for g in sigs if g.direction == direction]
                    if not group:
                        continue
                    group.sort(key=lambda g: RANK.get(self.edge.status(g.setup, g.direction)["status"], 9))
                    if await self.alert(group[0], now, also=group[1:]):
                        sent.append(group[0])
            self.done[s] = max(start, upto)
        if in_play:
            await self.alert_in_play(in_play, now)
            sent += in_play
        sent += await self.end_of_day(now)
        return sent

    async def end_of_day(self, now: float) -> list[Signal]:
        """3:30pm: rank the liquid universe by its return since yesterday's close; the biggest losers are bought for
        the end-of-day bounce (the biggest winners measured on paper as the short side). One message."""
        day = day_of(now)
        close = self.market.close_ts(day)
        t0 = close - EOD_START
        if self.eod_day == day or not t0 <= now < t0 + 600:
            return []
        cands = [x for x in UNIVERSE if x not in LEVERED and x not in BAND_SYMBOLS]
        await self.data.load_daily(cands, now)
        snaps = await self.data.snapshots(cands)
        if not snaps:
            return []                                      # try again on the next tick
        self.eod_day = day
        moves, info = {}, {}
        for sym in cands:
            daily, price = self.data.daily(sym), snap_price(snaps.get(sym) or {})
            if len(daily) < 15 or price <= 0 or daily[-1][C] <= 0:
                continue
            if daily[-1][C] < self.cfg.get("MIN_PRICE") or \
                    statistics.mean(d[V] for d in daily[-14:]) < self.cfg.get("MIN_AVG_VOLUME"):
                continue
            moves[sym] = price / daily[-1][C] - 1
            info[sym] = (price, atr(daily[-15:], 14))
        losers, winners = eod_rank(moves)
        sigs = []
        for direction, names in (("long", losers), ("short", winners)):
            for rank, sym in enumerate(names, 1):
                sig = eod_signal(sym, direction, info[sym][0], int(now), close, info[sym][1], moves[sym], rank)
                if sig:
                    sigs.append(sig)
        for sig in sigs:
            await self.alert(sig, now, quiet=True)
        longs = [g for g in sigs if g.direction == "long"]
        st = self.edge.status("eod", "long")
        proven = st["status"] == "proven"
        if longs and self.cfg.flag("ALERTS_ENABLED") and st["status"] not in NO_EDGE \
                and (proven or self.cfg.flag("ALERT_UNPROVEN")):
            text = format_eod(longs[:8], st, account_size(self.db, self.cfg), self.cfg, now, cash=spendable(self.db))
            await self.notify(text, silent=not proven, kind="SETUP", symbol=longs[0].symbol,
                              buttons=[[("📈 Chart " + longs[0].symbol, None, f"chart:{longs[0].symbol}")]])
        return sigs

    async def alert_in_play(self, sigs: list[Signal], now: float) -> None:
        """Every in-play breakout is recorded and paper-traded; you get one message with the best of them."""
        sigs = sorted(sigs, key=lambda g: g.context.get("rank", 99))
        for sig in sigs:
            await self.alert(sig, now, quiet=True)
        longs = [g for g in sigs if g.direction == "long"]
        shorts = [g for g in sigs if g.direction == "short"] if self.cfg.flag("SHORT_ALERTS") else []
        st = self.edge.status("orb5", "long")
        proven = st["status"] == "proven"
        if not self.cfg.flag("ALERTS_ENABLED") or not (longs or shorts) or st["status"] in NO_EDGE:
            return
        if not proven and not self.cfg.flag("ALERT_UNPROVEN"):
            return
        account = account_size(self.db, self.cfg)
        text = format_in_play(longs[:6], shorts[:3], st, account, self.cfg, now, cash=spendable(self.db))
        first = (longs or shorts)[0].symbol
        await self.notify(text, silent=not proven, kind="SETUP", symbol=first,
                          buttons=[[("📈 Chart " + first, None, f"chart:{first}")]])

    def _too_many(self, sig: Signal, now: float) -> bool:
        hour = int(self.db.scalar("select count(*) from alerts where ts>=? and silent=0", (int(now) - 3600,), default=0))
        return hour >= self.cfg.get("MAX_ALERTS_PER_HOUR")

    async def option_idea(self, sig: Signal, now: float, account: float) -> dict | None:
        if not self.cfg.flag("OPTION_IDEAS"):
            return None
        today = et(now).date()
        try:
            chain = await self.data.chain(sig.symbol, None, "call" if sig.direction == "long" else "put", sig.entry)
            oi = await self.data.open_interest(sig.symbol, today.isoformat(), (today + dt.timedelta(days=21)).isoformat(),
                                               sig.entry)
        except Exception as exc:
            self.last_error = f"options {type(exc).__name__}"
            return None
        idea = pick_idea(chain, oi, sig.direction, sig.entry, today, int(self.cfg.get("OPTION_MIN_DTE")),
                         self.cfg.get("OPTION_MAX_SPREAD_PCT"), self.cfg.get("OPTION_MIN_OI"))
        if not idea:
            return None
        budget = (account or self.cfg.get("PAPER_ACCOUNT")) * self.cfg.get("RISK_PER_TRADE_PCT") / 100
        idea["size"] = size_contracts(idea, sig.risk, budget, account or self.cfg.get("PAPER_ACCOUNT"),
                                      self.cfg.get("OPTIONS_MAX_PREMIUM_PCT"), spendable(self.db) if account else None)
        return idea

    async def alert(self, sig: Signal, now: float, account: float = 0.0, also: list[Signal] | None = None,
                    quiet: bool = False) -> int:
        """Record, paper-trade and (maybe) send one setup. `also`: other setups that fired on the same bar in the
        same direction — paper-traded too, mentioned in the one message. quiet: record and paper-trade only (the
        in-play breakouts go out together in one message)."""
        st = self.edge.status(sig.setup, sig.direction)
        proven = st["status"] == "proven"
        silent = not proven
        send = self.cfg.flag("ALERTS_ENABLED") and (proven or self.cfg.flag("ALERT_UNPROVEN"))
        if st["status"] in NO_EDGE:
            send = False                                # known losers: paper only
        if sig.direction == "short" and not self.cfg.flag("SHORT_ALERTS"):
            send = False                                # your broker can't short: paper only unless you turn it on
        if quiet:
            send = False
        if send and not silent and self._too_many(sig, now):
            silent = True
        account = account or account_size(self.db, self.cfg)
        idea = await self.option_idea(sig, now, account) if send else None
        alert_id = self.db.insert("""insert into alerts(ts, day, symbol, setup, direction, entry, stop, t1, t2, proven,
                                     silent, reason, context, option_idea) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                  (int(now), day_of(now), sig.symbol, sig.setup, sig.direction, sig.entry, sig.stop,
                                   sig.t1, sig.t2, int(proven), int(silent or not send), sig.reason, dumps(sig.context),
                                   dumps(idea) if idea else ""))
        follows = ((proven or self.cfg.flag("ALERT_UNPROVEN")) and st["status"] not in NO_EDGE
                   and (sig.direction == "long" or self.cfg.flag("SHORT_ALERTS")))
        self.paper.open(sig, alert_id, day_of(now), auto=follows)       # the autopilot follows what you'd be sent
        for other in also or []:
            other_id = self.db.insert("""insert into alerts(ts, day, symbol, setup, direction, entry, stop, t1, t2, proven,
                                         silent, reason, context) values(?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                                      (int(now), day_of(now), other.symbol, other.setup, other.direction, other.entry,
                                       other.stop, other.t1, other.t2,
                                       int(self.edge.status(other.setup, other.direction)["proven"]), other.reason,
                                       dumps(other.context)))
            self.paper.open(other, other_id, day_of(now))
        self.db.event(int(now), "SETUP", f"{sig.symbol} {sig.label}: {sig.reason}", sig.symbol)
        if not send:
            return alert_id
        text = format_alert(sig, st, idea, account, self.cfg, now, also, cash=spendable(self.db))
        photo = None
        if self.charts:
            photo = self.charts.render(sig.symbol, self.data.bars1(sig.symbol), day_of(now), self.market,
                                       levels=self.data.levels(sig.symbol, now),
                                       plan={"entry": sig.entry, "stop": sig.stop if sig.plan != "band" else 0,
                                             "t1": sig.t1, "t2": sig.t2},
                                       title=f"{sig.symbol} · {sig.label}")
        msg_id = await self.notify(text, silent=silent, kind="SETUP", symbol=sig.symbol, photo=photo,
                                   buttons=[[("🧾 Check it", None, f"check:{sig.symbol}"),
                                             ("📈 Chart", None, f"chart:{sig.symbol}")]])
        if msg_id:
            self.db.run("update alerts set tg_message_id=? where id=?", (msg_id, alert_id))
        return alert_id


RANK = {"proven": 0, "promising": 1, "untested": 2, "thin": 3, "flat": 4, "negative": 5, "benched": 6}


def format_alert(sig: Signal, st: dict, idea: dict | None, account: float, cfg, now: float,
                 also: list[Signal] | None = None, cash: float | None = None) -> str:
    icon = "🟢 LONG" if sig.direction == "long" else "🔴 SHORT"
    badge = "✅ proven setup" if st["status"] == "proven" else "🧪 unproven — paper is measuring it, size small"
    lines = [f"<b>{icon} {esc(sig.symbol)} · {esc(sig.label)}</b>", badge, esc(sig.reason[:1].upper() + sig.reason[1:])]
    if also:
        lines.append("Also fits: " + ", ".join(esc(o.label) for o in also))
    risk = sig.risk
    long = sig.direction == "long"
    if sig.plan == "band":
        lines.append(f"🎯 Entry ~{fmt_price(sig.entry)} · no fixed stop: get out when a half-hour close (10:30, 11:00…) "
                     f"is back {'under' if long else 'over'} the band or VWAP · out by 3:55pm · 1R ≈ ${risk:.2f}/share")
    elif sig.plan == "close":
        lines.append(f"🎯 {'Buy' if long else 'Sell'}-stop {fmt_price(sig.entry)} · stop {fmt_price(sig.stop)} "
                     f"({'−' if long else '+'}${risk:.2f}/share) · no target: hold until 3:55pm unless stopped")
    else:
        lines.append(f"🎯 Entry ~{fmt_price(sig.entry)} · stop {fmt_price(sig.stop)} ({'−' if long else '+'}"
                     f"${risk:.2f}/share) · half at {fmt_price(sig.t1)} (1R) · rest {fmt_price(sig.t2)} (2R) · out by 3:55pm")
    if account and risk > 0:
        budget = account * cfg.get("RISK_PER_TRADE_PCT") / 100
        sz = size_shares(budget, risk, sig.entry, cash)
        if sz["capped"] and sz["shares"] <= 0:
            lines.append(f"💼 You have {money(cash or 0)} free to buy with — not enough for this one. Watch it or "
                         "follow it on paper")
        elif sz["capped"]:
            lines.append(f"💼 Your {money(cash)} cash buys {fmt_qty(sz['shares'])} shares (~{money(sz['cost'])}, "
                         f"{money(sz['risk_usd'])} at risk at the stop)")
        elif sz["shares"] > 0:
            lines.append(f"💼 At your {cfg.get('RISK_PER_TRADE_PCT'):g}% risk ({money(budget)}): "
                         f"{fmt_qty(sz['shares'])} shares (~{money(sz['cost'])})")
    if idea:
        sz = idea.get("size") or {}
        txt = (f"🎟 Option idea: {esc(idea['underlying'])} {idea['expiry'][5:].replace('-', '/')} {idea['strike']:g}"
               f"{idea['type'][0].upper()} ~{fmt_price(idea['mid'])} (Δ {abs(idea['delta']):.2f}, spread "
               f"{idea['spread_pct']:.0f}%" + (f", OI {big(idea['oi'])}" if idea.get("oi", -1) >= 0 else "") + ")")
        if sz.get("contracts"):
            txt += f" → {sz['contracts']} contract{'s' if sz['contracts'] != 1 else ''} ≈ {money(sz['loss_at_stop'])} at the stop"
        elif sz.get("premium_each"):
            txt += f" → 1 contract ≈ {money(sz['premium_each'])}: too big for your account right now"
        txt += " · free-feed quote: confirm the real bid/ask in your app"
        lines.append(txt)
    lines.append(f"📊 Record: {describe(st)}")
    lines.append(f"<i>{clock_text(sig.ts)} · {label(sig.setup, sig.direction)}</i>")
    return "\n".join(lines)


def format_in_play(longs: list[Signal], shorts: list[Signal], st: dict, account: float, cfg, now: float,
                   cash: float | None = None) -> str:
    """The 9:35 message: today's stocks in play with a buy-stop and a stop for each."""
    badge = "✅ proven setup" if st["status"] == "proven" else "🧪 being tested — paper and the autopilot are measuring it"
    lines = [f"<b>🔥 Stocks in play · {clock_text(now)}</b>", badge,
             "In-play 5-min breakout: buy only if it trades above the first 5-minute high (a buy-stop order), stop at "
             "10% of its daily range, no target — hold to 3:55pm. Most trigger and get stopped for a small loss; "
             "a few run all day and pay for them."]
    budget = account * cfg.get("RISK_PER_TRADE_PCT") / 100 if account else 0.0
    for n, sig in enumerate(longs + shorts, 1):
        verb = "buy-stop" if sig.direction == "long" else "sell-stop (short)"
        line = (f"{n}. <b>{esc(sig.symbol)}</b> {verb} {fmt_price(sig.entry)} · stop {fmt_price(sig.stop)} "
                f"({'−' if sig.direction == 'long' else '+'}${sig.risk:.2f}/sh) · "
                f"{num(sig.context.get('rvol5')):.1f}x opening volume")
        if budget and sig.risk > 0:
            sz = size_shares(budget, sig.risk, sig.entry, cash)
            line += f" · 💼 {fmt_qty(sz['shares'])} sh" if sz["shares"] > 0 else " · 💼 no cash free"
        lines.append(line)
    lines.append(f"📊 Record: {describe(st)}")
    lines.append("<i>Orders not filled by 3:00pm are cancelled. The autopilot follows these on pretend money.</i>")
    return "\n".join(lines)


def format_eod(longs: list[Signal], st: dict, account: float, cfg, now: float, cash: float | None = None) -> str:
    """The 3:30pm message: the day's biggest losers, to hold into the close."""
    badge = "✅ proven setup" if st["status"] == "proven" else "🧪 being tested — paper and the autopilot are measuring it"
    lines = [f"<b>🔄 End-of-day bounce · {clock_text(now)}</b>", badge,
             "The day's biggest losers tend to bounce in the last half hour (Baltussen, Da & Soebhag; 2nd place, "
             "Quantpedia Awards 2025). Buy now, sell at 3:59pm, no stop. The edge is small — a few hundredths of a "
             "percent on average — so it only pays where buying and selling costs very little."]
    budget = account * cfg.get("RISK_PER_TRADE_PCT") / 100 if account else 0.0
    for n, sig in enumerate(longs, 1):
        line = (f"{n}. <b>{esc(sig.symbol)}</b> {pct(num(sig.context.get('move')) * 100)} today · "
                f"{fmt_price(sig.entry)}")
        if budget and sig.risk > 0:
            sz = size_shares(budget, sig.risk, sig.entry, cash)
            line += f" · 💼 {fmt_qty(sz['shares'])} sh" if sz["shares"] > 0 else " · 💼 no cash free"
        lines.append(line)
    lines.append(f"📊 Record: {describe(st)}")
    return "\n".join(lines)
