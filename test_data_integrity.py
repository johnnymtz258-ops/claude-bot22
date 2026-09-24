"""Price-feed glitches must never become returns, P/L, vetoes or proof evidence."""
import asyncio
import time

import pytest

import bot
from edge_engine import clean_price_path, outcome_from_path, price_move_plausible


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DB_PATH', tmp_path / 'state.db')
    d = bot.Database()
    yield d
    d.conn.close()


def test_amm_plausibility_separates_glitches_from_real_moves():
    assert price_move_plausible(3.0, None)                 # ordinary moves always pass
    assert price_move_plausible(100.0, 10.0)               # pump with sqrt-scaled liquidity
    assert not price_move_plausible(3834.0, 1.0)           # JUPCAT-style: price x3834, liquidity flat
    assert not price_move_plausible(100.0, None)           # unverifiable extreme pump
    assert price_move_plausible(0.001, 0.0)                # rug: liquidity pulled
    assert price_move_plausible(0.01, 0.1)                 # dumped through the pool
    assert not price_move_plausible(0.001, 1.0)            # "crash" with untouched liquidity
    assert not price_move_plausible(float('nan'), 1.0) and not price_move_plausible(-1, 1.0)


def test_clean_path_drops_single_spikes_and_persistent_glitches_but_keeps_real_paths():
    spike = [(1, 1.0), (2, 1.1), (3, 60.0), (4, 1.2), (5, 1.15)]
    clean, dropped = clean_price_path(spike, 1.0)
    assert dropped == 1 and max(p for _, p in clean) == 1.2
    glitch = [(1, 1.0, 50_000), (2, 5000.0, 51_000), (3, 5200.0, 50_500), (4, 1.1, 50_000)]
    clean, dropped = clean_price_path(glitch, 1.0, 50_000)
    assert dropped == 2 and max(p for _, p in clean) == 1.1
    pump = [(1, 1.0, 50_000), (2, 20.0, 240_000), (3, 30.0, 270_000)]
    assert clean_price_path(pump, 1.0, 50_000)[1] == 0
    rug = [(1, 1.0, 50_000), (2, 0.001, 10), (3, 0.001, 10)]
    out = outcome_from_path(rug, 1.0, 50_000)
    assert out['min_return_pct'] < -99 and out['suspect'] == 0
    assert clean_price_path([(1, 1.0), (2, 9.0)], 1.0)[1] == 1   # unconfirmed final jump


def _decision(db, token, ts, price=1.0, liq=50_000, event='ENTRY_SENT'):
    db.conn.execute("""insert into decision_ledger(ts,chain,token,symbol,event,tier,price,liquidity,build_version)
        values(?,?,?,?,?,?,?,?,?)""", (ts, 'solana', token, 'T', event, 'ENTRY OPTION', price, liq, bot.VERSION))
    return db.conn.execute('select max(id) from decision_ledger').fetchone()[0]


