"""DayBot setup: asks for the few keys it needs and saves them in .env (the same file FomoBot uses).

Press Enter to skip any question (or to keep what's already there). Run it again any time.
Nothing here can trade: market data is read-only, your broker connection is read-only.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"

STEPS = [
    ("ALPACA_API_KEY_ID", "Alpaca API key ID", True, """
1) Market data — Alpaca (free)
   alpaca.markets → sign up → switch to Paper Trading → API Keys → Generate.
   DayBot only reads prices with it. Free plan = real-time IEX prices + full-market data 15 minutes late."""),
    ("ALPACA_API_SECRET_KEY", "Alpaca secret key", True, ""),
    ("SNAPTRADE_CLIENT_ID", "SnapTrade client ID", False, """
2) Your trades — SnapTrade (free for personal use, read-only)
   snaptrade.com → Get started → Personal → dashboard: copy the Client ID and Consumer Key.
   After DayBot starts, send /connect in Telegram to link Robinhood / Chase (read-only)."""),
    ("SNAPTRADE_CONSUMER_KEY", "SnapTrade consumer key", True, ""),
    ("DAY_TELEGRAM_BOT_TOKEN", "Telegram token for a NEW bot", True, """
3) Telegram — give DayBot its own bot (two programs can't share one for commands)
   In Telegram: @BotFather → /newbot → name it e.g. "My DayBot" → copy the token.
   Then open your new bot and press Start."""),
    ("ANTHROPIC_API_KEY", "Anthropic API key (optional)", True, """
4) Optional — Claude reads your chart screenshots, answers /ask and writes the coach's notes
   console.anthropic.com → API keys. Pay-as-you-go (a chart read costs a few cents). Needs Python 3.10+."""),
    ("FINNHUB_API_KEY", "Finnhub API key (optional)", True, """
5) Optional — earnings dates (warns before you trade or hold into earnings)
   finnhub.io → free API key."""),
    ("DAY_WATCHLIST", "Stocks to always watch (optional, e.g. NVDA,TSLA,AMD)", False, """
6) Optional — stocks you always want watched (gappers and SPY/QQQ are added every day anyway)"""),
    ("ACCOUNT_SIZE", "Account size in $ (optional — read from your broker once connected)", False, """
7) Optional — your account size and risk per trade (change them any time with /set)"""),
    ("RISK_PER_TRADE_PCT", "Most to lose on one trade, % of account (Enter = 1)", False, ""),
]


def read_env() -> list[str]:
    return ENV.read_text().splitlines() if ENV.exists() else []


def current(lines: list[str], key: str) -> str:
    for line in lines:
        if line.strip().startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def set_key(lines: list[str], key: str, value: str) -> list[str]:
    out, done = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        if not any(line.strip() == "# --- DayBot ---" for line in out):
            out += ["", "# --- DayBot ---"]
        out.append(f"{key}={value}")
    return out


def _ctx():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _get(url: str, headers: dict | None = None, timeout: float = 10):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
        return json.loads(resp.read().decode() or "null")


REQUIRED = ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "SNAPTRADE_CLIENT_ID", "SNAPTRADE_CONSUMER_KEY",
            "DAY_TELEGRAM_BOT_TOKEN")


def main() -> None:
    lines = read_env()
    first_run = not any(current(lines, k) for k in REQUIRED)
    ask_all = first_run or "--all" in sys.argv
    missing = [k for k in REQUIRED if not current(lines, k)]
    if not ask_all and not missing:
        print("📈 DayBot setup — everything DayBot needs is already saved.")
        try:
            again = input("   Change or add a key (Claude, Finnhub, watchlist…)? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            again = ""
        if again not in {"y", "yes"}:
            print("Nothing changed.")
            return
        ask_all = True
    print("📈 DayBot setup — keys are saved only in .env on this computer. Press Enter to skip / keep.\n")
    for key, label, secret, intro in STEPS:
        have = current(lines, key)
        if not ask_all and (have or key not in REQUIRED):
            continue                                    # only what's missing
        if intro and ask_all:
            print(intro)
        hint = " [saved — Enter keeps it]" if have else ""
        ask = getpass.getpass if secret else input
        try:
            value = ask(f"   {label}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nStopped — nothing more was changed.")
            break
        if value:
            lines = set_key(lines, key, value)
    ENV.write_text("\n".join(lines).strip() + "\n")
    try:
        os.chmod(ENV, 0o600)
    except OSError:
        pass
    print("\nSaved to .env. Checking the connections…\n")
    check(lines)
    if current(lines, "ANTHROPIC_API_KEY") and sys.version_info < (3, 10):
        print("\n⚠️  Claude features need Python 3.10 or newer; this is " + sys.version.split()[0] + ".")
        print("   Install Python from python.org (3.12+), then start DayBot again — everything else works meanwhile.")
    if os.environ.get("DAYBOT_FROM_LAUNCHER"):
        print("\nStarting DayBot…")
    else:
        print("\nIf DayBot is running, press Control+C in its window, then double-click start_daybot.command again.")


def check(lines: list[str]) -> None:
    key, secret = current(lines, "ALPACA_API_KEY_ID"), current(lines, "ALPACA_API_SECRET_KEY")
    if key and secret:
        try:
            _get("https://paper-api.alpaca.markets/v2/clock",
                 {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
            print("✅ Alpaca market data: keys work")
        except Exception as exc:
            print(f"❌ Alpaca: {str(exc)[:120]} — check the keys (paper-trading keys from alpaca.markets)")
    else:
        print("⚪ Alpaca: not set — DayBot can't see prices without it")
    token = current(lines, "DAY_TELEGRAM_BOT_TOKEN")
    if token:
        try:
            me = _get(f"https://api.telegram.org/bot{token}/getMe")
            print(f"✅ Telegram: @{(me or {}).get('result', {}).get('username', '?')}")
        except Exception as exc:
            print(f"❌ Telegram: {str(exc)[:120]} — check the token")
    print("✅ SnapTrade keys saved — link your broker with /connect after starting" if current(lines, "SNAPTRADE_CLIENT_ID")
          else "⚪ SnapTrade: not set — the bot can't see your trades (everything else works)")


if __name__ == "__main__":
    main()
