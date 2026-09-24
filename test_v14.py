import asyncio
import json
import tempfile
import time
from pathlib import Path

import bot
from adaptive_engine import position_state


def with_db(fn):
    with tempfile.TemporaryDirectory() as td:
        old = bot.DB_PATH
        bot.DB_PATH = Path(td) / 'v14.db'
        try:
            db = bot.Database()
            fn(db)
            db.conn.close()
        finally:
            bot.DB_PATH = old


def pair(token='tok', symbol='TOK', price=.001, mc=420_000, liq=84_000,
         pc5=1.0, pc1=3.0, buys=60, sells=40, turnover=.50, chain='solana'):
    vol5 = mc * turnover / 100.0
    return {
        'chainId': chain,
        'pairCreatedAt': (time.time()-120*60)*1000,
        'baseToken': {'address':token,'symbol':symbol,'name':symbol},
        'priceUsd': price, 'marketCap': mc, 'fdv':mc,
        'liquidity': {'usd':liq},
        'priceChange': {'m5':pc5,'h1':pc1,'h6':0,'h24':0},
        'volume': {'m5':vol5,'h1':vol5*12},
        'txns': {'m5': {'buys':buys,'sells':sells}},
    }


def test_capital_edge_keeps_deep_active_normal_shape():
    def run(db):
        p=pair(token='macro',symbol='MACRODUCK',mc=420_181,liq=81_964,pc5=-1.05,pc1=4.31,
               buys=62,sells=43,turnover=1.10)
        ok, blocks, info=bot.capital_edge_gate(db,p,'ENTRY OPTION',80,85)
        assert ok,(blocks,info)
        assert info['liq_mc'] >= .15 and info['turnover_pct'] >= .20
    with_db(run)


def test_v15_does_not_reuse_v14_static_shape_box_as_positive_model():
    # The full Sep-9 holdout showed the v14 static box did not generalize.  v15's
    # capital edge layer is a veto layer; these snapshots must be judged later by
    # path/history/breadth/execution rather than hard-coded old shape thresholds.
    def run(db):
        ape=pair(token='ape',symbol='APEUS',mc=2_093_345,liq=136_175,pc5=1.98,pc1=-4.91,
                 buys=1247,sells=654,turnover=7.70)
        hee=pair(token='hee',symbol='HeeHaw',mc=3_042_670,liq=182_273,pc5=3.67,pc1=2.92,
                 buys=68,sells=51,turnover=.52)
        for p in (ape,hee):
            ok, blocks, info=bot.capital_edge_gate(db,p,'ENTRY OPTION',80,85)
            assert ok,(p['baseToken']['symbol'],blocks,info)
    with_db(run)

def test_token_trauma_blocks_repeat_severe_contract():
    def run(db):
        p=pair(token='apeus',symbol='APEUS')
        did=db.log_decision(p,'ENTRY_SENT','ENTRY OPTION',80,85,[],min_seconds=1)
        old=int(time.time()-600)
        db.conn.execute('update decision_ledger set ts=? where id=?',(old,did))
        db.conn.execute('''insert into decision_outcomes(decision_id,horizon_min,completed_ts,max_return_pct,min_return_pct,final_return_pct)
                           values(?,?,?,?,?,?)''',(did,30,int(time.time()),8.0,-31.0,-23.0))
        db.conn.commit()
        trauma=bot.token_trauma_context(db,'solana','apeus')
        assert trauma['blocked'] and trauma['min_return_pct'] <= -15,trauma
        ok,blocks,_=bot.capital_edge_gate(db,p,'ENTRY OPTION',80,85)
        assert not ok and any('trauma' in x for x in blocks),(blocks,trauma)
    with_db(run)


def test_structure_and_cross_chain_buy_lanes_default_to_watch_only():
    def run(db):
        p=pair(token='struct',mc=10_000_000,liq=1_000_000,turnover=.5)
        ok,b,_=bot.capital_edge_gate(db,p,'STRUCTURE ENTRY',80,85)
        assert not ok and any('shadow-only' in x for x in b),b
        q=pair(token='cross',chain='base',mc=5_000_000,liq=800_000,turnover=.5)
        ok,b,_=bot.capital_edge_gate(db,q,'MANUAL CHAIN ENTRY',80,85)
        assert not ok and any('watch-only' in x for x in b),b
    with_db(run)


