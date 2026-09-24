import os
import unittest
import inspect

os.environ['BIRDEYE_API_KEY']=''
os.environ['TELEGRAM_BOT_TOKEN']=''
os.environ['TELEGRAM_CHAT_ID']=''
os.environ['REALTIME_SOLANA']='false'

import bot


def pair(pc5=1.0, pc1=5.0, liq=80000, mc=500000, buys=40, sells=20, vol5=5000, symbol='TEST'):
    return {
        'chainId':'solana',
        'baseToken':{'address':'Abc123456789012345678901234567890123456789pump','symbol':symbol,'name':symbol},
        'priceUsd':'0.001',
        'marketCap':mc,
        'liquidity':{'usd':liq},
        'priceChange':{'m5':pc5,'h1':pc1},
        'volume':{'m5':vol5,'h1':max(vol5*6,1)},
        'txns':{'m5':{'buys':buys,'sells':sells}},
    }

VERIFIED={'hard':False,'rug_checked':True,'security_checked':False,'holder_checked':True}

class TestV107FlowPartial(unittest.TestCase):
    def test_low_dollar_turnover_blocks_flat_like_standard_entry(self):
        # Mirrors the v10.6 low-motion alerts: plenty of swaps/ratio, tiny dollar turnover.
        p=pair(liq=146000,mc=2840000,buys=15,sells=9,vol5=709)
        tier,reasons=bot.classify_entry_tier(p,56.5,56.5,[],False,rts={},safety=VERIFIED,scout_move=0.2)
        self.assertIsNone(tier)
        self.assertTrue(any('turnover' in r for r in reasons))

    def test_bitcat_like_activity_is_now_blocked_by_standard_downtrend_floor(self):
        p=pair(pc5=6.0,pc1=-11.8,liq=51200,mc=345300,buys=12,sells=2,vol5=953,symbol='BITCAT')
        tier,_=bot.classify_entry_tier(p,50.9,59.9,[],False,rts={},safety=VERIFIED,scout_move=0.0)
        self.assertIsNone(tier)
        self.assertTrue(any('standard-entry floor' in r or 'crash' in r for r in _))

    def test_micro_entry_requires_exceptional_flow_and_verified_safety(self):
        p=pair(pc5=1.2,pc1=-2.7,liq=29300,mc=117300,buys=221,sells=70,vol5=13190,symbol='TOGETHER')
        tier,reasons=bot.classify_entry_tier(p,60.8,74.9,[],False,rts={},safety=VERIFIED,scout_move=5.8)
        self.assertEqual(tier,'MICRO ENTRY')
        self.assertTrue(any('microcap rescue' in r for r in reasons))

    def test_micro_entry_does_not_bypass_safety(self):
        p=pair(pc5=1.2,pc1=-2.7,liq=29300,mc=117300,buys=221,sells=70,vol5=13190,symbol='TOGETHER')
        tier,reasons=bot.classify_entry_tier(p,60.8,74.9,[],False,rts={},safety={'hard':False,'rug_checked':True,'holder_checked':False},scout_move=5.8)
        self.assertIsNone(tier)
        self.assertTrue(any('holder' in r.lower() for r in reasons))

    def test_manual_partial_sale_reply_commands_exist(self):
        src=inspect.getsource(bot.handle_commands)
        self.assertIn('reply_contract and len(parts) >= 2',src)
        self.assertIn('/sold',src)
        tracker=inspect.getsource(bot.track_positions)
        self.assertIn('/sellpct 50',tracker)
        self.assertIn("Contract: {pos['token']}",tracker)

    def test_real_autopilot_does_not_auto_trade_micro(self):
        self.assertFalse(bot._auto_tier_allowed(type('D',(object,),{})(), 'MICRO ENTRY'))

if __name__=='__main__':
    unittest.main()
