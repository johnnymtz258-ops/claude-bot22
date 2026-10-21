"""The dashboard: http://localhost:8788 (only reachable from this computer)."""
from __future__ import annotations

import time

from aiohttp import web

from . import VERSION
from .clock import clock_text, day_of
from .coach import leaks
from .config import SETTINGS
from .setups import label


def state(app, now: float | None = None) -> dict:
    now = now or time.time()
    day = day_of(now)
    st = app.ledger.day_state(day)
    account = app.account_size()
    trades = app.db.rows("select * from trades where day=? or (status='closed' and closed_ts>=?) order by opened_ts",
                         (day, int(now) - 86400))
    trades = [t for t in trades if t["day"] == day or day_of(t["closed_ts"] or 0) == day]
    open_rows = []
    for t in app.db.rows("select * from trades where status='open' order by opened_ts"):
        px = app.data.last_price(t["underlying"])
        open_rows.append({**t, "und_now": px})
    alerts = []
    for a in app.db.rows("select * from alerts where day=? order by ts desc limit 40", (day,)):
        p = app.db.row("select status, r_mult from paper where alert_id=?", (a["id"],)) or {}
        alerts.append({**a, "label": label(a["setup"], a["direction"]), "time": clock_text(a["ts"]),
                       "paper": p.get("status"), "paper_r": p.get("r_mult")})
    watch = []
    for s in app.scanner.watchlist(day):
        lv = app.data.levels(s, now) if app.data.bars1(s) else {}
        px = app.data.last_price(s)
        watch.append({"symbol": s, "price": px, "chg": (px / lv["pdc"] - 1) * 100 if px and lv.get("pdc") else None,
                      "vwap": lv.get("vwap"), "rvol": lv.get("rvol"), "pmh": lv.get("pmh"), "pdh": lv.get("pdh")})
    lk = leaks(app.db, 30, now)
    table = app.edge.table()
    return {
        "version": VERSION, "now": int(now), "session": app.market.session(now), "account": account,
        "accounts": app.db.rows("select * from accounts order by total desc"),
        "rules": {k: app.cfg.get(k) for k in ("RISK_PER_TRADE_PCT", "DAILY_MAX_LOSS_PCT", "MAX_TRADES_PER_DAY")},
        "today": {**{k: v for k, v in st.items() if k != "last_loss"}, "trades": trades},
        "open": open_rows, "alerts": alerts, "gappers": app.scanner.gappers(day)[:12], "watch": watch,
        "setups": [{"label": r["label"], "status": r["status"], "bt": r["backtest"], "paper": r["paper"]} for r in table],
        "paper": app.paper.stats(), "journal": lk,
        "feed": list(app.notify.feed)[:60] or app.db.rows("select ts, kind, symbol, text from events order by id desc limit 60"),
        "status": app.status_text(now), "backtest": app.backtest_progress,
        "settings": [{"name": n, "value": app.cfg.get(n), "help": s.help, "bool": s.is_bool} for n, s in SETTINGS.items()],
        "watchlist": list(app.cfg.watchlist),
    }


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DayBot</title><style>
:root{--bg:#0f1419;--card:#161d25;--line:#243041;--fg:#d6dde6;--dim:#8a99a8;--up:#26a69a;--down:#ef5350;--warn:#ffb74d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;gap:16px;align-items:baseline;padding:14px 18px;border-bottom:1px solid var(--line);flex-wrap:wrap}
header h1{font-size:18px;margin:0}header .dim{color:var(--dim)}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;padding:14px 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;min-width:0}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin:0 0 8px}
.wide{grid-column:1/-1}table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--dim);font-weight:500}.up{color:var(--up)}.down{color:var(--down)}.warn{color:var(--warn)}.dim{color:var(--dim)}
.big{font-size:26px;font-weight:600}.row{display:flex;gap:18px;flex-wrap:wrap}.scroll{overflow-x:auto}
input,button,select{background:#0b1016;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:7px 9px;font:inherit}
button{cursor:pointer}button:hover{border-color:#3b4b60}.feed div{padding:5px 0;border-bottom:1px solid var(--line);white-space:pre-wrap}
#chart img{width:100%;border-radius:8px}#checkout{white-space:pre-wrap;margin-top:8px}a.sym{color:#64b5f6;cursor:pointer;text-decoration:none}
</style></head><body>
<header><h1>📈 DayBot</h1><span class="dim" id="hdr"></span></header>
<main>
<section class="card"><h2>Today</h2><div class="row"><div><div class="dim">Closed P&amp;L</div><div class="big" id="pnl">—</div></div>
<div><div class="dim">Trades</div><div class="big" id="ntr">—</div></div><div><div class="dim">Loss streak</div><div class="big" id="streak">—</div></div></div>
<div class="dim" id="rules" style="margin-top:8px"></div></section>
<section class="card"><h2>Check a trade</h2><form id="cf"><input id="ct" placeholder="NVDA · NVDA 140c 10/24 · TSLA short 50 @250" style="width:72%">
<button>Check</button></form><div id="checkout"></div></section>
<section class="card"><h2>Open trades vs plan</h2><div class="scroll"><table id="open"></table></div></section>
<section class="card"><h2>Setup alerts today</h2><div class="scroll"><table id="alerts"></table></div></section>
<section class="card"><h2>Watchlist</h2><div class="scroll"><table id="watch"></table></div></section>
<section class="card"><h2>Chart</h2><div id="chart" class="dim">Click a symbol.</div></section>
<section class="card"><h2>Gappers</h2><div class="scroll"><table id="gappers"></table></div></section>
<section class="card"><h2>Setups — what they've earned</h2><div class="scroll"><table id="setups"></table></div><div class="dim" id="paper"></div>
<button id="bt" style="margin-top:8px">Run backtest</button></section>
<section class="card"><h2>Journal — last 30 days</h2><div id="journal"></div></section>
<section class="card"><h2>Today's trades</h2><div class="scroll"><table id="trades"></table></div></section>
<section class="card wide"><h2>Feed</h2><div class="feed" id="feed"></div></section>
<section class="card wide"><h2>Status</h2><div id="status" style="white-space:pre-wrap"></div></section>
<section class="card wide"><h2>Settings</h2><div class="scroll"><table id="settings"></table></div></section>
</main>
<script>
const $=id=>document.getElementById(id);
const f2=v=>v==null||v===''?'—':(+v).toFixed(2);
const money=v=>v==null?'—':((v<0?'-':(v>0?'+':''))+'$'+Math.abs(v).toLocaleString(undefined,{maximumFractionDigits:2}));
const cls=v=>v>0?'up':(v<0?'down':'');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const sym=s=>`<a class="sym" data-s="${esc(s)}">${esc(s)}</a>`;
const tm=ts=>new Date(ts*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
function rows(id,head,list,fn){$(id).innerHTML='<tr>'+head.map(h=>`<th>${h}</th>`).join('')+'</tr>'+(list.length?list.map(fn).join(''):`<tr><td class="dim" colspan="${head.length}">nothing yet</td></tr>`)}
async function load(){const r=await fetch('/api/state');const s=await r.json();
$('hdr').textContent=`v${s.version} · market ${s.session} · account ${s.account?money(s.account).replace('+',''):'not synced'}`;
const t=s.today;$('pnl').textContent=money(t.realized);$('pnl').className='big '+cls(t.realized);$('ntr').textContent=t.trades;$('streak').textContent=t.streak;
$('rules').textContent=`Rules: risk ${s.rules.RISK_PER_TRADE_PCT}% a trade · stop the day at -${s.rules.DAILY_MAX_LOSS_PCT}% · max ${s.rules.MAX_TRADES_PER_DAY} trades`;
rows('open',['What','Side','Qty','Entry','Now','Stop','T1','T2'],s.open,o=>`<tr><td>${sym(o.underlying)} ${o.kind=='option'?esc(o.symbol):''}</td><td>${o.direction}</td><td>${o.open_qty}</td><td>${f2(o.entry_avg)}</td><td>${f2(o.und_now)}</td><td class="down">${f2(o.plan_stop)}</td><td class="up">${f2(o.plan_t1)}</td><td class="up">${f2(o.plan_t2)}</td></tr>`);
rows('alerts',['Time','','Symbol','Setup','Entry','Stop','Paper'],s.alerts,a=>`<tr><td>${a.time}</td><td>${a.proven?'✅':'🧪'}</td><td>${sym(a.symbol)}</td><td>${esc(a.label)}</td><td>${f2(a.entry)}</td><td>${f2(a.stop)}</td><td class="${cls(a.paper_r)}">${a.paper=='closed'?(+a.paper_r).toFixed(2)+'R':(a.paper||'')}</td></tr>`);
rows('watch',['Symbol','Price','Chg','VWAP','Vol×','PM high','Y high'],s.watch,w=>`<tr><td>${sym(w.symbol)}</td><td>${f2(w.price)}</td><td class="${cls(w.chg)}">${w.chg==null?'—':w.chg.toFixed(1)+'%'}</td><td>${f2(w.vwap)}</td><td>${w.rvol?w.rvol.toFixed(1):'—'}</td><td>${f2(w.pmh)}</td><td>${f2(w.pdh)}</td></tr>`);
rows('gappers',['Symbol','Gap','Price','PM vol','Catalyst'],s.gappers,g=>`<tr><td>${sym(g.symbol)}</td><td class="${cls(g.gap)}">${g.gap.toFixed(1)}%</td><td>${f2(g.price)}</td><td>${Math.round(g.pm_vol/1000)}K</td><td class="dim">${esc((g.catalyst||{}).headline||'').slice(0,60)}</td></tr>`);
const st={proven:'✅ proven',promising:'🟡 promising',thin:'⚪ thin',negative:'🔴 loses',benched:'⏸ benched',untested:'⚪ untested'};
rows('setups',['Setup','Status','Backtest','Avg R','Paper'],s.setups,x=>`<tr><td>${esc(x.label)}</td><td>${st[x.status]||x.status}</td><td>${x.bt?x.bt.n:'—'}</td><td class="${cls(x.bt&&x.bt.avg_r)}">${x.bt?(+x.bt.avg_r).toFixed(2):'—'}</td><td>${x.paper.n?x.paper.n+' · '+x.paper.avg_r.toFixed(2)+'R':'—'}</td></tr>`);
const p=s.paper;$('paper').textContent=`Paper: ${p.n} trades · ${p.r.toFixed(1)}R · balance $${Math.round(p.balance).toLocaleString()} · ${p.open} open`+(s.backtest?` · backtest ${s.backtest}`:'');
const j=s.journal,jt=j.total;let jh=jt.n?`<div>${jt.n} trades · <b class="${cls(jt.pnl)}">${money(jt.pnl)}</b> · won ${(jt.won*100).toFixed(0)}% · PF ${jt.pf.toFixed(2)}</div>`:'<div class="dim">No closed trades yet.</div>';
if(j.leaks.length)jh+='<div style="margin-top:6px"><b>Leaks</b></div>'+j.leaks.map(g=>`<div class="down">🔻 ${esc(g.name)} — ${g.n} trades, ${money(g.pnl)}</div>`).join('');
if(j.edges.length)jh+='<div style="margin-top:6px"><b>What works</b></div>'+j.edges.map(g=>`<div class="up">🔹 ${esc(g.name)} — ${g.n} trades, ${money(g.pnl)}</div>`).join('');
$('journal').innerHTML=jh;
rows('trades',['Opened','What','Side','P&L','R'],t.trades,x=>`<tr><td>${tm(x.opened_ts)}</td><td>${sym(x.underlying)} ${x.kind=='option'?esc(x.symbol):''}</td><td>${x.direction}</td><td class="${cls(x.pnl)}">${x.status=='closed'?money(x.pnl):'open'}</td><td>${x.r_mult==null?'':(+x.r_mult).toFixed(2)}</td></tr>`);
$('feed').innerHTML=s.feed.map(e=>`<div><span class="dim">${tm(e.ts)} ${esc(e.kind)}</span> ${esc(e.text)}</div>`).join('')||'<div class="dim">quiet</div>';
$('status').textContent=s.status.replace(/<[^>]+>/g,'');
rows('settings',['Setting','Value','What it does'],s.settings,x=>`<tr><td><code>${x.name}</code></td><td><input data-n="${x.name}" value="${x.value}" size="8"></td><td class="dim" style="white-space:normal">${esc(x.help)}</td></tr>`);}
document.addEventListener('click',e=>{const a=e.target.closest('a.sym');if(a){$('chart').innerHTML=`<img src="/chart/${encodeURIComponent(a.dataset.s)}.png?t=${Date.now()}" alt="chart">`;}});
document.addEventListener('change',async e=>{const n=e.target.dataset.n;if(!n)return;const r=await fetch('/api/set',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name:n,value:e.target.value})});const j=await r.json();e.target.style.borderColor=j.ok?'#26a69a':'#ef5350';});
$('cf').addEventListener('submit',async e=>{e.preventDefault();$('checkout').textContent='checking…';const r=await fetch('/api/check',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text:$('ct').value})});const j=await r.json();$('checkout').innerHTML=j.html;});
$('bt').addEventListener('click',async()=>{await fetch('/api/backtest',{method:'POST'});load();});
load();setInterval(load,15000);
</script></body></html>"""


def _safe_html(text: str) -> str:
    """Telegram-style HTML from the bot's own messages (already escaped) -> newline-preserving page HTML."""
    return text.replace("\n", "<br>")


