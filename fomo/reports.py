"""Numbers shared by Telegram and the dashboard, so both always agree."""
from __future__ import annotations

import statistics
import time

from .copies import return_pct
from .util import num


def _copy_returns(db, cfg, since: int) -> list[dict]:
    fee = cfg.get("COPY_FEE_PCT")
    rows = db.rows("""select c.*, a.grade, a.confluence from copies c left join alerts a on a.id=c.alert_id
        where c.open_ts>=? order by c.open_ts""", (since,))
    for r in rows:
        r["ret"] = num(r["return_pct"]) if r["status"] == "closed" else return_pct(r, num(r["last_price"]), fee)
        r["peak_x"] = num(r["peak_price"]) / num(r["entry_price"]) if num(r["entry_price"]) > 0 else 0.0
    return rows


def _summ(rows: list[dict]) -> dict:
    rets = [min(r["ret"], 1000.0) for r in rows]
    n = len(rets)
    return {"n": n, "win_rate": sum(x > 0 for x in rets) / n if n else 0.0, "avg": sum(rets) / n if n else 0.0,
            "median": statistics.median(rets) if n else 0.0, "per_100": sum(rets),
            "hit_2x": sum(1 for r in rows if r["hit_2x_ts"]) / n if n else 0.0}


def copy_report(db, cfg, days: int = 30) -> dict:
    everything = _copy_returns(db, cfg, int(time.time()) - days * 86400)
    rows = [r for r in everything if r["whale"] != "runner"]  # whale copies; runners reported separately
    winners = [r for r in rows if r["peak_x"] >= 1.5]
    by_grade = {g: _summ([r for r in rows if r.get("grade") == g]) for g in ("A", "B", "C")}
    return {
        "days": days, "all": _summ(rows), "open": sum(r["status"] == "open" for r in rows),
        "by_grade": by_grade,
        "confluence": _summ([r for r in rows if num(r.get("confluence")) >= 2]),
        "solo": _summ([r for r in rows if num(r.get("confluence")) < 2]),
        "typical_dip": statistics.median([num(r["dip_before_peak_pct"]) for r in winners]) if winners else 0.0,
        "winners": len(winners),
        "runners": _summ([r for r in everything if r["whale"] == "runner"]),
        # how many winners would a -20% stop-loss have sold before their run?
        "stop20_would_kill": sum(1 for r in winners if num(r["dip_before_peak_pct"]) <= -20),
    }


FUNNEL_LABELS = {
    "sent": "sent", "silent": "sent silently (grade C)", "whale_only": "hidden: whale-only coin",
    "muted": "from muted whales", "paused": "while alerts were paused", "late": "seen too late",
    "unsafe": "unsafe coin (freeze authority)", "too_small": "below MIN_WHALE_BUY_USD",
    "too_big": "above MAX_ENTRY_MC_USD", "earlier": "before this update",
    "scalp": "hidden: scalp setup (older version)",
    "chased": "not sent: already ran past the whale's price",
    "dumping": "not sent: already dumping below the whale's price",
    "micro": "not sent: micro-cap (MICRO_ALERTS off)",
}


def grade_outcomes(db, days: float = 14) -> dict:
    """Where alerted coins were hours later (6h, else 24h), by grade and for ⚡ scalp setups — keeps grades honest."""
    from .whales import aftermath
    shown = "coalesce(a.status,'') not in ('late','unsafe')"
    out = {g: aftermath(db, f"{shown} and a.grade=? and a.scalp=0", (g,), days) for g in ("A", "B", "C")}
    out["scalp"] = aftermath(db, f"{shown} and a.scalp=1", (), days)
    return out


def alert_funnel(db, hours: float = 24) -> dict:
    """What happened to every whale buy seen recently — explains quiet days."""
    rows = db.rows("""select status, count(*) n from alerts where kind in ('BUY','SEEN') and ts>=?
        group by status""", (int(time.time() - hours * 3600),))
    counts = {r["status"] or "earlier": int(r["n"]) for r in rows}
    return {"total": sum(counts.values()), "counts": counts}


def recent_buys(db, market, whales, limit: int = 15, since_hours: float = 48) -> list[dict]:
    rows = db.rows("""select a.*, s.usd_value, s.ts trade_ts from alerts a left join swaps s on s.id=a.swap_id
        where a.kind in ('BUY','RUNNER') and a.grade<>'SKIP' and a.ts>=? order by a.ts desc limit ?""",
                   (int(time.time() - since_hours * 3600), limit))
    out = []
    for r in rows:
        info = market.cached(r["mint"])
        swap_mc = num(db.scalar("select mc_usd from swaps where id=?", (r["swap_id"],))) or num(r["mc_usd"])
        now_mc = num(info.get("mc_usd"))
        out.append({**r, "symbol": info.get("symbol") or r["mint"][:4], "whale": whales.name(r["wallet"]),
                    "entry_mc": swap_mc, "now_mc": now_mc, "image": info.get("image") or "",
                    "change": (now_mc / swap_mc - 1) * 100 if swap_mc > 0 and now_mc > 0 else None})
    return out


def hot_coins(db, market, whales, hours: float = 24, min_whales: int = 2) -> list[dict]:
    since = int(time.time() - hours * 3600)
    rows = db.rows("""select mint, count(distinct wallet) n, min(ts) first_ts from swaps
        where side='BUY' and is_me=0 and ts>=? group by mint having n>=? order by n desc, first_ts desc limit 20""",
                   (since, min_whales))
    out = []
    for r in rows:
        holders = whales.holders_of(r["mint"])
        info = market.cached(r["mint"])
        first_mc = min((h["entry_mc"] for h in holders if h["entry_mc"] > 0), default=0.0)
        now_mc = num(info.get("mc_usd"))
        out.append({"mint": r["mint"], "symbol": info.get("symbol") or r["mint"][:4], "whales": r["n"],
                    "holding": sum(h["still_in"] for h in holders), "first_mc": first_mc, "now_mc": now_mc,
                    "x": now_mc / first_mc if first_mc > 0 and now_mc > 0 else 0.0, "first_ts": r["first_ts"],
                    "liquidity": num(info.get("liquidity_usd"), -1), "image": info.get("image") or "",
                    "names": [h["name"] for h in holders][:6]})
    return out


def coin_report(db, market, whales, portfolio, mint: str) -> dict:
    info = market.cached(mint)
    holders = whales.holders_of(mint)
    now_mc = num(info.get("mc_usd"))
    for h in holders:
        h["x"] = now_mc / h["entry_mc"] if h["entry_mc"] > 0 and now_mc > 0 else 0.0
    trades = db.rows("""select s.*, coalesce(w.name,'') name from swaps s left join whales w on w.address=s.wallet
        where s.mint=? order by s.ts desc limit 60""", (mint,))
    for t in trades:
        t["who"] = whales.name(t["wallet"])
    alerts = db.rows("select * from alerts where mint=? order by ts desc limit 20", (mint,))
    copies_ = db.rows("select * from copies where mint=? order by open_ts desc limit 20", (mint,))
    return {"mint": mint, "info": info, "holders": holders, "trades": trades, "alerts": alerts, "copies": copies_,
            "position": portfolio.position(mint)}
