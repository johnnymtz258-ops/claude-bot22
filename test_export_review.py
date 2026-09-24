"""The compact review export must be complete, small, split to size and free of secrets."""
import csv
import glob
import hashlib
import os
import re
import time
import zipfile
from collections import Counter

import bot
import export_review


def test_review_export_is_complete_split_and_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, 'DB_PATH', tmp_path / 'fomo_master.db')
    db = bot.Database()
    now = int(time.time())
    db.conn.executemany("insert or ignore into observations(ts,chain,token,symbol,price,mcap,liquidity,vol5,buys5,sells5,pc5,pc1) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(now - i, 'solana', f'T{i}', 'T', 1.0, 1, 1, 1, 1, 1, 1, 1) for i in range(5000)])
    db.conn.executemany("""insert into decision_ledger(ts,chain,token,symbol,event,tier,entry_score,price,sources,blockers)
        values(?,?,?,?,?,?,?,?,?,?)""", [(now - i, 'solana', hashlib.sha256(str(i).encode()).hexdigest(), 'S', 'ENTRY_PATH', 'FAST ENTRY', i % 100, 0.1234567891234 + i,
                                            'dexscreener', 'liquidity below minimum ' * 3) for i in range(45000)])
    db.conn.execute("insert into trade_failures(ts,stage,category,detail) values(?,?,?,?)",
                    (now, 'ORDER', 'X', 'https://mainnet.helius-rpc.com/?api-key=SECRETHELIUS123 and ?api-key=unknownKEY42'))
    db.conn.execute("""insert into execution_intents(idempotency_key,created_ts,updated_ts,side,token,amount_raw,wallet_address,state)
        values('k',1,1,'BUY','T','5551234567','MyPublicWallet1111111111111111111111111111','FAILED')""")
    db.conn.commit(); db.conn.close()
    monkeypatch.setattr(export_review, 'ROOT', tmp_path)
    (tmp_path / 'bot.log').write_text('start\n' + "URL('https://api.telegram.org/bot42:TGSECRETTOKEN_abcdefghij/getUpdates')\n")
    env = {'HELIUS_API_KEY': 'SECRETHELIUS123', 'TELEGRAM_BOT_TOKEN': '42:TGSECRETTOKEN_abcdefghij',
           'PUBLIC_SOLANA_WALLET_ADDRESS': 'MyPublicWallet1111111111111111111111111111', 'TELEGRAM_CHAT_ID': '555123'}

    paths, counts = export_review.export(tmp_path / 'fomo_master.db', tmp_path, part_mb=0.5, env=env)

    assert len(paths) >= 2 and all(p.stat().st_size <= 512 * 1024 for p in paths)
    assert all(re.search(r'_part\d+of\d+\.zip$', p.name) for p in paths)
    out = tmp_path / 'unzipped'
    for p in paths:
        zipfile.ZipFile(p).extractall(out)
    rows = Counter()
    for f in glob.glob(str(out / '*.csv')):
        table = re.sub(r'(_\d{3})?(_[ab])*\.csv$', '', os.path.basename(f))
        with open(f, newline='') as handle:
            rows[table] += sum(1 for _ in csv.reader(handle)) - 1
    assert rows['decision_ledger'] == 45000 and 'observations' not in rows
    blob = ''.join(open(f, errors='replace').read() for f in glob.glob(str(out / '*')))
    for secret in ['SECRETHELIUS123', 'unknownKEY42', 'TGSECRETTOKEN', 'MyPublicWallet']:
        assert secret not in blob, secret
    intent = next(csv.DictReader(open(out / 'execution_intents.csv')))
    assert 'wallet_address' not in intent and intent['amount_raw'] == '5551234567'
