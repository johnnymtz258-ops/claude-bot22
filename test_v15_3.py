from pathlib import Path
import importlib
import pytest

import bot


def make_db(tmp_path, monkeypatch):
    dbp=tmp_path/'state.db'
    monkeypatch.setattr(bot,'DB_PATH',dbp)
    return bot.Database()


def sample_pair():
    return {
        'chainId':'solana','priceUsd':'0.001','marketCap':200000,'fdv':200000,
        'liquidity':{'usd':60000},'volume':{'m5':5000},
        'txns':{'m5':{'buys':30,'sells':15}},'priceChange':{'m5':1.2,'h1':3.0},
        'baseToken':{'address':'MintTest111','symbol':'TEST','name':'Test Token'}
    }


def test_v15_3_version():
    assert bot.VERSION == 'v16.2 QUALITY MEASUREMENT'


def test_actionable_policy_surfaces_preproof_test_signal(tmp_path, monkeypatch):
    db=make_db(tmp_path,monkeypatch)
    mode=bot.entry_signal_mode(db,'ENTRY OPTION',80)
    assert mode['send'] is True
    assert mode['mode'] == 'TEST'
    assert 'proof' in mode['reason'].lower()


def test_strict_policy_preserves_proof_block(tmp_path, monkeypatch):
    db=make_db(tmp_path,monkeypatch)
    db.set_meta('signal_policy','strict')
    mode=bot.entry_signal_mode(db,'ENTRY OPTION',80)
    assert mode['send'] is False
    assert mode['mode'] == 'BLOCKED'


def test_test_size_stays_small_and_bankroll_based(monkeypatch):
    monkeypatch.setattr(bot,'bankroll_size_context',lambda db:{'bankroll':100.0,'suggested_usd':10.0,'ready':True})
    cal=bot._test_signal_calibration(object(),{'suggested_usd':20.0,'size_reasons':[]},'TEST')
    assert 0 < cal['suggested_usd'] <= bot.TEST_BUY_MAX_USD
    assert cal['size_label']=='TEST SIZE ONLY'


def test_test_alert_is_explicitly_not_live_qualified():
    msg=bot.beginner_alert('EARLY',sample_pair(),80,'ENTRY OPTION',['all gates passed'],[],[],True,True,True,True,
                           tier='ENTRY OPTION',confirmed_score=85,calibration={'suggested_usd':2,'size_label':'TEST SIZE ONLY','size_min_usd':1,'size_normal_max_usd':5,'size_exceptional_max_usd':5,'size_reasons':[]},
                           signal_mode='TEST',signal_note='proof PROBATION')
    assert 'BUY NOW SIGNAL' in msg
    assert 'TEST-SIZE / MANUAL ONLY' in msg
    assert 'proof PROBATION' in msg


def test_test_delivery_participates_in_entry_cooldown(tmp_path, monkeypatch):
    db=make_db(tmp_path,monkeypatch)
    pair=sample_pair()
    db.log_decision(pair,'ENTRY_SENT_TEST','ENTRY OPTION',80,85,['test'])
    row=db.recent_entry_decision('solana','MintTest111',hours=1,build_version=bot.VERSION)
    assert row is not None
    assert row['event']=='ENTRY_SENT_TEST'


def test_real_execution_gate_remains_locked_before_proof(tmp_path, monkeypatch):
    db=make_db(tmp_path,monkeypatch)
    gate=bot.live_execution_gate(db,include_capacity=False)
    assert gate['allowed'] is False
    assert any('proof gate' in x for x in gate['reasons'])
