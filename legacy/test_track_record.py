"""Every BUY alert shows its alert type's measured record; 'proven' policy filters on it."""
import time

import pytest

import bot


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DB_PATH', tmp_path / 'state.db')
    d = bot.Database()
    yield d
    d.conn.close()


def _closed(db, tier, pnls):
    now = int(time.time())
    for i, pnl in enumerate(pnls):
        db.conn.execute("""insert into paper_positions(open_ts,close_ts,chain,token,symbol,tier,entry_price,amount_usd,quantity,
            entry_liquidity,peak_price,active,close_reason,realized_pnl,shadow) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (now - 3600, now - 60, 'solana', f'{tier}{i}', 'S', tier, 1, 5, 5, 1, 1, 0, 'x', pnl, 1))
    db.conn.commit()


def test_losing_alert_type_is_labelled_as_losing(db):
    _closed(db, 'STRUCTURE ENTRY', [0.1] + [-0.3] * 11)
    line = bot.alert_track_record_line(db, 'STRUCTURE ENTRY')
    assert '12 tests' in line and '8% wins' in line and 'LOST money' in line


def test_consistent_winner_is_proven_and_small_samples_are_not_trusted(db):
    _closed(db, 'ENTRY OPTION', [0.4, 0.35, 0.5, 0.3, 0.45, 0.38, 0.42, -0.1, 0.33, 0.36, 0.41, 0.39])
    rec = bot.tier_track_record(db, 'ENTRY OPTION')
    assert rec['proven'] and '✅' in bot.alert_track_record_line(db, 'ENTRY OPTION')
    _closed(db, 'FAST ENTRY', [0.5, 0.5])
    assert 'too few' in bot.alert_track_record_line(db, 'FAST ENTRY')
    assert 'unproven' in bot.alert_track_record_line(db, 'FLOW ENTRY')


def test_glitch_closes_do_not_count(db):
    _closed(db, 'ENTRY OPTION', [0.2] * 3)
    db.conn.execute("update paper_positions set close_reason='price data unreliable (feed glitch) — excluded from proof'")
    db.conn.commit()
    assert bot.tier_track_record(db, 'ENTRY OPTION')['n'] == 0


def test_proven_policy_only_sends_proven_alert_types(db):
    db.set_meta('signal_policy', 'proven')
    tier, score = 'ENTRY OPTION', 90
    assert bot.entry_signal_mode(db, tier, score)['send'] is False
    _closed(db, tier, [0.4, 0.35, 0.5, 0.3, 0.45, 0.38, 0.42, -0.1, 0.33, 0.36, 0.41, 0.39])
    auth = bot.entry_signal_mode(db, tier, score)
    assert auth['send'] is True and auth['mode'] in {'TEST', 'PAPER', 'LIVE'}
    db.set_meta('signal_policy', 'actionable')
    assert bot.entry_signal_mode(db, 'STRONG ENTRY', score)['send'] is True


def test_alert_message_carries_the_track_record(db):
    pair = {'chainId': 'solana', 'baseToken': {'address': 'MINT', 'symbol': 'M', 'name': 'M'}, 'priceUsd': 0.001,
            'marketCap': 5e5, 'liquidity': {'usd': 8e4}, 'priceChange': {'m5': 1, 'h1': 2}, 'volume': {'m5': 1},
            'txns': {'m5': {'buys': 10, 'sells': 5}}}
    msg = bot.beginner_alert('EARLY', pair, 80, 'ENTRY OPTION', ['r'], [], [], False, False, False, False,
                             tier='ENTRY OPTION', signal_mode='TEST', track_record='📊 TRACK RECORD test line')
    assert '📊 TRACK RECORD test line' in msg
