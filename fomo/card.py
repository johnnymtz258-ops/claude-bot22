"""The live card: one pinned Telegram message the bot keeps up to date (every LIVE_CARD_SECONDS), so you
never have to type a command to see if it's running, what it blocked, your positions and the paper balance.
"""
from __future__ import annotations

import time

from . import exits, profiles, reports
from .util import ago, esc, mc, mult, num, pct, usd

CARD_BUTTONS = [[("📒 Positions", None, "cmd:positions"), ("🐋 Whales", None, "cmd:whales"),
                 ("📊 Stats", None, "cmd:stats")],
                [("⏸ Pause alerts", None, "cmd:pause"), ("▶️ Resume", None, "cmd:resume"),
                 ("🔄 Refresh", None, "cmd:card")]]

FUNNEL_SHORT = {"sent": "sent", "silent": "sent quietly", "flipper": "flipper", "weak_whale": "weak whale",
                "chased": "already ran", "dumping": "dumping", "micro": "bonding curve", "whale_only": "whale-only",
                "late": "seen late", "muted": "muted", "unsafe": "unsafe", "too_small": "dust", "too_big": "too big",
                "paused": "paused", "scalp": "scalp"}


def build_card(app, now: float | None = None) -> str:
    now = now or time.time()
    cfg, db = app.cfg, app.db
    from . import VERSION
    stream = getattr(app, "stream", None)
    healthy = bool(stream and stream.connected)
    lines = [f"<b>{'🟢' if healthy else '🔴'} FomoBot {VERSION} · live</b>  <i>{time.strftime('%H:%M', time.localtime(now))}</i>"]
    if not cfg.flag("ALERTS_ENABLED"):
        lines.append("⏸ <b>Alerts are paused</b> — tap Resume")
    # whales by style
    styles: dict[str, int] = {}
    blocked = 0
    for w in app.whales.active():
        prof = app.engine.profiles.get(w["address"])
        styles[(prof or {}).get("style", "NEW")] = styles.get((prof or {}).get("style", "NEW"), 0) + 1
        blocked += not profiles.verdict(prof, cfg.get("MIN_COPY_SCORE"))[0]
    parts = [f"{n} {profiles.STYLE_LABEL[s].split(' ', 1)[0]}" for s, n in sorted(styles.items(),
                                                                                   key=lambda kv: -kv[1])]
    last_trade = num(getattr(app.engine, "last_event_ts", 0))
    lines.append(f"🐋 {sum(styles.values())} whales ({' '.join(parts) or 'none'})"
                 + (f" · {blocked} blocked" if blocked else "")
                 + (f" · last trade seen {ago(last_trade, now)} ago" if last_trade else ""))
    # what happened to whale buys in the last 24h
    funnel = reports.alert_funnel(db, 24)
    if funnel["total"]:
        counts = funnel["counts"]
        shown = sorted(((FUNNEL_SHORT.get(k, k), v) for k, v in counts.items()), key=lambda kv: -kv[1])
        lines.append(f"📥 24h: {funnel['total']} whale buys → " + " · ".join(f"{v} {k}" for k, v in shown[:5]))
    last = db.row("""select a.ts, a.mint, a.grade, a.wallet from alerts a where kind='BUY' and status in ('sent','silent')
        order by ts desc limit 1""")
    if last:
        sym = app.market.cached(last["mint"]).get("symbol") or last["mint"][:4]
        lines.append(f"🔔 Last alert: ${esc(sym)} {ago(last['ts'], now)} ago ({esc(app.whales.name(last['wallet']))}, "
                     f"grade {last['grade']})")
    # paper autopilot
    if cfg.flag("PAPER_TRADING"):
        s = app.engine.paper.summary()
        lines.append(f"🤖 Paper: <b>{usd(s['equity'])}</b> ({pct(s['return_pct'], 1)}) · {len(s['open'])} open · "
                     f"{s['closed']} closed, {s['won']} won")
        for t in s["open"][:4]:
            lines.append(f"   ${esc(t['symbol'])} {mult(t['multiple'])}"
                         + (" · half sold" if t["half_taken"] else ""))
    # your positions with the plan
    positions = app.portfolio.open_positions()
    if positions:
        total = sum(p["value"] for p in positions)
        lines.append(f"\n<b>📒 You hold {len(positions)} coin{'s' if len(positions) != 1 else ''} · {usd(total)}</b>")
        for p in positions[:6]:
            holders = app.whales.holders_of(p["mint"])
            c = exits.coach(db, cfg, p, holders, int(now))
            line = (f"• <b>${esc(p['symbol'])}</b> {usd(p['value'])} ({pct(p['pnl_pct'])})"
                    + (f" · {mult(c['multiple'])} on cost" if c.get("multiple") else ""))
            target = p["entry_mc"] * 2 if p.get("entry_mc") else 0
            if c.get("multiple", 0) < 2 and target:
                line += f" · 2x at {mc(target)}"
            if c.get("hint"):
                line += f"\n   ↳ {esc(c['hint'])}"
            lines.append(line)
    tracked = app.engine.coins.card_lines(limit=5) if hasattr(app.engine, "coins") else []
    if tracked:
        lines.append("\n<b>📍 Tracked coins</b>")
        lines += tracked
    gap = db.get_meta("last_gap")
    if gap:
        start, end = (int(x) for x in gap.split("-"))
        if now - end < 6 * 3600:
            lines.append(f"\n💤 Paused {ago(start, end)} earlier (Mac asleep?) — keep the lid open + charger in")
    return "\n".join(lines)


class LiveCard:
    def __init__(self, app):
        self.app = app
        self._last_text = ""
        self._last_ok = 0.0
        self._last_edit = 0.0

    def message_id(self) -> int:
        return int(num(self.app.db.get_meta("live_card_id", "0")))

    async def update(self, force: bool = False) -> int:
        app = self.app
        if not (app.cfg.flag("LIVE_CARD") and app.telegram and app.telegram.ok):
            return 0
        try:
            text = build_card(app)
        except Exception as exc:   # never let one broken section freeze the card
            from . import VERSION
            text = (f"<b>🟢 FomoBot {VERSION} · live</b>  <i>{time.strftime('%H:%M')}</i>\n"
                    f"⚠️ Part of this card failed to build: {esc(type(exc).__name__)}: {esc(str(exc)[:120])}")
            app.engine.last_error = f"live card: {type(exc).__name__}: {exc}"
        msg_id = self.message_id()
        if msg_id and time.time() - self._last_ok > 600 and self._last_ok:
            msg_id = 0   # edits have been failing for 10 minutes: post a fresh card instead
        if msg_id and not force and text == self._last_text:
            return msg_id
        if msg_id and await app.telegram.edit(msg_id, text, buttons=CARD_BUTTONS):
            self._last_text, self._last_ok = text, time.time()
            return msg_id
        msg_id = await app.telegram.send(text, buttons=CARD_BUTTONS, silent=True)
        if msg_id:
            app.db.set_meta("live_card_id", msg_id)
            await app.telegram.pin(msg_id)
            self._last_text, self._last_ok = text, time.time()
        return msg_id

    async def run(self) -> None:
        import asyncio
        await asyncio.sleep(20)
        self.app.db.set_meta("live_card_id", 0)   # every start (and new version) gets a fresh pinned card
        while True:
            try:
                await self.update()
            except Exception as exc:
                self.app.engine.last_error = f"live card: {type(exc).__name__}: {exc}"
            await asyncio.sleep(max(20, self.app.cfg.get("LIVE_CARD_SECONDS")))
