"""Telegram commands. Replying to an alert gives the command that alert's coin."""
from __future__ import annotations

import asyncio
import time

from . import VERSION, messages, reports
from .config import TUNABLES
from .util import ago, dur, esc, find_address, is_address, mc, mult, num, parse_amount, pct, short, usd

HELP = f"""<b>🐋 FomoBot Whale Copy {VERSION}</b>

<b>Whales</b>
/whales — who you follow + how copying them has gone
/add WALLET name — follow a whale
/whale name — one whale's record · /analyze WALLET — check before following
/find COIN [COIN2 …] — early buyers of coins that ran (new whales)
/suggest — find new whales from today's biggest runners (more alerts)
/mute name · /unmute name · /remove name
/scout — whale picks: profitable wallets from today's runners, sent to you to follow · /scout now

<b>Coins</b>
/recent — latest whale buys · /hot — coins with 2+ whales
/runners — community coins with no whale (high activity, spread-out holders)
/coin COIN — which whales are in, entries, who's still holding

<b>Your trades</b> (reply to an alert, or add the coin)
/bought 20 · /bought 20 at 850k — you bought $20 (at that market cap)
/sold 5 · /sold 30% · /sold all — you sold (add "at 1.2m" if you like)
/positions — your coins + whales still in · /undo — undo last entry
/watch COIN 2x · /watch COIN 1.5m — ping me at a market-cap target · /watches · /unwatch N
/exits — which exit style works best + your sell-too-early / too-late habits

<b>Bot</b>
/stats — what copying has actually returned · /status — health
/settings · /set NAME VALUE · /pause · /resume
/export — sends you a review file (your data, no keys) to share for tuning

Every alert carries the exit plan: half at 2x, the rest if it falls 35% from its top or 40% below
your entry. On coins you hold the bot backs it up: 2x/3x/5x ladder, 🛡 protector after 1.5x, ✂️ at -40%."""


