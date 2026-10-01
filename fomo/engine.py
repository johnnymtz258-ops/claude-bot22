"""What happens when a watched wallet trades.

A whale BUY  -> graded alert (A/B/C) + a simulated copy that scores the whale.
A whale SELL -> the matching copy sells the same share; you get told if you hold the coin.
Your BUY/SELL -> recorded exactly in your positions.
"""
from __future__ import annotations

import asyncio
import statistics
import time

from . import copies, exits, messages
from .community import CommunityChecker
from .market import IGNORED_MINTS
from .swaps import parse_swap
from .util import esc, mc, num, pct, usd

ALERT_MAX_AGE = 600          # a trade first seen later than this is recorded but not alerted
SELL_ALERT_COOLDOWN = 600    # at most one partial-sell message per whale+coin per 10 minutes
ALERTED_RECENTLY = 48 * 3600
THIN_LIQUIDITY_USD = 5_000


def grade_buy(*, status: str, stats: dict, confluence: int, chase: float | None, safety: dict,
              rug: dict | None, liquidity: float, usd_value: float, usual_usd: float,
              late_chase_pct: float, community: dict | None = None) -> tuple[str, list]:
    """Grade a whale buy. Returns ('A'|'B'|'C'|'SKIP', [(ok, reason), ...]).

    ok=True is a plus, ok=False a warning, ok=None neutral information.
    """
    points = {"HOT": 2, "OK": 1, "NEW": 0, "WEAK": -1, "COLD": -2}[status]
    reasons = []
    if status == "HOT":
        reasons.append((True, f"HOT whale: copying them averaged {stats['avg']:+.0f}% over {stats['n']} buys"))
    elif status == "NEW":
        reasons.append((None, "New whale: fewer than 5 copies measured, record still building"))
    elif status in {"WEAK", "COLD"}:
        reasons.append((False, f"{status.title()} whale: copies averaged {stats['avg']:+.0f}% over {stats['n']} buys"))

    if confluence >= 3:
        points += 2
        reasons.append((True, f"{confluence} tracked whales bought this coin"))
    elif confluence == 2:
        points += 1
        reasons.append((True, "2 tracked whales bought this coin"))

    if chase is None:
        reasons.append((None, "Live price not confirmed yet — check the chart"))
    elif chase > late_chase_pct:
        points -= 2
        reasons.append((False, f"LATE: already {chase:+.0f}% above the whale's price"))
    elif chase > late_chase_pct / 2:
        points -= 1
        reasons.append((False, f"Price already {chase:+.0f}% above the whale's price"))

    if safety.get("ok"):
        if safety.get("freeze_authority"):
            return "SKIP", [(False, "Freeze authority is live — the dev can freeze your tokens")]
        if safety.get("mint_authority"):
            points -= 1
            reasons.append((False, "Mint authority is live — supply can be inflated"))
        else:
            reasons.append((True, "Mint & freeze authority revoked"))
    if rug and rug.get("danger"):
        points -= 1
        reasons.append((False, "RugCheck: " + ", ".join(rug["danger"][:2])))
    if 0 <= liquidity < THIN_LIQUIDITY_USD:
        points -= 1
        reasons.append((False, f"Thin liquidity (${liquidity:,.0f}) — big slippage"))

    if community:
        points += community["points"]
        reasons.append(({"STRONG": True, "OK": None, "EARLY": None}.get(community["label"], False), community["line"]))

    if usual_usd > 0 and usd_value >= 3 * usual_usd:
        points += 1
        reasons.append((True, f"Big buy for this whale ({usd_value / usual_usd:.1f}× their usual size)"))

    grade = "A" if points >= 2 else "B" if points >= 0 else "C"
    return grade, reasons


