"""DayBot: wires the parts together and runs them.

Loops
  market   every ~30s: calendar, premarket gappers, the 9:00 game plan, setups on every completed 5-minute bar,
           paper trades, your open trades vs their plans, in-play discovery, the 4:10pm day report, Friday's
           week report, Saturday's backtest
  broker   every 20s while the market is active (5 min otherwise): your fills -> checks and recaps
  telegram commands, chart screenshots, buttons (only from your own chat)
  web      the dashboard at http://localhost:8788
"""
from __future__ import annotations

import asyncio
import datetime as dt
import ssl
import time
import traceback

import aiohttp

from . import VERSION, charts as charts_mod
from .ai import AI
from .alpaca import Alpaca, AlpacaError
from .backtest import SETUPS_VERSION, STATUS_TEXT, Edge, run as run_backtest_job, save as save_backtest
from .broker import BrokerSync
from .check import TradeCheck
from .clock import Market, at, clock_text, day_of, et
from .coach import day_report as coach_day_report, leaks, week_report
from .commands import Commands
from .config import Config, load
from .data import MarketData
from .db import Database
from .earnings import Earnings
from .ledger import NOT_HOLDING, Ledger
from .monitor import Monitor
from .autopilot import Autopilot
from .paper import Paper
from .plan import build as build_plan
from .scanner import Scanner
from .setups import SETUP_NAMES, label
from .snaptrade import SnapTrade
from .tg import Notifier, Telegram
from .util import dumps, esc, loads, num


