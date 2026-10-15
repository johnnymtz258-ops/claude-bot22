"""Paper autopilot: trades a pretend balance on every alert the bot sends, mechanically, so you can see
whether the system makes money before risking any.

Entry   PAPER_TRADE_USD at the alert's confirmed price plus PAPER_SLIPPAGE_PCT (what a real swap
        a few seconds later costs you), at most PAPER_MAX_OPEN coins at once.
Exits   the same plan the alerts give you:
          half at 2x · the rest if it falls PROTECT_TRAIL_PCT from its top after 1.5x · STOP_LOSS_PCT
          below entry · when the whale has sold half its bag · after 24h.
        Every sell also pays PAPER_SLIPPAGE_PCT.
A price that jumps 8x+ in one reading must be seen twice before it counts (DexScreener glitches).
"""
from __future__ import annotations

import json
import time

from . import playbook
from .util import esc, mult, num, usd

GLITCH_JUMP = 8.0
MAX_HOLD = 24 * 3600
EQUITY_EVERY = 300
DEAD_LIQ = 1_000          # under $1K of liquidity left: nothing to sell into
FROZEN_FOR = 600          # the same price for 10 minutes while the pool is (nearly) empty: liquidity was pulled


def fill(value_usd: float, liq_usd: float) -> float:
    """What selling `value_usd` worth of tokens really returns from a pool with `liq_usd` of liquidity (constant
    product: half of the liquidity is the SOL side). Unknown liquidity (-1): the value as quoted. An emptied pool
    returns next to nothing — a price printed there can't be sold at."""
    if liq_usd < 0:
        return value_usd
    side = liq_usd / 2
    return value_usd * side / (side + value_usd) if side > 0 else 0.0


