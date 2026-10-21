"""DayBot end to end on synthetic market days: the scanner, the game plan, paper, reports, commands, dashboard."""
import asyncio
import json

import pytest

from daybot.clock import at
from daybot.commands import HELP
from tests.day_helpers import (DAY, PREV, chop_day, daily_history, flag_day, gap_day, make_app, option_snapshot,
                               stock_fill)
from daybot.options import occ


def _day_app(**kw):
    daily = {s: daily_history(PREV, 100.0, 2.0, seed=i) for i, s in enumerate(["TEST", "SPY", "QQQ", "GAPR", "NEWS"])}
    minute = {"TEST": flag_day(), "SPY": chop_day(seed=5), "QQQ": chop_day(seed=6), "GAPR": gap_day(),
              "NEWS": gap_day()}
    news = {"GAPR": [{"headline": "GAPR beats Q3 earnings, raises guidance", "created_at": "2026-10-21T11:00:00Z",
                      "symbols": ["GAPR"], "source": "wire"}]}
    app = make_app(minute, daily, now=at(DAY, "06:00"), news=news,
                   movers={"gainers": [{"symbol": "GAPR"}], "losers": []}, **kw)
    app.db.set_meta("backtest_ts", 1)
    app.cfg.watchlist = ["TEST"]
    return app


def _walk(app, start, end, step=60, broker=False):
    async def go():
        t = at(DAY, start)
        while t <= at(DAY, end):
            app.fake.now = t
            await app.market_tick(t)
            if broker:
                await app.broker_tick(t)
            t += step
    asyncio.run(go())


def test_a_full_day_plan_alerts_paper_and_report():
    app = _day_app()
    _walk(app, "06:30", "16:20")
    kinds = app.notes.kinds()
    assert kinds.count("PLAN") == 1 and kinds.count("REPORT") == 1
    plan = app.notes.last("PLAN")["text"]
    assert "GAPR" in plan and "earnings" in plan and "Plan: long over the premarket high" in plan
    setups = [m for m in app.notes.sent if m["kind"] == "SETUP"]
    assert {m["symbol"] for m in setups} == {"GAPR", "TEST"}
    assert all(m["silent"] and "unproven" in m["text"] for m in setups)   # nothing is proven yet
    gapr = next(m for m in setups if m["symbol"] == "GAPR")
    assert "Also fits: Gap-and-go" in gapr["text"]
    paper = app.db.rows("select * from paper where status='closed'")
    assert len(paper) == 3 and all(p["r_mult"] > 1 for p in paper)
    assert "Market gauge" in app.notes.last("REPORT")["text"]


def test_a_proven_setup_pings_with_size_and_an_option_idea():
    sym = occ("TEST", "2026-10-23", "C", 103)
    chain = {"TEST": {sym: option_snapshot(1.20, 1.25, 0.52, gamma=0.08)}}
    contracts = {"TEST": [{"symbol": sym, "open_interest": 4200}]}
    app = _day_app(chain=chain, contracts=contracts)
    app.db.insert("""insert into backtests(ts, setup, direction, n, won, avg_r, total_r, pf, half1, half2, no_best,
                     max_dd, status, days) values(1, 'leg2', 'long', 90, .52, .21, 19, 1.5, .2, .22, .15, -5, 'proven', 60)""")
    asyncio.run(app.broker.refresh_accounts(at(DAY, "09:00")))
    _walk(app, "10:40", "11:00")
    msg = next(m for m in app.notes.sent if m["kind"] == "SETUP" and m["symbol"] == "TEST")
    assert not msg["silent"] and "✅ proven setup" in msg["text"]
    assert "Option idea: TEST 10/23 103C" in msg["text"] and "backtest 90 trades" in msg["text"]
    assert "Your $10,000 cash buys 9" in msg["text"]        # 1% risk would be ~400 shares: more than the cash buys


def test_known_losers_are_paper_only():
    app = _day_app()
    app.db.insert("""insert into backtests(ts, setup, direction, n, won, avg_r, total_r, pf, half1, half2, no_best,
                     max_dd, status, days) values(1, 'leg2', 'long', 90, .3, -.2, -18, .7, -.2, -.2, -.3, -20, 'negative', 60)""")
    _walk(app, "10:40", "11:00")
    assert not [m for m in app.notes.sent if m["kind"] == "SETUP" and m["symbol"] == "TEST"]
    assert app.db.scalar("select count(*) from paper where symbol='TEST'") == 1


