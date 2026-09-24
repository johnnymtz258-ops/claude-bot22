from pathlib import Path
import csv, os, shutil, sqlite3, zipfile, time

ROOT=Path(__file__).resolve().parent
STATE=Path(os.path.expanduser(os.getenv('FOMO_STATE_DIR','~/Library/Application Support/FomoBot')))
DB=Path(os.path.expanduser(os.getenv('FOMO_STATE_DB','').strip() or str(STATE/'fomo_master.db')))
stamp=time.strftime('%Y%m%d_%H%M%S')
stage=ROOT/f'FOMO_DATA_EXPORT_{stamp}'
stage.mkdir(exist_ok=True)

backup=stage/'fomo_master.db'
if DB.exists():
    src=sqlite3.connect(DB); dst=sqlite3.connect(backup); src.backup(dst); dst.close(); src.close()

for p in [ROOT/'bot.log',ROOT/'signal_performance.csv',ROOT/'latency_events.csv',ROOT/'V11_WALK_FORWARD_REPORT.txt']:
    if p.exists(): shutil.copy2(p,stage/p.name)

# v11 learning/management tables are exported as plain CSV too. The SQLite DB is
# still the source of truth; these are convenience files for daily review.
if backup.exists():
    con=sqlite3.connect(backup)
    try:
        for table in ('positions','guardian_events','signal_outcomes','missed_opportunities','wallet_sync_state','social_events','decision_ledger','decision_outcomes','runs','lane_governor','journal_actions','paper_positions','execution_intents','execution_leases','trade_failures','auto_trade_events','decision_dedupe','copy_wallets','copy_wallet_events','copy_signals','copy_paper_positions','copy_paper_events','copy_live_positions','copy_wallet_candidates','copy_candidate_tokens','copy_wallet_history'):
            exists=con.execute("select 1 from sqlite_master where type='table' and name=?",(table,)).fetchone()
            if not exists: continue
            cur=con.execute(f'select * from {table}')
            cols=[d[0] for d in cur.description]
            with (stage/f'{table}.csv').open('w',newline='') as handle:
                w=csv.writer(handle); w.writerow(cols); w.writerows(cur.fetchall())
    finally:
        con.close()

zip_path=ROOT/f'FOMO_DATA_EXPORT_{stamp}.zip'
with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED) as z:
    for p in stage.iterdir(): z.write(p,p.name)
shutil.rmtree(stage)
print(f'\nCreated:\n{zip_path}\n')
print('Upload this ZIP for performance review. It does NOT include .env, seed phrases, or the bot wallet/keypair.')
input('Press Enter to close.')