class Commands:
    def __init__(self, app):
        self.app = app

    # -- plumbing ---------------------------------------------------------------------------
    async def handle_update(self, update: dict) -> None:
        tg = self.app.telegram
        if "callback_query" in update:
            cq = update["callback_query"]
            if str(((cq.get("message") or {}).get("chat") or {}).get("id")) != tg.chat_id:
                return
            await self.on_button(cq)
            return
        msg = update.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id") or "")
        text = str(msg.get("text") or "").strip()
        if not text:
            return
        if msg.get("date") and int(msg["date"]) < self.app.started - 60:
            return  # sent before this bot started (e.g. to the old bot): never replay stale commands
        if not tg.chat_id:
            await tg.send(f"Your chat id is <code>{chat}</code>. Put TELEGRAM_CHAT_ID={chat} in .env and restart.",
                          chat_id=chat)
            return
        if chat != tg.chat_id:
            return  # only you can control the bot
        reply_mint = ""
        reply = msg.get("reply_to_message") or {}
        if reply.get("message_id"):
            reply_mint = str(self.app.db.scalar("select mint from tg_messages where message_id=?",
                                                (reply["message_id"],), default="") or "")
            reply_mint = reply_mint or find_address(reply.get("text") or "")
        if not text.startswith("/"):
            address = find_address(text)
            if address:  # a pasted address: show the coin or the wallet
                await self.cmd_lookup([address], reply_mint)
            return
        parts = text.split()
        name = parts[0][1:].split("@")[0].lower()
        handler = getattr(self, f"cmd_{ALIASES.get(name, name)}", None)
        if not handler:
            await self.say("Unknown command. /help lists them.")
            return
        try:
            await handler(parts[1:], reply_mint)
        except ValueError as exc:
            await self.say(f"⚠️ {esc(exc)}")

    async def say(self, text: str, buttons=None) -> int:
        return await self.app.telegram.send(text, buttons=buttons)

    async def on_button(self, cq: dict) -> None:
        tg = self.app.telegram
        action, _, arg = str(cq.get("data") or "").partition(":")
        whales = self.app.whales
        if action in {"mute", "unmute"}:
            whales.set_muted(arg, action == "mute")
            await tg.answer(cq["id"], f"{'Muted' if action == 'mute' else 'Unmuted'} {whales.name(arg)}")
        elif action == "untrack":
            whales.remove(arg)
            self.app.refresh_wallets()
            await tg.answer(cq["id"], f"Stopped following {whales.name(arg)}")
        elif action == "track":
            ok, text = whales.add(arg, source="find")
            self.app.refresh_wallets()
            await tg.answer(cq["id"], text)
        elif action == "x2":
            await tg.answer(cq["id"], "Setting a 2x target…")
            try:
                await self.cmd_watch([arg, "2x"], "")
            except ValueError as exc:
                await self.say(f"⚠️ {esc(exc)}")
        elif action == "coin":
            await tg.answer(cq["id"])
            await self.cmd_coin([arg], "")
        elif action == "analyze":
            await tg.answer(cq["id"], "Reading wallet history…")
            await self.cmd_analyze([arg], "")
        elif action == "find":
            await tg.answer(cq["id"], "Researching early buyers…")
            try:
                await self.cmd_find([arg], "")
            except ValueError as exc:
                await self.say(f"⚠️ {esc(exc)}")
        else:
            await tg.answer(cq["id"])

    async def _mint(self, args: list[str], reply_mint: str, allow_symbol: bool = True) -> str:
        address = next((a for a in args if is_address(a)), "") or find_address(" ".join(args))
        if address:
            return await self.app.market.resolve_mint(address)
        if reply_mint:
            return reply_mint
        words = [a.lstrip("$").lower() for a in args if not _is_number_word(a)]
        if allow_symbol and words:
            for p in self.app.portfolio.open_positions():
                if p["symbol"].lower() in words:
                    return p["mint"]
            row = self.app.db.row("""select a.mint from alerts a join tokens t on t.mint=a.mint
                where lower(t.symbol)=? order by a.ts desc limit 1""", (words[0],))
            if row:
                return row["mint"]
        raise ValueError("which coin? Reply to its alert, or add the contract address (or ticker you hold).")

    # -- whales -----------------------------------------------------------------------------
    async def cmd_help(self, args, reply_mint):
        await self.say(HELP)

    async def cmd_whales(self, args, reply_mint):
        board = self.app.whales.leaderboard()
        if not board:
            await self.say("You're not following any whales yet.\n/add WALLET name — or /find COIN to discover some.")
            return
        lines = [f"<b>🐋 Your whales ({len(board)})</b> — copy results, last 30 days"]
        lines += [messages.whale_line(w, i + 1) for i, w in enumerate(board)]
        lines.append("\nHOT = copying made money · NEW = under 5 copies · COLD = auto-muted")
        await self.say("\n".join(lines))

    async def cmd_add(self, args, reply_mint):
        if not args:
            raise ValueError("usage: /add WALLET name")
        address = next((find_address(a) for a in args if find_address(a)), "")
        if not address:
            raise ValueError("that doesn't include a Solana wallet address. usage: /add WALLET name")
        name = " ".join(a for a in args if find_address(a) != address)
        ok, text = self.app.whales.add(address, name)
        if ok:
            self.app.refresh_wallets()
            text += "\nYou'll get an alert the moment they buy something. /analyze to check their history."
        await self.say(("✅ " if ok else "⚠️ ") + esc(text))

    def _whale(self, args) -> dict:
        whale = self.app.whales.find(" ".join(args))
        if not whale:
            raise ValueError("no whale by that name or address. /whales lists them.")
        return whale

    async def cmd_remove(self, args, reply_mint):
        w = self._whale(args)
        self.app.whales.remove(w["address"])
        self.app.refresh_wallets()
        await self.say(f"Removed {esc(w['name'])}. History is kept; /add brings them back.")

    async def cmd_mute(self, args, reply_mint):
        w = self._whale(args)
        self.app.whales.set_muted(w["address"], True)
        await self.say(f"🔕 Muted {esc(w['name'])}. Still tracked and scored; /unmute to hear them again.")

    async def cmd_unmute(self, args, reply_mint):
        w = self._whale(args)
        self.app.whales.set_muted(w["address"], False)
        await self.say(f"🔔 {esc(w['name'])} un-muted.")

    async def cmd_whale(self, args, reply_mint):
        w = self._whale(args)
        s = self.app.whales.stats(w["address"], fresh=True)
        lines = [f"<b>🐋 {esc(w['name'])}</b> {messages.STATUS_TEXT[s['status']]}"
                 + (" 🔕 muted" if w["muted"] else " 🧊 auto-muted" if w["auto_muted"] == 1 else ""),
                 f"<code>{w['address']}</code>"]
        if s["n"]:
            lines += [f"Copies (30d): {s['n']} · {s['win_rate'] * 100:.0f}% won · avg {pct(s['avg'])} · "
                      f"median {pct(s['median'])}",
                      f"$100 on every alert → {usd(s['profit_per_100'], signed=True)} · best {pct(s['best'])} · "
                      f"worst {pct(s['worst'])}",
                      f"Hit 2x: {s['hit_2x'] * 100:.0f}% · winners dipped {s['typical_dip']:.0f}% before running"]
        else:
            lines.append("No copies measured yet — the record builds as they trade.")
        if s["open"]:
            lines.append(f"Open copies: {s['open']} (avg {pct(s['open_avg'])} right now)")
        recent = self.app.db.rows("""select * from swaps where wallet=? order by ts desc limit 8""", (w["address"],))
        if recent:
            lines.append("\n<b>Latest trades</b>")
            for t in recent:
                lines.append(f"{'🟢' if t['side'] == 'BUY' else '🔴'} {ago(t['ts'])} "
                             f"${esc(self.app.market.symbol(t['mint']))} {usd(t['usd_value'])} @ {mc(t['mc_usd'])}")
        buttons = [[("🔎 Analyze history", None, f"analyze:{w['address']}"),
                    ("🔔 Unmute" if self.app.whales.is_silenced(w) else "🔕 Mute", None,
                     f"{'unmute' if self.app.whales.is_silenced(w) else 'mute'}:{w['address']}")],
                   [("GMGN", messages.whale_link(w["address"]), None),
                    ("Solscan", f"https://solscan.io/account/{w['address']}", None)]]
        await self.say("\n".join(lines), buttons)

    async def cmd_analyze(self, args, reply_mint):
        address = find_address(" ".join(args))
        if not address:
            w = self.app.whales.find(" ".join(args))
            address = w["address"] if w else ""
        if not address:
            raise ValueError("usage: /analyze WALLET")
        msg_id = await self.say(f"🔎 Reading recent swaps of <code>{short(address)}</code>…")

        async def progress(text):
            await self.app.telegram.edit(msg_id, f"🔎 {esc(text)}…")

        r = await self.app.discovery.analyze_wallet(address, progress)
        await self.app.telegram.edit(msg_id, format_analysis(r), buttons=None if not r.get("ok") else [[
            ("➕ Follow", None, f"track:{address}"), ("GMGN", messages.whale_link(address), None)]])

    async def cmd_find(self, args, reply_mint):
        mints = [await self.app.market.resolve_mint(a) for a in args if find_address(a)]
        if not mints and reply_mint:
            mints = [reply_mint]
        if not mints:
            raise ValueError("usage: /find COIN_ADDRESS [COIN2 …] — use coins that already ran")
        if self.app.find_task and not self.app.find_task.done():
            raise ValueError("a search is already running — wait for it to finish")
        msg_id = await self.say(f"🔎 Researching {len(mints)} coin(s)… this takes a minute or two.")
        self.app.find_task = asyncio.create_task(self._run_find(mints[:4], msg_id))

    async def _run_find(self, mints, msg_id):
        last = [0.0]

        async def progress(text):
            if time.monotonic() - last[0] >= 3:
                last[0] = time.monotonic()
                await self.app.telegram.edit(msg_id, f"🔎 {esc(text)}")

        try:
            result = await self.app.run_find(mints, progress)
        except Exception as exc:
            await self.app.telegram.edit(msg_id, f"⚠️ Search failed: {esc(type(exc).__name__)} {esc(exc)}")
            return
        text, buttons = format_find(result)
        await self.app.telegram.edit(msg_id, text[:4000], buttons)

    async def cmd_suggest(self, args, reply_mint):
        runners = self.app.runners
        if not runners.universe:
            await runners.tick()
        coins = runners.suggest_coins(3)
        if not coins:
            await self.say("No coin has run 2x+ today with enough liquidity to research yet. Try again later, "
                           "or use /find on a coin you know ran.")
            return
        await self.say("🔎 Researching today's biggest runners for early whales: "
                       + ", ".join(f"${esc(c['symbol'])} ({pct(c['change_h24'])} 24h)" for c in coins))
        await self.cmd_find([c["mint"] for c in coins], "")

    async def cmd_scout(self, args, reply_mint):
        scout = self.app.scout
        if args and args[0].lower() == "now":
            if scout.running:
                raise ValueError("the autopilot is already running")
            await self.say("🔎 Looking for whale picks — this takes a few minutes. You'll get them with Follow buttons.")
            self.app.find_task = asyncio.create_task(self._run_scout())
            return
        st = scout.status()
        s = st["last_summary"]
        mode = "auto-follows them" if st["auto_follow"] else "sends them to you to follow"
        lines = [f"<b>🔎 Whale picks</b> — {'ON' if st['enabled'] else 'OFF (/set WHALE_PICKS on)'}, {mode}",
                 f"Every {self.app.cfg.get('AUTO_SCOUT_HOURS'):g}h it researches today's biggest runners and picks wallets "
                 "profitable right now (6+ trades, +1 SOL, 45%+ won, active in 2 days, no snipers/bots).",
                 f"Last run: {ago(st['last_run']) + ' ago' if st['last_run'] else 'not yet'}"
                 + (f" · researched {len(s.get('coins', []))} coins, checked {s.get('checked', 0)} wallets, "
                    f"picked {s.get('picked', s.get('followed', 0))}" if s else ""),
                 "/scout now runs it right away."]
        recent = [c for c in st["candidates"] if c["status"] in ("picked", "followed")][:6]
        for c in recent:
            lines.append(f"• <code>{short(c['address'])}</code> {esc(c['reason'])}")
        await self.say("\n".join(lines))

    async def _run_scout(self):
        try:
            s = await self.app.scout.scout()
            await self.say(f"🔎 Done: researched {len(s['coins'])} coins, checked {s['checked']} wallets, "
                           f"picked {s.get('picked', 0) + s['followed']}." + ("" if s["coins"] else " No fresh runners to research right now."))
        except Exception as exc:
            await self.say(f"⚠️ Scout failed: {esc(type(exc).__name__)} {esc(exc)}")

    async def cmd_exits(self, args, reply_mint):
        await self.say(format_exits(self.app))

    async def cmd_watch(self, args, reply_mint):
        """/watch COIN 2x | 1.5m | at 1.5m — tell me when the market cap gets there."""
        target_text = next((a for a in args if a.lower().endswith("x") and parse_amount(a[:-1]) > 0), "")
        mc_text = next((a for a in args if not find_address(a) and a.lower() not in {"at", "@"}
                        and parse_amount(a.lstrip("@")) >= 1000), "")
        if not (target_text or mc_text):
            raise ValueError("usage: /watch COIN 2x  or  /watch COIN 1.5m (reply to an alert to skip COIN)")
        mint = await self._mint([a for a in args if a not in (target_text, mc_text)], reply_mint)
        info = await self.app.market.token(mint, max_age=15)
        now_mc = num(info.get("mc_usd"))
        if now_mc <= 0:
            raise ValueError("no live market cap for this coin yet — try again in a minute")
        target = now_mc * parse_amount(target_text[:-1]) if target_text else parse_amount(mc_text.lstrip("@"))
        direction = "up" if target >= now_mc else "down"
        self.app.db.insert("insert into watches(mint,target_mc,base_mc,direction,created_ts) values(?,?,?,?,?)",
                           (mint, target, now_mc, direction, int(time.time())))
        await self.say(f"🎯 Watching ${esc(info.get('symbol', '?'))}: I'll ping you when it "
                       f"{'reaches' if direction == 'up' else 'drops to'} {mc(target)} MC (now {mc(now_mc)}).\n"
                       "/watches lists them · /unwatch N removes one")

    async def cmd_watches(self, args, reply_mint):
        rows = self.app.db.rows("select * from watches where hit_ts=0 order by created_ts")
        if not rows:
            await self.say("No targets set. /watch COIN 2x")
            return
        lines = ["<b>🎯 Your targets</b>"]
        for r in rows:
            info = self.app.market.cached(r["mint"])
            lines.append(f"{r['id']}. ${esc(info.get('symbol') or r['mint'][:4])} → {mc(r['target_mc'])} "
                         f"(now {mc(info.get('mc_usd'))})")
        await self.say("\n".join(lines))

    async def cmd_unwatch(self, args, reply_mint):
        n = int(parse_amount(args[0])) if args else 0
        if not n or not self.app.db.run("delete from watches where id=?", (n,)):
            raise ValueError("usage: /unwatch N — /watches shows the numbers")
        await self.say(f"Removed target {n}.")

    async def cmd_runners(self, args, reply_mint):
        rows = self.app.db.rows("select * from alerts where kind='RUNNER' order by ts desc limit 10")
        record = self.app.whales.stats("runner", fresh=True)
        lines = ["<b>📈 Community runners</b> — no whale, broad crowd buying",
                 (f"Record: {record['n']} · {record['win_rate'] * 100:.0f}% won · avg {pct(record['avg'])} "
                  f"(sold after {self.app.cfg.get('RUNNER_HOLD_HOURS'):g}h)") if record["n"]
                 else "Record: still being measured"]
        for r in rows:
            info = self.app.market.cached(r["mint"])
            now_mc = num(info.get("mc_usd"))
            change = f" → {mc(now_mc)} ({pct((now_mc / r['mc_usd'] - 1) * 100)})" if now_mc and r["mc_usd"] else ""
            lines.append(f"{ago(r['ts'])} · <b>${esc(info.get('symbol') or r['mint'][:4])}</b> @ {mc(r['mc_usd'])}{change}")
        if not rows:
            lines.append("None yet. " + ("Scanner is on." if self.app.cfg.flag("RUNNER_ALERTS")
                                         else "Turn on with /set RUNNER_ALERTS on"))
        await self.say("\n".join(lines))

    # -- coins -------------------------------------------------------------------------------
    async def cmd_lookup(self, args, reply_mint):
        address = args[0]
        if self.app.whales.get(address) or (not self.app.db.scalar("select 1 from swaps where mint=?", (address,))
                                            and await self._is_wallet(address)):
            w = self.app.whales.get(address)
            if w and w["active"]:
                await self.cmd_whale([address], "")
            else:
                await self.say(f"Wallet <code>{address}</code>", [[("🔎 Analyze", None, f"analyze:{address}"),
                                                                   ("➕ Follow", None, f"track:{address}")]])
            return
        await self.cmd_coin([address], reply_mint)

    async def _is_wallet(self, address: str) -> bool:
        info = await self.app.rpc.mint_info(address)
        return info is None and not await self.app.market.token(address, max_age=300)

    async def cmd_coin(self, args, reply_mint):
        mint = await self._mint(args, reply_mint)
        await self.app.market.token(mint, max_age=15)
        r = reports.coin_report(self.app.db, self.app.market, self.app.whales, self.app.portfolio, mint)
        info = r["info"]
        lines = [f"<b>${esc(info.get('symbol') or mint[:4])}</b> · {mc(info.get('mc_usd'))} MC"
                 + (f" · liq {usd(info['liquidity_usd'])}" if num(info.get("liquidity_usd"), -1) >= 0 else "")]
        if r["holders"]:
            lines.append(f"<b>Tracked whales ({len(r['holders'])})</b>")
            for h in r["holders"]:
                state = f"holding {h['holding_pct']:.0f}%" if h["still_in"] else "sold out"
                lines.append(f"• {esc(h['name'])} in @ {mc(h['entry_mc'])} ({mult(h['x'])}) — {state} · "
                             f"bought {usd(h['bought_usd'])}, sold {usd(h['sold_usd'])}")
        else:
            lines.append("None of your whales have traded this coin.")
        p = r["position"]
        if p and p["open"]:
            lines.append(f"\nYou: {usd(p['value'])} · {usd(p['unrealized'], signed=True)} · in @ {mc(p['entry_mc'])}")
        lines.append(f"<code>{mint}</code>")
        links = [(label, url, None) for label, url in messages.token_links(mint, info.get("pair_address", ""))]
        await self.say("\n".join(lines), [links, [("🔎 Find its early whales", None, f"find:{mint}")]])

    async def cmd_recent(self, args, reply_mint):
        rows = reports.recent_buys(self.app.db, self.app.market, self.app.whales, limit=12)
        if not rows:
            await self.say("No whale buys in the last 48h.")
            return
        lines = ["<b>🕐 Latest whale buys</b>"]
        for r in rows:
            change = f" → {mc(r['now_mc'])} ({pct(r['change'])})" if r["change"] is not None else ""
            lines.append(f"{r['grade']} · {ago(r.get('trade_ts') or r['ts'])} · {esc(r['whale'])} → <b>${esc(r['symbol'])}</b> "
                         f"@ {mc(r['entry_mc'])}{change}" + (f" · 🐋×{r['confluence']}" if r["confluence"] > 1 else ""))
        await self.say("\n".join(lines))

    async def cmd_hot(self, args, reply_mint):
        rows = reports.hot_coins(self.app.db, self.app.market, self.app.whales)
        if not rows:
            await self.say("No coin has 2+ of your whales in the last 24h.")
            return
        lines = ["<b>🔥 Coins with 2+ of your whales (24h)</b>"]
        for i, r in enumerate(rows, 1):
            lines.append(f"{i}. <b>${esc(r['symbol'])}</b> · {r['whales']} whales ({r['holding']} holding) · "
                         f"first in {mc(r['first_mc'])} → {mc(r['now_mc'])} ({mult(r['x'])})\n"
                         f"   {esc(', '.join(r['names']))}")
        await self.say("\n".join(lines))

    # -- your trades --------------------------------------------------------------------------
    async def cmd_bought(self, args, reply_mint):
        usd_amount, at_mc = _amount_and_mc(args)
        if usd_amount <= 0:
            raise ValueError("usage: /bought 20 (at 850k) — reply to the alert or add the coin")
        mint = await self._mint(args, reply_mint)
        r = await self.app.portfolio.manual_buy(mint, usd_amount, at_mc)
        await self.say(f"📒 Bought {usd(usd_amount)} of ${esc(self.app.market.symbol(mint))} at {mc(r['mc'])} MC."
                       f"\n/positions shows it · /undo if that was a mistake")

    async def cmd_sold(self, args, reply_mint):
        fraction = 0.0
        usd_amount, at_mc = _amount_and_mc(args)
        for a in args:
            if a.lower() in {"all", "everything", "100%"}:
                fraction = 1.0
            elif a.endswith("%"):
                fraction = parse_amount(a[:-1]) / 100
        if fraction:
            usd_amount = 0.0
        elif not usd_amount:
            fraction = 1.0  # "/sold" with no amount = sold everything
        mint = await self._mint(args, reply_mint)
        r = await self.app.portfolio.manual_sell(mint, usd=usd_amount, fraction=fraction, mc_usd=at_mc)
        p = self.app.portfolio.position(mint)
        await self.say(f"📒 Sold {usd(r['usd'])} of ${esc(self.app.market.symbol(mint))} at {mc(r['mc'])} MC.\n"
                       f"Realized on this coin: {usd(p['realized'], signed=True)}"
                       + (f" · still holding {usd(p['value'])}" if p["open"] else " · position closed"))

    async def cmd_undo(self, args, reply_mint):
        row = self.app.portfolio.undo_last_manual()
        await self.say(f"↩️ Removed your last entry: {row['side'].lower()} {usd(row['usd'])} of "
                       f"${esc(self.app.market.symbol(row['mint']))}." if row else "Nothing to undo.")

    async def cmd_positions(self, args, reply_mint):
        positions = self.app.portfolio.open_positions()
        await self.app.market.tokens([p["mint"] for p in positions], max_age=20)
        positions = self.app.portfolio.open_positions()
        s = self.app.portfolio.summary()
        lines = [f"<b>📒 Your positions</b> · open {usd(s['unrealized'], signed=True)} · "
                 f"realized {usd(s['realized'], signed=True)}"]
        if not positions:
            lines.append("No open positions."
                         + ("" if self.app.cfg.my_wallets else " Set MY_WALLETS in .env to sync them automatically."))
        for p in positions:
            lines.append(messages.position_line(p, self.app.whales.holders_of(p["mint"])))
        if self.app.cfg.my_wallets:
            lines.append("\nSynced from your wallet: exact amounts incl. fees.")
        await self.say("\n".join(lines))

    # -- bot ---------------------------------------------------------------------------------
    async def cmd_stats(self, args, reply_mint):
        days = int(parse_amount(args[0])) if args and parse_amount(args[0]) else 30
        await self.say(format_stats(self.app, days))

    async def cmd_status(self, args, reply_mint):
        await self.say(format_status(self.app))

    async def cmd_settings(self, args, reply_mint):
        lines = ["<b>⚙️ Settings</b> — change with /set NAME VALUE"]
        for name, spec in TUNABLES.items():
            value = self.app.cfg.get(name)
            shown = ("on" if value >= 0.5 else "off") if spec.is_bool else f"{value:,.10g}"
            lines.append(f"<b>{name}</b> = {shown}\n   {esc(spec.help)}")
        await self.say("\n".join(lines))

    async def cmd_set(self, args, reply_mint):
        if len(args) < 2:
            raise ValueError("usage: /set NAME VALUE — /settings lists names")
        value = self.app.set_setting(args[0], args[1])
        await self.say(f"✅ {esc(args[0].upper())} = {value:,.10g}")

    async def cmd_export(self, args, reply_mint):
        """Send the review file (alerts, copies, whales, your trades, recent prices) into this chat."""
        from .export import build_review_zip
        out = self.app.cfg.db_path.parent / "FomoBot_review.zip"
        await self.say("📦 Packing your review file…")
        try:
            zipped = await asyncio.to_thread(build_review_zip, self.app.cfg.db_path, out)
        except Exception as exc:
            raise ValueError(f"export failed: {type(exc).__name__}: {exc}")
        ok = await self.app.telegram.send_document(
            zipped, caption=f"FomoBot_review.zip · {zipped.stat().st_size / 1e6:.1f} MB · no keys or tokens inside")
        if not ok:
            await self.say(f"⚠️ Couldn't upload it to Telegram ({esc(self.app.telegram.last_error)}). "
                           f"It's saved on your Mac at <code>{esc(zipped)}</code>")

    async def cmd_pause(self, args, reply_mint):
        self.app.set_setting("ALERTS_ENABLED", "0")
        await self.say("⏸ Alerts paused. Whales are still tracked and scored. /resume to turn them back on.")

    async def cmd_resume(self, args, reply_mint):
        self.app.set_setting("ALERTS_ENABLED", "1")
        await self.say("▶️ Alerts on.")


