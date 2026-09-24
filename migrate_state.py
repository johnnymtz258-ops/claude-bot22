from pathlib import Path
import sqlite3, shutil, os
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
STATE_DIR = Path(os.path.expanduser(os.getenv("FOMO_STATE_DIR", "~/Library/Application Support/FomoBot")))
STATE_DIR.mkdir(parents=True, exist_ok=True)
MASTER = Path(os.path.expanduser(os.getenv("FOMO_STATE_DB", "").strip() or str(STATE_DIR / "fomo_master.db")))


def info(path):
    try:
        con = sqlite3.connect(path)
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        active = con.execute("select count(*) from positions where active=1").fetchone()[0] if "positions" in tables else 0
        lastpos = con.execute("select coalesce(max(open_ts),0) from positions").fetchone()[0] if "positions" in tables else 0
        signals = con.execute("select count(*) from signals").fetchone()[0] if "signals" in tables else 0
        con.close()
        return (active, lastpos, signals, path.stat().st_mtime)
    except Exception:
        return (-1, 0, 0, 0)


def _position_fingerprint(row):
    """Stable identity for one recorded trade, not merely one token.

    Old version folders can contain a stale ACTIVE copy of a position that was
    later closed in the shared database.  open_ts identifies the original trade
    so a closed master row must win over that stale copy.
    """
    return (
        str(row["chain"] or "").lower(),
        str(row["token"] or ""),
        int(row["open_ts"] or 0),
    )


def _master_has_trade(master, row):
    chain, token, open_ts = _position_fingerprint(row)
    return master.execute(
        """select 1 from positions
           where lower(coalesce(chain,''))=? and token=? and coalesce(open_ts,0)=?
           limit 1""",
        (chain, token, open_ts),
    ).fetchone() is not None


def repair_legacy_position_duplicates(master):
    """Remove only duplicate rows representing the exact same historical trade.

    v10.6/v10.7 migration could re-import an old folder's ACTIVE row after the
    shared copy had been sold.  Those clones share chain+token+open_ts.  Keep the
    earliest master row (the original journal event) and remove later clones.
    Legitimate re-entries have a different open_ts and are preserved.
    """
    tables = {r[0] for r in master.execute("select name from sqlite_master where type='table'")}
    if "positions" not in tables:
        return 0

    groups = master.execute(
        """select lower(coalesce(chain,'')) as chain_key, token, coalesce(open_ts,0) as open_key,
                  count(*) as n
           from positions
           group by lower(coalesce(chain,'')), token, coalesce(open_ts,0)
           having count(*) > 1"""
    ).fetchall()

    removed = 0
    for chain_key, token, open_key, _ in groups:
        rows = master.execute(
            """select id, active, close_ts, entry_price, amount_usd
               from positions
               where lower(coalesce(chain,''))=? and token=? and coalesce(open_ts,0)=?
               order by id asc""",
            (chain_key, token, open_key),
        ).fetchall()
        if len(rows) < 2:
            continue

        # Only auto-repair rows that also agree on the original entry economics.
        # This makes the cleanup conservative if two legitimate trades somehow
        # received the same second-level open timestamp.
        first = rows[0]
        same_trade = all(
            abs(float(r[3] or 0) - float(first[3] or 0)) <= max(1e-15, abs(float(first[3] or 0)) * 1e-9)
            and abs(float(r[4] or 0) - float(first[4] or 0)) <= 1e-8
            for r in rows[1:]
        )
        if not same_trade:
            continue

        keep_id = int(first[0])
        delete_ids = [int(r[0]) for r in rows[1:]]
        master.executemany("delete from positions where id=?", [(i,) for i in delete_ids])
        removed += len(delete_ids)
        print(f"[state] removed {len(delete_ids)} stale duplicate(s) for {token[:8]}…; kept journal row id={keep_id}")

    if removed:
        master.commit()
    return removed


def merge_active_positions(master, source_db, source_label=None):
    """Recover truly missing ACTIVE trades without resurrecting closed trades."""
    imported = 0
    con = sqlite3.connect(source_db)
    con.row_factory = sqlite3.Row
    try:
        st = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        if "positions" not in st:
            return 0

        master_cols = [x[1] for x in master.execute("pragma table_info(positions)") if x[1] != "id"]
        for r in con.execute("select * from positions where active=1"):
            # Critical v10.8 rule: ANY master row for this exact trade means the
            # shared ledger is authoritative, even when that row is CLOSED.
            if _master_has_trade(master, r):
                continue
            use = [c for c in master_cols if c in r.keys()]
            vals = [r[c] for c in use]
            master.execute(
                f"insert into positions({','.join(use)}) values({','.join('?' for _ in use)})",
                vals,
            )
            imported += 1
            label = source_label or Path(source_db).parent.name
            print(f"[state] recovered genuinely missing open position {r['symbol']} from {label}")
    finally:
        con.close()
    return imported


def main():
    cands = []
    for p in ROOT.parent.glob("*/v9_signals.db"):
        if p.resolve() != MASTER.resolve():
            cands.append(p)
    local = ROOT / "v9_signals.db"
    if local.exists() and local not in cands:
        cands.append(local)

    if not MASTER.exists() and cands:
        # First-ever shared-state migration: prefer the database with open
        # positions, then newest position/history.
        best = max(cands, key=info)
        shutil.copy2(best, MASTER)
        print(f"[state] imported shared history from {best.parent.name}; open positions={info(best)[0]}")

    if MASTER.exists():
        master = sqlite3.connect(MASTER)
        master.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in master.execute("select name from sqlite_master where type='table'")}
            if "positions" in tables:
                repaired = repair_legacy_position_duplicates(master)
                imported = 0
                for src in cands:
                    try:
                        imported += merge_active_positions(master, src, src.parent.name)
                    except Exception as exc:
                        print(f"[state] skipped {src}: {exc}")
                master.commit()
                if repaired or imported:
                    print(f"[state] ledger repair complete: removed={repaired}, recovered={imported}")
        finally:
            master.close()

    print(f"[state] shared database: {MASTER}")


if __name__ == "__main__":
    main()
