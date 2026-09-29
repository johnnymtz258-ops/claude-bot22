import asyncio
import tempfile
import time
from pathlib import Path

import bot
from edge_engine import setup_path_quality, breadth_regime, analog_summary, risk_position_size, proof_metrics
from execution_quality import JupiterExecutionProbe


def with_db(fn):
    with tempfile.TemporaryDirectory() as td:
        old=bot.DB_PATH; bot.DB_PATH=Path(td)/'v15.db'
        try:
            db=bot.Database(); fn(db); db.conn.close()
        finally:
            bot.DB_PATH=old


def pair(token='tok',price=.001,mc=500_000,liq=90_000,pc5=1.5,pc1=3.0,buys=60,sells=35,turnover=1.0):
    vol=mc*turnover/100
    return {'chainId':'solana','pairCreatedAt':(time.time()-7200)*1000,
            'baseToken':{'address':token,'symbol':token.upper(),'name':token},'priceUsd':price,
            'marketCap':mc,'fdv':mc,'liquidity':{'usd':liq},'priceChange':{'m5':pc5,'h1':pc1},
            'volume':{'m5':vol,'h1':vol*12},'txns':{'m5':{'buys':buys,'sells':sells}}}


def test_v15_brand_and_solana_first_defaults():
    assert bot.VERSION=='v16.2 QUALITY MEASUREMENT'
    assert bot.CHAINS==['solana']
    assert bot.REENTRY_BUY_ALERTS_ENABLED is False
    assert bot.SHADOW_SIM_ALWAYS_ON is True
    assert bot.PROOF_FIRST_REQUIRE_MATURE is True


def test_stateful_path_accepts_controlled_continuation():
    now=int(time.time())
    h=[{'ts':now-60,'price':1.0,'liquidity':100,'buys5':12,'sells5':8},
       {'ts':now-30,'price':1.01,'liquidity':101,'buys5':14,'sells5':8},
       {'ts':now,'price':1.02,'liquidity':102,'buys5':15,'sells5':9}]
    x=setup_path_quality(h,{'price':1.021,'liquidity':102,'buys':16,'sells':9},min_span_seconds=45)
    assert x['ok'],x
    assert x['mode'] in {'CONTINUATION','RECLAIM'}


def test_stateful_path_blocks_chase_and_liquidity_decay():
    now=int(time.time())
    h=[{'ts':now-60,'price':1.0,'liquidity':100,'buys5':15,'sells5':8},
       {'ts':now-30,'price':1.03,'liquidity':98,'buys5':15,'sells5':8},
       {'ts':now,'price':1.06,'liquidity':94,'buys5':15,'sells5':8}]
    x=setup_path_quality(h,{'price':1.07,'liquidity':90,'buys':15,'sells':8},max_chase_pct=4,max_liquidity_drop_pct=5)
    assert not x['ok']
    assert any('ran' in b or 'liquidity' in b for b in x['blocks']),x


def test_breadth_risk_off_requires_multiple_stress_votes():
    bad=[{'pc5':-3} for _ in range(7)]+[{'pc5':1}]
    x=breadth_regime(bad,min_tokens=8)
    assert x['risk_off'] and x['label']=='RISK_OFF',x
    mixed=[{'pc5':-2},{'pc5':0},{'pc5':1},{'pc5':1},{'pc5':2},{'pc5':2},{'pc5':3},{'pc5':3}]
    y=breadth_regime(mixed,min_tokens=8)
    assert not y['risk_off'],y


def test_historical_analog_is_veto_only_for_repeated_bad_shape():
    current={'pc5':2,'pc1':3,'mcap':500000,'liquidity':90000,'turnover_pct':1.0,'buy_sell':1.7,'swaps':95}
    rows=[]
    for i in range(25):
        rows.append({**current,'pc5':2+(i%3)*.05,'max_return_pct':4,'min_return_pct':-11,'final_return_pct':-5})
    x=analog_summary(current,rows,nearest=25,min_neighbors=20)
    assert x['veto'] and x['severe8_rate']>=.5,x
    y=analog_summary(current,rows[:5],nearest=25,min_neighbors=20)
    assert not y['veto'] and y['n']<20,y


def test_risk_position_size_uses_half_percent_bankroll_risk():
    x=risk_position_size(100,stop_pct=8,risk_pct=.5,min_usd=1,max_usd=10)
    assert abs(x['risk_usd']-.5)<1e-9
    assert abs(x['suggested_usd']-6.25)<1e-9,x


