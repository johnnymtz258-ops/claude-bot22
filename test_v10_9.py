import tempfile
import time
from pathlib import Path
import bot


def pair(symbol='X', token='tok', price=1.0, mcap=2_000_000, liq=200_000,
         age_min=60, pc5=1.0, pc1=3.0, pc6=5.0, pc24=10.0,
         vol5=20_000, buys=30, sells=20):
    return {
        'chainId':'solana',
        'pairCreatedAt': int((time.time()-age_min*60)*1000),
        'baseToken':{'address':token,'symbol':symbol,'name':symbol},
        'priceUsd':price,
        'marketCap':mcap,
        'liquidity':{'usd':liq},
        'priceChange':{'m5':pc5,'h1':pc1,'h6':pc6,'h24':pc24},
        'volume':{'m5':vol5,'h1':vol5*5,'h6':vol5*20,'h24':vol5*50},
        'txns':{'m5':{'buys':buys,'sells':sells}},
    }


def test_manual_journal_has_no_position_or_loss_lock():
    with tempfile.TemporaryDirectory() as td:
        old = bot.DB_PATH
        bot.DB_PATH = Path(td)/'test.db'
        try:
            db=bot.Database()
            # Preload a realized loss much larger than the old daily lock.
            db.conn.execute("insert into positions(open_ts,close_ts,chain,token,symbol,name,entry_price,amount_usd,quantity,entry_liquidity,peak_price,active,close_price,realized_pnl) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (int(time.time())-100,int(time.time()),'solana','old','OLD','OLD',1,100,100,100000,1,0,.5,-50))
            db.conn.commit()
            ids=[]
            for i in range(6):
                sig={'chain':'solana','token':f't{i}','symbol':f'T{i}','name':f'T{i}','action':'MANUAL'}
                pid,err=db.record_position(sig,1.0,123.45,100000)
                assert err is None
                ids.append(pid)
            rows=db.open_positions()
            assert len(rows)==6, len(rows)
            assert all(abs(r['amount_usd']-123.45)<1e-9 for r in rows)
            db.conn.close()
        finally:
            bot.DB_PATH = old


def test_trade_context_snapshot_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        old = bot.DB_PATH
        bot.DB_PATH = Path(td)/'test.db'
        try:
            db=bot.Database()
            p=pair(price=0.000321,mcap=321000,liq=64000)
            sid=db.add_signal('EARLY',p,80,'ENTRY OPTION',['test'])
            db.map_trade_context(777,sid,p,'ENTRY OPTION')
            ctx=db.trade_context_from_message(777)
            assert abs(ctx['snapshot_price']-0.000321)<1e-12
            assert abs(ctx['snapshot_mcap']-321000)<1e-6
            assert ctx['token']=='tok'
            db.conn.close()
        finally:
            bot.DB_PATH = old


def test_hold_horizons():
    fresh=pair(age_min=25,liq=50000,mcap=250000,vol5=5000,buys=35,sells=20)
    assert bot.hold_plan(fresh,tier='FLOW ENTRY')['label']=='SCALP / FAST TRADE'

    established=pair(age_min=30*60,liq=250000,mcap=3_000_000,pc1=2,pc6=4,pc24=8,vol5=12000,buys=30,sells=20)
    ep=bot.hold_plan(established,tier='ENTRY OPTION')
    assert ep['label']=='EXTENDED HOLD CANDIDATE'
    assert ep['long_term'] is True

    risky=pair(price=.80,age_min=180,liq=150000,mcap=1_500_000,pc1=-5,vol5=10000,buys=20,sells=20)
    pos={'entry_price':1.0,'peak_price':1.05,'entry_tier':'ENTRY OPTION'}
    assert bot.hold_plan(risky,position=pos)['label']=='RISK / EXIT REVIEW'


def test_v109_static_contracts():
    src=Path(bot.__file__).read_text()
    for needle in [
        'VERSION = "v11.5 ENTRY CONFIRM + PROFIT REMINDER"',
        'Wrong? /undo',
        'telegram_trade_context',
        'FLOW ENTRY',
        'SECOND_LEG_ENABLED',
        'verified second-leg flow reset after prior entry alert',
        'if len(db.auto_open_positions())>=AUTO_LIVE_MAX_OPEN:',
        'elif cmd in {"/hold","/plan"}:',
        're.sub(r"^/bought\\b", "/buy"',
    ]:
        assert needle in src, needle


if __name__ == '__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for t in tests:
        t()
        print(t.__name__+': PASS')
    print('v10.9 tests: PASS')
