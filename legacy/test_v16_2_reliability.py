"""Regression tests for the v16.2 reliability patch (live exits, reconciliation, proof gate)."""
import asyncio
import json
import time

import pytest

import bot
import copy_trading
from copy_trading import CopyTradeEngine
from edge_engine import proof_metrics
from live_execution import LiveExecutor, b58encode, load_keypair, transaction_recent_blockhash, WSOL_MINT
from test_v15_1 import HTTPObj, SwapSession, make_executor, restore_env, synthetic_v0_tx


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DB_PATH', tmp_path / 'state.db')
    d = bot.Database()
    yield d
    d.conn.close()


@pytest.fixture
def executor(tmp_path):
    ex, kp, old = make_executor(str(tmp_path))
    yield ex, kp
    restore_env(old)


def _pair(price, liq=100000):
    return {'chainId': 'solana', 'baseToken': {'address': 'x', 'symbol': 'X', 'name': 'X'},
            'priceUsd': price, 'marketCap': 500000, 'fdv': 500000, 'liquidity': {'usd': liq},
            'priceChange': {'m5': 0.0, 'h1': 0.0}, 'volume': {'m5': 5000}, 'txns': {'m5': {'buys': 40, 'sells': 20}}}


# ---------------------------------------------------------------- live_execution

def test_recent_blockhash_is_parsed_from_signed_transaction(executor):
    ex, kp = executor
    _, pub = load_keypair(kp)
    assert transaction_recent_blockhash(synthetic_v0_tx(pub)) == b58encode(b'\x02' * 32)
    assert transaction_recent_blockhash('not-base64!!') == ''


def test_tx_owner_deltas_uses_transaction_balances_not_wallet_totals(executor):
    ex, kp = executor
    owner = ex.address()
    tx = {'transaction': {'message': {'accountKeys': [{'pubkey': owner}, {'pubkey': 'Other'}]}},
          'meta': {'err': None, 'preBalances': [2_000_000_000, 5], 'postBalances': [1_497_000_000, 5],
                   'preTokenBalances': [],
                   'postTokenBalances': [{'owner': owner, 'mint': 'TOKEN', 'uiTokenAmount': {'amount': '777'}},
                                         {'owner': 'Other', 'mint': 'TOKEN', 'uiTokenAmount': {'amount': '999'}}]}}

    async def rpc(_http, method, params):
        assert method == 'getTransaction'
        return {'ok': True, 'value': tx}
    ex._rpc_checked = rpc
    d = asyncio.run(ex.tx_owner_deltas(None, 'SIG', [WSOL_MINT, 'TOKEN']))
    assert d == {WSOL_MINT: -503_000_000, 'TOKEN': 777}


def test_swap_prefers_exact_transaction_delta_and_reports_true_cost(db, executor):
    ex, kp = executor
    _, pub = load_keypair(kp)
    order = {'transaction': synthetic_v0_tx(pub), 'requestId': 'R', 'outAmount': '100', 'priceImpactPct': '0'}

    async def bal(_http, mint):
        # A concurrent swap inflated the wallet-wide output balance; it must be ignored.
        return {'ok': True, 'raw': 10_000_000_000 if mint == WSOL_MINT else 0}

    async def confirm(_http, sig, timeout_seconds=None):
        return {'confirmed': True, 'failed': False, 'slot': 5}

    async def deltas(_http, sig, mints, owner=None, attempts=3):
        return {WSOL_MINT: -1_004_000_000, 'OUT': 98}
    ex._mint_balance_checked = bal; ex.confirm_signature = confirm; ex.tx_owner_deltas = deltas
    r = asyncio.run(ex.swap(HTTPObj(SwapSession(order)), WSOL_MINT, 'OUT', 1_000_000_000, db=db,
                            idempotency_key='exact', side='BUY', token='OUT'))
    assert r['ok'] and r['output_raw'] == 98 and r['actual_input_raw'] == 1_004_000_000
    ctx = json.loads(db.execution_intent('exact')['context_json'])
    assert ctx['recent_blockhash'] == b58encode(b'\x02' * 32)


