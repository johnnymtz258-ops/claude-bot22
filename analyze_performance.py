import csv
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

ROOT=Path(__file__).resolve().parent
DB=ROOT/'v9_signals.db'
if not DB.exists():
    print('No v9 database yet. Run the bot first.')
    raise SystemExit

con=sqlite3.connect(DB)
rows=con.execute("""select s.id,s.chain,s.kind,s.score,s.mcap,s.liquidity,e.checkpoint_min,e.return_pct
from signals s join evals e on e.signal_id=s.id
where s.kind in ('EARLY','PAPER_ENTRY') order by s.id""").fetchall()

if not rows:
    print('No completed ENTRY/PAPER_ENTRY evaluations yet. Let v9 collect data first.')
    raise SystemExit

out=[]
for checkpoint in (5,15,30,120,1440):
    groups=defaultdict(list)
    for sid,chain,kind,score,mcap,liq,cp,ret in rows:
        if cp!=checkpoint:
            continue
        liq_bucket = '<50k' if liq < 50000 else ('50-100k' if liq < 100000 else '100k+')
        mcap_bucket = '<250k' if mcap < 250000 else ('250k-1m' if mcap < 1000000 else '1m+')
        groups[(chain,kind,liq_bucket,mcap_bucket)].append(ret)
    for key,vals in sorted(groups.items()):
        chain,kind,liq_bucket,mcap_bucket=key
        out.append([
            checkpoint,chain,kind,liq_bucket,mcap_bucket,len(vals),
            sum(v>0 for v in vals)/len(vals)*100,mean(vals),median(vals),min(vals),max(vals)
        ])

path=ROOT/'performance_report.csv'
with path.open('w',newline='') as h:
    w=csv.writer(h)
    w.writerow(['checkpoint_min','chain','kind','liquidity_bucket','mcap_bucket','samples',
                'positive_pct','avg_return_pct','median_return_pct','worst_pct','best_pct'])
    w.writerows(out)

latency=ROOT/'latency_events.csv'
print(f'Wrote {path.name}')
print(f'Latency data: {latency.name if latency.exists() else "will appear after Scout/check/buy events"}')
print('Use these files to tune v9 only after a meaningful sample has accumulated.')