class App:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession | None = None, *, db: Database | None = None,
                 telegram: Telegram | None = None, alpaca: Alpaca | None = None, snap: SnapTrade | None = None,
                 charts=None, notify=None):
        self.cfg = cfg
        self.session = session
        self.db = db or Database(cfg.db_path)
        self._restore_settings()
        self.market = Market()
        self.alpaca = alpaca or Alpaca(session, cfg.alpaca_key, cfg.alpaca_secret, cfg.data_feed, cfg.alpaca_paper)
        self.data = MarketData(self.alpaca, self.market, cfg)
        self.ledger = Ledger(self.db)
        self.edge = Edge(self.db)
        self.earnings = Earnings(session, cfg.finnhub_key) if cfg.finnhub_key else None
        self.checker = TradeCheck(cfg, self.db, self.data, self.market, self.ledger, self.edge, self.earnings)
        self.tg = telegram
        owner = self.db.get_meta("owner_chat")
        if not cfg.telegram_chat_id and owner:          # linked earlier by your first message to the bot
            cfg.telegram_chat_id = owner
            if telegram:
                telegram.chat_id = owner
        self.notify = notify or Notifier(telegram, self.db)
        self.paper = Paper(cfg, self.db, self.data, self.market)
        self.autopilot = Autopilot(cfg, self.db, self.data, self.market, self.notify)
        self.paper.auto = self.autopilot
        if charts is False:
            self.charts = None
        else:
            self.charts = charts if charts is not None else (charts_mod if charts_mod.available() else None)
        self.scanner = Scanner(cfg, self.db, self.data, self.market, self.edge, self.paper, self.notify, self.charts)
        self.monitor = Monitor(cfg, self.db, self.data, self.market, self.notify)
        self.snap = snap or SnapTrade(session, cfg.snaptrade_client_id, cfg.snaptrade_consumer_key,
                                      cfg.snaptrade_user_id, cfg.snaptrade_user_secret)
        self.broker = BrokerSync(cfg, self.db, self.snap, self.ledger, self.checker, self.notify, self.market, self.data)
        self.ai = AI(cfg, self.db)
        self.commands = Commands(self)
        self.backtest_task: asyncio.Task | None = None
        self.backtest_progress = ""
        self.last_error = ""
        self.started = time.time()
        self._last = {}

    def _restore_settings(self) -> None:
        for row in self.db.rows("select key, value from meta where key like 'set:%'"):
            try:
                self.cfg.set(row["key"][4:], row["value"])
            except ValueError:
                pass
        saved = loads(self.db.get_meta("watchlist"), None)
        if isinstance(saved, list):
            self.cfg.watchlist = [str(s) for s in saved]

    def every(self, name: str, seconds: float, now: float) -> bool:
        if now - self._last.get(name, 0) >= seconds:
            self._last[name] = now
            return True
        return False

    def account_size(self) -> float:
        return self.checker.account_size()

    # ---- the market loop -----------------------------------------------------------------------------------------
    async def load_calendar(self, now: float) -> None:
        day = day_of(now)
        if self.db.get_meta("calendar_day") == day and self.market.days:
            return
        if not self.alpaca.ok:
            return
        start = (dt.date.fromisoformat(day) - dt.timedelta(days=150)).isoformat()
        end = (dt.date.fromisoformat(day) + dt.timedelta(days=40)).isoformat()
        try:
            self.market.load_calendar(await self.alpaca.calendar(start, end))
            self.db.set_meta("calendar_day", day)
        except AlpacaError as exc:
            self.last_error = f"calendar: {exc}"

    async def market_tick(self, now: float) -> None:
        await self.load_calendar(now)
        if not self.data.ok:
            return
        day = day_of(now)
        if self.every("prune", 3600, now):
            self.data.prune(now)
            self.db.prune(int(now))
        if not self.market.is_trading_day(day):
            await self._weekend(now)
            return
        session = self.market.session(now)
        open_ts = self.market.open_ts(day)
        plan_at = int(self.cfg.get("MORNING_PLAN_HHMM"))
        plan_ts = at(day, f"{plan_at // 100:02d}:{plan_at % 100:02d}") if plan_at else 0
        if session == "pre" and now >= at(day, "07:00") and self.every("premarket", 600, now):
            await self.scanner.premarket(now)
        if session == "pre" and now >= at(day, "09:00") and self.every("prepare", 300, now):
            await self.scanner.prepare(now)                # noise bands + usual opening volume, before 9:35
        if plan_ts and plan_ts <= now < open_ts + 1800 and session in {"pre", "open"} \
                and self.db.once(f"plan:{day}", int(now)):
            if not self.scanner.gappers(day):
                await self.scanner.premarket(now)
            await self.morning_plan(now, send=True)
        if session in {"pre", "post"} and self.monitor.symbols() and self.every("ext_alerts", 60, now):
            await self.data.refresh(self.monitor.symbols(), now)      # your price alerts work outside the session too
            await self.monitor.price_alerts(now)
        if session == "open":
            if now >= open_ts + 60 and self.db.once(f"gappers_open:{day}", int(now)):
                await self.scanner.premarket(now)          # the final gap list now that the open printed
            self.scanner.extra = list(dict.fromkeys(self.monitor.symbols() + self.paper.symbols()))
            await self.scanner.tick(now)
            await self.autopilot.announce(self.paper.update(now))
            await self.monitor.tick(now)
            if open_ts + 900 <= now <= self.market.close_ts(day) - 3600 and self.every("discover", 600, now):
                await self.scanner.discover(now)
        if session in {"post", "closed"} and now >= self.market.close_ts(day):
            if self.every("paper_final", 300, now):
                await self.data.refresh(list(dict.fromkeys(self.scanner.watchlist(day) + self.paper.symbols())), now)
                await self.autopilot.announce(self.paper.update(now))
            if now >= self.market.close_ts(day) + 600 and self.cfg.flag("EOD_REPORT") \
                    and self.db.once(f"dayreport:{day}", int(now)):
                await self.day_report(day, send=True)
            if et(now).weekday() == 4 and now >= self.market.close_ts(day) + 1200 \
                    and self.db.once(f"week:{day}", int(now)):
                week = week_report(self.db, 7, now)
                auto = self.autopilot.week_line(now)
                await self.notify(week + ("\n\n" + auto if auto else ""), kind="REPORT")
        quiet = session in {"closed", "post"} or (session == "pre" and now < at(day, "07:00"))
        if quiet:
            self._first_backtest(now)                   # first run: test the setups while the market is quiet

    def _first_backtest(self, now: float) -> None:
        """No backtest yet, or the setups changed since the last one: start one while the market is quiet (and retry
        every 2 hours if it couldn't finish)."""
        running = self.backtest_task and not self.backtest_task.done()
        stale = self.db.get_meta("backtest_setups") != f"{SETUPS_VERSION}:{','.join(SETUP_NAMES)}"
        if (not self.db.get_meta("backtest_ts") or stale) and not running and self.every("auto_backtest", 7200, now):
            self.start_backtest()

    async def _weekend(self, now: float) -> None:
        if et(now).weekday() == 5 and et(now).hour >= 10 and self.db.once(f"weekly_backtest:{day_of(now)}", int(now)):
            self.start_backtest()
        else:
            self._first_backtest(now)

    async def morning_plan(self, now: float, send: bool = True) -> str:
        await self.data.load_daily(["SPY", "QQQ"], now)
        await self.data.refresh(["SPY", "QQQ"], now)
        positions = self.db.rows("select * from positions")
        emap = {}
        if self.earnings:
            for sym in {g["symbol"] for g in self.scanner.gappers(day_of(now))} | {p["underlying"] for p in positions}:
                e = await self.earnings.next_report(sym, now)
                if e and e["days"] <= 1:
                    emap[sym] = e["text"]
        text = build_plan(self.cfg, self.db, self.data, self.edge, self.scanner, now, self.account_size(), positions, emap)
        if send:
            photo = None
            gs = self.scanner.gappers(day_of(now))
            if gs and self.charts:
                top = gs[0]["symbol"]
                await self.data.refresh([top], now)
                photo = self.charts.render(top, self.data.bars1(top), day_of(now), self.market,
                                           levels=self.data.levels(top, now), title=f"{top} · premarket", start="04:00")
            await self.notify(text, kind="PLAN", photo=photo)
        return text

    async def day_report(self, day: str, send: bool = True) -> str:
        text = coach_day_report(self.db, self.ledger, self.edge, day, self.account_size(), self.cfg)
        auto = self.autopilot.day_line(day)
        if auto:
            text += "\n" + auto
        if self.ai.ok and self.cfg.flag("AI"):
            lk = leaks(self.db, 30)
            notes = await self.ai.coach(f"{text}\n\n30-day groups: {dumps(lk['leaks'][:5] + lk['edges'][:3])}")
            if notes:
                text += "\n\n<b>Coach's notes</b>\n" + esc(notes)
        if send:
            await self.notify(text, kind="REPORT")
        return text

    # ---- backtest ------------------------------------------------------------------------------------------------
    def start_backtest(self) -> bool:
        if self.backtest_task and not self.backtest_task.done():
            return False
        if not self.data.ok:
            return False
        self.backtest_task = asyncio.ensure_future(self.run_backtest())
        return True

    async def run_backtest(self) -> dict | None:
        now = time.time()
        self.backtest_progress = "starting"

        async def progress(text):
            self.backtest_progress = str(text)
        try:
            await self.load_calendar(now)
            res = await run_backtest_job(self.alpaca, self.market, self.cfg, day_of(now), progress=progress)
        except Exception as exc:
            self.backtest_progress = f"failed: {exc}"
            self.last_error = f"backtest: {exc}"
            if self.db.once(f"backtest_failed:{day_of(now)}", int(now)):
                await self.notify(f"🧪 The backtest couldn't finish ({esc(str(exc)[:160])}). It retries every 2 hours.",
                                  kind="BACKTEST", silent=True)
            return None
        save_backtest(self.db, res, int(time.time()))
        self.backtest_progress = "done"
        await self.notify(self.backtest_summary(res), kind="BACKTEST")
        return res

    def backtest_summary(self, res: dict) -> str:
        lines = [f"<b>🧪 Backtest done</b> · {len(res['days'])} trading days · {res['pairs']} stock-days · "
                 f"{res['trades']} trades (costs included)"]
        for key, st in sorted(res["stats"].items(), key=lambda kv: -kv[1].get("avg_r", 0)):
            setup, direction = key.split("_", 1)
            lines.append(f"{STATUS_TEXT.get(st['status'], st['status'])} {esc(label(setup, direction))}: {st['n']} trades, "
                         f"{st['avg_r']:+.2f}R avg, {st['won'] * 100:.0f}% won")
        if not any(st["status"] == "proven" for st in res["stats"].values()):
            lines.append("\nNothing is proven yet — that's an honest result, not a failure. Alerts stay silent and "
                         "labelled until a setup earns it; paper keeps measuring.")
        return "\n".join(lines)

    # ---- the assistant's context -----------------------------------------------------------------------------------
    async def context(self, now: float) -> dict:
        day = day_of(now)
        state = self.ledger.day_state(day)
        account = self.account_size()
        open_trades = []
        for t in self.db.rows(f"select * from trades where status='open' and {NOT_HOLDING}"):
            px = self.data.last_price(t["underlying"])
            open_trades.append({"what": t["symbol"], "underlying": t["underlying"], "kind": t["kind"],
                                "direction": t["direction"], "qty": t["open_qty"], "entry": t["entry_avg"],
                                "stop": t["plan_stop"], "target1": t["plan_t1"], "target2": t["plan_t2"],
                                "underlying_price_now": px, "opened": clock_text(t["opened_ts"])})
        closed = self.db.rows("select * from trades where status='closed' and closed_ts>=? order by closed_ts",
                              (at(day, "00:00"),))
        watch = []
        for s in self.scanner.watchlist(day)[:12]:
            lv = self.data.levels(s, now)
            px = self.data.last_price(s)
            if px:
                watch.append({"symbol": s, "price": px, "change_pct": round((px / lv["pdc"] - 1) * 100, 2) if lv.get("pdc") else None,
                              **{k: round(num(lv[k]), 2) for k in ("vwap", "pmh", "pml", "pdh", "pdl", "hod", "lod", "rvol", "atr")
                                 if lv.get(k)}})
        alerts = self.db.rows("select ts, symbol, setup, direction, entry, stop, t1, t2, proven from alerts where day=? "
                              "order by ts desc limit 6", (day,))
        lk = leaks(self.db, 30, now)
        return {
            "now_new_york": et(now).strftime("%a %Y-%m-%d %H:%M"), "session": self.market.session(now),
            "account_size": account,
            "rules": {k: self.cfg.get(k) for k in ("RISK_PER_TRADE_PCT", "DAILY_MAX_LOSS_PCT", "MAX_TRADES_PER_DAY",
                                                   "LOSS_STREAK_BREAK", "OPTIONS_MAX_PREMIUM_PCT", "ALLOW_0DTE")},
            "today": {"realized_pnl": state["realized"], "closed_trades": state["closed"], "loss_streak": state["streak"],
                      "trades": [{"what": t["symbol"], "direction": t["direction"], "pnl": t["pnl"], "r": t["r_mult"]}
                                 for t in closed[-10:]]},
            "open_positions": open_trades,
            "watchlist": watch,
            "recent_alerts": [{**a, "time": clock_text(a["ts"]), "setup": label(a["setup"], a["direction"])} for a in alerts],
            "setups": [{"setup": r["label"], "status": r["status"],
                        "backtest_avg_r": num((r["backtest"] or {}).get("avg_r")), "backtest_n": int(num((r["backtest"] or {}).get("n"))),
                        "paper_n": r["paper"]["n"], "paper_avg_r": round(r["paper"]["avg_r"], 2)} for r in self.edge.table()],
            "journal_30d": {"total": lk["total"], "biggest_leaks": lk["leaks"][:3], "strengths": lk["edges"][:2]},
        }

    # ---- status -----------------------------------------------------------------------------------------------------
    def status_text(self, now: float) -> str:
        acc = self.db.rows("select * from accounts")
        feed = "full market, real-time (paid)" if self.alpaca.realtime_sip else "IEX real-time + full market 15 min late (free)"
        lines = [f"<b>📈 DayBot {VERSION}</b> · up {int((now - self.started) / 60)} min · market {self.market.session(now)}"]
        if not self.data.ok:
            lines.append("❌ Market data: not set up (ALPACA keys — run daybot_setup.py)")
        elif getattr(self.alpaca, "healthy", True):
            lines.append(f"✅ Market data: Alpaca — {feed}")
        else:
            lines.append("⚠️ Market data: Alpaca isn't answering — check the keys / internet (daybot_doctor.py)")
        if self.broker.ok:
            last = f", last sync {clock_text(self.broker.last_poll)}" if self.broker.last_poll else ""
            good = getattr(self.snap, "healthy", True)
            icon = "✅" if good else "⚠️"
            what = (f"SnapTrade, {len(acc)} account{'s' if len(acc) != 1 else ''}{last}" if good
                    else "SnapTrade isn't answering — check the keys (daybot_doctor.py)")
            if good and not acc:
                what += " — link Robinhood / Chase with /connect"
            lines.append(f"{icon} Your trades: {what}"
                         + (f" · ⚠️ re-link: {', '.join(self.broker.disabled)}" if self.broker.disabled else ""))
        elif self.cfg.snaptrade_client_id and not self.cfg.snaptrade_consumer_key:
            lines.append("⚪ Your trades: the SnapTrade consumer key is missing — double-click daybot_setup.command, "
                         "paste it, press Enter through the rest, then restart DayBot")
        else:
            lines.append("❌ Your trades: not connected (/connect)")
        lines.append(("✅" if self.ai.ok else "⚪") + f" AI (charts, /ask, coach): {esc(self.ai.status())}")
        lines.append(("✅" if self.charts else "⚪") + " Chart pictures" + ("" if self.charts else ": pip install matplotlib"))
        lines.append(("✅" if self.earnings else "⚪") + " Earnings dates" + ("" if self.earnings else " (optional FINNHUB_API_KEY)"))
        if self.tg and self.cfg.telegram_send_only:
            lines.append("⚠️ Telegram: sending through FomoBot's bot, so commands are off — give DayBot its own bot "
                         "(DAY_TELEGRAM_BOT_TOKEN) to use them")
        ts = int(num(self.db.get_meta("backtest_ts"), 0))
        lines.append(f"🧪 Backtest: {et(ts).strftime('%b %d') if ts else 'not run yet'}"
                     + (f" ({self.backtest_progress})" if self.backtest_task and not self.backtest_task.done() else ""))
        errs = [e for e in (self.alpaca.last_error, self.snap.last_error, self.broker.last_error, self.data.last_error,
                            self.scanner.last_error, self.last_error, self.ai.last_error) if e]
        if errs:
            lines.append("Last errors: " + esc(" | ".join(errs)[:400]))
        return "\n".join(lines)

    # ---- loops ------------------------------------------------------------------------------------------------------
    async def market_loop(self) -> None:
        while True:
            try:
                await self.market_tick(time.time())
            except Exception as exc:
                self.last_error = f"market: {type(exc).__name__}: {exc}"
                traceback.print_exc()
            await asyncio.sleep(30)

    async def broker_tick(self, now: float) -> None:
        if not self.broker.ok:
            return
        day = day_of(now)
        active = self.market.is_trading_day(day) and at(day, "04:00") <= now < at(day, "20:00")
        if self.every("accounts", 6 * 3600, now):
            await self.broker.refresh_accounts(now)
            await self.broker.refresh_positions(now)          # first: a holding's average cost for the history
            self._last["positions"] = now
            await self.broker.backfill(now)
            await self.broker.close_expired(now)
        if self.every("positions", 600 if active else 3600, now):
            await self.broker.refresh_positions(now)
        if self.every("fills", 20 if active else 300, now):
            await self.broker.poll(now)

    async def broker_loop(self) -> None:
        while True:
            try:
                await self.broker_tick(time.time())
            except Exception as exc:
                self.last_error = f"broker: {type(exc).__name__}: {exc}"
                traceback.print_exc()
            await asyncio.sleep(5)

    async def telegram_loop(self) -> None:
        tg = self.tg
        if not tg or not tg.ok or self.cfg.telegram_send_only:
            return
        offset = 0
        while True:
            try:
                updates = await tg.updates(offset)
            except Exception:
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = max(offset, int(u.get("update_id", 0)) + 1)
                try:
                    await self.handle_update(u)
                except Exception as exc:
                    self.last_error = f"telegram: {type(exc).__name__}: {exc}"
                    traceback.print_exc()
            if tg.conflict_ts and time.time() - tg.conflict_ts < 60:
                await asyncio.sleep(30)

    async def handle_update(self, u: dict) -> None:
        tg = self.tg
        cb = u.get("callback_query")
        msg = u.get("message") or (cb or {}).get("message") or {}
        chat = str((msg.get("chat") or {}).get("id") or "")
        if not chat:
            return
        if not self.cfg.telegram_chat_id and not cb and (msg.get("chat") or {}).get("type") == "private":
            # no chat set up yet: the first person to message this (private, new) bot is its owner
            self.cfg.telegram_chat_id = chat
            tg.chat_id = chat
            self.db.set_meta("owner_chat", chat)
            await tg.send("✅ Linked — DayBot will message you here.\n\n" + self.status_text(time.time())
                          + "\n\n/help for everything it does.")
            return
        if not self.cfg.telegram_chat_id or chat != str(self.cfg.telegram_chat_id):
            if not cb:
                await tg.send(f"This is a private bot. Your chat id is <code>{esc(chat)}</code> — put it in .env as "
                              "DAY_TELEGRAM_CHAT_ID if this is your bot.", chat_id=chat)
            return
        now = time.time()
        if cb:
            await tg.answer(cb.get("id", ""))
            replies = await self.commands.button(cb.get("data", ""), now)
        elif msg.get("photo"):
            file_id = msg["photo"][-1].get("file_id")
            await tg.send("🔍 Reading the chart…", silent=True)
            image = await tg.download(file_id)
            replies = await self.commands.photo(image, msg.get("caption", ""), now) if image else \
                [{"text": "Couldn't download that image.", "photo": None, "buttons": None}]
        elif msg.get("document") and str(msg["document"].get("mime_type", "")).startswith("image/"):
            image = await tg.download(msg["document"]["file_id"])
            replies = await self.commands.photo(image, msg.get("caption", ""), now) if image else []
        else:
            replies = await self.commands.text(msg.get("text", ""), now)
        for r in replies:
            if r.get("photo"):
                if len(r.get("text") or "") <= 1000:
                    await tg.send_photo(r["photo"], r.get("text") or "", buttons=r.get("buttons"))
                    continue
                await tg.send_photo(r["photo"], "")
            if r.get("text"):
                await tg.send(r["text"], buttons=r.get("buttons"))

    async def startup_message(self, wait: float = 25) -> None:
        await asyncio.sleep(wait)                    # after the first real attempt to reach everything
        if self.db.once(f"hello:{VERSION}:{day_of(time.time())}", int(time.time())):
            text = self.status_text(time.time()) + "\n\n/help for everything it does."
            if self.db.once(f"news:{VERSION}", int(time.time())):
                text += "\n\n" + WHATS_NEW
            await self.notify(text, kind="START", silent=True)

    async def run(self) -> None:
        tasks = [self.market_loop(), self.broker_loop(), self.telegram_loop(), self.startup_message()]
        if self.cfg.dashboard_enabled:
            from .dashboard import serve
            tasks.append(serve(self, self.cfg.dashboard_port))
        await asyncio.gather(*tasks)


