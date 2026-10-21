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
        "SOLANA_RPC_HTTP": getpass.getpass("Solana RPC link (free — see FREE_RPC.txt; recommended, hidden): ").strip(),
        "MY_WALLETS": input("Your PUBLIC wallet address for exact P/L (optional): ").strip(),
    }
    env = ROOT / ".env"
    # keep whatever is already in .env (DayBot's keys, for one): only the answers given here are changed
    lines = env.read_text().splitlines() if env.exists() else (ROOT / ".env.example").read_text().splitlines()
    from import_secrets import set_key
    for key, value in values.items():
        if value:
            lines = set_key(lines, key, value)
    env.write_text("\n".join(lines).rstrip() + "\n")
    print("\nSaved. You can edit .env any time.\n")


if __name__ == "__main__":
    main()
