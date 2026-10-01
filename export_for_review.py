"""Make a small copy of the bot's database for review (alerts, copies, recent trades and prices).

Writes FomoBot_review.zip to your Desktop. It holds only public wallet addresses and market
data — no Telegram token, Helius key or anything from .env. Safe to run while the bot is running.
"""
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

from fomo import config

DAYS = 10
FULL = ("alerts", "copies", "whales", "tokens", "my_trades", "watches", "position_notes", "whale_candidates")


def main() -> int:
    src = config.load().db_path
    if not src.exists():
        print(f"No database found at {src}")
        return 1
    out_dir = Path.home() / "Desktop"
    out_dir = out_dir if out_dir.is_dir() else Path.cwd()
    slim = out_dir / "fomo_review.db"
    slim.unlink(missing_ok=True)
    since = int(time.time()) - DAYS * 86400
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    con.execute("attach database ? as out", (str(slim),))
    tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    for t in FULL:
        if t in tables:
            con.execute(f"create table out.{t} as select * from main.{t}")
    con.execute("create table out.swaps as select * from main.swaps where ts>=?", (since,))
    con.execute("""create table out.price_marks as select * from main.price_marks where ts>=? and
        (mint in (select mint from main.alerts where ts>=?) or mint in (select mint from main.my_trades))""",
                (since, since - 86400))
    con.commit()
    con.execute("detach database out")
    con.close()
    zipped = out_dir / "FomoBot_review.zip"
    with zipfile.ZipFile(zipped, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.write(slim, "fomo_review.db")
    slim.unlink()
    print(f"Done: {zipped} ({zipped.stat().st_size / 1e6:.1f} MB). Send that file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
