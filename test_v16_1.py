import asyncio
import inspect
import time
from pathlib import Path

import bot
import adaptive_engine
from copy_trading import CopyTradeEngine


def fast_pair():
    return {
        'chainId':'solana','priceUsd':'0.001','marketCap':250000,'fdv':250000,
        'pairCreatedAt': int((time.time()-30*60)*1000),
        'liquidity':{'usd':60000},'volume':{'m5':1500},
        'txns':{'m5':{'buys':18,'sells':7}},'priceChange':{'m5':2.0,'h1':8.0},
        'baseToken':{'address':'MintFast111111111111111111111111111111111','symbol':'FAST','name':'Fast Token'}
    }


def test_v161_version_and_velocity_defaults():
    assert bot.VERSION == 'v16.2 QUALITY MEASUREMENT'
    assert bot.PROOF_ELIGIBLE_MIN_SCORE == 55
    assert bot.PROOF_FIRST_MIN_PATHS == 8
    assert bot.ENTRY_STABILITY_MIN_SPAN_SECONDS == 20
    assert bot.FAST_ENTRY_STABILITY_MIN_OBSERVATIONS == 2
    assert bot.FAST_ENTRY_STABILITY_MIN_SPAN_SECONDS == 15


def test_fast_lane_can_pass_basic_safety_while_strict_waits_for_holder():
    p=fast_pair()
    safety={'hard':False,'rug_checked':True,'mint_checked':True,'holder_checked':False}
    strict,_=bot.classify_entry_tier(p,58,63,[],False,rts={'available':True,'tx30':4},safety=safety,scout_move=1)
    fast,reasons=bot.classify_actionable_tier(p,58,63,[],False,rts={'available':True,'tx30':4},safety=safety,scout_move=1)
    assert strict is None
    assert fast == 'FAST ENTRY'
    assert any('fast lane' in x.lower() for x in reasons)


def test_fast_lane_never_bypasses_hard_security():
    p=fast_pair()
    tier,reasons=bot.classify_actionable_tier(p,80,90,[],True,rts={'available':True,'tx30':10},safety={'hard':True,'rug_checked':True},scout_move=0)
    assert tier is None
    assert any('hard security' in x.lower() for x in reasons)


def test_fast_lane_is_not_live_autopilot_tier():
    assert bot._auto_tier_allowed(None,'FAST ENTRY') is False


def test_test_signal_has_small_nonzero_size_without_bankroll(tmp_path, monkeypatch):
    monkeypatch.setattr(bot,'DB_PATH',tmp_path/'state.db')
    db=bot.Database()
    db.set_meta('bankroll_usd','0')
    cal=bot._test_signal_calibration(db,{'suggested_usd':0,'size_reasons':[]},'TEST')
    assert 0 < cal['suggested_usd'] <= bot.TEST_BUY_MAX_USD
    assert cal['size_label']=='TEST SIZE ONLY'
    db.conn.close()


def test_regime_sizing_is_directional_but_capped(monkeypatch):
    base={
        'chainId':'solana','priceUsd':'0.001','marketCap':250000,'fdv':250000,
        'liquidity':{'usd':80000},'volume':{'m5':1500},
        'txns':{'m5':{'buys':20,'sells':10}},'priceChange':{'m5':1.0,'h1':4.0},
    }
    monkeypatch.setattr(adaptive_engine,'confidence_report',lambda *a,**k:{
        'confidence':70,'live_quality':70,'endpoint_severe15_120':20,
        'path_history':{'n':10,'clean15':.4,'severe15':.2},'calibration_mature':False,
        'features':adaptive_engine.pair_metrics(base)
    })
    vals={}
    for regime in ['STRESS','RISK_OFF','NEUTRAL','RISK_ON']:
        vals[regime]=adaptive_engine.dollar_size_guide(None,base,'ENTRY OPTION',70,5,20,30,market_regime=regime)['suggested_usd']
    assert vals['STRESS'] < vals['RISK_OFF'] < vals['NEUTRAL'] <= vals['RISK_ON'] <= 20


def test_http_provider_circuit_opens_after_repeated_failures():
    http=object.__new__(bot.HTTP)
    http._circuits={}
    http._last_network_log={}
    host=next(iter(bot.API_CIRCUIT_HOSTS))
    url='https://'+host+'/test'
    for _ in range(bot.API_CIRCUIT_TRIP_COUNT):
        http._record_circuit(url,bot.API_CIRCUIT_SLOW_SECONDS+0.1,500)
    opened,_,_=http._circuit_open(url)
    assert opened is True


