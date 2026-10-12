"""First run: copy Telegram, RPC and wallet settings from a previous bot folder next to this one.

Only the keys listed below are copied. Private keys, seed phrases and old trading settings are
never read or copied. Exit code 2 means nothing was found (the launcher then runs the wizard).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEYS = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "HELIUS_API_KEY", "SOLANA_RPC_HTTP", "SOLANA_RPC_FALLBACKS",
        "SOLANA_RPC_WSS", "JUPITER_API_KEY", "MY_WALLETS", "PUBLIC_SOLANA_WALLET_ADDRESS", "FOMO_STATE_DIR",
        "DASHBOARD_PORT"]


def read_env(path: Path) -> dict:
    values = {}
    for line in path.read_text(errors="ignore").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if key.strip() in KEYS and value.strip():
                values[key.strip()] = value.strip()
    return values


def main() -> int:
    out = ROOT / ".env"
    if out.exists():
        return 0
    candidates = [p for p in ROOT.parent.glob("*/.env") if p.parent.resolve() != ROOT]
    candidates += [p for p in ROOT.parent.glob("*/*/.env") if p.parent.resolve() != ROOT]
    # the newest .env that has your RPC link wins (an older version's folder can be newer on disk after it ran)
    found = [(old, read_env(old)) for old in candidates]
    found = [(old, v) for old, v in found if v.get("TELEGRAM_BOT_TOKEN")]
    if not found:
        return 2
    found.sort(key=lambda ov: (bool(ov[1].get("SOLANA_RPC_HTTP")), ov[0].stat().st_mtime), reverse=True)
    old, values = found[0]
    if not values.get("MY_WALLETS") and values.get("PUBLIC_SOLANA_WALLET_ADDRESS"):
        values["MY_WALLETS"] = values["PUBLIC_SOLANA_WALLET_ADDRESS"]
    values.pop("PUBLIC_SOLANA_WALLET_ADDRESS", None)
    lines = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        lines.append(f"{key}={values.pop(key)}" if key in values else line)
    lines += [f"{k}={v}" for k, v in values.items()]
    out.write_text("\n".join(lines) + "\n")
    print(f"Copied Telegram, RPC and wallet settings from {old.parent.name}. Nothing else was copied "
          "(a TRADING_PRIVATE_KEY never is — add it again yourself if you use live trading).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