WHATS_NEW = ("<b>🆕 New in this version</b>\n"
             "🤖 <b>Autopilot</b>: a pretend $1,000 account now takes every trade the bot alerts you on, by itself, and "
             "tells you each buy and sell. In a couple of weeks you'll know in dollars whether following DayBot pays — "
             "/autopilot\n"
             "📊 A cleaner dashboard with tabs: http://localhost:8788\n"
             "💼 Sizes now fit the cash you actually have (fractional shares when needed)\n"
             "🧹 Crypto and long-term holdings are no longer treated as day trades; leveraged ETFs left out of gappers")


def _ssl_context():
    """Certificates from certifi: python.org's Mac Python ships without any."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


async def _main() -> None:
    cfg = load()
    connector = aiohttp.TCPConnector(ssl=_ssl_context(), limit=40)
    async with aiohttp.ClientSession(connector=connector) as session:
        telegram = Telegram(session, cfg.telegram_token, cfg.telegram_chat_id) if cfg.telegram_token else None
        app = App(cfg, session, telegram=telegram)
        print(f"DayBot {VERSION} · database {cfg.db_path}")
        if cfg.dashboard_enabled:
            print(f"Dashboard: http://localhost:{cfg.dashboard_port}")
        if not cfg.has_data:
            print("⚠️  No Alpaca keys: market data is off. Run: python3 daybot_setup.py")
        await app.run()


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("DayBot stopped.")