def test_short_setups_stay_on_paper_unless_you_turn_them_on():
    from daybot.setups import Signal
    app = _day_app()
    app.db.insert("""insert into backtests(ts, setup, direction, n, won, avg_r, total_r, pf, half1, half2, no_best,
                     max_dd, status, days) values(1, 'vwap', 'short', 90, .52, .21, 19, 1.5, .2, .22, .15, -5, 'proven', 60)""")
    now = at(DAY, "10:30")
    sig = Signal("vwap", "short", "TEST", int(now), 100.0, 101.0, 99.0, 98.0, "lost VWAP")
    asyncio.run(app.scanner.alert(sig, now))
    assert not [m for m in app.notes.sent if m["kind"] == "SETUP"]
    assert app.db.scalar("select count(*) from paper where symbol='TEST'") == 1        # still measured
    app.cfg.set("SHORT_ALERTS", "1")
    asyncio.run(app.scanner.alert(Signal("vwap", "short", "TEST", int(now) + 600, 100.0, 101.0, 99.0, 98.0, "x"),
                                  now + 600))
    assert [m for m in app.notes.sent if m["kind"] == "SETUP"]


def test_leveraged_and_inverse_funds_are_not_gappers():
    app = _day_app()
    names = {"XYZU": "Defiance Daily Target 2X Long XYZ ETF", "UCTT": "Ultra Clean Holdings, Inc. Common Stock"}
    asked = []

    async def asset(sym):
        asked.append(sym)
        return {"name": names.get(sym, "")}
    app.data.alpaca.asset = asset
    assert asyncio.run(app.scanner.levered("TQQQ")) and asyncio.run(app.scanner.levered("XYZU"))
    assert not asyncio.run(app.scanner.levered("UCTT"))
    asyncio.run(app.scanner.levered("UCTT"))
    assert asked == ["XYZU", "UCTT"]                                     # asked once, then remembered


def test_market_wraps_are_not_the_catalyst():
    from daybot.news import catalysts, main_catalyst
    items = [{"headline": "Stocks making the biggest moves premarket: NVDA, TSLA, AMD, SOFI and more",
              "symbols": ["NVDA", "TSLA", "AMD", "SOFI", "PLTR"], "created_at": "2026-10-21T12:00:00Z"},
             {"headline": "Morning wrap: tech leads as yields fall", "symbols": ["AAPL", "MSFT", "SOFI", "NVDA", "AMD"],
              "created_at": "2026-10-21T13:00:00Z"},
             {"headline": "SoFi Technologies raises full-year guidance", "symbols": ["SOFI"],
              "created_at": "2026-10-21T11:00:00Z"}]
    assert main_catalyst(catalysts(items, "SOFI"))["headline"] == "SoFi Technologies raises full-year guidance"
    assert catalysts(items, "TSLA") == []


def test_an_old_journal_is_rebuilt_once_with_the_new_rules():
    from tests.day_helpers import stock_fill
    app = _day_app()
    acc = "acc1"
    app.ledger.apply(app.ledger.add_fill(stock_fill("x", "BUY", 1, 60000.0, at(DAY, "10:00"), "BTC", acc)))
    app.db.set_meta("ledger_version", "1")
    app.db.set_meta("history_done", "1")
    app.db.insert("insert into accounts(id, name, checks) values('acc1', 'Brokerage', 0)")
    app.broker._rebuild_if_old()
    assert app.db.scalar("select count(*) from trades") == 0 and app.db.scalar("select count(*) from fills") == 0
    assert not app.db.get_meta("history_done") and app.db.get_meta("ledger_version") == app.broker.LEDGER_VERSION
    assert app.db.scalar("select checks from accounts where id='acc1'") == 0          # your choices are kept
    app.ledger.apply(app.ledger.add_fill(stock_fill("y", "BUY", 1, 10.0, at(DAY, "10:00"), "F", acc)))
    app.broker._rebuild_if_old()                                                     # only once
    assert app.db.scalar("select count(*) from trades") == 1


