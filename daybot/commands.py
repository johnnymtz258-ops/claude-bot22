"""Telegram commands. Each handler returns replies: {"text", "photo" (png bytes), "buttons"}.

Paste a ticker (NVDA) or a contract (NVDA 140c 10/24) to check it; send a chart screenshot to have it read;
end a message with "?" to ask the assistant.
"""
from __future__ import annotations

import re
import time

from . import VERSION
from .backtest import STATUS_TEXT
from .check import format_check
from .clock import at, clock_text, day_of, et
from .coach import format_leaks, leaks, week_report
from .config import SETTINGS, parse_watchlist
from .options import parse_contract_text
from . import today as today_mod
from .setups import label
from .util import big, dumps, esc, loads, money, num, pct, price as fmt_price, rmult

HELP = f"""<b>📈 DayBot {VERSION}</b> — your day-trading assistant. It reads your trades, never places one.

<b>Check a trade</b>
• Paste a ticker: <code>NVDA</code> · or a contract: <code>NVDA 140c 10/24</code>
• /check NVDA 100 @141.2 · /check NVDA short · /check TSLA 250p fri 2
• Your own fills are checked automatically the moment they happen

<b>Charts</b>
• /chart NVDA (5-min) · /chart NVDA 1 · send a <b>screenshot</b> to have it read

<b>Today</b>
• /plan game plan · /gappers · /alerts · /watch NVDA AMD (/watch -NVDA removes)
• /alert NVDA 141.50 — a ping when it trades through (works pre/after hours too)
• /trades your trades today · /positions · /accounts

<b>Getting better</b>
• /journal where your money leaks · /day report · /week
• /autopilot the pretend account that follows every alert — is the bot making money?
• /setups what each setup has earned · /backtest re-test them

<b>Right now</b>
• /today what to do today across everything, and what each part is worth at your size
<b>Invest (slow, tested)</b>
• /invest the ETF plans tested on 10 years of prices, which one to follow and what to buy or sell
• /invest grow 50 5 what $50 a month for 5 years became in the past
<b>Crypto</b>
• /crypto the daily trend plan for Bitcoin and the big coins, its pretend account and its backtest
• /btc15 which way Bitcoin most likely finishes this 15 minutes (Up or Down markets) · /btc15 62 vs a 62¢ price

<b>Assistant</b>
• /ask anything, or end a message with "?" — it answers from your live data

<b>Settings</b>
• /rules · /set RISK_PER_TRADE_PCT 0.5 · /pause /resume alerts · /connect your broker · /status · /export"""


def reply(text: str = "", photo: bytes | None = None, buttons=None) -> dict:
    return {"text": text, "photo": photo, "buttons": buttons}


_TICKER = re.compile(r"^\$?([A-Za-z]{1,5}(?:\.[A-Za-z])?)$")
CHATTER = {"hi", "hey", "ok", "okay", "yo", "thx", "ty", "yes", "no", "help", "what", "why", "how", "lol", "gm", "gn",
           "cool", "nice", "sure", "k", "kk", "hmm", "wow", "test", "ping"}


def parse_check_args(text: str, today) -> dict | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    px = 0.0
    m = re.search(r"@\s*\$?(\d+(?:\.\d+)?)", raw)
    if m:
        px = float(m.group(1))
        raw = (raw[:m.start()] + raw[m.end():]).strip()
    contract = parse_contract_text(raw, today)
    tokens = raw.replace(",", " ").split()
    if contract:
        strike_seen = False
        qty = 0.0
        for tok in tokens[1:]:
            low = tok.lower()
            if re.fullmatch(r"\d+(\.\d+)?(c|p|calls?|puts?)", low) or ("/" in low) or ("-" in low):
                strike_seen = strike_seen or bool(re.match(r"\d", low))
                continue
            if re.fullmatch(r"\d+(\.\d+)?", low):
                if not strike_seen and float(low) == contract["strike"]:
                    strike_seen = True
                    continue
                qty = float(low)
        return {"symbol": contract["underlying"], "kind": "option", "contract": contract.get("symbol", ""),
                "contract_info": contract, "direction": "long" if contract["type"] == "call" else "short",
                "qty": qty, "price": px}
    if not tokens or not _TICKER.match(tokens[0]):
        return None
    first = tokens[0]
    if len(tokens) == 1 and first.islower() and first in CHATTER:
        return None                                    # "ok", "hi": a word, not a ticker
    words = {"short", "sell", "long", "buy", "put", "puts", "down", "up", "shares", "sh"}
    if any(t.lower() not in words and not re.fullmatch(r"\d+(\.\d+)?", t) for t in tokens[1:]):
        return None                                    # a sentence, not a ticker
    sym = first.lstrip("$").upper()
    direction = "short" if any(t.lower() in {"short", "sell", "puts", "put", "down"} for t in tokens[1:]) else "long"
    qty = next((float(t) for t in tokens[1:] if re.fullmatch(r"\d+(\.\d+)?", t)), 0.0)
    return {"symbol": sym, "kind": "stock", "contract": "", "direction": direction, "qty": qty, "price": px}


