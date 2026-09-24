import tempfile
from pathlib import Path
import bot


def with_db(fn):
    with tempfile.TemporaryDirectory() as td:
        old=bot.DB_PATH; bot.DB_PATH=Path(td)/'v13.db'
        try:
            db=bot.Database(); fn(db); db.conn.close()
        finally:
            bot.DB_PATH=old


def mkpair(chain='solana', token='tok', symbol='TOK', mc=46_664_264, liq=2_112_238,
           pc5=.91, pc1=-4.07, buys=40, sells=20, vol5=8823, age_min=7*24*60):
    import time
    return {'chainId':chain,'pairCreatedAt':(time.time()-age_min*60)*1000,
            'baseToken':{'address':token,'symbol':symbol,'name':symbol},
            'priceUsd':.001,'marketCap':mc,'fdv':mc,'liquidity':{'usd':liq},
            'priceChange':{'m5':pc5,'h1':pc1},'volume':{'m5':vol5,'h1':vol5*12},
            'txns':{'m5':{'buys':buys,'sells':sells}}}


def test_cate_like_established_structure_lane():
    p=mkpair()
    tier,reasons=bot.classify_entry_tier(p,84.6,90.6,[],False,rts={'available':True,'tx30':5},safety={'hard':False})
    assert tier=='STRUCTURE ENTRY',(tier,reasons)


def test_gg_like_cross_chain_flow_lane():
    p=mkpair(chain='robinhood',mc=8_886_000,liq=168_035,pc5=.91,pc1=-1.92,buys=10,sells=6,vol5=4408)
    tier,reasons=bot.classify_entry_tier(p,72.7,72.7,[],False,safety={'hard':False})
    assert tier is None and any('watch-only' in x.lower() for x in reasons),(tier,reasons)


def test_gg_like_cross_chain_breakout_lane():
    # Later GG snapshot: deep liquidity + breakout, but only five 5m swaps.
    p=mkpair(chain='robinhood',mc=9_556_061,liq=547_902,pc5=5.06,pc1=14.1,buys=3,sells=2,vol5=2683)
    tier,reasons=bot.classify_entry_tier(p,78.4,86.4,[],False,safety={'hard':False})
    assert tier is None and any('watch-only' in x.lower() for x in reasons),(tier,reasons)


def test_cross_chain_weak_snapshot_stays_blocked():
    p=mkpair(chain='robinhood',mc=9_000_000,liq=160_000,pc5=.5,pc1=2,buys=5,sells=5,vol5=500)
    tier,_=bot.classify_entry_tier(p,75,80,[],False,safety={'hard':False})
    assert tier is None


def test_tier_targets_and_risk_are_not_launch_defaults():
    assert bot.tier_profit_targets('STRUCTURE ENTRY')==(8.0,18.0)
    assert bot.tier_profit_targets('MANUAL CHAIN ENTRY')==(8.0,16.0)
    assert bot.tier_risk_profile('MANUAL CHAIN ENTRY')['small_hard']==7.0
    assert bot.tier_risk_profile('STRUCTURE ENTRY')['small_hard']==8.0


def test_execution_truth_cash_only_stays_verified():
    def run(db):
        sig={'chain':'solana','token':'tok','symbol':'TOK','name':'TOK','action':'ENTRY OPTION'}
        pid,_=db.record_position(sig,1,10,80_000,manual_source='BOT_ALERT')
        db.partial_close_position_cash(pid,.30,4.0)
        total,leg=db.close_position_cash(pid,8.0)
        row=db.conn.execute('select * from positions where id=?',(pid,)).fetchone()
        assert row['pnl_quality']=='CASH_VERIFIED',dict(row)
        assert abs(total-2.0)<1e-9,(total,leg)
    with_db(run)


