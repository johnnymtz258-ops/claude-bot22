"""Forward price-path recorder for every candidate the bot evaluates.

Why this exists
---------------
Outcome capture used to read ``observations``, which only contains coins that are still in
the scanner's candidate lists. Coins that crash tend to drop out of those lists, so their
recorded "final" price was simply the last price seen -- a survivorship bias -- and max/min
could not say which came first, so take-profit/stop rules could not be backtested exactly.

This module follows each candidate (deduplicated per token) for ``horizon`` seconds after
the decision, whether or not the scanner still lists it, with one batched price request per
30 tokens. For each path it stores:
- max/min return and when they happened,
- the first time +10/+20/+30/+50/+100% and -5/-8/-10/-15/-25/-50% were touched,
- returns at 5/15/30/60/120 minutes, and
- a compact sampled path ("seconds:return%" pairs) for trailing-stop simulations.

Price-feed glitches are rejected with the AMM plausibility check and a confirmation rule
for sudden 5x jumps, so a bad quote can never become a recorded "runner".

It is research data only: nothing here can open, size or close a trade.
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable, Mapping, Sequence

from edge_engine import price_move_plausible

UP_LEVELS = (10, 20, 30, 50, 100)
DOWN_LEVELS = (5, 8, 10, 15, 25, 50)
CHECKPOINTS_MIN = (5, 15, 30, 60, 120)
SPIKE_RATIO = 5.0

FetchBatch = Callable[[str, Sequence[str]], Awaitable[Mapping[str, Mapping]]]


def _f(v, default=0.0):
    try:
        x = float(v)
        return x if x == x else default
    except (TypeError, ValueError):
        return default


def _col(level, up):
    return f"tu{level}" if up else f"td{level}"


def init_schema(conn):
    up = ",".join(f"{_col(l, True)} integer" for l in UP_LEVELS)
    down = ",".join(f"{_col(l, False)} integer" for l in DOWN_LEVELS)
    cps = ",".join(f"r{m} real" for m in CHECKPOINTS_MIN)
    conn.execute(f"""create table if not exists candidate_paths(
        id integer primary key autoincrement, decision_id integer unique, chain text, token text,
        symbol text, event text, tier text, entry_score real, confirmed_score real,
        start_ts integer, entry_price real, entry_liq real, entry_mcap real,
        pc5 real, pc1 real, buy_sell real, swaps real, turnover_pct real, market_regime text,
        horizon_s integer, status text default 'TRACKING', last_ts integer default 0,
        samples integer default 0, dropped integer default 0, last_price real default 0,
        pending_price real, pending_ts integer,
        max_ret real, min_ret real, t_max integer, t_min integer, final_ret real,
        {cps}, {up}, {down}, path text default '')""")
    conn.execute("create index if not exists idx_candidate_paths_status on candidate_paths(status,start_ts)")
    conn.execute("create index if not exists idx_candidate_paths_token on candidate_paths(token,start_ts)")


class CandidatePathTracker:
    def __init__(self, db, fetch_batch: FetchBatch, *, horizon_s: int = 7200, poll_s: int = 30,
                 dedupe_s: int = 1800, max_active: int = 300, lost_s: int = 900,
                 sample_s: int = 60, retention_days: int = 30, now_fn=time.time):
        self.db = db
        self.fetch_batch = fetch_batch
        self.horizon_s = int(horizon_s)
        self.poll_s = int(poll_s)
        self.dedupe_s = int(dedupe_s)
        self.max_active = int(max_active)
        self.lost_s = int(lost_s)
        self.sample_s = int(sample_s)
        self.retention_days = int(retention_days)
        self.now = now_fn
        self.skipped_capacity = 0
        with db.transaction():
            init_schema(db.conn)
        if not db.get_meta("path_tracker_last_decision_id", ""):
            # Start from "now": earlier decisions' prices are already gone.
            last = db.conn.execute("select coalesce(max(id),0) from decision_ledger").fetchone()[0]
            db.set_meta("path_tracker_last_decision_id", int(last or 0))

    # -------------------------------------------------------------- enrollment
    def enroll_new(self, limit: int = 500) -> int:
        last = int(self.db.get_meta("path_tracker_last_decision_id", "0") or 0)
        rows = [dict(r) for r in self.db.conn.execute(
            """select * from decision_ledger where id>? order by id asc limit ?""", (last, int(limit))).fetchall()]
        if not rows:
            return 0
        active = self.db.conn.execute("select count(*) from candidate_paths where status='TRACKING'").fetchone()[0]
        added = 0
        with self.db.transaction():
            for d in rows:
                last = max(last, int(d["id"]))
                price = _f(d.get("price"))
                if price <= 0 or not d.get("token"):
                    continue
                recent = self.db.conn.execute(
                    "select 1 from candidate_paths where token=? and chain=? and start_ts>? limit 1",
                    (d["token"], d.get("chain") or "solana", int(d["ts"]) - self.dedupe_s)).fetchone()
                if recent:
                    continue
                if active >= self.max_active:
                    self.skipped_capacity += 1
                    continue
                self.db.conn.execute("""insert or ignore into candidate_paths(
                    decision_id,chain,token,symbol,event,tier,entry_score,confirmed_score,start_ts,entry_price,entry_liq,
                    entry_mcap,pc5,pc1,buy_sell,swaps,turnover_pct,market_regime,horizon_s,last_ts,last_price,max_ret,min_ret,
                    t_max,t_min,final_ret)
                    values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,0,0)""",
                    (int(d["id"]), d.get("chain") or "solana", d["token"], d.get("symbol"), d.get("event"), d.get("tier"),
                     _f(d.get("entry_score")), _f(d.get("confirmed_score")), int(d["ts"]), price, _f(d.get("liquidity")),
                     _f(d.get("mcap")), _f(d.get("pc5")), _f(d.get("pc1")), _f(d.get("buy_sell")), _f(d.get("swaps")),
                     _f(d.get("turnover_pct")), d.get("market_regime"), self.horizon_s, int(d["ts"]), price))
                active += 1
                added += 1
            self.db.conn.execute("insert or replace into meta(key,value) values('path_tracker_last_decision_id',?)", (str(last),))
        return added

    # -------------------------------------------------------------- sampling
    def _accept(self, row: dict, ts: int, price: float) -> dict:
        entry = _f(row["entry_price"])
        ret = (price / entry - 1.0) * 100.0
        t = max(0, int(ts) - int(row["start_ts"]))
        upd = {"last_ts": int(ts), "last_price": price, "samples": int(row["samples"] or 0) + 1, "final_ret": ret}
        # The path starts at the entry (0% at t=0); only new extremes move max/min.
        if ret > _f(row["max_ret"]):
            upd["max_ret"], upd["t_max"] = ret, t
        if ret < _f(row["min_ret"]):
            upd["min_ret"], upd["t_min"] = ret, t
        for level in UP_LEVELS:
            c = _col(level, True)
            if row.get(c) is None and ret >= level:
                upd[c] = t
        for level in DOWN_LEVELS:
            c = _col(level, False)
            if row.get(c) is None and ret <= -level:
                upd[c] = t
        for m in CHECKPOINTS_MIN:
            c = f"r{m}"
            if row.get(c) is None and t >= m * 60 and t <= m * 60 + 3 * self.poll_s:
                upd[c] = ret
        path = str(row.get("path") or "")
        last_t = int(path.rsplit(";", 1)[-1].split(":", 1)[0]) if path else -10 ** 9
        if t - last_t >= self.sample_s:
            upd["path"] = (path + ";" if path else "") + f"{t}:{ret:.2f}"
        row.update(upd)
        return upd

    def ingest(self, row: dict, ts: int, pair: Mapping | None) -> dict:
        """Apply one observation to a path row (mutates ``row``); returns fields to persist."""
        upd: dict = {}
        if not pair:
            return upd
        price = _f(pair.get("priceUsd"))
        liq = _f(((pair.get("liquidity") or {}).get("usd")))
        if price <= 0:
            return upd
        entry = _f(row["entry_price"]); entry_liq = _f(row["entry_liq"])
        if not price_move_plausible(price / entry, (liq / entry_liq) if entry_liq > 0 else None):
            upd["dropped"] = int(row.get("dropped") or 0) + 1
            row.update(upd)
            return upd
        last = _f(row.get("last_price")) or entry
        pending = _f(row.get("pending_price"))
        if pending > 0:
            if max(price, pending) / min(price, pending) <= 2.0:
                upd.update(self._accept(row, int(row.get("pending_ts") or ts), pending))
            else:
                upd["dropped"] = int(row.get("dropped") or 0) + 1
            upd["pending_price"], upd["pending_ts"] = None, None
            row.update({"pending_price": None, "pending_ts": None, **({"dropped": upd["dropped"]} if "dropped" in upd else {})})
            last = _f(row.get("last_price")) or entry
        if price >= SPIKE_RATIO * last or price * SPIKE_RATIO <= last:
            upd["pending_price"], upd["pending_ts"] = price, int(ts)
            row.update({"pending_price": price, "pending_ts": int(ts)})
            return upd
        upd.update(self._accept(row, ts, price))
        return upd

    async def poll(self) -> dict:
        now = int(self.now())
        rows = [dict(r) for r in self.db.conn.execute(
            "select * from candidate_paths where status='TRACKING' order by start_ts asc").fetchall()]
        stats = {"tracking": len(rows), "updated": 0, "finished": 0}
        if not rows:
            return stats
        live = [r for r in rows if now - int(r["start_ts"]) <= int(r["horizon_s"])]
        by_chain: dict[str, list[str]] = {}
        for r in live:
            by_chain.setdefault(str(r["chain"] or "solana"), []).append(r["token"])
        prices: dict = {}
        answered: set = set()
        for chain, tokens in by_chain.items():
            try:
                got = await self.fetch_batch(chain, list(dict.fromkeys(tokens)))
            except Exception:
                got = {}
            # fetch_batch may return (quotes, answered_tokens); a plain dict means only the
            # returned tokens are known to have been answered.
            if isinstance(got, tuple):
                got, ok = got
            else:
                ok = set((got or {}).keys())
            answered |= {(chain, t) for t in (ok or ())}
            for tok, pair in (got or {}).items():
                prices[(chain, tok)] = pair
        with self.db.transaction():
            for r in rows:
                upd = {}
                age = now - int(r["start_ts"])
                if age <= int(r["horizon_s"]):
                    upd = self.ingest(r, now, prices.get((str(r["chain"] or "solana"), r["token"])))
                if age > int(r["horizon_s"]):
                    upd["status"] = "DONE"
                elif (now - int(r.get("last_ts") or r["start_ts"]) > self.lost_s and not upd.get("last_ts")
                      and (str(r["chain"] or "solana"), r["token"]) in answered):
                    # Only a coin the price source answered for (with no usable price) is lost;
                    # a failed request (network outage) never ends tracking.
                    upd["status"] = "LOST"
                if upd:
                    sets = ",".join(f"{k}=?" for k in upd)
                    self.db.conn.execute(f"update candidate_paths set {sets} where id=?", (*upd.values(), int(r["id"])))
                    stats["updated"] += 1
                    if upd.get("status") in {"DONE", "LOST"}:
                        stats["finished"] += 1
        return stats

    def prune(self) -> int:
        cutoff = int(self.now()) - self.retention_days * 86400
        with self.db.transaction():
            cur = self.db.conn.execute("delete from candidate_paths where start_ts<?", (cutoff,))
        return cur.rowcount or 0

    async def step(self) -> dict:
        added = self.enroll_new()
        stats = await self.poll()
        stats["enrolled"] = added
        return stats


# ------------------------------------------------------------------ backtesting helpers

def parse_path(path: str) -> list[tuple[int, float]]:
    out = []
    for part in str(path or "").split(";"):
        if ":" in part:
            t, r = part.split(":", 1)
            try:
                out.append((int(t), float(r)))
            except ValueError:
                continue
    return out


def simulate_exit(row: Mapping, tp: float | None, sl: float | None, max_hold_s: int,
                  trail: float | None = None, stop_slippage: float = 0.0) -> float | None:
    """Gross return (%) of a TP/SL/time/trailing exit on one recorded path.

    Take-profit and stop ordering is exact whenever the level is one of the recorded
    first-touch thresholds; otherwise the sampled path is used. ``stop_slippage`` adds the
    typical overshoot beyond the stop observed in practice. Returns None for unusable rows.
    """
    pts = parse_path(row.get("path") or "")
    if not pts:
        return None

    def touch(level: float, up: bool):
        col = _col(int(level), up) if (int(level) == level and int(level) in (UP_LEVELS if up else DOWN_LEVELS)) else None
        if col is not None:
            v = row.get(col)
            return int(v) if v is not None else None
        for t, r in pts:
            if (r >= level) if up else (r <= -level):
                return t
        return None

    t_tp = touch(tp, True) if tp else None
    t_sl = touch(sl, False) if sl else None
    t_tr = None
    if trail:
        peak = -1e9
        for t, r in pts:
            peak = max(peak, r)
            if peak > 0 and (1 + r / 100) / (1 + peak / 100) - 1 <= -trail / 100:
                t_tr = t
                trail_ret = r
                break
    candidates = []
    if t_tp is not None and t_tp <= max_hold_s:
        candidates.append((t_tp, float(tp)))
    if t_sl is not None and t_sl <= max_hold_s:
        candidates.append((t_sl, -float(sl) - stop_slippage))
    if t_tr is not None and t_tr <= max_hold_s:
        candidates.append((t_tr, trail_ret))
    if candidates:
        return min(candidates, key=lambda c: (c[0], c[1]))[1]
    held = [r for t, r in pts if t <= max_hold_s]
    return held[-1] if held else None