ALIASES = {"start": "help", "pos": "positions", "p": "positions", "w": "whales", "track": "add", "follow": "add",
           "unfollow": "remove", "buy": "bought", "sell": "sold", "health": "status", "leaderboard": "whales"}


def _is_number_word(text: str) -> bool:
    t = text.lower().rstrip("%")
    return t in {"at", "all", "everything"} or parse_amount(t) > 0 or t == "0"


def _amount_and_mc(args: list[str]) -> tuple[float, float]:
    """'/bought 20 at 850k' -> (20, 850000). The number after 'at' is a market cap."""
    amount = at_mc = 0.0
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a.lower() in {"at", "@"} and i + 1 < len(args):
            at_mc = parse_amount(args[i + 1])
            skip = True
        elif a.lower().startswith("@") and len(a) > 1:
            at_mc = parse_amount(a[1:])
        elif not amount and not a.endswith("%") and not is_address(a) and parse_amount(a) > 0:
            amount = parse_amount(a)
    return amount, at_mc


# -- formatting shared by commands and the daily summary -----------------------------------------

def format_analysis(r: dict) -> str:
    if not r.get("ok"):
        return f"⚠️ {esc(r.get('error', 'analysis failed'))}"
    lines = [f"<b>🔎 {r['verdict']}</b>", f"<code>{r['wallet']}</code>",
             f"{r['swaps']} swaps over {r['span_days']:.1f} days · {r['tokens']} coins · "
             f"{r['buys_per_day']:.0f} buys/day · avg buy {r['avg_buy_sol']:.2f} SOL",
             f"Round trips: {r['trips']} · {r['win_rate'] * 100:.0f}% won · P/L {r['pnl_sol']:+.2f} SOL "
             f"(≈{usd(r['pnl_sol'] * r['sol_usd'], signed=True)})",
             f"Median trade {pct(r['median_roi'])} · best {pct(r['best_roi'])} · worst {pct(r['worst_roi'])}",
             f"Median hold {dur(r['median_hold_s'])}"
             + (f" · typical entry {mc(r['median_entry_mc'])} MC" if r["median_entry_mc"] else "")]
    lines += [f"• {esc(w)}" for w in r["why"]]
    return "\n".join(lines)