def make_app(app) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_req):
        return web.Response(text=PAGE, content_type="text/html")

    @routes.get("/api/state")
    async def api_state(_req):
        return web.json_response(state(app), dumps=lambda o: __import__("json").dumps(o, default=str))

    @routes.get("/chart/{sym}.png")
    async def chart(req):
        sym = req.match_info["sym"].upper()[:6]
        if not sym.isalpha() or not app.charts:
            raise web.HTTPNotFound()
        now = time.time()
        if app.data.ok and not app.data.bars1(sym):
            await app.data.load_daily([sym], now)
            await app.data.refresh([sym], now)
        png = app.commands._render(sym, now)
        if not png:
            raise web.HTTPNotFound()
        return web.Response(body=png, content_type="image/png")

    @routes.post("/api/check")
    async def api_check(req):
        body = await req.json()
        replies = await app.commands.text(str(body.get("text", ""))[:80])
        html_text = "<br><br>".join(_safe_html(r["text"]) for r in replies if r.get("text"))
        return web.json_response({"html": html_text})

    @routes.post("/api/set")
    async def api_set(req):
        body = await req.json()
        try:
            v = app.cfg.set(str(body.get("name")), body.get("value"))
            app.db.set_meta(f"set:{str(body.get('name')).upper()}", v)
            return web.json_response({"ok": True, "value": v})
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)})

    @routes.post("/api/backtest")
    async def api_backtest(_req):
        return web.json_response({"started": app.start_backtest()})

    wa = web.Application()
    wa.add_routes(routes)
    return wa


async def serve(app, port: int) -> None:
    runner = web.AppRunner(make_app(app))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    try:
        await site.start()
    except OSError as exc:
        print(f"Dashboard couldn't start on port {port}: {exc}")
        return
    import asyncio
    while True:
        await asyncio.sleep(3600)

