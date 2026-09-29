from pathlib import Path
import ast
src=Path(__file__).with_name("bot.py").read_text()
ast.parse(src)
for item in [
"def entry_stability_check(db, pair, tier=None):",
"rapid raw-price break",
"STRONG_MIN_PERSISTENT_BS",
"alert_move < -CHECK_MAX_DROP_FROM_ALERT_PCT",
"ADD CHECK PASSES",
"owned_ret <= -CHECK_POSITION_RISK_PCT",
]:
    assert item in src,item
print("v10.6 stability checks: PASS")