def test_exit_sells_use_looser_price_impact_cap_than_entries(db, executor):
    ex, kp = executor
    _, pub = load_keypair(kp)
    order = {'transaction': synthetic_v0_tx(pub), 'requestId': 'R', 'outAmount': '100', 'priceImpactPct': '10'}

    async def bal(_http, mint):
        return {'ok': True, 'raw': 10_000_000_000}

    async def confirm(_http, sig, timeout_seconds=None):
        return {'confirmed': False, 'failed': True, 'err': 'x', 'slot': 1}
    ex._mint_balance_checked = bal; ex.confirm_signature = confirm
    buy = asyncio.run(ex.swap(HTTPObj(SwapSession(order)), WSOL_MINT, 'T', 100, db=db, idempotency_key='b', side='BUY', token='T'))
    assert buy['error_kind'] == 'PRICE_IMPACT'
    sell = asyncio.run(ex.swap(HTTPObj(SwapSession(order)), 'T', WSOL_MINT, 100, db=db, idempotency_key='s', side='SELL', token='T'))
    assert sell['error_kind'] == 'CHAIN_FAILED'  # got past the impact check to submission


def _expiry_rpc(valid=False, status_row=None, fail=False):
    async def rpc(_http, method, params):
        if fail:
            return {'ok': False, 'error': 'down', 'value': None}
        if method == 'isBlockhashValid':
            return {'ok': True, 'value': {'context': {}, 'value': valid}}
        if method == 'getSignatureStatuses':
            return {'ok': True, 'value': {'value': [status_row]}}
        return {'ok': True, 'value': None}
    return rpc


@pytest.mark.parametrize('age,valid,row,fail,expected', [
    (600, False, None, False, 'FAILED'),          # provably dead -> released
    (600, True, None, False, 'AMBIGUOUS'),        # blockhash still valid
    (600, False, {'slot': 1}, False, 'AMBIGUOUS'),  # it did land
    (600, False, None, True, 'AMBIGUOUS'),        # RPC failure is never "not found"
    (30, False, None, False, 'AMBIGUOUS'),        # too young
])
def test_never_landed_transaction_is_released_only_when_provably_expired(db, executor, monkeypatch, age, valid, row, fail, expected):
    ex, kp = executor
    monkeypatch.setattr(asyncio, 'sleep', _no_sleep)
    db.create_execution_intent(idempotency_key='k', side='BUY', token='T', signature='SIG', wallet_address=ex.address(),
                               context={'recent_blockhash': 'BH'})
    db.conn.execute("update execution_intents set created_ts=?, state='AMBIGUOUS' where idempotency_key='k'", (int(time.time()) - age,))
    db.conn.commit()

    async def confirm(_http, sig, timeout_seconds=None):
        return {'confirmed': False, 'failed': False, 'timeout': True}
    ex.confirm_signature = confirm; ex._rpc_checked = _expiry_rpc(valid, row, fail)
    r = asyncio.run(ex.reconcile_intent(None, db.execution_intent('k'), db=db))
    assert r['state'] == expected
    assert db.execution_intent('k')['state'] == expected
    assert db.has_unresolved_execution() == (expected != 'FAILED')


async def _no_sleep(*_a, **_k):
    return None


# ---------------------------------------------------------------- bot.py live exits

class _ReadyExecutor:
    keypair_path = 'x'

    def ready(self):
        return True


def _open_auto(db, token, entry=1.0):
    sig = {'id': None, 'chain': 'solana', 'token': token, 'symbol': token.upper(), 'name': token}
    return db.record_auto_buy_atomic(sig, entry, 10.0, 100000, 'ENTRY OPTION', 'buy-' + token, 1000)


def test_unresolved_intent_on_one_token_does_not_disable_other_stop_losses(db, monkeypatch):
    _open_auto(db, 'safe'); _open_auto(db, 'stuck')
    db.create_execution_intent(idempotency_key='amb', side='SELL', token='stuck', signature='S')
    db.update_execution_intent('amb', state='AMBIGUOUS')
    sold = []

    async def pair_for_token(_http, _chain, _token):
        return _pair(0.5)  # -50%: stop-loss territory

    async def auto_sell(_http, _db, _state, pos, fraction, reason, flag=None, final=False):
        sold.append((pos['token'], reason)); return True
    monkeypatch.setattr(bot, 'pair_for_token', pair_for_token)
    monkeypatch.setattr(bot, '_auto_sell', auto_sell)
    asyncio.run(bot.live_autopilot_track_positions(None, db, {'live_executor': _ReadyExecutor()}))
    assert [t for t, _ in sold] == ['safe'] and 'stop loss' in sold[0][1]