def format_find(result: dict) -> tuple[str, list]:
    lines = ["<b>🔎 Early buyers of coins that ran</b>"]
    for coin in result["coins"]:
        if not coin.get("ok"):
            lines.append(f"⚠️ {coin['mint'][:6]}…: {esc(coin.get('error'))}")
            continue
        lines.append(f"<b>${esc(coin['symbol'])}</b> peak {mc(coin['peak_mc'])} · now {mc(coin['now_mc'])} · "
                     f"early = under {mc(coin['early_mc'])} · read {coin['early_trades_read']} early trades"
                     + ("" if coin["from_launch"] else " (history too long to reach launch)"))
    wallets = result["wallets"][:10]
    if not wallets:
        lines.append("No early buyers over $100 found.")
    buttons = []
    for i, c in enumerate(wallets, 1):
        held = ("holds " + f"{c['held_pct']:.0f}%" if c.get("held_pct") is not None and c["held_pct"] >= 1
                else "sold/out" if c.get("held_pct") is not None else "?")
        flags = f" ⚠️ {', '.join(c['flags'])}" if c["flags"] else ""
        repeat = f" ⭐ early in {len(c['coins'])} coins" if len(c["coins"]) > 1 else ""
        tracked = " (following)" if c["tracked"] else ""
        lines.append(f"{i}. <code>{c['wallet']}</code>{tracked}{repeat}\n"
                     f"   in {usd(c['buy_usd'])} @ {mc(c['entry_mc'])} → peak {mult(c['to_peak'])} · {held}{flags}")
        if not c["tracked"] and len(buttons) < 5:
            buttons.append([(f"➕ Follow #{i}", None, f"track:{c['wallet']}"),
                            (f"🔎 Analyze #{i}", None, f"analyze:{c['wallet']}")])
    lines.append("\nTip: analyze before following — snipers and bots are hard to copy by hand.")
    return "\n".join(lines), buttons


