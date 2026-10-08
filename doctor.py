"""Startup self-check: prints what's configured and what to fix. Never stops the bot."""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def line(ok, text):
    print(("  ✅ " if ok is True else "  ⚠️  " if ok is False else "  •  ") + text)


async def online_checks(cfg):
    import aiohttp

    from fomo.app import _ssl_context
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_ssl_context()), timeout=timeout) as s:
        if cfg.telegram_token:
            try:
                async with s.get(f"https://api.telegram.org/bot{cfg.telegram_token}/getMe") as r:
                    data = await r.json(content_type=None)
                ok = bool(data.get("ok"))
                line(ok, f"Telegram bot @{data['result']['username']}" if ok else
                     f"Telegram token rejected: {data.get('description')}")
            except Exception as exc:
                line(False, f"Telegram unreachable ({type(exc).__name__})")
        for i, url in enumerate(cfg.rpc_http[:2]):
            name = "Helius" if "helius" in url else "Public Solana RPC"
            try:
                async with s.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "getSlot"}) as r:
                    status, body = r.status, await r.text()
                try:
                    data = json.loads(body)
                except ValueError:
                    hint = (" — your Helius credits may be used up (check dashboard.helius.dev) or it's rate limiting you"
                            if "helius" in url else "")
                    line(False, f"{name} answered HTTP {status} with a non-JSON page: {body.strip()[:80]!r}{hint}")
                    continue
                ok = "result" in data
                line(ok, f"{name} answering" if ok else f"{name} error: {str(data.get('error'))[:120]}")
                if ok and i == 0:
                    break   # the main node works: no need to test the fallback
            except Exception as exc:
                line(False, f"{name} unreachable ({type(exc).__name__})")
        if len(cfg.rpc_http) > 1:
            print("   (if Helius fails, the bot switches to the public Solana RPC automatically — slower, but it keeps working)")
        try:
            async with s.get("https://api.dexscreener.com/tokens/v1/solana/So11111111111111111111111111111111111111112") as r:
                line(r.status == 200, "DexScreener prices" if r.status == 200 else f"DexScreener HTTP {r.status}")
        except Exception as exc:
            line(False, f"DexScreener unreachable ({type(exc).__name__})")


def main():
    print("Self-check:")
    if sys.version_info < (3, 9):
        line(False, f"Python {sys.version.split()[0]} is too old — install Python 3.9 or newer")
        return
    try:
        from fomo import config
        from fomo.db import Database
    except ImportError as exc:
        line(False, f"missing package: {exc.name} — delete the .venv folder and start again")
        return
    if not (ROOT / ".env").exists():
        line(False, "no .env file — copy .env.example to .env and fill in the Telegram lines")
    cfg = config.load()
    line(bool(cfg.telegram_token), "Telegram token set" if cfg.telegram_token else
         "TELEGRAM_BOT_TOKEN missing — alerts will only print in this window")
    line(bool(cfg.telegram_chat_id), "Telegram chat id set" if cfg.telegram_chat_id else
         "TELEGRAM_CHAT_ID missing — message your bot once and it will tell you the id")
    line(True if cfg.uses_helius else False, "Helius RPC (fast)" if cfg.uses_helius else
         "No HELIUS_API_KEY — using public RPC (slower alerts; /find is slow). Free key: helius.dev")
    line(True if cfg.my_wallets else None, f"Wallet sync on for {len(cfg.my_wallets)} wallet(s) — exact P/L"
         if cfg.my_wallets else "Wallet sync off — set MY_WALLETS=your public address for exact P/L")
    try:
        db = Database(cfg.db_path)
        whales = db.scalar("select count(*) from whales where active=1", default=0)
        line(True, f"Database {cfg.db_path} ({whales} whales followed)")
        db.close()
    except Exception as exc:
        line(False, f"Database problem at {cfg.db_path}: {exc}")
    try:
        asyncio.run(asyncio.wait_for(online_checks(cfg), timeout=25))
    except Exception as exc:
        line(False, f"online checks skipped ({type(exc).__name__})")


if __name__ == "__main__":
    main()
