import tempfile, unittest
from pathlib import Path
import bot

class T(unittest.TestCase):
    def test_auto_defaults_are_bounded(self):
        self.assertLessEqual(bot.AUTO_LIVE_TRADE_USD, bot.MAX_POSITION)
        self.assertGreaterEqual(bot.AUTO_LIVE_MAX_OPEN,1)
        self.assertFalse(bot.AUTO_LIVE_ALLOW_FRESH_DEFAULT)

    def test_pending_contract_detector(self):
        self.assertTrue(bot.looks_solana_contract('2WbTSrJHr1hNCtmYBjuSetZNdsZWZ2eZTuK9GjNPpump'))
        self.assertTrue(bot.looks_evm_contract('0x'+'1'*40))
        self.assertFalse(bot.looks_solana_contract('SKY'))

    def test_auto_database_upgrade_and_events(self):
        original=bot.DB_PATH
        with tempfile.TemporaryDirectory() as td:
            bot.DB_PATH=Path(td)/'state.db'
            db=bot.Database()
            cols={r[1] for r in db.conn.execute('pragma table_info(positions)')}
            self.assertIn('auto_managed',cols)
            self.assertIn('remaining_fraction',cols)
            sig={'id':1,'chain':'solana','token':'Mint111111111111111111111111111111111111111','symbol':'T','name':'Test'}
            pid=db.force_record_auto_position(sig,0.01,10,50000,'ENTRY OPTION','sig')
            self.assertEqual(len(db.auto_open_positions()),1)
            db.auto_event(pid,1,'BUY','solana',sig['token'],'T','ENTRY OPTION',10,100,0.01,'sig')
            self.assertEqual(db.auto_daily_stats()['buys'],1)
            db.conn.close()
        bot.DB_PATH=original

if __name__=='__main__': unittest.main()
