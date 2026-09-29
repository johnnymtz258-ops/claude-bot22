from pathlib import Path
import getpass, os, re
from dotenv import load_dotenv
from live_execution import generate_keypair, wallet_address

ROOT=Path(__file__).resolve().parent
ENV=ROOT/".env"
load_dotenv(ENV)
STATE=Path(os.path.expanduser(os.getenv("FOMO_STATE_DIR","~/Library/Application Support/FomoBot")))
STATE.mkdir(parents=True,exist_ok=True)
DEFAULT_WALLET=STATE/"live_wallet.json"

def set_env(key,value):
    lines=ENV.read_text().splitlines() if ENV.exists() else []
    out=[]; found=False
    for line in lines:
        if re.match(rf"^{re.escape(key)}=",line):
            out.append(f"{key}={value}"); found=True
        else: out.append(line)
    if not found: out.append(f"{key}={value}")
    ENV.write_text("\n".join(out)+"\n")

print("\nFomoBot REAL AUTOPILOT local setup")
print("This enables real Solana swaps through Jupiter. Use a DEDICATED small hot wallet, not your main wallet.")
print("Never send this wallet file, seed phrase, or private key through Telegram or ChatGPT.\n")

api=os.getenv("JUPITER_API_KEY","").strip()
if not api:
    api=getpass.getpass("Paste your Jupiter API key (input hidden): ").strip()
if not api:
    raise SystemExit("No Jupiter API key entered; nothing changed.")

existing=os.getenv("LIVE_WALLET_KEYPAIR_PATH","").strip()
if existing and Path(os.path.expanduser(existing)).exists():
    wallet_path=Path(os.path.expanduser(existing))
    print(f"Using configured wallet file: {wallet_path}")
else:
    ans=input(f"Generate a NEW dedicated bot wallet at {DEFAULT_WALLET}? [Y/n]: ").strip().lower()
    if ans in {"","y","yes"}:
        wallet_path=DEFAULT_WALLET
        if wallet_path.exists():
            print("A dedicated wallet already exists there; reusing it.")
        else:
            address=generate_keypair(wallet_path)
            print(f"Created dedicated wallet. Public address: {address}")
    else:
        raw=input("Path to an existing Solana CLI JSON keypair file: ").strip()
        wallet_path=Path(os.path.expanduser(raw))
        if not wallet_path.exists(): raise SystemExit("That keypair file does not exist.")

try: address=wallet_address(wallet_path)
except Exception as exc: raise SystemExit(f"Wallet file is invalid: {exc}")

size=input("Normal auto-trade size in USD [5]: ").strip() or "5"
loss=input("Maximum realized auto-loss per day in USD [10]: ").strip() or "10"
opens=input("Maximum simultaneous auto positions [2]: ").strip() or "2"
for key,val in {
    "LIVE_TRADING_ENABLED":"true",
    "JUPITER_API_KEY":api,
    "LIVE_WALLET_KEYPAIR_PATH":str(wallet_path),
    "AUTO_LIVE_TRADE_USD":size,
    "AUTO_LIVE_MAX_DAILY_LOSS_USD":loss,
    "AUTO_LIVE_MAX_OPEN_POSITIONS":opens,
}.items(): set_env(key,val)

print("\nLocal live-trading capability is configured, but REAL AUTOPILOT is still OFF.")
print(f"Fund ONLY the amount you are comfortable risking at this public address:\n{address}")
print("Restart the bot. In Telegram run /autotest, then /autolive arm and /autolive confirm.")
print("Fresh-launch auto-buys stay OFF unless you explicitly run /autofresh on.\n")
