"""Reads your trades (SnapTrade, read-only) and reacts to each one:

  • a new position  -> the trade check, with a stop / targets / size plan, the moment the fill shows up
  • adding to one   -> a quick check (averaging down? over your size?)
  • trimming        -> a short note
  • closing         -> a recap: P&L, R, how it went against the plan, what it did after you left, and what your
                       day looks like now (loss streak -> take a break; daily limit -> done for today)

Partial fills are booked as they arrive (an order's filled quantity grows between polls).
"""
from __future__ import annotations

import datetime as dt

from .check import GREEN, RED, WORD, format_check
from .clock import clock_text, day_of, et
from .coach import bucket, excursion
from .ledger import is_holding
from .snaptrade import SnapTradeError
from .util import dumps, dur, esc, money, num, pct, price as fmt_price, rmult


def describe_fill(f: dict) -> str:
    qty = num(f["qty"])
    q = f"{qty:g}"
    if f["kind"] == "option":
        what = f"{f['underlying']} {num(f['strike']):g}{(f['option_type'] or 'C')[0]} {f['expiry'][5:].replace('-', '/')}"
        unit = "contract" if qty == 1 else "contracts"
        return f"{q} {esc(what)} {unit} @ {fmt_price(f['price'])}"
    return f"{q} {esc(f['symbol'])} @ {fmt_price(f['price'])}"


LATE_SECONDS = 15 * 60       # a fill older than this when we first see it gets booked, not checked


