import tempfile, time
from pathlib import Path
import bot


def with_db(fn):
    with tempfile.TemporaryDirectory() as td:
        old=bot.DB_PATH; bot.DB_PATH=Path(td)/'v132.db'
        try:
            db=bot.Database(); fn(db); db.conn.close()
        finally:
            bot.DB_PATH=old


def pair(chain='base',token='tok',symbol='TOK',mc=5_000_000,liq=600_000,pc5=1.0,pc1=1.0,
         buys=30,sells=10,vol5=7000,age_min=300,price=.001):
    return {'chainId':chain,'pairCreatedAt':(time.time()-age_min*60)*1000,
            'baseToken':{'address':token,'symbol':symbol,'name':symbol},
            'priceUsd':price,'marketCap':mc,'fdv':mc,'liquidity':{'usd':liq},
            'priceChange':{'m5':pc5,'h1':pc1},'volume':{'m5':vol5,'h1':vol5*12},
            'txns':{'m5':{'buys':buys,'sells':sells}}}


def seed_bad_live_batch(db,tier='MANUAL CHAIN ENTRY'):
    for i in range(bot.LANE_HEALTH_MIN_SIGNALS):
        p=pair(token=f'bad{i}',symbol=f'BAD{i}')
        sid=db.add_signal('EARLY',p,80,tier,['v13.2 governor test'])
        db.conn.execute('''insert or replace into signal_outcomes(
            signal_id,horizon_min,completed_ts,max_return_pct,min_return_pct,final_return_pct)
            values(?,?,?,?,?,?)''',(sid,30,int(time.time()),1.0,-12.0,-5.0))
    db.conn.commit()


def add_shadow_result(db,i,maxret,minret,finalret,tier='MANUAL CHAIN ENTRY'):
    p=pair(token=f'shadow{i}',symbol=f'SH{i}',price=.002+i*1e-6)
    did=db.log_decision(p,'LANE_SHADOW',tier,85,90,['lane health quarantined'],min_seconds=1)
    assert did
    db.conn.execute('''insert or replace into decision_outcomes(
        decision_id,horizon_min,completed_ts,max_return_pct,min_return_pct,final_return_pct)
        values(?,?,?,?,?,?)''',(did,30,int(time.time()),maxret,minret,finalret))
    db.conn.commit()


def test_v132_quarantine_persists_in_state_table():
    def run(db):
        seed_bad_live_batch(db)
        h=bot.lane_health(db,'MANUAL CHAIN ENTRY')
        assert h['status']=='QUARANTINED' and h['paused'],h
        row=db.conn.execute('select state,reason from lane_governor where build_version=? and tier=?',
                            (bot.VERSION,'MANUAL CHAIN ENTRY')).fetchone()
        assert row and row['state']=='QUARANTINED' and 'quality failed' in row['reason'],dict(row) if row else None
    with_db(run)


def test_v132_quarantine_uses_zero_bot_guided_size():
    def run(db):
        seed_bad_live_batch(db)
        assert bot.lane_health(db,'MANUAL CHAIN ENTRY')['paused']
        g=bot.adaptive_dollar_size(db,pair(chain='robinhood'),'MANUAL CHAIN ENTRY',90)
        assert g['suggested_usd']==0.0,g
        assert 'QUARANTINED' in g.get('size_label',''),g
    with_db(run)


def test_v132_bad_shadow_does_not_reopen_lane():
    def run(db):
        seed_bad_live_batch(db); bot.lane_health(db,'MANUAL CHAIN ENTRY')
        for i in range(bot.LANE_HEALTH_RECOVERY_MIN_SIGNALS):
            add_shadow_result(db,i,2.0,-12.0,-4.0)
        h=bot.lane_health(db,'MANUAL CHAIN ENTRY')
        assert h['status']=='QUARANTINED' and h['paused'],h
        assert h['shadow_n']>=bot.LANE_HEALTH_RECOVERY_MIN_SIGNALS,h
    with_db(run)


def test_v132_clean_shadow_returns_lane_to_fresh_probation():
    def run(db):
        seed_bad_live_batch(db); bot.lane_health(db,'MANUAL CHAIN ENTRY')
        for i in range(bot.LANE_HEALTH_RECOVERY_MIN_SIGNALS):
            add_shadow_result(db,i,10.0,-2.0,7.0)
        h=bot.lane_health(db,'MANUAL CHAIN ENTRY')
        assert h['status']=='PROBATION' and not h['paused'],h
        assert h.get('recovered') is True,h
        # Old failed live paths must not instantly re-quarantine the recovered lane.
        h2=bot.lane_health(db,'MANUAL CHAIN ENTRY')
        assert h2['status']=='PROBATION' and not h2['paused'] and h2['live_n']==0,h2
    with_db(run)



def test_v132_same_build_restart_preserves_quarantine_state():
    with tempfile.TemporaryDirectory() as td:
        old=bot.DB_PATH; bot.DB_PATH=Path(td)/'restart.db'
        try:
            db=bot.Database(); seed_bad_live_batch(db); h=bot.lane_health(db,'MANUAL CHAIN ENTRY')
            assert h['status']=='QUARANTINED',h
            db.conn.close()
            db2=bot.Database()
            h2=bot.lane_health(db2,'MANUAL CHAIN ENTRY')
            assert h2['status']=='QUARANTINED' and h2['paused'],h2
            db2.conn.close()
        finally:
            bot.DB_PATH=old


def test_v132_user_facing_version_labels_are_current():
    root=Path(bot.__file__).parent
    assert 'v16.2 QUALITY MEASUREMENT' in (root/'start_mac.command').read_text()
    assert 'v16.2 QUALITY MEASUREMENT' in (root/'doctor.py').read_text()
    assert 'FOMO_BOT_v16_2_QUALITY_MEASUREMENT_FINAL.zip' in (root/'README.md').read_text()

def test_v132_shadow_event_and_export_are_present():
    src=Path(bot.__file__).read_text()
    export=Path(bot.__file__).with_name('export_data.py').read_text()
    assert '"LANE_SHADOW"' in src
    assert "'lane_governor'" in export
    assert bot.VERSION=='v16.2 QUALITY MEASUREMENT'


if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in tests:
        fn(); print(fn.__name__+': PASS')
    print('v13.2 SHADOW RECOVERY tests: PASS')
