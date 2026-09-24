"""Entry checklist (PASS/WARN/DROP rows) and a local live web dashboard.

The checklist mirrors the "liquidity depth / holder spread / volume vs mcap / rug signature /
fresh wallets / entry window" panel popularised by screener bots, using only data the bot
already collects (no Helius key needed). The dashboard is a single page served on
http://localhost:8787 by the bot itself; it reads the shared database and never trades.
"""
from __future__ import annotations

import json
import re
import time
from collections import deque

from aiohttp import web

RECENT_CHECKS: deque = deque(maxlen=200)
SCAN_STATS: dict = {"discovered": 0, "candidates": 0, "ts": 0, "scans": 0}


def _f(v, d=0.0):
    try:
        x = float(v)
        return x if x == x else d
    except (TypeError, ValueError):
        return d


def entry_checklist(pair, safety, risks, *, min_liq=50_000, min_turnover_pct=0.075, holder_warn=50.0,
                    entry_min_5m=-2.0, entry_max_5m=8.0, chase_ok=True):
    """Six PASS/WARN/DROP rows for a candidate. Returns [(name, status, detail)]."""
    liq = _f((pair.get("liquidity") or {}).get("usd"))
    mc = _f(pair.get("marketCap") or pair.get("fdv"))
    v5 = _f((pair.get("volume") or {}).get("m5"))
    pc5 = _f((pair.get("priceChange") or {}).get("m5"))
    safety = safety or {}
    text = " | ".join(str(r) for r in (risks or [])).lower()
    rows = []

    liq_mc = liq / mc if mc > 0 else 0
    rows.append(("Liquidity depth", "PASS" if liq >= min_liq and liq_mc >= 0.05 else ("WARN" if liq >= min_liq * 0.6 else "DROP"),
                 f"${liq:,.0f} ({liq_mc*100:.0f}% of mcap)"))

    m = re.search(r"top-10[^0-9~]*~?\s*([0-9]+(?:\.[0-9]+)?)%", text)
    if m:
        top10 = float(m.group(1))
        rows.append(("Holder spread", "PASS" if top10 < holder_warn else ("WARN" if top10 < 90 else "DROP"), f"top-10 hold ~{top10:.0f}%"))
    elif safety.get("holder_checked"):
        rows.append(("Holder spread", "PASS", "holder check passed"))
    else:
        rows.append(("Holder spread", "WARN", "not verified yet"))

    turnover = 100.0 * v5 / mc if mc > 0 else 0
    rows.append(("Volume vs mcap", "PASS" if turnover >= min_turnover_pct else "WARN", f"{turnover:.2f}% traded in 5m"))

    if safety.get("hard"):
        rows.append(("Rug signature", "DROP", "rug-critical flag"))
    elif "wide mode" in text or "rugcheck" in text and ("danger" in text or "hard-risk" in text):
        rows.append(("Rug signature", "WARN", "RugCheck flags shown as warnings"))
    elif safety.get("rug_checked"):
        rows.append(("Rug signature", "PASS", "RugCheck clean"))
    else:
        rows.append(("Rug signature", "WARN", "RugCheck pending"))

    fresh = [k for k in ("bundle", "insider", "correlation", "tagged-cohort", "fresh wallet", "creator balance") if k in text]
    rows.append(("Fresh/bundled wallets", "WARN" if fresh else ("PASS" if safety.get("rug_checked") else "WARN"),
                 ("flags: " + ", ".join(fresh)) if fresh else ("no bundle/insider flags" if safety.get("rug_checked") else "unverified")))

    in_window = entry_min_5m <= pc5 <= entry_max_5m
    rows.append(("Entry window", "PASS" if chase_ok and in_window else "DROP", f"5m {pc5:+.1f}%" + ("" if chase_ok else " — ran past alert band")))
    return rows


def checklist_text(rows):
    icon = {"PASS": "✅", "WARN": "⚠️", "DROP": "⛔"}
    passed = sum(1 for _, s, _ in rows if s == "PASS")
    lines = [f"CHECKLIST {passed}/{len(rows)} PASS"]
    lines += [f"{icon.get(s, '•')} {name}: {s} — {detail}" for name, s, detail in rows]
    return "\n".join(lines)


def record_check(pair, rows, outcome):
    base = pair.get("baseToken") or {}
    RECENT_CHECKS.appendleft({
        "ts": int(time.time()), "symbol": base.get("symbol") or "?", "token": base.get("address") or "",
        "mcap": _f(pair.get("marketCap") or pair.get("fdv")), "price": _f(pair.get("priceUsd")),
        "rows": [{"name": n, "status": s, "detail": d} for n, s, d in rows], "outcome": outcome})


