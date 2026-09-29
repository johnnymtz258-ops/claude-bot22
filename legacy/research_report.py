"""Reproducible edge research over the bot's own data (read-only, stdlib only).

Answers, with out-of-sample checks (first half vs second half of the period):
- Do the bot's paper trades and real journal trades make money after fees?
- Across every candidate the bot evaluated, is there any baseline edge?
- Do the later filter stages pick better coins than the early ones?
- Does any single feature bucket beat fees in BOTH halves?
- Which take-profit / stop / hold rules would have worked? Exact when candidate_paths
  (path_tracker.py) exist; otherwise bounded between best- and worst-case ordering.

Usage:
  python3 research_report.py                      # shared bot database (read-only)
  python3 research_report.py --db PATH            # a specific database file
  python3 research_report.py --export DIR|ZIP...  # a review export (FOMO_REVIEW_*.zip parts)
"""
from __future__ import annotations

import argparse
import csv
import glob
import io
import math
import os
import re
import sqlite3
import statistics
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_FRICTION = 2.6   # % round trip; median simulated friction of the Sep export
STOP_OVERSHOOT = 5.0     # % typical gap beyond a stop observed in paper trading
PLAUSIBLE_MAX = 1000.0   # % — larger forward "returns" are treated as feed glitches


# ------------------------------------------------------------------ loading

def _connect_db(path: Path) -> sqlite3.Connection:
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        con.execute("select 1 from sqlite_master limit 1")
    except sqlite3.Error:
        con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _load_export(sources: list[str]) -> sqlite3.Connection:
    """Load review-export CSVs (zips or directories) into an in-memory database."""
    files: list[tuple[str, bytes]] = []
    for src in sources:
        p = Path(os.path.expanduser(src))
        if p.is_dir():
            for f in sorted(p.glob("*.csv")):
                files.append((f.name, f.read_bytes()))
            for z in sorted(p.glob("FOMO_REVIEW_*.zip")):
                sources.append(str(z))
        elif p.suffix == ".zip":
            with zipfile.ZipFile(p) as zf:
                for name in zf.namelist():
                    if name.endswith(".csv"):
                        files.append((name, zf.read(name)))
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    created = set()
    for name, data in sorted(files):
        table = re.sub(r"(_\d{3})?(_[ab])*\.csv$", "", os.path.basename(name))
        reader = csv.reader(io.StringIO(data.decode("utf-8", "replace")))
        try:
            header = next(reader)
        except StopIteration:
            continue
        if table not in created:
            cols = ",".join(f'"{c}"' for c in header)
            con.execute(f'create table "{table}" ({cols})')
            created.add(table)
        marks = ",".join("?" for _ in header)
        con.executemany(f'insert into "{table}" values ({marks})',
                        ([None if v == "" else v for v in row] for row in reader if len(row) == len(header)))
    return con


def _has_table(con, name):
    return bool(con.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone())


def _cols(con, table):
    return {r[1] for r in con.execute(f'pragma table_info("{table}")')}