def format_stats(app, days: int = 30) -> str:
    r = reports.copy_report(app.db, app.cfg, days)
    a = r["all"]
    fee = app.cfg.get("COPY_FEE_PCT")
    lines = [f"<b>📊 Last {days} days</b>",
             f"<b>Copying every alert</b> (buy at alert, sell when the whale sells, {fee:g}% fee each way):"]
    if a["n"]:
        lines.append(f"{a['n']} copies · {a['win_rate'] * 100:.0f}% won · avg {pct(a['avg'])} · median "
                     f"{pct(a['median'])} · $100 each → {usd(a['per_100'], signed=True)}")
        grades = " | ".join(f"{g}: {s['n']} · {pct(s['avg'])}" for g, s in r["by_grade"].items() if s["n"])
        if grades:
            lines.append(f"By grade — {grades}")
        later = reports.grade_outcomes(app.db, min(days, 30))
        shown = [(("⚡ scalp" if g == "scalp" else g), v) for g, v in later.items() if v["n"]]
        if shown:
            lines.append("Coins 6h after the alert — " + " | ".join(
                f"{g}: {v['n']} · median {v['median_x']:.2f}x · {v['dead']} dead · {v['hit_2x']} hit 2x" for g, v in shown))
        if r["confluence"]["n"]:
            lines.append(f"2+ whales: {r['confluence']['n']} · avg {pct(r['confluence']['avg'])} vs solo "
                         f"{pct(r['solo']['avg'])}")
        if r["winners"]:
            lines.append(f"Winners dipped {r['typical_dip']:.0f}% (median) before running; "
                         f"{r['stop20_would_kill']}/{r['winners']} would have been sold by a -20% stop.")
    else:
        lines.append("No copies yet — they start with the first whale buy.")
    run = r["runners"]
    if run["n"]:
        lines.append(f"<b>Community runners</b> (no whale, sold after {app.cfg.get('RUNNER_HOLD_HOURS'):g}h): "
                     f"{run['n']} · {run['win_rate'] * 100:.0f}% won · avg {pct(run['avg'])}")
    s = app.portfolio.summary(days)
    lines.append(f"\n<b>Your trades</b>: realized {usd(s['realized'], signed=True)} · open "
                 f"{usd(s['unrealized'], signed=True)} · {s['closed']} closed coin{'' if s['closed'] == 1 else 's'} · "
                 f"{s['win_rate'] * 100:.0f}% won")
    top = [w for w in app.whales.leaderboard(days) if w["n"]][:5]
    if top:
        lines.append("<b>Whales</b>: " + " · ".join(
            f"{esc(w['name'])} {messages.STATUS_TEXT[w['status']].split()[0]} {pct(w['avg'])} ({w['n']})" for w in top))
    return "\n".join(lines)


def format_exits(app) -> str:
    from . import exits
    lab = exits.exit_lab(app.db, app.cfg)
    habits = exits.my_exit_habits(app.db)
    lines = ["<b>🚪 Exit lab</b> — your whales' last 30 days of alerts, replayed with different exit styles"]
    if lab["copies"]:
        for r in sorted(lab["rules"], key=lambda r: -r["avg"]):
            star = " ⭐" if lab["best"] and r["key"] == lab["best"]["key"] else ""
            lines.append(f"{pct(r['avg'])} avg · {r['win_rate'] * 100:.0f}% won — {esc(r['label'])}{star}")
        lines.append(f"({lab['copies']} alerts with a recorded price path)")
    else:
        lines.append("Not enough recorded price paths yet — fills in after a day or two of alerts.")
    lines.append("\n<b>Your selling habits</b>")
    if habits["measured_after"]:
        lines.append(f"After you sell, coins go another {pct(habits['median_after_gain'])} within 24h (median of "
                     f"{habits['measured_after']}); {habits['too_early']} went 50%+ higher.")
    if habits["measured_before"]:
        lines.append(f"You sell {habits['median_below_peak']:.0f}% below the best price you had (median of "
                     f"{habits['measured_before']}); {habits['too_late']} gave back 35%+.")
    lines += [f"• {esc(t)}" for t in habits["advice"]]
    lines.append(f"\nLadder nudges: {'on' if app.cfg.flag('PROFIT_LADDER') else 'off'} · profit protector: "
                 + (f"after {app.cfg.get('PROTECT_AFTER_X'):g}x, warns at -{app.cfg.get('PROTECT_TRAIL_PCT'):g}% from peak"
                    if app.cfg.get("PROTECT_AFTER_X") > 0 else "off"))
    return "\n".join(lines)


def format_status(app) -> str:
    stream, rpc = app.stream, app.rpc.health()
    last = app.engine.last_event_ts
    lines = [f"<b>{'✅' if stream and stream.connected else '⚠️'} FomoBot Whale Copy {VERSION}</b> · up "
             f"{dur(time.time() - app.started)}",
             f"Following {app.whales.count()} whales" + (" + your wallet" if app.cfg.my_wallets else "")
             + f" · stream {'connected' if stream and stream.connected else 'reconnecting'}"
             + (f" ({stream.subscribed} subscriptions)" if stream else ""),
             f"Last trade seen: {ago(last) + ' ago' if last else 'none yet'} · {app.engine.events} trades since start",
             f"RPC: {'Helius' if app.cfg.uses_helius else 'public (slow — add HELIUS_API_KEY)'} · "
             f"{rpc['calls']:,} calls · {rpc['errors']} errors",
             f"SOL {usd(num(app.db.get_meta('sol_usd')))} · alerts {'ON' if app.cfg.flag('ALERTS_ENABLED') else 'PAUSED'}"]
    funnel = reports.alert_funnel(app.db)
    if funnel["total"]:
        parts = [f"{n} {reports.FUNNEL_LABELS.get(k, k)}" for k, n in
                 sorted(funnel["counts"].items(), key=lambda kv: -kv[1])]
        lines.append(f"Whale buys seen (24h): {funnel['total']} → " + " · ".join(parts))
        if funnel["counts"].get("too_small", 0) >= 5:
            lines.append("Tip: many small whale buys skipped — /set MIN_WHALE_BUY_USD 50 to see more.")
        if funnel["counts"].get("muted", 0) >= 5:
            lines.append("Tip: lots of buys from muted whales — /whales shows who's muted.")
    else:
        lines.append("Whale buys seen (24h): none yet — more whales = more alerts (/suggest, /scout now).")
    if app.cfg.dashboard_enabled:
        lines.append(f"Dashboard: http://localhost:{app.cfg.dashboard_port}")
    errors = [e for e in (app.engine.last_error, app.tracker.last_error, rpc["last_error"],
                          stream.last_error if stream else "") if e]
    if errors:
        lines.append("Last issue: " + esc(errors[0][:160]))
    return "\n".join(lines)