class Engine:
    def __init__(self, cfg, db, rpc, market, whales, portfolio, notify):
        self.cfg = cfg
        self.db = db
        self.rpc = rpc
        self.market = market
        self.whales = whales
        self.portfolio = portfolio
        self.notify = notify  # async (text, *, buttons=None, silent=False, mint="", wallet="", kind="") -> msg id
        self.my_wallets = set(cfg.my_wallets)
        self.community = CommunityChecker(rpc, market)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._inflight: set[tuple[str, str]] = set()
        self._buying: set[tuple[str, str]] = set()  # whale+coin buys being handled right now
        self.started_ts = int(time.time())
        self.last_event_ts = 0
        self.events = 0
        self.last_error = ""

    # -- intake ------------------------------------------------------------------------------
    def watched_wallets(self) -> list[str]:
        return [w["address"] for w in self.whales.active()] + sorted(self.my_wallets)

    async def enqueue(self, wallet: str, signature: str, source: str = "stream") -> None:
        key = (signature, wallet)
        if key in self._inflight or self.db.scalar("select 1 from processed where sig=? and wallet=?", key):
            return
        self._inflight.add(key)
        try:
            self.queue.put_nowait((wallet, signature, source))
        except asyncio.QueueFull:
            self._inflight.discard(key)
            self.last_error = "event queue full"

    async def worker(self) -> None:
        while True:
            wallet, signature, source = await self.queue.get()
            try:
                await self.process(wallet, signature, source)
            except Exception as exc:  # one bad transaction must never stop the bot
                self.last_error = f"{time.strftime('%H:%M:%S')} {type(exc).__name__}: {exc}"
            finally:
                self._inflight.discard((signature, wallet))
                self.queue.task_done()

    async def process(self, wallet: str, signature: str, source: str = "stream") -> None:
        tx = await self.rpc.transaction(signature)
        if not tx:
            return  # not marked processed, so the backup poller retries it later
        self.db.run("insert or ignore into processed(sig,wallet,ts) values(?,?,?)",
                    (signature, wallet, int(time.time())))
        sol_usd = await self.market.sol_usd()
        swap = parse_swap(tx, wallet, sol_usd)
        if swap and swap["mint"] not in IGNORED_MINTS:
            await self.handle_swap(wallet, signature, swap, source, sol_usd)

    # -- one parsed trade --------------------------------------------------------------------
    async def handle_swap(self, wallet: str, signature: str, swap: dict, source: str, sol_usd: float) -> None:
        is_me = wallet in self.my_wallets
        if not is_me and not (self.whales.get(wallet) or {}).get("active"):
            return  # removed while its transaction was queued
        rate = sol_usd if swap["base"] == "SOL" else 1.0
        if rate <= 0:
            self.last_error = "no SOL price; trade recorded without USD values"
        usd_value = swap["base_amount"] * rate
        price = usd_value / swap["token_amount"] if swap["token_amount"] > 0 else 0.0
        # live trades need a fresh price; history backfill can reuse a cached one (DexScreener rate limits)
        info = await self.market.token(swap["mint"], max_age=5 if source != "backfill" else 900)
        supply = await self._supply(swap["mint"], info)
        trade_mc = price * supply
        ts = swap["ts"] or int(time.time())
        swap_id = self.db.insert("""insert or ignore into swaps(sig,wallet,is_me,ts,seen_ts,side,mint,token_amount,base,
                base_amount,usd_value,price_usd,mc_usd,sell_fraction,holding_after,pre_holding,dex,source)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                 (signature, wallet, int(is_me), ts, int(time.time()), swap["side"], swap["mint"],
                                  swap["token_amount"], swap["base"], swap["base_amount"], usd_value, price, trade_mc,
                                  swap["sell_fraction"], swap["holding_after"], swap["pre_holding"], swap["dex"], source))
        if not swap_id:
            return
        self.events += 1
        self.last_event_ts = int(time.time())
        if is_me:
            self.portfolio.record_wallet_swap(wallet, signature, swap, usd_value, swap["fee_sol"] * sol_usd,
                                              trade_mc, ts)
            if swap["side"] == "BUY" and source == "stream":
                await self._rebuy_guard(swap["mint"], ts, info)
            return
        self.db.run("update whales set last_trade_ts=max(coalesce(last_trade_ts,0),?) where address=?", (ts, wallet))
        ctx = {"wallet": wallet, "swap": swap, "swap_id": swap_id, "usd_value": usd_value, "price": price,
               "trade_mc": trade_mc, "info": info, "ts": ts, "source": source}
        if swap["side"] == "SELL":
            await self.on_whale_sell(ctx)
            return
        key = (wallet, swap["mint"])
        if key in self._buying:
            return  # a second buy landing while the first is still being handled = adding to the bag
        self._buying.add(key)
        try:
            await self.on_whale_buy(ctx)
        finally:
            self._buying.discard(key)

    async def _supply(self, mint: str, info: dict) -> float:
        price, mcap = num(info.get("price_usd")), num(info.get("mc_usd"))
        if price > 0 and mcap > 0:
            return mcap / price
        safety = await self.market.safety(mint)
        return num(safety.get("supply"))

    def _confluence(self, mint: str, now_ts: int) -> list[dict]:
        since = now_ts - int(self.cfg.get("CONFLUENCE_HOURS") * 3600)
        rows = self.db.rows("""select wallet, min(ts) ts, (select mc_usd from swaps s2 where s2.wallet=s.wallet
                and s2.mint=s.mint and s2.side='BUY' order by ts limit 1) entry_mc
                from swaps s where mint=? and side='BUY' and is_me=0 and ts>=? group by wallet order by min(ts)""",
                            (mint, since))
        out: list[dict] = []
        for r in rows:
            # wallets that keep buying together are one group (bundles/bots), not separate whales
            if any(self.whales.same_group(r["wallet"], o["wallet"]) for o in out):
                continue
            out.append({"wallet": r["wallet"], "name": self.whales.name(r["wallet"]), "entry_mc": num(r["entry_mc"])})
        return out

    async def on_whale_buy(self, c: dict) -> None:
        wallet, swap, mint = c["wallet"], c["swap"], c["swap"]["mint"]
        too_small = c["usd_value"] < self.cfg.get("MIN_WHALE_BUY_USD")
        if too_small or c["trade_mc"] > self.cfg.get("MAX_ENTRY_MC_USD"):
            # dust/test buy, or the coin is past the "early" range — noted so /status can explain quiet days
            self.db.insert("insert into alerts(ts,kind,mint,wallet,swap_id,mc_usd,status) values(?,?,?,?,?,?,?)",
                           (int(time.time()), "SEEN", mint, wallet, c["swap_id"], c["trade_mc"],
                            "too_small" if too_small else "too_big"))
            return
        repeat_since = int(time.time()) - int(self.cfg.get("REPEAT_ALERT_HOURS") * 3600)
        if self.db.scalar("select 1 from alerts where wallet=? and mint=? and kind='BUY' and ts>=?",
                          (wallet, mint, repeat_since)):
            await self._maybe_add_alert(c)  # the whale is adding to a buy we already handled
            return

        whale = self.whales.get(wallet) or {}
        stats = self.whales.stats(wallet)
        info = c["info"]
        safety, rug, community = await asyncio.gather(self.market.safety(mint), self._rugcheck(mint),
                                                      self.community.check(mint, info))
        now_price = num(info.get("price_usd"))
        chase = (now_price / c["price"] - 1) * 100 if now_price > 0 and c["price"] > 0 else None
        confluence = self._confluence(mint, c["ts"])
        grade, reasons = grade_buy(
            status=stats["status"], stats=stats, confluence=len(confluence), chase=chase, safety=safety, rug=rug,
            liquidity=num(info.get("liquidity_usd"), -1), usd_value=c["usd_value"],
            usual_usd=self.whales.usual_buy_usd(wallet), late_chase_pct=self.cfg.get("LATE_CHASE_PCT"),
            community=community)
        if not swap["new_position"] and grade != "SKIP":
            reasons.append((None, "Adding to a bag they already held"))
        flip = self.whales.flip_speed(wallet)
        if flip.get("median_s") is not None and flip["median_s"] <= 600 and grade != "SKIP":
            reasons.append((False, f"Fast flipper: usually starts selling ~{max(1, round(flip['median_s'] / 60))}m after buying "
                                   f"({flip['within_2m'] * 100:.0f}% of the time within 2m) — take profit quickly"))

        alert_id = self.db.insert("""insert into alerts(ts,kind,mint,wallet,swap_id,grade,mc_usd,price_usd,chase_pct,
                confluence,reasons) values(?,?,?,?,?,?,?,?,?,?,?)""",
                                  (int(time.time()), "BUY", mint, wallet, c["swap_id"], grade, c["trade_mc"],
                                   now_price or c["price"], chase or 0.0, len(confluence),
                                   " | ".join(t for _, t in reasons)))
        latency = max(0, int(time.time()) - c["ts"])

        def status(value: str) -> None:
            self.db.run("update alerts set status=? where id=?", (value, alert_id))

        if grade == "SKIP" or latency > ALERT_MAX_AGE:
            status("unsafe" if grade == "SKIP" else "late")
            return  # unsafe coin, or seen too late for anyone to have acted on it
        # Every alert-worthy first buy is copied (muted whales too) so muted whales keep being scored.
        entry_price = now_price or c["price"]
        copies.open_copy(self.db, whale=wallet, mint=mint, swap_id=c["swap_id"], alert_id=alert_id,
                         price=entry_price, mc_usd=num(info.get("mc_usd")) or c["trade_mc"], whale_price=c["price"])
        if not self.whales.is_alerting(whale):
            status("muted")
            return
        if not self.cfg.flag("ALERTS_ENABLED"):
            status("paused")
            return
        if community["label"] == "WHALE-ONLY" and self.cfg.flag("HIDE_WHALE_ONLY"):
            status("whale_only")
            return  # a few wallets hold it and hardly anyone trades it: tracked and scored, not sent
        text = messages.buy_alert(
            symbol=info.get("symbol") or self.market.symbol(mint), mint=mint, whale_name=whale.get("name") or wallet[:6],
            whale_addr=wallet, stats=stats, grade=grade, reasons=reasons, usd_value=c["usd_value"],
            base_amount=swap["base_amount"], base=swap["base"], entry_mc=c["trade_mc"],
            now_mc=num(info.get("mc_usd")), chase=chase, confluence=confluence, latency_s=latency, info=info,
            late_detect=c["source"] != "stream", hold_line=exits.hold_plan_line(exits.hold_plan(self.db, wallet)),
            form=self.whales.recent_form(wallet))
        buttons = [[(label, url, None) for label, url in messages.token_links(mint, info.get("pair_address", ""))],
                   [(f"🔕 Mute {whale.get('name', '')[:12]}", None, f"mute:{wallet}"),
                    ("🐋 Whales in coin", None, f"coin:{mint}")]]
        silent = grade == "C" and self.cfg.flag("QUIET_LOW_GRADE")
        msg_id = await self.notify(text, buttons=buttons, silent=silent, mint=mint, wallet=wallet, kind="BUY")
        self.db.run("update alerts set tg_message_id=?, status=? where id=?",
                    (msg_id or 0, "silent" if silent else "sent", alert_id))

    def rebuy_stats(self) -> dict:
        """How your buys did when every whale in the coin had already sold, vs while one still held."""
        out = {"after": [], "during": []}
        for b in self.db.rows("select * from swaps where is_me=1 and side='BUY' and price_usd>0 order by ts desc limit 400"):
            nxt = self.db.row("""select price_usd from swaps where is_me=1 and mint=? and side='SELL' and ts>?
                order by ts limit 1""", (b["mint"], b["ts"]))
            state = self._whale_state(b["mint"], b["ts"])
            if nxt and state:
                out[state].append((num(nxt["price_usd"]) / num(b["price_usd"]) - 1) * 100)
        return {k: {"n": len(v), "median": statistics.median(v) if v else 0.0, "won": sum(x > 0 for x in v)}
                for k, v in out.items()}

    def _whale_state(self, mint: str, ts: int) -> str:
        """'during' if a tracked whale held the coin at `ts`, 'after' if all had sold, '' if none was in."""
        wallets = [r["wallet"] for r in self.db.rows(
            "select distinct wallet from swaps where mint=? and is_me=0 and side='BUY' and ts<=?", (mint, ts))]
        if not wallets:
            return ""
        for w in wallets:
            last = self.db.row("select holding_after from swaps where wallet=? and mint=? and ts<=? order by ts desc limit 1",
                               (w, mint, ts))
            if last and num(last["holding_after"]) > 0:
                return "during"
        return "after"

    async def _rebuy_guard(self, mint: str, ts: int, info: dict) -> None:
        """You bought a coin every tracked whale has already left — historically your worst trades."""
        if not self.cfg.flag("REBUY_GUARD") or self._whale_state(mint, ts) != "after":
            return
        now = int(time.time())
        if self.db.scalar("select 1 from alerts where kind='REBUY' and mint=? and ts>=?", (mint, now - 3600)):
            return
        st = self.rebuy_stats()["after"]
        history = (f" Your buys after whales left: {st['n']}, median {st['median']:+.0f}%, {st['won']} won."
                   if st["n"] >= 5 else "")
        text = (f"<b>⚠️ Every whale in ${esc(info.get('symbol') or self.market.symbol(mint))} has already sold</b>\n"
                f"You just bought after they left.{history} Consider a quick exit if it doesn't bounce.\n<code>{mint}</code>")
        msg_id = await self.notify(text, mint=mint, kind="REBUY")
        self.db.insert("insert into alerts(ts,kind,mint,wallet,tg_message_id,status) values(?,?,?,?,?,?)",
                       (now, "REBUY", mint, "", msg_id or 0, "rebuy"))

    async def _maybe_add_alert(self, c: dict) -> None:
        """A whale buying MORE of a coin you hold is a hold signal — tell you (at most every 30 min)."""
        wallet, mint = c["wallet"], c["swap"]["mint"]
        now = int(time.time())
        whale = self.whales.get(wallet) or {}
        if (not self.portfolio.holds(mint) or not self.whales.is_alerting(whale) or not self.cfg.flag("ALERTS_ENABLED")
                or now - c["ts"] > ALERT_MAX_AGE
                or self.db.scalar("select 1 from alerts where kind='ADD' and wallet=? and mint=? and ts>=?",
                                  (wallet, mint, now - 1800))):
            return
        info = c["info"]
        position = self.portfolio.position(mint, num(info.get("price_usd")) or None)
        text = (f"<b>➕ {esc(whale.get('name') or wallet[:6])} added {usd(c['usd_value'])} more · "
                f"${esc(info.get('symbol') or self.market.symbol(mint))}</b>\n"
                f"Bought more at {mc(c['trade_mc'])} MC — still building the position. You hold "
                f"{usd(position['value'])} ({pct(position['pnl_pct'])}).\n<code>{mint}</code>")
        msg_id = await self.notify(text, mint=mint, wallet=wallet, kind="ADD")
        self.db.insert("insert into alerts(ts,kind,mint,wallet,swap_id,mc_usd,tg_message_id,status) values(?,?,?,?,?,?,?,?)",
                       (now, "ADD", mint, wallet, c["swap_id"], c["trade_mc"], msg_id or 0, "add"))

    async def _rugcheck(self, mint: str):
        try:
            return await asyncio.wait_for(self.market.rugcheck(mint), timeout=3.0)
        except asyncio.TimeoutError:
            return None

    async def on_whale_sell(self, c: dict) -> None:
        wallet, swap, mint = c["wallet"], c["swap"], c["swap"]["mint"]
        info = c["info"]
        price = num(info.get("price_usd")) or c["price"]
        fraction = swap["sell_fraction"] if swap["pre_holding"] > 0 else 1.0
        if swap["holding_after"] <= 0:
            fraction = 1.0
        copy = self.db.row("select * from copies where whale=? and mint=? and status='open'", (wallet, mint))
        if copy:
            # the whale's own sale confirms the market price, so it counts as a path reading
            confirmed = c["price"] > 0 and 0.5 <= price / c["price"] <= 2.0
            copy = copies.update_path(self.db, copy, price, trusted=confirmed)
            copies.sell(self.db, copy, fraction, price, self.cfg.get("COPY_FEE_PCT"),
                        "whale exited" if fraction >= 0.98 else "whale partial sell")

        holders = self.whales.holders_of(mint)
        me = next((h for h in holders if h["wallet"] == wallet), None)
        full = fraction >= 0.9 or (me and me["holding_pct"] < 5)
        if not full and fraction * 100 < self.cfg.get("MIN_SELL_ALERT_PCT"):
            return
        if not self._should_tell_about_sell(wallet, mint, full):
            return
        whale = self.whales.get(wallet) or {}
        if not (whale.get("active") and self.cfg.flag("ALERTS_ENABLED")):
            return
        if int(time.time()) - c["ts"] > ALERT_MAX_AGE:
            return
        avg_buy = num(self.db.scalar("""select sum(usd_value)/sum(token_amount) from swaps where wallet=? and mint=?
                and side='BUY' and token_amount>0""", (wallet, mint)))
        others = [h for h in holders if h["wallet"] != wallet and h["still_in"]]
        position = self.portfolio.position(mint, price)
        text = messages.sell_alert(
            symbol=info.get("symbol") or self.market.symbol(mint), mint=mint, whale_name=whale.get("name") or wallet[:6],
            whale_addr=wallet, fraction=fraction, usd_value=c["usd_value"], exit_mc=c["trade_mc"],
            whale_multiple=c["price"] / avg_buy if avg_buy > 0 else 0.0,
            whale_entry_mc=me["entry_mc"] if me else 0.0, left_pct=me["holding_pct"] if me else 0.0,
            others_in=others, position=position, all_out=len(holders) > 1 and not others)
        buttons = [[(label, url, None) for label, url in messages.token_links(mint, info.get("pair_address", ""))]]
        msg_id = await self.notify(text, buttons=buttons, mint=mint, wallet=wallet, kind="SELL")
        self.db.insert("""insert into alerts(ts,kind,mint,wallet,swap_id,mc_usd,price_usd,tg_message_id)
            values(?,?,?,?,?,?,?,?)""", (int(time.time()), "SELL" if not full else "EXIT", mint, wallet,
                                         c["swap_id"], c["trade_mc"], price, msg_id or 0))

    def _should_tell_about_sell(self, wallet: str, mint: str, full: bool) -> bool:
        """Only coins you hold — or, without wallet sync, coins you were alerted on recently."""
        if not self.portfolio.holds(mint):
            if self.my_wallets:
                return False  # wallet sync is on and you don't hold it
            if not self.db.scalar("select 1 from alerts where mint=? and kind='BUY' and tg_message_id>0 and ts>=?",
                                  (mint, int(time.time()) - ALERTED_RECENTLY)):
                return False
        if not full and self.db.scalar("""select 1 from alerts where kind='SELL' and wallet=? and mint=? and ts>=?""",
                                       (wallet, mint, int(time.time()) - SELL_ALERT_COOLDOWN)):
            return False
        return True
