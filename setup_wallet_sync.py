from pathlib import Path
from wallet_sync import valid_solana_address

ROOT=Path(__file__).resolve().parent
env=ROOT/'.env'
if not env.exists():
    raise SystemExit('No .env exists yet. Start the bot once first so setup/import can create it.')
print('\nFomo Bot v16.0 COPY EDGE — READ-ONLY wallet sync setup')
print('This needs only your PUBLIC Solana wallet address. Never enter a seed phrase/private key.\n')
address=input('Public Solana wallet address (Enter to disable): ').strip()
if address and not valid_solana_address(address):
    raise SystemExit('That does not look like a valid Solana public address. Nothing changed.')
lines=env.read_text().splitlines()
out=[]; found=False
for line in lines:
    if line.startswith('PUBLIC_SOLANA_WALLET_ADDRESS='):
        out.append('PUBLIC_SOLANA_WALLET_ADDRESS='+address); found=True
    else:
        out.append(line)
if not found:
    out.append('PUBLIC_SOLANA_WALLET_ADDRESS='+address)
env.write_text('\n'.join(out)+'\n')
print('\nRead-only wallet sync '+('configured.' if address else 'disabled.')+' Restart the bot, then use /wallet or /sync.\n')
input('Press Enter to close.')
