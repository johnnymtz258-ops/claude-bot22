"""Copy the settings you already entered in another bot folder into this one: FomoBot's Telegram / RPC / wallet
settings and DayBot's keys. Only what's missing here is filled in; nothing you set in this folder is overwritten.

Where it looks: the folders next to this one, and bot folders on your Desktop, in Documents and in Downloads
(a folder counts as a bot folder when it has bot.py or day.py in it).

Only the keys listed below are copied. Private keys, seed phrases and trading settings are never read or copied.
Exit code 2 means there is still no Telegram bot token for FomoBot afterwards (its launcher then asks).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FOMO_KEYS = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "HELIUS_API_KEY", "SOLANA_RPC_HTTP", "SOLANA_RPC_FALLBACKS",
             "SOLANA_RPC_WSS", "JUPITER_API_KEY", "MY_WALLETS", "PUBLIC_SOLANA_WALLET_ADDRESS", "FOMO_STATE_DIR",
             "DASHBOARD_PORT"]
DAY_KEYS = ["ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "ALPACA_DATA_FEED", "SNAPTRADE_CLIENT_ID",
            "SNAPTRADE_CONSUMER_KEY", "DAY_TELEGRAM_BOT_TOKEN", "DAY_TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY",
            "FINNHUB_API_KEY", "DAY_WATCHLIST", "ACCOUNT_SIZE", "RISK_PER_TRADE_PCT"]
KEYS = FOMO_KEYS + DAY_KEYS


def read_env(path: Path) -> dict:
    values = {}
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return values
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if key.strip() in KEYS and value:
                values[key.strip()] = value
    return values


def candidates(root: Path, home: Path | None = None) -> list[Path]:
    """.env files of other bot folders: next to this one, and on the Desktop / in Documents / in Downloads."""
    home = home or Path.home()
    places = [root.parent] + [home / name for name in ("Desktop", "Documents", "Downloads")]
    seen, out = set(), []
    for place in places:
        for pattern in ("*/.env", "*/*/.env"):
            try:
                found = list(place.glob(pattern))
            except OSError:                     # a folder macOS won't let Terminal read
                continue
            for env in found:
                folder = env.parent
                try:
                    is_bot = (folder / "bot.py").exists() or (folder / "day.py").exists()
                    key = folder.resolve()
                except OSError:
                    continue
                if is_bot and key != root.resolve() and key not in seen:
                    seen.add(key)
                    out.append(env)
    return out


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def best(found: list[tuple[Path, dict]], keys: list[str], need: str, prefer: str = "") -> tuple[Path | None, dict]:
    """The newest folder that has `need` set (ones with `prefer` set first), and its values for `keys`."""
    rows = [(p, v) for p, v in found if v.get(need)]
    if not rows:
        return None, {}
    rows.sort(key=lambda pv: (bool(pv[1].get(prefer)) if prefer else False, _mtime(pv[0])), reverse=True)
    path, values = rows[0]
    return path, {k: values[k] for k in keys if values.get(k)}


def set_key(lines: list[str], key: str, value: str) -> list[str]:
    out, done = [], False
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#") and line.split("=", 1)[0].strip() == key:
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        if key in DAY_KEYS and not any(line.strip() == "# --- DayBot ---" for line in out):
            out += ["", "# --- DayBot ---"]
        out.append(f"{key}={value}")
    return out


def main(root: Path = ROOT, home: Path | None = None) -> int:
    out = root / ".env"
    have = read_env(out) if out.exists() else {}
    found = [(p, read_env(p)) for p in candidates(root, home)]
    found = [(p, v) for p, v in found if v]
    fomo_from, fomo = best(found, FOMO_KEYS, "TELEGRAM_BOT_TOKEN", prefer="SOLANA_RPC_HTTP")
    # DayBot's keys one by one, each from the newest folder that has it (a key added later in one folder counts)
    day, day_from = {}, None
    for path, values in sorted(found, key=lambda pv: _mtime(pv[0]), reverse=True):
        for k in DAY_KEYS:
            if values.get(k) and k not in day:
                day[k] = values[k]
                day_from = day_from or path
    if not fomo.get("MY_WALLETS") and fomo.get("PUBLIC_SOLANA_WALLET_ADDRESS"):
        fomo["MY_WALLETS"] = fomo["PUBLIC_SOLANA_WALLET_ADDRESS"]
    fomo.pop("PUBLIC_SOLANA_WALLET_ADDRESS", None)
    add = {k: v for k, v in {**fomo, **day}.items() if not have.get(k)}
    if add:
        if out.exists():
            lines = out.read_text(errors="ignore").splitlines()
        elif (root / ".env.example").exists():
            lines = (root / ".env.example").read_text().splitlines()
        else:
            lines = []
        for k, v in add.items():
            lines = set_key(lines, k, v)
        out.write_text("\n".join(lines).rstrip() + "\n")
        try:
            out.chmod(0o600)
        except OSError:
            pass
        froms = []
        if any(k in FOMO_KEYS for k in add) and fomo_from:
            froms.append(f"FomoBot's settings from {fomo_from.parent.name}")
        if any(k in DAY_KEYS for k in add) and day_from:
            froms.append(f"DayBot's keys from {day_from.parent.name}")
        print("Copied " + " and ".join(froms) + " (" + str(len(add)) + " settings). Nothing else was copied — a "
              "TRADING_PRIVATE_KEY never is.")
    elif not found and not have.get("TELEGRAM_BOT_TOKEN"):
        print("No earlier bot folder with settings was found next to this one (or on your Desktop / in Documents / "
              "in Downloads).\nIf you have one somewhere else, copy its .env file into this folder (in Finder, "
              "Command+Shift+. shows hidden files) and start again.")
    final = {**have, **add}
    return 0 if final.get("TELEGRAM_BOT_TOKEN") else 2


if __name__ == "__main__":
    sys.exit(main())