def _obs(db, token, ts, price, liq=50_000):
    db.conn.execute("insert or replace into observations values(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, 'solana', token, 'T', price, 1e6, liq, 1, 1, 1, 0, 0))


def test_decision_outcomes_are_cleaned_flagged_and_filtered(db):
    now = int(time.time()); start = now - 3 * 3600
    good = _decision(db, 'GOOD', start)
    bad = _decision(db, 'BAD', start)
    for i in range(1, 60):
        _obs(db, 'GOOD', start + i * 60, 1.0 + 0.002 * i)
        _obs(db, 'BAD', start + i * 60, 4000.0 if i > 5 else 1.0)  # persistent glitch, flat liquidity
    _obs(db, 'GOOD', start + 30 * 60 + 1, 80.0)   # isolated spike inside the good path
    db.conn.commit()
    bot.capture_decision_outcomes(db)
    rows = {r['decision_id']: dict(r) for r in db.conn.execute('select * from decision_outcomes where horizon_min=60')}
    assert rows[good]['suspect'] == 0 and rows[good]['max_return_pct'] < 20 and rows[good]['samples'] > 50
    assert rows[bad]['max_return_pct'] < 1  # glitch samples removed
    trauma_src = db.analog_rows(days=1)
    assert all(r['token'] != 'BAD' or r['max_return_pct'] < 1 for r in trauma_src)


def test_stale_decision_without_observations_is_closed_as_suspect_not_retried(db):
    now = int(time.time())
    _decision(db, 'NOOBS', now - 5 * 3600)
    db.conn.commit()
    bot.capture_decision_outcomes(db)
    rows = db.conn.execute("select suspect,samples from decision_outcomes").fetchall()
    assert rows and all(r['suspect'] == 1 and r['samples'] == 0 for r in rows)


def test_glitched_checkpoint_eval_is_flagged_and_excluded(db, monkeypatch):
    sig = {'chainId': 'solana', 'baseToken': {'address': 'EVAL', 'symbol': 'E', 'name': 'E'}, 'priceUsd': 1.0,
           'liquidity': {'usd': 50_000}, 'marketCap': 1e6, 'priceChange': {}, 'volume': {}, 'txns': {}}
    sid = db.add_signal('EARLY', sig, 70, 'ENTRY OPTION', ['t'])
    db.conn.execute('update signals set ts=? where id=?', (int(time.time()) - 40 * 60, sid)); db.conn.commit()

    async def glitch_pair(_http, _chain, _token):
        return dict(sig, priceUsd=3000.0)
    monkeypatch.setattr(bot, 'pair_for_token', glitch_pair)
    asyncio.run(bot.evaluate_signals(None, db))
    rows = db.conn.execute('select suspect from evals where signal_id=?', (sid,)).fetchall()
    assert rows and all(r['suspect'] == 1 for r in rows)
    assert db.rolling_entry_performance()['n'] == 0


def _paper(db, token='PAPR', price=1.0, liq=50_000):
    sig = {'id': None, 'chain': 'solana', 'token': token, 'symbol': token, 'name': token}
    return db.paper_open(sig, price, liq, 'ENTRY OPTION', amount=5, friction_pct=2, shadow=True,
                         source_event='TEST', proof_eligible=True, proof_reason='t')


def _pair(price, liq):
    return {'chainId': 'solana', 'baseToken': {'address': 'PAPR'}, 'priceUsd': price, 'liquidity': {'usd': liq},
            'marketCap': 1e6, 'priceChange': {'m5': 0, 'h1': 0}, 'volume': {'m5': 0}, 'txns': {'m5': {'buys': 1, 'sells': 1}}}


def test_paper_glitch_never_books_a_fake_take_profit(db, monkeypatch):
    pid = _paper(db)
    bot._PAPER_GLITCH_SINCE.clear()
    events = asyncio.run(bot.track_paper_positions(None, db, prices={'PAPR': _pair(3834.0, 50_000)}))
    row = db.conn.execute('select * from paper_positions where id=?', (pid,)).fetchone()
    assert events == [] and row['active'] == 1 and row['peak_price'] == 1.0
    # A persistent glitch is closed as unmeasurable: zero P/L and never proof evidence.
    bot._PAPER_GLITCH_SINCE[pid] = time.time() - bot.PAPER_GLITCH_MAX_MINUTES * 60 - 1
    asyncio.run(bot.track_paper_positions(None, db, prices={'PAPR': _pair(3834.0, 50_000)}))
    row = db.conn.execute('select * from paper_positions where id=?', (pid,)).fetchone()
    assert row['active'] == 0 and row['realized_pnl'] == 0 and row['proof_eligible'] == 0
    assert all(r['id'] != pid for r in db.proof_shadow_rows(50))


def test_real_paper_take_profit_still_closes(db):
    pid = _paper(db, liq=50_000)
    bot._PAPER_GLITCH_SINCE.clear()
    events = asyncio.run(bot.track_paper_positions(None, db, prices={'PAPR': _pair(1.40, 60_000)}))
    row = db.conn.execute('select * from paper_positions where id=?', (pid,)).fetchone()
    assert row['active'] == 0 and 'TP2' in row['close_reason'] and row['realized_pnl'] > 0
    assert any(e[0] == 'close' for e in events)
