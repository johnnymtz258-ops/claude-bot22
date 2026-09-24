"""Checklist rows, the local dashboard and the optional AI second opinion."""
import asyncio
import json
import time
import types

import aiohttp
import pytest

import ai_verdict
import bot
import dashboard

MINT = 'DashMintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApump'


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DB_PATH', tmp_path / 'state.db')
    d = bot.Database()
    dashboard.RECENT_CHECKS.clear()
    yield d
    d.conn.close()


def _pair(**kw):
    p = {'chainId': 'solana', 'baseToken': {'address': MINT, 'symbol': 'DSH'}, 'priceUsd': 0.001, 'marketCap': 1e6,
         'liquidity': {'usd': 150_000}, 'priceChange': {'m5': 1.0, 'h1': 3.0}, 'volume': {'m5': 20_000},
         'txns': {'m5': {'buys': 50, 'sells': 30}}}
    p.update(kw)
    return p


def test_checklist_rows_reflect_the_coin():
    safe = {'rug_checked': True, 'holder_checked': True}
    rows = dashboard.entry_checklist(_pair(), safe, ['top-10 wallets hold ~35.0%'])
    assert [r[0] for r in rows] == ['Liquidity depth', 'Holder spread', 'Volume vs mcap', 'Rug signature',
                                    'Fresh/bundled wallets', 'Entry window']
    assert all(s == 'PASS' for _, s, _ in rows)
    risky = dashboard.entry_checklist(_pair(liquidity={'usd': 20_000}, priceChange={'m5': 15}), {'rug_checked': True},
                                      ['bundled launch — WIDE mode warning', 'top-10 wallets hold ~80.0%'], chase_ok=False)
    status = {n: s for n, s, _ in risky}
    assert status['Liquidity depth'] == 'DROP' and status['Holder spread'] == 'WARN'
    assert status['Fresh/bundled wallets'] == 'WARN' and status['Entry window'] == 'DROP'
    assert 'CHECKLIST 1/6 PASS' in dashboard.checklist_text(risky)


def test_dashboard_serves_page_and_live_state(db):
    db.conn.execute("""insert into positions(open_ts,close_ts,chain,token,symbol,entry_price,amount_usd,quantity,active,realized_pnl)
        values(?,?,?,?,?,?,?,?,0,?)""", (int(time.time()) - 3600, int(time.time()) - 60, 'solana', 'X', 'WIN', 1, 10, 10, 2.5))
    db.conn.execute("""insert into positions(open_ts,chain,token,symbol,entry_price,amount_usd,quantity,active,remaining_fraction,peak_price)
        values(?,?,?,?,?,?,?,1,1,1.2)""", (int(time.time()) - 600, 'solana', MINT, 'DSH', 1, 10, 10))
    db.conn.commit()
    dashboard.record_scan(876, 31)
    dashboard.record_check(_pair(), dashboard.entry_checklist(_pair(), {'rug_checked': True}, []), 'ALERT sent')

    async def run():
        runner = await dashboard.start_dashboard(db, lambda: 'wide', 0.95, port=18787)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get('http://127.0.0.1:18787/') as r:
                    page = await r.text()
                async with s.get('http://127.0.0.1:18787/api/state') as r:
                    state = json.loads(await r.text())
        finally:
            await runner.cleanup()
        return page, state
    page, state = asyncio.run(run())
    assert 'FOMOBOT · LIVE' in page and '/api/state' in page
    assert state['scan']['discovered'] == 876 and state['profile'] == 'wide'
    assert state['checks'][0]['symbol'] == 'DSH' and state['checks'][0]['outcome'] == 'ALERT sent'
    assert state['journal']['net'] == 2.5 and state['journal']['wins'] == 1
    pos = state['positions'][0]
    assert pos['symbol'] == 'DSH' and abs(pos['value_after_fees'] - 12 * 0.95) < 1e-6


def test_ai_verdict_is_off_without_a_key(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    assert not ai_verdict.enabled()
    assert asyncio.run(ai_verdict.ai_verdict('x')) is None


class _FakeMessages:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.kwargs = resp, exc, None

    async def create(self, **kw):
        self.kwargs = kw
        if self.exc:
            raise self.exc
        return self.resp


def _client(messages):
    return types.SimpleNamespace(beta=types.SimpleNamespace(messages=messages))


def test_ai_verdict_calls_claude_and_returns_one_line(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test')
    msgs = _FakeMessages(types.SimpleNamespace(stop_reason='end_turn', content=[
        types.SimpleNamespace(type='thinking', thinking=''),
        types.SimpleNamespace(type='text', text='WAIT — liquidity is thin versus volume.\nextra')]))
    monkeypatch.setattr(ai_verdict, '_client', _client(msgs))
    out = asyncio.run(ai_verdict.ai_verdict('coin summary'))
    assert out == 'WAIT — liquidity is thin versus volume.'
    assert msgs.kwargs['model'] == 'claude-opus-5' and msgs.kwargs['fallbacks'] == 'default'
    assert msgs.kwargs['output_config'] == {'effort': 'low'}


def test_ai_verdict_swallows_errors_and_refusals(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test')
    monkeypatch.setattr(ai_verdict, '_client', _client(_FakeMessages(exc=RuntimeError('down'))))
    assert asyncio.run(ai_verdict.ai_verdict('x')) is None
    monkeypatch.setattr(ai_verdict, '_client', _client(_FakeMessages(types.SimpleNamespace(stop_reason='refusal', content=[]))))
    assert asyncio.run(ai_verdict.ai_verdict('x')) is None