class Commands:
    def __init__(self, app):
        self.app = app

    # ---- entry points ----------------------------------------------------------------------------------------
    async def text(self, text: str, now: float | None = None) -> list[dict]:
        now = now or time.time()
        t = str(text or "").strip()
        if not t:
            return []
        if t.startswith("/"):
            head, _, rest = t.partition(" ")
            name = head[1:].split("@")[0].lower()
            fn = getattr(self, f"cmd_{ALIASES.get(name, name)}", None)
            if not fn:
                return [reply(f"Unknown command {esc(head)}. /help lists them.")]
            return await fn(rest.strip(), now)
        if t.endswith("?") and self.app.ai.ok:
            return await self.cmd_ask(t, now)
        args = parse_check_args(t, et(now).date())
        if args and len(t) <= 40:
            return await self._check(args, now)
        if self.app.ai.ok:
            return await self.cmd_ask(t, now)
        return [reply("Send a ticker (NVDA) or a contract (NVDA 140c 10/24) to check it, a chart screenshot to read "
                      "it, or /help.")]

    async def photo(self, image: bytes, caption: str = "", now: float | None = None) -> list[dict]:
        now = now or time.time()
        ai = self.app.ai
        if not ai.ok:
            return [reply(f"Chart reading needs Claude: {esc(ai.why_off or 'AI is off')}. "
                          "See DAYBOT.md → “AI chart reading”.")]
        res = await ai.read_chart(image, "image/jpeg", caption)
        if not res:
            return [reply(f"Couldn't read that chart: {esc(ai.last_error)}")]
        out = []
        bot_lines: list[str] = []
        sym = str(res.get("ticker") or "").upper().lstrip("$")
        if not sym and caption:
            first = caption.strip().split()[0] if caption.strip() else ""
            if _TICKER.match(first):
                sym = first.lstrip("$").upper()
        photo = None
        if sym and _TICKER.match(sym) and self.app.data.ok:
            try:
                check = await self.app.checker.check({"symbol": sym, "kind": "stock",
                                                      "direction": "short" if res.get("bias") == "short" else "long"}, now)
                lv = check.get("levels") or {}
                if check.get("und_price"):
                    bits = [f"{esc(sym)} {fmt_price(check['und_price'])}"]
                    if lv.get("vwap"):
                        bits.append(f"VWAP {fmt_price(lv['vwap'])}")
                    for key, name in (("pmh", "PM high"), ("pdh", "Y high"), ("pdl", "Y low")):
                        if lv.get(key):
                            bits.append(f"{name} {fmt_price(lv[key])}")
                    bot_lines.append("📐 The bot's numbers: " + " · ".join(bits))
                    bot_lines.append(f"🧾 Its check on a {check['direction']} here: {check['verdict']} "
                                     + "; ".join(esc(i[1]) for i in check["items"] if i[0] != "🟢")[:300])
                    photo = self._render(sym, now, plan=check.get("plan"))
            except Exception as exc:
                self.app.last_error = f"photo check {type(exc).__name__}"
        from .ai import format_chart_read
        out.append(reply(format_chart_read(res, bot_lines)))
        if photo:
            out.append(reply(f"{esc(sym)} now, from live data", photo=photo))
        return out

    async def button(self, data: str, now: float | None = None) -> list[dict]:
        now = now or time.time()
        kind, _, arg = str(data or "").partition(":")
        if kind == "chart":
            return await self.cmd_chart(arg, now)
        if kind == "check":
            return await self.cmd_check(arg, now)
        return []

    # ---- checks and charts -----------------------------------------------------------------------------------
    async def _check(self, args: dict, now: float) -> list[dict]:
        if not self.app.data.ok:
            return [reply(NO_DATA)]
        res = await self.app.checker.check(args, now)
        self.app.db.insert("""insert into checks(ts, symbol, contract, side, qty, price, verdict, items, plan, source)
                              values(?,?,?,?,?,?,?,?,?, 'manual')""",
                           (int(now), args["symbol"], args.get("contract", ""), "BUY" if args["direction"] == "long" else "SELL",
                            args.get("qty", 0), args.get("price", 0), res["verdict"], dumps([list(i) for i in res["items"]]),
                            dumps(res.get("plan") or {})))
        photo = self._render(args["symbol"], now, plan={**(res.get("plan") or {}), "entry": res.get("und_price")})
        return [reply(format_check(res), photo=photo,
                      buttons=[[("📈 Chart", None, f"chart:{args['symbol']}")]])]

    def _render(self, sym: str, now: float, plan: dict | None = None, minutes: int = 5) -> bytes | None:
        charts = self.app.charts
        if not charts or not self.app.data.bars1(sym):
            return None
        day = day_of(now)
        if self.app.market.session(now) == "closed" or not self.app.market.is_trading_day(day):
            bars = self.app.data.bars1(sym)
            day = day_of(bars[-1][0]) if bars else day
        fills = self.app.db.rows("select ts, side, price from fills where underlying=? and day=? and kind='stock'",
                                 (sym, day))
        return charts.render(sym, self.app.data.bars1(sym), day, self.app.market, levels=self.app.data.levels(sym, now),
                             plan=plan, fills=fills, minutes=minutes)

    async def cmd_check(self, rest: str, now: float) -> list[dict]:
        args = parse_check_args(rest, et(now).date())
        if not args:
            return [reply("Usage: /check NVDA · /check NVDA 100 @141.2 · /check NVDA short · /check NVDA 140c 10/24 2")]
        return await self._check(args, now)

    async def cmd_chart(self, rest: str, now: float) -> list[dict]:
        parts = rest.split()
        if not parts or not _TICKER.match(parts[0]):
            return [reply("Usage: /chart NVDA [1|5|15]")]
        if not self.app.data.ok:
            return [reply(NO_DATA)]
        sym = parts[0].lstrip("$").upper()
        minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) in (1, 2, 5, 10, 15, 30) else 5
        await self.app.data.load_daily([sym], now)
        await self.app.data.refresh([sym], now)
        if not self.app.data.bars1(sym):
            await self._last_session(sym, now)
        lv = self.app.data.levels(sym, now)
        px = self.app.data.last_price(sym)
        if not px:
            return [reply(f"No data for {esc(sym)} — check the ticker.")]
        bits = [f"<b>{esc(sym)} {fmt_price(px)}</b>"]
        if lv.get("pdc"):
            bits.append(pct((px / lv["pdc"] - 1) * 100))
        if lv.get("vwap"):
            bits.append(f"VWAP {fmt_price(lv['vwap'])}")
        if lv.get("rvol"):
            bits.append(f"vol {lv['rvol']:.1f}×")
        if lv.get("atr"):
            bits.append(f"daily range ~{fmt_price(lv['atr'])}")
        photo = self._render(sym, now, minutes=minutes)
        caption = " · ".join(bits)
        if not photo:
            caption += "\n(chart pictures need matplotlib: pip install -r requirements-day.txt)"
        return [reply(caption, photo=photo, buttons=[[("🧾 Check long", None, f"check:{sym}"),
                                                      ("🧾 Check short", None, f"check:{sym} short")]])]

    async def _last_session(self, sym: str, now: float) -> None:
        """Weekend / overnight: load the last session's bars so /chart still shows something."""
        day = day_of(now)
        mkt = self.app.market
        prev = day if mkt.is_trading_day(day) and now >= at(day, "04:00") else mkt.prev_day(day)
        got = await self.app.data.alpaca.bars([sym], "1Min", at(prev, "04:00"), min(now, at(prev, "20:00")))
        self.app.data.minute[sym] = got.get(sym) or []
        self.app.data.minute_day[sym] = day

    # ---- today ------------------------------------------------------------------------------------------------
    async def cmd_plan(self, rest: str, now: float) -> list[dict]:
        if not self.app.data.ok:
            return [reply(NO_DATA)]
        text = await self.app.morning_plan(now, send=False)
        return [reply(text)]

    async def cmd_gappers(self, rest: str, now: float) -> list[dict]:
        day = day_of(now)
        gs = self.app.scanner.gappers(day)
        if not gs and self.app.data.ok:
            gs = await self.app.scanner.premarket(now)
        if not gs:
            return [reply("No gappers past your filters right now.")]
        lines = ["<b>Gappers</b>"]
        for g in gs[:12]:
            cat = (g.get("catalyst") or {}).get("headline", "")
            lines.append(f"• <b>{esc(g['symbol'])}</b> {pct(g['gap'])} {fmt_price(g['price'])} · PM vol {big(g['pm_vol'])}"
                         + (f" — {esc(cat[:80])}" if cat else ""))
        return [reply("\n".join(lines))]

    async def cmd_alerts(self, rest: str, now: float) -> list[dict]:
        rows = self.app.db.rows("select * from alerts where day=? order by ts desc limit 20", (day_of(now),))
        if not rows:
            return [reply("No setup alerts today yet.")]
        lines = ["<b>Today's setups</b>"]
        for a in rows:
            p = self.app.db.row("select * from paper where alert_id=?", (a["id"],))
            res = ""
            if p and p["status"] == "closed":
                res = f" → paper {rmult(p['r_mult'])}"
            elif p and p["status"] == "open":
                res = " → paper open"
            badge = "✅" if a["proven"] else "🧪"
            lines.append(f"{clock_text(a['ts'])} {badge} {esc(a['symbol'])} {esc(label(a['setup'], a['direction']))} @ "
                         f"{fmt_price(a['entry'])}{res}")
        return [reply("\n".join(lines))]

    async def cmd_watch(self, rest: str, now: float) -> list[dict]:
        cfg = self.app.cfg
        tokens = rest.replace(",", " ").split()
        remove = [t.lstrip("-$").upper() for t in tokens if t.startswith("-")]
        add = parse_watchlist(" ".join(t for t in tokens if not t.startswith("-")))
        cur = list(cfg.watchlist)
        for s in add:
            if s not in cur:
                cur.append(s)
        cur = [s for s in cur if s not in remove]
        if add or remove:
            cfg.watchlist = cur
            self.app.db.set_meta("watchlist", dumps(cur))
        return [reply("👀 Always watching: " + (", ".join(cur) if cur else "nothing extra (gappers + SPY/QQQ only)")
                      + "\nAdd: /watch NVDA AMD · remove: /watch -NVDA")]

    async def cmd_alert(self, rest: str, now: float) -> list[dict]:
        """/alert NVDA 141.50 [note] · /alert (list) · /alert -3 (delete #3)"""
        db = self.app.db
        parts = rest.split()
        if parts and parts[0].startswith("-") and parts[0][1:].isdigit():
            n = db.run("delete from price_alerts where id=?", (int(parts[0][1:]),))
            return [reply("🗑 Alert removed." if n else "No alert with that number.")]
        if len(parts) >= 2 and _TICKER.match(parts[0]) and re.fullmatch(r"\$?\d+(\.\d+)?", parts[1]):
            sym, level = parts[0].lstrip("$").upper(), float(parts[1].lstrip("$"))
            if self.app.data.ok:
                await self.app.data.refresh([sym], now)
            px = self.app.data.last_price(sym)
            if not px and self.app.data.ok:
                snap = await self.app.data.snapshot(sym)
                from .alpaca import snap_price
                px = snap_price(snap)
            direction = "above" if not px or level >= px else "below"
            db.insert("insert into price_alerts(symbol, price, direction, note, created) values(?,?,?,?,?)",
                      (sym, level, direction, " ".join(parts[2:])[:80], int(now)))
            where = f" (now {fmt_price(px)})" if px else ""
            return [reply(f"🔔 I'll tell you when {esc(sym)} trades {direction} {fmt_price(level)}{where}.")]
        rows = db.rows("select * from price_alerts where triggered=0 order by symbol, price")
        if not rows:
            return [reply("No price alerts. Set one: /alert NVDA 141.50 (optional note after it)")]
        lines = ["<b>🔔 Your price alerts</b>"] + [f"#{r['id']} {esc(r['symbol'])} {r['direction']} {fmt_price(r['price'])}"
                                                  + (f" — {esc(r['note'])}" if r["note"] else "") for r in rows]
        lines.append("Remove one: /alert -NUMBER")
        return [reply("\n".join(lines))]

    async def cmd_trades(self, rest: str, now: float) -> list[dict]:
        day = day_of(now)
        rows = self.app.db.rows("""select * from trades where day=? or (status='closed' and closed_ts>=?)
                                   order by opened_ts""", (day, int(now) - 86400))
        rows = [r for r in rows if r["day"] == day or day_of(r["closed_ts"] or 0) == day]
        if not rows:
            return [reply("No trades today." + ("" if self.app.broker.ok else " (Broker not connected: /connect)"))]
        lines = [f"<b>Your trades · {et(now).strftime('%a %b %d')}</b>"]
        total = 0.0
        for t in rows:
            what = (f"{t['underlying']} {num(t['strike']):g}{(t['option_type'] or 'C')[0]} {t['expiry'][5:]}"
                    if t["kind"] == "option" else t["symbol"])
            if t["status"] == "closed":
                total += num(t["pnl"])
                r = f" {rmult(t['r_mult'])}" if t["r_mult"] is not None else ""
                lines.append(f"{'✅' if num(t['pnl']) > 0 else '❌'} {esc(what)} {t['direction']} {money(t['pnl'], True)}{r}")
            else:
                mark = self.app.data.last_price(t["underlying"]) if t["kind"] == "stock" else 0
                s = 1 if t["direction"] == "long" else -1
                unreal = s * (mark - num(t["entry_avg"])) * num(t["open_qty"]) if mark else None
                lines.append(f"⏳ {esc(what)} {t['direction']} {num(t['open_qty']):g} @ {fmt_price(t['entry_avg'])}"
                             + (f" · {money(unreal, True)} open" if unreal is not None else "")
                             + (f" · stop {fmt_price(t['plan_stop'])}" if num(t["plan_stop"]) else ""))
        lines.append(f"Closed today: {money(total, True)}")
        return [reply("\n".join(lines))]

    async def cmd_positions(self, rest: str, now: float) -> list[dict]:
        rows = self.app.db.rows("select * from positions order by kind, symbol")
        if not rows:
            return [reply("No positions synced." + ("" if self.app.broker.ok else " Connect your broker: /connect"))]
        lines = ["<b>Positions</b>"]
        for p in rows:
            what = (f"{p['underlying']} {num(p['strike']):g}{(p['option_type'] or 'C')[0]} {p['expiry']}"
                    if p["kind"] == "option" else p["symbol"])
            val = num(p["price"]) * num(p["qty"]) * num(p["multiplier"], 1)
            pnl = (num(p["price"]) - num(p["avg_price"])) * num(p["qty"]) * num(p["multiplier"], 1) if num(p["avg_price"]) else None
            lines.append(f"• {esc(what)} × {num(p['qty']):g} · {money(val)}" + (f" · {money(pnl, True)}" if pnl is not None else ""))
        return [reply("\n".join(lines))]

    async def cmd_accounts(self, rest: str, now: float) -> list[dict]:
        db = self.app.db
        broker, cfg = self.app.broker, self.app.cfg
        if not broker.ok:
            if cfg.snaptrade_client_id and not cfg.snaptrade_consumer_key:
                return [reply("DayBot has your SnapTrade Client ID but not the <b>Consumer Key</b>, so it can't read your "
                              "accounts yet. Linking Robinhood / Chase on SnapTrade's site isn't enough by itself — the "
                              "key is what lets DayBot in. Get it in the SnapTrade dashboard (after the 2-step code), then "
                              "add it with daybot_setup.command and restart DayBot.")]
            return [reply("SnapTrade isn't set up in DayBot yet — /connect explains the two keys it needs.")]
        await broker.refresh_accounts(now)               # ask SnapTrade now, not at the next scheduled sync
        rows = db.rows("select * from accounts order by total desc")
        if not rows:
            err = self.app.snap.last_error
            if err and not self.app.snap.healthy:
                return [reply(f"SnapTrade answered with an error: <code>{esc(err[:220])}</code>\nIf it says the "
                              "signature or key is invalid, the Consumer Key was pasted wrong — add it again with "
                              "daybot_setup.command and restart DayBot.")]
            conns = getattr(self.app.snap, "last_connections", []) or []
            if conns:
                names = ", ".join(esc(c["name"] or "a brokerage") + (" (needs re-linking)" if c.get("disabled") else "")
                                  for c in conns)
                return [reply(f"SnapTrade sees your connection ({names}) but no trading account in it yet — it's "
                              "usually still syncing right after you link. Send /accounts again in a few minutes. If a "
                              "connection says it needs re-linking, tap /connect.")]
            return [reply("Your SnapTrade keys work, but SnapTrade shows <b>no brokerage linked to these keys</b>. Tap "
                          "/connect and link Robinhood / Chase there (read-only), then send /accounts again. (If you "
                          "linked them on SnapTrade's website, it may have been under a different SnapTrade login.)")]
        parts = rest.split()
        if len(parts) == 2 and parts[0].lower() in {"on", "off"} and parts[1].isdigit() and 1 <= int(parts[1]) <= len(rows):
            acc = rows[int(parts[1]) - 1]
            db.run("update accounts set checks=? where id=?", (1 if parts[0].lower() == "on" else 0, acc["id"]))
            rows = db.rows("select * from accounts order by total desc")
        lines = ["<b>Accounts</b> (read-only)"]
        for i, a in enumerate(rows, 1):
            mode = "🧾 trades checked" if num(a["checks"], 1) else "💤 investing — synced, not checked"
            lines.append(f"{i}. {esc(a['institution'] or a['name'])} {esc(a['number'])} · {money(a['total'])} · cash "
                         f"{money(a['cash'])} · buying power {money(a['buying_power'])}"
                         + (f" · {a['margin']}" if a["margin"] else "") + f"\n   {mode}")
        lines.append("Long-term account? /accounts off 2 stops the day-trade checks on #2 (/accounts on 2 undoes it).")
        return [reply("\n".join(lines))]

    async def cmd_connect(self, rest: str, now: float) -> list[dict]:
        broker = self.app.broker
        if not broker.ok and self.app.cfg.snaptrade_client_id:
            return [reply("Almost there: your SnapTrade Client ID is set, the Consumer Key isn't. Get it in the SnapTrade "
                          "dashboard (it asks for a 2-step code first), then double-click daybot_setup.command, paste "
                          "it, press Enter through the rest, and restart DayBot.")]
        if not broker.ok:
            return [reply("To read your Robinhood / Chase trades, DayBot uses SnapTrade (read-only, free for personal "
                          "use):\n1. Sign up at snaptrade.com → Personal → copy your Client ID and Consumer Key\n"
                          "2. Put them in .env as SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY (or run "
                          "daybot_setup.py)\n3. Restart DayBot and send /connect again")]
        try:
            link = await broker.snap.connect_link()
        except Exception as exc:
            return [reply(f"Couldn't get a connection link: {esc(str(exc)[:200])}")]
        if not link:
            return [reply("SnapTrade didn't return a link — try again in a minute.")]
        return [reply("Connect Robinhood / Chase <b>read-only</b> (the link works for 5 minutes). Robinhood will ask "
                      "for your login and a code by text. When you're done, send /accounts.",
                      buttons=[[("🔗 Connect a broker (read-only)", link, None)]])]

    # ---- getting better ---------------------------------------------------------------------------------------
    async def cmd_journal(self, rest: str, now: float) -> list[dict]:
        days = int(rest) if rest.strip().isdigit() else 30
        return [reply(format_leaks(leaks(self.app.db, days, now)))]

    async def cmd_day(self, rest: str, now: float) -> list[dict]:
        return [reply(await self.app.day_report(day_of(now), send=False))]

    async def cmd_week(self, rest: str, now: float) -> list[dict]:
        return [reply(week_report(self.app.db, 7, now))]

    async def cmd_setups(self, rest: str, now: float) -> list[dict]:
        table = self.app.edge.table()
        ts = int(num(self.app.db.get_meta("backtest_ts"), 0))
        lines = ["<b>Setups and what they've earned</b>"]
        if ts:
            info = loads(self.app.db.get_meta("backtest_summary"), {}) or {}
            lines.append(f"Backtest {et(ts).strftime('%b %d')}: {info.get('days', '?')} days, {info.get('pairs', '?')} "
                         f"stock-days, {info.get('trades', '?')} trades, costs included")
        else:
            lines.append("No backtest yet — /backtest")
        shorts = self.app.cfg.flag("SHORT_ALERTS")
        for r in table:
            bt = r["backtest"]
            pp = r["paper"]
            status = "✅ proven live" if r.get("live") else STATUS_TEXT.get(r["status"], r["status"])
            line = f"{status} <b>{esc(r['label'])}</b>" + (" (paper only)" if r["direction"] == "short" and not shorts else "")
            if bt and bt["n"]:
                line += (f"\n   backtest {int(bt['n'])} trades · {num(bt['avg_r']):+.2f}R avg · {num(bt['won']) * 100:.0f}% won · "
                         f"halves {num(bt['half1']):+.2f}/{num(bt['half2']):+.2f}R"
                         + (f" · {num(bt['p_pos']) * 100:.0f}% sure it's real" if bt.get("p_pos") not in (None, 0.5) else "")
                         + (f"\n   {num(bt['avg_pct']):+.2f}% of the money a trade (typical {num(bt['med_pct']):+.2f}%)"
                            if bt.get("avg_pct") else ""))
            if pp["n"]:
                line += f"\n   paper {pp['n']} trades · {pp['avg_r']:+.2f}R avg"
            lines.append(line)
        lines.append("\nProven = made money after costs in both halves, without its 3 best trades, 40+ trades, and clearly above "
                     "luck when whole days are resampled. Probably real = a positive average that's 85%+ likely not luck. "
                     "No measurable edge / loses = paper only.")
        return [reply("\n".join(lines))]

    async def cmd_backtest(self, rest: str, now: float) -> list[dict]:
        if not self.app.data.ok:
            return [reply(NO_DATA)]
        started = self.app.start_backtest()
        if not started:
            return [reply(f"A backtest is already running ({esc(self.app.backtest_progress)}).")]
        return [reply(f"🧪 Backtest started: {self.app.cfg.get('BACKTEST_DAYS'):.0f} trading days of history. It takes a few "
                      "minutes; you'll get the results here.")]

    async def cmd_autopilot(self, rest: str, now: float) -> list[dict]:
        ap = self.app.autopilot
        words = rest.strip().lower().split()
        if words and words[0] in {"reset", "restart", "start"}:
            amount = num(words[1].replace("$", "").replace(",", "")) if len(words) > 1 else 0.0
            amount = ap.reset(now, amount)
            if not ap.on:
                self.app.cfg.set("AUTOPILOT", "1")
                self.app.db.set_meta("set:AUTOPILOT", "1")
            return [reply(f"🤖 Autopilot starts over with a pretend {money(amount)}. It takes the next setup the bot "
                          "alerts on and tells you each buy and sell.")]
        if words and words[0] in {"off", "stop"}:
            self.app.cfg.set("AUTOPILOT", "0")
            self.app.db.set_meta("set:AUTOPILOT", "0")
            return [reply("Autopilot off. /autopilot on turns it back on (the balance is kept).")]
        if words and words[0] == "on":
            self.app.cfg.set("AUTOPILOT", "1")
            self.app.db.set_meta("set:AUTOPILOT", "1")
        text = ap.text(now)
        st = self.app.paper.stats()
        if st["n"]:
            text += (f"\n\n📊 Setup gauge (every signal, alerted or not): {st['n']} paper trades, {st['avg_r']:+.2f}R "
                     f"avg, {st['won'] * 100:.0f}% won — /setups for each one")
        return [reply(text)]

    async def cmd_crypto(self, rest: str, now: float) -> list[dict]:
        cr, cfg, db = self.app.crypto, self.app.cfg, self.app.db
        words = rest.strip().lower().split()
        verb = words[0] if words else ""
        if verb in {"on", "off"}:
            cfg.set("CRYPTO", "1" if verb == "on" else "0")
            db.set_meta("set:CRYPTO", cfg.get("CRYPTO"))
            if verb == "off":
                return [reply("🪙 Crypto plan off. /crypto on turns it back on (the pretend account is kept).")]
            await cr.tick(now)
        elif verb in {"reset", "restart", "start"}:
            amount = num(words[1].replace("$", "").replace(",", "")) if len(words) > 1 else 0.0
            amount = cr.reset(now, amount)
            await cr.tick(now)
            return [reply(f"🪙 The crypto autopilot starts over with a pretend {money(amount)} and buys into whatever is "
                          "trending now.\n\n" + cr.autopilot_line())]
        elif verb in {"test", "backtest", "bt"}:
            if not cr.on:
                return [reply("🪙 The crypto plan is off. /crypto on first.")]
            if not cr.series:
                await cr.tick(now)
            if not cr.series:
                return [reply(cr.text(now))]
            res = await cr.run_backtest(now, announce=False)
            if res.get("error"):
                return [reply(f"🪙 Couldn't test: {esc(res['error'])}")]
            return [reply(cr.backtest_text(res))]
        elif cr.on and not cr.series:
            await cr.tick(now)
        return [reply(cr.text(now))]

    async def cmd_today(self, rest: str, now: float) -> list[dict]:
        app = self.app
        for part in (app.invest, app.crypto, app.updown):
            if part.on and getattr(app.alpaca, "ok", False) and not (getattr(part, "bars", None) or getattr(part, "series", None)):
                await part.tick(now)
        return [reply(today_mod.text(app.today(now)))]

    async def cmd_invest(self, rest: str, now: float) -> list[dict]:
        iv, words = self.app.invest, rest.strip().lower().split()
        if words and words[0] == "plan":
            if len(words) < 2:
                lab = iv.lab_now() or {}
                keys = ", ".join(f"<code>{r['key']}</code>" for r in lab.get("strategies", []))
                return [reply(f"Which plan? /invest plan trend_spy — choices: {keys or 'loading…'}")]
            label = iv.set_plan(words[1])
            if not label:
                return [reply(f"No plan called {esc(words[1])}. /invest lists them.")]
            return [reply(f"🧺 You're following <b>{esc(label)}</b>.\n\n" + iv.text(now))]
        if words and words[0] in {"grow", "growth", "future", "project"}:
            nums = [num(w.replace("$", "").replace(",", ""), -1) for w in words[1:]]
            add = nums[0] if nums and nums[0] >= 0 else 50.0
            years = int(nums[1]) if len(nums) > 1 and 1 <= nums[1] <= 10 else 5
            return [reply(iv.grow_text(now, add, years))]
        if words and words[0] in {"test", "rerun", "refresh"}:
            await iv.rerun(now)
        elif iv.on and not iv.lab_now():
            await iv.tick(now)
        return [reply(iv.text(now))]

    async def cmd_btc15(self, rest: str, now: float) -> list[dict]:
        ud = self.app.updown
        if ud.on and getattr(self.app.alpaca, "ok", False):
            if not ud.bars:
                await ud.tick(now)
            else:
                await ud.load(now)
                await ud.refresh_price(now)
        return [reply(ud.text(now, rest))]

    # ---- the assistant ----------------------------------------------------------------------------------------
    async def cmd_ask(self, rest: str, now: float) -> list[dict]:
        ai = self.app.ai
        if not rest.strip():
            return [reply("Ask anything: /ask should I hold AMD into the close?")]
        if not ai.ok:
            return [reply(f"The assistant needs Claude: {esc(ai.why_off or 'AI is off')}.")]
        answer = await ai.ask(rest, await self.app.context(now))
        return [reply(esc(answer) if answer else f"No answer: {esc(ai.last_error)}")]

    # ---- settings ---------------------------------------------------------------------------------------------
    async def cmd_rules(self, rest: str, now: float) -> list[dict]:
        cfg = self.app.cfg
        lines = ["<b>Rules and settings</b> (/set NAME value)"]
        for name, spec in SETTINGS.items():
            v = cfg.get(name)
            val = ("on" if v >= 0.5 else "off") if spec.is_bool else f"{v:g}"
            lines.append(f"• <code>{name}</code> = {val} — {esc(spec.help)}")
        return [reply("\n".join(lines))]

    async def cmd_set(self, rest: str, now: float) -> list[dict]:
        parts = rest.split()
        if len(parts) != 2:
            return [reply("Usage: /set RISK_PER_TRADE_PCT 0.5 (see /rules)")]
        try:
            v = self.app.cfg.set(parts[0], parts[1])
        except ValueError as exc:
            return [reply(f"❌ {esc(exc)}")]
        self.app.db.set_meta(f"set:{parts[0].upper()}", v)
        return [reply(f"✅ {parts[0].upper()} = {v:g}")]

    async def cmd_pause(self, rest: str, now: float) -> list[dict]:
        self.app.cfg.set("ALERTS_ENABLED", 0)
        self.app.db.set_meta("set:ALERTS_ENABLED", 0)
        return [reply("⏸ Setup alerts paused (your trade checks still run). /resume to turn them back on.")]

    async def cmd_resume(self, rest: str, now: float) -> list[dict]:
        self.app.cfg.set("ALERTS_ENABLED", 1)
        self.app.db.set_meta("set:ALERTS_ENABLED", 1)
        return [reply("▶️ Setup alerts on.")]

    async def cmd_export(self, rest: str, now: float) -> list[dict]:
        from .export import write
        path = write(self.app.db, self.app.cfg, self.app.cfg.state_dir / "exports", now)
        tg = self.app.tg
        if tg and tg.ok and await tg.send_document(path, caption="DayBot review export — no keys, no account numbers"):
            return [reply("📦 Export sent above.")]
        return [reply(f"📦 Export saved: {esc(str(path))}")]

    async def cmd_status(self, rest: str, now: float) -> list[dict]:
        return [reply(self.app.status_text(now))]

    async def cmd_help(self, rest: str, now: float) -> list[dict]:
        return [reply(HELP)]

    cmd_start = cmd_help


ALIASES = {"c": "check", "edge": "setups", "gap": "gappers", "g": "gappers", "j": "journal", "leaks": "journal",
           "p": "plan", "report": "day", "pos": "positions", "acct": "accounts", "account": "accounts",
           "settings": "rules", "health": "status", "q": "ask", "w": "watch", "mytrades": "trades",
           "paper": "autopilot", "auto": "autopilot", "ap": "autopilot", "bot": "autopilot",
           "coin": "crypto", "coins": "crypto", "cr": "crypto", "trend": "crypto", "inv": "invest", "etf": "invest",
           "etfs": "invest", "tactical": "invest", "now": "today", "todo": "today",
           "btc": "btc15", "bitcoin": "btc15", "updown": "btc15", "15m": "btc15", "btc15m": "btc15", "ud": "btc15"}

NO_DATA = ("Market data isn't connected. DayBot needs a free Alpaca account for prices: sign up at alpaca.markets, "
           "create paper-trading API keys, and put them in .env as ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY "
           "(or run daybot_setup.py).")