def test_execution_truth_mixed_does_not_claim_cash_verified():
    def run(db):
        sig={'chain':'solana','token':'tok','symbol':'TOK','name':'TOK','action':'ENTRY OPTION'}
        pid,_=db.record_position(sig,1,10,80_000,manual_source='BOT_ALERT')
        db.partial_close_position(pid,1.1,.30)  # quote-estimated leg
        db.close_position_cash(pid,8.0)        # actual final leg
        row=db.conn.execute('select * from positions where id=?',(pid,)).fetchone()
        assert row['pnl_quality']=='MIXED',dict(row)
    with_db(run)


def test_undo_restores_accidental_telegram_sell():
    def run(db):
        sig={'chain':'solana','token':'tok','symbol':'TOK','name':'TOK','action':'ENTRY OPTION'}
        pid,_=db.record_position(sig,1,14.70,80_000,manual_source='BOT_ALERT')
        before=dict(db.conn.execute('select * from positions where id=?',(pid,)).fetchone())
        aid=db.journal_snapshot(pid,'SELL_ESTIMATED',before=before)
        db.close_position(pid,.95); db.journal_finish(aid,pid)
        assert not db.position_by_token('tok')
        ok,msg=db.undo_last_journal_action(); assert ok,msg
        restored=db.position_by_token('tok')
        assert restored and abs(restored['amount_usd']-14.70)<1e-9 and restored['active']==1
    with_db(run)


def test_reconcile_overwrites_bad_quote_estimate_with_actual_cash():
    def run(db):
        sig={'chain':'robinhood','token':'gg','symbol':'GG','name':'GG','action':'MANUAL'}
        pid,_=db.record_position(sig,1,30.14,180_000,manual_source='MANUAL_EXPLICIT')
        db.close_position(pid,.90) # deliberately wrong estimate
        ok,res=db.reconcile_closed_cash('gg',30.14,31.43); assert ok,res
        row=db.conn.execute('select * from positions where id=?',(pid,)).fetchone()
        assert row['pnl_quality']=='CASH_VERIFIED'
        assert abs(row['realized_pnl']-1.29)<1e-9
    with_db(run)


def test_sizing_context_ignores_estimated_and_mixed_closes():
    def run(db):
        sig={'chain':'solana','token':'a','symbol':'A','name':'A','action':'ENTRY OPTION'}
        p1,_=db.record_position(sig,1,10,80_000); db.close_position(p1,.5)
        sig2={'chain':'solana','token':'b','symbol':'B','name':'B','action':'ENTRY OPTION'}
        p2,_=db.record_position(sig2,1,10,80_000); db.close_position_cash(p2,12)
        ctx=bot.sizing_risk_context(db)
        assert abs(ctx['realized_24h']-2)<1e-9,ctx
        assert ctx['losing_streak']==0,ctx
    with_db(run)


def test_decision_ledger_is_version_aware():
    def run(db):
        p=mkpair(token='manual')
        did=db.log_decision(p,'MANUAL_BUY','MANUAL OBSERVED',70,75,['user buy'],min_seconds=1)
        row=db.conn.execute('select * from decision_ledger where id=?',(did,)).fetchone()
        assert row['build_version']==bot.VERSION
        runrow=db.conn.execute('select * from runs order by id desc limit 1').fetchone()
        assert runrow['version']==bot.VERSION
    with_db(run)


def test_v15_defaults_to_solana_only_actionable_scan():
    assert bot.CHAINS == ['solana']
    assert bot.MANUAL_ENTRY_CHAINS.issubset(set(bot.CHAINS))


def test_static_v13_commands_and_export():
    src=Path(bot.__file__).read_text()
    for needle in ['/undo','/reconcile TICKER CASH_IN CASH_OUT','MANUAL_BUY','STRUCTURE ENTRY','MANUAL CHAIN ENTRY']:
        assert needle in src,needle
    exp=(Path(bot.__file__).parent/'export_data.py').read_text()
    for table in ["'positions'","'runs'","'journal_actions'"]:
        assert table in exp,table


if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in tests:
        fn(); print(fn.__name__+': PASS')
    print('v13.0 STABLE EDGE tests: PASS')
