import time
import bot


def _insert_closed(db, *, build, tier='FAST ENTRY', pnl=0.25, amount=5.0, token='MintA'):
    now=int(time.time())
    db.conn.execute("""insert into paper_positions(
        open_ts,close_ts,chain,token,symbol,name,tier,entry_price,close_price,amount_usd,quantity,
        entry_liquidity,peak_price,entry_fee_pct,exit_fee_pct,shadow,active,source_event,
        estimated_friction_pct,build_version,proof_eligible,proof_reason,gross_pnl,fees_usd,realized_pnl)
        values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now-60,now,'solana',token,'T','Test',tier,1.0,1.0,amount,amount,50000,1.0,0,0,1,0,
         'TEST',0,build,1,'test',pnl,0,pnl))
    db.conn.commit()


def test_v162_version_and_quality_cohort():
    assert bot.VERSION == 'v16.2 QUALITY MEASUREMENT'
    assert 'v16.1 VELOCITY EDGE' in bot.QUALITY_COHORT_BUILD_VERSIONS
    assert bot.VERSION in bot.QUALITY_COHORT_BUILD_VERSIONS


def test_fast_entry_counts_in_proof_and_v161_history_carries_forward(tmp_path, monkeypatch):
    monkeypatch.setattr(bot,'DB_PATH',tmp_path/'state.db')
    db=bot.Database()
    _insert_closed(db,build='v16.1 VELOCITY EDGE',tier='FAST ENTRY',token='OldFast')
    _insert_closed(db,build=bot.VERSION,tier='FLOW ENTRY',token='NewFlow')
    rows=db.proof_shadow_rows(20)
    assert {r['token'] for r in rows} >= {'OldFast','NewFlow'}
    stats=db.paper_stats(7,shadow_only=True,build_version=bot.QUALITY_COHORT_BUILD_VERSIONS,proof_only=True)
    assert stats['closed'] >= 2
    db.conn.close()


def test_research_only_tier_does_not_enter_proof_counter(tmp_path, monkeypatch):
    monkeypatch.setattr(bot,'DB_PATH',tmp_path/'state.db')
    db=bot.Database()
    _insert_closed(db,build='v16.1 VELOCITY EDGE',tier='STRUCTURE ENTRY',token='Struct')
    assert all(r['token'] != 'Struct' for r in db.proof_shadow_rows(20))
    db.conn.close()
