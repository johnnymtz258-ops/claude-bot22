import asyncio
import base64
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

import aiohttp

import bot
from live_execution import LiveExecutor, generate_keypair, load_keypair, sign_solana_transaction, transaction_signature


def pair(token='tok'):
    return {'chainId':'solana','baseToken':{'address':token,'symbol':token.upper(),'name':token},
            'priceUsd':0.001,'marketCap':500000,'fdv':500000,'liquidity':{'usd':100000},
            'priceChange':{'m5':1.0,'h1':2.0},'volume':{'m5':5000},'txns':{'m5':{'buys':40,'sells':20}}}


def temp_db():
    td=tempfile.TemporaryDirectory(); old=bot.DB_PATH; bot.DB_PATH=Path(td.name)/'state.db'
    db=bot.Database()
    return td,old,db


def close_temp(td,old,db):
    try: db.conn.close()
    finally:
        bot.DB_PATH=old; td.cleanup()


def synthetic_v0_tx(pub):
    msg=bytes([0x80,1,0,0,1])+pub+(b'\x02'*32)+bytes([0,0])
    tx=bytes([1])+(b'\x00'*64)+msg
    return base64.b64encode(tx).decode()


def make_executor(td):
    kp=Path(td)/'wallet.json'; generate_keypair(kp)
    old={k:os.environ.get(k) for k in ['LIVE_TRADING_ENABLED','JUPITER_API_KEY','LIVE_WALLET_KEYPAIR_PATH','SOLANA_RPC_HTTP','SOLANA_RPC_FALLBACKS']}
    os.environ['LIVE_TRADING_ENABLED']='true'; os.environ['JUPITER_API_KEY']='test'; os.environ['LIVE_WALLET_KEYPAIR_PATH']=str(kp)
    os.environ['SOLANA_RPC_HTTP']='https://rpc1.invalid'; os.environ['SOLANA_RPC_FALLBACKS']='https://rpc2.invalid'
    ex=LiveExecutor()
    return ex,kp,old


def restore_env(old):
    for k,v in old.items():
        if v is None: os.environ.pop(k,None)
        else: os.environ[k]=v


class Resp:
    def __init__(self,status,data): self.status=status; self.data=data
    async def json(self,content_type=None): return self.data
    async def text(self): return json.dumps(self.data)


class CM:
    def __init__(self,resp=None,exc=None): self.resp=resp; self.exc=exc
    async def __aenter__(self):
        if self.exc: raise self.exc
        return self.resp
    async def __aexit__(self,*args): return False


class SwapSession:
    def __init__(self,order,execute_status=200,execute=None):
        self.order=order; self.execute_status=execute_status; self.execute=execute or {'status':'Success','code':0}
        self.gets=0; self.posts=0
    def get(self,url,**kwargs):
        self.gets+=1; return CM(Resp(200,self.order))
    def post(self,url,**kwargs):
        self.posts+=1; return CM(Resp(self.execute_status,self.execute))


class HTTPObj:
    def __init__(self,session): self.session=session


def test_v151_brand_and_hardening_tables():
    assert bot.VERSION=='v16.2 QUALITY MEASUREMENT'
    td,old,db=temp_db()
    try:
        tables={r[0] for r in db.conn.execute("select name from sqlite_master where type='table'")}
        for name in {'execution_intents','execution_leases','trade_failures','decision_dedupe'}:
            assert name in tables
    finally: close_temp(td,old,db)


def test_sqlite_wal_busy_timeout_and_indexes():
    td,old,db=temp_db()
    try:
        assert str(db.conn.execute('pragma journal_mode').fetchone()[0]).lower()=='wal'
        assert int(db.conn.execute('pragma busy_timeout').fetchone()[0])>=10000
        idx={r[1] for r in db.conn.execute("pragma index_list('execution_intents')")}
        assert any('state' in x or 'token' in x for x in idx)
    finally: close_temp(td,old,db)


def test_transaction_rolls_back_on_exception():
    td,old,db=temp_db()
    try:
        try:
            with db.transaction():
                db.conn.execute("insert or replace into meta(key,value) values('rollback_probe','yes')")
                raise RuntimeError('boom')
        except RuntimeError:
            pass
        assert db.get_meta('rollback_probe','')==''
    finally: close_temp(td,old,db)


