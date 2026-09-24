from pathlib import Path
import ast
src=Path(__file__).with_name("bot.py").read_text()
ast.parse(src)
required=[
    'REENTRY_WATCH_CONFIRM_SECONDS = ei("REENTRY_WATCH_CONFIRM_SECONDS", 180)',
    'REENTRY_MAX_RECOVERY = ef("REENTRY_MAX_RECOVERY_FROM_LOW_PCT", 35)',
    'REENTRY_MIN_LIQUIDITY = ef("REENTRY_MIN_LIQUIDITY_USD", 60000)',
    'REENTRY_MIN_LIQ_RETENTION_PCT = ef("REENTRY_MIN_LIQ_RETENTION_PCT", 85)',
    'AUTO_LIVE_ALLOW_REENTRY = eb("AUTO_LIVE_ALLOW_REENTRY", False)',
    'recent_crash <= -REENTRY_MAX_RECENT_CRASH_PCT',
    'base_range > REENTRY_MAX_BASE_RANGE_PCT',
    'good < REENTRY_REQUIRED_GOOD_SNAPSHOTS',
    'REENTRY_WATCH',
    'reentry_watch_alert',
    'holder_ok=bool(safety and safety.get("holder_checked"))',
    'watch_age >= REENTRY_WATCH_CONFIRM_SECONDS',
]
for x in required:
    assert x in src, x
print("v10.4 data-tuned re-entry checks: PASS")
