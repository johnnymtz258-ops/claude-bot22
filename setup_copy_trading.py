from pathlib import Path
import getpass, json

ROOT=Path(__file__).resolve().parent
ENV=ROOT/'.env'; EXAMPLE=ROOT/'.env.example'; WALLETS=ROOT/'tracked_wallets.json'
if not ENV.exists():
    ENV.write_text(EXAMPLE.read_text() if EXAMPLE.exists() else '')
lines=ENV.read_text().splitlines()

def getv(k):
    p=k+'='
    for line in lines:
        if line.startswith(p): return line[len(p):].strip()
    return ''

def setv(k,v):
    p=k+'='
    for i,line in enumerate(lines):
        if line.startswith(p): lines[i]=p+str(v); return
    lines.append(p+str(v))

print('\n=== FomoBot v16.0 COPY EDGE setup ===')
print('This configures PUBLIC smart-wallet tracking. Never paste a seed phrase/private key here.')
print('Paper copy and Telegram COPY BUY NOW alerts work without live trading.\n')
helius=getpass.getpass('Helius API key (recommended; Enter keeps current/public fallback): ').strip()
if helius: setv('HELIUS_API_KEY',helius)
setv('COPY_TRADING_ENABLED','true')

try: data=json.loads(WALLETS.read_text()) if WALLETS.exists() else {'wallets':[]}
except Exception: data={'wallets':[]}
rows=data.get('wallets') if isinstance(data,dict) else []
rows=rows if isinstance(rows,list) else []
print(f'Currently configured wallet rows: {len(rows)}')
raw=input('Paste public Solana wallet addresses separated by commas (Enter keeps current/uses auto-discovery): ').strip()
if raw:
    existing={str(x.get('address') if isinstance(x,dict) else x) for x in rows}
    for addr in [x.strip() for x in raw.split(',') if x.strip()]:
        if addr not in existing:
            rows.append({'address':addr,'label':'manual-smart','score':60}); existing.add(addr)
    WALLETS.write_text(json.dumps({'_comment':'Public wallets only. score is an initial prior; /copyanalyze can replace it with observed history.','wallets':rows},indent=2)+'\n')
else:
    if not WALLETS.exists():
        WALLETS.write_text(json.dumps({'_comment':'Public wallets only. The bot can auto-discover Birdeye smart/top-flow wallets.','wallets':rows},indent=2)+'\n')
ENV.write_text('\n'.join(lines)+'\n')
print('\nSaved. Restart with start_mac.command, then use /copy, /copywallets and /copydiscover.')
print('Live copying stays OFF and kill-switched. Do not enter a private key here.')
input('Press Enter to close.')
