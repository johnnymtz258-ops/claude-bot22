import tempfile
from pathlib import Path

import bot
from edge_engine import proof_metrics


def temp_db():
    td=tempfile.TemporaryDirectory(); old=bot.DB_PATH; bot.DB_PATH=Path(td.name)/'state.db'
    db=bot.Database()
    return td,old,db


def close_temp(td,old,db):
    try: db.conn.close()
    finally:
        bot.DB_PATH=old; td.cleanup()


def sig(token):
    return {'id':None,'chain':'solana','token':token,'symbol':token.upper(),'name':token}


def test_v152_brand_schema_and_export_surface():
    assert bot.VERSION=='v16.2 QUALITY MEASUREMENT'
    td,old,db=temp_db()
    try:
        cols={r[1] for r in db.conn.execute("pragma table_info(paper_positions)")}
        assert {'proof_eligible','proof_reason'} <= cols
    finally: close_temp(td,old,db)
    export=Path(bot.__file__).with_name('export_data.py').read_text()
    assert "'paper_positions'" in export and "'decision_dedupe'" in export


def test_v152_selective_proof_floor_separates_research_from_live_proof():
    ok,why=bot.proof_candidate_eligibility('ENTRY OPTION',bot.PROOF_ELIGIBLE_MIN_SCORE)
    assert ok and 'selective-proof floor' in why
    low,_=bot.proof_candidate_eligibility('ENTRY OPTION',bot.PROOF_ELIGIBLE_MIN_SCORE-0.1)
    structure,_=bot.proof_candidate_eligibility('STRUCTURE ENTRY',99)
    assert not low and not structure


def test_v152_proof_requires_sample_diversity_and_net_roi():
    # Profitable repeated trades in one token are not independent proof.
    repeated=[{'realized_pnl':.50,'amount_usd':5,'token':'same'} for _ in range(12)]
    h=proof_metrics(repeated,min_paths=12,min_unique_tokens=6,min_net_roi_pct=1.0)
    assert h['status']=='PROBATION' and h['unique_tokens']==1
    diverse=[{'realized_pnl':.30,'amount_usd':5,'token':f't{i}'} for i in range(7)] + \
            [{'realized_pnl':-.10,'amount_usd':5,'token':f't{i}'} for i in range(7,12)]
    h2=proof_metrics(diverse,min_paths=12,min_unique_tokens=6,min_net_roi_pct=1.0)
    assert h2['status']=='ACTIVE' and h2['net_roi_pct']>1.0
    tiny=[{'realized_pnl':.01,'amount_usd':5,'token':f'x{i}'} for i in range(12)]
    h3=proof_metrics(tiny,min_paths=12,min_unique_tokens=6,min_net_roi_pct=1.0)
    assert h3['status']=='QUARANTINED' and h3['net_roi_pct']<1.0


def test_v152_research_losses_do_not_poison_proof_or_risk_circuit():
    td,old,db=temp_db()
    try:
        for i in range(bot.PAPER_LOSS_STREAK_LIMIT+2):
            pid=db.paper_open(sig(f'r{i}'),1,100000,'STRUCTURE ENTRY',amount=5,shadow=True,
                              proof_eligible=False,proof_reason='research only')
            db.paper_close(pid,.90,'research loss')
        assert db.proof_shadow_rows(50)==[]
        rg=bot.paper_risk_guard(db)
        assert not rg['paused'] and rg['losing_streak']==0
        # The same loss sequence in the proof cohort must trip the circuit.
        for i in range(bot.PAPER_LOSS_STREAK_LIMIT):
            pid=db.paper_open(sig(f'p{i}'),1,100000,'ENTRY OPTION',amount=5,shadow=True,
                              proof_eligible=True,proof_reason='proof')
            db.paper_close(pid,.90,'proof loss')
        rg2=bot.paper_risk_guard(db)
        assert rg2['paused'] and rg2['losing_streak']>=bot.PAPER_LOSS_STREAK_LIMIT
    finally: close_temp(td,old,db)


def test_v152_proof_rows_filter_low_score_research_metadata():
    td,old,db=temp_db()
    try:
        pid=db.paper_open(sig('low'),1,100000,'ENTRY OPTION',amount=5,shadow=True,
                          proof_eligible=False,proof_reason='score below floor')
        db.paper_close(pid,1.2,'done')
        pid2=db.paper_open(sig('high'),1,100000,'ENTRY OPTION',amount=5,shadow=True,
                           proof_eligible=True,proof_reason='score above floor')
        db.paper_close(pid2,1.2,'done')
        rows=db.proof_shadow_rows(20)
        assert len(rows)==1 and rows[0]['token']=='high' and rows[0]['proof_eligible']==1
        all_stats=db.paper_stats(7,shadow_only=True,build_version=bot.VERSION)
        proof_stats=db.paper_stats(7,shadow_only=True,build_version=bot.VERSION,proof_only=True)
        assert all_stats['closed']==2 and proof_stats['closed']==1
    finally: close_temp(td,old,db)
