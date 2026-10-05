"""A small copy of the database for review: alerts, copies, whales, your trades, recent swaps and prices.

Only public wallet addresses and market data — nothing from .env (no Telegram token, Helius key or keys).
Opens the live database read-only, so it is safe while the bot is running.
"""
from __future__ import annotations

import sqlite3
import time
import zipfile
from pathlib import Path

DAYS = 10
FULL = ("alerts", "copies", "whales", "tokens", "my_trades", "watches", "position_notes", "whale_candidates")


def build_review_zip(db_path: Path, out_zip: Path, days: int = DAYS) -> Path:
    db_path, out_zip = Path(db_path), Path(out_zip)
    if not db_path.exists():
        raise FileNotFoundError(f"no database at {db_path}")
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    slim = out_zip.with_name(out_zip.stem + ".tmp.db")
    if slim.exists():
        slim.unlink()
    since = int(time.time()) - days * 86400
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.execute("attach database ? as out", (str(slim),))
        tables = {r[0] for r in con.execute("select name from main.sqlite_master where type='table'")}
        for t in FULL:
            if t in tables:
                con.execute(f"create table out.{t} as select * from main.{t}")
        con.execute("create table out.swaps as select * from main.swaps where ts>=?", (since,))
        con.execute("""create table out.price_marks as select * from main.price_marks where ts>=? and
            (mint in (select mint from main.alerts where ts>=?) or mint in (select mint from main.my_trades))""",
                    (since, since - 86400))
        con.commit()
        con.execute("detach database out")
    finally:
        con.close()
    try:
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            z.write(slim, "fomo_review.db")
    finally:
        slim.unlink()
    return out_zip
