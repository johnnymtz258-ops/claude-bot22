import sqlite3
import time
from pathlib import Path

import bot
from adaptive_engine import dollar_size_guide, position_state


def pair(price=1.0, liq=150000, mcap=500000, vol5=8000, buys=90, sells=45, pc5=2.0, pc1=6.0):
    return {
        'chainId':'solana',
        'baseToken':{'address':'SmartSize111111111111111111111111111111111','symbol':'SIZE','name':'SIZE'},
        'priceUsd':price,'marketCap':mcap,'liquidity':{'usd':liq},
        'volume':{'m5':vol5,'h1':50000},
        'txns':{'m5':{'buys':buys,'sells':sells}},
        'priceChange':{'m5':pc5,'h1':pc1,'h6':0,'h24':0},
    }


def conn_with_history(n=0, clean=True):
    c=sqlite3.connect(':memory:')
    c.execute('create table signals(id integer primary key, ts integer, kind text, chain text, token text, symbol text, name text, score real, price real, mcap real, liquidity real, action text, reasons text)')
    c.execute('create table evals(signal_id integer, checkpoint_min integer, checked_ts integer, price real, return_pct real, primary key(signal_id,checkpoint_min))')
    c.execute('create table signal_outcomes(signal_id integer, horizon_min integer, completed_ts integer, max_return_pct real, min_return_pct real, final_return_pct real, primary key(signal_id,horizon_min))')
    now=int(time.time())
    for i in range(1,n+1):
        c.execute('insert into signals values(?,?,?,?,?,?,?,?,?,?,?,?,?)',(i,now-i*60,'EARLY','solana',f't{i}','S','',75,1,500000,150000,'ENTRY OPTION',''))
        ret30=12 if clean else -8
        ret120=22 if clean else -20
        c.execute('insert into evals values(?,?,?,?,?)',(i,30,now,1+ret30/100,ret30))
        c.execute('insert into evals values(?,?,?,?,?)',(i,120,now,1+ret120/100,ret120))
        if i <= max(0,n-2):
            mx,mn,final=(28,-4,18) if clean else (8,-25,-16)
            c.execute('insert into signal_outcomes values(?,?,?,?,?,?)',(i,60,now,mx,mn,final))
    c.commit(); return c


def test_learning_size_never_exceeds_normal_max():
    c=conn_with_history(0)
    g=dollar_size_guide(c,pair(vol5=20000,buys=140,sells=45), 'STRONG ENTRY', 90, 5,20,30)
    assert 5 <= g['suggested_usd'] <= 20, g
    assert not g['exceptional_eligible'], g


def test_exceptional_requires_mature_clean_path():
    c=conn_with_history(42,clean=True)
    g=dollar_size_guide(c,pair(vol5=12000,buys=110,sells=45,pc5=2,pc1=8), 'ENTRY OPTION', 80, 5,20,30)
    assert g['calibration_mature'], g
    assert g['exceptional_eligible'], g
    assert 20 < g['suggested_usd'] <= 30, g


def test_late_entry_sizes_down():
    c=conn_with_history(42,clean=True)
    early=dollar_size_guide(c,pair(), 'ENTRY OPTION', 80, 5,20,30, late_move_pct=0)
    late=dollar_size_guide(c,pair(), 'ENTRY OPTION', 80, 5,20,30, late_move_pct=3.5)
    assert late['suggested_usd'] < early['suggested_usd'], (early,late)


def pos(amount=5, entry=1.0, peak=1.0, liq=100000, remaining_fraction=1.0, tp1_sent=0):
    return {'entry_price':entry,'peak_price':peak,'entry_liquidity':liq,'amount_usd':amount,
            'remaining_fraction':remaining_fraction,'tp1_sent':tp1_sent,'open_ts':int(time.time())-300}


def test_tiny_tp1_closes_instead_of_fee_heavy_partial():
    info=position_state(pair(price=1.16,liq=100000,buys=70,sells=40,pc5=1),pos(amount=5),tp1=15,tp2=35,min_partial_sale_usd=5)
    assert info['state']=='TAKE_PARTIAL',info
    assert info['suggested_partial_pct']==100,info
    assert info['partial_note'],info
    assert 'full' in info['action'].lower(),info


def test_normal_tp1_gives_percent_and_dollars():
    info=position_state(pair(price=1.16,liq=100000,buys=70,sells=40,pc5=1),pos(amount=20),tp1=15,tp2=35,min_partial_sale_usd=5)
    assert info['state']=='TAKE_PARTIAL',info
    assert info['suggested_partial_pct']>=20,info
    assert info['suggested_partial_usd']>=5,info
    assert info['suggested_partial_pct']==100,info
    assert info['remaining_value_usd']==0,info


