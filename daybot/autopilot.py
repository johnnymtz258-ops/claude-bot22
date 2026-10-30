"""Autopilot: a pretend account that takes every trade DayBot alerts you on, by itself — like FomoBot's paper
autopilot — so you can see in dollars whether following the bot makes money before you put yours in.

It trades exactly like you would if you followed every alert:
  Entry   the next minute's open after the alert, plus SLIPPAGE_PCT (spread + slippage)
  Size    RISK_PER_TRADE_PCT of its balance at the stop, at most 1/AUTOPILOT_MAX_OPEN of the balance per trade and
          never more than the cash it has free (no margin); fractional shares when a whole one doesn't fit
  Exits   half at the first target (1R) with the stop moved to breakeven, the rest at 2R, the stop, or out by 3:55pm
  Limits  AUTOPILOT_MAX_OPEN trades at a time, one per stock, nothing new in the last 30 minutes
Nothing here touches a real account. /autopilot shows it, /autopilot reset starts it over.
"""
from __future__ import annotations

import time

from .accounts import fmt_qty, size_shares
from .bars import C, T
from .clock import at, clock_text, day_of, et
from .setups import label
from .stats import clustered_mean_ci
from .util import esc, money, num, pct, price as fmt_price, rmult

LATE = 30 * 60
JUDGE_AFTER = 60            # trades before the verdict means anything (these setups lose small most of the time)

REASON = {"target": "half at the first target, the rest at the second", "stop": "stopped out",
          "breakeven": "half at the first target, the rest back at breakeven", "time": "closed before the bell",
          "trail": "closed back inside the noise band at a half-hour check", "close": "sold at 3:59pm"}


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def window(ts: float) -> str:
    t = et(ts)
    m = t.hour * 60 + t.minute
    return "first hour (9:30–10:30)" if m < 630 else ("midday (10:30–2pm)" if m < 840 else "afternoon (2–4pm)")