def test_all_real_normal_entries_need_persistence_confirmation():
    def run(db):
        p=pair(token='persist',price=.001)
        ok,b,info=bot.capital_confirmation_gate(db,p,'STRONG ENTRY')
        assert not ok and info['required'] and any('confirmation' in x for x in b),b
        sig=db.recent_signal('solana','persist','CAPITAL_PENDING',hours=.2)
        assert sig
        db.conn.execute('update signals set ts=? where id=?',(int(time.time()-bot.SOL_EDGE_CONFIRM_SECONDS-5),sig['id']))
        db.conn.commit()
        p2=pair(token='persist',price=.00101,liq=84_000)
        ok,b,info=bot.capital_confirmation_gate(db,p2,'STRONG ENTRY')
        assert ok,(b,info)
        assert info['age_seconds'] >= bot.SOL_EDGE_CONFIRM_SECONDS and 0 <= info['move_pct'] <= 3
    with_db(run)


def test_fast_lane_entry_counts_as_same_build_cooldown():
    def run(db):
        p=pair(token='hot')
        did=db.log_decision(p,'ENTRY_SENT_HOT','ENTRY OPTION',80,85,[],min_seconds=1)
        old=int(time.time()-120)
        db.conn.execute('update decision_ledger set ts=? where id=?',(old,did)); db.conn.commit()
        row=db.recent_entry_decision('solana','hot',hours=8,build_version=bot.VERSION)
        assert row and row['event']=='ENTRY_SENT_HOT',row
    with_db(run)


def test_persisted_old_size_preferences_cannot_bypass_v14_caps():
    def run(db):
        db.set_meta('size_guide_min_usd','5')
        db.set_meta('size_guide_normal_max_usd','20')
        db.set_meta('size_guide_exceptional_max_usd','30')
        assert bot.sizing_preferences(db)==(5.0,10.0,15.0)
    with_db(run)


def state_pair(price=1.0,liq=100_000,pc5=0,pc1=0,buys=55,sells=45):
    return {'chainId':'solana','baseToken':{'address':'x','symbol':'X'},'priceUsd':price,
            'marketCap':500_000,'liquidity':{'usd':liq},'volume':{'m5':5000},
            'txns':{'m5':{'buys':buys,'sells':sells}},'priceChange':{'m5':pc5,'h1':pc1}}


def state_pos(amount=10,entry=1.0,peak=1.0,tp1_sent=0):
    return {'entry_price':entry,'peak_price':peak,'entry_liquidity':100_000,'amount_usd':amount,
            'remaining_fraction':1.0,'tp1_sent':tp1_sent,'open_ts':int(time.time()-300)}


def test_legacy_twenty_percent_small_stop_is_capped_at_eight():
    info=position_state(state_pair(price=.919),state_pos(amount=10),risk_line=10,small_hard_stop=20,mid_hard_stop=16)
    assert info['price_hard_stop'] == 8.0,info
    assert info['state']=='EXIT_REVIEW',info


def test_small_position_banks_full_first_target():
    info=position_state(state_pair(price=1.13,pc5=1),state_pos(amount=10),tp1=12,tp2=25)
    assert info['state']=='TAKE_PARTIAL',info
    assert info['suggested_partial_pct']==100,info
    assert info['remaining_value_usd']==0,info


def test_high_water_giveback_escalates_to_full_profit_lock():
    info=position_state(state_pair(price=1.11,pc5=-2),state_pos(amount=10,peak=1.35),tp1=12,tp2=25)
    assert info['state']=='EXIT_REVIEW',info
    assert info['profit_lock_trigger'],info
    assert info['suggested_partial_pct']==100,info


