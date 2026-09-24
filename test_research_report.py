"""research_report runs on a live-format database and on a review export."""
import time

import bot
import export_review
import research_report


def _seed(db):
    now = int(time.time())
    for i in range(300):
        ts = now - 5 * 86400 + i * 900
        db.conn.execute("""insert into decision_ledger(ts,chain,token,symbol,event,tier,entry_score,price,liquidity,mcap,pc5,pc1,
            buy_sell,swaps,turnover_pct) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ts, 'solana', f'T{i%40}', 'S', 'NEAR_ENTRY', 'ENTRY OPTION', i % 100, 1.0, 5e4 + i, 5e5, i % 7 - 3,
                         i % 11 - 5, 1 + i % 3, i, i % 9))
        did = db.conn.execute('select max(id) from decision_ledger').fetchone()[0]
        for h in (30, 60, 120):
            db.conn.execute("insert into decision_outcomes(decision_id,horizon_min,completed_ts,max_return_pct,min_return_pct,final_return_pct,suspect) values(?,?,?,?,?,?,?)",
                            (did, h, ts + h * 60, 5 + i % 13, -(3 + i % 9), (i % 7) - 3, 1 if i == 5 else 0))
    for i in range(20):
        db.conn.execute("""insert into paper_positions(open_ts,close_ts,chain,token,symbol,tier,entry_price,amount_usd,quantity,
            entry_liquidity,peak_price,active,close_price,close_reason,realized_pnl,gross_pnl,fees_usd,build_version)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (now - 3600, now - 60, 'solana', f'P{i}', 'P', 'ENTRY OPTION', 1, 5, 5, 1, 1, 0, 0.95,
                         'time stop -5.0% after 30m', -0.35, -0.25, 0.1, bot.VERSION))
    db.conn.commit()


def test_report_on_database_and_export(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DB_PATH', tmp_path / 'fomo_master.db')
    db = bot.Database(); _seed(db); db.conn.close()
    text = research_report.build_report(research_report._connect_db(tmp_path / 'fomo_master.db'))
    for needle in ['1. PAPER / SHADOW TRADES', 'n=20', '3. EVERY CANDIDATE', '4. DO LATER FILTER STAGES',
                   '5. FEATURE BUCKETS', '6. EXIT RULES', 'buckets positive in both halves']:
        assert needle in text, needle
    monkeypatch.setattr(export_review, 'ROOT', tmp_path)
    paths, _ = export_review.export(tmp_path / 'fomo_master.db', tmp_path, part_mb=1, env={})
    text2 = research_report.build_report(research_report._load_export([str(p) for p in paths]))
    assert text2.split('\n', 2)[2] == text.split('\n', 2)[2]   # identical apart from the timestamp line


def test_report_survives_an_empty_database(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DB_PATH', tmp_path / 'empty.db')
    bot.Database().conn.close()
    text = research_report.build_report(research_report._connect_db(tmp_path / 'empty.db'))
    assert 'no decision outcomes available' in text
