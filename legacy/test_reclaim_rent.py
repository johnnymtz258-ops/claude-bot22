"""Rent reclamation: wire format verified against the Solana SDK (solders) when available."""
import asyncio
import base64
import json
import os
from pathlib import Path

import pytest

import bot
import live_execution as le
from live_execution import (LiveExecutor, TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID, WSOL_MINT, b58decode, b58encode,
                            build_close_accounts_message, generate_keypair, load_keypair)


def _key(i):
    return bytes([i]) * 32


def test_close_message_structure_is_valid_for_the_solana_sdk(tmp_path):
    solders = pytest.importorskip("solders")
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction
    kp = tmp_path / 'w.json'; generate_keypair(kp)
    key, owner = load_keypair(kp)
    accts = [(_key(7), b58decode(TOKEN_PROGRAM_ID)), (_key(8), b58decode(TOKEN_2022_PROGRAM_ID)),
             (_key(9), b58decode(TOKEN_PROGRAM_ID))]
    blockhash = _key(3)
    msg_bytes = build_close_accounts_message(owner, accts, blockhash)
    msg = Message.from_bytes(msg_bytes)
    assert bytes(msg) == msg_bytes
    h = msg.header
    assert (h.num_required_signatures, h.num_readonly_signed_accounts, h.num_readonly_unsigned_accounts) == (1, 0, 2)
    keys = [bytes(k) for k in msg.account_keys]
    assert keys[0] == owner and str(msg.recent_blockhash) == b58encode(blockhash)
    assert len(msg.instructions) == 3
    def writable(i):  # Solana legacy-message rule, from the SDK-parsed header
        n, ro_signed, ro_unsigned = h.num_required_signatures, h.num_readonly_signed_accounts, h.num_readonly_unsigned_accounts
        return i < n - ro_signed or (n <= i < len(keys) - ro_unsigned)
    assert msg.is_signer(0) and writable(0)
    for ix, (acct, prog) in zip(msg.instructions, accts):
        assert keys[ix.program_id_index] == prog
        assert [keys[i] for i in ix.accounts] == [acct, owner, owner]
        assert bytes(ix.data) == bytes([9])
        assert writable(ix.accounts[0]) and not msg.is_signer(ix.accounts[0])
        assert not writable(ix.program_id_index)
    tx_bytes = le._shortvec(1) + key.sign(msg_bytes) + msg_bytes
    tx = Transaction.from_bytes(tx_bytes)
    tx.verify()  # raises if the signature does not match the owner
    assert tx.message.account_keys[0] == Pubkey.from_bytes(owner)


def test_close_message_rejects_bad_input():
    with pytest.raises(ValueError):
        build_close_accounts_message(_key(1), [], _key(2))
    with pytest.raises(ValueError):
        build_close_accounts_message(_key(1), [(_key(1), _key(5))], _key(2))  # owner used as token account


def _acct(pubkey, mint, program, amount='0', owner=None, close_auth=None, lamports=2039280, withheld=0, state='initialized'):
    info = {'mint': mint, 'owner': owner, 'state': state, 'tokenAmount': {'amount': amount}}
    if close_auth:
        info['closeAuthority'] = close_auth
    if withheld:
        info['extensions'] = [{'extension': 'transferFeeAmount', 'state': {'withheldAmount': withheld}}]
    return {'pubkey': pubkey, 'account': {'owner': program, 'lamports': lamports, 'data': {'parsed': {'info': info}}}}


@pytest.fixture
def executor(tmp_path, monkeypatch):
    kp = tmp_path / 'w.json'; generate_keypair(kp)
    monkeypatch.setenv('LIVE_TRADING_ENABLED', 'true')
    monkeypatch.setenv('JUPITER_API_KEY', 'x')
    monkeypatch.setenv('LIVE_WALLET_KEYPAIR_PATH', str(kp))
    return LiveExecutor()