def test_proof_gate_requires_sample_and_positive_net_quality():
    rows=[{'realized_pnl':.25,'amount_usd':5,'token':f'w{i}'} for i in range(7)]+[{'realized_pnl':-.10,'amount_usd':5,'token':f'l{i}'} for i in range(5)]
    a=proof_metrics(rows[:8],min_paths=12); assert a['status']=='PROBATION' and a['paused']
    b=proof_metrics(rows,min_paths=12); assert b['status']=='ACTIVE' and not b['paused'],b
    bad=[{'realized_pnl':-.2,'amount_usd':5,'token':f'b{i}'} for i in range(8)]+[{'realized_pnl':.1,'amount_usd':5,'token':f'g{i}'} for i in range(4)]
    c=proof_metrics(bad,min_paths=12); assert c['status']=='QUARANTINED' and c['paused'],c


def test_fee_aware_shadow_paper_charges_friction_even_flat_trade():
    def run(db):
        sig={'id':None,'chain':'solana','token':'x','symbol':'X','name':'X'}
        pid=db.paper_open(sig,1.0,100000,'ENTRY OPTION',amount=5,friction_pct=1.7,shadow=True,source_event='TEST')
        pnl=db.paper_close(pid,1.0,'flat')
        assert abs(pnl+0.085)<.002,pnl
        st=db.paper_stats(7,shadow_only=True,build_version=bot.VERSION)
        assert st['closed']==1 and st['pnl']<0 and st['fees_usd']>0,st
    with_db(run)


def test_proof_rows_only_use_current_build_core_shadow():
    def run(db):
        for i,tier in enumerate(['ENTRY OPTION','STRONG ENTRY','STRUCTURE ENTRY']):
            sig={'id':None,'chain':'solana','token':f'x{i}','symbol':f'X{i}','name':f'X{i}'}
            pid=db.paper_open(sig,1,100000,tier,amount=5,friction_pct=0,shadow=True,source_event='TEST',proof_eligible=(tier!='STRUCTURE ENTRY'),proof_reason='test')
            db.paper_close(pid,1.1,'done')
        rows=db.proof_shadow_rows(20)
        assert len(rows)==2 and {r['tier'] for r in rows}=={'ENTRY OPTION','STRONG ENTRY'},rows
    with_db(run)


def test_adaptive_size_is_zero_before_fresh_v15_proof():
    def run(db):
        g=bot.adaptive_dollar_size(db,pair(),'ENTRY OPTION',90)
        assert g['suggested_usd']==0,g
        assert g['proof_health']['status']=='PROBATION',g
    with_db(run)


def test_paper_losing_streak_trips_risk_circuit():
    def run(db):
        for i in range(bot.PAPER_LOSS_STREAK_LIMIT):
            sig={'id':None,'chain':'solana','token':f'l{i}','symbol':f'L{i}','name':f'L{i}'}
            pid=db.paper_open(sig,1,100000,'ENTRY OPTION',amount=5,friction_pct=0,shadow=True,source_event='TEST',proof_eligible=True,proof_reason='test')
            db.paper_close(pid,.98,'loss')
        x=bot.paper_risk_guard(db)
        assert x['paused'] and x['losing_streak']>=bot.PAPER_LOSS_STREAK_LIMIT,x
    with_db(run)


def test_jupiter_probe_measures_roundtrip_and_never_builds_transaction():
    class FakeHTTP:
        def __init__(self): self.calls=[]
        async def get(self,url,headers=None,params=None,retries=0):
            self.calls.append((url,dict(params or {})))
            if params['inputMint'].startswith('So111'):
                return 200,{'outAmount':'1000000','priceImpactPct':'0.001'}
            return 200,{'outAmount':'29800000','priceImpactPct':'0.001'}
    old=(bot.os.environ.get('PHANTOM_FEE_BPS_PER_SIDE'),bot.os.environ.get('JUPITER_PROBE_SOL'))
    try:
        bot.os.environ['PHANTOM_FEE_BPS_PER_SIDE']='85'; bot.os.environ['JUPITER_PROBE_SOL']='0.03'
        probe=JupiterExecutionProbe(); http=FakeHTTP(); info=asyncio.run(probe.probe(http,'TOKENMINT'))
        assert info['available'] and not info['hard_block'],info
        assert 200 < info['total_friction_bps'] < 300,info
        assert len(http.calls)==2 and all('/quote' in u for u,_ in http.calls)
    finally:
        for k,v in zip(['PHANTOM_FEE_BPS_PER_SIDE','JUPITER_PROBE_SOL'],old):
            if v is None: bot.os.environ.pop(k,None)
            else: bot.os.environ[k]=v


def test_runner_radar_tracks_broad_flow_without_becoming_buy_rule():
    p=pair(mc=800000,liq=80000,turnover=.8,buys=40,sells=25,pc5=2,pc1=8)
    ok,reasons=bot.runner_radar_qualifies(p)
    assert ok and reasons


