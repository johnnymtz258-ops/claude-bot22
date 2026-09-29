import sqlite3
import tempfile
import time
from pathlib import Path

import bot
from adaptive_engine import confidence_report, position_state, walk_forward_summary
from wallet_sync import reconcile_balance, valid_solana_address


def pair(price=1.0, liq=150000, mc=500000, vol5=10000, buys=40, sells=20, pc5=2, pc1=5, age_min=120):
    return {
        'chainId':'solana',
        'pairCreatedAt':int((time.time()-age_min*60)*1000),
        'baseToken':{'address':'So11111111111111111111111111111111111111112','symbol':'TEST','name':'TEST'},
        'priceUsd':price,'marketCap':mc,'liquidity':{'usd':liq},
        'volume':{'m5':vol5,'h1':vol5*6,'h6':vol5*20,'h24':vol5*60},
        'priceChange':{'m5':pc5,'h1':pc1,'h6':5,'h24':10},
        'txns':{'m5':{'buys':buys,'sells':sells}},
    }


def test_guardian_state_machine():
    base={'entry_price':1.0,'peak_price':1.0,'entry_liquidity':150000,'open_ts':int(time.time()-600)}
    assert position_state(pair(price=.88),base)['state']=='EXIT_REVIEW'
    assert position_state(pair(price=1.18),base)['state']=='TAKE_PARTIAL'
    assert position_state(pair(price=1.40),base)['state']=='RUNNER'
    weak=dict(base,peak_price=1.40)
    assert position_state(pair(price=1.20,pc5=-7,buys=15,sells=20),weak)['state']=='EXIT_REVIEW'


def _calibration_db(positive=True):
    c=sqlite3.connect(':memory:')
    c.execute('create table signals(id integer primary key, ts integer, kind text, chain text, action text, score real)')
    c.execute('create table evals(signal_id integer, checkpoint_min integer, return_pct real)')
    now=int(time.time())
    for i in range(30):
        c.execute('insert into signals values(?,?,?,?,?,?)',(i+1,now-i,'EARLY','solana','ENTRY OPTION',70+i%5))
        r30=(6+i%3) if positive else (-12-i%4)
        r120=(18+i%8) if positive else (-25-i%10)
        c.execute('insert into evals values(?,?,?)',(i+1,30,r30))
        c.execute('insert into evals values(?,?,?)',(i+1,120,r120))
    c.commit(); return c


def test_adaptive_confidence_responds_to_history():
    good=_calibration_db(True); bad=_calibration_db(False)
    g=confidence_report(good,pair(),'ENTRY OPTION',72,5)
    b=confidence_report(bad,pair(),'ENTRY OPTION',72,5)
    assert g['confidence']>b['confidence'],(g,b)
    assert g['endpoint_hit15_120']>b['endpoint_hit15_120']
    assert g['suggested_usd']>=b['suggested_usd']
    good.close(); bad.close()


def test_wallet_reconcile_math():
    assert valid_solana_address('So11111111111111111111111111111111111111112')
    assert reconcile_balance(None,100)['event']=='baseline'
    d=reconcile_balance(100,60,3)
    assert d['event']=='decrease' and abs(d['sold_fraction']-.4)<1e-9
    assert reconcile_balance(100,.1,3)['event']=='closed'
    assert reconcile_balance(100,130,3)['event']=='increase'


def test_v11_schema_and_learning_ledgers():
    with tempfile.TemporaryDirectory() as td:
        old=bot.DB_PATH; bot.DB_PATH=Path(td)/'test.db'
        try:
            db=bot.Database()
            tables={r[0] for r in db.conn.execute("select name from sqlite_master where type='table'")}
            assert {'guardian_events','signal_outcomes','missed_opportunities','wallet_sync_state'}<=tables
            cols={r[1] for r in db.conn.execute('pragma table_info(positions)')}
            assert {'guardian_state','guardian_confidence','guardian_last_alert_ts'}<=cols

            now=int(time.time())
            p=pair(price=1.0)
            sid=db.add_signal('EARLY',p,72,'ENTRY OPTION',['test'])
            # Make the signal old enough for a 30m path and supply raw observations.
            db.conn.execute('update signals set ts=? where id=?',(now-31*60,sid))
            token=p['baseToken']['address']
            points=[(now-31*60,1.0),(now-25*60,1.20),(now-15*60,.95),(now-1*60,1.10)]
            for ts,price in points:
                db.conn.execute('insert or replace into observations values(?,?,?,?,?,?,?,?,?,?,?,?)',
                                (ts,'solana',token,'TEST',price,500000,150000,10000,40,20,2,5))
            db.conn.commit()
            assert bot.capture_signal_outcomes(db)>=1
            out=db.conn.execute('select * from signal_outcomes where signal_id=? and horizon_min=30',(sid,)).fetchone()
            assert out is not None
            assert float(out['max_return_pct'])>=19.9
            assert float(out['min_return_pct'])<=-4.9
            db.conn.close()
        finally:
            bot.DB_PATH=old


