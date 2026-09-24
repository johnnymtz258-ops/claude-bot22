import asyncio, time
import bot


def test_secret_normalization():
    assert bot.clean_api_secret('  "Bearer abc123"  ', ('Authorization',)) == 'abc123'
    assert bot.clean_api_secret('X-API-KEY: key987', ('X-API-KEY',)) == 'key987'


class CoreFallbackHTTP:
    def __init__(self): self.calls=[]
    async def get(self,url,headers=None,params=None,retries=1):
        self.calls.append((url,headers,params))
        if url.endswith('/defi/networks'):
            return 400, {'message':'ambiguous networks probe'}
        if url.endswith('/defi/price'):
            return 200, {'success':True,'data':{'value':200}}
        return 500, {'message':'unexpected'}


def test_birdeye_ambiguous_networks_probe_uses_documented_price_fallback():
    old=bot.BE_KEY
    try:
        bot.BE_KEY='test-key'
        g=bot.BirdeyeGuard(); g.last_call=time.time()-10
        status=asyncio.run(g.probe_core(CoreFallbackHTTP(),force=True))
        assert status==200,(status,g.status_text())
        assert g.key_state=='valid'
        assert g.last_core_probe_name=='price-fallback'
        assert 'CORE OK' in g.status_text(),g.status_text()
    finally:
        bot.BE_KEY=old


class X402HTTP:
    async def get(self,url,headers=None,params=None,retries=1):
        return 402, {'title':'Payment Required','detail':'credits required'}

class DummyDB:
    def add_catalyst(self,*a,**k): pass
    def add_social_event(self,*a,**k): pass


def test_x_402_enters_billing_hold_and_reports_payment_required():
    old_token=bot.XTOKEN; old_status=bot.X_LAST_STATUS; old_hold=bot.X_BILLING_HOLD_UNTIL; old_err=bot.X_LAST_ERROR
    try:
        bot.XTOKEN='test-token'; bot.X_LAST_STATUS=None; bot.X_BILLING_HOLD_UNTIL=0; bot.X_LAST_ERROR=''
        ok,n=asyncio.run(bot.x_recent_query(X402HTTP(),DummyDB(),'from:XDevelopers -is:retweet',max_results=10))
        assert not ok and n==0
        assert bot.X_LAST_STATUS==402
        assert bot.X_BILLING_HOLD_UNTIL>time.time()
        text=bot.x_health_text()
        assert 'PAYMENT REQUIRED' in text and 'credits' in text,text
    finally:
        bot.XTOKEN=old_token; bot.X_LAST_STATUS=old_status; bot.X_BILLING_HOLD_UNTIL=old_hold; bot.X_LAST_ERROR=old_err


class ShouldNotCallHTTP:
    async def get(self,*a,**k):
        raise AssertionError('X should not be called during billing hold')


def test_x_billing_hold_suppresses_repeat_paid_calls():
    old_token=bot.XTOKEN; old_hold=bot.X_BILLING_HOLD_UNTIL
    try:
        bot.XTOKEN='test-token'; bot.X_BILLING_HOLD_UNTIL=time.time()+600
        ok,n=asyncio.run(bot.x_recent_query(ShouldNotCallHTTP(),DummyDB(),'test',max_results=10))
        assert not ok and n==0
    finally:
        bot.XTOKEN=old_token; bot.X_BILLING_HOLD_UNTIL=old_hold


def test_birdeye_degraded_status_includes_safe_provider_detail():
    old=bot.BE_KEY
    try:
        bot.BE_KEY='test-key'
        g=bot.BirdeyeGuard(); g.last_core_status=400; g.key_state='unknown'; g.last_core_probe_name='price-fallback'
        g.last_error['core:price']='bad request: account or key configuration'
        text=g.status_text()
        assert 'DEGRADED' in text and 'provider:' in text and 'price-fallback' in text,text
    finally:
        bot.BE_KEY=old


if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in tests:
        fn(); print(fn.__name__+': PASS')
    print('v12.2 SOURCE RESILIENCE tests: PASS')
