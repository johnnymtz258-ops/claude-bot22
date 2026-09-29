from pathlib import Path
import getpass

ROOT = Path(__file__).resolve().parent
env = ROOT / ".env"
print("\nFomo Early-Signal Bot v11 — first-run setup")
print("Private keys stay in this folder on your Mac.\n")
vals = {
    "BIRDEYE_API_KEY": getpass.getpass("Birdeye API key (hidden; Enter to skip): ").strip(),
    "TELEGRAM_BOT_TOKEN": getpass.getpass("Telegram bot token (hidden; Enter to skip): ").strip(),
    "TELEGRAM_CHAT_ID": input("Telegram chat ID (Enter to skip): ").strip(),
    "X_BEARER_TOKEN": getpass.getpass("X API bearer token (hidden; Enter to skip): ").strip(),
    "HELIUS_API_KEY": getpass.getpass("Helius API key (optional; Enter to use public Solana RPC): ").strip(),
    "PUBLIC_SOLANA_WALLET_ADDRESS": input("Public Solana wallet address for read-only position sync (optional): ").strip(),
}
text = (ROOT / ".env.example").read_text()
for key, value in vals.items():
    text = text.replace(f"{key}=", f"{key}={value}", 1)
env.write_text(text)
print("\nSetup saved.\n")
