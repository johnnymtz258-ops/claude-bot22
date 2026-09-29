import tempfile, time
from pathlib import Path
import bot


def pair(price=0.001, token='tok', symbol='TOK'):
    return {'chainId':'solana','baseToken':{'address':token,'symbol':symbol,'name':symbol},
            'priceUsd':price,'marketCap':500000,'liquidity':{'usd':80000},
            'priceChange':{'m5':1,'h1':2},'volume':{'m5':5000},'txns':{'m5':{'buys':40,'sells':20}}}


def with_db(fn):
    with tempfile.TemporaryDirectory() as td:
        old=bot.DB_PATH; bot.DB_PATH=Path(td)/'x.db'
        try:
            db=bot.Database(); fn(db); db.conn.close()
        finally: bot.DB_PATH=old


def test_context_expires_and_consumes():
    def run(db):
        p=pair(); sid=db.add_signal('EARLY',p,70,'ENTRY OPTION',['x'])
        db.map_trade_context(11,sid,p,'ENTRY OPTION')
        ctx=db.trade_context_from_message(11)
        ok,why=db.context_buy_status(ctx); assert ok,why
        db.consume_trade_context(11)
        ok,why=db.context_buy_status(db.trade_context_from_message(11)); assert not ok and why=='already used',(ok,why)
        db.map_trade_context(12,sid,p,'ENTRY OPTION',valid_seconds=1)
        db.conn.execute('update telegram_trade_context set valid_until=? where message_id=?',(int(time.time())-1,12)); db.conn.commit()
        ok,why=db.context_buy_status(db.trade_context_from_message(12)); assert not ok and why=='expired',(ok,why)
    with_db(run)


def test_signal_context_supersession():
    def run(db):
        p=pair(); sid=db.add_signal('EARLY',p,70,'ENTRY OPTION',['x'])
        db.map_trade_context(21,sid,p,'ENTRY OPTION')
        db.expire_signal_contexts(sid,'SUPERSEDED')
        ok,why=db.context_buy_status(db.trade_context_from_message(21))
        assert not ok and why=='superseded',(ok,why)
    with_db(run)


def test_position_context_never_usable_as_buy():
    def run(db):
        p=pair(); db.map_trade_context(31,None,p,'ENTRY OPTION','POSITION',0)
        ok,why=db.context_buy_status(db.trade_context_from_message(31))
        assert not ok and 'position' in why,(ok,why)
    with_db(run)


def test_cash_close_uses_actual_proceeds():
    def run(db):
        sig={'chain':'solana','token':'tok','symbol':'TOK','name':'TOK','action':'ENTRY OPTION'}
        pid,_=db.record_position(sig,1,15,80000)
        total,leg=db.close_position_cash(pid,17.99)
        assert abs(leg-2.99)<1e-9,(total,leg)
        assert abs(total-2.99)<1e-9,(total,leg)
        row=db.conn.execute('select * from positions where id=?',(pid,)).fetchone()
        assert row['active']==0 and abs(row['realized_pnl']-2.99)<1e-9
    with_db(run)


def test_cash_partial_uses_actual_proceeds():
    def run(db):
        sig={'chain':'solana','token':'tok','symbol':'TOK','name':'TOK','action':'ENTRY OPTION'}
        pid,_=db.record_position(sig,1,10,80000)
        pnl,rem=db.partial_close_position_cash(pid,.30,4.03)
        assert abs(pnl-1.03)<1e-9,(pnl,rem)
        assert abs(rem-.70)<1e-9,(pnl,rem)
    with_db(run)


def test_fix_entry_rebases_only_unpartialed_position():
    def run(db):
        sig={'chain':'solana','token':'tok','symbol':'TOK','name':'TOK','action':'ENTRY OPTION'}
        pid,_=db.record_position(sig,2,10,80000)
        ok,why=db.fix_position_entry(pid,1); assert ok,why
        row=db.conn.execute('select * from positions where id=?',(pid,)).fetchone()
        assert abs(row['entry_price']-1)<1e-12 and abs(row['quantity']-10)<1e-12
        db.partial_close_position(pid,1.2,.5)
        ok,why=db.fix_position_entry(pid,.9); assert not ok and 'partial' in why.lower()
    with_db(run)


def test_static_command_fixes_present():
    src=Path(bot.__file__).read_text()
    for needle in ['VERSION = "v16.2 QUALITY MEASUREMENT"','source_guard=state.get("guard")',
                   'live_scout = db.recent_signal','/closecash TICKER 17.99','/sellcash TICKER 30 4.03','/fixentry TICKER PRICE',
                   'alert age doesn\'t matter; live price is used']:
        assert needle in src,needle


if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in tests:
        fn(); print(fn.__name__+': PASS')
    print('v12.3 CONTEXT GUARD tests: PASS')
