import sqlite3, tempfile
from pathlib import Path
import migrate_state as ms

SCHEMA='''create table positions(
 id integer primary key autoincrement, open_ts integer, close_ts integer,
 chain text, token text, symbol text, entry_price real, amount_usd real,
 active integer, realized_pnl real
)'''

def db(path):
    c=sqlite3.connect(path); c.execute(SCHEMA); c.commit(); return c

with tempfile.TemporaryDirectory() as td:
    td=Path(td); mp=td/'master.db'; sp=td/'source.db'
    m=db(mp); s=db(sp)
    # Shared ledger says the original trade was sold.
    m.execute("insert into positions(open_ts,close_ts,chain,token,symbol,entry_price,amount_usd,active,realized_pnl) values(?,?,?,?,?,?,?,?,?)",
              (100,200,'solana','AAA','FRUG',0.1,10,0,-2))
    # Old folder still incorrectly says the same original trade is active.
    s.execute("insert into positions(open_ts,close_ts,chain,token,symbol,entry_price,amount_usd,active,realized_pnl) values(?,?,?,?,?,?,?,?,?)",
              (100,None,'solana','AAA','FRUG',0.1,10,1,None))
    # A genuinely later re-entry should still migrate.
    s.execute("insert into positions(open_ts,close_ts,chain,token,symbol,entry_price,amount_usd,active,realized_pnl) values(?,?,?,?,?,?,?,?,?)",
              (300,None,'solana','AAA','FRUG',0.2,5,1,None))
    s.commit(); s.close()
    imported=ms.merge_active_positions(m,sp,'old')
    m.commit()
    rows=m.execute("select open_ts,active from positions order by open_ts").fetchall()
    assert imported==1, imported
    assert rows==[(100,0),(300,1)], rows
    # Simulate duplicate clones already created by the old bug.
    m.execute("insert into positions(open_ts,close_ts,chain,token,symbol,entry_price,amount_usd,active,realized_pnl) values(?,?,?,?,?,?,?,?,?)",
              (100,400,'solana','AAA','FRUG',0.1,10,0,-9))
    m.execute("insert into positions(open_ts,close_ts,chain,token,symbol,entry_price,amount_usd,active,realized_pnl) values(?,?,?,?,?,?,?,?,?)",
              (100,500,'solana','AAA','FRUG',0.1,10,0,-9))
    m.commit()
    removed=ms.repair_legacy_position_duplicates(m)
    m.commit()
    assert removed==2, removed
    assert m.execute("select count(*) from positions where open_ts=100").fetchone()[0]==1
    assert m.execute("select count(*) from positions where open_ts=300").fetchone()[0]==1
    m.close()
print('v10.8 state-ledger tests: PASS')