def test_copy_engine_already_runs_independently_from_scanner():
    src=inspect.getsource(bot.main)
    assert 'copy_task = asyncio.create_task(copy_engine.run' in src
    assert 'await cycle(http,guard,limiter,db,state)' in src


def test_real_live_gate_still_fails_closed_before_proof(tmp_path,monkeypatch):
    monkeypatch.setattr(bot,'DB_PATH',tmp_path/'state.db')
    db=bot.Database()
    gate=bot.live_execution_gate(db,include_capacity=False)
    assert not gate['allowed']
    assert any('proof gate' in x for x in gate['reasons'])
    db.conn.close()


def test_fast_stability_uses_two_observations_and_shorter_span():
    now=int(time.time())
    class DB:
        def token_history(self,*a,**k):
            return [
                {'ts':now-20,'price':0.00100,'pc5':0,'buys5':18,'sells5':8,'liquidity':60000},
                {'ts':now-3,'price':0.00101,'pc5':0,'buys5':19,'sells5':8,'liquidity':61000},
            ]
    ok_fast,_,info=bot.entry_stability_check(DB(),fast_pair(),'FAST ENTRY')
    ok_normal,normal_blocks,_=bot.entry_stability_check(DB(),fast_pair(),'ENTRY OPTION')
    assert ok_fast is True and info['span_seconds'] >= 15
    assert ok_normal is False and any('2/3' in x for x in normal_blocks)


def test_fast_lane_softens_breadth_and_history_veto(monkeypatch):
    p=fast_pair()
    class DB: pass
    monkeypatch.setattr(bot,'token_trauma_context',lambda *a,**k:{'blocked':False})
    monkeypatch.setattr(bot,'breadth_guard_context',lambda *a,**k:{'label':'RISK_OFF','risk_off':True,'reason':'broad weakness'})
    monkeypatch.setattr(bot,'historical_edge_context',lambda *a,**k:{'veto':True,'reason':'bad analogs'})
    ok_fast,blocks_fast,info=bot.capital_edge_gate(DB(),p,'FAST ENTRY',58,63)
    ok_strict,blocks_strict,_=bot.capital_edge_gate(DB(),p,'ENTRY OPTION',58,63)
    assert ok_fast is True and not blocks_fast
    assert len(info.get('soft_warnings') or []) == 2
    assert ok_strict is False and blocks_strict


def test_safety_preflight_publishes_basic_cache_before_premium_enrichment(monkeypatch):
    async def run():
        state={'safety_cache':{},'copy_engine':None}
        pair=fast_pair(); token=pair['baseToken']['address']
        class Guard:
            def available(self,*a): return True
        class DB:
            def previous(self,*a): return None
        monkeypatch.setattr(bot,'rugcheck_summary',lambda *a,**k: asyncio.sleep(0,result=(0,['rug ok'],False,True)))
        monkeypatch.setattr(bot,'pair_for_token',lambda *a,**k: asyncio.sleep(0,result=pair))
        monkeypatch.setattr(bot,'early_score',lambda *a,**k:(80,[],[],'SCOUT'))
        monkeypatch.setattr(bot,'solana_mint_authority_context',lambda *a,**k: asyncio.sleep(0,result=(0,['mint ok'],False,True)))
        entered=asyncio.Event(); release=asyncio.Event()
        async def slow_trades(*a,**k):
            entered.set(); await release.wait(); return []
        monkeypatch.setattr(bot,'birdeye_trades',slow_trades)
        monkeypatch.setattr(bot,'wallet_score',lambda *a,**k:(0,[],[]))
        monkeypatch.setattr(bot,'wallet_flow_profile',lambda *a,**k:{'top_buyer_share':0,'unique_buyers':0})
        monkeypatch.setattr(bot,'resilient_holder_context',lambda *a,**k: asyncio.sleep(0,result=(0,[],False,True,'test')))
        monkeypatch.setattr(bot,'token_security',lambda *a,**k: asyncio.sleep(0,result=(0,[],False,True)))
        monkeypatch.setattr(bot,'top_trader_context',lambda *a,**k: asyncio.sleep(0,result=(0,[],[],True)))
        task=asyncio.create_task(bot.preflight_safety(None,Guard(),DB(),'solana',token,state))
        await asyncio.wait_for(entered.wait(),1)
        cached=state['safety_cache'].get('solana:'+token)
        assert cached and cached['rug_checked'] and cached['mint_checked']
        assert cached['enrichment_complete'] is False
        release.set(); final=await task
        assert final['enrichment_complete'] is True
    asyncio.run(run())