def test_auto_buy_accounting_is_atomic_on_event_failure():
    td,old,db=temp_db()
    try:
        db.conn.execute("create trigger fail_auto_event before insert on auto_trade_events begin select raise(abort,'boom'); end")
        db.conn.commit()
        sig={'id':123,'chain':'solana','token':'atomic','symbol':'ATOM','name':'Atomic'}
        try:
            db.record_auto_buy_atomic(sig,.01,5,100000,'ENTRY OPTION','sig',123,intent_key='')
            assert False,'expected trigger failure'
        except sqlite3.DatabaseError:
            pass
        assert db.conn.execute("select count(*) from positions where token='atomic'").fetchone()[0]==0
        assert db.conn.execute("select count(*) from auto_trade_events where token='atomic'").fetchone()[0]==0
    finally: close_temp(td,old,db)


def test_execution_lease_blocks_second_process_then_releases():
    with tempfile.TemporaryDirectory() as td:
        old=bot.DB_PATH; bot.DB_PATH=Path(td)/'shared.db'
        try:
            a=bot.Database(); b=bot.Database()
            assert a.acquire_execution_lease('entry:tok','A',60)
            assert not b.acquire_execution_lease('entry:tok','B',60)
            a.release_execution_lease('entry:tok','A')
            assert b.acquire_execution_lease('entry:tok','B',60)
            a.conn.close(); b.conn.close()
        finally: bot.DB_PATH=old


def test_governor_corruption_resets_to_probation_on_restart():
    with tempfile.TemporaryDirectory() as td:
        old=bot.DB_PATH; bot.DB_PATH=Path(td)/'gov.db'
        try:
            a=bot.Database()
            a.conn.execute("update lane_governor set state='BROKEN',signal_floor_id=999999999 where build_version=?",(bot.VERSION,)); a.conn.commit(); a.conn.close()
            b=bot.Database()
            rows=b.conn.execute("select state from lane_governor where build_version=?",(bot.VERSION,)).fetchall()
            assert rows and all(r[0]=='PROBATION' for r in rows)
            assert int(b.get_meta('governor_integrity_repairs','0'))>=1
            b.conn.close()
        finally: bot.DB_PATH=old


def test_decision_dedupe_is_serialized_across_connections():
    with tempfile.TemporaryDirectory() as td:
        old=bot.DB_PATH; bot.DB_PATH=Path(td)/'dedupe.db'
        try:
            a=bot.Database(); b=bot.Database(); p=pair('dupe')
            i1=a.log_decision(p,'ENTRY_TEST','ENTRY OPTION',80,80,[],min_seconds=120)
            i2=b.log_decision(p,'ENTRY_TEST','ENTRY OPTION',80,80,[],min_seconds=120)
            assert i1==i2
            assert a.conn.execute("select count(*) from decision_ledger where token='dupe' and event='ENTRY_TEST'").fetchone()[0]==1
            a.conn.close(); b.conn.close()
        finally: bot.DB_PATH=old


def test_execution_intent_idempotency_and_unresolved_gate():
    td,old,db=temp_db()
    try:
        row,created=db.create_execution_intent(idempotency_key='K',side='BUY',token='x',signature='S',state='SIGNED' if False else None) if False else db.create_execution_intent(idempotency_key='K',side='BUY',token='x',signature='S')
        assert created and row['state']=='SIGNED'
        row2,created2=db.create_execution_intent(idempotency_key='K',side='BUY',token='x',signature='S2')
        assert not created2 and row2['signature']=='S'
        gate=bot.live_execution_gate(db,include_capacity=False)
        assert not gate['allowed'] and any('unresolved' in x for x in gate['reasons'])
    finally: close_temp(td,old,db)


def test_trade_failures_persist_after_reopen():
    with tempfile.TemporaryDirectory() as td:
        old=bot.DB_PATH; bot.DB_PATH=Path(td)/'fail.db'
        try:
            a=bot.Database(); a.log_trade_failure('ORDER','TIMEOUT','probe',retryable=True,token='x'); a.conn.close()
            b=bot.Database(); row=b.conn.execute("select * from trade_failures order by id desc limit 1").fetchone()
            assert row['category']=='TIMEOUT' and row['retryable']==1 and row['detail']=='probe'; b.conn.close()
        finally: bot.DB_PATH=old