def test_alert_limiter_reservation_can_be_released_after_delivery_failure():
    lim=bot.AlertLimiter(); key='entry:solana:tok'
    assert lim.allowed_action(key,cooldown=3600)
    assert not lim.allowed_action(key,cooldown=3600)
    assert lim.release_action(key)
    assert lim.allowed_action(key,cooldown=3600)


def test_telegram_send_retries_and_confirms_delivery():
    class FakeHTTP:
        def __init__(self): self.calls=0
        async def post(self,url,payload,retries=0):
            self.calls+=1
            if self.calls<3: return 0,'temporary network error'
            return 200,json.dumps({'ok':True,'result':{'message_id':12345}})
    old_tg,old_chat=bot.TG,bot.CHAT
    try:
        bot.TG='test-token'; bot.CHAT='1'; bot.TG_CONSEC_FAILURES=0
        http=FakeHTTP(); mid=asyncio.run(bot.send(http,'delivery test'))
        assert mid==12345 and http.calls==3
        assert bot.TG_CONSEC_FAILURES==0 and bot.TG_LAST_SUCCESS_TS>0
    finally:
        bot.TG,bot.CHAT=old_tg,old_chat


def test_v14_branding_and_capital_controls_are_packaged():
    root=Path(bot.__file__).parent
    assert bot.VERSION=='v16.2 QUALITY MEASUREMENT'
    src=Path(bot.__file__).read_text()
    for needle in ['def capital_edge_gate','def capital_confirmation_gate','def token_trauma_context',
                   'ENTRY_DELIVERY_FAIL','telegram_delivery_status','CAPITAL_HARD_STOP_PCT']:
        assert needle in src,needle
    assert 'v16.2 QUALITY MEASUREMENT' in (root/'start_mac.command').read_text()
    assert 'v16.2 QUALITY MEASUREMENT' in (root/'doctor.py').read_text()
    assert 'FOMO_BOT_v16_2_QUALITY_MEASUREMENT_FINAL.zip' in (root/'README.md').read_text()

def _add_capital_path(db,i,maxret,minret,finalret,event='ENTRY_SENT'):
    p=pair(token=f'cap{i}_{event}',symbol=f'C{i}')
    did=db.log_decision(p,event,'ENTRY OPTION',80,85,[],min_seconds=1)
    db.conn.execute('''insert or replace into decision_outcomes(
        decision_id,horizon_min,completed_ts,max_return_pct,min_return_pct,final_return_pct)
        values(?,?,?,?,?,?)''',(did,30,int(time.time()),maxret,minret,finalret))
    db.conn.commit()


def test_capital_core_quarantines_bad_fresh_build_cohort():
    def run(db):
        for i in range(bot.CAPITAL_CORE_MIN_PATHS):
            _add_capital_path(db,i,2.0,-12.0,-4.0)
        h=bot.capital_core_health(db)
        assert h['status']=='QUARANTINED' and h['paused'],h
        assert h['median_max'] < bot.CAPITAL_CORE_MIN_MEDIAN_MAX,h
    with_db(run)


def test_capital_core_recovers_only_from_clean_shadow_paths():
    def run(db):
        for i in range(bot.CAPITAL_CORE_MIN_PATHS):
            _add_capital_path(db,i,2.0,-12.0,-4.0)
        assert bot.capital_core_health(db)['paused']
        for i in range(bot.CAPITAL_CORE_RECOVERY_PATHS):
            _add_capital_path(db,100+i,12.0,-2.0,8.0,event='CAPITAL_SHADOW')
        h=bot.capital_core_health(db)
        assert h['status']=='PROBATION' and not h['paused'] and h.get('recovered'),h
    with_db(run)


def test_capital_core_probation_caps_normal_guidance_at_five():
    def run(db):
        g=bot.adaptive_dollar_size(db,pair(token='prob'),'ENTRY OPTION',90)
        assert g['capital_core_health']['status']=='PROBATION',g
        assert g['suggested_usd'] <= bot.CAPITAL_CORE_PROBATION_SIZE_USD,g
    with_db(run)
