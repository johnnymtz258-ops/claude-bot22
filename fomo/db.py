"""SQLite storage. One file, a handful of tables, no hidden state.

whales      wallets you follow (muted = still tracked and scored, but no alerts)
swaps       every parsed buy/sell by a whale or by your own wallet
tokens      latest market data + safety for each coin seen
alerts      what was sent to Telegram and why
copies      simulated follower copies of whale buys (used to score each whale)
my_trades   your own buys/sells: exact from wallet sync, or typed with /bought /sold
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
create table if not exists meta(key text primary key, value text);

create table if not exists whales(
    address text primary key, name text not null, added_ts integer, source text default 'manual',
    active integer default 1, muted integer default 0, auto_muted integer default 0,
    last_trade_ts integer default 0, note text default '');

create table if not exists processed(
    sig text, wallet text, ts integer, primary key(sig, wallet));

create table if not exists swaps(
    id integer primary key autoincrement, sig text, wallet text, is_me integer default 0,
    ts integer, seen_ts integer, side text, mint text, token_amount real, base text,
    base_amount real, usd_value real, price_usd real, mc_usd real, sell_fraction real default 0,
    holding_after real default 0, pre_holding real default 0, dex text default '',
    source text default 'stream', unique(sig, wallet, mint));
create index if not exists idx_swaps_mint_ts on swaps(mint, ts);
create index if not exists idx_swaps_wallet_ts on swaps(wallet, ts);

create table if not exists tokens(
    mint text primary key, symbol text default '', name text default '', decimals integer,
    supply real default 0, price_usd real default 0, mc_usd real default 0,
    liquidity_usd real default -1, pair_address text default '', dex text default '',
    pair_created_ts integer default 0, image text default '', url text default '',
    mint_authority text, freeze_authority text, safety_ts integer default 0,
    updated_ts integer default 0);

create table if not exists alerts(
    id integer primary key autoincrement, ts integer, kind text, mint text, wallet text,
    swap_id integer, grade text default '', mc_usd real default 0, price_usd real default 0,
    chase_pct real default 0, confluence integer default 1, reasons text default '',
    tg_message_id integer default 0);
create index if not exists idx_alerts_mint_ts on alerts(mint, ts);

create table if not exists copies(
    id integer primary key autoincrement, whale text, mint text, swap_id integer,
    alert_id integer default 0, open_ts integer, entry_price real, entry_mc real default 0,
    whale_price real default 0, remaining real default 1, proceeds real default 0,
    status text default 'open', close_ts integer default 0, close_reason text default '',
    return_pct real, last_price real, last_ts integer, peak_price real, peak_ts integer,
    low_price real, dip_before_peak_pct real default 0, hit_2x_ts integer default 0,
    p5m real, p15m real, p1h real, p4h real, p24h real, pending_price real, pending_ts integer);
create index if not exists idx_copies_whale on copies(whale, open_ts);
create index if not exists idx_copies_status on copies(status);

create table if not exists my_trades(
    id integer primary key autoincrement, ts integer, mint text, side text, usd real,
    tokens real, price_usd real default 0, mc_usd real default 0, source text,
    sig text, wallet text default '', note text default '', unique(sig));
create index if not exists idx_my_trades_mint on my_trades(mint, ts);

create table if not exists token_watch(
    mint text primary key, liq_peak real default 0, liq_peak_ts integer default 0,
    liq_peak_price real default 0, rug_pending_ts integer default 0, rug_alert_ts integer default 0,
    initial_note_ts integer default 0);

create table if not exists tg_messages(
    message_id integer primary key, mint text, wallet text, kind text, ts integer);

create table if not exists price_marks(
    mint text, ts integer, price real, primary key(mint, ts));

create table if not exists whale_candidates(
    address text primary key, found_ts integer, analyzed_ts integer default 0, coins text default '',
    verdict text default '', pnl_sol real default 0, win_rate real default 0, trips integer default 0,
    median_hold_s real default 0, last_trade_ts integer default 0, status text default 'new',
    reason text default '');

create table if not exists position_notes(
    mint text, kind text, level real, ts integer, primary key(mint, kind, level));

create table if not exists coins(
    mint text primary key, symbol text, added_ts integer, base_price real, base_mc real,
    entry_price real default 0, active integer default 1, source text default 'manual');

create table if not exists live_trades(
    id integer primary key autoincrement, paper_id integer, mint text, symbol text, open_ts integer,
    sol_in real default 0, sol_out real default 0, tokens_raw integer default 0, buy_sig text, sell_sigs text,
    status text, close_ts integer, close_reason text, dry integer default 0);

create table if not exists paper_trades(
    id integer primary key autoincrement, alert_id integer, mint text, whale text, symbol text, open_ts integer,
    entry_price real, entry_mc real, size_usd real, tokens real, remaining real, proceeds_usd real default 0,
    peak_x real default 1, last_price real default 0, half_taken integer default 0, status text,
    close_ts integer, close_reason text);
create index if not exists idx_paper_status on paper_trades(status);

create table if not exists paper_equity(ts integer primary key, equity real);

create table if not exists whale_profiles(
    address text primary key, data text, updated_ts integer default 0);

create table if not exists watches(
    id integer primary key autoincrement, mint text, target_mc real, base_mc real, direction text,
    created_ts integer, hit_ts integer default 0);

create table if not exists finds(
    id integer primary key autoincrement, ts integer, mints text, status text,
    progress text default '', result_json text default '', finished_ts integer default 0);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma synchronous=normal")
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()
        self.conn.commit()

    def _add_missing_columns(self) -> None:
        """Upgrade databases created by earlier versions in place (nothing is lost)."""
        columns = {r[1] for r in self.conn.execute("pragma table_info(alerts)")}
        if "status" not in columns:
            self.conn.execute("alter table alerts add column status text default ''")
        # what the coin did after the alert (peak and price 1h / 6h / 24h later), and whether it was a scalp
        for name, kind in (("scalp", "integer default 0"), ("peak_price", "real default 0"), ("peak_ts", "integer default 0"),
                           ("p1h", "real"), ("p6h", "real"), ("p24h", "real")):
            if name not in columns:
                self.conn.execute(f"alter table alerts add column {name} {kind}")

    # -- tiny query helpers -------------------------------------------------------------
    def run(self, sql: str, params=()) -> int:
        """Execute + commit. Returns the number of rows changed."""
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return int(cur.rowcount or 0)

    def insert(self, sql: str, params=()) -> int:
        """Execute an INSERT + commit. Returns the new row id, or 0 when it was ignored."""
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return int(cur.lastrowid or 0) if cur.rowcount == 1 else 0

    def rows(self, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def row(self, sql: str, params=()) -> dict | None:
        r = self.conn.execute(sql, params).fetchone()
        return dict(r) if r else None

    def scalar(self, sql: str, params=(), default=None):
        r = self.conn.execute(sql, params).fetchone()
        return r[0] if r and r[0] is not None else default

    def get_meta(self, key: str, default: str = "") -> str:
        v = self.scalar("select value from meta where key=?", (key,))
        return default if v is None else str(v)

    def set_meta(self, key: str, value) -> None:
        self.run("insert into meta(key,value) values(?,?) on conflict(key) do update set value=excluded.value",
                 (key, str(value)))

    def close(self) -> None:
        self.conn.close()

    # -- maintenance ---------------------------------------------------------------------
    def prune(self, keep_days: int = 14) -> None:
        cutoff = int(time.time()) - keep_days * 86400
        self.run("delete from processed where ts<?", (cutoff,))
        self.run("delete from price_marks where ts<?", (int(time.time()) - 45 * 86400,))
        self.run("delete from tg_messages where ts<?", (int(time.time()) - 60 * 86400,))


OUTCOME_POINTS = (("p1h", 3600, 3600), ("p6h", 6 * 3600, 3 * 3600), ("p24h", 86400, 6 * 3600))  # (column, after, grace)


def backfill_alert_outcomes(db: Database, now: int | None = None) -> int:
    """Fill in what alerted coins did afterwards from prices the bot already recorded (older databases)."""
    now = int(now or time.time())
    filled = 0
    for a in db.rows("""select id,ts,mint,price_usd,peak_price,p1h,p6h,p24h from alerts
            where kind='BUY' and price_usd>0 and ts<=? and (p1h is null or p6h is null or p24h is null)""", (now - 3600,)):
        updates = {}
        for col, after, grace in OUTCOME_POINTS:
            if a[col] is not None or a["ts"] + after > now:
                continue
            lo, hi = a["ts"] + after, a["ts"] + after + grace
            price = db.scalar("select price from price_marks where mint=? and ts between ? and ? order by ts limit 1",
                              (a["mint"], lo, hi))
            if not price:
                price = db.scalar("select last_price from copies where alert_id=? and last_ts between ? and ?",
                                  (a["id"], lo, hi))
            if not price:
                price = db.scalar("select price_usd from tokens where mint=? and updated_ts between ? and ?",
                                  (a["mint"], lo, hi))
            if not price and col == "p24h" and a["p6h"] is None and "p6h" not in updates:
                # older databases only kept a coin's latest price: any reading 6-48h later stands in for "a day later"
                price = db.scalar("select price_usd from tokens where mint=? and updated_ts between ? and ?",
                                  (a["mint"], a["ts"] + 6 * 3600, a["ts"] + 48 * 3600))
            if price and price > 0:
                updates[col] = float(price)
        peak = max([float(a["peak_price"] or 0)] + [float(x or 0) for x in (
            db.scalar("select max(price) from price_marks where mint=? and ts between ? and ?",
                      (a["mint"], a["ts"], a["ts"] + 86400)),
            db.scalar("select max(peak_price) from copies where alert_id=?", (a["id"],)))])
        if peak > float(a["peak_price"] or 0):
            updates["peak_price"] = peak
        if updates:
            db.run(f"update alerts set {', '.join(k + '=?' for k in updates)} where id=?", (*updates.values(), a["id"]))
            filled += 1
    return filled


def coin_addresses_in_whales(db: Database) -> list[str]:
    """'Whales' that are really coin addresses — the app moves them to tracked coins (nothing is lost)."""
    from .whales import looks_like_coin
    return [w["address"] for w in db.rows("select address from whales where active=1")
            if looks_like_coin(db, w["address"])]


def drop_autopilot_whales(db: Database) -> int:
    """One-time: unfollow whales the earlier autopilot followed by itself (whales you added are kept)."""
    if db.get_meta("autopilot_whales_dropped") == "1":
        return 0
    dropped = db.run("update whales set active=0 where source='auto' and active=1")
    db.set_meta("autopilot_whales_dropped", "1")
    return dropped


def import_legacy_whales(db: Database, legacy_db: Path, wallet_files: list[Path]) -> int:
    """One-time import of wallets tracked by the old bot (v16 copy engine) and tracked_wallets.json."""
    if db.get_meta("legacy_import_done") == "1":
        return 0
    found: dict[str, str] = {}
    try:
        if legacy_db.exists():
            old = sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)
            try:
                for address, label, enabled in old.execute("select address,label,enabled from copy_wallets"):
                    if enabled:
                        found[str(address)] = str(label or "")
            finally:
                old.close()
    except sqlite3.Error:
        pass  # old database missing the table or unreadable: nothing to import
    for path in wallet_files:
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            continue
        for item in data.get("wallets") or []:
            if isinstance(item, str):
                found.setdefault(item, "")
            elif isinstance(item, dict) and item.get("address"):
                address, label = str(item["address"]), str(item.get("label") or item.get("name") or "")
                if not found.get(address) or found[address].startswith("auto-"):
                    found[address] = label  # a name you typed beats an auto-discovery tag
    added = 0
    now = int(time.time())
    from .util import is_address, short
    for address, label in found.items():
        if not is_address(address):
            continue
        name = label if label and not label.startswith("auto-") else short(address)
        added += bool(db.run("insert or ignore into whales(address,name,added_ts,source) values(?,?,?,?)",
                             (address, name[:32], now, "imported")))
    db.set_meta("legacy_import_done", "1")
    return added