def test_what_you_bought_before_is_a_holding_not_an_open_day_trade():
    from tests.day_helpers import stock_fill
    app = _day_app()
    asyncio.run(app.broker.refresh_accounts(at(DAY, "09:00")))
    acc = app.db.scalar("select id from accounts limit 1")
    app.snap.history_list = [stock_fill("h1", "BUY", 3, 400.0, at(DAY, "10:00") - 30 * 86400, "VOO", acc)]
    assert asyncio.run(app.broker.backfill(at(DAY, "09:30"))) == 1
    t = app.db.row("select * from trades where symbol='VOO'")
    from daybot.ledger import is_holding
    assert t["status"] == "open" and is_holding(t)
    assert app.ledger.day_state(DAY)["open"] == 0 and "VOO" not in app.monitor.symbols()


def test_in_play_discovery_adds_a_mover():
    app = _day_app()
    app.fake.movers_data = {"gainers": [{"symbol": "NEWS"}], "losers": []}
    app.fake.now = at(DAY, "10:30")
    asyncio.run(app.data.load_daily(["NEWS"], app.fake.now))
    app.data.live_avg["NEWS"] = (app.fake.now, 400_000.0)
    found = asyncio.run(app.scanner.discover(at(DAY, "10:30")))
    assert [f["symbol"] for f in found] == ["NEWS"]
    assert "In play: NEWS" in app.notes.last("INPLAY")["text"]
    assert "NEWS" in app.scanner.watchlist(DAY)


def test_commands_answer():
    app = _day_app()
    app.fake.now = at(DAY, "11:00")
    now = at(DAY, "11:00")

    def say(text):
        return asyncio.run(app.commands.text(text, now))
    assert say("/help")[0]["text"] == HELP
    out = say("TEST")[0]["text"]
    assert "Check · TEST long" in out and "Plan:" in out
    assert "Unknown command" in say("/nope")[0]["text"]
    assert "NVDA" in say("/watch NVDA AMD")[0]["text"] and app.cfg.watchlist[-2:] == ["NVDA", "AMD"]
    assert "NVDA" not in say("/watch -NVDA")[0]["text"].split("\n")[0]
    assert "✅ RISK_PER_TRADE_PCT = 0.5" in say("/set RISK_PER_TRADE_PCT 0.5")[0]["text"]
    assert "must be between" in say("/set RISK_PER_TRADE_PCT 99")[0]["text"]
    assert "RISK_PER_TRADE_PCT" in say("/rules")[0]["text"]
    assert "Setups and what they've earned" in say("/setups")[0]["text"]
    assert "No trades today" in say("/trades")[0]["text"]
    assert "No closed trades" in say("/journal")[0]["text"]
    assert "Market data" in say("/status")[0]["text"]
    assert "read-only" in say("/connect")[0]["text"]
    assert "Paper autopilot" in say("/paper")[0]["text"]
    assert "Today's setups" in say("/alerts")[0]["text"] or "No setup alerts" in say("/alerts")[0]["text"]
    assert "paused" in say("/pause")[0]["text"] and not app.cfg.flag("ALERTS_ENABLED")
    assert "on" in say("/resume")[0]["text"] and app.cfg.flag("ALERTS_ENABLED")
    assert "needs Claude" in say("/ask should I hold?")[0]["text"]
    chart = say("/chart TEST")[0]
    assert "TEST" in chart["text"]
    plan = say("/plan")[0]["text"]
    assert "Game plan" in plan


def test_settings_survive_a_restart():
    app = _day_app()
    asyncio.run(app.commands.text("/set MAX_TRADES_PER_DAY 3", at(DAY, "11:00")))
    asyncio.run(app.commands.text("/watch AMD", at(DAY, "11:00")))
    from daybot.app import App
    again = App(app.cfg.__class__(), None, db=app.db, alpaca=app.fake, snap=app.snap, notify=app.notes, charts=False)
    assert again.cfg.get("MAX_TRADES_PER_DAY") == 3 and again.cfg.watchlist == ["TEST", "AMD"]


