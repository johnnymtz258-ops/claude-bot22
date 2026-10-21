"""The dashboard: http://localhost:8788 (only reachable from this computer)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from aiohttp import web

from . import VERSION
from .clock import clock_text, day_of, et
from .coach import leaks
from .accounts import spendable
from .config import SETTINGS
from .ledger import NOT_HOLDING
from .setups import label


def _market_text(app, now: float) -> str:
    day = day_of(now)
    session = app.market.session(now)
    if not app.market.is_trading_day(day):
        return "Market closed today"
    if session == "open":
        left = app.market.close_ts(day) - now
        return f"Market open · closes in {int(left // 3600)}h {int(left % 3600 // 60):02d}m"
    if session == "pre":
        left = app.market.open_ts(day) - now
        return f"Premarket · opens in {int(left // 3600)}h {int(left % 3600 // 60):02d}m"
    if now < app.market.open_ts(day):
        return "Market opens 9:30am ET"
    return "After hours" if session == "post" else "Market closed"


def state(app, now: float | None = None) -> dict:
    now = now or time.time()
    day = day_of(now)
    st = app.ledger.day_state(day)
    account = app.account_size()
    trades = app.db.rows(f"""select * from trades where (day=? or (status='closed' and closed_ts>=?)) and {NOT_HOLDING}
                             order by opened_ts""", (day, int(now) - 86400))
    trades = [{**t, "time": clock_text(t["opened_ts"])} for t in trades
              if t["day"] == day or day_of(t["closed_ts"] or 0) == day]
    open_rows = []
    for t in app.db.rows(f"select * from trades where status='open' and {NOT_HOLDING} order by opened_ts"):
        px = app.data.last_price(t["underlying"])
        s = 1 if t["direction"] == "long" else -1
        live = None
        if px and t["kind"] == "stock":
            live = s * (px - t["entry_avg"]) * t["open_qty"] * (t["multiplier"] or 1)
        open_rows.append({**t, "und_now": px, "unrealized": live, "time": clock_text(t["opened_ts"])})
    holdings = []
    for h in app.db.rows("""select p.*, a.institution, a.number, coalesce(a.checks, 1) as checks from positions p
                            left join accounts a on a.id=p.account_id where p.qty != 0
                            order by abs(p.qty * p.price * p.multiplier) desc limit 40"""):
        mult = h["multiplier"] or 1
        value = h["qty"] * (h["price"] or 0) * mult
        pnl = (h["price"] - h["avg_price"]) * h["qty"] * mult if h["avg_price"] and h["price"] else None
        holdings.append({"symbol": h["symbol"], "underlying": h["underlying"], "kind": h["kind"], "qty": h["qty"],
                         "avg": h["avg_price"], "price": h["price"], "value": value, "pnl": pnl,
                         "account": f"{h['institution'] or ''} {('····' + str(h['number'])[-4:]) if h['number'] else ''}".strip(),
                         "investing": not h["checks"]})
    alerts = []
    for a in app.db.rows("select * from alerts where day=? order by ts desc limit 40", (day,)):
        p = app.db.row("select status, r_mult, auto from paper where alert_id=?", (a["id"],)) or {}
        alerts.append({**a, "label": label(a["setup"], a["direction"]), "time": clock_text(a["ts"]),
                       "paper": p.get("status"), "paper_r": p.get("r_mult"), "auto": p.get("auto")})
    watch = []
    for s in app.scanner.watchlist(day):
        lv = app.data.levels(s, now) if app.data.bars1(s) else {}
        px = app.data.last_price(s)
        watch.append({"symbol": s, "price": px, "chg": (px / lv["pdc"] - 1) * 100 if px and lv.get("pdc") else None,
                      "vwap": lv.get("vwap"), "rvol": lv.get("rvol"), "pmh": lv.get("pmh"), "pdh": lv.get("pdh")})
    lk = leaks(app.db, 30, now)
    table = app.edge.table()
    auto = app.autopilot.summary(now)
    for t in auto["recent"]:
        t["time"] = clock_text(t["opened_ts"])
        t["date"] = et(t["opened_ts"]).strftime("%-m/%-d")
        t["label"] = label(t["setup"], t["direction"])
    for t in auto["open"]:
        t["label"] = label(t["setup"], t["direction"])
    feed = list(app.notify.feed)[:40] or app.db.rows("select ts, kind, symbol, text from events order by id desc limit 40")
    feed = [{**e, "time": clock_text(e["ts"]) if e.get("ts") else ""} for e in feed]
    accounts = app.db.rows("select id, name, institution, number, total, cash, buying_power, margin, "
                           "coalesce(checks, 1) as checks from accounts order by total desc")
    return {
        "version": VERSION, "now": int(now), "session": app.market.session(now), "market": _market_text(app, now),
        "account": account, "cash": spendable(app.db), "holdings": holdings, "accounts": accounts,
        "health": {"data": bool(app.data.ok and getattr(app.alpaca, "healthy", True)),
                   "broker": bool(app.broker.ok and getattr(app.snap, "healthy", True)),
                   "accounts": len(accounts), "claude": bool(app.ai.ok)},
        "rules": {k: app.cfg.get(k) for k in ("RISK_PER_TRADE_PCT", "DAILY_MAX_LOSS_PCT", "MAX_TRADES_PER_DAY")},
        "today": {k: v for k, v in st.items() if k != "last_loss"}, "trade_list": trades,
        "open": open_rows, "alerts": alerts, "gappers": app.scanner.gappers(day)[:12], "watch": watch,
        "setups": [{"label": r["label"], "status": r["status"], "live": r.get("live"), "bt": r["backtest"],
                    "paper": r["paper"]} for r in table],
        "gauge": app.paper.stats(), "journal": lk, "autopilot": auto,
        "feed": feed,
        "status": app.status_text(now), "backtest": app.backtest_progress,
        "settings": [{"name": n, "label": LABELS.get(n, n.replace("_", " ").capitalize()), "value": app.cfg.get(n),
                      "help": s.help, "bool": s.is_bool, "group": _group(n)} for n, s in SETTINGS.items()],
        "watchlist": list(app.cfg.watchlist),
    }


LABELS = {
    "ACCOUNT_SIZE": "Account size ($)", "RISK_PER_TRADE_PCT": "Risk per trade (%)", "DAILY_MAX_LOSS_PCT": "Daily max loss (%)",
    "MAX_TRADES_PER_DAY": "Max trades a day", "LOSS_STREAK_BREAK": "Losses in a row before a break",
    "BREAK_MINUTES": "Break length (minutes)", "REVENGE_MINUTES": "Revenge-trade window (minutes)",
    "ALERTS_ENABLED": "Setup alerts", "ALERT_UNPROVEN": "Send unproven setups too", "SHORT_ALERTS": "Short setups",
    "MAX_ALERTS_PER_HOUR": "Max alerts an hour", "ORB_MINUTES": "Opening range (minutes)",
    "MORNING_PLAN_HHMM": "Morning plan time (HHMM, ET)", "EOD_REPORT": "End-of-day report",
    "CHECK_MY_TRADES": "Check my trades", "MONITOR_POSITIONS": "Watch my open trades", "AI": "Claude (AI)",
    "AUTOPILOT": "Autopilot", "AUTOPILOT_START": "Starting balance ($)", "AUTOPILOT_MAX_OPEN": "Max open trades",
    "AUTOPILOT_NOTIFY": "Message me its trades", "SLIPPAGE_PCT": "Costs per buy or sell (%)",
    "PAPER_ACCOUNT": "Setup gauge size ($)", "BACKTEST_DAYS": "Backtest length (trading days)",
    "MIN_PRICE": "Min stock price ($)", "GAP_MIN_PCT": "Min gap (%)", "MIN_AVG_VOLUME": "Min average volume (shares)",
    "WATCH_MAX": "Max stocks watched", "OPTION_IDEAS": "Option ideas in alerts",
    "OPTIONS_MAX_PREMIUM_PCT": "Max option premium (% of account)", "ALLOW_0DTE": "Allow same-day options",
    "OPTION_MIN_DTE": "Min days to expiry", "OPTION_MAX_SPREAD_PCT": "Max option spread (%)",
    "OPTION_MIN_OI": "Min open interest",
}

GROUPS = {
    "Your rules": ("ACCOUNT_SIZE", "RISK_PER_TRADE_PCT", "DAILY_MAX_LOSS_PCT", "MAX_TRADES_PER_DAY", "LOSS_STREAK_BREAK",
                   "BREAK_MINUTES", "REVENGE_MINUTES"),
    "Alerts": ("ALERTS_ENABLED", "ALERT_UNPROVEN", "SHORT_ALERTS", "MAX_ALERTS_PER_HOUR", "ORB_MINUTES",
               "MORNING_PLAN_HHMM", "EOD_REPORT", "CHECK_MY_TRADES", "MONITOR_POSITIONS", "AI"),
    "Autopilot": ("AUTOPILOT", "AUTOPILOT_START", "AUTOPILOT_MAX_OPEN", "AUTOPILOT_NOTIFY", "SLIPPAGE_PCT",
                  "PAPER_ACCOUNT", "BACKTEST_DAYS"),
    "Scanner": ("MIN_PRICE", "GAP_MIN_PCT", "MIN_AVG_VOLUME", "WATCH_MAX"),
    "Options": ("OPTION_IDEAS", "OPTIONS_MAX_PREMIUM_PCT", "ALLOW_0DTE", "OPTION_MIN_DTE", "OPTION_MAX_SPREAD_PCT",
                "OPTION_MIN_OI"),
}


def _group(name: str) -> str:
    return next((g for g, names in GROUPS.items() if name in names), "Other")


PAGE_FILE = Path(__file__).resolve().parent / "web" / "index.html"


def page() -> str:
    return PAGE_FILE.read_text(encoding="utf-8")


def _safe_html(text: str) -> str:
    """Telegram-style HTML from the bot's own messages (already escaped) -> newline-preserving page HTML."""
    return text.replace("\n", "<br>")


