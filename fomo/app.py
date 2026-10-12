"""Starts everything: wallet stream, workers, price tracker, backup poller, Telegram, dashboard."""
from __future__ import annotations

import asyncio
import json
import socket
import ssl
import time
from datetime import datetime

import aiohttp

from . import VERSION, config, profiles
from .card import LiveCard
from .commands import Commands, format_stats
from .db import Database, backfill_alert_outcomes, backup, coin_addresses_in_whales, drop_autopilot_whales, import_legacy_whales
from .discovery import Discovery
from .engine import Engine
from .live import LiveTrader
from .market import Market
from .portfolio import Portfolio
from .rpc import SolanaRPC, WalletStream
from .hype import HypeScanner
from .runners import RunnerScanner
from .scout import WhaleScout
from .telegram import Notifier, Telegram
from .tracker import Tracker
from .tuner import SETTINGS as _TUNED_SETTINGS, Tuner
from .util import esc
from .whales import Whales


HOST = socket.gethostname().split(".")[0] or "this computer"
TUNED = {name for names in _TUNED_SETTINGS.values() for name in names} | {"PAPER_SLIPPAGE_PCT"}   # set by the bot itself too


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class App:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        self.started = time.time()
        self.db = Database(cfg.db_path)
        for name in config.TUNABLES:  # live changes made with /set survive restarts
            saved = self.db.get_meta(f"setting:{name}")
            if saved:
                try:
                    cfg.set(name, saved)
                except ValueError:
                    pass
        if self.db.get_meta("quality_gate_off_v25_2") != "1":   # held-back grade C did as well as sent alerts
            self.db.set_meta("setting:QUALITY_GATE", 0)
            cfg.set("QUALITY_GATE", 0)
            self.db.set_meta("quality_gate_off_v25_2", "1")
        if self.db.get_meta("runner_alerts_off_v22_4") != "1":   # community runners averaged -71% on your data
            self.db.set_meta("setting:RUNNER_ALERTS", 0)
            cfg.set("RUNNER_ALERTS", 0)
            self.db.set_meta("runner_alerts_off_v22_4", "1")
        self.stream: WalletStream | None = None
        self.crashes: dict[str, int] = {}
        self.last_crash = ""
        self.find_task: asyncio.Task | None = None
        self.last_find: dict | None = None
        self._jobs: set = set()

    def set_setting(self, name: str, value, by_tuner: bool = False) -> float:
        parsed = self.cfg.set(name, value)
        key = name.strip().upper()
        self.db.set_meta(f"setting:{key}", parsed)
        if not by_tuner and key in TUNED:
            self.db.set_meta(f"tune:manual:{key}", int(time.time()))   # your choice: the tuner leaves it alone a week
        return parsed

    async def move_coin_whales(self) -> list[str]:
        """Coin addresses followed as whales become tracked coins (they can't trade, so they'd never alert)."""
        moved = []
        for address in coin_addresses_in_whales(self.db):
            ok, _ = await self.engine.coins.add(address, source="from whales")
            if ok:
                self.db.run("update whales set active=0 where address=?", (address,))
                moved.append(self.db.scalar("select symbol from coins where mint=?", (address,)) or address[:6])
        if moved:
            self.refresh_wallets()
        return moved

    def on_followed(self, address: str) -> None:
        """Profile a newly followed whale from its on-chain history right away (style + copy score)."""
        async def job():
            try:
                prof = await self.engine.profiles.profile_history(address)
                if not prof or prof["trips"] <= (self.engine.profiles.get(address) or {}).get("trips", 0):
                    return
                self.engine.profiles.save(address, prof)
                ok, why = profiles.verdict(prof, self.cfg.get("MIN_COPY_SCORE"))
                text = (f"🔍 <b>{esc(self.whales.name(address))}</b> — last {prof['trips']} trades: "
                        f"{esc(profiles.describe(prof))}")
                if not ok:
                    text += f"\n🚫 Entry alerts off for this whale: {esc(why)}."
                await self.notify(text, silent=True, kind="INFO")
            except Exception as exc:
                self.engine.last_error = f"profile {address[:6]}: {type(exc).__name__}: {exc}"
        task = asyncio.create_task(job())
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    def refresh_wallets(self) -> None:
        if self.stream:
            self.stream.set_wallets(self.engine.watched_wallets())

    async def run_find(self, mints: list[str], progress=None) -> dict:
        job = self.db.insert("insert into finds(ts,mints,status) values(?,?,?)",
                             (int(time.time()), ",".join(mints), "running"))

        async def step(text):
            self.db.run("update finds set progress=? where id=?", (text, job))
            if progress:
                await progress(text)

        try:
            result = await self.discovery.find(mints, step)
        except Exception as exc:
            self.db.run("update finds set status='failed',progress=?,finished_ts=? where id=?",
                        (f"{type(exc).__name__}: {exc}", int(time.time()), job))
            raise
        self.db.run("update finds set status='done',result_json=?,finished_ts=? where id=?",
                    (json.dumps(result), int(time.time()), job))
        self.last_find = result
        return result

    # -- loops ---------------------------------------------------------------------------------
    async def telegram_loop(self) -> None:
        if not self.telegram.ok:
            return
        offset = int(self.db.get_meta("tg_offset", "0") or 0)
        while True:
            updates = await self.telegram.updates(offset)
            await self.check_conflict()
            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                asyncio.create_task(self._safe(self.commands.handle_update(update)))
            if updates:
                self.db.set_meta("tg_offset", offset)
            else:
                await asyncio.sleep(1)

    async def check_conflict(self, now: float | None = None) -> bool:
        """Two copies running with the same Telegram bot (the Mac AND the server): double alerts, commands answered by
        whichever copy wins, and — worst — double real trades. Say so (hourly) and hold real-money buys meanwhile."""
        now = now or time.time()
        if now - self.telegram.conflict_ts > 120:
            return False
        live = getattr(self, "live", None)
        if live:
            live.hold_until = now + 600
        if now - float(self.db.get_meta("conflict_warned", "0") or 0) >= 3600:
            self.db.set_meta("conflict_warned", int(now))
            await self.notify(f"⚠️ <b>Two copies of FomoBot are running</b> with this Telegram bot — this one is on "
                              f"<b>{esc(HOST)}</b>. You'll get double alerts, and commands go to either copy.\n"
                              "Stop one: on the Mac press Control+C in its window (and run remove_autostart.command); "
                              "on the server: sudo systemctl stop fomobot.\nReal-money buys are on hold until only one runs.",
                              kind="INFO")
        return True

    async def _safe(self, coro) -> None:
        try:
            await coro
        except Exception as exc:
            print(f"[command] {type(exc).__name__}: {exc}")

    async def daily_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            hour = int(self.cfg.get("DAILY_SUMMARY_HOUR"))
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if int(time.time()) % 600 < 60:   # every 10 minutes: a health snapshot (it goes into /export)
                try:
                    from .health import report
                    self.db.set_meta("health:last", json.dumps(report(self)))
                except Exception as exc:
                    print(f"[health] {type(exc).__name__}: {exc}")
            if self.db.get_meta("backup_day") != today:
                self.db.set_meta("backup_day", today)
                try:   # a daily copy of the database (in its own thread: a big file must not stall alerts)
                    await asyncio.to_thread(backup, self.cfg.db_path, self.cfg.state_dir / "backups")
                except Exception as exc:
                    print(f"[backup] {type(exc).__name__}: {exc}")
            if hour >= 0 and now.hour == hour and self.db.get_meta("daily_sent") != today:
                self.db.set_meta("daily_sent", today)
                await self.notify("🌙 <b>Daily summary</b>\n" + format_stats(self, 1).split("\n", 1)[1]
                                  + "\n/stats for 30 days", silent=True, kind="DAILY")
                self.db.prune()

    async def run(self) -> None:
        cfg = self.cfg
        connector = aiohttp.TCPConnector(ssl=_ssl_context(), limit=60)
        headers = {"User-Agent": f"FomoBotWhaleCopy/{VERSION}"}
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            # free private plans allow ~10-25 requests/second; stay just under 10 so bursts
            # (like rebuilding your positions at startup) don't get rate limited.
            self.rpc = SolanaRPC(session, cfg.rpc_http, concurrency=6 if cfg.fast_rpc else 3,
                                 min_interval=0.11 if cfg.fast_rpc else 0.15)
            self.market = Market(session, self.rpc, self.db)
            self.telegram = Telegram(session, cfg.telegram_token, cfg.telegram_chat_id)
            self.notify = Notifier(self.telegram, self.db)
            self.whales = Whales(self.db, cfg, cfg.my_wallets)
            self.portfolio = Portfolio(self.db, cfg, self.market)
            self.engine = Engine(cfg, self.db, self.rpc, self.market, self.whales, self.portfolio, self.notify)
            self.tracker = Tracker(cfg, self.db, self.rpc, self.market, self.whales, self.portfolio, self.engine,
                                   self.notify)
            self.discovery = Discovery(self.rpc, self.market, self.db, cfg)
            self.runners = RunnerScanner(cfg, self.db, self.market, self.engine, self.whales, self.notify)
            self.hype = HypeScanner(cfg, self.db, self.market, self.engine, self.whales, self.notify)
            self.runners.hype = self.engine.hype = self.hype
            self.hype.tuner = self.tuner = Tuner(cfg, self.db, self.notify,
                                                 lambda name, value: self.set_setting(name, value, by_tuner=True))
            self.engine.refresh_wallets = self.refresh_wallets
            self.engine.profiles.discovery = self.discovery
            self.scout = WhaleScout(cfg, self.db, self.rpc, self.market, self.whales, self.runners, self.notify,
                                    self.refresh_wallets, self.engine.profiles)
            self.commands = Commands(self)
            self.stream = WalletStream(cfg.rpc_wss, self.engine.enqueue, cfg.rpc_wss_fallback)
            self.engine.on_stream_miss = self.stream.resubscribe
            self.tracker.stream = self.stream

            imported = import_legacy_whales(self.db, cfg.legacy_db_path,
                                            [config.ROOT / "tracked_wallets.json",
                                             config.ROOT / "legacy" / "tracked_wallets.json"])
            drop_autopilot_whales(self.db)
            backfill_alert_outcomes(self.db)
            self.engine.profiles.refresh_tracked()
            self.refresh_wallets()
            self.live = LiveTrader(cfg, self.db, self.rpc, session, self.notify)
            self.engine.paper.live = self.live
            self.live.set_setting = lambda name, value: self.set_setting(name, value, by_tuner=True)
            self.live.market, self.live.stream = self.market, self.stream
            self.card = LiveCard(self)
            loops = [("wallet stream", self.stream.run), ("prices", self.tracker.run_prices),
                     ("backup poller", self.tracker.run_poller), ("telegram", self.telegram_loop),
                     ("daily", self.daily_loop), ("hype scanner", self.runners.run), ("whale scanner", self.scout.run),
                     ("live card", self.card.run)]
            loops += [(f"worker {i + 1}", self.engine.worker) for i in range(3)]
            if cfg.dashboard_enabled:
                from .dashboard import start_dashboard
                loops.append(("dashboard", lambda: start_dashboard(self)))
            await self.market.sol_usd()
            self.coins_moved = await self.move_coin_whales()
            await self.notify(self.startup_text(imported), silent=True, kind="INFO")
            await asyncio.gather(*(self.supervise(name, start) for name, start in loops))

    async def supervise(self, name: str, start) -> None:
        """Run one of the bot's loops for good: if it crashes, note it and restart it (2s, 4s … at most a minute
        apart) — one bad response can't take the whole bot down, and the rest keeps running meanwhile."""
        delay = 2.0
        while True:
            began = time.monotonic()
            try:
                await start()
                return   # finished on purpose (e.g. the dashboard couldn't get its port)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.crashes[name] = self.crashes.get(name, 0) + 1
                self.last_crash = f"{time.strftime('%H:%M:%S')} {name}: {type(exc).__name__}: {str(exc)[:120]}"
                print(f"[{name}] crashed ({type(exc).__name__}: {exc}) — restarting in {delay:.0f}s")
                if time.monotonic() - began > 300:
                    delay = 2.0   # it had been running fine for a while: restart quickly
                await asyncio.sleep(delay)
                delay = min(60.0, delay * 2)

    def startup_text(self, imported: int) -> str:
        n = self.whales.count()
        lines = [f"✅ <b>FomoBot Whale Copy {VERSION}</b> is running on {esc(HOST)}",
                 f"Following {n} whale{'s' if n != 1 else ''}" + (f" ({imported} imported from the old bot)"
                                                                   if imported else "")]
        lines.append("Your wallet: synced (exact P/L)" if self.cfg.my_wallets
                     else "Your wallet: not synced — add MY_WALLETS=your_address to .env for exact P/L")
        if not self.cfg.fast_rpc:
            lines.append("⚠️ No private RPC: the public Solana RPC is slow and can miss the first seconds. Add a free one "
                         "(SOLANA_RPC_HTTP in .env — see FREE_RPC.txt).")
        else:
            lines.append(f"RPC: {esc(self.cfg.rpc_name)}" + (f" (+{len(self.cfg.rpc_http) - 2} backup)"
                                                             if len(self.cfg.rpc_http) > 2 else ""))
        if self.cfg.dashboard_enabled:
            lines.append(f"Dashboard: http://localhost:{self.cfg.dashboard_port}")
        for symbol in getattr(self, "coins_moved", []):
            lines.append(f"📍 ${esc(symbol)} was in your whales but it's a coin — it's now a tracked coin with "
                         "take-profit messages (/coins).")
        styles = {}
        for w in self.whales.active():
            prof = self.engine.profiles.get(w["address"])
            ok = profiles.verdict(prof, self.cfg.get("MIN_COPY_SCORE"))[0]
            label = profiles.STYLE_LABEL.get((prof or {}).get("style", "NEW"), "🆕 New")
            pb = ((prof or {}).get("playbook") or {}).get("ok")
            styles.setdefault(label + (" (📘 own playbook)" if pb else "" if ok else " (blocked)"), []).append(esc(w["name"]))
        for label, names in styles.items():
            lines.append(f"{label}: {', '.join(names)}")
        live = getattr(self, "live", None)
        if live and self.cfg.flag("LIVE_TRADING"):
            ok, why = live.ready()
            lines.append(("🧪 Live autopilot: DRY RUN (nothing is sent)" if live.dry_run else "🔴 Live autopilot: ON — trading real SOL")
                         if ok else f"⚠️ Live autopilot is on but can't trade: {esc(why)}")
        lines.append("📌 The pinned live card keeps you up to date — no commands needed.")
        if not n:
            lines.append("\nStart with /add WALLET name, or /find COIN on a coin that already ran.")
        lines.append("/help for commands")
        return "\n".join(lines)


def single_instance(state_dir) -> object | None:
    """Hold a lock so two copies of the bot never run at once (double alerts, double trades)."""
    try:
        import fcntl
    except ImportError:
        return object()
    state_dir.mkdir(parents=True, exist_ok=True)
    handle = open(state_dir / "bot.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> None:
    cfg = config.load()
    lock = single_instance(cfg.state_dir)
    if lock is None:
        print("FomoBot is already running in another window. Close that one first — this one stops.")
        return
    print(f"FomoBot Whale Copy {VERSION} — database {cfg.db_path}")
    if not cfg.telegram_token:
        print("⚠️  TELEGRAM_BOT_TOKEN is missing in .env — alerts will only print here.")
    try:
        asyncio.run(App(cfg).run())
    except KeyboardInterrupt:
        print("\nStopped.")