def test_v15_commands_and_modules_packaged():
    root=Path(bot.__file__).parent; src=Path(bot.__file__).read_text()
    for needle in ['/edge','/leaders','/bankroll 100','/shadow on|off','ENTRY_PATH','ENTRY_EXECUTION','PROOF_SHADOW','RUNNER_RADAR']:
        assert needle in src,needle
    assert (root/'edge_engine.py').exists() and (root/'execution_quality.py').exists()


def test_live_guidance_requires_explicit_bankroll_even_after_proof_active():
    def run(db):
        for i in range(bot.PROOF_FIRST_MIN_PATHS):
            sig={'id':None,'chain':'solana','token':f'p{i}','symbol':f'P{i}','name':f'P{i}'}
            pid=db.paper_open(sig,1,100000,'ENTRY OPTION',amount=5,friction_pct=0,shadow=True,source_event='TEST',proof_eligible=True,proof_reason='test')
            db.paper_close(pid,1.10,'win')
        assert bot.proof_first_health(db)['status']=='ACTIVE'
        g=bot.adaptive_dollar_size(db,pair(),'ENTRY OPTION',90)
        assert g['suggested_usd']==0 and g['size_label']=='SET BANKROLL',g
        db.set_meta('bankroll_usd','100')
        g2=bot.adaptive_dollar_size(db,pair(),'ENTRY OPTION',90)
        assert 0 < g2['suggested_usd'] <= 6.25,g2
    with_db(run)


def test_jupiter_v2_preflight_uses_public_taker_but_never_execute_or_build():
    class FakeHTTP:
        def __init__(self): self.calls=[]
        async def get(self,url,headers=None,params=None,retries=0):
            self.calls.append((url,dict(params or {})))
            if params['inputMint'].startswith('So111'):
                return 200,{'outAmount':'1000000','transaction':'UNSIGNED','requestId':'REQ'}
            return 200,{'outAmount':'29900000','transaction':'UNSIGNED2','requestId':'REQ2'}
    keys=['PUBLIC_SOLANA_WALLET_ADDRESS','JUPITER_PREFLIGHT_TAKER','PHANTOM_FEE_BPS_PER_SIDE','JUPITER_PROBE_SOL']
    old={k:bot.os.environ.get(k) for k in keys}
    try:
        bot.os.environ['PUBLIC_SOLANA_WALLET_ADDRESS']='11111111111111111111111111111111'
        bot.os.environ['PHANTOM_FEE_BPS_PER_SIDE']='85'; bot.os.environ['JUPITER_PROBE_SOL']='0.03'
        probe=JupiterExecutionProbe(); http=FakeHTTP(); info=asyncio.run(probe.probe(http,'TOKENMINT'))
        assert info['available'] and 'swap-v2-order-readonly' in info['source'],info
        assert len(http.calls)==2
        assert all('/swap/v2/order' in u for u,_ in http.calls)
        assert all('/execute' not in u and '/build' not in u for u,_ in http.calls)
        assert all(p.get('taker')=='11111111111111111111111111111111' for _,p in http.calls)
    finally:
        for k,v in old.items():
            if v is None: bot.os.environ.pop(k,None)
            else: bot.os.environ[k]=v


def test_jupiter_token_intel_blocks_explicit_suspicious_flag_only():
    class FakeHTTP:
        async def get(self,url,headers=None,params=None,retries=0):
            assert '/tokens/v2/search' in url
            return 200,[{'id':'TOKENMINT','organicScore':12,'organicScoreLabel':'low','isVerified':False,
                         'audit':{'isSus':True,'topHoldersPercentage':44}}]
    old=bot.os.environ.get('JUPITER_API_KEY')
    try:
        bot.os.environ['JUPITER_API_KEY']='jup_test'
        probe=JupiterExecutionProbe(); info=asyncio.run(probe.token_intel(FakeHTTP(),'TOKENMINT'))
        assert info['available'] and info['hard_block'] and info['is_sus'],info
        assert info['organic_score']==12
    finally:
        if old is None: bot.os.environ.pop('JUPITER_API_KEY',None)
        else: bot.os.environ['JUPITER_API_KEY']=old


def test_disabled_structure_lane_can_still_reach_shadow_validation():
    def run(db):
        p=pair(mc=8_000_000,liq=600_000,pc5=1,pc1=2,buys=80,sells=45,turnover=.5)
        ok,blocks,info=bot.capital_edge_gate(db,p,'STRUCTURE ENTRY',80,85)
        assert not ok and any('shadow-only' in x for x in blocks),blocks
        assert info.get('shadow_only') is True,info
    with_db(run)