class Autopilot:
    def __init__(self, cfg, db, data, market, notify):
        self.cfg, self.db, self.data, self.market, self.notify = cfg, db, data, market, notify

    @property
    def on(self) -> bool:
        return self.cfg.flag("AUTOPILOT")

    def following(self) -> list[dict]:
        """The setups it takes right now: the ones that alert you, and silent 'candidates' (a probable but unproven
        edge) so evidence keeps building."""
        edge = getattr(self, "edge", None)
        if not edge:
            return []
        out = []
        for r in edge.table():
            if r["direction"] == "short" and not self.cfg.flag("SHORT_ALERTS"):
                continue
            bt = r.get("backtest") or {}
            if r["status"] in ("proven", "promising"):
                kind = r["status"]
            elif edge.is_candidate(r["setup"], r["direction"]):
                kind = "candidate"
            else:
                continue
            out.append({"label": r["label"], "kind": kind, "avg_r": num(bt.get("avg_r")), "sure": num(bt.get("p_pos"), 0.5)})
        return out

    # ---- the account --------------------------------------------------------------------------------------------
    def since(self) -> int:
        return int(num(self.db.get_meta("auto_since", "0")))

    def start(self) -> float:
        return num(self.db.get_meta("auto_start", "0")) or self.cfg.get("AUTOPILOT_START")

    def _begin(self, now: float) -> None:
        if not self.db.get_meta("auto_since"):
            self.db.set_meta("auto_since", int(now))
            self.db.set_meta("auto_start", self.cfg.get("AUTOPILOT_START"))

    def reset(self, now: float, amount: float = 0.0) -> float:
        """Start over: a new pretend balance from now on (old trades stay in the history)."""
        amount = amount if amount > 0 else self.cfg.get("AUTOPILOT_START")
        self.db.run("update paper set auto=0, auto_note='before a reset' where auto=1 and status in ('open','pending')")
        self.db.set_meta("auto_since", int(now))
        self.db.set_meta("auto_start", amount)
        return amount

    def closed(self, since: int | None = None) -> list[dict]:
        return self.db.rows("select * from paper where auto=1 and status='closed' and closed_ts>=? order by closed_ts",
                            (self.since() if since is None else since,))

    def open_rows(self) -> list[dict]:
        return self.db.rows("select * from paper where auto=1 and status in ('open','pending') order by id")

    def balance(self) -> float:
        return self.start() + sum(num(t["pnl"]) for t in self.closed())

    def holding(self) -> list[dict]:
        """Trades that use cash: filled ones, and market entries about to fill. A buy-stop that hasn't triggered
        holds nothing (it may never fill)."""
        return [t for t in self.open_rows() if t["status"] == "open" or t["entry_type"] != "stop"]

    def in_use(self, exclude: int = 0) -> float:
        used = 0.0
        for t in self.holding():
            if t["id"] != exclude:
                used += num(t["qty"]) * num(t["entry"]) * (0.5 if t["half_done"] else 1.0)
        return used

    def free_cash(self, exclude: int = 0) -> float:
        return max(0.0, self.balance() - self.in_use(exclude))

    def _size(self, entry: float, risk_ps: float, exclude: int) -> float:
        """Risk RISK_PER_TRADE_PCT at the stop, with at most an equal share of the balance in any one trade (so it
        can hold AUTOPILOT_MAX_OPEN at once) and never more than the cash it has free."""
        bal = self.balance()
        budget = bal * self.cfg.get("RISK_PER_TRADE_PCT") / 100
        cash = min(self.free_cash(exclude), bal / max(1, int(self.cfg.get("AUTOPILOT_MAX_OPEN"))))
        return size_shares(budget, risk_ps, entry, cash)["shares"]

    # ---- taking trades (called by Paper) ------------------------------------------------------------------------
    def take(self, paper_id: int, sig) -> str:
        """Decide whether the autopilot takes this alert. Returns '' when it does, else why not."""
        if not self.on:
            return "autopilot off"
        self._begin(sig.ts)
        opens = self.holding()
        slip = num(self.db.scalar("select cost from paper where id=?", (paper_id,))) / 100 \
            or self.cfg.get("SLIPPAGE_PCT") / 100
        entry = sig.entry * (1 + slip if sig.direction == "long" else 1 - slip)
        note = ""
        if sig.ts >= self.market.close_ts(day_of(sig.ts)) - LATE and sig.plan != "eod":
            note = "too late in the day"
        elif sig.entry_type == "stop":
            # a buy-stop order: whether there's room for it is decided when (if) it fills
            self.db.run("update paper set auto=1, auto_note='' where id=?", (paper_id,))
            return ""
        elif any(t["symbol"] == sig.symbol for t in opens):
            note = f"already in {sig.symbol}"
        elif len(opens) >= int(self.cfg.get("AUTOPILOT_MAX_OPEN")):
            note = f"{len(opens)} trades already open"
        else:
            qty = self._size(entry, sig.risk, paper_id)
            if qty * entry < 1:
                note = "no cash free"
            else:
                self.db.run("update paper set auto=1, qty=?, auto_note='' where id=?", (qty, paper_id))
                return ""
        self.db.run("update paper set auto_note=? where id=?", (note, paper_id))
        return note

    def size_at_fill(self, p: dict, entry: float, fill_ts: float = 0) -> float:
        """The real entry price is known: size again (the stop distance and the free cash may have changed). A stop
        entry is checked against the limits now, when it fills."""
        risk_ps = num(p["risk"]) if p.get("plan") in ("band", "eod") and num(p.get("risk")) else abs(entry - num(p["stop"]))
        note = ""
        if p.get("entry_type") == "stop":
            others = [t for t in self.open_rows() if t["status"] == "open" and t["id"] != p["id"]]
            if fill_ts and fill_ts >= self.market.close_ts(p["day"]) - LATE:
                note = "filled too late in the day"
            elif any(t["symbol"] == p["symbol"] for t in others):
                note = f"already in {p['symbol']}"
            elif len(others) >= int(self.cfg.get("AUTOPILOT_MAX_OPEN")):
                note = f"{len(others)} trades already open"
        qty = self._size(entry, risk_ps, p["id"]) if risk_ps > 0 and not note else 0.0
        if qty * entry < 1:
            self.db.run("update paper set auto=0, auto_note=? where id=?", (note or "no cash free at the entry", p["id"]))
            gauge = self.cfg.get("PAPER_ACCOUNT") * self.cfg.get("RISK_PER_TRADE_PCT") / 100
            return float(int(gauge // risk_ps)) if risk_ps > 0 else 0.0
        return qty

    # ---- telling you --------------------------------------------------------------------------------------------
    async def announce(self, events: list[dict]) -> int:
        if not self.cfg.flag("AUTOPILOT_NOTIFY"):
            return 0
        sent = 0
        for ev in events:
            t = ev["trade"]
            if not t.get("auto") or ev["type"] not in ("entry", "close"):
                continue                                  # the close says whether half was taken: 2 messages a trade
            text = self.note(ev)
            if text:
                await self.notify(text, silent=True, kind="AUTO", symbol=t["symbol"],
                                  buttons=[[("📈 Chart", None, f"chart:{t['symbol']}")]])
                sent += 1
        return sent

    def note(self, ev: dict) -> str:
        t, px = ev["trade"], num(ev.get("price"))
        sym = esc(t["symbol"])
        side = "bought" if t["direction"] == "long" else "shorted"
        qty = fmt_qty(t["qty"])
        if ev["type"] == "entry":
            plan = t.get("plan") or "standard"
            if plan == "close":
                how = f"stop {fmt_price(t['stop'])} · no target: holds to 3:55pm unless stopped"
            elif plan == "band":
                how = "out when a half-hour close is back inside the noise band or past VWAP · or 3:55pm"
            elif plan == "eod":
                how = "the day's biggest losers tend to bounce into the close · sells at 3:59pm"
            else:
                how = f"stop {fmt_price(t['stop'])} · half at {fmt_price(t['t1'])} · rest {fmt_price(t['t2'])}"
            why = " — it broke the first 5-minute high" if t.get("entry_type") == "stop" else ""
            return (f"🤖 <b>Autopilot {side} {qty} {sym}</b> @ {fmt_price(px)} ({money(num(t['qty']) * px)}){why}\n"
                    f"{esc(label(t['setup'], t['direction']))} · {how}\n"
                    f"<i>Pretend money — following the alert so you can see if it pays. /autopilot</i>")
        if ev["type"] == "half":
            s = 1 if t["direction"] == "long" else -1
            gain = s * (px - num(t["entry"])) * num(t["qty"]) / 2
            return (f"💰 Autopilot sold half {sym} at the first target, {fmt_price(px)} after costs "
                    f"({money(gain, signed=True)}) · stop moved to breakeven")
        if ev["type"] == "close":
            pnl = num(t["pnl"])
            sm = self.summary(time.time(), light=True)
            how = REASON.get(t["reason"], t["reason"])
            if t.get("half_done") and t["reason"] == "time":
                how = "half at the first target, the rest closed before the bell"
            elif t.get("plan") == "close" and t["reason"] == "time":
                how = "held to the close (3:55pm)"
            return (f"{'🟩' if pnl >= 0 else '🟥'} <b>Autopilot closed {sym} {money(pnl, signed=True)}</b> "
                    f"({rmult(t['r_mult'])}) — {esc(how)}\n"
                    f"Balance {money(sm['balance'])} ({pct(sm['return_pct'])}) · {plural(sm['n'], 'trade')}, "
                    f"{sm['won'] * 100:.0f}% won")
        return ""

    # ---- how it's going -----------------------------------------------------------------------------------------
    def _unrealized(self, t: dict) -> float:
        if t["status"] != "open":
            return 0.0
        px = self.data.last_price(t["symbol"]) or num(t["entry"])
        s = 1 if t["direction"] == "long" else -1
        q = num(t["qty"])
        if t["half_done"]:
            return s * (num(t["t1"]) - num(t["entry"])) * q / 2 + s * (px - num(t["entry"])) * q / 2
        return s * (px - num(t["entry"])) * q

    def spy_change(self, since: int, now: float) -> float | None:
        """What simply holding SPY did over the same time (the bar to beat)."""
        daily = self.data.daily("SPY") if hasattr(self.data, "daily") else []
        if not daily or not since:
            return None
        start_day = day_of(since)
        before = [b for b in daily if day_of(b[T]) < start_day]
        if not before:
            return None
        base = before[-1][C]
        last = self.data.last_price("SPY") or daily[-1][C]
        return (last / base - 1) * 100 if base else None

    def summary(self, now: float, light: bool = False) -> dict:
        rows = self.closed()
        pnls = [num(t["pnl"]) for t in rows]
        rs = [num(t["r_mult"]) for t in rows]
        start, bal = self.start(), self.balance()
        opens = [{**t, "unrealized": self._unrealized(t), "now": self.data.last_price(t["symbol"])}
                 for t in self.holding()]
        waiting = [{"symbol": t["symbol"], "entry": t["entry"], "stop": t["stop"], "direction": t["direction"],
                    "setup": t["setup"]} for t in self.open_rows() if t["status"] == "pending" and t["entry_type"] == "stop"]
        equity = bal + sum(o["unrealized"] for o in opens)
        wins, losses = sum(p for p in pnls if p > 0), -sum(p for p in pnls if p < 0)
        out = {"on": self.on, "following": self.following(), "start": start, "since": self.since(), "balance": bal, "equity": equity,
               "return_pct": (equity / start - 1) * 100 if start else 0.0, "n": len(pnls),
               "won": sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0, "pnl": sum(pnls),
               "avg_r": sum(rs) / len(rs) if rs else 0.0, "pf": wins / losses if losses else (99.0 if wins else 0.0),
               "best": max(pnls, default=0.0), "worst": min(pnls, default=0.0), "open": opens, "waiting": waiting}
        if light:
            return out
        day = day_of(now)
        today = [t for t in rows if day_of(t["closed_ts"]) == day]
        out["today"] = {"n": len(today), "pnl": sum(num(t["pnl"]) for t in today)}
        out["skipped_today"] = self.db.rows("""select symbol, setup, direction, auto_note from paper where day=?
                                               and auto=0 and auto_note!='' order by id""", (day,))
        groups: dict[str, dict] = {}
        for t in rows:
            for key, name in ((f"s:{t['setup']}_{t['direction']}", label(t["setup"], t["direction"])),
                              (f"w:{window(t['opened_ts'])}", window(t["opened_ts"]))):
                g = groups.setdefault(key, {"name": name, "kind": key[0], "n": 0, "won": 0, "pnl": 0.0, "r": 0.0})
                g["n"] += 1
                g["won"] += num(t["pnl"]) > 0
                g["pnl"] += num(t["pnl"])
                g["r"] += num(t["r_mult"])
        for g in groups.values():
            g["avg_r"] = g["r"] / g["n"]
        out["by_setup"] = sorted((g for g in groups.values() if g["kind"] == "s"), key=lambda g: -g["pnl"])
        out["by_time"] = sorted((g for g in groups.values() if g["kind"] == "w"), key=lambda g: -g["pnl"])
        curve, b = [(out["since"], start)], start
        for t in rows:
            b += num(t["pnl"])
            curve.append((int(t["closed_ts"]), round(b, 2)))
        out["curve"] = curve
        out["recent"] = list(reversed(rows[-12:]))
        out["spy_pct"] = self.spy_change(out["since"], now)
        out["verdict"] = self.verdict(out)
        return out

    def verdict(self, sm: dict) -> str:
        n = sm["n"]
        if not sm["on"]:
            return "The autopilot is off — /set AUTOPILOT on to let it follow the alerts on pretend money."
        if n == 0:
            return ("No trades yet. It takes the next setup the bot alerts on, during market hours (9:30am–4pm ET), "
                    "and tells you here when it buys and sells.")
        if n < JUDGE_AFTER:
            return (f"Too early to judge: {n} trade{'s' if n != 1 else ''}. These setups lose a little most of the time "
                    f"and win big now and then, so give it {JUDGE_AFTER}+ trades (a few weeks) before trusting the "
                    "result either way — a handful of trades is mostly luck.")
        spy = sm.get("spy_pct")
        vs = f" Holding SPY did {pct(spy)} over the same time." if spy is not None else ""
        good = [g for g in sm.get("by_setup", []) if g["n"] >= 8 and g["avg_r"] >= 0.1]
        bad = [g for g in sm.get("by_setup", []) if g["n"] >= 8 and g["avg_r"] <= -0.1]
        follow = (" Worth following: " + ", ".join(f"{g['name']} ({g['n']} trades, {money(g['pnl'], signed=True)})"
                                                    for g in good[:3]) + ".") if good else ""
        skip = (" Losing: " + ", ".join(f"{g['name']} ({money(g['pnl'], signed=True)})" for g in bad[:3])
                + " — skip those.") if bad else ""
        ci = clustered_mean_ci([(day_of(t["closed_ts"]), num(t["r_mult"])) for t in self.closed()])
        sure = f" Its average is {rmult(ci['mean'])} a trade (80% range {rmult(ci['lo'])} to {rmult(ci['hi'])})."
        if sm["pnl"] > 0 and ci["lo"] > 0:
            head = (f"✅ Following DayBot's alerts made money, and more than luck explains: {money(sm['pnl'], signed=True)} "
                    f"({pct(sm['return_pct'])}) over {n} trades.")
        elif ci["hi"] < 0:
            head = (f"🔴 Following every alert lost {money(-sm['pnl'])} over {n} trades, and that's more than bad luck. "
                    "Don't follow them blindly with real money — only the setups that work below, if any.")
        else:
            head = (f"🟡 {money(sm['pnl'], signed=True)} over {n} trades — can't be told apart from luck yet; the range "
                    "includes both a real edge and none.")
        return head + sure + vs + follow + skip

    def text(self, now: float) -> str:
        sm = self.summary(now)
        since = et(sm["since"]).strftime("%b %-d") if sm["since"] else "today"
        fol = sm.get("following") or []
        lines = [f"<b>🤖 Autopilot</b> — pretend {money(sm['start'])} account that follows every DayBot alert "
                 f"(since {since})",
                 ("Following: " + ", ".join(f"{esc(f['label'])}" + (f" (candidate: {f['avg_r']:+.2f}R, {f['sure'] * 100:.0f}% sure it's real — paper only)"
                                                                  if f["kind"] == "candidate" else "") for f in fol))
                 if fol else "Following: nothing — no setup has even a probable edge right now, so it waits.",
                 f"Balance {money(sm['equity'])} ({pct(sm['return_pct'])})"
                 + (f" · SPY {pct(sm['spy_pct'])} same time" if sm.get("spy_pct") is not None else "")]
        if sm["n"]:
            lines.append(f"{plural(sm['n'], 'trade')} · {sm['won'] * 100:.0f}% won · {sm['avg_r']:+.2f}R avg · best "
                         f"{money(sm['best'], signed=True)} · worst {money(sm['worst'], signed=True)}")
        lines.append(f"Today: {sm['today']['n']} closed, {money(sm['today']['pnl'], signed=True)} · open now: "
                     f"{len(sm['open'])}")
        if sm["waiting"]:
            lines.append("Buy-stops waiting to trigger: " + ", ".join(
                f"{esc(w['symbol'])} {fmt_price(w['entry'])}" for w in sm["waiting"][:8])
                + (f" +{len(sm['waiting']) - 8} more" if len(sm["waiting"]) > 8 else ""))
        if sm["open"]:
            lines.append("\n<b>Open</b>")
            for o in sm["open"]:
                if o["status"] == "pending":
                    lines.append(f"• {esc(o['symbol'])} — buying at the next minute's open")
                    continue
                lines.append(f"• {esc(o['symbol'])} {fmt_qty(o['qty'])} @ {fmt_price(o['entry'])} · now "
                             f"{fmt_price(o['now']) if o['now'] else '—'} ({money(o['unrealized'], signed=True)}) · stop "
                             f"{fmt_price(o['stop_now'])}")
        if sm["recent"]:
            lines.append("\n<b>Last trades</b>")
            for t in sm["recent"][:6]:
                lines.append(f"{'🟩' if num(t['pnl']) >= 0 else '🟥'} {esc(t['symbol'])} {money(t['pnl'], signed=True)} "
                             f"({rmult(t['r_mult'])}) · {esc(label(t['setup'], t['direction']))} · "
                             f"{et(t['opened_ts']).strftime('%-m/%-d')} {clock_text(t['opened_ts'])}")
        if sm["by_setup"]:
            lines.append("\n<b>By setup</b>")
            for g in sm["by_setup"]:
                icon = "✅" if g["avg_r"] >= 0.1 and g["n"] >= 8 else ("🔻" if g["avg_r"] < 0 else "⚪")
                lines.append(f"{icon} {esc(g['name'])}: {plural(g['n'], 'trade')}, {money(g['pnl'], signed=True)}, "
                             f"{g['won'] / g['n'] * 100:.0f}% won")
        if sm["skipped_today"]:
            why = {}
            for s in sm["skipped_today"]:
                why[s["auto_note"]] = why.get(s["auto_note"], 0) + 1
            lines.append("Passed on today: " + ", ".join(f"{n} ({esc(k)})" for k, n in why.items()))
        lines.append("\n" + esc(sm["verdict"]))
        lines.append("<i>/autopilot reset 1000 starts over with $1,000 · /set AUTOPILOT off stops it</i>")
        return "\n".join(lines)

    def week_line(self, now: float) -> str:
        """For Friday's weekly report."""
        if not self.on or not self.since():
            return ""
        week = self.closed(max(self.since(), int(now) - 7 * 86400))
        sm = self.summary(now, light=True)
        if not week:
            return f"🤖 Autopilot: no trades this week · balance {money(sm['equity'])} ({pct(sm['return_pct'])})"
        pnl = sum(num(t["pnl"]) for t in week)
        won = sum(1 for t in week if num(t["pnl"]) > 0)
        spy = self.spy_change(int(now) - 7 * 86400, now)
        return (f"🤖 Autopilot this week: {plural(len(week), 'trade')} ({won} won), {money(pnl, signed=True)}"
                + (f" · SPY {pct(spy)}" if spy is not None else "")
                + f" · balance {money(sm['equity'])} ({pct(sm['return_pct'])} since it started) — /autopilot")

    def day_line(self, day: str) -> str:
        """One line for the 4:10pm report."""
        if not self.on or not self.since():
            return ""
        start = at(day, "00:00")
        today = self.closed(max(self.since(), start))
        today = [t for t in today if num(t["closed_ts"]) < start + 86400]
        sm = self.summary(time.time(), light=True)
        if not today:
            return (f"🤖 Autopilot: no trades today · balance {money(sm['equity'])} ({pct(sm['return_pct'])}, "
                    f"{plural(sm['n'], 'trade')} so far)")
        pnl = sum(num(t["pnl"]) for t in today)
        won = sum(1 for t in today if num(t["pnl"]) > 0)
        return (f"🤖 Autopilot today: {plural(len(today), 'trade')} ({won} won), {money(pnl, signed=True)} · balance "
                f"{money(sm['equity'])} ({pct(sm['return_pct'])} since "
                f"{et(self.since()).strftime('%b %-d')})")
