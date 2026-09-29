import tempfile, time
from pathlib import Path
import bot


def with_db(fn):
    with tempfile.TemporaryDirectory() as td:
        old=bot.DB_PATH; bot.DB_PATH=Path(td)/'v133.db'
        try:
            db=bot.Database(); fn(db); db.conn.close()
        finally:
            bot.DB_PATH=old


def pair(token='tok',symbol='TOK',price=.001,pc5=2.0,pc1=1.0,mc=250_000,liq=75_000,buys=40,sells=15,vol5=3500):
    return {'chainId':'solana','pairCreatedAt':(time.time()-120*60)*1000,
            'baseToken':{'address':token,'symbol':symbol,'name':symbol},
            'priceUsd':price,'marketCap':mc,'fdv':mc,'liquidity':{'usd':liq},
            'priceChange':{'m5':pc5,'h1':pc1},'volume':{'m5':vol5,'h1':vol5*12},
            'txns':{'m5':{'buys':buys,'sells':sells}}}


def set_signal_age(db, signal_id, age_minutes):
    ts=int(time.time()-age_minutes*60)
    db.conn.execute('update signals set ts=? where id=?',(ts,signal_id)); db.conn.commit()
    return ts


def test_old_build_alert_no_longer_blocks_full_eight_hours():
    def run(db):
        p=pair(token='heehaw',symbol='HeeHaw')
        sid=db.add_signal('EARLY',p,80,'ENTRY OPTION',['old build'])
        set_signal_age(db,sid,120)
        # Explicit older-build ledger row proves this was not a current-build alert.
        db.conn.execute('''insert into decision_ledger(ts,chain,token,symbol,event,tier,entry_score,confirmed_score,
            price,mcap,liquidity,pc5,pc1,buy_sell,swaps,turnover_pct,rt30,social_score,market_regime,sources,blockers,build_version)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (int(time.time()-120*60),'solana','heehaw','HeeHaw','ENTRY_SENT','ENTRY OPTION',80,85,.001,250000,75000,2,1,2,50,1,0,0,'NEUTRAL','','','v13.2 SHADOW RECOVERY'))
        db.conn.commit()
        ctx=bot.entry_cooldown_context(db,'solana','heehaw')
        assert ctx['recent_early'] is None,ctx
        assert ctx['cross_build_reset'] is True,ctx
    with_db(run)


def test_cross_build_grace_still_prevents_upgrade_duplicate():
    def run(db):
        p=pair(token='grace')
        sid=db.add_signal('EARLY',p,80,'ENTRY OPTION',['old build'])
        set_signal_age(db,sid,20)
        ctx=bot.entry_cooldown_context(db,'solana','grace')
        assert ctx['recent_early'] is not None,ctx
        assert ctx['cross_build_grace'] is True,ctx
        assert ctx['cross_build_reset'] is False,ctx
    with_db(run)


def test_same_build_cooldown_survives_restart_window():
    def run(db):
        p=pair(token='same')
        sid=db.add_signal('EARLY',p,80,'ENTRY OPTION',['current build'])
        oldts=set_signal_age(db,sid,120)
        did=db.log_decision(p,'ENTRY_SENT','ENTRY OPTION',80,85,[],min_seconds=1)
        db.conn.execute('update decision_ledger set ts=? where id=?',(oldts,did)); db.conn.commit()
        ctx=bot.entry_cooldown_context(db,'solana','same')
        assert ctx['recent_early'] is not None,ctx
        assert ctx['same_build_entry'] is not None,ctx
        assert not ctx['cross_build_reset'],ctx
    with_db(run)


def seed_stability(db, token, crash_age_min=None, crash_pc5=-18.0):
    now=int(time.time())
    rows=[]
    if crash_age_min is not None:
        rows.append((now-int(crash_age_min*60),'solana',token,'TOK',.00098,250000,75000,3000,30,20,crash_pc5,-40))
    rows += [
        (now-180,'solana',token,'TOK',.00098,250000,75000,3000,35,15,1.0,-3),
        (now-90,'solana',token,'TOK',.00099,250000,75000,3200,40,15,2.0,-1),
        (now-5,'solana',token,'TOK',.00100,250000,75000,3500,42,15,2.0,1),
    ]
    db.conn.executemany('insert or replace into observations values(?,?,?,?,?,?,?,?,?,?,?,?)',rows)
    db.conn.commit()


def test_violent_flush_is_remembered_for_thirty_minutes():
    def run(db):
        seed_stability(db,'zcat',crash_age_min=20,crash_pc5=-18.0)
        ok, blockers, info=bot.entry_stability_check(db,pair(token='zcat'),'ENTRY OPTION')
        assert not ok,(blockers,info)
        assert any('recent crash recovery too fresh' in x for x in blockers),blockers
        assert info.get('recent_crash_pc5') <= -17.5,info
    with_db(run)


def test_old_flush_expires_after_new_crash_window():
    def run(db):
        seed_stability(db,'oldcrash',crash_age_min=31,crash_pc5=-18.0)
        ok, blockers, info=bot.entry_stability_check(db,pair(token='oldcrash'),'ENTRY OPTION')
        assert ok,(blockers,info)
        assert not any('recent crash recovery too fresh' in x for x in blockers),blockers
    with_db(run)


def test_pipeline_diagnostics_count_current_build_only():
    def run(db):
        p=pair(token='diag')
        db.log_decision(p,'ENTRY_STABILITY','ENTRY OPTION',80,85,['test'],min_seconds=1)
        db.log_decision(pair(token='sent'),'ENTRY_SENT','ENTRY OPTION',80,85,[],min_seconds=1)
        db.conn.execute("update decision_ledger set build_version='v13.2 SHADOW RECOVERY' where token='sent'")
        db.conn.commit()
        d=db.entry_pipeline_summary(60)
        assert d.get('ENTRY_STABILITY')==1,d
        assert d.get('ENTRY_SENT',0)==0,d
    with_db(run)


def test_v133_diagnostics_and_branding_present():
    root=Path(bot.__file__).parent
    src=Path(bot.__file__).read_text()
    assert bot.VERSION=='v16.2 QUALITY MEASUREMENT'
    assert 'ENTRY_COOLDOWN' in src and 'ENTRY_STABILITY' in src and 'ENTRY_TAPE' in src and 'ENTRY_COMMIT' in src
    assert 'why_coin_report' in src
    assert 'v16.2 QUALITY MEASUREMENT' in (root/'start_mac.command').read_text()
    assert 'v16.2 QUALITY MEASUREMENT' in (root/'doctor.py').read_text()
    assert 'FOMO_BOT_v16_2_QUALITY_MEASUREMENT_FINAL.zip' in (root/'README.md').read_text()


if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in tests:
        fn(); print(fn.__name__+': PASS')
    print('v13.3 EDGE RECOVERY tests: PASS')