def test_reclaim_closes_only_safe_empty_accounts_and_sends_one_signed_tx(executor):
    owner = executor.address()
    other = b58encode(_key(4))
    a_ok = b58encode(_key(20)); a_ok22 = b58encode(_key(21))
    legacy_rows = [
        _acct(a_ok, 'MintA', TOKEN_PROGRAM_ID, owner=owner),
        _acct(b58encode(_key(22)), 'MintB', TOKEN_PROGRAM_ID, amount='5', owner=owner),         # not empty
        _acct(b58encode(_key(23)), WSOL_MINT, TOKEN_PROGRAM_ID, owner=owner),                   # wrapped SOL
        _acct(b58encode(_key(24)), 'MintC', TOKEN_PROGRAM_ID, owner=owner, close_auth=other),   # someone else closes
        _acct(b58encode(_key(25)), 'KEEP', TOKEN_PROGRAM_ID, owner=owner),                      # excluded mint
        _acct(b58encode(_key(26)), 'MintF', TOKEN_PROGRAM_ID, owner=owner, state='frozen'),
    ]
    t22_rows = [_acct(a_ok22, 'MintD', TOKEN_2022_PROGRAM_ID, owner=owner),
                _acct(b58encode(_key(27)), 'MintE', TOKEN_2022_PROGRAM_ID, owner=owner, withheld=10)]
    calls = []

    async def rpc(_http, method, params):
        calls.append((method, params))
        if method == 'getTokenAccountsByOwner':
            rows = legacy_rows if params[1].get('programId') == TOKEN_PROGRAM_ID else t22_rows
            return {'ok': True, 'value': {'value': rows}}
        if method == 'getLatestBlockhash':
            return {'ok': True, 'value': {'value': {'blockhash': b58encode(_key(3)), 'lastValidBlockHeight': 1}}}
        if method == 'sendTransaction':
            return {'ok': True, 'value': 'sig'}
        raise AssertionError(method)

    async def confirm(_http, sig, timeout_seconds=None):
        return {'confirmed': True, 'failed': False, 'slot': 1}
    executor._rpc_checked = rpc; executor.confirm_signature = confirm
    r = asyncio.run(executor.reclaim_rent(None, exclude_mints={'KEEP'}))
    assert r['ok'] and r['closed'] == 2 and r['lamports'] == 2 * 2039280 and set(r['mints']) == {'MintA', 'MintD'}
    sends = [p for m, p in calls if m == 'sendTransaction']
    assert len(sends) == 1
    raw = base64.b64decode(sends[0][0])
    msg = raw[65:]
    keys_count = msg[3]
    keys = [msg[4 + 32 * i: 36 + 32 * i] for i in range(keys_count)]
    assert b58decode(a_ok) in keys and b58decode(a_ok22) in keys and len(keys) == 1 + 2 + 2


def test_reclaim_fails_closed_on_rpc_error_and_when_live_is_off(executor, monkeypatch):
    async def rpc(_http, method, params):
        return {'ok': False, 'error': 'down'}
    executor._rpc_checked = rpc
    r = asyncio.run(executor.reclaim_rent(None))
    assert not r['ok'] and r['closed'] == 0
    executor.capable = False
    assert asyncio.run(executor.reclaim_rent(None))['error'] == 'LIVE_TRADING_ENABLED is off'


def test_reclaim_exclusions_cover_open_positions_and_unfinished_executions(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DB_PATH', tmp_path / 's.db')
    db = bot.Database()
    sig = {'id': None, 'chain': 'solana', 'token': 'OPEN', 'symbol': 'O', 'name': 'O'}
    db.record_auto_buy_atomic(sig, 1.0, 10, 1e5, 'ENTRY OPTION', 'b', 100)
    db.create_execution_intent(idempotency_key='k', side='SELL', token='PENDING', signature='s')
    db.update_execution_intent('k', state='AMBIGUOUS')
    assert {'OPEN', 'PENDING'} <= bot.reclaim_exclusions(db)
    db.conn.close()
