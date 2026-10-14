"""Telemetry: is every part of the bot doing its job, and how fast are whale trades reaching you?

/health in Telegram, the Settings tab on the dashboard, and a snapshot every 10 minutes in the database (it goes into
/export, so a review sees what the bot saw). Nothing here contains keys: RPC nodes are shown by provider name only.
"""
from __future__ import annotations

import resource
import sys
import time

from .util import esc


def _pct(values: list[int], p: float) -> int | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(p * len(values)))]


def latency(db, hours: float = 24) -> dict:
    """How long whale trades took to reach the bot (seen time − block time), by how they arrived."""
    since = int(time.time() - hours * 3600)
    by: dict[str, list[int]] = {}
    asleep = 0
    for r in db.rows("""select source, ts, seen_ts - ts d,
            (select count(*) from paper_equity e where e.ts between s.ts - 400 and s.ts + 400) up
            from swaps s where is_me=0 and ts>=? and seen_ts>0""", (since,)):
        by.setdefault(str(r["source"]), []).append(max(0, int(r["d"] or 0)))
        asleep += int(r["d"] or 0) > 600 and not r["up"]   # it happened while the bot wasn't running at all
    total = sum(len(v) for v in by.values())
    out = {"n": total, "stream_share": len(by.get("stream", [])) / total if total else None, "sources": {}}
    for source, ds in by.items():
        out["sources"][source] = {"n": len(ds), "p50": _pct(ds, 0.5), "p90": _pct(ds, 0.9),
                                  "over_10min": sum(d > 600 for d in ds)}
    out["asleep"] = asleep
    out["late_alerts"] = int(db.scalar("select count(*) from alerts where kind='BUY' and status='late' and ts>=?",
                                       (since,), default=0))
    return out


def memory_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)


def report(app) -> dict:
    now = time.time()
    stream, engine = getattr(app, "stream", None), app.engine
    live = getattr(app, "live", None)
    jup = getattr(getattr(app, "market", None), "jup", None)
    tracker = getattr(app, "tracker", None)
    return {
        "ts": int(now), "uptime_s": int(now - getattr(app, "started", now)), "memory_mb": memory_mb(),
        "stream": {"connected": bool(getattr(stream, "connected", False)), "subscribed": getattr(stream, "subscribed", 0),
                   "wanted": len(getattr(stream, "wanted", ()) or ()), "notifications": getattr(stream, "notifications", 0),
                   "reconnects": getattr(stream, "reconnects", 0), "failed_subs": getattr(stream, "failed_subs", 0),
                   "resubscribed": getattr(stream, "resubscribed", 0),
                   "providers": stream.detail() if hasattr(stream, "detail") else []} if stream else {},
        "ingest": {"fast_buys": getattr(engine, "fast_buys", 0), "queue": engine.queue.qsize(),
                   "poll_every_s": tracker.poll_every() if tracker and hasattr(tracker, "poll_every") else None,
                   "latency_24h": latency(app.db)},
        "rpc": app.rpc.health() if getattr(app, "rpc", None) else {},
        "live": ({"halt": live.halt_reason(), "exec": live.exec_stats(), "enabled": app.cfg.flag("LIVE_TRADING")}
                 if live else {}),
        "jupiter": {"calls": getattr(jup, "calls", 0), "last_error": getattr(jup, "last_error", "")} if jup else {},
        "loops": {"crashes": dict(getattr(app, "crashes", {})), "last_crash": getattr(app, "last_crash", "")},
    }


def _secs(s) -> str:
    if s is None:
        return "—"
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h"


def format_health(r: dict) -> str:
    lines = [f"<b>🩺 Health</b> · up {_secs(r['uptime_s'])} · memory {r['memory_mb']:.0f} MB"]
    st = r.get("stream") or {}
    if st:
        lines.append(f"Live stream: {'🟢 connected' if st['connected'] else '🔴 reconnecting'} · {st['subscribed']}/"
                     f"{st['wanted']} wallets subscribed · {st['notifications']} trades pushed · {st['reconnects']} "
                     f"reconnects · {st['resubscribed']} resubscribed after missing trades")
        for p in st.get("providers") or []:
            if len(st["providers"]) > 1:
                lines.append(f"  · {esc(p['name'])}: {'🟢' if p['connected'] else '🔴'} {p['subscribed']}/{p['wanted']} "
                             f"subscribed · {p['notifications']} trades ({p['first']} first) · {p['reconnects']} reconnects"
                             f" · {p['failed_subs']} failed subscriptions")
    ing = r.get("ingest") or {}
    lat = ing.get("latency_24h") or {}
    if lat.get("n"):
        parts = [f"{src}: {v['n']} (typical {_secs(v['p50'])}, slow 10% {_secs(v['p90'])})"
                 for src, v in sorted(lat["sources"].items(), key=lambda kv: -kv[1]["n"])]
        lines.append(f"Whale trades, 24h: {lat['n']} · {lat['stream_share'] * 100:.0f}% straight from the stream · "
                     + " · ".join(parts))
        if lat.get("asleep"):
            lines.append(f"💤 {lat['asleep']} whale trades happened while the bot wasn't running (computer asleep / "
                         "offline) — only a server that's always on catches those in time (SERVER_SETUP.txt)")
        if lat.get("late_alerts"):
            lines.append(f"⚠️ {lat['late_alerts']} whale buys reached the bot over 10 minutes late (asleep / RPC down?)")
    lines.append(f"Pump.fun buys read from the stream's logs (no RPC call): {ing.get('fast_buys', 0)} · backup check "
                 f"every {ing.get('poll_every_s') or '—'}s · queue {ing.get('queue', 0)}")
    rpc = r.get("rpc") or {}
    for node in rpc.get("nodes") or []:
        state = f"resting {_secs(node['resting_s'])}" if node["resting_s"] else "ok"
        line = (f"RPC {esc(node['name'])}: {node.get('ok', 0):,} answered of {node['calls']:,} · {node['avg_ms'] or '—'} ms · "
                f"{state}" + (f" · up to {node['per_second']}/s" if node.get("per_second") else ""))
        if node.get("not_on_plan"):
            line += " · not on its plan (sent elsewhere): " + ", ".join(node["not_on_plan"])
        if node.get("last") and node["errors"]:
            line += f" · last problem: {esc(node['last'])}"
        lines.append(line)
    lv = r.get("live") or {}
    if lv.get("enabled"):
        ex = lv.get("exec") or {}
        lines.append(f"Live: {'⏸ halted — ' + esc(lv['halt']) if lv.get('halt') else '🟢 ready'} · {ex.get('sent', 0)} sent · "
                     f"{ex.get('failed', 0)} failed · {ex.get('skipped', 0)} skipped by the checks")
    loops = r.get("loops") or {}
    if loops.get("crashes"):
        lines.append("Restarted after a crash: " + ", ".join(f"{k} ×{v}" for k, v in loops["crashes"].items())
                     + (f" (last: {esc(loops['last_crash'])})" if loops.get("last_crash") else ""))
    else:
        lines.append("No part of the bot has crashed since it started.")
    return "\n".join(lines)
