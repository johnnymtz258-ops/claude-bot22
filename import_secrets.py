"""First run: copy Telegram/Helius/wallet settings from a previous bot folder next to this one.

Only the keys listed below are copied. Private keys, seed phrases and old trading settings are
never read or copied. Exit code 2 means nothing was found (the launcher then runs the wizard).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEYS = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "HELIUS_API_KEY", "SOLANA_RPC_FALLBACKS", "MY_WALLETS",
        "PUBLIC_SOLANA_WALLET_ADDRESS", "FOMO_STATE_DIR", "DASHBOARD_PORT"]


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
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old in candidates:
        values = read_env(old)
        if values.get("TELEGRAM_BOT_TOKEN"):
            break
    else:
        return 2
    if not values.get("MY_WALLETS") and values.get("PUBLIC_SOLANA_WALLET_ADDRESS"):
        values["MY_WALLETS"] = values["PUBLIC_SOLANA_WALLET_ADDRESS"]
    values.pop("PUBLIC_SOLANA_WALLET_ADDRESS", None)
    lines = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        lines.append(f"{key}={values.pop(key)}" if key in values else line)
    lines += [f"{k}={v}" for k, v in values.items()]
    out.write_text("\n".join(lines) + "\n")
    print(f"Copied Telegram/Helius/wallet settings from {old.parent.name}. Nothing else was copied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