def test_autolive_off_still_protects_existing_auto_positions(db, monkeypatch):
    _open_auto(db, 'held')
    bot.set_bool_pref(db, 'live_auto', False)
    sold = []

    async def pair_for_token(_http, _chain, _token):
        return _pair(0.5)

    async def auto_sell(_http, _db, _state, pos, *a, **k):
        sold.append(pos['token']); return True
    monkeypatch.setattr(bot, 'pair_for_token', pair_for_token)
    monkeypatch.setattr(bot, '_auto_sell', auto_sell)
    monkeypatch.setattr(bot, 'AUTO_LIVE_EXITS_WHEN_OFF', True)
    asyncio.run(bot.live_autopilot_track_positions(None, db, {'live_executor': _ReadyExecutor()}))
    assert sold == ['held']
    sold.clear(); monkeypatch.setattr(bot, 'AUTO_LIVE_EXITS_WHEN_OFF', False)
    asyncio.run(bot.live_autopilot_track_positions(None, db, {'live_executor': _ReadyExecutor()}))
    assert sold == []


class _ReconcileExecutor(_ReadyExecutor):
    async def sol_usd(self, _http):
        return 200.0

    async def reconcile_intent(self, *_a, **_k):
        raise AssertionError('orphans must not re-query the chain')


def test_crash_orphan_confirmed_buy_is_accounted_once(db):
    db.unaccounted_confirmed_intents()  # establishes the accounting floor
    db.create_execution_intent(idempotency_key='orph', side='BUY', token='ORPH', symbol='ORPH', amount_usd=10, signature='SIG1',
                               context={'current_price': 0.01, 'liquidity': 50000, 'solusd': 200})
    db.update_execution_intent('orph', state='CONFIRMED', actual_output_raw='5000')
    db.conn.execute("update execution_intents set updated_ts=? where idempotency_key='orph'", (int(time.time()) - 3600,)); db.conn.commit()
    state = {'live_executor': _ReconcileExecutor()}
    asyncio.run(bot.reconcile_unresolved_execution_intents(None, db, state))
    pos = db.position_by_token('ORPH')
    assert pos and int(pos['auto_managed']) == 1 and pos['auto_token_raw_remaining'] == '5000'
    assert db.execution_intent('orph')['state'] == 'ACCOUNTED'
    asyncio.run(bot.reconcile_unresolved_execution_intents(None, db, state))
    assert db.conn.execute("select count(*) from positions where token='ORPH'").fetchone()[0] == 1


def test_legacy_confirmed_intents_before_floor_are_not_reapplied(db):
    db.create_execution_intent(idempotency_key='old', side='COPY_SELL', token='T', signature='OLD')
    db.update_execution_intent('old', state='CONFIRMED', actual_output_raw='1')
    db.conn.execute("update execution_intents set created_ts=?, updated_ts=? where idempotency_key='old'",
                    (int(time.time()) - 86400, int(time.time()) - 86400)); db.conn.commit()
    assert db.unaccounted_confirmed_intents() == []


def test_live_exit_loop_is_started_independently_of_discovery_scan():
    import inspect
    src = inspect.getsource(bot.main)
    assert 'live_exit_task = asyncio.create_task(live_exit_loop(http,db,state))' in src


# ---------------------------------------------------------------- copy engine

class _CopyExec:
    def __init__(self, balance=1000):
        self.balance = balance; self.swaps = []

    def ready(self):
        return True

    async def token_raw_balance(self, _http, _mint):
        return self.balance

    async def sol_usd(self, _http):
        return 100.0

    async def swap(self, _http, inp, out, amount, **kw):
        self.swaps.append((inp, out, amount, kw.get('idempotency_key')))
        return {'ok': True, 'signature': 'SELLSIG', 'output_raw': 30_000_000}


def _copy_engine(db, ex=None):
    ce = CopyTradeEngine(db, ex, bot.VERSION)
    return ce


def test_copy_live_position_has_independent_stop_loss_even_when_copy_live_is_off(db):
    ex = _CopyExec(); ce = _copy_engine(db, ex)
    db.conn.execute("""insert into copy_live_positions(wallet,token,symbol,open_ts,amount_usd,cost_remaining,token_raw_initial,token_raw_remaining,buy_signature,entry_price)
        values('W','TOK','TOK',?,10,10,'1000','1000','B',1.0)""", (int(time.time()),)); db.conn.commit()
    assert db.get_meta('copy_live_enabled') == '0'

    async def price(_http, _token):
        return {'price': 0.7, 'liq': 20000, 'symbol': 'TOK'}
    ce._price_context = price
    asyncio.run(ce._risk_pass(None))
    assert len(ex.swaps) == 1 and ex.swaps[0][2] == 1000
    row = db.conn.execute("select * from copy_live_positions").fetchone()
    assert row['active'] == 0 and 'stop loss' in row['close_reason']
    assert abs(row['realized_pnl'] - (3.0 - 10.0)) < 1e-9


