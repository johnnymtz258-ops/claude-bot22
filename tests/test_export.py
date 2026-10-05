import sqlite3
import time
import zipfile

from fomo.db import Database
from fomo.export import build_review_zip


def make_db(path):
    db = Database(path)
    now = int(time.time())
    db.run("insert into alerts(ts,kind,mint,wallet,price_usd) values(?,?,?,?,?)", (now, "BUY", "Mint1", "W1", 1.0))
    db.run("insert into swaps(sig,wallet,is_me,ts,seen_ts,side,mint) values('s1','W1',0,?,?,'BUY','Mint1')", (now, now))
    db.run("insert into swaps(sig,wallet,is_me,ts,seen_ts,side,mint) values('s0','W1',0,?,?,'BUY','Mint0')",
           (now - 40 * 86400, now))
    db.run("insert into price_marks(mint,ts,price) values('Mint1',?,1.2)", (now,))
    db.run("insert into price_marks(mint,ts,price) values('Other',?,9.9)", (now,))  # never alerted, not traded
    db.run("insert into processed(sig,wallet,ts) values('s1','W1',?)", (now,))
    db.close()


def test_review_zip_keeps_what_matters_and_stays_small(tmp_path):
    src = tmp_path / "whales.db"
    make_db(src)
    out = build_review_zip(src, tmp_path / "out" / "FomoBot_review.zip")
    assert out.exists() and not list(out.parent.glob("*.tmp.db"))
    with zipfile.ZipFile(out) as z:
        assert z.namelist() == ["fomo_review.db"]
        z.extractall(tmp_path / "x")
    con = sqlite3.connect(tmp_path / "x" / "fomo_review.db")
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert {"alerts", "swaps", "price_marks", "whales", "my_trades"} <= tables
    assert "processed" not in tables and "meta" not in tables
    assert con.execute("select count(*) from swaps").fetchone()[0] == 1            # last 10 days only
    assert con.execute("select mint from price_marks").fetchall() == [("Mint1",)]  # alerted coins only
