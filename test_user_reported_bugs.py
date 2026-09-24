"""Regression tests for two bugs reported from real use (Sep 24).

1. Selling part of a coin was recorded as a full exit ("/sell 5" ignored the amount).
2. Liquidity "dropped up to 100%" while the coin was fine: DexScreener omits the liquidity
   field for some pools and the bot treated the missing value as $0.
"""
import asyncio
import time

import pytest

import adaptive_engine
import bot

MINT = 'MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApump'


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DB_PATH', tmp_path / 'state.db')
    d = bot.Database()
    bot._LIQ_DROP_SEEN.clear()
    yield d
    d.conn.close()


def _position(db, amount=15.0, entry=1.0, liq=100_000):
    db.conn.execute("""insert into positions(open_ts,chain,token,symbol,name,entry_price,amount_usd,quantity,entry_liquidity,
        peak_price,active,remaining_fraction,realized_pnl_partial) values(?,?,?,?,?,?,?,?,?,?,1,1.0,0)""",
                    (int(time.time()) - 600, 'solana', MINT, 'FAM', 'familiars', entry, amount, amount / entry, liq, entry))
    db.conn.commit()
    return db.conn.execute('select max(id) from positions').fetchone()[0]


def _pair(price=1.0, liq=100_000, include_liq=True):
    p = {'chainId': 'solana', 'baseToken': {'address': MINT, 'symbol': 'FAM'}, 'priceUsd': price,
         'marketCap': 1.6e6, 'priceChange': {'m5': -1, 'h1': -5}, 'volume': {'m5': 1000},
         'txns': {'m5': {'buys': 20, 'sells': 18}}}
    if include_liq:
        p['liquidity'] = {'usd': liq}
    return p


# ------------------------------------------------------------------ liquidity

def test_missing_liquidity_is_unknown_not_zero(db):
    pos = {'entry_liquidity': 100_000}
    raw = _pair(include_liq=False)
    assert bot.metrics(raw)['liq'] == 0          # the old bug: reads as $0 -> "-100%"
    safe = bot.monitored_pair(raw, pos, key=('pos', 1))
    assert bot.metrics(safe)['liq'] == 100_000


def test_liquidity_is_summed_across_pools(monkeypatch):
    class HTTP:
        async def get(self, url, **_k):
            return 200, [dict(_pair(liq=20_000), pairAddress='A'), dict(_pair(liq=70_000), pairAddress='B'),
                         dict(_pair(include_liq=False), pairAddress='C')]
    pair = asyncio.run(bot.pair_for_token(HTTP(), 'solana', MINT))
    assert pair['pairAddress'] == 'B' and pair['_total_liquidity_usd'] == 90_000
    safe = bot.monitored_pair(pair, {'entry_liquidity': 70_000}, confirm=False)
    assert bot.metrics(safe)['liq'] == 90_000    # pool migration/splitting is not a drain


def test_a_single_bad_liquidity_read_is_ignored_but_a_real_drain_is_confirmed(db):
    pos = {'entry_liquidity': 100_000}
    key = ('pos', 7)
    first = bot.monitored_pair(_pair(liq=5_000), pos, key=key)
    assert bot.metrics(first)['liq'] == 100_000  # held for confirmation
    healthy = bot.monitored_pair(_pair(liq=95_000), pos, key=key)
    assert bot.metrics(healthy)['liq'] == 95_000
    bot.monitored_pair(_pair(liq=5_000), pos, key=key)
    second = bot.monitored_pair(_pair(liq=5_000), pos, key=key)
    assert bot.metrics(second)['liq'] == 5_000   # persisted on consecutive checks: real


