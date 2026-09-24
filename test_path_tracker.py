"""Candidate path recorder: unbiased forward paths with exact first-touch times."""
import asyncio

import pytest

import bot
from path_tracker import CandidatePathTracker, parse_path, simulate_exit


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Feed:
    """fetch_batch stand-in: token -> (price, liquidity) or None."""

    def __init__(self):
        self.quotes = {}
        self.calls = []

    async def __call__(self, chain, tokens):
        self.calls.append((chain, list(tokens)))
        out = {}
        for t in tokens:
            q = self.quotes.get(t)
            if q:
                out[t] = {'baseToken': {'address': t}, 'priceUsd': q[0], 'liquidity': {'usd': q[1]}}
        return out


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DB_PATH', tmp_path / 'state.db')
    d = bot.Database()
    yield d
    d.conn.close()


def _decide(db, token, ts, price=1.0, liq=100_000, event='NEAR_ENTRY'):
    db.conn.execute("""insert into decision_ledger(ts,chain,token,symbol,event,tier,entry_score,price,liquidity,mcap)
        values(?,?,?,?,?,?,?,?,?,?)""", (ts, 'solana', token, token, event, 'ENTRY OPTION', 70, price, liq, 1e6))
    db.conn.commit()


def _tracker(db, clock, feed, **kw):
    return CandidatePathTracker(db, feed, now_fn=clock, poll_s=30, horizon_s=kw.pop('horizon_s', 3600), **kw)


def test_only_new_decisions_enroll_deduped_per_token_and_capped(db):
    clock = Clock(1_000_000)
    _decide(db, 'OLD', clock.t - 100)            # before the tracker existed: prices are gone
    tr = _tracker(db, clock, Feed(), max_active=2)
    _decide(db, 'AAA', clock.t)
    _decide(db, 'AAA', clock.t + 60, event='EDGE_ARMED')   # same token within 30 min
    _decide(db, 'BBB', clock.t + 61)
    _decide(db, 'CCC', clock.t + 62)                        # over capacity
    assert tr.enroll_new() == 2
    toks = [r[0] for r in db.conn.execute('select token from candidate_paths order by id')]
    assert toks == ['AAA', 'BBB'] and tr.skipped_capacity == 1
    assert tr.enroll_new() == 0                             # watermark advanced


def test_path_records_extremes_first_touches_checkpoints_and_finishes(db):
    clock = Clock(2_000_000)
    feed = Feed()
    tr = _tracker(db, clock, feed)
    _decide(db, 'RUN', clock.t)
    tr.enroll_new()
    # price path: +12% at 60s, +35% at 300s, -9% at 900s, flat later
    for dt, price in [(30, 1.05), (60, 1.12), (300, 1.35), (330, 1.30), (900, 0.91), (1800, 1.00)]:
        clock.t = 2_000_000 + dt
        feed.quotes['RUN'] = (price, 100_000 * price ** 0.5)
        asyncio.run(tr.poll())
    row = dict(db.conn.execute("select * from candidate_paths where token='RUN'").fetchone())
    assert round(row['max_ret'], 1) == 35.0 and row['t_max'] == 300
    assert round(row['min_ret'], 1) == -9.0 and row['t_min'] == 900
    assert row['tu10'] == 60 and row['tu20'] == 300 and row['tu30'] == 300 and row['tu50'] is None
    assert row['td5'] == 900 and row['td8'] == 900 and row['td10'] is None
    assert round(row['r5'], 1) == 35.0 and round(row['r15'], 1) == -9.0 and round(row['r30'], 1) == 0.0
    assert row['status'] == 'TRACKING' and row['samples'] == 6
    assert [t for t, _ in parse_path(row['path'])] == [30, 300, 900, 1800]   # >=60s sampling
    clock.t = 2_000_000 + 3601 + 1
    asyncio.run(tr.poll())
    assert db.conn.execute("select status from candidate_paths where token='RUN'").fetchone()[0] == 'DONE'


def test_glitches_are_rejected_and_real_jumps_confirmed(db):
    clock = Clock(3_000_000)
    feed = Feed()
    tr = _tracker(db, clock, feed)
    _decide(db, 'GLI', clock.t)
    tr.enroll_new()
    steps = [
        (30, (4000.0, 100_000)),   # 4000x with flat liquidity: implausible, dropped
        (60, (1.02, 100_000)),
        (90, (6.00, 260_000)),     # 5.9x jump: plausible vs liquidity, but held pending
        (120, (1.03, 100_000)),    # not confirmed -> the 6.0 sample is discarded
        (150, (6.20, 262_000)),    # jump again ...
        (180, (6.40, 265_000)),    # ... confirmed by the next sample -> both accepted
    ]
    for dt, q in steps:
        clock.t = 3_000_000 + dt
        feed.quotes['GLI'] = q
        asyncio.run(tr.poll())
    row = dict(db.conn.execute("select * from candidate_paths where token='GLI'").fetchone())
    assert row['dropped'] == 2
    assert row['tu100'] == 150 and round(row['max_ret']) == 540
    rets = [r for _, r in parse_path(row['path'])]
    assert max(rets) < 1000


def test_coin_that_disappears_is_marked_lost_not_left_hanging(db):
    clock = Clock(4_000_000)
    feed = Feed()
    tr = _tracker(db, clock, feed, lost_s=900)
    _decide(db, 'GONE', clock.t)
    tr.enroll_new()
    feed.quotes['GONE'] = (0.8, 80_000)
    clock.t += 30
    asyncio.run(tr.poll())
    feed.quotes.pop('GONE')
    clock.t += 901
    asyncio.run(tr.poll())
    row = db.conn.execute("select status,final_ret from candidate_paths where token='GONE'").fetchone()
    assert row['status'] == 'LOST' and round(row['final_ret']) == -20


def test_one_batched_request_per_chain_per_poll(db):
    clock = Clock(5_000_000)
    feed = Feed()
    tr = _tracker(db, clock, feed)
    for i in range(5):
        _decide(db, f'T{i}', clock.t + i)
    tr.enroll_new()
    asyncio.run(tr.poll())
    assert len(feed.calls) == 1 and len(feed.calls[0][1]) == 5


def test_simulate_exit_uses_exact_touch_order():
    tp_first = {'path': '60:12;300:35;900:-9', 'tu20': 300, 'td8': 900, 'tu10': 60, 'td5': 900}
    assert simulate_exit(tp_first, tp=20, sl=8, max_hold_s=3600) == 20
    sl_first = {'path': '60:-9;300:35', 'tu20': 300, 'td8': 60}
    assert simulate_exit(sl_first, tp=20, sl=8, max_hold_s=3600, stop_slippage=5) == -13
    flat = {'path': '60:1;600:2;1800:-1'}
    assert simulate_exit(flat, tp=20, sl=8, max_hold_s=900) == 2
    trail = {'path': '60:10;120:30;180:15'}
    assert simulate_exit(trail, tp=None, sl=None, max_hold_s=3600, trail=10) == 15
    assert simulate_exit({'path': ''}, tp=20, sl=8, max_hold_s=60) is None
