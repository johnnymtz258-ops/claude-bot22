import tempfile, time
from pathlib import Path
import bot
from adaptive_engine import position_state


def with_db(fn):
    with tempfile.TemporaryDirectory() as td:
        old=bot.DB_PATH; bot.DB_PATH=Path(td)/'v131.db'
        try:
            db=bot.Database(); fn(db); db.conn.close()
        finally:
            bot.DB_PATH=old


def pair(chain='solana',token='tok',symbol='TOK',mc=400_000,liq=60_000,pc5=1.0,pc1=1.0,
         buys=30,sells=15,vol5=2000,age_min=300,price=.001):
    return {'chainId':chain,'pairCreatedAt':(time.time()-age_min*60)*1000,
            'baseToken':{'address':token,'symbol':symbol,'name':symbol},
            'priceUsd':price,'marketCap':mc,'fdv':mc,'liquidity':{'usd':liq},
            'priceChange':{'m5':pc5,'h1':pc1},'volume':{'m5':vol5,'h1':vol5*12},
            'txns':{'m5':{'buys':buys,'sells':sells}}}

SAFE={'hard':False,'rug_checked':True,'holder_checked':True}


def test_v13_bad_4_crosschain_is_blocked():
    p=pair(chain='bsc',mc=16_257_845,liq=1_275_530,pc5=.66,pc1=-5.09,buys=32,sells=18,vol5=5981)
    tier,reasons=bot.classify_entry_tier(p,78.13,78.13,[],False,safety={'hard':False},scout_move=.62)
    assert tier is None,(tier,reasons)


def test_bluechip_chase_bug_is_blocked():
    p=pair(chain='base',mc=12_893_420,liq=451_835,pc5=3.06,pc1=-2.4,buys=13,sells=1,vol5=3404)
    tier,reasons=bot.classify_entry_tier(p,83.42,97.42,[],False,safety={'hard':False},scout_move=14.37)
    assert tier is None,(tier,reasons)
    assert any('watch-only' in x.lower() or 'scout' in x.lower() or 'strict' in x.lower() for x in reasons),reasons


def test_jimothy_downtrend_structure_is_blocked():
    p=pair(mc=16_452_331,liq=1_014_045,pc5=.13,pc1=-7.56,buys=22,sells=16,vol5=11840,age_min=7*24*60)
    tier,reasons=bot.classify_entry_tier(p,82.30,82.30,[],False,rts={'available':True,'tx30':253},safety=SAFE,scout_move=-.06)
    assert tier is None,(tier,reasons)


def test_triplet_clean_structure_is_preserved():
    p=pair(mc=9_827_696,liq=654_312,pc5=.03,pc1=-4.11,buys=80,sells=33,vol5=39870,age_min=7*24*60)
    tier,reasons=bot.classify_entry_tier(p,100,100,[],False,rts={'available':True,'tx30':1389},safety=SAFE,scout_move=-.43)
    assert tier=='STRUCTURE ENTRY',(tier,reasons)


def test_standard_deep_downtrend_entries_are_blocked():
    hee=pair(mc=405_633,liq=59_541,pc5=4.75,pc1=-16.96,buys=51,sells=38,vol5=4118)
    tier,_=bot.classify_entry_tier(hee,50.45,53.45,[],False,rts={'available':False},safety=SAFE,scout_move=0)
    assert tier is None
    leafy=pair(mc=377_870,liq=54_137,pc5=2.92,pc1=-14.36,buys=8,sells=2,vol5=650)
    tier,_=bot.classify_entry_tier(leafy,50.86,51.86,[],False,rts={'available':False},safety=SAFE,scout_move=0)
    assert tier is None


def test_old_leafy_recovery_pocket_is_preserved():
    p=pair(mc=335_264,liq=51_075,pc5=2.89,pc1=-8.53,buys=15,sells=6,vol5=947)
    tier,reasons=bot.classify_entry_tier(p,83.09,84.09,[],False,rts={'available':True,'tx30':3},safety=SAFE,scout_move=0)
    assert tier in {'ENTRY OPTION','STRONG ENTRY'},(tier,reasons)


