"""WIDE signal profile, simpler buy/sell journaling and fee-aware P/L."""
import asyncio
import time

import pytest

import bot

MINT = 'WideMintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApump'


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DB_PATH', tmp_path / 'state.db')
    d = bot.Database()
    bot._SIGNAL_PROFILE['value'] = 'wide'
    yield d
    bot._SIGNAL_PROFILE['value'] = 'wide'
    d.conn.close()


def _pair(mc=1e6, liq=150_000, price=0.001, pc5=1.0, pc1=4.0, buys=60, sells=30, v5=None):
    return {'chainId': 'solana', 'baseToken': {'address': MINT, 'symbol': 'WID', 'name': 'Wide'},
            'priceUsd': price, 'marketCap': mc, 'fdv': mc, 'liquidity': {'usd': liq},
            'priceChange': {'m5': pc5, 'h1': pc1}, 'volume': {'m5': v5 if v5 is not None else mc * 0.01},
            'txns': {'m5': {'buys': buys, 'sells': sells}}, 'pairCreatedAt': int((time.time() - 86400) * 1000)}


SAFE = {'rug_checked': True, 'holder_checked': True, 'security_checked': True, 'hard': False}


# ------------------------------------------------------------------ profile

def test_wide_profile_admits_larger_caps_that_strict_blocked(db):
    pair = _pair(mc=80_000, liq=55_000)   # below the strict $100k floor, inside WIDE's $60k floor
    bot._SIGNAL_PROFILE['value'] = 'strict'
    tier, why = bot.classify_entry_tier(pair, 80, 85, [], False, safety=SAFE)
    assert tier is None and 'market cap outside entry range' in why
    bot._SIGNAL_PROFILE['value'] = 'wide'
    tier, _ = bot.classify_entry_tier(pair, 80, 85, [], False, safety=SAFE)
    assert tier is not None


class _RugHTTP:
    def __init__(self, risks, score=40):
        self.data = {'score_normalised': score, 'risks': risks}

    async def get(self, *_a, **_k):
        return 200, self.data


@pytest.mark.parametrize('risks,wide_hard', [
    ([{'name': 'Bundled launch', 'level': 'danger'}], False),
    ([{'name': 'High holder correlation', 'level': 'warn'}], False),
    ([{'name': 'Freeze Authority still enabled', 'level': 'danger'}], True),
    ([{'name': 'Mint Authority still enabled', 'level': 'danger'}], True),
    ([{'name': 'Honeypot', 'level': 'danger'}], True),
])
def test_only_rug_critical_findings_block_in_wide_mode(db, risks, wide_hard):
    bot._RUGCHECK_DOWN_UNTIL = 0
    bot._SIGNAL_PROFILE['value'] = 'strict'
    _, _, strict_hard, _ = asyncio.run(bot.rugcheck_summary(_RugHTTP(risks), MINT))
    assert strict_hard is True
    bot._SIGNAL_PROFILE['value'] = 'wide'
    _, notes, hard, checked = asyncio.run(bot.rugcheck_summary(_RugHTTP(risks), MINT))
    assert hard is wide_hard and checked
    if not wide_hard:
        assert any('WIDE mode' in n for n in notes)


# ------------------------------------------------------------------ wide alert

class _Limiter:
    def __init__(self):
        self.used = set()

    def allowed_action(self, key, cooldown=0):
        if key in self.used:
            return False
        self.used.add(key)
        return True

    def release_action(self, key):
        self.used.discard(key)


class _Probe:
    def __init__(self, hard=False):
        self.hard = hard

    async def token_intel(self, _http, _token):
        return {'hard_block': self.hard, 'reason': 'isSus'}


def _wide(db, monkeypatch, *, chase_ok=True, probe=None, limiter=None):
    sent = []

    async def fake_send(_http, msg):
        sent.append(msg)
        return 100 + len(sent)
    monkeypatch.setattr(bot, 'send', fake_send)
    monkeypatch.setattr(bot, 'schedule_entry_followups', lambda *a, **k: None)
    state = {'execution_probe': probe or _Probe()}
    lim = limiter or _Limiter()
    ok = asyncio.run(bot.send_wide_signal(None, db, state, lim, 'solana:' + MINT, _pair(), 'ENTRY OPTION', 72, 80,
                                          ['flow'], ['bundled launch — WIDE mode warning'], [], SAFE, {}, {'label': 'NEUTRAL'},
                                          {}, {}, chase_ok=chase_ok, chase_note='ran'))
    return ok, sent, lim


def test_wide_alert_is_sent_labelled_tracked_and_paper_simulated(db, monkeypatch):
    bot._WIDE_SENT_TS.clear()
    ok, sent, lim = _wide(db, monkeypatch)
    assert ok and len(sent) == 1
    assert 'WIDE MODE' in sent[0] and 'TRACK RECORD (WIDE ENTRY OPTION' in sent[0] and 'before you are in profit' in sent[0]
    assert db.conn.execute("select count(*) from paper_positions where tier='WIDE ENTRY OPTION'").fetchone()[0] == 1
    assert db.conn.execute("select count(*) from decision_ledger where event='ENTRY_SENT_WIDE'").fetchone()[0] == 1
    assert db.latest_buy_alert()['token'] == MINT
    ok2, sent2, _ = _wide(db, monkeypatch, limiter=lim)      # same coin again: cooldown
    assert not ok2 and sent2 == []


def test_wide_alert_respects_anti_chase_and_jupiter_veto(db, monkeypatch):
    bot._WIDE_SENT_TS.clear()
    ok, sent, _ = _wide(db, monkeypatch, chase_ok=False)
    assert not ok and not sent
    ok, sent, _ = _wide(db, monkeypatch, probe=_Probe(hard=True))
    assert not ok and not sent


