"""DayBot's database (SQLite). Everything the bot learns lives here: your fills and round trips, every check it
made, its alerts and paper trades, backtests, and the levels it computed each morning."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
create table if not exists meta(key text primary key, value text);

-- your brokerage accounts (SnapTrade, read-only)
create table if not exists accounts(
  id text primary key, name text, institution text, number text, margin text default '',
  total real, cash real, buying_power real, updated integer, status text default '', checks integer default 1);

-- every fill in your accounts. kind: stock | option. side: BUY | SELL. effect: open | close | '' (stocks)
create table if not exists fills(
  id integer primary key, account_id text, order_id text, ts integer, day text, symbol text, underlying text,
  kind text, option_type text default '', strike real default 0, expiry text default '', side text, effect text default '',
  qty real, price real, multiplier real default 1, fee real default 0, source text default '', checked integer default 0,
  unique(account_id, order_id, qty, price, ts));

-- per order: how much of it we've already booked as fills (partial fills arrive over several polls)
create table if not exists orders_seen(account_id text, order_id text, filled real, value real default 0, ts integer,
  primary key(account_id, order_id));

-- round trips built from fills (FIFO). direction long | short
create table if not exists trades(
  id integer primary key, account_id text, key text, symbol text, underlying text, kind text, option_type text default '',
  strike real default 0, expiry text default '', direction text, day text, opened_ts integer, closed_ts integer,
  qty real, open_qty real, entry_avg real, exit_avg real default 0, multiplier real default 1, cost real default 0,
  proceeds real default 0, pnl real default 0, pnl_pct real default 0, fees real default 0, r_mult real,
  plan_stop real default 0, plan_t1 real default 0, plan_t2 real default 0, plan_risk real default 0,
  und_entry real default 0, closed_qty real default 0, setup text default '', tags text default '{}',
  mfe real, mae real, status text default 'open', check_id integer default 0, note text default '');

-- every trade check (your fills and /check)
create table if not exists checks(
  id integer primary key, ts integer, symbol text, contract text default '', side text, qty real, price real,
  verdict text, items text, plan text, trade_id integer default 0, source text default '');

-- positions as the broker reports them (refreshed every few minutes)
create table if not exists positions(
  account_id text, key text, symbol text, underlying text, kind text, option_type text default '', strike real default 0,
  expiry text default '', qty real, avg_price real, price real, multiplier real default 1, updated integer,
  primary key(account_id, key));

-- setup alerts
create table if not exists alerts(
  id integer primary key, ts integer, day text, symbol text, setup text, direction text, entry real, stop real,
  t1 real, t2 real, proven integer default 0, silent integer default 0, reason text, context text default '{}',
  option_idea text default '', tg_message_id integer default 0);
create index if not exists idx_alerts_day on alerts(day, symbol);

-- paper autopilot: every alert, traded with the plan
create table if not exists paper(
  id integer primary key, alert_id integer, day text, symbol text, setup text, direction text, opened_ts integer,
  entry real, stop real, t1 real, t2 real, qty real, half_done integer default 0, stop_now real, closed_ts integer,
  exit_price real, r_mult real, pnl real, reason text default '', status text default 'open', seen_ts integer default 0,
  auto integer default 0, auto_note text default '', half_ts integer default 0, plan text default 'standard',
  entry_type text default 'market', expires integer default 0, plan_data text default '', risk real default 0,
  cost real default 0);

-- backtest results per setup (latest run kept with history)
create table if not exists backtests(
  id integer primary key, ts integer, setup text, direction text, n integer, won real, avg_r real, total_r real,
  pf real, half1 real, half2 real, no_best real, max_dd real, status text, days integer, detail text default '');

-- today's watchlist and levels
create table if not exists watch(day text, symbol text, source text, score real default 0, data text default '{}',
  added integer, primary key(day, symbol));

-- your price alerts (/alert NVDA 141.50)
create table if not exists price_alerts(id integer primary key, symbol text, price real, direction text, note text default '',
  created integer, triggered integer default 0);

-- feed for the dashboard + one-off message dedupe
create table if not exists events(id integer primary key, ts integer, kind text, symbol text default '', text text);
create table if not exists sent(key text primary key, ts integer);
create table if not exists ai_log(id integer primary key, ts integer, kind text, input_tokens integer,
  output_tokens integer, ok integer, note text default '');
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
        """Upgrade a database made by an earlier DayBot in place: add any column the schema has that it lacks."""
        ref = sqlite3.connect(":memory:")
        ref.executescript(SCHEMA)
        for (table,) in ref.execute("select name from sqlite_master where type='table'").fetchall():
            have = {r[1] for r in self.conn.execute(f"pragma table_info({table})")}
            for cid, name, kind, notnull, default, pk in ref.execute(f"pragma table_info({table})").fetchall():
                if name not in have:
                    extra = f" default {default}" if default is not None else ""
                    self.conn.execute(f"alter table {table} add column {name} {kind}{extra}")
        ref.close()

    def run(self, sql: str, params=()) -> int:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return int(cur.rowcount or 0)

    def insert(self, sql: str, params=()) -> int:
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
        self.run("insert or replace into meta(key, value) values(?, ?)", (key, str(value)))

    def once(self, key: str, ts: int) -> bool:
        """True the first time a key is seen (for one-off messages like 'your stop was hit')."""
        return self.insert("insert or ignore into sent(key, ts) values(?, ?)", (key, ts)) > 0

    def event(self, ts: int, kind: str, text: str, symbol: str = "") -> None:
        self.insert("insert into events(ts, kind, symbol, text) values(?,?,?,?)", (ts, kind, symbol, text[:2000]))

    def prune(self, now: int, keep_days: int = 120) -> None:
        cutoff = now - keep_days * 86400
        self.run("delete from events where ts<?", (cutoff,))
        self.run("delete from sent where ts<?", (cutoff,))

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