def test_popape_shape_still_qualifies():
    p=pair(mc=387_862,liq=55_013,pc5=-.45,pc1=-3.66,buys=82,sells=27,vol5=5596)
    tier,reasons=bot.classify_entry_tier(p,64.88,63.88,[],False,rts={'available':True,'tx30':5},safety=SAFE,scout_move=0)
    assert tier is not None,(tier,reasons)


def test_gg_crosschain_shapes_remain_watch_only_in_v15():
    early=pair(chain='robinhood',mc=8_886_000,liq=168_035,pc5=.91,pc1=-1.92,buys=10,sells=6,vol5=4408,age_min=7*24*60)
    tier,_=bot.classify_entry_tier(early,72.7,72.7,[],False,safety={'hard':False},scout_move=0)
    assert tier is None
    breakout=pair(chain='robinhood',mc=9_556_061,liq=547_902,pc5=5.06,pc1=14.1,buys=3,sells=2,vol5=2683,age_min=7*24*60)
    tier,_=bot.classify_entry_tier(breakout,78.4,86.4,[],False,safety={'hard':False},scout_move=0)
    assert tier is None


def test_experimental_lane_health_quarantines_bad_batch():
    def run(db):
        for i in range(6):
            p=pair(chain='base',token=f'x{i}',symbol=f'X{i}',mc=5_000_000,liq=600_000,pc5=1,pc1=1,buys=30,sells=10,vol5=7000)
            sid=db.add_signal('EARLY',p,80,'MANUAL CHAIN ENTRY',['test'])
            db.conn.execute('insert or replace into signal_outcomes(signal_id,horizon_min,completed_ts,max_return_pct,min_return_pct,final_return_pct) values(?,?,?,?,?,?)',
                            (sid,30,int(time.time()),1.0,-12.0,-5.0))
        db.conn.commit()
        health=bot.lane_health(db,'MANUAL CHAIN ENTRY')
        assert health['paused'] and health['status']=='QUARANTINED',health
    with_db(run)


def test_experimental_lane_probation_caps_size():
    def run(db):
        p=pair(chain='robinhood',mc=8_886_000,liq=168_035,pc5=.91,pc1=-1.92,buys=10,sells=6,vol5=4408)
        g=bot.adaptive_dollar_size(db,p,'MANUAL CHAIN ENTRY',90)
        assert g['suggested_usd']<=bot.MANUAL_CHAIN_PROBATION_MAX_SIZE_USD+1e-9,g
        assert g['lane_health']['status']=='PROBATION',g
    with_db(run)


def test_fee_aware_tp1_scales_with_small_position():
    assert bot.fee_aware_tp1(8,5) >= 15
    assert bot.fee_aware_tp1(8,10) >= 10
    assert abs(bot.fee_aware_tp1(8,20)-8)<1e-9


def test_tiny_take_partial_becomes_full_profit_lock():
    p=pair(price=.00118,mc=400_000,liq=60_000,pc5=1,pc1=2,buys=30,sells=10,vol5=3000)
    pos={'entry_price':.001,'peak_price':.00118,'entry_liquidity':60_000,'open_ts':time.time()-20*60,
         'amount_usd':5.0,'remaining_fraction':1.0}
    info=position_state(p,pos,tp1=15,tp2=35,min_partial_sale_usd=5,round_trip_friction=5)
    assert info['state']=='TAKE_PARTIAL',info
    assert info['suggested_partial_pct']==100,info
    assert 'full' in info['action'].lower(),info


def test_manual_chain_profit_targets_are_fee_less_silly():
    assert bot.tier_profit_targets('MANUAL CHAIN ENTRY')==(8.0,16.0)


if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in tests:
        fn(); print(fn.__name__+': PASS')
    print('v13.1 QUALITY GOVERNOR tests: PASS')