def record_scan(discovered, candidates):
    SCAN_STATS.update(discovered=int(discovered), candidates=int(candidates), ts=int(time.time()),
                      scans=SCAN_STATS.get("scans", 0) + 1)


# ------------------------------------------------------------------ state for the page

def dashboard_state(db, profile="wide", fee_keep=1.0):
    now = int(time.time())
    positions = []
    for r in db.conn.execute("select * from positions where active=1 order by open_ts desc").fetchall():
        p = dict(r)
        last = db.conn.execute("select price from observations where token=? order by ts desc limit 1", (p["token"],)).fetchone()
        price = _f(last[0]) if last else _f(p.get("peak_price"))
        rem = _f(p.get("remaining_fraction"), 1.0)
        value = _f(p.get("quantity")) * rem * price
        cost = _f(p.get("amount_usd")) * rem
        positions.append({"symbol": p.get("symbol"), "token": p.get("token"), "cost": cost,
                          "value_after_fees": value * fee_keep, "pnl": value * fee_keep - cost,
                          "age_min": int((now - int(p.get("open_ts") or now)) / 60)})
    closed = db.conn.execute("select close_ts, realized_pnl from positions where active=0 and close_ts>0 order by close_ts").fetchall()
    curve, total = [], 0.0
    for ts, pnl in closed:
        total += _f(pnl)
        curve.append([int(ts), round(total, 2)])
    wins = sum(1 for _, pnl in closed if _f(pnl) > 0)
    tr = []
    for r in db.conn.execute("""select tier,count(*),sum(realized_pnl>0),avg(realized_pnl*100.0/nullif(amount_usd,0)),sum(realized_pnl)
            from paper_positions where active=0 and close_ts>=? and coalesce(close_reason,'') not like 'price data unreliable%'
            group by tier order by count(*) desc""", (now - 14 * 86400,)).fetchall():
        tr.append({"tier": r[0], "n": r[1], "win_rate": (r[2] or 0) / max(r[1], 1), "avg_roi": _f(r[3]), "net": _f(r[4])})
    alerts_today = db.conn.execute("select count(*) from signals where kind='EARLY' and ts>=?", (now - 86400,)).fetchone()[0]
    return {"now": now, "profile": profile, "scan": SCAN_STATS, "alerts_24h": alerts_today,
            "checks": list(RECENT_CHECKS)[:60], "positions": positions,
            "journal": {"closed": len(closed), "wins": wins, "net": round(total, 2), "curve": curve[-500:]},
            "track": tr}


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FomoBot Live</title><style>
:root{--bg:#0b0d12;--panel:#141821;--line:#232a36;--text:#e6e9ef;--mute:#8a93a3;--pass:#3fbf7f;--warn:#e0a53a;--drop:#e25c5c;--accent:#e86bd0}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:16px;align-items:baseline;padding:14px 18px;border-bottom:2px solid var(--accent)}
h1{margin:0;font-size:20px;letter-spacing:.5px}.pill{color:var(--mute)}.pill b{color:var(--text)}
main{display:grid;grid-template-columns:1.2fr 1fr;gap:14px;padding:14px}@media(max-width:900px){main{grid-template-columns:1fr}}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}
h2{margin:0 0 8px;font-size:13px;letter-spacing:1px;color:var(--mute);text-transform:uppercase}
table{width:100%;border-collapse:collapse}td,th{padding:5px 6px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--mute);font-weight:500;font-size:12px}.PASS{color:var(--pass)}.WARN{color:var(--warn)}.DROP{color:var(--drop)}
.dots span{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:3px}.dots .PASS{background:var(--pass)}.dots .WARN{background:var(--warn)}.dots .DROP{background:var(--drop)}
.big{font-size:28px;font-weight:600}.pos{color:var(--pass)}.neg{color:var(--drop)}svg{width:100%;height:180px}
.scroll{max-height:420px;overflow:auto}small{color:var(--mute)}
</style></head><body>
<header><h1>FOMOBOT · LIVE</h1><span class="pill">profile <b id="profile">-</b></span><span class="pill">scanned <b id="scanned">-</b></span>
<span class="pill">candidates <b id="cands">-</b></span><span class="pill">alerts 24h <b id="alerts">-</b></span><span class="pill" id="updated"></span></header>
<main>
<section><h2>Checklist feed</h2><div class="scroll"><table><thead><tr><th>Time</th><th>Coin</th><th>MCap</th><th>Checks</th><th>Result</th></tr></thead><tbody id="checks"></tbody></table></div>
<small>Rows: liquidity depth · holder spread · volume vs mcap · rug signature · fresh/bundled wallets · entry window</small></section>
<section><h2>Journal balance (your recorded trades, after fees)</h2><div class="big" id="net">-</div><small id="record"></small><svg id="curve" viewBox="0 0 600 180" preserveAspectRatio="none"></svg></section>
<section><h2>Open positions</h2><table><thead><tr><th>Coin</th><th>Cost</th><th>Value after fees</th><th>P/L</th><th>Age</th></tr></thead><tbody id="positions"></tbody></table></section>
<section><h2>Track record by alert type (paper, 14d)</h2><table><thead><tr><th>Type</th><th>Tests</th><th>Wins</th><th>Avg/trade</th><th>Net</th></tr></thead><tbody id="track"></tbody></table></section>
</main><script>
const $=id=>document.getElementById(id);const usd=v=>(v<0?'-$':'$')+Math.abs(v).toFixed(2);
const mc=v=>v>=1e6?'$'+(v/1e6).toFixed(1)+'M':'$'+(v/1e3).toFixed(0)+'K';
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function tick(){try{const s=await (await fetch('/api/state')).json();
$('profile').textContent=s.profile.toUpperCase();$('scanned').textContent=s.scan.discovered;$('cands').textContent=s.scan.candidates;$('alerts').textContent=s.alerts_24h;
$('updated').textContent='updated '+new Date(s.now*1000).toLocaleTimeString();
$('checks').innerHTML=s.checks.map(c=>`<tr title="${esc(c.rows.map(r=>r.name+': '+r.status+' — '+r.detail).join('\n'))}"><td>${new Date(c.ts*1000).toLocaleTimeString()}</td><td>${esc(c.symbol)}</td><td>${mc(c.mcap)}</td><td class="dots">${c.rows.map(r=>`<span class="${r.status}"></span>`).join('')}</td><td class="${c.outcome.startsWith('ALERT')?'PASS':'DROP'}">${esc(c.outcome)}</td></tr>`).join('')||'<tr><td colspan=5><small>waiting for candidates…</small></td></tr>';
const j=s.journal;$('net').textContent=usd(j.net);$('net').className='big '+(j.net>=0?'pos':'neg');$('record').textContent=`${j.closed} closed trades · ${j.wins} wins`;
const pts=j.curve;let svg='';if(pts.length>1){const ys=pts.map(p=>p[1]),lo=Math.min(0,...ys),hi=Math.max(0,...ys),span=(hi-lo)||1;
const xy=pts.map((p,i)=>[i/(pts.length-1)*600,170-(p[1]-lo)/span*160]);const z=170-(0-lo)/span*160;
svg=`<line x1="0" x2="600" y1="${z}" y2="${z}" stroke="#2a3140" stroke-dasharray="4 4"/><polyline fill="none" stroke="${j.net>=0?'#3fbf7f':'#e25c5c'}" stroke-width="2" points="${xy.map(p=>p.join(',')).join(' ')}"/>`}
$('curve').innerHTML=svg;
$('positions').innerHTML=s.positions.map(p=>`<tr><td>${esc(p.symbol)}</td><td>${usd(p.cost)}</td><td>${usd(p.value_after_fees)}</td><td class="${p.pnl>=0?'PASS':'DROP'}">${usd(p.pnl)}</td><td>${p.age_min}m</td></tr>`).join('')||'<tr><td colspan=5><small>no open positions</small></td></tr>';
$('track').innerHTML=s.track.map(t=>`<tr><td>${esc(t.tier)}</td><td>${t.n}</td><td>${(t.win_rate*100).toFixed(0)}%</td><td class="${t.avg_roi>=0?'PASS':'DROP'}">${t.avg_roi.toFixed(1)}%</td><td>${usd(t.net)}</td></tr>`).join('')||'<tr><td colspan=5><small>no completed tests yet</small></td></tr>';
}catch(e){$('updated').textContent='bot not reachable'}}
tick();setInterval(tick,5000);
</script></body></html>"""


async def start_dashboard(db, profile_fn, fee_keep=1.0, host="127.0.0.1", port=8787):
    async def index(_req):
        return web.Response(text=PAGE, content_type="text/html")

    async def state(_req):
        return web.Response(text=json.dumps(dashboard_state(db, profile_fn(), fee_keep)), content_type="application/json")

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner
