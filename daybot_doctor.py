"""DayBot doctor: checks every connection and says exactly what to fix. Run: python3 daybot_doctor.py"""
from __future__ import annotations

import asyncio
import sys
import time

import aiohttp

from daybot import VERSION
from daybot.alpaca import Alpaca, AlpacaError
from daybot.app import _ssl_context
from daybot.config import load
from daybot.snaptrade import SnapTrade, SnapTradeError


async def main() -> int:
    cfg = load()
    problems = 0
    print(f"📈 DayBot {VERSION} doctor · Python {sys.version.split()[0]}\n")
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_ssl_context())) as session:
        # market data
        if not cfg.has_data:
            print("❌ Market data: no Alpaca keys in .env — run daybot_setup.py")
            problems += 1
        else:
            a = Alpaca(session, cfg.alpaca_key, cfg.alpaca_secret, cfg.data_feed, cfg.alpaca_paper)
            try:
                clock = await a.clock()
                bars = await a.bars(["SPY"], "1Day", time.time() - 10 * 86400, feed="sip")
                n = len(bars.get("SPY") or [])
                print(f"✅ Alpaca: market {'open' if clock.get('is_open') else 'closed'}, {n} days of SPY history · "
                      f"feed: {cfg.data_feed}")
            except AlpacaError as exc:
                print(f"❌ Alpaca: {exc} — check ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY (paper-trading keys)")
                problems += 1
        # your trades
        if not cfg.has_broker:
            print("⚪ Your trades: SnapTrade not set up (optional but it's the point) — see DAYBOT.md")
        else:
            st = SnapTrade(session, cfg.snaptrade_client_id, cfg.snaptrade_consumer_key, cfg.snaptrade_user_id,
                           cfg.snaptrade_user_secret)
            try:
                accounts = await st.accounts()
                if accounts:
                    names = ", ".join(f"{x['institution'] or x['name']} {x['number']}" for x in accounts)
                    print(f"✅ SnapTrade: {len(accounts)} account(s) — {names}")
                else:
                    print("🟡 SnapTrade: keys work, no broker linked yet — send /connect in Telegram")
            except SnapTradeError as exc:
                print(f"❌ SnapTrade: {exc} — check SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY")
                problems += 1
        # telegram
        if not cfg.telegram_token:
            print("❌ Telegram: no bot token — run daybot_setup.py (DAY_TELEGRAM_BOT_TOKEN)")
            problems += 1
        else:
            try:
                async with session.get(f"https://api.telegram.org/bot{cfg.telegram_token}/getMe",
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json(content_type=None)
                name = (data.get("result") or {}).get("username", "?") if data.get("ok") else None
                if name:
                    mode = " (sending through FomoBot's bot: commands off — set DAY_TELEGRAM_BOT_TOKEN)" \
                        if cfg.telegram_send_only else ""
                    print(f"✅ Telegram: @{name}{mode}" + ("" if cfg.telegram_chat_id else " · ⚠️ no chat id yet: "
                                                                                      "message the bot, it replies with it"))
                else:
                    print("❌ Telegram: the token was rejected")
                    problems += 1
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                print(f"❌ Telegram: {type(exc).__name__}")
                problems += 1
        # optional extras
        if cfg.anthropic_key:
            import importlib.util
            if importlib.util.find_spec("anthropic"):
                print(f"✅ Claude: key set, model {cfg.ai_model}")
            else:
                print("🟡 Claude: key set but the anthropic package isn't installed — it needs Python 3.10+ "
                      "(python.org), then start_daybot.command again")
        else:
            print("⚪ Claude (chart screenshots, /ask, coach notes): off — optional ANTHROPIC_API_KEY")
        from daybot.charts import available
        print("✅ Chart pictures: on" if available() else "🟡 Chart pictures: pip install -r requirements-day.txt")
        print("✅ Earnings dates: on" if cfg.finnhub_key else "⚪ Earnings dates: off — optional FINNHUB_API_KEY")
    print(f"\nDatabase: {cfg.db_path}")
    print("All set." if not problems else f"{problems} thing(s) to fix above.")
    return problems


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
