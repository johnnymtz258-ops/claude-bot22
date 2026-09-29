import asyncio, time
import bot
from adaptive_engine import dollar_size_guide


def pair(price=0.001679, liq=155868.49, mcap=1626025, vol5=8603.0,
         buys=80, sells=46, pc5=-0.72, pc1=-29.12,
         token='AVBN6kXdaw27ySuvMevKYzNTL8d39b7sGQFDCmsvpump'):
    return {
        'chainId':'solana','baseToken':{'address':token,'symbol':'PINK','name':'PINK COIN'},
        'priceUsd':price,'marketCap':mcap,'pairCreatedAt':(time.time()-6*3600)*1000,
        'liquidity':{'usd':liq},'volume':{'m5':vol5,'h1':50000},
        'txns':{'m5':{'buys':buys,'sells':sells}},
        'priceChange':{'m5':pc5,'h1':pc1,'h6':0,'h24':0},
    }


def safety():
    return {'hard':False,'rug_checked':True,'holder_checked':True,'security_checked':True,
            'trader_checked':False,'wallet_checked':False,'risks':[],'reasons':[]}


def test_pink_washout_snapshot_is_reversal_entry():
    p=pair()
    tier,reasons=bot.classify_entry_tier(
        p,53.06,59.06,[],False,
        rts={'available':True,'tx30':50,'acceleration':1.0},
        safety=safety(),scout_move=0.0)
    assert tier=='REVERSAL ENTRY',(tier,reasons)
    assert any('washout reversal' in x for x in reasons),reasons


def test_reversal_does_not_lower_normal_crash_floor():
    # Same crash, but weak buyer breadth. It must remain blocked.
    p=pair(buys=55,sells=55)
    tier,reasons=bot.classify_entry_tier(
        p,70,70,[],False,
        rts={'available':True,'tx30':50,'acceleration':1.0},
        safety=safety(),scout_move=0.0)
    assert tier is None,(tier,reasons)
    assert any('crash/falling-knife' in x for x in reasons),reasons


def test_reversal_requires_real_liquidity_and_flow():
    p=pair(liq=70000)
    tier,_=bot.classify_entry_tier(p,70,70,[],False,rts={'available':True,'tx30':50},safety=safety(),scout_move=0)
    assert tier is None,tier
    p=pair(vol5=3000)
    tier,_=bot.classify_entry_tier(p,70,70,[],False,rts={'available':True,'tx30':50},safety=safety(),scout_move=0)
    assert tier is None,tier


class PendingDB:
    def __init__(self, ts=None): self.ts=ts; self.added=[]
    def recent_signal(self,chain,token,kind,hours=.1):
        if kind=='REVERSAL_PENDING' and self.ts is not None: return {'ts':self.ts}
        return None
    def add_signal(self,kind,pair,score,action,reasons):
        self.added.append(kind); self.ts=time.time(); return 1


def test_reversal_requires_fresh_confirmation():
    db=PendingDB()
    ok,blocks,info=bot.entry_commitment_gate(db,pair(),'REVERSAL ENTRY',53,59,rts={'available':True,'tx30':50})
    assert not ok and db.added==['REVERSAL_PENDING'],(ok,blocks,db.added)
    db=PendingDB(time.time()-bot.REVERSAL_CONFIRM_SECONDS-2)
    ok,blocks,info=bot.entry_commitment_gate(db,pair(),'REVERSAL ENTRY',53,59,rts={'available':True,'tx30':50})
    assert ok and not blocks,(ok,blocks)


def test_reversal_sizing_is_reduction_only():
    db=bot.Database()
    g=bot.adaptive_dollar_size(db,pair(),'REVERSAL ENTRY',70,market_regime='NEUTRAL',safety={})
    mn,normal,exceptional=bot.sizing_preferences(db)
    assert g['suggested_usd'] <= mn+0.35*(normal-mn)+0.51,g
    db.conn.close()


def test_source_health_reports_degraded_core():
    old=bot.BE_KEY
    try:
        bot.BE_KEY='configured-for-test'
        g=bot.BirdeyeGuard(); g.last_core_status=400; g.key_state='unknown'
        assert 'DEGRADED' in g.status_text(),g.status_text()
    finally:
        bot.BE_KEY=old


if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in tests:
        fn(); print(fn.__name__+': PASS')
    print('v12.1 REVERSAL EDGE tests: PASS')