def test_keypair_change_is_detected_before_signing():
    with tempfile.TemporaryDirectory() as td:
        ex,kp,old=make_executor(td)
        try:
            assert ex.keypair_integrity()[0]
            kp.unlink(); generate_keypair(kp)
            ok,msg=ex.keypair_integrity()
            assert not ok and 'changed since bot startup' in msg
        finally: restore_env(old)


def test_signed_transaction_signature_can_be_derived_before_submit():
    with tempfile.TemporaryDirectory() as td:
        kp=Path(td)/'wallet.json'; generate_keypair(kp); _,pub=load_keypair(kp)
        signed=sign_solana_transaction(synthetic_v0_tx(pub),kp)
        sig=transaction_signature(signed,kp)
        assert isinstance(sig,str) and len(sig)>50


def test_swap_rejects_amount_above_verified_balance_without_network_order():
    with tempfile.TemporaryDirectory() as td:
        ex,kp,old=make_executor(td)
        try:
            async def bal(http,mint):
                return {'ok':True,'raw':100,'value':1e-7}
            ex._mint_balance_checked=bal
            class NoSession:
                def get(self,*a,**k): raise AssertionError('should not request Jupiter order')
            r=asyncio.run(ex.swap(HTTPObj(NoSession()),'TOKEN','OUT',200,side='SELL',token='TOKEN'))
            assert not r['ok'] and r['error_kind']=='INSUFFICIENT_BALANCE'
        finally: restore_env(old)


def test_rpc_pool_fails_over_to_second_endpoint():
    with tempfile.TemporaryDirectory() as td:
        ex,kp,old=make_executor(td)
        try:
            class S:
                def post(self,url,**kwargs):
                    if 'rpc1' in url: return CM(exc=aiohttp.ClientConnectionError('down'))
                    return CM(Resp(200,{'jsonrpc':'2.0','result':{'value':123}}))
            r=asyncio.run(ex._rpc_checked(HTTPObj(S()),'getBalance',['x']))
            assert r['ok'] and 'rpc2' in r['rpc'] and 'rpc2' in ex.last_rpc_url
        finally: restore_env(old)


def test_chain_confirmation_overrides_jupiter_http_failure_and_uses_wallet_delta():
    td,old_db,db=temp_db()
    try:
        with tempfile.TemporaryDirectory() as wallet_td:
            ex,kp,old=make_executor(wallet_td)
            try:
                _,pub=load_keypair(kp); order={'transaction':synthetic_v0_tx(pub),'requestId':'R','outAmount':'100','priceImpactPct':'0.001'}
                http=HTTPObj(SwapSession(order,execute_status=500,execute={'error':'gateway lost response'}))
                calls={'TOKEN':0,'OUT':0}
                async def bal(_http,mint):
                    calls[mint]=calls.get(mint,0)+1
                    if mint=='TOKEN': return {'ok':True,'raw':1000}
                    return {'ok':True,'raw':0 if calls[mint]==1 else 95}
                async def confirm(_http,sig,timeout_seconds=None): return {'confirmed':True,'failed':False,'slot':9}
                ex._mint_balance_checked=bal; ex.confirm_signature=confirm
                r=asyncio.run(ex.swap(http,'TOKEN','OUT',100,db=db,idempotency_key='chainwins',side='SELL',token='TOKEN'))
                assert r['ok'] and r['executed'] and r['output_raw']==95
                row=db.execution_intent('chainwins'); assert row['state']=='CONFIRMED' and row['confirmed_slot']==9
            finally: restore_env(old)
    finally: close_temp(td,old_db,db)


