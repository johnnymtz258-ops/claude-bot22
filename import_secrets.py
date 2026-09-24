from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
out = ROOT / ".env"
example = ROOT / ".env.example"
if out.exists():
    raise SystemExit(0)

candidates = [
    ROOT.parent / "FOMO_BOT_v10_ACTIONABLE" / ".env",
    ROOT.parent / "FOMO_BOT_FINAL_READY_v9_5_2" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v9_3_clarity" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v9_2_final" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v9_1_stable" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v9" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v8" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v7_latency" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v6_4" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v6_3" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v6_2" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v6_1" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v6" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v5" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v4" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v3" / ".env",
    ROOT.parent / "fomo_early_signal_bot_v2" / ".env",
]
old = next((p for p in candidates if p.exists()), None)
# Folder names change frequently. If none of the known names exists, safely inspect
# neighboring bot folders and use the newest .env. Only the allow-listed API/Telegram
# fields below are copied; private-key/live-trading settings are never imported.
# A PUBLIC read-only wallet address is safe to carry forward.
if old is None:
    generic = [q for q in ROOT.parent.glob("*/.env") if q.parent.resolve() != ROOT.resolve()]
    generic.sort(key=lambda q: q.stat().st_mtime, reverse=True)
    old = generic[0] if generic else None
if old is None:
    raise SystemExit(2)

secrets = {"BIRDEYE_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "X_BEARER_TOKEN", "HELIUS_API_KEY", "PUBLIC_SOLANA_WALLET_ADDRESS", "PUMPPORTAL_API_KEY"}
values = {}
for line in old.read_text().splitlines():
    if "=" not in line or line.lstrip().startswith("#"):
        continue
    key, value = line.split("=", 1)
    if key.strip() in secrets:
        values[key.strip()] = value.strip()

text = example.read_text()
for key, value in values.items():
    text = text.replace(f"{key}=", f"{key}={value}", 1)
out.write_text(text)

old_wallets = old.parent / "tracked_wallets.json"
new_wallets = ROOT / "tracked_wallets.json"
if old_wallets.exists() and old_wallets.stat().st_size > 100:
    shutil.copy2(old_wallets, new_wallets)

print(f"Imported API/Telegram settings and any PUBLIC read-only wallet address from {old.parent.name}; private-key/live-trading settings were not copied.")
