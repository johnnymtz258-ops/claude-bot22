from pathlib import Path
import getpass

ROOT=Path(__file__).resolve().parent
ENV=ROOT/'.env'
EXAMPLE=ROOT/'.env.example'

if not ENV.exists():
    if EXAMPLE.exists():
        ENV.write_text(EXAMPLE.read_text())
    else:
        raise SystemExit('Missing .env and .env.example')

lines=ENV.read_text().splitlines()

def get_value(key):
    prefix=key+'='
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ''

def normalize_secret(value, header_names=()):
    text=str(value or '').strip()
    if len(text)>=2 and text[0]==text[-1] and text[0] in {'"', "'"}:
        text=text[1:-1].strip()
    low=text.lower()
    for name in header_names:
        prefix=str(name).lower()+':'
        if low.startswith(prefix):
            text=text[len(prefix):].strip(); low=text.lower(); break
    if low.startswith('bearer '):
        text=text[7:].strip()
    return text

def set_value(key,value):
    prefix=key+'='
    for i,line in enumerate(lines):
        if line.startswith(prefix):
            lines[i]=prefix+value
            return
    lines.append(prefix+value)

print('\n=== v16.0 COPY EDGE optional edge-source setup ===')
print('This stores API access for READ-ONLY discovery/social data.')
print('Never enter a seed phrase or trading private key here.')
print('Secrets are hidden while you type and are saved only in this bot folder.\n')

be_current=get_value('BIRDEYE_API_KEY')
print('Birdeye Data API key:', 'already configured' if be_current else 'not configured')
be=getpass.getpass('Paste a NEW Birdeye BDS API key, or press Enter to keep current: ').strip()
if be:
    set_value('BIRDEYE_API_KEY',normalize_secret(be,('X-API-KEY','BIRDEYE_API_KEY')))

x_current=get_value('X_BEARER_TOKEN')
print('\nX recent-search bearer token:', 'already configured' if x_current else 'not configured')
print('Note: X API search is pay-per-use. A 402 response means the app needs usable X API credits.')
x=getpass.getpass('Paste a new X bearer token, or press Enter to keep current: ').strip()
if x:
    set_value('X_BEARER_TOKEN',normalize_secret(x,('Authorization','X_BEARER_TOKEN')))

pp_current=get_value('PUMPPORTAL_API_KEY')
print('\nPumpPortal API key:', 'already configured' if pp_current else 'not configured')
pp=getpass.getpass('Paste a PumpPortal API key, or press Enter to keep current: ').strip()
if pp:
    set_value('PUMPPORTAL_API_KEY',normalize_secret(pp,('PUMPPORTAL_API_KEY',)))
    set_value('PUMPPORTAL_ENABLED','true')


helius_current=get_value('HELIUS_API_KEY')
print('\nHelius API key (recommended for low-latency smart-wallet WebSockets):', 'already configured' if helius_current else 'not configured')
helius=getpass.getpass('Paste a Helius API key, or press Enter to keep current/public RPC fallback: ').strip()
if helius:
    set_value('HELIUS_API_KEY',normalize_secret(helius,('HELIUS_API_KEY',)))

ENV.write_text('\n'.join(lines)+'\n')
print('\nSaved. Restart the bot, then use /sources and /copy to verify runtime status.')
print('If X says PAYMENT REQUIRED, add API credits in the X Developer Console or use /socialdeep off.')
print('If Birdeye still says CORE DEGRADED after a fresh BDS key, the bot will keep public/on-chain fallbacks active and show provider details.')
input('Press Enter to close.')
