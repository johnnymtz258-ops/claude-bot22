"""Offline chronological validation report for Fomo Bot v16.0 COPY EDGE.

No trades are placed and no live thresholds are changed. The newest completed data is
held out from the older data to make daily tuning less vulnerable to overfitting.
"""
from pathlib import Path
import os, sqlite3, time
from dotenv import load_dotenv
from adaptive_engine import walk_forward_summary, format_walk_forward

ROOT=Path(__file__).resolve().parent
load_dotenv(ROOT/'.env')
STATE=Path(os.path.expanduser(os.getenv('FOMO_STATE_DIR','~/Library/Application Support/FomoBot')))
DB=Path(os.path.expanduser(os.getenv('FOMO_STATE_DB','').strip() or str(STATE/'fomo_master.db')))
OUT=ROOT/'V13_3_WALK_FORWARD_REPORT.txt'


def main():
    if not DB.exists():
        raise SystemExit(f'Master database not found: {DB}')
    con=sqlite3.connect(DB)
    try:
        reports=[walk_forward_summary(con,30),walk_forward_summary(con,120)]
    finally:
        con.close()
    text=(
        'FOMO BOT v16.0 COPY EDGE — WALK-FORWARD VALIDATION\n'
        f'Generated: {time.strftime("%Y-%m-%d %H:%M:%S")}\n'
        f'Database: {DB}\n\n'
        + '\n\n'.join(format_walk_forward(r) for r in reports)
        + '\n\nInterpretation:\n'
          '- The newest chronological slice is not used to define the older comparison slice.\n'
          '- Endpoint returns do not prove a target was reached before a drawdown.\n'
          '- This lab never loosens entry filters or changes real-trading settings automatically.\n'
          '- Use it together with signal path outcomes, missed-opportunity review, and Guardian trade management.\n'
    )
    OUT.write_text(text)
    print('\n'+text)
    print(f'\nSaved: {OUT}')

if __name__=='__main__':
    main()