def test_confirmation_timeout_persists_ambiguous_and_blocks_same_retry():
    td,old_db,db=temp_db()
    try:
        with tempfile.TemporaryDirectory() as wallet_td:
            ex,kp,old=make_executor(wallet_td)
            try:
                _,pub=load_keypair(kp); order={'transaction':synthetic_v0_tx(pub),'requestId':'R','outAmount':'100','priceImpactPct':'0'}
                http=HTTPObj(SwapSession(order))
                counts={}
                async def bal(_http,mint): counts[mint]=counts.get(mint,0)+1; return {'ok':True,'raw':1000 if mint=='TOKEN' else 0}
                async def confirm(_http,sig,timeout_seconds=None): return {'confirmed':False,'failed':False,'timeout':True}
                ex._mint_balance_checked=bal; ex.confirm_signature=confirm
                r=asyncio.run(ex.swap(http,'TOKEN','OUT',100,db=db,idempotency_key='amb',side='SELL',token='TOKEN'))
                assert not r['ok'] and r['ambiguous'] and db.execution_intent('amb')['state']=='AMBIGUOUS'
                r2=asyncio.run(ex.swap(http,'TOKEN','OUT',100,db=db,idempotency_key='amb',side='SELL',token='TOKEN'))
                assert r2.get('ambiguous') and r2['error_kind']=='PENDING_EXECUTION'
            finally: restore_env(old)
    finally: close_temp(td,old_db,db)


def test_post_fill_slippage_is_logged_but_chain_trade_remains_accountable():
    td,old_db,db=temp_db()
    try:
        with tempfile.TemporaryDirectory() as wallet_td:
            ex,kp,old=make_executor(wallet_td)
            try:
                _,pub=load_keypair(kp); order={'transaction':synthetic_v0_tx(pub),'requestId':'R','outAmount':'100','priceImpactPct':'0'}
                http=HTTPObj(SwapSession(order))
                counts={}
                async def bal(_http,mint):
                    counts[mint]=counts.get(mint,0)+1
                    if mint=='TOKEN': return {'ok':True,'raw':1000}
                    return {'ok':True,'raw':0 if counts[mint]==1 else 50}
                async def confirm(_http,sig,timeout_seconds=None): return {'confirmed':True,'failed':False,'slot':10}
                ex._mint_balance_checked=bal; ex.confirm_signature=confirm; ex.max_fill_slippage=6
                r=asyncio.run(ex.swap(http,'TOKEN','OUT',100,db=db,idempotency_key='slip',side='SELL',token='TOKEN'))
                assert r['ok'] and r['slippage_pct']>40 and r['warning']
                assert db.conn.execute("select count(*) from trade_failures where category='EXCESS_FILL_SLIPPAGE'").fetchone()[0]==1
            finally: restore_env(old)
    finally: close_temp(td,old_db,db)


def test_confirmed_but_unreadable_wallet_delta_stays_unresolved():
    td,old_db,db=temp_db()
    try:
        with tempfile.TemporaryDirectory() as wallet_td:
            ex,kp,old=make_executor(wallet_td)
            try:
                _,pub=load_keypair(kp); order={'transaction':synthetic_v0_tx(pub),'requestId':'R','outAmount':'100','priceImpactPct':'0'}
                http=HTTPObj(SwapSession(order)); counts={}
                async def bal(_http,mint):
                    counts[mint]=counts.get(mint,0)+1
                    if mint=='TOKEN': return {'ok':True,'raw':1000}
                    if counts[mint]==1: return {'ok':True,'raw':0}
                    return {'ok':False,'error':'rpc unavailable','raw':None}
                async def confirm(_http,sig,timeout_seconds=None): return {'confirmed':True,'failed':False,'slot':11}
                ex._mint_balance_checked=bal; ex.confirm_signature=confirm
                r=asyncio.run(ex.swap(http,'TOKEN','OUT',100,db=db,idempotency_key='unrec',side='SELL',token='TOKEN'))
                assert not r['ok'] and r['executed'] and not r['reconciled']
                assert db.execution_intent('unrec')['state']=='CONFIRMED_UNRECONCILED'
                assert db.has_unresolved_execution()
            finally: restore_env(old)
    finally: close_temp(td,old_db,db)


def test_export_includes_execution_audit_tables():
    src=Path(bot.__file__).with_name('export_data.py').read_text()
    for name in ['execution_intents','execution_leases','trade_failures','auto_trade_events']:
        assert name in src


def test_execstatus_and_central_gate_are_packaged():
    src=Path(bot.__file__).read_text()
    for needle in ['/execstatus','def live_execution_gate','def reconcile_unresolved_execution_intents','record_auto_buy_atomic','record_auto_sell_atomic']:
        assert needle in src
