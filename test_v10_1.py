import os, tempfile, unittest, inspect
os.environ['BIRDEYE_API_KEY']=''
os.environ['REALTIME_SOLANA']='false'
os.environ['FOMO_STATE_DIR']=tempfile.mkdtemp(prefix='fomo-test-')
import bot

class TestV101(unittest.TestCase):
    def pair(self,age=10,liq=45000,mc=300000,buys=25,sells=10,pc5=4):
        import time
        return {'chainId':'solana','pairCreatedAt':int((time.time()-age*60)*1000),'priceUsd':'0.001',
          'baseToken':{'address':'ABC123456789012345678901234567890123','symbol':'TEST','name':'Test'},
          'marketCap':mc,'liquidity':{'usd':liq},'priceChange':{'m5':pc5,'h1':8},
          'volume':{'m5':12000,'h1':60000},'txns':{'m5':{'buys':buys,'sells':sells}}}
    def test_fresh_entry_path(self):
        p=self.pair(); safety={'hard':False,'rug_checked':True,'security_checked':False,'holder_checked':True}
        tier,_=bot.classify_entry_tier(p,50,60,[],False,rts={},safety=safety,scout_move=2)
        self.assertEqual(tier,'FRESH ENTRY')
    def test_shared_db(self):
        db=bot.Database(); self.assertIn('fomo_master.db',str(bot.DB_PATH)); db.conn.close()
    def test_manual_watch_methods(self):
        db=bot.Database(); db.add_manual_watch('solana','ABC','A','Alpha'); self.assertEqual(len(db.manual_watches()),1); db.conn.close()
    def test_reentry_function_exists(self): self.assertTrue(callable(bot.reentry_setup))
    def test_resolver_is_contract_safe(self):
        self.assertIn('duplicate',inspect.getsource(bot.dex_search_candidates).lower())
    def test_paper_tables(self):
        db=bot.Database(); names={r[0] for r in db.conn.execute("select name from sqlite_master where type='table'")}; self.assertIn('paper_positions',names); db.conn.close()
if __name__=='__main__': unittest.main()
