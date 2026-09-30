"""Community runners: coins with no tracked whale but a real, broad crowd buying.

Candidates come from what DexScreener users are watching (new profiles, community takeovers,
boosts). A coin is only alerted when all of this holds at once:
  - market cap in range, liquidity >= $20K and >= 5% of market cap, pair 30 min – 7 days old
  - 2,000+ trades in 24h, 120+ buys in the last hour, more buyers than sellers
  - at least 2 social links (X / Telegram / website)
  - top-10 wallets hold < 30% (pools excluded)  -> community STRONG
  - not dumping (1h > -10%) and not already parabolic (1h < +120%, 5m < +25%)
  - freeze authority revoked

The old scanner lost money, so these alerts get their own measured record: each opens a
simulated copy (bought at alert, sold after RUNNER_HOLD_HOURS) shown separately in /stats.
"""
from __future__ import annotations

import asyncio
import time

from . import copies, messages
from .util import ago, esc, mc, num, pct, usd

RUNNER = "runner"          # pseudo-wallet used for runner copies
SCAN_EVERY = 60
COOLDOWN = 24 * 3600


def prefilter(info: dict, cfg, now: float | None = None) -> tuple[bool, str]:
    """Cheap checks on DexScreener data. Returns (passed, reason if not)."""
    now = now or time.time()
    mcap, liq = num(info.get("mc_usd")), num(info.get("liquidity_usd"), -1)
    buys, sells = num(info.get("buys_h1")), num(info.get("sells_h1"))
    trades = num(info.get("buys_h24")) + num(info.get("sells_h24"))
    age_h = (now - num(info.get("pair_created_ts"))) / 3600 if info.get("pair_created_ts") else -1
    checks = [
        (cfg.get("RUNNER_MIN_MC_USD") <= mcap <= cfg.get("RUNNER_MAX_MC_USD"), "market cap out of range"),
        (liq >= 20_000 and liq >= 0.05 * mcap, "liquidity too thin"),
        (0.5 <= age_h <= 7 * 24, "too new or too old"),
        (trades >= 2000, "under 2,000 trades in 24h"),
        (buys >= 120 and buys >= 1.1 * sells, "not enough buying in the last hour"),
        (int(num(info.get("socials_count"))) >= 2, "fewer than 2 social links"),
        (-10 <= num(info.get("change_h1")) <= 120 and num(info.get("change_m5")) <= 25, "dumping or already parabolic"),
    ]
    for ok, why in checks:
        if not ok:
            return False, why
    return True, ""


class RunnerScanner:
    def __init__(self, cfg, db, market, engine, whales, notify):
        self.cfg = cfg
        self.db = db
        self.market = market
        self.engine = engine
        self.whales = whales
        self.notify = notify
        self.universe: dict[str, dict] = {}   # last scan's candidates (also used by /suggest)
        self.last_scan = 0
        self.last_error = ""

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:
                self.last_error = f"{time.strftime('%H:%M:%S')} {type(exc).__name__}: {exc}"
            await asyncio.sleep(SCAN_EVERY)

    def _sent_last_hour(self, now: int) -> int:
        return int(self.db.scalar("select count(*) from alerts where kind='RUNNER' and ts>=?", (now - 3600,),
                                  default=0))

    async def tick(self, now: int | None = None) -> list[str]:
        now = int(now or time.time())
        self.last_scan = now
        mints = await self.market.discovery_lists()
        infos = await self.market.tokens(mints, max_age=50) if mints else {}
        self.universe = infos
        if not self.cfg.flag("RUNNER_ALERTS"):
            return []
        passed = [i for i in infos.values() if prefilter(i, self.cfg, now)[0]]
        passed.sort(key=lambda i: -num(i.get("buys_h1")))
        sent = []
        for info in passed:
            if self._sent_last_hour(now) >= self.cfg.get("RUNNER_MAX_PER_HOUR"):
                break
            mint = info["mint"]
            if self.db.scalar("select 1 from alerts where mint=? and ((kind='RUNNER' and ts>=?) or (kind='BUY' and ts>=?))",
                              (mint, now - COOLDOWN, now - 6 * 3600)):
                continue  # already alerted as a runner today, or a whale alert already covered it
            safety = await self.market.safety(mint)
            if not safety.get("ok") or safety.get("freeze_authority"):
                continue
            community = await self.engine.community.check(mint, info)
            if community["label"] != "STRONG":
                continue
            await self._alert(info, safety, community, now)
            sent.append(mint)
        return sent

    async def _alert(self, info: dict, safety: dict, community: dict, now: int) -> None:
        mint = info["mint"]
        alert_id = self.db.insert("""insert into alerts(ts,kind,mint,wallet,grade,mc_usd,price_usd,reasons)
            values(?,?,?,?,?,?,?,?)""", (now, "RUNNER", mint, RUNNER, "R", num(info.get("mc_usd")),
                                         num(info.get("price_usd")), community["line"]))
        copies.open_copy(self.db, whale=RUNNER, mint=mint, swap_id=0, alert_id=alert_id,
                         price=num(info["price_usd"]), mc_usd=num(info.get("mc_usd")),
                         whale_price=num(info["price_usd"]), ts=now)
        if not self.cfg.flag("ALERTS_ENABLED"):
            return
        record = self.whales.stats(RUNNER, fresh=True)
        holders = [h for h in self.whales.holders_of(mint) if h["still_in"]]
        lines = [f"<b>📈 COMMUNITY RUNNER · ${esc(info.get('symbol'))}</b>",
                 "No whale signal — a broad crowd is buying and supply is spread out.",
                 f"{mc(info.get('mc_usd'))} MC · liq {usd(info.get('liquidity_usd'))} · "
                 f"pair age {ago(info.get('pair_created_ts'))} · 1h {pct(info.get('change_h1'))}",
                 f"Last hour: {int(num(info.get('buys_h1')))} buys / {int(num(info.get('sells_h1')))} sells",
                 f"👥 {esc(community['line'])}",
                 "✅ Mint &amp; freeze authority revoked" if not safety.get("mint_authority")
                 else "⚠️ Mint authority is live — supply can be inflated"]
        if holders:
            lines.append("🐋 Your whales holding: " + ", ".join(esc(h["name"]) for h in holders[:4]))
        if record["n"]:
            lines.append(f"Runner alerts so far: {record['n']} · {record['win_rate'] * 100:.0f}% won · "
                         f"avg {pct(record['avg'])} (sold after {self.cfg.get('RUNNER_HOLD_HOURS'):g}h)")
        else:
            lines.append("Runner alerts are new — their record is being measured. Start small.")
        lines.append(f"<code>{mint}</code>")
        links = [(label, url, None) for label, url in messages.token_links(mint, info.get("pair_address", ""))]
        msg_id = await self.notify("\n".join(lines), buttons=[links, [("🔎 Find its early whales", None, f"find:{mint}")]],
                                   silent=True, mint=mint, wallet=RUNNER, kind="RUNNER")
        if msg_id:
            self.db.run("update alerts set tg_message_id=? where id=?", (msg_id, alert_id))

    def suggest_coins(self, limit: int = 3) -> list[dict]:
        """Today's biggest runners worth mining for early whales (/suggest)."""
        pool = [i for i in self.universe.values()
                if num(i.get("mc_usd")) >= 300_000 and num(i.get("liquidity_usd"), -1) >= 30_000
                and num(i.get("change_h24")) >= 100]
        pool.sort(key=lambda i: -num(i.get("change_h24")))
        return pool[:limit]
