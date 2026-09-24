import asyncio, json, os, time
from pathlib import Path
import bot
from adaptive_engine import dollar_size_guide, position_state
from pumpportal_radar import PumpPortalRadar


def pair(price=1.0,liq=150000,mcap=500000,vol5=8000,buys=90,sells=45,pc5=2,pc1=6,token='Edge111111111111111111111111111111111111'):
    return {'chainId':'solana','baseToken':{'address':token,'symbol':'EDGE','name':'EDGE'},'priceUsd':price,
            'marketCap':mcap,'liquidity':{'usd':liq},'volume':{'m5':vol5,'h1':50000},
            'txns':{'m5':{'buys':buys,'sells':sells}},'priceChange':{'m5':pc5,'h1':pc1,'h6':0,'h24':0}}


def test_flow_concentration_penalty():
    now=time.time()
    trades=[{'owner':'whale','side':'buy','time':now,'usd':1800},{'owner':'a','side':'buy','time':now,'usd':100},{'owner':'b','side':'buy','time':now,'usd':100}]
    score,reasons,risks=bot.wallet_score(trades)
    assert any('concentrated' in x for x in risks), risks
    prof=bot.wallet_flow_profile(trades)
    assert prof['top_buyer_share'] >= .89, prof


def test_source_convergence_excludes_paid_boost():
    state={}
    bot.mark_candidate_source(state,'solana','T','DexScreener boost')
    a=bot.candidate_source_context(state,'solana','T')
    assert a['paid_boost'] and a['bonus']==0, a
    bot.mark_candidate_source(state,'solana','T','PumpPortal migration')
    bot.mark_candidate_source(state,'solana','T','social exact-contract')
    b=bot.candidate_source_context(state,'solana','T')
    assert b['bonus']>0 and 'DEX_BOOST' not in b['buckets'], b


def test_social_pulse_penalizes_single_author(tmp_token='Soc1111111111111111111111111111111111111'):
    db=bot.Database()
    db.conn.execute('delete from social_events where token=?',(tmp_token,)); db.conn.commit()
    for i in range(4): db.add_social_event('X',f'post{i}',tmp_token,author='same',url=f'https://x/{i}',text='x')
    p=db.social_pulse(tmp_token,20)
    assert p['top_author_share']==1.0 and p['score']<0, p
    db.conn.execute('delete from social_events where token=?',(tmp_token,)); db.conn.commit(); db.conn.close()


def test_market_regime_and_concentration_reduce_size():
    # Empty database is fine: calibration is learning/reduction-only.
    db=bot.Database()
    neutral=bot.adaptive_dollar_size(db,pair(),'ENTRY OPTION',75,market_regime='NEUTRAL',safety={'flow_concentration':0})
    stress=bot.adaptive_dollar_size(db,pair(),'ENTRY OPTION',75,market_regime='STRESS',safety={'flow_concentration':.80})
    assert stress['suggested_usd'] <= neutral['suggested_usd'], (neutral,stress)
    db.conn.close()


def test_pumpportal_records_migration():
    p=PumpPortalRadar(); mint='11111111111111111111111111111111'
    p._record(mint,'MIGRATION')
    assert mint in p.candidate_mints() and p.is_migration(mint)


def test_mint_authority_hard_block():
    class H:
        async def post(self,url,payload,retries=0):
            return 200,json.dumps({'result':{'value':{'data':{'parsed':{'info':{'mintAuthority':None,'freezeAuthority':'abc'}}}}}})
    score,notes,hard,checked=asyncio.run(bot.solana_mint_authority_context(H(),'x'))
    assert hard and checked and score<0 and any('freeze authority' in x for x in notes), (score,notes,hard,checked)


def test_decision_ledger_schema_and_outcome_function():
    db=bot.Database()
    for table in ('decision_ledger','decision_outcomes','social_events'):
        assert db.conn.execute("select 1 from sqlite_master where type='table' and name=?",(table,)).fetchone(), table
    db.conn.close()


def test_early_thesis_warning_and_earlier_giveback():
    pos={'entry_price':1.0,'peak_price':1.16,'entry_liquidity':100000,'amount_usd':20,'remaining_fraction':1.0,
         'tp1_sent':1,'open_ts':int(time.time())-300}
    info=position_state(pair(price=1.08,liq=100000,buys=60,sells=50,pc5=-1),pos,tp1=15,tp2=35,min_partial_sale_usd=5)
    assert info['state']=='EXIT_REVIEW' and 'PROFIT LOCK' in info['action'], info
    fresh=dict(pos,peak_price=1.0,tp1_sent=0,open_ts=int(time.time())-180)
    info2=position_state(pair(price=.94,liq=89000,buys=30,sells=50,pc5=-5),fresh,tp1=15,tp2=35,min_partial_sale_usd=5)
    assert info2['state']=='WEAKENING' and any('early entry thesis' in x for x in info2['reasons']), info2


if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in tests:
        fn(); print(fn.__name__+': PASS')
    print('v12 EDGE ENGINE tests: PASS')
