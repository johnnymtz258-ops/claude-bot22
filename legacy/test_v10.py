import os
import unittest
import inspect

os.environ.pop('AUTO_PERFORMANCE_GUARD', None)
os.environ['BIRDEYE_API_KEY']=''
os.environ['TELEGRAM_BOT_TOKEN']=''
os.environ['TELEGRAM_CHAT_ID']=''
os.environ['REALTIME_SOLANA']='false'

import bot


def pair(pc5=1.0, pc1=5.0, liq=80000, mc=500000, buys=40, sells=20, symbol='TEST'):
    return {
        'chainId':'solana',
        'baseToken':{'address':'Abc123456789012345678901234567890123456789pump','symbol':symbol,'name':symbol},
        'priceUsd':'0.001',
        'marketCap':mc,
        'liquidity':{'usd':liq},
        'priceChange':{'m5':pc5,'h1':pc1},
        'volume':{'m5':15000,'h1':90000},
        'txns':{'m5':{'buys':buys,'sells':sells}},
    }

SAFETY={'hard':False,'rug_checked':True,'security_checked':False,'holder_checked':False}

class TestV10Actionable(unittest.TestCase):
    def test_confirmation_can_promote_ectf_like_setup(self):
        p=pair(pc5=0.1,pc1=2.1,liq=50500,mc=1020000,buys=40,sells=20,symbol='ECTF')
        tier,reasons=bot.classify_entry_tier(p,44,70,[],False,rts={},safety=SAFETY,scout_move=1.0)
        self.assertEqual(tier,'ENTRY OPTION')
        self.assertTrue(any('promoted' in r for r in reasons))

    def test_slight_pullback_doggystyle_like_setup_can_be_option(self):
        p=pair(pc5=-0.4,pc1=4.8,liq=69148,mc=356284,buys=139,sells=100,symbol='DOGGYSTYLE')
        tier,reasons=bot.classify_entry_tier(p,52.7,52.7,[],False,rts={},safety=SAFETY,scout_move=1.0)
        self.assertEqual(tier,'ENTRY OPTION')

    def test_strong_entry_requires_stronger_flow(self):
        p=pair(pc5=2.0,pc1=8.0,liq=120000,mc=900000,buys=60,sells=25)
        tier,_=bot.classify_entry_tier(p,65,75,[],False,rts={'available':True,'tx30':4},safety=SAFETY,scout_move=2.0)
        self.assertEqual(tier,'STRONG ENTRY')

    def test_liquidity_floor_still_blocks(self):
        p=pair(liq=34000,mc=900000,buys=30,sells=15)
        tier,reasons=bot.classify_entry_tier(p,70,80,[],False,rts={},safety=SAFETY,scout_move=1.0)
        self.assertIsNone(tier)
        self.assertTrue(any('liquidity' in r for r in reasons))

    def test_info_alert_budget_cannot_consume_action_budget(self):
        lim=bot.AlertLimiter()
        for i in range(bot.MAX_ALERTS):
            self.assertTrue(lim.allowed(f'info{i}',cooldown=0))
        self.assertFalse(lim.allowed('info-extra',cooldown=0))
        self.assertTrue(lim.allowed_action('entry-one',cooldown=0))

    def test_performance_guard_off_by_default(self):
        self.assertFalse(bot.AUTO_PERF_GUARD)

    def test_check_handler_uses_same_classifier(self):
        src=inspect.getsource(bot.handle_commands)
        self.assertIn('classify_actionable_tier',src)

    def test_internal_scout_not_dependent_on_alert_limiter(self):
        src=inspect.getsource(bot.cycle)
        scout_insert=src.index('db.add_signal("SCOUT"')
        alert_check=src.index('bool_pref(db, "telegram_scouts"')
        self.assertLess(scout_insert, alert_check)

if __name__=='__main__':
    unittest.main()
