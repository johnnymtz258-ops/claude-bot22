from pathlib import Path
import ast, asyncio, json
src=Path(__file__).with_name('bot.py').read_text()
ast.parse(src)
req=[
'VERSION = "v11.5 ENTRY CONFIRM + PROFIT REMINDER"',
'async def solana_rpc_holder_context',
'async def resilient_holder_context',
'OPTION_MIN_1H = max(-20.0, ef("ENTRY_OPTION_MIN_1H_PCT", -20.0))',
'RugCheck has not completed',
'holder distribution has not been verified',
'"near_entries"',
'elif cmd == "/near":',
'elif cmd == "/safety":',
'lsafety = cached_safety',
'actionable_blockers(lp,leader["entry"],leader["confirmed"]',
]
for x in req: assert x in src,x
print('v10.5 resilient static checks: PASS')