def _f(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ stats helpers

def summary(pnls: list[float], amounts: list[float] | None = None) -> dict:
    n = len(pnls)
    if not n:
        return {"n": 0}
    wins = [x for x in pnls if x > 0]; losses = [x for x in pnls if x < 0]
    gl = -sum(losses)
    out = {"n": n, "win": len(wins) / n, "net": sum(pnls), "pf": (sum(wins) / gl) if gl > 0 else float("inf"),
           "ex_best": sum(pnls) - max(pnls)}
    if amounts:
        rois = [p / a * 100 for p, a in zip(pnls, amounts) if a > 0]
        out["avg_roi"] = statistics.mean(rois) if rois else 0.0
        out["med_roi"] = statistics.median(rois) if rois else 0.0
    return out


def t_stat(xs: list[float]) -> float:
    if len(xs) < 3:
        return 0.0
    sd = statistics.stdev(xs)
    return statistics.mean(xs) / (sd / math.sqrt(len(xs))) if sd > 0 else 0.0


def fmt_row(label, s, width=26):
    if not s.get("n"):
        return f"  {label:<{width}} n=0"
    pf = "inf" if math.isinf(s["pf"]) else f"{s['pf']:.2f}"
    roi = f" avg {s['avg_roi']:+6.2f}%  med {s['med_roi']:+6.2f}%" if "avg_roi" in s else ""
    return (f"  {label:<{width}} n={s['n']:<5} win {s['win']*100:4.0f}%  net ${s['net']:+8.2f}  PF {pf:>5}"
            f"{roi}  net-ex-best ${s['ex_best']:+.2f}")


class Report:
    def __init__(self):
        self.lines: list[str] = []

    def h(self, title):
        self.lines += ["", "=" * 78, title, "=" * 78]

    def p(self, *xs):
        self.lines += [str(x) for x in xs]

    def text(self):
        return "\n".join(self.lines) + "\n"


# ------------------------------------------------------------------ sections

def section_paper(con, R: Report):
    if not _has_table(con, "paper_positions"):
        return
    rows = [dict(r) for r in con.execute("select * from paper_positions where cast(active as integer)=0")]
    rows = [r for r in rows if not str(r.get("close_reason") or "").startswith("price data unreliable")]
    R.h("1. PAPER / SHADOW TRADES (simulated, net of simulated fees)")
    if not rows:
        R.p("  none closed yet"); return
    pn = [_f(r.get("realized_pnl")) for r in rows]; am = [_f(r.get("amount_usd")) for r in rows]
    R.p(fmt_row("ALL", summary(pn, am)))
    if "gross_pnl" in rows[0]:
        gross = [_f(r.get("gross_pnl")) / _f(r.get("amount_usd"), 1) * 100 for r in rows]
        fees = [_f(r.get("fees_usd")) / _f(r.get("amount_usd"), 1) * 100 for r in rows]
        R.p(f"  before fees: avg {statistics.mean(gross):+.2f}% per trade, {sum(g > 0 for g in gross)/len(gross)*100:.0f}% winners;"
            f" fees avg {statistics.mean(fees):.2f}% per trade")
    for key, title in (("build_version", "by build"), ("tier", "by alert type (tier)")):
        R.p("", f"  {title}:")
        groups = defaultdict(list)
        for r in rows:
            groups[str(r.get(key) or "?")].append(r)
        for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            R.p(fmt_row(g[:26], summary([_f(r.get("realized_pnl")) for r in rs], [_f(r.get("amount_usd")) for r in rs])))
    R.p("", "  by exit reason:")
    groups = defaultdict(list)
    for r in rows:
        groups[re.sub(r"[-+]?\d+(\.\d+)?", "#", str(r.get("close_reason") or "?"))[:40]].append(r)
    for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:8]:
        moves = [(_f(r.get("close_price")) / _f(r.get("entry_price"), 1) - 1) * 100 for r in rs if _f(r.get("entry_price")) > 0]
        R.p(fmt_row(g[:26], summary([_f(r.get("realized_pnl")) for r in rs], [_f(r.get("amount_usd")) for r in rs]))
            + (f"  avg price move {statistics.mean(moves):+.1f}%" if moves else ""))


def section_journal(con, R: Report):
    if not _has_table(con, "positions"):
        return
    rows = [dict(r) for r in con.execute("select * from positions where cast(active as integer)=0")]
    R.h("2. YOUR RECORDED TRADES (journal /bought ... /sell)")
    if not rows:
        R.p("  none"); return
    pn = [_f(r.get("realized_pnl")) for r in rows]; am = [_f(r.get("amount_usd")) for r in rows]
    R.p(fmt_row("ALL", summary(pn, am)), f"  capital traded ${sum(am):.2f}")
    worst = sorted(rows, key=lambda r: _f(r.get("realized_pnl")))[:3]
    best = sorted(rows, key=lambda r: -_f(r.get("realized_pnl")))[:3]
    R.p("  worst: " + ", ".join(f"{r.get('symbol')} ${_f(r.get('realized_pnl')):+.2f}" for r in worst))
    R.p("  best:  " + ", ".join(f"{r.get('symbol')} ${_f(r.get('realized_pnl')):+.2f}" for r in best))


