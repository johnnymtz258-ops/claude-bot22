"""Local web dashboard at http://localhost:8787 (and the API the browser extension uses).

Only reachable from this computer. Actions (follow, mute, settings, journal) need the
session token the page is served with, so other websites can't trigger them.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path

from aiohttp import web

from . import VERSION, exits, profiles, reports
from .commands import format_analysis
from .copies import return_pct
from .config import TUNABLES
from .util import find_address, is_address, num, parse_amount

WEB = Path(__file__).resolve().parent / "web"


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, default=str))


class Dashboard:
    def __init__(self, app):
        self.app = app
        self.token = secrets.token_urlsafe(24)
        self.port = app.cfg.dashboard_port
        self.allowed_hosts = {f"localhost:{self.port}", f"127.0.0.1:{self.port}"}

    # -- guards -------------------------------------------------------------------------------
    @web.middleware
    async def guard(self, request: web.Request, handler):
        if request.host not in self.allowed_hosts:  # blocks DNS-rebinding tricks
            return web.Response(status=403, text="forbidden host")
        if request.method == "POST" and request.headers.get("X-Fomo-Token") != self.token:
            return _json({"error": "missing or wrong session token — reload the page"}, 403)
        try:
            return await handler(request)
        except ValueError as exc:
            return _json({"error": str(exc)}, 400)

    # -- pages ----------------------------------------------------------------------------------
    async def index(self, request):
        html = (WEB / "index.html").read_text().replace("__TOKEN__", self.token).replace("__VERSION__", VERSION)
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def session(self, request):
        # No CORS headers: web pages can't read this; the extension (host permission) can.
        return _json({"token": self.token, "version": VERSION})

    # -- read APIs ----------------------------------------------------------------------------
    async def overview(self, request):
        a = self.app
        s = a.portfolio.summary()
        rep = reports.copy_report(a.db, a.cfg, 30)
        stream = a.stream
        return _json({
            "version": VERSION, "uptime": int(time.time() - a.started),
            "stream": {"connected": bool(stream and stream.connected), "subs": stream.subscribed if stream else 0,
                       "error": stream.last_error if stream else ""},
            "rpc": a.rpc.health(), "helius": a.cfg.uses_helius, "sol_usd": num(a.db.get_meta("sol_usd")),
            "whales": a.whales.count(), "wallet_synced": bool(a.cfg.my_wallets),
            "alerts_on": a.cfg.flag("ALERTS_ENABLED"), "last_trade_ts": a.engine.last_event_ts,
            "alerts_24h": a.db.scalar("select count(*) from alerts where kind='BUY' and grade<>'SKIP' and ts>=?",
                                      (int(time.time()) - 86400,), default=0),
            "me": s, "copies": rep["all"], "funnel": reports.alert_funnel(a.db),
            "paper": {k: v for k, v in a.engine.paper.summary().items() if k != "open"},
            "live": ({"enabled": a.live.cfg.flag("LIVE_TRADING"), "dry_run": a.live.dry_run,
                      "problem": a.live.ready()[1], "realized_sol": a.live.summary()["realized_sol"]}
                     if getattr(a, "live", None) else None),
            "tracked_coins": len(a.engine.coins.active()),
            "funnel_labels": reports.FUNNEL_LABELS, "errors": [e for e in (a.engine.last_error, a.tracker.last_error,
                                                                  a.rpc.health()["last_error"]) if e][:3],
        })

    async def feed(self, request):
        a = self.app
        return _json({"buys": reports.recent_buys(a.db, a.market, a.whales, limit=60, since_hours=72),
                      "messages": list(a.notify.feed)[:40]})

    async def hot(self, request):
        hours = num(request.query.get("hours"), 24) or 24
        return _json(reports.hot_coins(self.app.db, self.app.market, self.app.whales, hours=hours))

    async def whales(self, request):
        board = self.app.whales.leaderboard(int(num(request.query.get("days"), 30) or 30))
        prof = self.app.engine.profiles
        for w in board:
            w.pop("stats", None)
            w["profile"] = prof.get(w["address"])
            ok, why = profiles.verdict(w["profile"], self.app.cfg.get("MIN_COPY_SCORE"))
            w["blocked"], w["blocked_why"] = not ok, why
        return _json(board)

    async def paper(self, request):
        a = self.app
        s = a.engine.paper.summary()
        curve = [[r["ts"], r["equity"]] for r in a.db.rows("select ts, equity from paper_equity order by ts")]
        closed = a.db.rows("select * from paper_trades where status='closed' order by close_ts desc limit 60")
        return _json({"summary": s, "curve": curve, "closed": closed, "enabled": a.cfg.flag("PAPER_TRADING"),
                      "rules": {"size": a.cfg.get("PAPER_TRADE_USD"), "slippage": a.cfg.get("PAPER_SLIPPAGE_PCT"),
                                "trail": a.cfg.get("PROTECT_TRAIL_PCT"), "stop": a.cfg.get("STOP_LOSS_PCT")}})

    async def whale(self, request):
        a = self.app
        w = a.whales.find(request.match_info["addr"])
        if not w:
            return _json({"error": "not a followed whale"}, 404)
        trades = a.db.rows("select * from swaps where wallet=? order by ts desc limit 50", (w["address"],))
        for t in trades:
            t["symbol"] = a.market.symbol(t["mint"])
        copies = a.db.rows("select * from copies where whale=? order by open_ts desc limit 50", (w["address"],))
        fee = a.cfg.get("COPY_FEE_PCT")
        for c in copies:
            c["symbol"] = a.market.symbol(c["mint"])
            c["ret"] = num(c["return_pct"]) if c["status"] == "closed" else return_pct(c, num(c["last_price"]), fee)
        return _json({"whale": w, "stats": a.whales.stats(w["address"], fresh=True), "trades": trades,
                      "copies": copies})

    async def coin(self, request):
        a = self.app
        mint = await a.market.resolve_mint(request.match_info["addr"])
        if not is_address(mint):
            return _json({"error": "not a Solana address"}, 400)
        await a.market.token(mint, max_age=20)
        report = reports.coin_report(a.db, a.market, a.whales, a.portfolio, mint)
        now = int(time.time())
        start = min([int(t["ts"]) for t in report["trades"]] + [now - 86400])
        mine = a.db.rows("select ts, side, usd, price_usd from my_trades where mint=? and ts>=? order by ts", (mint, start))
        report["path"] = exits.marks(a.db, mint, start - 600, now)
        report["my_trades"] = mine
        return _json(report)

    async def lookup(self, request):
        """For the browser extension: is this address a coin your whales traded, or a wallet?"""
        a = self.app
        address = find_address(request.match_info["addr"])
        if not address:
            return _json({"type": "unknown"})
        whale = a.whales.get(address)
        if whale and whale["active"]:
            return _json({"type": "whale", "whale": whale, "stats": a.whales.stats(address)})
        mint = await a.market.resolve_mint(address)
        info = await a.market.token(mint, max_age=30)
        holders = a.whales.holders_of(mint)
        if info or holders:
            return _json({"type": "coin", "mint": mint, "info": info, "holders": holders,
                          "position": a.portfolio.position(mint)})
        return _json({"type": "wallet", "address": address})

    async def positions(self, request):
        a = self.app
        await a.market.tokens(a.portfolio.mints()[:60], max_age=20)
        open_ = a.portfolio.open_positions()
        for p in open_:
            p["holders"] = a.whales.holders_of(p["mint"])
            p["coach"] = exits.coach(a.db, a.cfg, p, p["holders"])
        closed = []
        for mint in a.portfolio.mints():
            p = a.portfolio.position(mint)
            if p and not p["open"] and p["bought"] > 0:
                closed.append(p)
        return _json({"open": open_, "closed": closed[:100], "summary": a.portfolio.summary(),
                      "curve": a.portfolio.pnl_curve(), "wallet_synced": bool(a.cfg.my_wallets)})

    async def exits_view(self, request):
        days = int(num(request.query.get("days"), 30) or 30)
        habits = exits.my_exit_habits(self.app.db, days)
        for sell in habits["sells"]:
            sell["symbol"] = self.app.market.symbol(sell["mint"])
        return _json({"lab": exits.exit_lab(self.app.db, self.app.cfg, days), "habits": habits,
                      "ladder": self.app.cfg.flag("PROFIT_LADDER"), "protect_after": self.app.cfg.get("PROTECT_AFTER_X"),
                      "protect_trail": self.app.cfg.get("PROTECT_TRAIL_PCT")})

    async def scout_view(self, request):
        st = self.app.scout.status()
        return _json(st)

    async def scout_run(self, request):
        scout = self.app.scout
        if scout.running:
            raise ValueError("the autopilot is already running")
        asyncio.create_task(scout.scout())
        return _json({"ok": True})

    async def stats(self, request):
        days = int(num(request.query.get("days"), 30) or 30)
        return _json({**reports.copy_report(self.app.db, self.app.cfg, days),
                      "fee_pct": self.app.cfg.get("COPY_FEE_PCT")})

    async def settings(self, request):
        cfg = self.app.cfg
        rows = [{"name": n, "value": cfg.get(n), "help": t.help, "min": t.lo, "max": t.hi, "bool": t.is_bool,
                 "group": setting_group(n)} for n, t in TUNABLES.items()]
        rows.sort(key=lambda r: GROUP_ORDER.index(r["group"]))
        return _json(rows)

    # -- tracked coins & live autopilot ------------------------------------------------------------
    async def coins(self, request):
        tracker = self.app.engine.coins
        out = []
        for coin in tracker.active():
            st = tracker.status(coin)
            st.pop("coach", None)
            out.append(st)
        return _json(out)

    async def add_coin(self, request):
        body = await self._body(request)
        mint = await self.app.market.resolve_mint(find_address(body.get("mint")) or str(body.get("mint") or ""))
        if not is_address(mint):
            raise ValueError("coin address needed")
        ok, text = await self.app.engine.coins.add(mint, parse_amount(body.get("entry_mc")))
        return _json({"ok": ok, "message": text}, 200 if ok else 400)

    async def remove_coin(self, request):
        self.app.engine.coins.remove(request.match_info["mint"])
        return _json({"ok": True})

    async def live_view(self, request):
        live = getattr(self.app, "live", None)
        if not live:
            return _json({"available": False})
        return _json({"available": True, **live.summary(), "size": self.app.cfg.get("LIVE_TRADE_SOL"),
                      "max_open": self.app.cfg.get("LIVE_MAX_OPEN"), "daily_loss": self.app.cfg.get("LIVE_DAILY_LOSS_SOL")})

    async def live_action(self, request):
        live = getattr(self.app, "live", None)
        action = request.match_info["action"]
        if not live:
            return _json({"error": "live trading unavailable"}, 400)
        if action in ("on", "off"):
            if action == "on" and not live.keypair:
                return _json({"error": f"can't turn on: {live.key_problem}"}, 400)
            self.app.set_setting("LIVE_TRADING", action)
        elif action in ("dry-on", "dry-off"):
            self.app.set_setting("LIVE_DRY_RUN", action[4:])
        elif action == "sellall":
            await live.sell_all()
        else:
            return _json({"error": "unknown action"}, 400)
        return _json({"ok": True})

    async def find_status(self, request):
        job = request.match_info.get("id")
        row = (self.app.db.row("select * from finds where id=?", (int(num(job)),)) if job
               else self.app.db.row("select * from finds order by id desc limit 1"))
        if not row:
            return _json({})
        row["result"] = json.loads(row.pop("result_json") or "null")
        return _json(row)

    # -- actions ------------------------------------------------------------------------------
    async def _body(self, request) -> dict:
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise ValueError("bad request body") from None
        return data if isinstance(data, dict) else {}

    async def add_whale(self, request):
        body = await self._body(request)
        address = find_address(body.get("address")) or str(body.get("address") or "")
        if is_address(address) and await self.app.engine.coins.is_coin(address):
            ok, text = await self.app.engine.coins.add(address)   # a coin, not a wallet: track it instead
            return _json({"ok": ok, "message": text}, 200 if ok else 400)
        ok, text = self.app.whales.add(address, str(body.get("name") or ""), source=str(body.get("source") or "dashboard"))
        if ok:
            self.app.refresh_wallets()
            getattr(self.app, "on_followed", lambda a: None)(address)
        return _json({"ok": ok, "message": text}, 200 if ok else 400)

    async def whale_action(self, request):
        a = self.app
        w = a.whales.get(request.match_info["addr"])
        action = request.match_info["action"]
        if not w:
            return _json({"error": "unknown whale"}, 404)
        if action in {"mute", "unmute"}:
            a.whales.set_muted(w["address"], action == "mute")
        elif action == "remove":
            a.whales.remove(w["address"])
            a.refresh_wallets()
        elif action == "rename":
            body = await self._body(request)
            a.whales.add(w["address"], str(body.get("name") or ""))
        else:
            return _json({"error": "unknown action"}, 400)
        return _json({"ok": True})

    async def start_find(self, request):
        body = await self._body(request)
        mints = [find_address(m) for m in body.get("mints") or []]
        mints = [await self.app.market.resolve_mint(m) for m in mints if m][:4]
        if not mints:
            raise ValueError("paste 1–4 coin addresses")
        if self.app.find_task and not self.app.find_task.done():
            raise ValueError("a search is already running")
        self.app.find_task = asyncio.create_task(self.app.run_find(mints))
        await asyncio.sleep(0.05)
        row = self.app.db.row("select id from finds order by id desc limit 1")
        return _json({"id": row["id"] if row else 0})

    async def analyze(self, request):
        body = await self._body(request)
        address = find_address(body.get("wallet"))
        if not address:
            raise ValueError("paste a wallet address")
        result = await self.app.discovery.analyze_wallet(address)
        result["text"] = format_analysis(result)
        return _json(result)

    async def set_setting(self, request):
        body = await self._body(request)
        value = self.app.set_setting(str(body.get("name") or ""), body.get("value"))
        return _json({"ok": True, "value": value})

    async def trade(self, request):
        body = await self._body(request)
        mint = await self.app.market.resolve_mint(find_address(body.get("mint")))
        if not is_address(mint):
            raise ValueError("coin address needed")
        at_mc = parse_amount(body.get("mc"))
        if body.get("side") == "BUY":
            r = await self.app.portfolio.manual_buy(mint, num(body.get("usd")), at_mc)
        else:
            r = await self.app.portfolio.manual_sell(mint, usd=num(body.get("usd")),
                                                     fraction=num(body.get("fraction")), mc_usd=at_mc)
        return _json({"ok": True, **r})

    async def undo(self, request):
        row = self.app.portfolio.undo_last_manual()
        return _json({"ok": bool(row), "removed": row})

    def routes(self, application: web.Application) -> None:
        r = application.router
        r.add_get("/", self.index)
        r.add_static("/static/", WEB, show_index=False)
        r.add_get("/api/session", self.session)
        r.add_get("/api/overview", self.overview)
        r.add_get("/api/feed", self.feed)
        r.add_get("/api/hot", self.hot)
        r.add_get("/api/whales", self.whales)
        r.add_get("/api/paper", self.paper)
        r.add_get("/api/coins", self.coins)
        r.add_post("/api/coins", self.add_coin)
        r.add_post("/api/coins/{mint}/remove", self.remove_coin)
        r.add_get("/api/live", self.live_view)
        r.add_post("/api/live/{action}", self.live_action)
        r.add_get("/api/whale/{addr}", self.whale)
        r.add_get("/api/coin/{addr}", self.coin)
        r.add_get("/api/lookup/{addr}", self.lookup)
        r.add_get("/api/positions", self.positions)
        r.add_get("/api/stats", self.stats)
        r.add_get("/api/exits", self.exits_view)
        r.add_get("/api/scout", self.scout_view)
        r.add_post("/api/scout/run", self.scout_run)
        r.add_get("/api/settings", self.settings)
        r.add_get("/api/find", self.find_status)
        r.add_get("/api/find/{id}", self.find_status)
        r.add_post("/api/whales", self.add_whale)
        r.add_post("/api/whales/{addr}/{action}", self.whale_action)
        r.add_post("/api/find", self.start_find)
        r.add_post("/api/analyze", self.analyze)
        r.add_post("/api/settings", self.set_setting)
        r.add_post("/api/trades", self.trade)
        r.add_post("/api/trades/undo", self.undo)


def build_app(app) -> tuple[web.Application, Dashboard]:
    dash = Dashboard(app)
    application = web.Application(middlewares=[dash.guard])
    dash.routes(application)
    return application, dash


async def start_dashboard(app) -> None:
    application, dash = build_app(app)
    runner = web.AppRunner(application, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", dash.port).start()
        print(f"Dashboard: http://localhost:{dash.port}")
    except OSError as exc:
        print(f"Dashboard could not start on port {dash.port}: {exc} (set DASHBOARD_PORT in .env)")
        return
    while True:
        await asyncio.sleep(3600)


GROUP_ORDER = ["Automation", "Live trading (real money)", "Which buys get sent", "Selling", "Scoring", "Other"]
_GROUPS = {
    "Automation": ("AUTO_", "WHALE_PICKS", "PAPER_", "LIVE_CARD", "RUNNER_ALERTS"),
    "Live trading (real money)": ("LIVE_TRADING", "LIVE_DRY_RUN", "LIVE_TRADE", "LIVE_MAX", "LIVE_DAILY", "LIVE_SLIP",
                                  "LIVE_PRIO"),
    "Which buys get sent": ("MIN_WHALE", "MAX_ENTRY", "CONFIRM_", "LATE_CHASE", "DUMP_GATE", "BLOCK_FLIPPERS",
                            "MIN_COPY", "MICRO_", "HIDE_", "QUIET_", "ALERTS_ENABLED", "REPEAT_", "CONFLUENCE",
                            "MIN_SELL", "REBUY", "RUNNER_"),
    "Selling": ("PROTECT_", "STOP_LOSS", "PROFIT_LADDER", "TAKE_INITIAL", "SCALP_", "RUG_"),
    "Scoring": ("COPY_", "AUTO_MUTE", "MANUAL_FEE"),
}


def setting_group(name: str) -> str:
    for group in GROUP_ORDER[:-1]:
        if name.startswith(_GROUPS[group]):
            return group
    return "Other"