class BrokerSync:
    LEDGER_VERSION = "3"            # 3: cash sweeps (Chase QACDS…) are no longer booked as trades

    def __init__(self, cfg, db, snap, ledger, checker, notify, market, data):
        self.cfg, self.db, self.snap, self.ledger, self.checker = cfg, db, snap, ledger, checker
        self.notify, self.market, self.data = notify, market, data
        self.last_poll = 0.0
        self.last_error = ""
        self.disabled: list[str] = []
        self._rebuild_if_old()

    def _rebuild_if_old(self) -> None:
        """Earlier versions booked crypto and cash sweeps as stock trades and old investments as open day trades.
        Start the journal over once with the right rules: your history is read again from the broker (nothing on the
        broker side changes)."""
        if self.db.get_meta("ledger_version") == self.LEDGER_VERSION:
            return
        for table in ("fills", "trades", "orders_seen", "positions", "checks"):
            self.db.run(f"delete from {table}")
        self.db.run("delete from meta where key in ('history_done', 'fills_live_since') or key like 'primed:%'")
        self.db.run("delete from sent where key like 'mon:%'")
        self.db.set_meta("ledger_version", self.LEDGER_VERSION)

    @property
    def ok(self) -> bool:
        return bool(self.snap and self.snap.ok)

    def account_ids(self) -> list[str]:
        return [r["id"] for r in self.db.rows("select id from accounts where status!='closed'")]

    async def refresh_accounts(self, now: float) -> list[dict]:
        if not self.ok:
            return []
        try:
            accounts = await self.snap.accounts()
        except SnapTradeError as exc:
            self.last_error = str(exc)
            return []
        for a in accounts:
            bal = None                                  # None: couldn't read it this time, keep the last value
            try:
                bal = await self.snap.balances(a["id"])
            except SnapTradeError as exc:
                self.last_error = str(exc)
            self.db.run("""insert into accounts(id, name, institution, number, margin, total, cash, buying_power, updated, status)
                           values(?,?,?,?,?,?,?,?,?, 'open') on conflict(id) do update set name=excluded.name,
                           institution=excluded.institution, number=excluded.number, margin=excluded.margin,
                           total=excluded.total, cash=coalesce(excluded.cash, accounts.cash),
                           buying_power=coalesce(excluded.buying_power, accounts.buying_power), updated=excluded.updated""",
                        (a["id"], a["name"], a["institution"], a["number"], a["margin"], a["total"],
                         num(bal.get("cash")) if bal is not None else None,
                         num(bal.get("buying_power")) if bal is not None else None, int(now)))
        conns = getattr(self.snap, "last_connections", None)
        if conns is None:
            try:
                conns = await self.snap.connections()
            except SnapTradeError:
                conns = []
        self.disabled = [c["name"] or c["id"] for c in conns if c.get("disabled")]
        if self.disabled and self.db.once(f"reconnect:{day_of(now)}", int(now)):
            await self.notify("⚠️ Your " + ", ".join(esc(x) for x in self.disabled) + " connection stopped syncing "
                              "(brokers ask you to log in again every so often). Send /connect to re-link it, read-only.",
                              kind="BROKER")
        return accounts

    async def refresh_positions(self, now: float) -> None:
        for acc in self.account_ids():
            try:
                rows = await self.snap.positions(acc)
            except SnapTradeError as exc:
                self.last_error = str(exc)
                continue
            self.db.run("delete from positions where account_id=?", (acc,))
            self._reconcile(acc, rows, now)
            for p in rows:
                self.db.run("""insert or replace into positions(account_id, key, symbol, underlying, kind, option_type,
                               strike, expiry, qty, avg_price, price, multiplier, updated) values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (acc, p["key"], p["symbol"], p["underlying"], p["kind"], p["option_type"], p["strike"],
                             p["expiry"], p["qty"], p["avg_price"], p["price"], p["multiplier"], int(now)))

    def _reconcile(self, account_id: str, positions: list[dict], now: float) -> int:
        """Trades the journal still has open from an earlier day that the broker no longer holds were closed while
        DayBot was off: close them, exit price unknown, and keep them out of the stats."""
        held = {p["key"] for p in positions}
        n = 0
        for t in self.db.rows("select * from trades where account_id=? and status='open' and day<?",
                              (account_id, day_of(now))):
            if t["key"] in held:
                continue
            self.db.run("""update trades set status='closed', closed_ts=?, open_qty=0, note='closed while DayBot was off: '
                           || 'exit price unknown' where id=?""", (int(now), t["id"]))
            self.ledger.tag(t["id"], gap=1)
            n += 1
        return n

    async def backfill(self, now: float, days: int = 90) -> int:
        """Your past trades for the journal (once). Live fills from today on come from the real-time feed."""
        if self.db.get_meta("history_done"):
            return 0
        today = et(now).date()
        start = (today - dt.timedelta(days=days)).isoformat()
        end = (today - dt.timedelta(days=1)).isoformat()
        fills = []
        for acc in self.account_ids():
            try:
                fills.extend(await self.snap.history(acc, start, end))
            except SnapTradeError as exc:
                self.last_error = str(exc)
                return 0
        live_since = self.db.get_meta("fills_live_since") or today.isoformat()
        n = 0
        for f in sorted(fills, key=lambda x: x["ts"]):
            if day_of(f["ts"]) >= live_since:
                continue
            fid = self.ledger.add_fill(f)
            if fid:
                self.ledger.apply(fid)
                self.db.run("update fills set checked=1 where id=?", (fid,))
                n += 1
        self.ledger.mark_holdings(self.market.open_ts(day_of(now)) if self.market.is_trading_day(day_of(now))
                                  else now)       # what you still hold from before today: investments
        self.db.set_meta("history_done", int(now))
        return n

    async def poll(self, now: float) -> list[dict]:
        """New fills since the last poll -> booked, checked, reported. Returns the ledger events."""
        if not self.ok:
            return []
        if not self.db.get_meta("fills_live_since"):
            self.db.set_meta("fills_live_since", day_of(now))
        if not self.account_ids():
            await self.refresh_accounts(now)
        events = []
        for acc in self.account_ids():
            try:
                fills = await self.snap.recent_fills(acc)
            except SnapTradeError as exc:
                self.last_error = str(exc)
                continue
            # the first look at an account books what's already there quietly — no checks for old fills
            primed = bool(self.db.get_meta(f"primed:{acc}"))
            for f in sorted(fills, key=lambda x: x["ts"]):
                new = self._new_part(f)
                if not new:
                    continue
                fid = self.ledger.add_fill(new)
                if not fid:
                    continue
                fresh = primed and now - num(new["ts"]) <= LATE_SECONDS
                for ev in self.ledger.apply(fid):
                    events.append(ev)
                    try:
                        if fresh:
                            await self.handle(ev, now)
                        elif primed:
                            await self.handle_late(ev, now)
                    except Exception as exc:          # a failed message never loses the fill
                        self.last_error = f"handle {type(exc).__name__}: {exc}"
                self.db.run("update fills set checked=1 where id=?", (fid,))
            if not primed:
                self.db.set_meta(f"primed:{acc}", int(now))
        self.last_poll = now
        return events

    def _new_part(self, f: dict) -> dict | None:
        """The part of an order that filled since we last looked (orders fill in pieces)."""
        if not f.get("order_id"):
            return f
        seen = self.db.row("select * from orders_seen where account_id=? and order_id=?", (f["account_id"], f["order_id"]))
        filled, value = num(f["qty"]), num(f["qty"]) * num(f["price"])
        if seen and num(seen["filled"]) >= filled - 1e-9:
            return None
        prev_q = num(seen["filled"]) if seen else 0.0
        prev_v = num(seen["value"]) if seen else 0.0
        self.db.run("insert or replace into orders_seen(account_id, order_id, filled, value, ts) values(?,?,?,?,?)",
                    (f["account_id"], f["order_id"], filled, value, int(f["ts"])))
        part_q = filled - prev_q
        part_px = (value - prev_v) / part_q if part_q > 0 else num(f["price"])
        if part_px <= 0:
            part_px = num(f["price"])
        return {**f, "qty": part_q, "price": part_px, "order_id": f"{f['order_id']}#{filled:g}" if seen else f["order_id"]}

    async def close_expired(self, now: float) -> int:
        """Options past their expiry that the live feed never closed (expirations aren't orders): close at 0."""
        today = et(now).date().isoformat()
        n = 0
        for t in self.db.rows("select * from trades where status='open' and kind='option' and expiry<? and expiry!=''",
                              (today,)):
            f = {"account_id": t["account_id"], "order_id": f"expire:{t['id']}", "ts": self.market.close_ts(t["expiry"]),
                 "symbol": t["symbol"], "underlying": t["underlying"], "kind": "option", "option_type": t["option_type"],
                 "strike": t["strike"], "expiry": t["expiry"], "side": "EXPIRE", "effect": "close",
                 "qty": num(t["open_qty"]), "price": 0.0, "multiplier": t["multiplier"], "fee": 0.0, "source": "expiry"}
            fid = self.ledger.add_fill(f)
            if fid:
                for ev in self.ledger.apply(fid):
                    if ev["type"] == "close":
                        await self.notify(f"⌛ {esc(t['underlying'])} {num(t['strike']):g}{(t['option_type'] or 'C')[0]} "
                                          f"{t['expiry']} expired — booked at $0 ({money(ev['trade']['pnl'], True)}). If it "
                                          "was in the money and exercised, the shares show up as their own trade.",
                                          silent=True, kind="TRADE", symbol=t["underlying"])
                n += 1
        return n

    async def handle_late(self, ev: dict, now: float) -> None:
        """A fill we only saw well after it happened (DayBot was off): book it, plan it, no live check."""
        t, f = ev["trade"], ev["fill"]
        if ev["type"] == "holding" or not self.checks_on(t["account_id"]):
            return
        if ev["type"] == "open":
            direction = self._option_direction(t) if t["kind"] == "option" else t["direction"]
            res = await self.checker.check({**self._request(t, f), "direction": direction, "closing": True}, now)
            self.ledger.set_plan(t["id"], res.get("plan") or {}, 0, "own", {"late_sync": 1, "flags": [],
                                                                            "bucket": bucket(t["opened_ts"])})
        elif ev["type"] == "close":
            await self._on_close(t, f, now)

    # ---- reactions ----------------------------------------------------------------------------------------------
    def checks_on(self, account_id: str) -> bool:
        return self.ledger.checks_on(account_id)

    async def handle(self, ev: dict, now: float) -> None:
        t, f = ev["trade"], ev["fill"]
        if ev["type"] == "holding_add":
            return                                    # investing: a buy into your holdings
        if ev["type"] == "holding" or (ev["type"] == "close" and is_holding(t)):
            if self.checks_on(t["account_id"]):
                await self._on_holding(t, f)
            return
        if not self.checks_on(t["account_id"]):
            return                                    # an investing account: synced, not day-trade checked
        if ev["type"] == "open":
            await self._on_open(t, f, now)
        elif ev["type"] == "add":
            await self._on_add(t, f, now)
        elif ev["type"] == "reduce":
            await self._on_reduce(t, f)
        elif ev["type"] == "close":
            await self._on_close(t, f, now)

    def _request(self, t: dict, f: dict, **extra) -> dict:
        return {"symbol": t["underlying"], "kind": t["kind"], "contract": t["symbol"] if t["kind"] == "option" else "",
                "direction": "long" if t["kind"] == "option" else t["direction"], "qty": num(f["qty"]),
                "price": num(f["price"]), "account_id": t["account_id"], "source": "fill",
                "short_option": t["kind"] == "option" and t["direction"] == "short", **extra}

    def _option_direction(self, t: dict) -> str:
        """Which way the stock has to go for this option position to make money."""
        bullish = (t["option_type"] or "C").upper().startswith("C")
        if t["direction"] == "short":
            bullish = not bullish
        return "long" if bullish else "short"

    async def _on_open(self, t: dict, f: dict, now: float) -> None:
        if not self.cfg.flag("CHECK_MY_TRADES"):
            return
        direction = self._option_direction(t) if t["kind"] == "option" else t["direction"]
        res = await self.checker.check({**self._request(t, f), "direction": direction}, now)
        check_id = self.db.insert("""insert into checks(ts, symbol, contract, side, qty, price, verdict, items, plan,
                                     trade_id, source) values(?,?,?,?,?,?,?,?,?,?, 'fill')""",
                                  (int(now), t["underlying"], t["symbol"] if t["kind"] == "option" else "", f["side"],
                                   num(f["qty"]), num(f["price"]), res["verdict"],
                                   dumps([list(i) for i in res["items"]]), dumps(res.get("plan") or {}), t["id"]))
        alert = res.get("alert")
        tags = {"verdict": WORD[res["verdict"]].lower(), "flags": res.get("flags") or [], "bucket": bucket(t["opened_ts"]),
                "setup": alert["setup"] + "_" + alert["direction"] if alert else "own"}
        if t["kind"] == "option":
            tags["dte"] = res.get("dte")
        self.ledger.set_plan(t["id"], res.get("plan") or {}, check_id, tags["setup"], tags)
        verb = "bought" if f["side"] == "BUY" else "shorted" if t["kind"] == "stock" else "sold (opened)"
        head = f"<b>🧾 You {verb} {describe_fill(f)} · {res['verdict']} {WORD[res['verdict']]}</b>"
        body = format_check(res, header=head)
        state = self.ledger.day_state(day_of(now), t["account_id"])
        body += f"\nToday: {state['trades']} trade{'s' if state['trades'] != 1 else ''}, {money(state['realized'], True)} closed"
        await self.notify(body, silent=res["verdict"] == GREEN, kind="CHECK", symbol=t["underlying"],
                          buttons=[[("📈 Chart", None, f"chart:{t['underlying']}")]])

    async def _on_add(self, t: dict, f: dict, now: float) -> None:
        if is_holding(t) or not self.cfg.flag("CHECK_MY_TRADES"):
            return                                    # adding to an investment: not a day trade
        direction = self._option_direction(t) if t["kind"] == "option" else t["direction"]
        mark = self.data.last_price(t["underlying"]) if t["kind"] == "stock" else num(f["price"])
        res = await self.checker.check({**self._request(t, f), "direction": direction, "adding": True,
                                        "avg_price": num(t["entry_avg"]), "mark": mark}, now)
        if res["verdict"] == GREEN:
            return
        reds = [it for it in res["items"] if it[0] == RED]
        lines = [f"<b>➕ You added {describe_fill(f)} · {res['verdict']} {WORD[res['verdict']]}</b>",
                 f"Position now {num(t['open_qty']):g} @ {fmt_price(t['entry_avg'])} avg"]
        lines += [f"{it[0]} {esc(it[1])}" for it in (reds or res["items"])[:4] if it[0] != GREEN]
        await self.notify("\n".join(lines), silent=not reds, kind="CHECK", symbol=t["underlying"])

    async def _on_holding(self, t: dict, f: dict) -> None:
        avg = num(t["entry_avg"])
        vs = f" — {money(t['pnl'], True)} vs your average cost {fmt_price(avg)}" if avg else ""
        await self.notify(f"💼 Sold {describe_fill(f)} — a holding from before DayBot{vs}. Not counted as a day trade.",
                          silent=True, kind="TRADE", symbol=t["underlying"])

    async def _on_reduce(self, t: dict, f: dict) -> None:
        if is_holding(t):
            return
        s = 1 if t["direction"] == "long" else -1
        locked = s * (num(f["price"]) - num(t["entry_avg"])) * num(f["qty"]) * num(t["multiplier"], 1)
        await self.notify(f"✂️ Trimmed {describe_fill(f)} — {money(locked, True)} on that part · "
                          f"{num(t['open_qty']):g} left", silent=True, kind="TRADE", symbol=t["underlying"])

    async def _on_close(self, t: dict, f: dict, now: float) -> None:
        if is_holding(t):
            await self._on_holding(t, f)
            return
        exc = await excursion(self.data, self.market, t)
        if exc:
            self.db.run("update trades set mfe=?, mae=? where id=?", (exc.get("mfe_r"), exc.get("mae_r"), t["id"]))
            self.ledger.tag(t["id"], hold_s=int(num(t["closed_ts"]) - num(t["opened_ts"])), **exc.get("tags", {}))
        t = self.db.row("select * from trades where id=?", (t["id"],))
        pnl = num(t["pnl"])
        icon = "✅" if pnl > 0 else "❌" if pnl < 0 else "➖"
        what = (f"{t['underlying']} {num(t['strike']):g}{(t['option_type'] or 'C')[0]} {t['expiry'][5:].replace('-', '/')}"
                if t["kind"] == "option" else t["symbol"])
        r = t["r_mult"]
        head = f"<b>{icon} Closed {esc(what)} {t['direction']} {money(pnl, True)} ({pct(t['pnl_pct'])})" + \
               (f" · {rmult(r)}" if r is not None else "") + "</b>"
        lines = [head, f"In {fmt_price(t['entry_avg'])} → out {fmt_price(t['exit_avg'])} · {num(t['qty']):g} · held "
                       f"{dur(num(t['closed_ts']) - num(t['opened_ts']))}"]
        if num(t["plan_stop"]):
            lines.append(f"Plan was: stop {fmt_price(t['plan_stop'])} · half {fmt_price(t['plan_t1'])} · rest "
                         f"{fmt_price(t['plan_t2'])} (on {esc(t['underlying'])})")
        if exc:
            lines.extend(exc.get("notes", []))
        state = self.ledger.day_state(day_of(now), t["account_id"])
        lines.append(f"Today: {state['closed']} closed, {money(state['realized'], True)} ({state['wins']} won)")
        account = self.checker.account_size(t["account_id"])
        loud = False
        if account and state["realized"] <= -account * self.cfg.get("DAILY_MAX_LOSS_PCT") / 100:
            lines.append(f"🛑 Daily loss limit reached ({money(state['realized'])}). Done for today — tomorrow is a "
                         "new day. Every trade from here gets flagged red.")
            loud = True
        elif pnl < 0 and state["streak"] >= self.cfg.get("LOSS_STREAK_BREAK"):
            until = clock_text(now + self.cfg.get("BREAK_MINUTES") * 60)
            lines.append(f"🛑 {state['streak']} losses in a row. Take a break until {until} — the next trade before "
                         "then gets flagged.")
            loud = True
        await self.notify("\n".join(lines), silent=not loud, kind="TRADE", symbol=t["underlying"],
                          buttons=[[("📈 Chart", None, f"chart:{t['underlying']}")]])