def test_runner_can_pull_principal():
    info=position_state(pair(price=1.65,liq=100000,buys=80,sells=40,pc5=2),pos(amount=10,peak=1.65),tp1=15,tp2=35,min_partial_sale_usd=5)
    assert info['state']=='RUNNER',info
    assert 55 <= info['suggested_partial_pct'] <= 65,info
    assert abs(info['suggested_partial_usd']-10) < 1.5,info



def test_partial_plan_uses_remaining_stake_after_prior_sale():
    info=position_state(pair(price=1.40,liq=100000,buys=70,sells=40,pc5=1),
                        pos(amount=20,peak=1.40,remaining_fraction=0.50),
                        tp1=15,tp2=35,min_partial_sale_usd=5)
    assert abs(info['amount_usd']-10.0) < 1e-9, info
    assert info['current_value_usd'] < 15.0, info
    assert info['suggested_partial_usd'] <= info['current_value_usd'], info


def test_missed_tp1_profit_giveback_is_called_out():
    info=position_state(pair(price=1.02,liq=100000,buys=55,sells=50,pc5=-2),
                        pos(amount=20,peak=1.20,tp1_sent=1),
                        tp1=15,tp2=35,min_partial_sale_usd=5)
    assert info['missed_partial_giveback'], info
    assert info['state']=='EXIT_REVIEW', info
    assert info['suggested_partial_pct'] == 100, info
    assert 'PROFIT LOCK' in info['action'], info

def test_static_v114_contracts():
    src=Path(bot.__file__).read_text()
    for needle in [
        'VERSION = "v11.5 ENTRY CONFIRM + PROFIT REMINDER"',
        'AUTO_ENTRY_RECHECK_SECONDS',
        'def schedule_entry_followups',
        'AUTO CHECK {check_num}/{total_checks}',
        'adaptive_dollar_size',
        'GUARDIAN_MIN_PARTIAL_SALE_USD',
        'elif cmd in {"/sizerange","/riskrange"}',
        'elif cmd == "/autocheck"',
        'schedule_entry_followups(http,db,state,signal_id)',
    ]:
        assert needle in src, needle

# Extra async smoke test for the automatic freshness message plumbing.
def test_auto_entry_update_maps_fresh_snapshot():
    import asyncio
    class DB:
        def __init__(self): self.mapped=[]; self.logged=[]
        def signal_by_id(self, sid): return {'id':sid,'kind':'EARLY','chain':'solana','token':'tok','symbol':'TOK','price':1.0,'score':60,'action':'ENTRY OPTION'}
        def position_by_token(self, token): return None
        def recent_signal(self, chain, token, kind, hours=2):
            if kind=='SCOUT': return {'price':1.0}
            if kind=='CONFIRMED': return {'score':72}
            return None
        def log_latency(self,*args): self.logged.append(args)
        def map_telegram_signal(self,*args): self.mapped.append(('sig',args))
        def map_trade_context(self,*args): self.mapped.append(('ctx',args))
    db=DB(); state={'realtime':None,'safety_cache':{}}
    live=pair(price=1.01,liq=120000,mcap=400000,vol5=6000,buys=60,sells=30,pc5=1,pc1=4)
    sent=[]
    old_pair,old_send,old_cached,old_class,old_stab,old_size=(bot.pair_for_token,bot.send,bot.cached_safety,bot.classify_entry_tier,bot.entry_stability_check,bot.adaptive_dollar_size)
    async def fake_pair(http,chain,token): return live
    async def fake_send(http,text): sent.append(text); return 777
    try:
        bot.pair_for_token=fake_pair; bot.send=fake_send
        bot.cached_safety=lambda *a,**k:{'hard':False,'rug_checked':True,'holder_checked':True,'risks':[],'reasons':[]}
        bot.classify_entry_tier=lambda *a,**k:('ENTRY OPTION',[])
        bot.entry_stability_check=lambda *a,**k:(True,[],{})
        bot.adaptive_dollar_size=lambda *a,**k:{'suggested_usd':12,'size_label':'NORMAL'}
        result=asyncio.run(bot.auto_entry_update_once(None,db,state,1,1,2,60,False))
    finally:
        bot.pair_for_token,bot.send,bot.cached_safety,bot.classify_entry_tier,bot.entry_stability_check,bot.adaptive_dollar_size=(old_pair,old_send,old_cached,old_class,old_stab,old_size)
    assert result=='OPEN',result
    assert sent and 'AUTO CHECK 1/2' in sent[0] and 'Refreshed suggested buy: ~$12' in sent[0],sent
    assert any(x[0]=='ctx' for x in db.mapped),db.mapped

if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for t in tests:
        t(); print(t.__name__+': PASS')
    print('v11.4 smart-size/auto-update tests: PASS')

