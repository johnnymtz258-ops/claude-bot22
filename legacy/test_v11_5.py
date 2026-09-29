import time
from pathlib import Path
import bot


def pair(price=1.0, liq=100000, mcap=300000, vol5=6000, buys=30, sells=15, pc5=2.0, pc1=1.0):
    return {
        'chainId':'solana',
        'baseToken':{'address':'V115Token11111111111111111111111111111111','symbol':'V115','name':'V115'},
        'priceUsd':price,'marketCap':mcap,'liquidity':{'usd':liq},
        'volume':{'m5':vol5,'h1':30000},
        'txns':{'m5':{'buys':buys,'sells':sells}},
        'priceChange':{'m5':pc5,'h1':pc1,'h6':0,'h24':0},
    }


def safety():
    return {'hard':False,'rug_checked':True,'holder_checked':True,'security_checked':True,'risks':[],'reasons':[]}


def test_realtime_classifier_uses_configured_minimum():
    p=pair(liq=90000,vol5=7000,buys=28,sells=18,pc5=2,pc1=1)
    tier, reasons=bot.classify_entry_tier(p,55,65,[],False,rts={'available':True,'tx30':1,'acceleration':1.2},safety=safety(),scout_move=1)
    assert tier is None, (tier,reasons)
    assert any('realtime activity' in x for x in reasons), reasons


def test_realtime_classifier_allows_minimum_when_other_gates_pass():
    p=pair(liq=90000,vol5=7000,buys=28,sells=18,pc5=2,pc1=1)
    tier, reasons=bot.classify_entry_tier(p,55,65,[],False,rts={'available':True,'tx30':bot.RT_ENTRY_TX30,'acceleration':1.2},safety=safety(),scout_move=1)
    assert tier == 'ENTRY OPTION', (tier,reasons)


class PendingDB:
    def __init__(self, pending_ts=None):
        self.pending_ts=pending_ts
        self.added=[]
    def recent_signal(self, chain, token, kind, hours=0.1):
        if kind=='ENTRY_PENDING' and self.pending_ts is not None:
            return {'ts':self.pending_ts,'kind':'ENTRY_PENDING','chain':chain,'token':token}
        return None
    def add_signal(self, kind, pair, score, action, reasons):
        self.added.append((kind,score,action,list(reasons)))
        self.pending_ts=time.time()
        return 1


def test_fragile_normal_entry_requires_extra_confirmation():
    p=pair(liq=50300,vol5=8000,buys=30,sells=18,pc5=7.0,pc1=-1.0)
    db=PendingDB()
    ok, blockers, info=bot.entry_commitment_gate(db,p,'ENTRY OPTION',59,69,rts={'available':True,'tx30':3})
    assert not ok, (blockers,info)
    assert info['fragile']
    assert db.added and db.added[0][0]=='ENTRY_PENDING'


def test_fragile_normal_entry_can_pass_after_confirmation():
    p=pair(liq=50300,vol5=8000,buys=30,sells=18,pc5=4.8,pc1=0.5)
    db=PendingDB(time.time()-bot.ENTRY_COMMIT_CONFIRM_SECONDS-5)
    ok, blockers, info=bot.entry_commitment_gate(db,p,'ENTRY OPTION',59,69,rts={'available':True,'tx30':3})
    # liq + weak score are still two fragile traits; after persistence they may pass.
    assert ok, (blockers,info)


def test_strong_entry_bypasses_fragile_delay():
    p=pair(liq=50300,vol5=10000,buys=60,sells=20,pc5=6.0,pc1=2)
    db=PendingDB()
    ok, blockers, info=bot.entry_commitment_gate(db,p,'STRONG ENTRY',80,85,rts={'available':True,'tx30':4})
    assert ok and not blockers, (blockers,info)
    assert not db.added


def test_tp1_reminder_is_one_time_and_requires_unrecorded_partial():
    now=time.time()
    pos={'tp1_sent':1,'remaining_fraction':1.0}
    info={'ret':15.5}
    rt={}
    assert bot.tp1_reminder_due(pos,info,'TAKE_PARTIAL',now-bot.TP1_REMINDER_SECONDS-1,rt,now=now)
    rt['tp1_reminder_sent']=True
    assert not bot.tp1_reminder_due(pos,info,'TAKE_PARTIAL',now-bot.TP1_REMINDER_SECONDS-1,rt,now=now)
    rt={}
    pos['remaining_fraction']=0.7
    assert not bot.tp1_reminder_due(pos,info,'TAKE_PARTIAL',now-bot.TP1_REMINDER_SECONDS-1,rt,now=now)


def test_static_v115_contracts():
    src=Path(bot.__file__).read_text()
    for needle in [
        'VERSION = "v11.5 ENTRY CONFIRM + PROFIT REMINDER"',
        'ENTRY_COMMIT_CONFIRM_SECONDS',
        'def entry_commitment_gate',
        'realtime activity {rts.get(\'tx30\',0)} tx/30s < required {RT_ENTRY_TX30}',
        'def tp1_reminder_due',
        'PROFIT REMINDER:',
    ]:
        assert needle in src, needle


if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for t in tests:
        t(); print(t.__name__+': PASS')
    print('v11.5 entry-confirm/profit-reminder tests: PASS')