def test_a_chart_screenshot_is_read_and_backed_by_the_bots_numbers():
    app = _day_app()

    class FakeAI:
        ok = True
        why_off = ""
        last_error = ""

        async def read_chart(self, image, media_type, question):
            return {"ticker": "TEST", "timeframe": "5-min", "trend": "up", "pattern": "bull flag", "bias": "long",
                    "confidence": "medium", "levels": [{"price": 103.0, "kind": "high of day"}], "entry_idea": "over 102.2",
                    "stop_idea": "101.7", "targets": "103, 104", "invalidation": "back under VWAP", "risks": [],
                    "answer": "Only over the flag high."}
    app.ai = FakeAI()
    app.fake.now = at(DAY, "10:56")
    out = asyncio.run(app.commands.photo(b"img", "should I buy?", at(DAY, "10:56")))
    text = out[0]["text"]
    assert "Chart read · TEST" in text and "The bot's numbers" in text and "an opinion, not a signal" in text


def test_without_claude_the_screenshot_reply_explains_how_to_turn_it_on():
    app = _day_app()
    out = asyncio.run(app.commands.photo(b"img", "", at(DAY, "10:56")))
    assert "needs Claude" in out[0]["text"]


def test_day_report_after_trades_and_week_report():
    app = _day_app()
    _walk(app, "10:30", "12:15", broker=False)
    for f in (stock_fill("a", "BUY", 100, 102.6, at(DAY, "10:57")), stock_fill("b", "SELL", 100, 103.9, at(DAY, "11:40"))):
        app.snap.fills["acc1"].append(f)
    asyncio.run(app.broker.poll(at(DAY, "11:41")))
    text = asyncio.run(app.day_report(DAY, send=False))
    assert "1 trades closed: +$130.00" in text and "Rules: ✅" in text
    week = asyncio.run(app.commands.text("/week", at(DAY, "16:30")))[0]["text"]
    assert "Your week" in week


def test_dashboard_state_and_routes():
    from aiohttp.test_utils import TestClient, TestServer
    from daybot.dashboard import make_app as make_web, state
    app = _day_app()
    _walk(app, "10:40", "11:00")
    st = state(app, at(DAY, "11:00"))
    json.dumps(st, default=str)
    assert st["alerts"] and st["watch"] and len(st["setups"]) == 8 and st["settings"]
    assert isinstance(st["today"]["trades"], int) and isinstance(st["trade_list"], list)    # a count, not a list

    async def go():
        client = TestClient(TestServer(make_web(app)))
        await client.start_server()
        page = await (await client.get("/")).text()
        api = await (await client.get("/api/state")).json()
        chk = await (await client.post("/api/check", json={"text": "TEST"})).json()
        bad = await (await client.post("/api/set", json={"name": "RISK_PER_TRADE_PCT", "value": "50"})).json()
        good = await (await client.post("/api/set", json={"name": "RISK_PER_TRADE_PCT", "value": "0.75"})).json()
        await client.close()
        return page, api, chk, bad, good
    page, api, chk, bad, good = asyncio.run(go())
    assert "DayBot" in page and api["version"] and "Check · TEST" in chk["html"]
    assert not bad["ok"] and good["ok"] and app.cfg.get("RISK_PER_TRADE_PCT") == 0.75


def test_dashboard_keeps_what_you_own_apart_from_day_trades():
    from daybot.dashboard import state
    from tests.day_helpers import stock_fill
    app = _day_app()
    asyncio.run(app.broker.refresh_accounts(at(DAY, "09:00")))
    acc = app.db.scalar("select id from accounts limit 1")
    led = app.ledger
    led.apply(led.add_fill(stock_fill("old", "BUY", 2, 400.0, at(DAY, "10:00") - 40 * 86400, "VOO", acc)))
    led.mark_holdings(at(DAY, "00:00"))
    app.db.run("""insert into positions(account_id, key, symbol, underlying, kind, qty, avg_price, price, multiplier,
                  updated) values(?, 'VOO', 'VOO', 'VOO', 'stock', 2.123456789, 400, 410, 1, 0)""", (acc,))
    led.apply(led.add_fill(stock_fill("d1", "BUY", 10, 100.0, at(DAY, "10:05"), "TEST", acc)))
    st = state(app, at(DAY, "10:30"))
    assert [o["underlying"] for o in st["open"]] == ["TEST"]
    assert [t["underlying"] for t in st["trade_list"]] == ["TEST"] and st["today"]["trades"] == 1
    voo = next(h for h in st["holdings"] if h["symbol"] == "VOO")
    assert abs(voo["pnl"] - 2.123456789 * 10) < 1e-6 and not voo["investing"]
    assert st["cash"] == 10_000.0


