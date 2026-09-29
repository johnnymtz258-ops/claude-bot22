import os, unittest, inspect
for k in ["ENTRY_MIN_BUY_SELL_RATIO","ENTRY_MIN_SWAPS_5M","MAX_MOVE_FROM_SCOUT_PCT","REALTIME_ENTRY_TX30"]: os.environ.pop(k,None)
os.environ["BIRDEYE_API_KEY"]=""
os.environ["REALTIME_SOLANA"]="false"
import bot
class T(unittest.TestCase):
 def test_thresholds(self):
  self.assertAlmostEqual(bot.ENTRY_MIN_BUY_SELL,1.25); self.assertEqual(bot.ENTRY_MIN_SWAPS_5M,10); self.assertAlmostEqual(bot.MAX_MOVE_FROM_SCOUT,6); self.assertEqual(bot.RT_ENTRY_TX30,2)
 def test_evidence(self):
  self.assertGreaterEqual(bot.evidence_count(["price still early (+2% 5m / +8% 1h)","buy/sell 1.8x","20 swaps/5m"],{}),2)
 def test_preflight(self): self.assertIn("db",inspect.signature(bot.preflight_safety).parameters)
if __name__=="__main__": unittest.main()