def load_candidates(con):
    """One decision per token per 30 minutes, joined with 30/60/120m outcomes (non-suspect)."""
    if not (_has_table(con, "decision_ledger") and _has_table(con, "decision_outcomes")):
        return []
    suspect = " and coalesce(cast(o.suspect as integer),0)=0" if "suspect" in _cols(con, "decision_outcomes") else ""
    outs = defaultdict(dict)
    for r in con.execute(f"select decision_id,horizon_min,max_return_pct,min_return_pct,final_return_pct from decision_outcomes o where 1=1{suspect}"):
        outs[int(_f(r[0]))][int(_f(r[1]))] = (_f(r[2]), _f(r[3]), _f(r[4]))
    rows = []
    last = {}
    for r in con.execute("select * from decision_ledger order by cast(ts as integer) asc"):
        d = dict(r); did = int(_f(d["id"])); o = outs.get(did)
        if not o or not all(h in o for h in (30, 60, 120)):
            continue
        if any(o[h][0] > PLAUSIBLE_MAX or o[h][2] > PLAUSIBLE_MAX for h in o):
            continue
        ts = int(_f(d["ts"])); tok = d.get("token")
        if ts - last.get(tok, -10 ** 12) < 1800:
            continue
        last[tok] = ts
        d["o"] = o
        rows.append(d)
    return rows


def bracket(o, h, tp, sl, worst=True):
    mx, mn, fi = o[h]
    hit_sl = mn <= -sl; hit_tp = mx >= tp
    if worst:
        return -(sl + STOP_OVERSHOOT) if hit_sl else (tp if hit_tp else fi)
    return tp if hit_tp else (-(sl + STOP_OVERSHOOT) if hit_sl else fi)


def mid_r(d, friction, h=60, tp=30, sl=10):
    return (bracket(d["o"], h, tp, sl, True) + bracket(d["o"], h, tp, sl, False)) / 2 - friction


def section_universe(cands, friction, R: Report):
    R.h("3. EVERY CANDIDATE THE BOT EVALUATED (deduplicated per token per 30 min)")
    if not cands:
        R.p("  no decision outcomes available"); return
    t0 = time.strftime("%Y-%m-%d", time.gmtime(int(_f(cands[0]["ts"]))))
    t1 = time.strftime("%Y-%m-%d", time.gmtime(int(_f(cands[-1]["ts"]))))
    R.p(f"  {len(cands)} candidates, {len({c['token'] for c in cands})} tokens, {t0} → {t1}; friction assumed {friction:.1f}% round trip")
    for h in (30, 60, 120):
        fin = [c["o"][h][2] for c in cands]; mx = [c["o"][h][0] for c in cands]; mn = [c["o"][h][1] for c in cands]
        R.p(f"  {h:>3}m: median final {statistics.median(fin):+.2f}% | P(final > fees) {sum(x > friction for x in fin)/len(fin)*100:.0f}%"
            f" | P(max ≥ +10%) {sum(x >= 10 for x in mx)/len(mx)*100:.0f}% | P(min ≤ -10%) {sum(x <= -10 for x in mn)/len(mn)*100:.0f}%")
    R.p("", "  bracket exits on ALL candidates (net of fees; true ordering unknown, so best/worst case):")
    for h, tp, sl in ((30, 20, 8), (60, 30, 10), (120, 50, 15)):
        w = statistics.mean(bracket(c["o"], h, tp, sl, True) - friction for c in cands)
        b = statistics.mean(bracket(c["o"], h, tp, sl, False) - friction for c in cands)
        R.p(f"    TP{tp}/SL{sl} within {h}m: worst-case {w:+.2f}%  best-case {b:+.2f}% per trade")


def _halves(cands):
    cut = statistics.median([int(_f(c["ts"])) for c in cands])
    return cut


