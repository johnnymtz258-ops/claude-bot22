import tempfile
from pathlib import Path

import bot
from copy_trading import CopyTradeEngine, parse_wallet_swap, WSOL_MINT, USDC_MINT

WALLET='7Y9dQ7W4eQYB9N3GsqC8dxBo8aPcMhy6HZx3k1f2uabc'
TOKEN='9tKX3rY6oQH9cQ8Wv7sL5yX6P1kM2nA4bC5dE6fGhijK'


def _bal(owner,mint,amount,dec=6):
    return {'owner':owner,'mint':mint,'uiTokenAmount':{'amount':str(int(amount)),'decimals':dec,'uiAmountString':str(int(amount)/(10**dec))}}


def _tx(pre_sol,post_sol,pre_tokens,post_tokens,err=None,fee=5000,logs=None):
    return {
        'slot':123,'blockTime':1700000000,
        'transaction':{'message':{'accountKeys':[{'pubkey':WALLET},{'pubkey':'11111111111111111111111111111111'}]}},
        'meta':{'err':err,'fee':fee,'preBalances':[pre_sol,0],'postBalances':[post_sol,0],
                'preTokenBalances':pre_tokens,'postTokenBalances':post_tokens,'logMessages':logs or ['Program log: Instruction: Swap']}
    }


def test_parse_buy_from_final_balance_deltas():
    tx=_tx(2_000_000_000,999_995_000,[],[_bal(WALLET,TOKEN,1_000_000_000,6)])
    ev=parse_wallet_swap(tx,WALLET)
    assert ev and ev['action']=='BUY'
    assert ev['token']==TOKEN
    assert ev['base_mint']==WSOL_MINT
    assert abs(ev['base_ui']-1.0)<1e-9
    assert abs(ev['token_ui']-1000.0)<1e-9


def test_parse_sell_fraction_and_failed_tx_ignored():
    pre=[_bal(WALLET,TOKEN,2_000_000_000,6)]
    post=[_bal(WALLET,TOKEN,1_000_000_000,6)]
    tx=_tx(1_000_000_000,1_499_995_000,pre,post)
    ev=parse_wallet_swap(tx,WALLET)
    assert ev and ev['action']=='SELL'
    assert 0.49 <= ev['leader_sell_fraction'] <= 0.51
    assert parse_wallet_swap(_tx(1_000_000_000,1_000_000_000,pre,post,err={'InstructionError':[1,'Custom']}),WALLET) is None


def test_multihop_intermediate_zero_net_does_not_confuse_final_token():
    pre=[_bal(WALLET,USDC_MINT,0,6)]
    post=[_bal(WALLET,USDC_MINT,0,6),_bal(WALLET,TOKEN,500_000_000,6)]
    tx=_tx(2_000_000_000,1_499_995_000,pre,post,logs=['Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 invoke','Program log: Instruction: Route'])
    ev=parse_wallet_swap(tx,WALLET)
    assert ev and ev['action']=='BUY' and ev['token']==TOKEN
    assert ev['dex']=='Jupiter'


def with_db(fn):
    with tempfile.TemporaryDirectory() as td:
        old=bot.DB_PATH; bot.DB_PATH=Path(td)/'v16.db'
        try:
            db=bot.Database(); ce=CopyTradeEngine(db,None,bot.VERSION); fn(db,ce); db.conn.close()
        finally:
            bot.DB_PATH=old


def test_copy_schema_and_default_live_kill_switch():
    def run(db,ce):
        names={r[0] for r in db.conn.execute("select name from sqlite_master where type='table'")}
        for name in {'copy_wallets','copy_wallet_events','copy_signals','copy_paper_positions','copy_live_positions','copy_wallet_candidates','copy_wallet_history'}:
            assert name in names
        assert db.get_meta('copy_kill_switch')=='1'
        assert db.get_meta('copy_live_enabled')=='0'
    with_db(run)


def test_tagged_smart_wallet_auto_discovery_tracks_paper_candidate():
    def run(db,ce):
        ce.observe_tagged_wallet(WALLET,smart=True,risky=False)
        rows=ce.wallets()
        assert any(r['address']==WALLET for r in rows)
        assert ce.wallet_score(WALLET)>=55
    with_db(run)


def test_risky_tag_never_auto_promotes():
    other='8Y9dQ7W4eQYB9N3GsqC8dxBo8aPcMhy6HZx3k1f2uabc'
    def run(db,ce):
        ce.observe_tagged_wallet(other,smart=True,risky=True)
        assert not any(r['address']==other for r in ce.wallets())
    with_db(run)


def test_v16_brand_and_get_ready_default_off():
    assert bot.VERSION=='v16.2 QUALITY MEASUREMENT'
    assert bot.SCOUT_TELEGRAM_DEFAULT is False
    root=Path(bot.__file__).parent
    assert 'copy_trading' in (root/'bot.py').read_text()
    export=(root/'export_data.py').read_text()
    assert "'copy_wallet_events'" in export and "'copy_paper_positions'" in export


def test_venue_detection_for_pump_raydium_meteora_and_jito():
    cases = [
        ([PUMP_LOG := 'Program log: pump swap'], 'Pump.fun/PumpSwap'),
        (['Program log: raydium swap'], 'Raydium'),
        (['Program log: meteora dlmm swap'], 'Meteora'),
    ]
    for logs, expected in cases:
        tx=_tx(2_000_000_000,1_499_995_000,[],[_bal(WALLET,TOKEN,500_000_000,6)],logs=logs)
        ev=parse_wallet_swap(tx,WALLET)
        assert ev and ev['dex']==expected
    tx=_tx(2_000_000_000,1_499_995_000,[],[_bal(WALLET,TOKEN,500_000_000,6)],logs=['Jito tip','Program log: swap'])
    ev=parse_wallet_swap(tx,WALLET)
    assert ev and ev['jito_hint'] is True


def test_paper_partial_exit_preserves_remaining_position():
    def run(db,ce):
        pid=ce._open_paper(WALLET,TOKEN,'TOK',1.0,80,'sig-buy')
        assert pid
        pos=ce._paper_position(WALLET,TOKEN)
        out=ce._close_paper(pos,1.5,0.5,'leader partial sell')
        assert out and out['final'] is False
        pos2=ce._paper_position(WALLET,TOKEN)
        assert pos2 and 0 < float(pos2['remaining_quantity']) < float(pos2['quantity'])
        out2=ce._close_paper(pos2,1.5,1.0,'leader final sell')
        assert out2 and out2['final'] is True
        assert ce._paper_position(WALLET,TOKEN) is None
    with_db(run)


def test_duplicate_copy_event_is_idempotent():
    import asyncio, time
    def run(db,ce):
        ev={'action':'BUY','token':TOKEN,'base_mint':WSOL_MINT,'token_ui':1000,'base_ui':1,
            'leader_price_base':0.001,'leader_sell_fraction':0,'dex':'Jupiter','jito_hint':False,'slot':1}
        a=asyncio.run(ce._record_event(WALLET,'same-signature',ev,time.time()))
        b=asyncio.run(ce._record_event(WALLET,'same-signature',ev,time.time()))
        assert a
        assert b is None
        n=db.conn.execute("select count(*) from copy_wallet_events where signature='same-signature'").fetchone()[0]
        assert n==1
    with_db(run)


def test_live_gate_fails_closed_before_paper_proof_and_with_kill_switch():
    def run(db,ce):
        g=ce.live_gate()
        assert g['allowed'] is False
        assert any('kill switch' in x for x in g['reasons'])
        assert any('paper age' in x or 'closed copy trades' in x for x in g['reasons'])
    with_db(run)
