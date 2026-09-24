from pathlib import Path
import ast

root = Path(__file__).resolve().parent
src = (root / 'bot.py').read_text()
ast.parse(src)

required = [
    'VERSION = "v11.5 ENTRY CONFIRM + PROFIT REMINDER"',
    'ssl.create_default_context(cafile=certifi.where())',
    'aiohttp.TCPConnector(ssl=self.ssl_context)',
    '_RUGCHECK_DOWN_UNTIL',
    'temporarily unavailable; pausing RugCheck calls for 5 minutes',
    'RugCheck temporarily unavailable (safe fallback active)',
]
for item in required:
    assert item in src, item
req = (root / 'requirements.txt').read_text()
assert 'certifi>=' in req
# Never weaken TLS verification in the network patch.
assert 'ssl=False' not in src
assert 'CERT_NONE' not in src
print('v11.2 network hardening static checks: PASS')