def test_backtest_command_runs_in_the_background():
    app = _day_app()
    app.db.run("delete from meta where key='backtest_ts'")

    async def go():
        out = await app.commands.text("/backtest", at(DAY, "18:00"))
        again = await app.commands.text("/backtest", at(DAY, "18:00"))
        await app.backtest_task
        return out, again
    out, again = asyncio.run(go())
    assert "Backtest started" in out[0]["text"] and "already running" in again[0]["text"]
    assert app.notes.last("BACKTEST") and app.db.get_meta("backtest_ts")


def test_charts_render_when_matplotlib_is_there():
    pytest.importorskip("matplotlib")
    from daybot import charts
    app = _day_app(charts=charts)
    app.fake.now = at(DAY, "12:00")
    out = asyncio.run(app.commands.text("/chart TEST", at(DAY, "12:00")))
    assert out[0]["photo"][:4] == b"\x89PNG"


def test_export_has_the_journal_and_no_secrets(tmp_path):
    import zipfile
    import io
    from daybot.export import build
    app = _day_app()
    app.cfg.alpaca_secret = "SECRET-123"
    app.cfg.snaptrade_consumer_key = "CONSUMER-456"
    asyncio.run(app.broker.refresh_accounts(at(DAY, "09:00")))
    app.snap.fills["acc1"] = [stock_fill("x", "BUY", 10, 102.0, at(DAY, "10:57"))]
    asyncio.run(app.broker.poll(at(DAY, "10:58")))
    data = build(app.db, app.cfg, at(DAY, "16:30"))
    z = zipfile.ZipFile(io.BytesIO(data))
    assert {"trades.json", "fills.json", "checks.json", "accounts.json", "settings.json"} <= set(z.namelist())
    blob = b"".join(z.read(n) for n in z.namelist())
    assert b"SECRET-123" not in blob and b"CONSUMER-456" not in blob and b"****1234" not in blob
    assert json.loads(z.read("trades.json"))[0]["symbol"] == "TEST"
    app.cfg.state_dir = tmp_path
    out = asyncio.run(app.commands.text("/export", at(DAY, "16:30")))
    assert "Export saved" in out[0]["text"] and list((tmp_path / "exports").glob("*.zip"))


def test_price_alerts():
    app = _day_app()
    app.fake.now = at(DAY, "10:30")
    out = asyncio.run(app.commands.text("/alert TEST 102.9 flag high", at(DAY, "10:30")))[0]["text"]
    assert "above $102.90" in out
    assert "TEST above $102.90" in asyncio.run(app.commands.text("/alert", at(DAY, "10:30")))[0]["text"]
    _walk(app, "10:31", "11:45")
    hits = [m for m in app.notes.sent if m["kind"] == "PRICE"]
    assert len(hits) == 1 and "flag high" in hits[0]["text"]
    assert "No price alerts" in asyncio.run(app.commands.text("/alert", at(DAY, "12:00")))[0]["text"]


def test_the_first_private_message_links_the_bot_to_your_chat():
    from daybot.app import App

    class FakeTG:
        ok = True
        chat_id = ""
        conflict_ts = 0

        def __init__(self):
            self.sent = []

        async def send(self, text, buttons=None, silent=False, reply_to=None, chat_id=None):
            self.sent.append((chat_id or self.chat_id, text))
            return 1

        async def answer(self, *a, **k):
            pass
    app = _day_app()
    app.cfg.telegram_chat_id = ""
    tg = FakeTG()
    app.tg = tg
    asyncio.run(app.handle_update({"update_id": 1, "message": {"chat": {"id": 777, "type": "private"}, "text": "/start"}}))
    assert app.cfg.telegram_chat_id == "777" and tg.chat_id == "777" and "Linked" in tg.sent[-1][1]
    asyncio.run(app.handle_update({"update_id": 2, "message": {"chat": {"id": 999, "type": "private"}, "text": "/help"}}))
    assert tg.sent[-1][0] == "999" and "private bot" in tg.sent[-1][1]        # someone else: not answered
    again = App(app.cfg.__class__(), None, db=app.db, alpaca=app.fake, snap=app.snap, notify=app.notes, charts=False)
    assert again.cfg.telegram_chat_id == "777"                                # remembered after a restart
