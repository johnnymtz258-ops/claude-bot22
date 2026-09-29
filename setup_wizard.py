"""Asks for the few settings the bot needs and writes .env. Press Enter to skip any question."""
import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    print("\n🐋 FomoBot Whale Copy — first-time setup")
    print("Everything stays in the .env file on this Mac. Never enter a private key or seed phrase.\n")
    values = {
        "TELEGRAM_BOT_TOKEN": getpass.getpass("Telegram bot token (from @BotFather, hidden): ").strip(),
        "TELEGRAM_CHAT_ID": input("Your Telegram chat id (Enter if unknown — the bot will tell you): ").strip(),
        "HELIUS_API_KEY": getpass.getpass("Helius API key (free at helius.dev, recommended, hidden): ").strip(),
        "MY_WALLETS": input("Your PUBLIC wallet address for exact P/L (optional): ").strip(),
    }
    lines = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        lines.append(f"{key}={values[key]}" if values.get(key) else line)
    (ROOT / ".env").write_text("\n".join(lines) + "\n")
    print("\nSaved. You can edit .env any time.\n")


if __name__ == "__main__":
    main()