def test_guardian_does_not_call_exit_on_missing_liquidity(db):
    pos = dict(db.conn.execute('select * from positions where id=?', (_position(db),)).fetchone())
    raw = _pair(price=0.93, include_liq=False)   # coin down ~7%, liquidity field missing
    old = adaptive_engine.position_state(raw, pos, liquidity_exit=bot.LIQ_DROP)
    assert old['liq_drop'] >= 99                 # what users saw: "liquidity -100%"
    new = adaptive_engine.position_state(bot.monitored_pair(raw, pos, key=('pos', pos['id'])), pos, liquidity_exit=bot.LIQ_DROP)
    assert abs(new['liq_drop']) < 1 and 'liquidity' not in ' '.join(new.get('reasons') or []).lower()


def test_paper_trade_is_not_closed_by_missing_liquidity(db):
    sig = {'id': None, 'chain': 'solana', 'token': MINT, 'symbol': 'FAM', 'name': 'FAM'}
    pid = db.paper_open(sig, 1.0, 100_000, 'ENTRY OPTION', amount=5, friction_pct=2, shadow=True,
                        source_event='T', proof_eligible=True, proof_reason='t')
    asyncio.run(bot.track_paper_positions(None, db, prices={MINT: _pair(price=0.97, include_liq=False)}))
    assert db.conn.execute('select active from paper_positions where id=?', (pid,)).fetchone()[0] == 1


# ------------------------------------------------------------------ partial sells

class TelegramStub:
    def __init__(self, text, reply_contract=True):
        self.text = text
        self.reply = reply_contract

    async def get(self, url, params=None, **_k):
        msg = {'chat': {'id': 42}, 'text': self.text}
        if self.reply:
            msg['reply_to_message'] = {'message_id': 999, 'text': f'familiars alert\nContract: {MINT}'}
        return 200, {'result': [{'update_id': 1, 'message': msg}]}


def _run(db, monkeypatch, text, reply=True):
    sent = []

    async def fake_send(_http, message):
        sent.append(message)

    async def fake_pair(_http, _chain, _token):
        return _pair(price=1.0)
    monkeypatch.setattr(bot, 'TG', 'token'); monkeypatch.setattr(bot, 'CHAT', '42')
    monkeypatch.setattr(bot, 'send', fake_send); monkeypatch.setattr(bot, 'pair_for_token', fake_pair)
    db.set_meta('telegram_offset', '0')
    asyncio.run(bot.handle_commands(TelegramStub(text, reply), db, {}))
    return sent


def _row(db, pid):
    return dict(db.conn.execute('select * from positions where id=?', (pid,)).fetchone())


@pytest.mark.parametrize('text', ['/sell 5', '/sell $5', '/sell5', 'sell 5'])
def test_selling_five_of_fifteen_dollars_keeps_the_rest_open(db, monkeypatch, text):
    pid = _position(db, amount=15.0)
    sent = _run(db, monkeypatch, text)
    row = _row(db, pid)
    assert row['active'] == 1, sent
    assert abs(row['remaining_fraction'] - 2 / 3) < 1e-6
    assert 'PARTIAL SALE RECORDED' in sent[-1] and '$5.00' in sent[-1]


def test_percent_sell_and_plain_full_sell(db, monkeypatch):
    pid = _position(db, amount=15.0)
    _run(db, monkeypatch, '/sell 30%')
    assert abs(_row(db, pid)['remaining_fraction'] - 0.7) < 1e-6
    _run(db, monkeypatch, '/sell')
    assert _row(db, pid)['active'] == 0


def test_amount_covering_the_whole_position_closes_it(db, monkeypatch):
    pid = _position(db, amount=15.0)
    sent = _run(db, monkeypatch, '/sell 20')
    assert _row(db, pid)['active'] == 0 and 'whole remaining position' in sent[-1]


def test_amount_without_reply_asks_which_coin_instead_of_closing(db, monkeypatch):
    pid = _position(db, amount=15.0)
    sent = _run(db, monkeypatch, '/sell 5', reply=False)
    assert _row(db, pid)['active'] == 1 and 'Reply to the coin' in sent[-1]