def test_copy_paper_positions_mirror_the_follower_stop(db):
    ce = _copy_engine(db)
    ce._open_paper('W', 'TOK', 'TOK', 1.0, 80, 'sig')

    async def price(_http, _token):
        return {'price': 0.5}
    ce._price_context = price
    asyncio.run(ce._risk_pass(None))
    row = db.conn.execute("select * from copy_paper_positions").fetchone()
    assert row['active'] == 0 and 'stop loss' in row['close_reason']


def test_copy_orphan_intents_are_booked_idempotently(db):
    ce = _copy_engine(db)
    db.create_execution_intent(idempotency_key='cb', side='COPY_BUY', token='TOK', symbol='TOK', amount_usd=5, signature='CB',
                               context={'leader_wallet': 'W', 'current_price': 0.02})
    intent = db.execution_intent('cb')
    assert ce.account_confirmed_intent(intent, 250, 100.0, 51_000_000)
    assert ce.account_confirmed_intent(intent, 250, 100.0, 51_000_000)
    rows = db.conn.execute("select * from copy_live_positions").fetchall()
    assert len(rows) == 1 and abs(rows[0]['amount_usd'] - 5.1) < 1e-9 and rows[0]['token_raw_remaining'] == '250'
    assert db.execution_intent('cb')['state'] == 'ACCOUNTED'


def test_copy_live_gate_rejects_a_lucky_outlier_paper_record(db, monkeypatch):
    ce = _copy_engine(db, _CopyExec())
    ce.min_paper_hours = 0; ce.min_closed = 10
    db.set_meta('copy_kill_switch', '0'); db.set_meta('copy_engine_first_ts', str(int(time.time()) - 86400 * 3))
    pnls = [4.0] + [-0.3] * 9  # one runner, nine losers: net positive, no real edge
    for i, p in enumerate(pnls):
        db.conn.execute("""insert into copy_paper_positions(wallet,token,symbol,open_ts,close_ts,entry_price,amount_usd,quantity,remaining_quantity,realized_pnl,active)
            values('W',?,?,1,2,1,5,5,0,?,0)""", (f't{i}', f't{i}', p))
    db.conn.commit()
    g = ce.live_gate()
    assert not g['allowed'] and any('edge not proven' in r for r in g['reasons'])


# ---------------------------------------------------------------- proof gate

def test_proof_gate_does_not_activate_on_one_outlier():
    rows = [{'realized_pnl': 3.0, 'amount_usd': 5, 'token': 'moon'}] + \
           [{'realized_pnl': 0.1, 'amount_usd': 5, 'token': f'w{i}'} for i in range(3)] + \
           [{'realized_pnl': -0.3, 'amount_usd': 5, 'token': f'l{i}'} for i in range(4)]
    h = proof_metrics(rows, min_paths=8, min_unique_tokens=6)
    # Old thresholds pass (PF, 50% win, ROI) ...
    assert h['pnl'] > 0 and h['profit_factor'] >= 1.2 and h['win_rate'] >= 0.45 and h['net_roi_pct'] >= 1
    # ... but the edge is one trade.
    assert h['status'] == 'PROBATION' and h['paused']
    assert 'noise' in h['reason'] or 'single trade' in h['reason']
    loose = proof_metrics(rows, min_paths=8, min_unique_tokens=6, min_t_stat=0, require_ex_best_positive=False)
    assert loose['status'] == 'ACTIVE'


def test_proof_gate_activates_on_consistent_edge():
    rows = [{'realized_pnl': 0.4, 'amount_usd': 5, 'token': f'w{i}'} for i in range(6)] + \
           [{'realized_pnl': -0.2, 'amount_usd': 5, 'token': f'l{i}'} for i in range(3)]
    h = proof_metrics(rows, min_paths=8, min_unique_tokens=6)
    assert h['status'] == 'ACTIVE' and h['t_stat'] >= 1.3 and h['pnl_ex_best'] > 0
