import time
from pathlib import Path

import bot
from adaptive_engine import position_state


def pair(price=1.0, liq=100000, pc5=1.0, pc1=5.0, buys=60, sells=40):
    return {
        'chainId':'solana',
        'baseToken':{'address':'CrashMemory111111111111111111111111111111111','symbol':'MEM','name':'MEM'},
        'priceUsd':price,'marketCap':500000,'liquidity':{'usd':liq},
        'volume':{'m5':12000,'h1':40000},
        'txns':{'m5':{'buys':buys,'sells':sells}},
        'priceChange':{'m5':pc5,'h1':pc1,'h6':0,'h24':0},
    }


class FakeDB:
    def __init__(self, rows): self.rows=rows
    def token_history(self, chain, token, minutes=240): return list(self.rows)


def stable_rows(crash_pc5=-1.0, crash_age=90):
    now=int(time.time())
    return [
        {'ts':now-120,'price':1.00,'mcap':500000,'liquidity':100000,'vol5':10000,'buys5':50,'sells5':30,'pc5':-1,'pc1':2},
        {'ts':now-crash_age,'price':1.01,'mcap':505000,'liquidity':101000,'vol5':12000,'buys5':60,'sells5':35,'pc5':crash_pc5,'pc1':-8},
        {'ts':now-50,'price':1.015,'mcap':507000,'liquidity':101500,'vol5':13000,'buys5':65,'sells5':35,'pc5':1,'pc1':3},
        {'ts':now-10,'price':1.02,'mcap':510000,'liquidity':102000,'vol5':14000,'buys5':70,'sells5':35,'pc5':1.5,'pc1':5},
    ]


def test_recent_crash_memory_fresh_flush_blocks():
    ok, blockers, info = bot.entry_stability_check(FakeDB(stable_rows(-13,90)), pair(pc1=5), 'ENTRY OPTION')
    assert not ok and any('sharp flush' in x or 'crash recovery' in x for x in blockers), (blockers,info)


def test_recent_crash_memory_requires_1h_repair():
    rows=stable_rows(-13,400)
    # Move the crash row back while leaving enough recent stability rows.
    now=int(time.time()); rows[1]['ts']=now-400
    ok, blockers, _ = bot.entry_stability_check(FakeDB(rows), pair(pc1=-8), 'ENTRY OPTION')
    assert not ok and any('1h tape' in x for x in blockers), blockers
    ok, blockers, _ = bot.entry_stability_check(FakeDB(rows), pair(pc1=9), 'ENTRY OPTION')
    assert ok, blockers


def test_recent_crash_memory_hard_flush_blocks_for_10m():
    rows=stable_rows(-25,500); now=int(time.time()); rows[1]['ts']=now-500
    ok, blockers, _ = bot.entry_stability_check(FakeDB(rows), pair(pc1=15), 'FLOW ENTRY')
    assert not ok and any('recent crash recovery' in x for x in blockers), blockers


def small_pos(entry=1.0, amount=10, liq=100000, guardian_state='ENTRY'):
    return {'id':1,'entry_price':entry,'peak_price':entry,'entry_liquidity':liq,'amount_usd':amount,
            'open_ts':int(time.time())-600,'guardian_state':guardian_state}


def test_small_structure_exit_must_persist():
    p=small_pos()
    raw=position_state(pair(price=.84, liq=96000, pc5=-5, pc1=-12, buys=20, sells=30), p,
                       risk_line=10, small_position_usd=15, small_hard_stop=20, mid_hard_stop=16)
    assert raw['state']=='EXIT_REVIEW', raw
    rt={}
    first=bot.stabilize_guardian_state(raw,p,rt,now=1000)
    # v14: a -16% price loss is already beyond the 8% capital hard line, so no debounce.
    assert first['state']=='EXIT_REVIEW', first
    assert first['price_hard_stop'] <= 8, first


def test_guardian_hard_failures_stay_immediate():
    p=small_pos()
    deep=position_state(pair(price=.79, liq=97000, pc5=-2, buys=45, sells=40),p,
                        risk_line=10,small_position_usd=15,small_hard_stop=20)
    out=bot.stabilize_guardian_state(deep,p,{},now=1000)
    assert out['state']=='EXIT_REVIEW',out
    liq=position_state(pair(price=.90, liq=65000, pc5=-1, buys=45, sells=40),p,
                       risk_line=10,liquidity_exit=30,small_position_usd=15,small_hard_stop=20)
    out=bot.stabilize_guardian_state(liq,p,{},now=1000)
    assert out['state']=='EXIT_REVIEW',out


def test_weakening_hysteresis_prevents_state_flap():
    p=small_pos(guardian_state='WEAKENING')
    # -8% has improved above the soft line but not above the -7% recovery buffer.
    raw=position_state(pair(price=.92, pc5=0, pc1=2, buys=50, sells=45),p,
                       risk_line=10,small_position_usd=15,small_hard_stop=20)
    assert raw['state']=='ENTRY',raw
    out=bot.stabilize_guardian_state(raw,p,{},now=1000)
    assert out['state']=='WEAKENING',out
    # A cleaner -5% recovery can return to ENTRY.
    raw=position_state(pair(price=.95, pc5=1, pc1=3, buys=55, sells=40),p,
                       risk_line=10,small_position_usd=15,small_hard_stop=20)
    out=bot.stabilize_guardian_state(raw,p,{},now=1010)
    assert out['state']=='ENTRY',out


def test_repeat_weakening_alert_requires_material_worsening():
    rt={'last_alert_snapshot':{'state':'WEAKENING','ret':-11,'drawdown':-14,'liq_drop':5}}
    info={'state':'WEAKENING','ret':-12,'drawdown':-15,'liq_drop':6}
    assert not bot.guardian_repeat_alert_worthy(rt,info)
    info={'state':'WEAKENING','ret':-17,'drawdown':-20,'liq_drop':6}
    assert bot.guardian_repeat_alert_worthy(rt,info)



def test_network_log_debounce_helpers():
    g=bot.BirdeyeGuard()
    assert g._disable("holders","solana",1800,"x") is True
    assert g._disable("holders","solana",1800,"x") is False


def test_static_v113_contracts():
    src=Path(bot.__file__).read_text()
    for needle in [
        'VERSION = "v11.5 ENTRY CONFIRM + PROFIT REMINDER"',
        'RECENT_CRASH_MEMORY_ENABLED',
        'def stabilize_guardian_state',
        'GUARDIAN_SMALL_STRUCTURE_CONFIRM_SECONDS',
        'elif cmd == "/quiet"',
        'guardian_runtime',
        'gt_rate_log_until',
        'QUIET MODE',
        'entry_stability_check(db,pair,"RE-ENTRY OPTION")',
        'new_state=="EXIT_REVIEW" and (changed or not int(pos.get("exit_sent") or 0))',
    ]:
        assert needle in src, needle


if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for t in tests:
        t(); print(t.__name__+': PASS')
    print('v11.3 stability tests: PASS')