def section_stages(con, friction, R: Report):
    R.h("4. DO LATER FILTER STAGES PICK BETTER COINS? (mid-case TP30/SL10/60m, net)")
    if not (_has_table(con, "decision_ledger") and _has_table(con, "decision_outcomes")):
        R.p("  no data"); return
    suspect = " and coalesce(cast(o.suspect as integer),0)=0" if "suspect" in _cols(con, "decision_outcomes") else ""
    q = f"""select d.event,d.token,cast(d.ts as integer) ts,o.max_return_pct,o.min_return_pct,o.final_return_pct
            from decision_ledger d join decision_outcomes o on o.decision_id=d.id
            where cast(o.horizon_min as integer)=60{suspect} order by cast(d.ts as integer)"""
    seen = set(); per = defaultdict(list)
    for r in con.execute(q):
        key = (r["event"], r["token"], int(r["ts"]) // 1800)
        mx, mn, fi = _f(r["max_return_pct"]), _f(r["min_return_pct"]), _f(r["final_return_pct"])
        if key in seen or mx > PLAUSIBLE_MAX or fi > PLAUSIBLE_MAX:
            continue
        seen.add(key)
        o = {60: (mx, mn, fi)}
        per[r["event"]].append(((bracket(o, 60, 30, 10, True) + bracket(o, 60, 30, 10, False)) / 2 - friction, int(r["ts"])))
    allts = sorted(t for v in per.values() for _, t in v)
    cut = allts[len(allts) // 2] if allts else 0
    for ev, vals in sorted(per.items(), key=lambda kv: -len(kv[1])):
        if len(vals) < 20:
            continue
        a = [x for x, t in vals if t < cut]; b = [x for x, t in vals if t >= cut]
        R.p(f"  {ev:<18} n={len(vals):<6} mean {statistics.mean(x for x, _ in vals):+6.2f}%"
            f"  1st half {statistics.mean(a) if a else float('nan'):+6.2f}%  2nd half {statistics.mean(b) if b else float('nan'):+6.2f}%")


FEATURES = ("entry_score", "confirmed_score", "mcap", "liquidity", "pc5", "pc1", "buy_sell", "swaps", "turnover_pct")


def section_features(cands, friction, R: Report):
    R.h("5. FEATURE BUCKETS (quintiles) — positive in BOTH halves would be an edge")
    if not cands:
        R.p("  no data"); return
    cut = _halves(cands)
    found = []
    for feat in FEATURES:
        xs = [(_f(c.get(feat)), mid_r(c, friction), int(_f(c["ts"])) < cut) for c in cands if c.get(feat) not in (None, "")]
        if len({x for x, _, _ in xs}) < 5:
            continue
        xs.sort(key=lambda t: t[0])
        q = len(xs) // 5
        parts = []
        for i in range(5):
            chunk = xs[i * q:(i + 1) * q if i < 4 else len(xs)]
            a = [r for _, r, h in chunk if h]; b = [r for _, r, h in chunk if not h]
            ma = statistics.mean(a) if a else float("nan"); mb = statistics.mean(b) if b else float("nan")
            parts.append(f"Q{i+1}[{chunk[0][0]:.3g}..{chunk[-1][0]:.3g}] {ma:+.1f}/{mb:+.1f}")
            if a and b and ma > 0 and mb > 0 and len(chunk) >= 100:
                found.append(f"{feat} Q{i+1}")
        R.p(f"  {feat}:", "    " + " | ".join(parts))
    R.p("", "  buckets positive in both halves: " + (", ".join(found) if found else "NONE"))


def section_exit_grid(con, cands, friction, R: Report):
    R.h("6. EXIT RULES — which take-profit / stop / hold would have worked?")
    paths = []
    if _has_table(con, "candidate_paths"):
        paths = [dict(r) for r in con.execute("select * from candidate_paths where status in ('DONE','LOST')")]
    grid = [(tp, sl, hold) for tp in (10, 20, 30, 50) for sl in (5, 8, 10, 15) for hold in (30, 60, 120)]
    if len(paths) >= 200:
        from path_tracker import simulate_exit
        R.p(f"  EXACT backtest on {len(paths)} recorded candidate paths (stop gap {STOP_OVERSHOOT:.0f}%, fees {friction:.1f}%)")
        paths.sort(key=lambda r: int(_f(r["start_ts"])))
        cut = int(_f(paths[len(paths) // 2]["start_ts"]))
        res = []
        for tp, sl, hold in grid:
            a, b = [], []
            for r in paths:
                x = simulate_exit(r, tp, sl, hold * 60, stop_slippage=STOP_OVERSHOOT)
                if x is None:
                    continue
                (a if int(_f(r["start_ts"])) < cut else b).append(x - friction)
            if a and b:
                res.append((statistics.mean(a + b), statistics.mean(a), statistics.mean(b), t_stat(a + b), tp, sl, hold, len(a) + len(b)))
        res.sort(reverse=True)
        for m, ma, mb, t, tp, sl, hold, n in res[:10]:
            R.p(f"    TP{tp:<3} SL{sl:<3} hold {hold:>3}m: {m:+6.2f}% (halves {ma:+.2f}/{mb:+.2f}, t={t:+.1f}, n={n})")
        both = [r for r in res if r[1] > 0 and r[2] > 0 and r[3] >= 2.0]
        R.p("  rules positive in both halves with t ≥ 2: " + (", ".join(f"TP{r[4]}/SL{r[5]}/{r[6]}m" for r in both) or "NONE"))
    else:
        R.p(f"  only {len(paths)} recorded paths so far (need 200 for an exact backtest) — showing bounds from max/min:")
        for tp, sl, hold in [(10, 8, 30), (20, 8, 60), (30, 10, 60), (50, 15, 120)]:
            if not cands:
                break
            w = statistics.mean(bracket(c["o"], hold, tp, sl, True) - friction for c in cands)
            b = statistics.mean(bracket(c["o"], hold, tp, sl, False) - friction for c in cands)
            R.p(f"    TP{tp} SL{sl} hold {hold}m: between {w:+.2f}% and {b:+.2f}% per trade")


def section_model(cands, friction, R: Report):
    R.h("7. CAN A MODEL RANK CANDIDATES? (walk-forward, trained only on the past)")
    try:
        from sklearn.ensemble import HistGradientBoostingRegressor
    except Exception:
        R.p("  scikit-learn not installed — skipped (pip install scikit-learn to enable)"); return
    if len(cands) < 2000:
        R.p("  fewer than 2000 candidates — skipped"); return
    import numpy as np
    X = np.array([[_f(c.get(f)) for f in FEATURES] + [_f(c.get("liquidity")) / max(_f(c.get("mcap")), 1)] for c in cands])
    y = np.array([mid_r(c, friction, 120, 50, 15) for c in cands])
    blocks = np.array_split(np.arange(len(cands)), 5)
    for k in range(1, 5):
        tr = np.concatenate(blocks[:k]); te = blocks[k]
        m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=100)
        m.fit(X[tr], np.clip(y[tr], -30, 60))
        p = m.predict(X[te]); yt = y[te]
        top = yt[p >= np.quantile(p, 0.9)]
        R.p(f"  fold {k}: all {yt.mean():+.2f}%  model top-10% {top.mean():+.2f}% (n={len(top)})  [TP50/SL15/120m mid-case, net]")


def build_report(con, friction=DEFAULT_FRICTION) -> str:
    R = Report()
    R.p(f"FOMOBOT RESEARCH REPORT — generated {time.strftime('%Y-%m-%d %H:%M')}",
        "Read-only analysis. Positive numbers in ONE half only are usually luck; look for results that hold in both halves.")
    section_paper(con, R)
    section_journal(con, R)
    cands = load_candidates(con)
    section_universe(cands, friction, R)
    section_stages(con, friction, R)
    section_features(cands, friction, R)
    section_exit_grid(con, cands, friction, R)
    section_model(cands, friction, R)
    return R.text()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="database file (default: shared bot database)")
    ap.add_argument("--export", nargs="+", help="review export zips or a directory of CSVs")
    ap.add_argument("--friction", type=float, default=DEFAULT_FRICTION, help="round-trip fees+slippage in %%")
    ap.add_argument("--out", help="write the report here (default: RESEARCH_REPORT_<time>.txt next to this script)")
    args = ap.parse_args(argv)
    if args.export:
        con = _load_export(list(args.export))
    else:
        if args.db:
            db = Path(os.path.expanduser(args.db))
        else:
            from export_review import _env_values, resolve_db
            db = resolve_db(_env_values(ROOT / ".env"))
        if not db.exists():
            print(f"No database at {db}"); return 1
        con = _connect_db(db)
    text = build_report(con, args.friction)
    out = Path(args.out) if args.out else ROOT / f"RESEARCH_REPORT_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    out.write_text(text)
    print(text)
    print(f"Saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