# ------------------------------------------------------------------ journaling

class _TG:
    def __init__(self, text, reply_text=None, reply_id=None):
        self.text, self.reply_text, self.reply_id = text, reply_text, reply_id

    async def get(self, url, params=None, **_k):
        msg = {'chat': {'id': 42}, 'text': self.text}
        if self.reply_text is not None:
            msg['reply_to_message'] = {'message_id': self.reply_id or 999, 'text': self.reply_text}
        return 200, {'result': [{'update_id': 1, 'message': msg}]}


def _cmd(db, monkeypatch, text, reply_text=None, price=0.0012, reply_id=None):
    sent = []

    async def fake_send(_http, message):
        sent.append(message)

    async def fake_pair(_http, _chain, _token):
        return _pair(price=price)
    monkeypatch.setattr(bot, 'TG', 't'); monkeypatch.setattr(bot, 'CHAT', '42')
    monkeypatch.setattr(bot, 'send', fake_send); monkeypatch.setattr(bot, 'pair_for_token', fake_pair)
    db.set_meta('telegram_offset', '0')
    asyncio.run(bot.handle_commands(_TG(text, reply_text, reply_id), db, {}))
    return sent


def _old_alert(db, minutes_old=45):
    sid = db.add_signal('EARLY', _pair(price=0.0010), 75, 'ENTRY OPTION', ['r'])
    db.conn.execute('update signals set ts=? where id=?', (int(time.time()) - minutes_old * 60, sid))
    db.map_telegram_signal(555, sid)
    db.map_trade_context(555, sid, _pair(price=0.0010), 'ENTRY OPTION')
    db.conn.execute("update telegram_trade_context set ts=?, valid_until=? where message_id=555",
                    (int(time.time()) - minutes_old * 60, int(time.time()) - minutes_old * 60 + 60))
    db.conn.commit()
    return sid


def test_bought_reply_to_an_old_alert_just_works_at_the_live_price(db, monkeypatch):
    _old_alert(db, 45)
    sent = _cmd(db, monkeypatch, '/bought 10', reply_text=f'alert\nContract: {MINT}', reply_id=555, price=0.0012)
    pos = db.position_by_token(MINT)
    assert pos and abs(pos['entry_price'] - 0.0012) < 1e-12 and abs(pos['amount_usd'] - 10) < 1e-9
    assert 'PURCHASE RECORDED' in sent[-1] and 'too old' not in sent[-1].lower()
    assert 'Price moved +20.0%' in sent[-1] and 'Break-even' in sent[-1]


def test_bought_without_reply_uses_the_latest_alert_or_an_explicit_contract(db, monkeypatch):
    _old_alert(db, 10)
    _cmd(db, monkeypatch, '/bought 7')
    assert abs(db.position_by_token(MINT)['amount_usd'] - 7) < 1e-9
    db.conn.execute('delete from positions'); db.conn.commit()
    _cmd(db, monkeypatch, f'/bought 9 {MINT}')
    assert abs(db.position_by_token(MINT)['amount_usd'] - 9) < 1e-9


def test_sold_amounts_and_full_exit(db, monkeypatch):
    _old_alert(db, 5)
    _cmd(db, monkeypatch, '/bought 15', reply_text=f'x\nContract: {MINT}', reply_id=555, price=0.001)
    _cmd(db, monkeypatch, '/sold 5', reply_text=f'x\nContract: {MINT}', price=0.001)
    pos = db.position_by_token(MINT)
    assert pos and abs(pos['remaining_fraction'] - 2 / 3) < 1e-6
    _cmd(db, monkeypatch, '/sold', reply_text=f'x\nContract: {MINT}', price=0.001)
    assert db.position_by_token(MINT) is None


def test_journal_profit_is_net_of_buy_and_sell_fees(db, monkeypatch):
    _old_alert(db, 5)
    _cmd(db, monkeypatch, '/bought 10', reply_text=f'x\nContract: {MINT}', reply_id=555, price=0.001)
    pid = db.position_by_token(MINT)['id']
    pnl = db.close_position(pid, 0.001)            # flat price: you still lose the fees
    assert abs(pnl - (10 * bot.FEE_KEEP_FACTOR - 10)) < 1e-9 and pnl < 0
    assert abs(bot.BREAKEVEN_MOVE_PCT - ((1 / bot.FEE_KEEP_FACTOR - 1) * 100)) < 1e-9


def test_short_help_lists_only_the_core_commands(db, monkeypatch):
    sent = _cmd(db, monkeypatch, '/help')
    assert 'THE ONLY COMMANDS YOU NEED' in sent[-1] and '/sellcash' not in sent[-1]
    sent = _cmd(db, monkeypatch, '/help all')
    assert '/sellcash' in sent[-1]


def test_signals_command_switches_profile(db, monkeypatch):
    sent = _cmd(db, monkeypatch, '/signals strict')
    assert bot._SIGNAL_PROFILE['value'] == 'strict' and db.get_meta('signal_profile') == 'strict' and 'STRICT' in sent[-1]
    _cmd(db, monkeypatch, '/signals wide')
    assert bot.wide_mode()


def test_hourly_cap_limits_wide_alert_floods(db, monkeypatch):
    bot._WIDE_SENT_TS.clear()
    monkeypatch.setattr(bot, 'WIDE_MAX_ALERTS_PER_HOUR', 1)
    ok, sent, _ = _wide(db, monkeypatch)
    assert ok
    db.conn.execute('delete from paper_positions'); db.conn.commit()
    ok2, sent2, _ = _wide(db, monkeypatch)          # new limiter, same hour: capped
    assert not ok2 and not sent2
    bot._WIDE_SENT_TS.clear()