class PaperTrader:
    def __init__(self, db, cfg, notify):
        self.db = db
        self.cfg = cfg
        self.notify = notify
        self._pending: dict[int, tuple[float, int]] = {}
        self._liq_seen: dict[int, int] = {}   # trade id -> first reading with the pool mostly emptied
        self._frozen: dict[int, tuple[float, int]] = {}   # trade id -> (price, since) while the price doesn't move
        self._last_equity = 0
        self.live = None   # LiveTrader: makes these trades for real when LIVE_TRADING is on

    # -- book keeping ---------------------------------------------------------------------------
    def start_balance(self) -> float:
        saved = self.db.get_meta("paper_start")
        if not saved:
            saved = str(self.cfg.get("PAPER_START_USD"))
            self.db.set_meta("paper_start", saved)
            self.db.set_meta("paper_start_ts", int(time.time()))
        return num(saved)

    def period_start(self) -> int:
        """Trades closed before this count in the history, not in the current balance (a new honest period)."""
        return int(num(self.db.get_meta("paper_period_ts", "0")))

    def new_period(self, why: str = "") -> None:
        self.db.set_meta("paper_period_ts", int(time.time()))
        self.db.set_meta("paper_start", self.cfg.get("PAPER_START_USD"))
        self.db.set_meta("paper_start_ts", int(time.time()))
        self.db.set_meta("paper_period_why", why)

    def cash(self) -> float:
        realized = num(self.db.scalar("select coalesce(sum(proceeds_usd - size_usd),0) from paper_trades "
                                      "where status='closed' and coalesce(close_ts,0)>=?", (self.period_start(),)))
        open_cost = num(self.db.scalar("select coalesce(sum(size_usd - proceeds_usd),0) from paper_trades "
                                       "where status='open'"))
        return self.start_balance() + realized - open_cost

    def open_trades(self) -> list[dict]:
        return self.db.rows("select * from paper_trades where status='open' order by open_ts")

    def summary(self, prices: dict | None = None) -> dict:
        prices = prices or {}
        open_value = 0.0
        opens = []
        for t in self.open_trades():
            price = num(prices.get(t["mint"])) or num(t["last_price"])
            value = t["remaining"] * t["tokens"] * price * (1 - self._slip())
            open_value += value
            opens.append({**t, "value": value, "multiple": price / t["entry_price"] if t["entry_price"] else 0.0})
        closed = self.db.rows("select * from paper_trades where status='closed' and coalesce(close_ts,0)>=?",
                              (self.period_start(),))
        pnl = [c["proceeds_usd"] - c["size_usd"] for c in closed]
        equity = self.cash() + open_value
        start = self.start_balance()
        return {"start": start, "equity": equity, "cash": self.cash(), "return_pct": (equity / start - 1) * 100 if start else 0,
                "open": opens, "closed": len(closed), "won": sum(p > 0 for p in pnl), "realized": sum(pnl),
                "best": max(pnl) if pnl else 0.0, "worst": min(pnl) if pnl else 0.0,
                "since_ts": int(num(self.db.get_meta("paper_start_ts", "0")))}

    def by_type(self, days: int = 7) -> list[dict]:
        """Closed paper trades over the last `days`, split by alert type: which alerts actually make the money."""
        out = []
        for kind, label, cond in (("hype", "🔥 Hype alerts", "whale='hype'"), ("whales", "🐋 Whale alerts", "whale<>'hype'")):
            rows = self.db.rows(f"""select size_usd, proceeds_usd from paper_trades where status='closed' and {cond}
                and close_ts>=? and coalesce(interrupted,0)=0""", (int(time.time()) - days * 86400,))
            pnl = [num(r["proceeds_usd"]) - num(r["size_usd"]) for r in rows]
            out.append({"kind": kind, "label": label, "n": len(pnl), "won": sum(p > 0 for p in pnl), "pnl": sum(pnl),
                        "avg": sum(pnl) / len(pnl) if pnl else 0.0})
        rows = self.db.rows("""select size_usd, proceeds_usd from paper_trades where status='closed' and close_ts>=?
            and coalesce(interrupted,0)=1""", (int(time.time()) - days * 86400,))
        pnl = [num(r["proceeds_usd"]) - num(r["size_usd"]) for r in rows]
        out.append({"kind": "offline", "label": "💤 Open while the bot was asleep", "n": len(pnl),
                    "won": sum(p > 0 for p in pnl), "pnl": sum(pnl), "avg": sum(pnl) / len(pnl) if pnl else 0.0})
        return out

    def _slip(self) -> float:
        return self.cfg.get("PAPER_SLIPPAGE_PCT") / 100

    # -- trading --------------------------------------------------------------------------------
    def open(self, *, alert_id: int, mint: str, whale: str, symbol: str, price: float, mc_usd: float,
             ts: int | None = None, info: dict | None = None, signal: dict | None = None, plan: dict | None = None) -> int:
        if not (self.cfg.flag("PAPER_TRADING") or self.cfg.flag("LIVE_TRADING")) or price <= 0:
            return 0
        if self.db.scalar("select 1 from paper_trades where mint=? and status='open'", (mint,)):
            return 0
        if len(self.open_trades()) >= int(self.cfg.get("PAPER_MAX_OPEN")):
            return 0
        size = min(self.cfg.get("PAPER_TRADE_USD"), self.cash())
        if size < 1:
            return 0
        entry = price * (1 + self._slip())
        trade_id = self.db.insert("""insert into paper_trades(alert_id,mint,whale,symbol,open_ts,entry_price,entry_mc,
                size_usd,tokens,remaining,proceeds_usd,peak_x,last_price,status,entry_liq,plan)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                  (alert_id, mint, whale, symbol, int(ts or time.time()), entry, mc_usd, size,
                                   size / entry, 1.0, 0.0, 1.0, price, "open", num((info or {}).get("liquidity_usd")),
                                   json.dumps(plan) if plan else ""))
        if trade_id and self.live and self.live.ready()[0]:
            self.live.spawn(self.live.buy(trade_id, mint, symbol, whale, info=info, signal=signal))
        return trade_id

    @staticmethod
    def _own_plan(t: dict) -> dict | None:
        try:
            plan = json.loads(t.get("plan") or "null")
        except ValueError:
            return None
        if not isinstance(plan, dict) or not plan.get("tp"):
            return None
        return {"half": bool(plan.get("half")), "follow_whale": False, "label": plan.get("label", ""),
                "tp": num(plan["tp"]), "stop": num(plan.get("stop"), 0.75), "hold": int(num(plan.get("hold"), 1800)),
                "trail": num(plan.get("trail"), 35)}

    def _accept(self, t: dict, price: float, now: int) -> float | None:
        last = num(t["last_price"])
        if price <= 0:
            return None
        # only a sudden SPIKE needs a second reading; a crash is real far more often than a glitch (rugs: the old
        # filter held them as 'glitches' while the stop and the time limit couldn't fire)
        if last > 0 and price / last >= GLITCH_JUMP:
            pending = self._pending.get(t["id"])
            if pending and 0.8 <= price / pending[0] <= 1.25 and now - pending[1] >= 15:
                self._pending.pop(t["id"], None)
                return price
            self._pending[t["id"]] = (price, now)
            return None
        self._pending.pop(t["id"], None)
        return price

    def _liquidity_pulled(self, t: dict, liq: float, now: int) -> bool:
        """Most of the pool's liquidity gone since entry (RUG_LIQUIDITY_DROP_PCT, seen on two readings 10s+ apart):
        the dev or insiders are pulling it — the price can't be sold into much longer."""
        entry = num(t.get("entry_liq"))
        if entry <= 0 or liq < 0 or liq > entry * (1 - self.cfg.get("RUG_LIQUIDITY_DROP_PCT") / 100):
            self._liq_seen.pop(t["id"], None)
            return False
        first = self._liq_seen.setdefault(t["id"], now)
        return now - first >= 10

    def _dead(self, t: dict, price: float, liq: float, now: int) -> bool:
        """The price hasn't moved for 10 minutes and the pool is (nearly) empty: nobody can trade it any more."""
        frozen = self._frozen.get(t["id"])
        if not frozen or frozen[0] != price:
            self._frozen[t["id"]] = (price, now)
            return False
        entry = num(t.get("entry_liq"))
        empty = 0 <= liq < DEAD_LIQ or (entry > 0 and 0 <= liq < 0.1 * entry)
        return empty and now - frozen[1] >= FROZEN_FOR

    def _whale_half_out(self, whale: str, mint: str, since: int) -> bool:
        peak = num(self.db.scalar("select max(max(holding_after), max(pre_holding)) from swaps where wallet=? and mint=?",
                                  (whale, mint)))
        last = self.db.row("select holding_after from swaps where wallet=? and mint=? order by ts desc, id desc limit 1",
                           (whale, mint))
        return bool(peak > 0 and last and num(last["holding_after"]) <= 0.5 * peak
                    and self.db.scalar("select 1 from swaps where wallet=? and mint=? and side='SELL' and ts>=?",
                                       (whale, mint, since - 120)))

    async def tick(self, infos: dict, now: int | None = None) -> list[tuple[dict, str, float]]:
        """Apply the exit plan to every open paper trade. Returns [(trade, what, usd)] for what happened."""
        now = int(now or time.time())
        done = []
        trail, stop = self.cfg.get("PROTECT_TRAIL_PCT"), self.cfg.get("STOP_LOSS_PCT")
        for t in self.open_trades():
            price = self._accept(t, num((infos.get(t["mint"]) or {}).get("price_usd")), now)
            if price is None:
                limit = MAX_HOLD
                if self._own_plan(t):
                    limit = self._own_plan(t)["hold"]
                elif t["whale"] == "hype":
                    from .hype import exit_plan
                    limit = exit_plan(self.cfg)["hold"]
                if now - t["open_ts"] >= limit and num(t["last_price"]) > 0:   # no usable price: the limit still applies
                    done.append(self._sell(t, 1.0, num(t["last_price"]), "time limit (no fresh price)", now))
                continue
            x = price / t["entry_price"]
            liq = num((infos.get(t["mint"]) or {}).get("liquidity_usd"), -1)
            if self._liquidity_pulled(t, liq, now) or self._dead(t, price, liq, now):
                # the pool was emptied (rug): sold for whatever the little liquidity left pays — usually nothing
                done.append(self._sell(t, 1.0, price, f"liquidity pulled — rug ({max(liq, 0):,.0f}$ left of "
                                                      f"{num(t['entry_liq']):,.0f}$)", now, liq))
                continue
            if 0 <= liq < DEAD_LIQ and x >= 3:
                continue   # a price jump printed into an empty pool: not a real price, never a take-profit
            peak = max(num(t["peak_x"]), x)
            self.db.run("update paper_trades set last_price=?, peak_x=? where id=?", (price, peak, t["id"]))
            t.update(last_price=price, peak_x=peak)
            trail = self.cfg.get("PROTECT_TRAIL_PCT")
            plan = self._own_plan(t)                             # the picked setup's exit plan (edge.py)…
            if plan is not None:
                trail = num(plan.get("trail"), trail)
            elif t["whale"] == "hype":
                from .hype import exit_plan
                plan = exit_plan(self.cfg)                       # …else the hype plan: take the first move
            else:
                plan = playbook.plan_for(self.db, t["whale"])   # …or this whale's own winning exit plan, if it has one
            if plan and not plan["half"] and x >= plan["tp"]:
                why = "hype take-profit" if t["whale"] == "hype" else "whale's playbook"
                done.append(self._sell(t, 1.0, price, f"all out at {plan['tp']:g}x ({why})", now, liq))
                continue
            half_at = plan["tp"] if plan and plan["half"] else 2.0
            if not t["half_taken"] and x >= half_at:
                done.append(self._sell(t, 0.5, price, f"half at {half_at:g}x", now, liq))
                t = self.db.row("select * from paper_trades where id=?", (t["id"],))
            stop_pct = (1 - plan["stop"]) * 100 if plan else stop
            reason = ""
            if stop_pct > 0 and x <= 1 - stop_pct / 100:
                reason = f"stop -{stop_pct:.0f}%"
            elif peak >= 1.5 and x <= peak * (1 - trail / 100):
                reason = f"fell {trail:.0f}% from its {peak:.1f}x top"
            elif plan and now - t["open_ts"] >= plan["hold"]:
                reason = (f"{plan['hold'] // 60} min limit (hype exit)" if t["whale"] == "hype"
                          else f"{plan['hold'] // 3600}h max hold (whale's playbook)")
            elif (not plan or plan["follow_whale"]) and self._whale_half_out(t["whale"], t["mint"], t["open_ts"]):
                reason = "whale sold half its bag"
            elif now - t["open_ts"] >= MAX_HOLD:
                reason = "24h max hold"
            if reason:
                done.append(self._sell(t, 1.0, price, reason, now, liq))
        self._record_equity(infos, now)
        for trade, what, amount in done:
            if self.cfg.flag("PAPER_NOTIFY"):
                await self.notify(self._note(trade, what, amount), mint=trade["mint"], silent=True, kind="PAPER")
        return done

    def _sell(self, t: dict, share_of_remaining: float, price: float, reason: str, now: int,
              liq: float = -1.0) -> tuple[dict, str, float]:
        if self.live:
            self.live.spawn(self.live.sell(t["mint"], share_of_remaining, reason))
        part = t["remaining"] * share_of_remaining
        # what a real sell gets: the quoted value, less the price impact the pool's depth allows, less slippage
        got = fill(part * t["tokens"] * price, liq) * (1 - self._slip())
        remaining = t["remaining"] - part
        proceeds = num(t["proceeds_usd"]) + got
        closed = remaining <= 1e-9
        self.db.run("""update paper_trades set remaining=?, proceeds_usd=?, half_taken=case when ? like 'half at %'
                then 1 else half_taken end, status=?, close_ts=?, close_reason=? where id=?""",
                    (0.0 if closed else remaining, proceeds, reason, "closed" if closed else "open",
                     now if closed else None, reason if closed else t.get("close_reason"), t["id"]))
        trade = self.db.row("select * from paper_trades where id=?", (t["id"],))
        return trade, reason, got

    async def close(self, trade_id: int, reason: str = "closed by you") -> dict | None:
        """Close a paper trade now at its last price (dashboard button). Live mirrors it like any paper sell."""
        t = self.db.row("select * from paper_trades where id=? and status='open'", (trade_id,))
        if not t or num(t["last_price"]) <= 0:
            return None
        trade, what, got = self._sell(t, 1.0, num(t["last_price"]), reason, int(time.time()))
        return trade

    def audit_rugs(self) -> dict:
        """One-time correction: closed paper trades whose last sale went into an emptied pool (liquidity pulled, or a
        price frozen for 10 minutes in a pool with almost no liquidity left, or a spike printed into one) are re-booked
        at what that sale could really get: nothing. The first figure stays in paper_proceeds."""
        n, before, after = 0, 0.0, 0.0
        for t in self.db.rows("""select p.*, k.liquidity_usd liq_now from paper_trades p left join tokens k on k.mint=p.mint
                where p.status='closed' and p.paper_proceeds is null"""):
            if not self._sold_into_empty_pool(t):
                continue
            last_share = 0.5 if t["half_taken"] else 1.0
            last_sale = last_share * num(t["tokens"]) * num(t["last_price"]) * (1 - self._slip())
            fixed = max(0.0, num(t["proceeds_usd"]) - last_sale)
            self.db.run("""update paper_trades set paper_proceeds=proceeds_usd, proceeds_usd=?,
                    close_reason=close_reason||' → pool emptied: not sellable (corrected)' where id=?""", (fixed, t["id"]))
            n += 1
            before += num(t["proceeds_usd"]) - num(t["size_usd"])
            after += fixed - num(t["size_usd"])
        return {"n": n, "paper_pnl": before, "real_pnl": after}

    def _sold_into_empty_pool(self, t: dict) -> bool:
        reason = str(t["close_reason"] or "")
        if reason.startswith("liquidity pulled"):
            return True
        empty_now = t["liq_now"] is not None and 0 <= num(t["liq_now"]) < DEAD_LIQ
        if not empty_now or not t["close_ts"]:
            return False
        before = [num(r["price"]) for r in self.db.rows("""select price from price_marks where mint=? and ts>=? and ts<=?
            order by ts""", (t["mint"], int(t["close_ts"]) - FROZEN_FOR, int(t["close_ts"])))]
        if len(before) >= 8 and max(before) == min(before):
            return True   # sold at a price that had been frozen for 10 minutes: the pool was already empty
        after = [num(r["price"]) for r in self.db.rows("""select price from price_marks where mint=? and ts>? and ts<=?
            order by ts""", (t["mint"], int(t["close_ts"]), int(t["close_ts"]) + FROZEN_FOR))]
        x = num(t["proceeds_usd"]) / num(t["size_usd"]) if num(t["size_usd"]) else 0
        return x > 3 and len(after) >= 5 and max(after) == min(after)   # a spike printed into an emptied pool

    def _record_equity(self, infos: dict, now: int) -> None:
        if now - self._last_equity < EQUITY_EVERY:
            return
        self._last_equity = now
        # open trades are valued at their last ACCEPTED price, so a glitch the filter rejected can't spike the curve
        self.db.run("insert or replace into paper_equity(ts,equity) values(?,?)", (now, self.summary()["equity"]))

    def _note(self, t: dict, what: str, got: float) -> str:
        x = num(t["last_price"]) / t["entry_price"] if t["entry_price"] else 0
        if t["status"] == "closed":
            pnl = t["proceeds_usd"] - t["size_usd"]
            head = f"{'🟩' if pnl >= 0 else '🟥'} Paper: closed ${esc(t['symbol'])} {usd(pnl, signed=True)}"
        else:
            head = f"💰 Paper: sold half of ${esc(t['symbol'])} at {mult(x)}"
        return (f"<b>{head}</b>\n{esc(what)} · got {usd(got)} · balance {usd(self.summary()['equity'])}"
                f"\n<code>{t['mint']}</code>")
