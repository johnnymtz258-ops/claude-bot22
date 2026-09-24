import ast
from pathlib import Path
src=Path(__file__).with_name("bot.py").read_text()
ast.parse(src)
checks=[
'FRESH_REQUIRE_RUGCHECK = eb("FRESH_REQUIRE_RUGCHECK", True)',
'FRESH_REQUIRE_HOLDER_CHECK = eb("FRESH_REQUIRE_HOLDER_CHECK", True)',
'fresh launch blocked: RugCheck has not completed',
'fresh launch blocked: holder distribution has not been verified',
'holder correlation',
'def partial_close_position',
'elif cmd in {"/sellpct","/sellusd","/sold"}:',
'POSITION AVERAGED',
'LIQUIDITY WARNING',
'LIQ_DROP = ef("LIQUIDITY_DROP_EXIT_PCT", 30)',
]
for c in checks: assert c in src, c
print("v10.3 hardening regression checks: PASS")