def make_app(app) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_req):
        return web.Response(text=page(), content_type="text/html", headers={"Cache-Control": "no-store"})

    @routes.get("/api/state")
    async def api_state(_req):
        return web.json_response(state(app), dumps=lambda o: json.dumps(o, default=str))

    @routes.get("/chart/{sym}.png")
    async def chart(req):
        sym = req.match_info["sym"].upper()[:6]
        if not sym.isalpha() or not app.charts:
            raise web.HTTPNotFound()
        now = time.time()
        if app.data.ok and not app.data.bars1(sym):
            await app.data.load_daily([sym], now)
            await app.data.refresh([sym], now)
        png = app.commands._render(sym, now)
        if not png:
            raise web.HTTPNotFound()
        return web.Response(body=png, content_type="image/png")

    @routes.post("/api/check")
    async def api_check(req):
        body = await req.json()
        replies = await app.commands.text(str(body.get("text", ""))[:80])
        html_text = "<br><br>".join(_safe_html(r["text"]) for r in replies if r.get("text"))
        return web.json_response({"html": html_text})

    @routes.post("/api/set")
    async def api_set(req):
        body = await req.json()
        try:
            v = app.cfg.set(str(body.get("name")), body.get("value"))
            app.db.set_meta(f"set:{str(body.get('name')).upper()}", v)
            return web.json_response({"ok": True, "value": v})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)})

    @routes.post("/api/autopilot")
    async def api_autopilot(req):
        body = await req.json()
        action = str(body.get("action", ""))
        if action == "reset":
            try:
                amount = float(str(body.get("amount") or 0).replace("$", "").replace(",", ""))
            except ValueError:
                amount = 0.0
            app.autopilot.reset(time.time(), amount)
        if action in {"on", "off", "reset"}:
            v = "0" if action == "off" else "1"
            app.cfg.set("AUTOPILOT", v)
            app.db.set_meta("set:AUTOPILOT", v)
        return web.json_response({"ok": True, "on": app.autopilot.on, "start": app.autopilot.start()})

    @routes.post("/api/backtest")
    async def api_backtest(_req):
        return web.json_response({"started": app.start_backtest()})

    wa = web.Application()
    wa.add_routes(routes)
    return wa


async def serve(app, port: int) -> None:
    runner = web.AppRunner(make_app(app))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    try:
        await site.start()
    except OSError as exc:
        print(f"Dashboard couldn't start on port {port}: {exc}")
        return
    import asyncio
    while True:
        await asyncio.sleep(3600)