def test_missed_opportunity_audit():
    with tempfile.TemporaryDirectory() as td:
        old=bot.DB_PATH; bot.DB_PATH=Path(td)/'test.db'
        try:
            db=bot.Database(); now=int(time.time()); token='Missed1111111111111111111111111111111111111'
            base_ts=now-70*60
            # Baseline meets the strong-flow retrospective screen, with a clean +35% path.
            rows=[
                (base_ts,1.0,120000,35000,8000,90,40,1,4),
                (base_ts+10*60,1.10,132000,36000,9000,100,45,4,8),
                (base_ts+30*60,1.35,162000,38000,12000,120,50,8,15),
                (base_ts+55*60,1.25,150000,37000,9000,80,50,-2,8),
            ]
            for ts,price,mc,liq,vol,b,s,pc5,pc1 in rows:
                db.conn.execute('insert or replace into observations values(?,?,?,?,?,?,?,?,?,?,?,?)',
                                (ts,'solana',token,'MISS',price,mc,liq,vol,b,s,pc5,pc1))
            db.conn.commit()
            added=bot.audit_missed_opportunities(db)
            assert added>=1,added
            r=db.conn.execute('select * from missed_opportunities where token=?',(token,)).fetchone()
            assert r is not None and float(r['max_return_pct'])>=34
            db.conn.close()
        finally:
            bot.DB_PATH=old



def test_walk_forward_holdout_report():
    c=sqlite3.connect(':memory:')
    c.execute('create table signals(id integer primary key, ts integer, kind text, chain text, action text, score real)')
    c.execute('create table evals(signal_id integer, checkpoint_min integer, return_pct real)')
    for i in range(40):
        c.execute('insert into signals values(?,?,?,?,?,?)',(i+1,1000+i,'EARLY','solana','ENTRY OPTION',55+(i%25)))
        # Deliberately let the newest chronological slice differ from the older data.
        r=(-2+i%5) if i<28 else (5+i%8)
        c.execute('insert into evals values(?,?,?)',(i+1,30,r))
    c.commit()
    report=walk_forward_summary(c,30)
    assert report['ready'] and report['train_n']==28 and report['test_n']==12,report
    assert report['test']['median']>report['train']['median'],report
    c.close()


def test_v11_adaptive_real_auto_is_reduction_only():
    src=Path(bot.__file__).read_text()
    assert 'auto_mult=min(1.0' in src
    assert 'adaptive trailing drawdown' in src
    assert 'stale deteriorating setup' in src
    assert 'walk_forward_summary' in src
    wsrc=(Path(bot.__file__).parent/'wallet_sync.py').read_text()
    assert 'recent_sale_evidence' in wsrc
    assert 'USDT_MINT' not in wsrc

def test_static_v11_contracts():
    src=Path(bot.__file__).read_text()
    for needle in [
        'VERSION = "v11.5 ENTRY CONFIRM + PROFIT REMINDER"',
        'async def guardian_loop',
        'PublicSolanaWalletSync',
        'confidence_report',
        'signal_outcomes',
        'missed_opportunities',
        'elif cmd == "/guardian"',
        'elif cmd in {"/confidence","/size"}',
        'elif cmd == "/sync"',
        'elif cmd == "/lab"',
    ]:
        assert needle in src,needle


if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for t in tests:
        t(); print(t.__name__+': PASS')
    print('v11 tests: PASS')
