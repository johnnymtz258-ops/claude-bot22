/* FomoBot dashboard. Plain JS, no build step. All API text is inserted with textContent. */
(() => {
  "use strict";
  const TOKEN = window.FOMO_TOKEN;
  const $ = (id) => document.getElementById(id);
  let activeTab = "live";

  // ---------- helpers -------------------------------------------------------------------
  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }
  const svgEl = (tag, attrs) => {
    const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    return n;
  };
  async function api(path) {
    const r = await fetch(path, { cache: "no-store" });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    return r.json();
  }
  async function post(path, body) {
    const r = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json", "X-Fomo-Token": TOKEN },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || data.message || r.statusText);
    return data;
  }
  const num = (v) => (typeof v === "number" && isFinite(v) ? v : Number(v) || 0);
  function usd(v, signed) {
    v = num(v);
    const a = Math.abs(v);
    const body = a >= 1000 ? a.toLocaleString(undefined, { maximumFractionDigits: 0 })
      : a >= 1 ? a.toFixed(2) : a === 0 ? "0.00" : a.toPrecision(3);
    const sign = signed ? (v > 0 ? "+" : v < 0 ? "−" : "") : v < 0 ? "−" : "";
    return `${sign}$${body}`;
  }
  function mc(v) {
    v = num(v);
    if (v <= 0) return "?";
    for (const [s, u] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) {
      if (v >= s) return `$${(v / s).toFixed(1).replace(/\.0$/, "")}${u}`;
    }
    return `$${v.toFixed(0)}`;
  }
  const pct = (v, d = 0) => `${num(v) > 0 ? "+" : num(v) < 0 ? "−" : ""}${Math.abs(num(v)).toFixed(d)}%`;
  const mult = (v) => (num(v) <= 0 ? "?" : num(v) < 1 ? `${num(v).toFixed(2)}x` : num(v) < 10 ? `${num(v).toFixed(1)}x` : `${num(v).toFixed(0)}x`);
  function dur(s) {
    s = Math.max(0, Math.floor(num(s)));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h`;
    return `${Math.floor(s / 86400)}d`;
  }
  const ago = (ts) => (num(ts) ? `${dur(Date.now() / 1000 - num(ts))} ago` : "—");
  const signClass = (v) => (num(v) > 0 ? "up" : num(v) < 0 ? "down" : "");
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
  const short = (a) => (a && a.length > 10 ? `${a.slice(0, 4)}…${a.slice(-4)}` : a || "");
  const STATUS = { HOT: "🔥 HOT", OK: "✅ OK", NEW: "🆕 NEW", WEAK: "〰️ WEAK", COLD: "🧊 COLD" };

  function coinCell(symbol, image, mint) {
    return el("span", { class: "coin" }, image ? el("img", { src: image, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : null,
      `$${symbol || short(mint)}`);
  }
  function links(mint, pair) {
    return el("span", { class: "links" },
      el("a", { href: `https://dexscreener.com/solana/${pair || mint}`, target: "_blank", rel: "noopener", onclick: (e) => e.stopPropagation() }, "DexS"),
      el("a", { href: `https://gmgn.ai/sol/token/${mint}`, target: "_blank", rel: "noopener", onclick: (e) => e.stopPropagation() }, "GMGN"));
  }
  function table(node, headers, rows, emptyText) {
    node.replaceChildren();
    node.append(el("thead", {}, el("tr", {}, headers.map((h) => el("th", { class: h.num ? "num" : "" }, h.label || h)))));
    const body = el("tbody");
    if (!rows.length) body.append(el("tr", {}, el("td", { class: "empty", colspan: headers.length }, emptyText || "Nothing yet.")));
    rows.forEach((r) => body.append(r));
    node.append(body);
  }
  function tile(label, value, sub, cls, hero) {
    return el("div", { class: `tile${hero ? " hero" : ""}` }, el("div", { class: "label" }, label),
      el("div", { class: `value ${cls || ""}` }, value), sub ? el("div", { class: "sub" }, sub) : null);
  }
  function td(content, cls) { return el("td", { class: cls || "" }, content); }

  // ---------- tooltip -------------------------------------------------------------------
  const tip = $("tooltip");
  function showTip(evt, value, label, color) {
    tip.replaceChildren(el("strong", {}, value), color ? el("span", { class: "key", style: `background:${color}` }) : null, label);
    tip.style.display = "block";
    const x = Math.min(evt.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    const y = Math.min(evt.clientY + 14, window.innerHeight - tip.offsetHeight - 8);
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  }
  const hideTip = () => { tip.style.display = "none"; };
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ---------- charts --------------------------------------------------------------------
  function niceTicks(lo, hi, count = 4) {
    if (lo === hi) { lo -= 1; hi += 1; }
    const raw = (hi - lo) / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
    // always cover the full data range: first tick <= lo, last tick >= hi
    const start = Math.floor(lo / step) * step, end = Math.ceil(hi / step) * step;
    const ticks = [];
    for (let t = start; t <= end + step * 0.001; t += step) ticks.push(+t.toFixed(10));
    return ticks;
  }

  // Single series line (value over time) with a zero baseline, area wash, end label and crosshair.
  // opts.base: reference value drawn as the baseline (0 for profit; the starting balance for the paper balance)
  function lineChart(node, points, fmt, opts = {}) {
    node.replaceChildren();
    const base = opts.base || 0;
    if (points.length < 2) { node.append(el("div", { class: "empty" }, opts.empty || "The curve appears after your first two sells.")); return; }
    const W = Math.max(node.clientWidth, 320), H = 240, L = 56, R = 64, T = 24, B = 26;
    const xs = points.map((p) => p[0]), ys = points.map((p) => p[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const ticks = niceTicks(Math.min(base, ...ys), Math.max(base, ...ys));
    const y0 = ticks[0], y1 = ticks[ticks.length - 1];
    const sx = (x) => L + ((x - x0) / Math.max(1, x1 - x0)) * (W - L - R);
    const sy = (y) => T + (1 - (y - y0) / Math.max(1e-9, y1 - y0)) * (H - T - B);
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.label || "Realized profit over time" });
    ticks.forEach((t) => {
      svg.append(svgEl("line", { x1: L, x2: W - R, y1: sy(t), y2: sy(t), class: t === 0 && !base ? "base-line" : "grid-line" }));
      const lab = svgEl("text", { x: L - 8, y: sy(t) + 4, "text-anchor": "end", class: "axis-text" });
      lab.textContent = fmt(t);
      svg.append(lab);
    });
    const sameDay = x1 - x0 < 2 * 86400;
    [x0, x1].forEach((x, i) => {
      const lab = svgEl("text", { x: sx(x), y: H - 6, "text-anchor": i ? "end" : "start", class: "axis-text" });
      const d = new Date(x * 1000);
      lab.textContent = sameDay ? d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
        : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
      svg.append(lab);
    });
    const color = cssVar("--series-pos");
    const path = points.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join("");
    if (base) svg.append(svgEl("line", { x1: L, x2: W - R, y1: sy(base), y2: sy(base), class: "base-line" }));
    svg.append(svgEl("path", { d: `${path}L${sx(x1)},${sy(base)}L${sx(x0)},${sy(base)}Z`, fill: color, "fill-opacity": 0.1 }));
    svg.append(svgEl("path", { d: path, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    const last = points[points.length - 1];
    svg.append(svgEl("circle", { cx: sx(last[0]), cy: sy(last[1]), r: 4, fill: color, stroke: cssVar("--surface-1"), "stroke-width": 2 }));
    const endLab = svgEl("text", { x: sx(last[0]) + 8, y: sy(last[1]) + 4, class: "label-text" });
    endLab.textContent = fmt(last[1]);
    svg.append(endLab);
    const cross = svgEl("line", { y1: T, y2: H - B, class: "cross", visibility: "hidden" });
    const dot = svgEl("circle", { r: 4, fill: color, stroke: cssVar("--surface-1"), "stroke-width": 2, visibility: "hidden" });
    svg.append(cross, dot);
    const hit = svgEl("rect", { x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent" });
    hit.addEventListener("pointermove", (e) => {
      const box = svg.getBoundingClientRect();
      const px = ((e.clientX - box.left) / box.width) * W;
      let best = points[0];
      for (const p of points) if (Math.abs(sx(p[0]) - px) < Math.abs(sx(best[0]) - px)) best = p;
      cross.setAttribute("x1", sx(best[0])); cross.setAttribute("x2", sx(best[0]));
      dot.setAttribute("cx", sx(best[0])); dot.setAttribute("cy", sy(best[1]));
      cross.setAttribute("visibility", "visible"); dot.setAttribute("visibility", "visible");
      showTip(e, fmt(best[1]), new Date(best[0] * 1000).toLocaleString(), color);
    });
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip(); });
    svg.append(hit);
    node.append(svg);
  }

  // Signed columns from a zero baseline (blue = gain, red = loss), value at the tip.
  function barChart(node, items, fmt, unit = ["copy", "copies"]) {
    node.replaceChildren();
    const shown = items.filter((i) => i.n > 0);
    if (!shown.length) { node.append(el("div", { class: "empty" }, "No copies closed in this period yet.")); return; }
    const W = Math.max(node.clientWidth, 280), H = 240, L = 48, R = 12, T = 30, B = 40;
    const ticks = niceTicks(Math.min(0, ...shown.map((i) => i.value)), Math.max(0, ...shown.map((i) => i.value)));
    const y0 = ticks[0], y1 = ticks[ticks.length - 1];
    const sy = (y) => T + (1 - (y - y0) / Math.max(1e-9, y1 - y0)) * (H - T - B);
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Average copy return by grade" });
    ticks.forEach((t) => {
      svg.append(svgEl("line", { x1: L, x2: W - R, y1: sy(t), y2: sy(t), class: t === 0 ? "base-line" : "grid-line" }));
      const lab = svgEl("text", { x: L - 8, y: sy(t) + 4, "text-anchor": "end", class: "axis-text" });
      lab.textContent = fmt(t);
      svg.append(lab);
    });
    const band = (W - L - R) / items.length, bw = Math.min(24, band * 0.5);
    items.forEach((it, i) => {
      const cx = L + band * (i + 0.5);
      const cat = svgEl("text", { x: cx, y: H - 20, "text-anchor": "middle", class: "label-text" });
      cat.textContent = it.label;
      const cnt = svgEl("text", { x: cx, y: H - 6, "text-anchor": "middle", class: "axis-text" });
      cnt.textContent = plural(it.n, unit[0], unit[1]);
      svg.append(cat, cnt);
      if (!it.n) return;
      const color = it.value >= 0 ? cssVar("--series-pos") : cssVar("--series-neg");
      const top = sy(Math.max(0, it.value)), bottom = sy(Math.min(0, it.value));
      const h = Math.max(1, bottom - top), r = Math.min(4, h / 2);
      // rounded data-end, square at the baseline
      const d = it.value >= 0
        ? `M${cx - bw / 2},${bottom}V${top + r}Q${cx - bw / 2},${top} ${cx - bw / 2 + r},${top}H${cx + bw / 2 - r}Q${cx + bw / 2},${top} ${cx + bw / 2},${top + r}V${bottom}Z`
        : `M${cx - bw / 2},${top}V${bottom - r}Q${cx - bw / 2},${bottom} ${cx - bw / 2 + r},${bottom}H${cx + bw / 2 - r}Q${cx + bw / 2},${bottom} ${cx + bw / 2},${bottom - r}V${top}Z`;
      const bar = svgEl("path", { d, fill: color, tabindex: 0 });
      const val = svgEl("text", { x: cx, y: it.value >= 0 ? top - 6 : bottom + 14, "text-anchor": "middle", class: "label-text" });
      val.textContent = fmt(it.value);
      const hit = svgEl("rect", { x: cx - band / 2, y: T, width: band, height: H - T - B, fill: "transparent" });
      const show = (e) => showTip(e, fmt(it.value), `${it.label}: ${plural(it.n, unit[0], unit[1])}, ${Math.round(it.win * 100)}% won`, color);
      hit.addEventListener("pointermove", show);
      hit.addEventListener("pointerleave", hideTip);
      svg.append(bar, val, hit);
    });
    node.append(svg);
  }

  // Price path (as market cap) with whale and your buys/sells marked on it.
  function priceChart(node, path, events, supply) {
    node.replaceChildren();
    if (path.length < 2) { node.append(el("div", { class: "empty" }, "The price chart appears once the bot has watched this coin for a few minutes.")); return; }
    const W = Math.max(node.clientWidth, 320), H = 240, L = 58, R = 16, T = 18, B = 26;
    const pts = path.map((p) => [p[0], p[1] * supply]);
    const evs = events.filter((e) => e.ts >= pts[0][0] - 60 && e.ts <= pts[pts.length - 1][0] + 60 && e.mc > 0);
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]).concat(evs.map((e) => e.mc));
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const ticks = niceTicks(Math.min(...ys) * 0.95, Math.max(...ys) * 1.05);
    const y0 = Math.max(0, ticks[0]), y1 = ticks[ticks.length - 1];
    const sx = (x) => L + ((Math.min(Math.max(x, x0), x1) - x0) / Math.max(1, x1 - x0)) * (W - L - R);
    const sy = (y) => T + (1 - (y - y0) / Math.max(1e-9, y1 - y0)) * (H - T - B);
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Market cap with whale and your trades" });
    ticks.filter((t) => t >= y0).forEach((t) => {
      svg.append(svgEl("line", { x1: L, x2: W - R, y1: sy(t), y2: sy(t), class: "grid-line" }));
      const lab = svgEl("text", { x: L - 8, y: sy(t) + 4, "text-anchor": "end", class: "axis-text" });
      lab.textContent = t > 0 ? mc(t) : "$0";
      svg.append(lab);
    });
    [x0, x1].forEach((x, i) => {
      const lab = svgEl("text", { x: sx(x), y: H - 6, "text-anchor": i ? "end" : "start", class: "axis-text" });
      lab.textContent = new Date(x * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
      svg.append(lab);
    });
    const line = cssVar("--series-pos");
    const d = pts.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join("");
    svg.append(svgEl("path", { d, fill: "none", stroke: line, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    const cross = svgEl("line", { y1: T, y2: H - B, class: "cross", visibility: "hidden" });
    svg.append(cross);
    const hit = svgEl("rect", { x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent" });
    hit.addEventListener("pointermove", (e) => {
      const box = svg.getBoundingClientRect();
      const px = ((e.clientX - box.left) / box.width) * W;
      let best = pts[0];
      for (const p of pts) if (Math.abs(sx(p[0]) - px) < Math.abs(sx(best[0]) - px)) best = p;
      cross.setAttribute("x1", sx(best[0])); cross.setAttribute("x2", sx(best[0])); cross.setAttribute("visibility", "visible");
      showTip(e, mc(best[1]), new Date(best[0] * 1000).toLocaleString(), line);
    });
    hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
    svg.append(hit);
    for (const ev of evs) {
      const color = cssVar(ev.who === "me" ? "--series-me" : "--series-whale");
      const x = sx(ev.ts), y = sy(ev.mc), up = ev.side === "BUY";
      const tri = up ? `M${x},${y - 7}L${x - 6},${y + 4}L${x + 6},${y + 4}Z` : `M${x},${y + 7}L${x - 6},${y - 4}L${x + 6},${y - 4}Z`;
      const mark = svgEl("path", { d: tri, fill: color, stroke: cssVar("--surface-1"), "stroke-width": 2, tabindex: 0 });
      const target = svgEl("circle", { cx: x, cy: y, r: 12, fill: "transparent" });
      const show = (e) => showTip(e, `${ev.side === "BUY" ? "Buy" : "Sell"} ${usd(ev.usd)} @ ${mc(ev.mc)}`, `${ev.label} · ${new Date(ev.ts * 1000).toLocaleString()}`, color);
      target.addEventListener("pointermove", show);
      target.addEventListener("pointerleave", hideTip);
      svg.append(mark, target);
    }
    node.append(el("div", { class: "legend" },
      el("span", {}, el("span", { class: "ln", style: `background:${line}` }), "Market cap"),
      el("span", {}, el("span", { class: "sw", style: `background:${cssVar("--series-whale")}` }), "Whale trades ▲ buy ▼ sell"),
      el("span", {}, el("span", { class: "sw", style: `background:${cssVar("--series-me")}` }), "Your trades")), svg);
  }

  // ---------- status bar ----------------------------------------------------------------
  async function renderStatus() {
    const o = await api("/api/overview");
    const pills = [];
    const s = o.stream;
    pills.push(el("span", { class: `pill ${s.connected ? "good" : "bad"}` }, el("span", { class: "dot" }),
      s.connected ? `Live · ${s.subs} wallets` : "Reconnecting…"));
    pills.push(el("span", { class: `pill ${o.alerts_on ? "good" : "warn"}` }, el("span", { class: "dot" }), o.alerts_on ? "Alerts on" : "Alerts paused"));
    pills.push(el("span", { class: "pill" }, `${o.whales} whales`));
    pills.push(el("span", { class: "pill" }, `SOL ${usd(o.sol_usd)}`));
    pills.push(el("span", { class: "pill" }, `last trade ${ago(o.last_trade_ts)}`));
    if (!o.helius) pills.push(el("span", { class: "pill warn" }, el("span", { class: "dot" }), "Public RPC (slow)"));
    $("status").replaceChildren(...pills);
    return o;
  }

  // ---------- tabs ----------------------------------------------------------------------
  async function renderLive() {
    const [o, f] = await Promise.all([renderStatus(), api("/api/feed")]);
    const me = o.me, c = o.copies;
    const fn0 = (o.funnel || { counts: {} }).counts;
    const sent = (fn0.sent || 0) + (fn0.silent || 0);
    const blocked = ["flipper", "weak_whale", "chased", "dumping", "micro", "whale_only"].reduce((n, k) => n + (fn0[k] || 0), 0);
    const p = o.paper || {}, lv = o.live;
    void lv; void c;
    $("live-tiles").replaceChildren(
      tile("Your total P/L", usd(me.total, true), `${usd(me.realized, true)} realized · ${usd(me.unrealized, true)} open`, signClass(me.total), true),
      tile("Whale buys (24h)", `${sent} sent`, `${blocked} blocked (flippers, already ran, dumping)`),
      tile("Your coins", String((o.open_positions || 0) + (o.tracked_coins || 0)), `${o.open_positions || 0} held · ${o.tracked_coins || 0} tracked`),
      tile("Paper autopilot", usd(p.equity), `${pct(p.return_pct, 1)} · ${p.closed || 0} closed, ${p.won || 0} won`, signClass((p.equity || 0) - (p.start || 0))));
    renderMyCoins().catch(() => {});
    const rows = f.buys.map((b) => el("tr", { class: "click", onclick: () => openCoin(b.mint) },
      td(ago(b.trade_ts || b.ts)), td(el("span", { class: "grade", title: b.scalp ? "Scalp: take profit into the pump, don't hold" : "" }, b.scalp ? `⚡${b.grade}` : b.grade)), td(b.whale), td(coinCell(b.symbol, b.image, b.mint)),
      td(mc(b.entry_mc), "num"), td(mc(b.now_mc), "num"),
      td(b.change === null ? "—" : pct(b.change), `num ${signClass(b.change)}`),
      td(b.confluence > 1 ? `🐋×${b.confluence}` : ""), td(links(b.mint, ""))));
    table($("feed"), ["When", "Grade", "Whale", "Coin", { label: "Whale entry", num: 1 }, { label: "Now", num: 1 },
      { label: "Since whale", num: 1 }, "Whales", ""], rows, "No whale buys in the last 3 days. Add whales on the Whales tab.");
    const fn = o.funnel || { total: 0, counts: {} };
    $("feed-note").textContent = fn.total
      ? `24h: ${fn.total} whale buys seen → ` + Object.entries(fn.counts).sort((a, b) => b[1] - a[1])
          .map(([k, n]) => `${n} ${(o.funnel_labels || {})[k] || k}`).join(" · ")
      : "click a row for details";
    $("messages").replaceChildren(...(f.messages.length ? f.messages.map((m) => el("li", {},
      el("b", {}, `${new Date(m.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} `), m.text))
      : [el("li", { class: "empty" }, "Nothing sent since the bot started.")]));
  }

  async function renderHot() {
    const rows = await api(`/api/hot?hours=${$("hot-hours").value}`);
    table($("hot"), ["Coin", "Whales", "Still holding", { label: "First whale in", num: 1 }, { label: "Now", num: 1 }, { label: "Move", num: 1 }, "Who", ""],
      rows.map((r) => el("tr", { class: "click", onclick: () => openCoin(r.mint) },
        td(coinCell(r.symbol, r.image, r.mint)), td(String(r.whales)), td(`${r.holding} of ${r.whales}`),
        td(mc(r.first_mc), "num"), td(mc(r.now_mc), "num"), td(mult(r.x), `num ${r.x >= 1 ? "up" : "down"}`),
        td(r.names.join(", "), "wrap"), td(links(r.mint, "")))),
      "No coin has 2+ of your whales in this window.");
  }

  async function whaleAction(address, action) {
    await post(`/api/whales/${address}/${action}`);
    renderWhales();
  }
  // what a whale's coins did hours after they bought; ⚡ marks scalp-only whales (most coins died)
  function afterText(a) {
    if (!a || a.n < 3) return "—";
    const scalp = a.n >= 5 && a.dead >= 0.6 * a.n;
    return `${scalp ? "⚡ " : ""}${a.dead}/${a.n}`;
  }
  const STYLE = { HOLDER: "🟢 Holder", SWING: "🔵 Swing", FLIPPER: "🟠 Flipper", BOT: "🤖 Bot", NEW: "🆕 New" };
  function styleCell(w) {
    const p = w.profile;
    if (!p) return "—";
    if (p.style === "NEW") return el("span", {}, el("b", {}, STYLE.NEW), el("br"), el("span", { class: "muted" }, `${p.trips} trades so far — profiling`));
    const copy = p.copy_n >= 3 ? `${p.copy_avg.toFixed(2)}x avg · ${Math.round(p.copy_win * 100)}% won (${p.copy_n})` : "not enough coins yet";
    return el("span", { title: w.blocked_why || "" }, el("b", {}, STYLE[p.style] || p.style), w.blocked ? " · 🚫 blocked" : "",
      el("br"), el("span", { class: "muted" }, `sells ~${dur(p.median_hold_s)} in · copy ${copy}`));
  }
  async function renderWhales() {
    renderScout().catch(() => {});
    const rows = await api("/api/whales");
    rows.sort((a, b) => (a.blocked - b.blocked) || (((b.profile || {}).copy_avg || 0) - ((a.profile || {}).copy_avg || 0)));
    table($("whales"), ["Whale", "Style & copy score", "Status", { label: "Copies", num: 1 }, { label: "Won", num: 1 }, { label: "Avg", num: 1 },
      { label: "Median", num: 1 }, { label: "Hit 2x", num: 1 }, { label: "Dip before run", num: 1 },
      { label: "Coins dead 6h later", num: 1 }, "Last trade", ""],
      rows.map((w) => el("tr", { class: "click", onclick: () => openWhale(w.address) },
        td(el("span", {}, el("b", {}, w.name), " ", el("span", { class: "muted mono" }, short(w.address)))),
        td(styleCell(w), "wrap"),
        td(el("span", { class: "status-badge" }, STATUS[w.status], w.muted ? " · 🔕 muted" : w.auto_muted === 1 ? " · auto-muted" : "")),
        td(String(w.n), "num"), td(w.n ? `${Math.round(w.win_rate * 100)}%` : "—", "num"),
        td(w.n ? pct(w.avg) : "—", `num ${signClass(w.avg)}`), td(w.n ? pct(w.median) : "—", `num ${signClass(w.median)}`),
        td(w.n ? `${Math.round(w.hit_2x * 100)}%` : "—", "num"), td(w.winners ? pct(w.typical_dip) : "—", "num"),
        td(afterText(w.after), "num"),
        td(ago(w.last_trade_ts)),
        td(el("span", { class: "row" },
          el("button", { class: "ghost small", onclick: (e) => { e.stopPropagation(); whaleAction(w.address, w.muted || w.auto_muted === 1 ? "unmute" : "mute"); } },
            w.muted || w.auto_muted === 1 ? "Unmute" : "Mute"),
          el("button", { class: "ghost small", onclick: (e) => { e.stopPropagation(); if (confirm(`Stop following ${w.name}?`)) whaleAction(w.address, "remove"); } }, "Remove"))))),
      "You're not following anyone yet. Paste a wallet above, or use Find whales.");
  }

  // Live tab: every coin you hold or track in one table, with the plan's advice and sell buttons
  async function renderMyCoins() {
    const [pos, coins] = await Promise.all([api("/api/positions"), api("/api/coins"), refreshCanSell()]);
    const held = pos.open.map((p) => ({ mint: p.mint, symbol: p.symbol, kind: "held", value: usd(p.value),
      x: p.multiple, top: p.coach && p.coach.peak_multiple, mcNow: p.mc_now, whales: p.holders.filter((h) => h.still_in).length,
      hint: (p.coach && p.coach.hint) || "" }));
    const seen = new Set(held.map((h) => h.mint));
    const tracked = coins.filter((c) => !seen.has(c.mint)).map((c) => ({ mint: c.mint, symbol: c.symbol, kind: "tracked", value: "—",
      x: c.multiple, top: c.peak_multiple, mcNow: c.mc_now, whales: c.whales_in, hint: c.hint || "" }));
    table($("my-coins"), ["Coin", "", { label: "Value", num: 1 }, { label: "On cost", num: 1 }, { label: "Top", num: 1 }, { label: "MC now", num: 1 }, "Whales in", "Plan says", ""],
      [...held, ...tracked].map((c) => el("tr", { class: "click", onclick: () => openCoin(c.mint) },
        td(coinCell(c.symbol, "", c.mint)), td(el("span", { class: "muted" }, c.kind)), td(c.value, "num"),
        td(c.x ? mult(c.x) : "—", `num ${signClass((c.x || 1) - 1)}`), td(c.top ? mult(c.top) : "—", "num"),
        td(mc(c.mcNow), "num"), td(String(c.whales || 0)), td(el("span", { class: "coach" }, c.hint), "wrap"),
        td(sellButtons(c.mint, c.symbol, renderMyCoins)))),
      "Nothing held or tracked. Paste a coin address on My trades → Tracked coins to follow one.");
  }

  async function renderCoins() {
    const rows = await api("/api/coins");
    table($("coins"), ["Coin", { label: "Progress", num: 1 }, { label: "Top", num: 1 }, { label: "Now", num: 1 }, "Whales in", "What the plan says", ""],
      rows.map((c) => el("tr", { class: "click", onclick: () => openCoin(c.mint) },
        td(coinCell(c.symbol, "", c.mint)), td(`${mult(c.multiple)} (${c.ref_label})`, `num ${signClass(c.multiple - 1)}`),
        td(mult(c.peak_multiple), "num"), td(mc(c.mc_now), "num"), td(String(c.whales_in)),
        td(el("span", { class: "coach" }, c.hint || ""), "wrap"),
        td(el("span", { class: "row" }, sellButtons(c.mint, c.symbol, renderCoins),
          el("button", { class: "ghost small", onclick: async (e) => { e.stopPropagation(); await post(`/api/coins/${c.mint}/remove`, {}); renderCoins(); } }, "Stop tracking"))))),
      "No tracked coins. Paste a coin address above (add your entry market cap if you know it).");
  }

  async function renderTrades() {
    await refreshCanSell();
    renderCoins().catch(() => {});
    const d = await api("/api/positions");
    const s = d.summary;
    $("trade-tiles").replaceChildren(
      tile("Realized", usd(s.realized, true), `${plural(s.closed, "closed coin")} · ${Math.round(s.win_rate * 100)}% won`, signClass(s.realized), true),
      tile("Open P/L", usd(s.unrealized, true), `${d.open.length} open positions`, signClass(s.unrealized)),
      tile("Total bought", usd(s.invested), "all recorded buys"));
    $("sync-note").textContent = d.wallet_synced ? "synced from your wallet — exact, fees included" : "manual entries — set MY_WALLETS in .env for exact sync";
    const curve = d.curve.length ? [[d.curve[0][0] - 60, 0], ...d.curve] : [];
    lineChart($("curve"), curve, (v) => usd(v, true));
    $("curve-note").textContent = d.curve.length ? "one point per sell" : "";
    table($("open"), ["Coin", { label: "Value", num: 1 }, { label: "P/L", num: 1 }, { label: "Peak", num: 1 }, { label: "Your entry", num: 1 }, { label: "Now", num: 1 }, "Whales in", "Exit coach", ""],
      d.open.map((p) => {
        const inNow = p.holders.filter((h) => h.still_in);
        return el("tr", { class: "click", onclick: () => openCoin(p.mint) },
          td(coinCell(p.symbol, "", p.mint)), td(usd(p.value), "num"),
          td(`${usd(p.unrealized, true)} (${pct((p.multiple - 1) * 100)})`, `num ${signClass(p.unrealized)}`),
          td(p.coach && p.coach.peak_multiple ? `${mult(p.coach.peak_multiple)}` : "—", "num"),
          td(mc(p.entry_mc), "num"), td(mc(p.mc_now), "num"),
          td(p.holders.length ? `${inNow.length}/${p.holders.length} ${inNow.map((h) => h.name).slice(0, 3).join(", ")}` : "—", "wrap"),
          td(el("span", { class: "coach" }, (p.coach && p.coach.hint) || ""), "wrap"),
          td(sellButtons(p.mint, p.symbol, renderTrades)));
      }), "No open positions.");
    table($("closed"), ["Coin", { label: "Bought", num: 1 }, { label: "Sold", num: 1 }, { label: "Realized", num: 1 }, { label: "Return", num: 1 }, "Last trade"],
      d.closed.map((p) => el("tr", { class: "click", onclick: () => openCoin(p.mint) },
        td(coinCell(p.symbol, "", p.mint)), td(usd(p.bought), "num"), td(usd(p.sold), "num"),
        td(usd(p.realized, true), `num ${signClass(p.realized)}`), td(pct(p.pnl_pct), `num ${signClass(p.pnl_pct)}`), td(ago(p.last_ts)))),
      "No closed coins yet.");
  }

  // Sell buttons: sell from the live autopilot's trading wallet (asks first; the bot confirms in Telegram too)
  let canSell = false;
  async function refreshCanSell() {
    try { const l = await api("/api/live"); canSell = !!(l.available && l.wallet); } catch (e) { canSell = false; }
    return canSell;
  }
  function sellButtons(mint, symbol, after) {
    if (!canSell) return "";
    const btn = (pct, label) => el("button", { class: "ghost small", onclick: async (e) => {
      e.stopPropagation();
      if (!confirm(`Sell ${pct}% of $${symbol} from the trading wallet now?`)) return;
      e.target.disabled = true;
      try { const r = await post("/api/sell", { mint, pct }); alert(r.message); }
      catch (err) { alert(err.message); }
      if (after) after();
    } }, label);
    return el("span", { class: "row" }, btn(50, "Sell 50%"), btn(100, "Sell all"));
  }

  async function renderLiveTrading() {
    const l = await api("/api/live");
    if (!l.available) { $("live-card").hidden = true; return; }
    const state = !l.enabled ? "⚪️ Off" : l.problem ? `⚠️ On but can't trade: ${l.problem}` : l.dry_run ? "🧪 Dry run — nothing is sent" : "🔴 On — trading real SOL";
    $("live-state").textContent = state;
    $("live-hint").textContent = l.wallet
      ? `Wallet ${short(l.wallet)} · ${l.size} SOL per buy · max ${l.max_open} open · stops for the day at −${l.daily_loss} SOL. It copies the paper autopilot's trades below. ${l.dry_run ? "Dry run: real quotes and signed transactions, nothing sent." : ""}`
      : "Add TRADING_PRIVATE_KEY (a separate wallet, funded only with what you can lose) to .env and restart to enable. Start in dry run.";
    const act = (label, action, cls) => el("button", { class: cls || "ghost small", onclick: async () => {
      if (action === "dry-off" && !confirm("Trade REAL SOL from the trading wallet?")) return;
      if (action === "sellall" && !confirm("Sell every live position now?")) return;
      try { await post(`/api/live/${action}`, {}); } catch (err) { alert(err.message); }
      renderLiveTrading();
    } }, label);
    $("live-actions").replaceChildren(
      l.enabled ? act("Turn off", "off") : act("Turn on", "on", "small"),
      l.dry_run ? act("Trade for real", "dry-off") : act("Back to dry run", "dry-on"),
      act("Sell everything", "sellall"),
      el("span", { class: "muted" }, ` ${l.closed} closed · ${l.won} won · ${l.realized_sol >= 0 ? "+" : ""}${l.realized_sol.toFixed(3)} SOL · today ${l.today_sol >= 0 ? "+" : ""}${l.today_sol.toFixed(3)} SOL`));
    canSell = !!l.wallet;
    table($("live-trades"), ["Coin", { label: "SOL in", num: 1 }, { label: "SOL out", num: 1 }, { label: "P/L", num: 1 }, "Status", "Opened", ""],
      l.recent.map((t) => el("tr", { class: "click", onclick: () => openCoin(t.mint) },
        td(coinCell(t.symbol, "", t.mint)), td(t.sol_in.toFixed(3), "num"), td(t.sol_out.toFixed(3), "num"),
        td(t.status === "closed" ? `${(t.sol_out - t.sol_in >= 0 ? "+" : "")}${(t.sol_out - t.sol_in).toFixed(3)}` : "—", `num ${signClass(t.sol_out - t.sol_in)}`),
        td(t.status === "closed" ? t.close_reason : (t.dry ? "open (dry run)" : "open"), "wrap"), td(ago(t.open_ts)),
        td(t.status === "open" ? sellButtons(t.mint, t.symbol, renderLiveTrading) : ""))),
      "No live trades yet.");
  }

  async function renderPaper() {
    renderLiveTrading().catch(() => {});
    const d = await api("/api/paper");
    const s = d.summary;
    $("paper-tiles").replaceChildren(
      tile("Paper balance", usd(s.equity), `${pct(s.return_pct)} from ${usd(s.start)}`, signClass(s.equity - s.start), true),
      tile("Closed trades", String(s.closed), s.closed ? `${s.won} won · ${Math.round((s.won / s.closed) * 100)}%` : "none yet"),
      tile("Realized", usd(s.realized, true), s.closed ? `best ${usd(s.best, true)} · worst ${usd(s.worst, true)}` : "", signClass(s.realized)),
      tile("Open now", String(s.open.length), d.enabled ? "trading every alert" : "paper trading is off (Settings)"));
    lineChart($("paper-curve"), d.curve, (v) => usd(v), { base: s.start, label: "Paper balance over time",
      empty: "The balance line starts once the autopilot has traded for a few minutes." });
    $("paper-note").textContent = d.curve.length ? "every 5 minutes, open trades at market" : "";
    $("paper-rules").textContent = `${usd(d.rules.size)} a trade · ${d.rules.slippage}% slippage each way · half at 2x · out at -${d.rules.trail}% from top or -${d.rules.stop}%`;
    table($("paper-open"), ["Coin", { label: "Now", num: 1 }, { label: "Value left", num: 1 }, { label: "Top", num: 1 }, "Half sold", "Opened", ""],
      s.open.map((t) => el("tr", { class: "click", onclick: () => openCoin(t.mint) },
        td(coinCell(t.symbol, "", t.mint)), td(mult(t.multiple), `num ${signClass(t.multiple - 1)}`), td(usd(t.value), "num"),
        td(mult(t.peak_x), "num"), td(t.half_taken ? "yes" : "—"), td(ago(t.open_ts)),
        td(el("button", { class: "ghost small", onclick: async (e) => {
          e.stopPropagation();
          if (!confirm(`Close the paper trade in $${t.symbol} now?${canSell ? " (The live autopilot sells it too.)" : ""}`)) return;
          try { await post(`/api/paper/${t.id}/close`, {}); } catch (err) { alert(err.message); }
          renderPaper();
        } }, "Close")))), "No open paper trades.");
    table($("paper-closed"), ["Coin", { label: "In", num: 1 }, { label: "Out", num: 1 }, { label: "P/L", num: 1 }, "Why it sold", "Closed"],
      d.closed.map((t) => el("tr", { class: "click", onclick: () => openCoin(t.mint) },
        td(coinCell(t.symbol, "", t.mint)), td(usd(t.size_usd), "num"), td(usd(t.proceeds_usd), "num"),
        td(usd(t.proceeds_usd - t.size_usd, true), `num ${signClass(t.proceeds_usd - t.size_usd)}`),
        td(t.close_reason, "wrap"), td(ago(t.close_ts)))), "No closed paper trades yet.");
  }

  async function renderReport() {
    const r = await api(`/api/report?days=${$("report-days").value}`);
    $("lessons").replaceChildren(...r.lessons.map((t) => el("li", {}, t)));
    $("report-tiles").replaceChildren(
      tile("Result", usd(r.total, true), `${plural(r.n, "closed coin")} · ${r.dead} went to zero`, signClass(r.total), true),
      tile("Won", r.n ? `${Math.round(r.won * 100)}%` : "—", "of closed coins"),
      tile("Average win", usd(r.avg_win, true), "per winning coin", "up"),
      tile("Average loss", usd(r.avg_loss, true), "per losing coin", "down"));
    $("report-groups").replaceChildren(...r.groups.filter((g) => g.rows.length).map((g) => {
      const box = el("div", { class: "chart" });
      const card = el("div", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, g.title),
        el("span", { class: "muted" }, "total result per group")), box);
      setTimeout(() => barChart(box, g.rows.map((x) => ({ label: x.label, value: x.total, n: x.n, win: x.won })), (v) => usd(v, true), ["coin", "coins"]), 0);
      return card;
    }));
    const rows = (list) => list.map((t) => el("tr", { class: "click", onclick: () => openCoin(t.mint) },
      td(coinCell(t.symbol, "", t.mint)), td(usd(t.result, true), `num ${signClass(t.result)}`), td(usd(t.bought), "num"),
      td(t.source), td(t.hold)));
    const head = ["Coin", { label: "Result", num: 1 }, { label: "Bought", num: 1 }, "Idea", "Held"];
    table($("report-best"), head, rows(r.best), "No closed trades yet.");
    table($("report-worst"), head, rows(r.worst), "No closed trades yet.");
  }

  async function renderStats() {
    const days = $("stats-days").value;
    const r = await api(`/api/stats?days=${days}`);
    const a = r.all;
    $("stats-tiles").replaceChildren(
      tile("Copies", String(a.n), `${r.open} still open`),
      tile("Won", a.n ? `${Math.round(a.win_rate * 100)}%` : "—", "closed above entry after fees"),
      tile("Average copy", a.n ? pct(a.avg) : "—", a.n ? `median ${pct(a.median)}` : "", signClass(a.avg)),
      tile("$100 on every alert", a.n ? usd(a.per_100, true) : "—", `${r.fee_pct}% fee each way`, signClass(a.per_100)));
    barChart($("grade-chart"), ["A", "B", "C"].map((g) => ({ label: `Grade ${g}`, value: r.by_grade[g].avg, n: r.by_grade[g].n, win: r.by_grade[g].win_rate })), (v) => pct(v));
    const facts = [];
    if (r.confluence.n) facts.push([`2+ whales in the same coin: `, `${pct(r.confluence.avg)} avg`, ` over ${plural(r.confluence.n, "copy", "copies")}, vs ${pct(r.solo.avg)} for a single whale.`]);
    if (r.winners) facts.push([`Winners dipped `, `${pct(r.typical_dip)}`, ` (median) before running. ${r.stop20_would_kill} of ${r.winners} would have been sold by a −20% stop-loss — which is why the bot follows the whale's exit instead.`]);
    if (a.n) facts.push([`Copies that reached 2x: `, `${Math.round(a.hit_2x * 100)}%`, "."]);
    if (r.runners && r.runners.n) facts.push([`Community runners (no whale): `, `${pct(r.runners.avg)} avg`,
      ` over ${plural(r.runners.n, "alert")}, ${Math.round(r.runners.win_rate * 100)}% won. Measured separately from whale copies.`]);
    if (!facts.length) facts.push(["", "No copies yet.", " Every alert opens a simulated copy; results appear as whales sell."]);
    $("facts").replaceChildren(...facts.map(([pre, strong, post]) => el("li", {}, pre, el("b", {}, strong), post)));
  }

  async function renderExits() {
    const d = await api("/api/exits");
    const h = d.habits, lab = d.lab;
    $("exit-tiles").replaceChildren(
      tile("After you sell", h.measured_after ? pct(h.median_after_gain) : "—", h.measured_after ? `median move in the next 24h · ${plural(h.measured_after, "sell")}` : "needs a day of tracked sells", h.median_after_gain >= 40 ? "down" : ""),
      tile("Below your peak", h.measured_before ? `${Math.round(h.median_below_peak)}%` : "—", h.measured_before ? "how far under the best price you'd seen" : "needs tracked sells", h.median_below_peak >= 30 ? "down" : ""),
      tile("Best exit style", lab.best && lab.best.n ? lab.best.label : "—", lab.best && lab.best.n ? `${pct(lab.best.avg)} avg over ${plural(lab.copies, "alert")}` : "needs recorded alerts", ""));
    const styleName = { whale: "Whale exit", all2x: "All at 2x", half2x: "Half at 2x", trail: "Trail 35%", ladder: "Ladder" };
    barChart($("lab-chart"), lab.rules.map((r) => ({ label: styleName[r.key], value: r.avg, n: r.n, win: r.win_rate })), (v) => pct(v));
    $("lab-note").textContent = lab.copies ? plural(lab.copies, "alert") : "";
    const advice = h.advice.slice();
    if (lab.best && lab.best.n >= 5) advice.push(`On your whales lately, "${lab.best.label}" returned ${pct(lab.best.avg)} per alert on average.`);
    advice.push(`Nudges: ladder ${d.ladder ? "on" : "off"} (2x/3x/5x/10x) · profit protector ${d.protect_after > 0 ? `after ${d.protect_after}x at −${d.protect_trail}% from peak` : "off"}. Change in Settings.`);
    $("exit-advice").replaceChildren(...advice.map((t) => el("li", {}, t)));
    table($("my-sells"), ["When", "Coin", { label: "Sold", num: 1 }, { label: "At MC", num: 1 }, { label: "Below your peak", num: 1 }, { label: "Next 24h high", num: 1 }, "Verdict"],
      h.sells.map((sl) => {
        const early = sl.after_gain_pct !== undefined && sl.after_gain_pct >= 50;
        const late = sl.below_peak_pct !== undefined && sl.below_peak_pct >= 35;
        return el("tr", { class: "click", onclick: () => openCoin(sl.mint) }, td(ago(sl.ts)), td(`$${sl.symbol}`),
          td(usd(sl.usd), "num"), td(mc(sl.mc), "num"),
          td(sl.below_peak_pct === undefined ? "—" : `${Math.round(sl.below_peak_pct)}%`, "num"),
          td(sl.after_gain_pct === undefined ? "—" : pct(sl.after_gain_pct), "num"),
          td(early && late ? "late, then it ran again" : early ? "too early" : late ? "too late" : sl.after_gain_pct === undefined ? "watching…" : "good"));
      }), "No sells in the last 30 days.");
  }

  async function renderScout() {
    const s = await api("/api/scout");
    const sum = s.last_summary || {};
    $("scout-summary").textContent = `${s.enabled ? "On" : "Off (Settings: WHALE_PICKS)"} · ${s.auto_follow ? "auto-follows picks" : "suggests, you follow"} · last run ${s.last_run ? ago(s.last_run) : "not yet"}`
      + (sum.coins ? ` — researched ${plural(sum.coins.length, "coin")}, checked ${plural(sum.checked || 0, "wallet")}, picked ${(sum.picked || 0) + (sum.followed || 0)}` : "")
      + ". Early buyers of today's runners, replayed as a copier who buys a minute late: holders whose copies made money are followed, flippers are skipped.";
    $("scout-run").disabled = s.running;
    table($("scout"), ["Wallet", "Found in", "Status", { label: "Profit", num: 1 }, { label: "Won", num: 1 }, { label: "Trades", num: 1 }, "Why", ""],
      s.candidates.map((c) => el("tr", {}, td(el("span", { class: "mono" }, short(c.address))), td(c.coins.split(",").filter(Boolean).map((x) => `$${x}`).join(", ")),
        td(c.status === "followed" ? "➕ followed" : c.status === "picked" ? "⭐ picked" : c.status === "dropped" ? "➖ dropped" : "passed"),
        td(`${num(c.pnl_sol) > 0 ? "+" : ""}${num(c.pnl_sol).toFixed(1)} SOL`, `num ${signClass(c.pnl_sol)}`),
        td(`${Math.round(num(c.win_rate) * 100)}%`, "num"), td(String(c.trips), "num"), td(c.reason, "wrap"),
        td(c.status === "picked" ? el("button", { class: "small", onclick: async (e) => {
          try { await post("/api/whales", { address: c.address, source: "picks" }); e.target.textContent = "Following"; e.target.disabled = true; }
          catch (err) { e.target.textContent = err.message; } } }, "Follow") : ""))),
      "The autopilot hasn't scouted yet — it runs a few minutes after start, then every few hours.");
  }

  async function renderSettings() {
    const [settings, o] = await Promise.all([api("/api/settings"), renderStatus()]);
    let lastGroup = "";
    $("settings").replaceChildren(...settings.flatMap((s) => {
      const head = s.group !== lastGroup ? [el("h3", { class: "settings-group" }, (lastGroup = s.group))] : [];
      const input = s.bool
        ? el("select", {}, el("option", { value: "1", selected: s.value >= 0.5 }, "on"), el("option", { value: "0", selected: s.value < 0.5 }, "off"))
        : el("input", { type: "number", value: s.value, min: s.min, max: s.max, step: "any" });
      const msg = el("span", { class: "form-msg" });
      input.addEventListener("change", async () => {
        try { await post("/api/settings", { name: s.name, value: input.value }); msg.textContent = "saved"; }
        catch (err) { msg.textContent = err.message; }
      });
      return [...head, el("div", { class: "settings-row" }, el("div", {}, el("div", { class: "name" }, s.name), el("div", { class: "help" }, s.help), msg), input)];
    }));
    const h = [["Version", o.version], ["Up for", dur(o.uptime)], ["Wallet stream", o.stream.connected ? `connected (${o.stream.subs} subscriptions)` : `reconnecting ${o.stream.error || ""}`],
      ["RPC", `${o.helius ? "Helius" : "public"} · ${o.rpc.calls.toLocaleString()} calls · ${o.rpc.errors} errors · ${o.rpc.rate_limited} rate-limited`],
      ["Your wallet", o.wallet_synced ? "synced" : "not synced (MY_WALLETS in .env)"], ["Recent issues", o.errors.join(" | ") || "none"]];
    $("health").replaceChildren(...h.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
  }

  // ---------- find & analyze ------------------------------------------------------------
  function findTable(result) {
    const box = el("div");
    for (const c of result.coins) {
      box.append(el("p", { class: "hint" }, c.ok
        ? `$${c.symbol}: peak ${mc(c.peak_mc)} · now ${mc(c.now_mc)} · "early" = under ${mc(c.early_mc)} · read ${c.early_trades_read} early trades${c.from_launch ? "" : " (history too long to reach launch)"}`
        : `${short(c.mint)}: ${c.error}`));
    }
    const t = el("table");
    table(t, ["Wallet", "Early in", { label: "Bought", num: 1 }, { label: "Entry MC", num: 1 }, { label: "To peak", num: 1 }, "Holding", "Flags", ""],
      result.wallets.map((w) => {
        const msg = el("span", { class: "form-msg" });
        return el("tr", {}, td(el("span", { class: "mono" }, short(w.wallet))), td(w.coins.join(", ")),
          td(usd(w.buy_usd), "num"), td(mc(w.entry_mc), "num"), td(mult(w.to_peak), "num"),
          td(w.held_pct === null || w.held_pct === undefined ? "?" : w.held_pct >= 1 ? `${Math.round(w.held_pct)}%` : "sold"),
          td(w.flags.join(", ") || (w.coins.length > 1 ? "⭐ repeat" : ""), "wrap"),
          td(el("span", { class: "row" },
            w.tracked ? el("span", { class: "muted" }, "following") : el("button", { class: "small", onclick: async () => {
              try { await post("/api/whales", { address: w.wallet, source: "find" }); msg.textContent = "following"; } catch (err) { msg.textContent = err.message; }
            } }, "Follow"),
            el("button", { class: "ghost small", onclick: () => analyze(w.wallet) }, "Analyze"), msg)));
      }), "No early buyers over $100 found.");
    box.append(el("div", { class: "table-wrap" }, t));
    return box;
  }
  async function pollFind(id) {
    const job = await api(`/api/find/${id}`);
    if (job.status === "running") {
      $("find-msg").textContent = job.progress || "working…";
      setTimeout(() => pollFind(id), 2000);
      return;
    }
    $("find-msg").textContent = job.status === "done" ? "done" : `failed: ${job.progress}`;
    if (job.result) $("find-result").replaceChildren(findTable(job.result));
  }
  async function analyze(wallet) {
    $("analyze-form").wallet.value = wallet;
    $("analyze-msg").textContent = "reading recent swaps… (up to a minute)";
    $("analyze-result").replaceChildren();
    showTab("find");
    try {
      const r = await post("/api/analyze", { wallet });
      $("analyze-msg").textContent = "";
      const text = (r.text || "").replace(/<[^>]+>/g, "").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">");
      const follow = el("button", { class: "small", onclick: async () => {
        try { await post("/api/whales", { address: wallet, source: "analyze" }); follow.textContent = "Following"; follow.disabled = true; } catch (err) { follow.textContent = err.message; }
      } }, "Follow this wallet");
      $("analyze-result").replaceChildren(el("div", { class: "analysis" }, text), r.ok ? el("div", { class: "row", style: "margin-top:8px" }, follow) : null);
    } catch (err) { $("analyze-msg").textContent = err.message; }
  }

  // ---------- drawers -------------------------------------------------------------------
  function openDrawer(...children) {
    $("drawer-body").replaceChildren(...children);
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
  }
  function closeDrawer() { $("drawer").classList.remove("open"); $("drawer").setAttribute("aria-hidden", "true"); hideTip(); }
  $("drawer-close").addEventListener("click", closeDrawer);
  $("drawer").addEventListener("click", (e) => { if (e.target === $("drawer")) closeDrawer(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  async function openCoin(mint) {
    openDrawer(el("p", { class: "muted" }, "Loading…"));
    const r = await api(`/api/coin/${mint}`);
    const i = r.info || {};
    const holders = el("table");
    table(holders, ["Whale", { label: "Entry MC", num: 1 }, { label: "Now", num: 1 }, "Holding", { label: "Bought", num: 1 }, { label: "Sold", num: 1 }],
      r.holders.map((h) => el("tr", {}, td(h.name), td(mc(h.entry_mc), "num"), td(mult(h.x), `num ${h.x >= 1 ? "up" : "down"}`),
        td(h.still_in ? `${Math.round(h.holding_pct)}% of bag` : "sold out"), td(usd(h.bought_usd), "num"), td(usd(h.sold_usd), "num"))),
      "None of your whales traded this coin.");
    const trades = el("table");
    table(trades, ["When", "Who", "Side", { label: "USD", num: 1 }, { label: "MC", num: 1 }],
      r.trades.map((t) => el("tr", {}, td(ago(t.ts)), td(t.who), td(t.side === "BUY" ? "🟢 buy" : `🔴 sell ${t.sell_fraction ? Math.round(t.sell_fraction * 100) + "%" : ""}`),
        td(usd(t.usd_value), "num"), td(mc(t.mc_usd), "num"))), "No trades recorded.");
    const p = r.position;
    const supply = num(i.price_usd) > 0 ? num(i.mc_usd) / num(i.price_usd) : 0;
    const chart = el("div", { class: "chart" });
    const events = r.trades.map((t) => ({ ts: t.ts, mc: t.mc_usd, side: t.side, usd: t.usd_value, who: t.is_me ? "me" : "whale", label: t.who }))
      .concat(r.my_trades.filter((t) => !r.trades.some((w) => w.is_me && Math.abs(w.ts - t.ts) < 5))
        .map((t) => ({ ts: t.ts, mc: t.price_usd * supply, side: t.side, usd: t.usd, who: "me", label: "You" })));
    openDrawer(
      el("h3", {}, coinCell(i.symbol, i.image, mint)),
      el("div", { class: "kv" }, el("span", {}, "MC ", el("b", {}, mc(i.mc_usd))),
        el("span", {}, "Liquidity ", el("b", {}, num(i.liquidity_usd) >= 0 && i.liquidity_usd !== undefined ? usd(i.liquidity_usd) : "unknown")),
        el("span", {}, "1h ", el("b", {}, pct(i.change_h1))), links(mint, i.pair_address)),
      el("div", { class: "mono muted" }, mint),
      p && p.open ? el("div", { class: "kv" }, el("span", {}, "You hold ", el("b", {}, usd(p.value))),
        el("span", { class: signClass(p.unrealized) }, `${usd(p.unrealized, true)} (${pct((p.multiple - 1) * 100)})`),
        el("span", {}, "your entry ", el("b", {}, mc(p.entry_mc)))) : null,
      chart,
      el("h2", { style: "margin-top:16px" }, "Your whales in this coin"), el("div", { class: "table-wrap" }, holders),
      el("h2", { style: "margin-top:16px" }, "Trades"), el("div", { class: "table-wrap" }, trades));
    if (supply > 0) priceChart(chart, r.path, events, supply);
  }

  async function openWhale(address) {
    openDrawer(el("p", { class: "muted" }, "Loading…"));
    const r = await api(`/api/whale/${address}`);
    const s = r.stats, w = r.whale;
    const trades = el("table");
    table(trades, ["When", "Coin", "Side", { label: "USD", num: 1 }, { label: "MC", num: 1 }],
      r.trades.map((t) => el("tr", { class: "click", onclick: () => openCoin(t.mint) }, td(ago(t.ts)), td(`$${t.symbol}`),
        td(t.side === "BUY" ? "🟢 buy" : "🔴 sell"), td(usd(t.usd_value), "num"), td(mc(t.mc_usd), "num"))), "No trades seen yet.");
    const copies = el("table");
    table(copies, ["Opened", "Coin", { label: "Return", num: 1 }, { label: "Peak", num: 1 }, { label: "Dip first", num: 1 }, "State"],
      r.copies.map((c) => {
        const ret = c.ret;
        return el("tr", {}, td(ago(c.open_ts)), td(`$${c.symbol}`), td(pct(ret), `num ${signClass(ret)}`),
          td(mult(c.peak_price / c.entry_price), "num"), td(pct(c.dip_before_peak_pct), "num"), td(c.status === "closed" ? c.close_reason : "open"));
      }), "No copies yet.");
    openDrawer(
      el("h3", {}, w.name), el("div", { class: "mono muted" }, w.address),
      el("div", { class: "kv" }, el("span", {}, el("b", {}, STATUS[s.status])),
        el("span", {}, "copies ", el("b", {}, String(s.n))), el("span", {}, "won ", el("b", {}, s.n ? `${Math.round(s.win_rate * 100)}%` : "—")),
        el("span", {}, "avg ", el("b", { class: signClass(s.avg) }, s.n ? pct(s.avg) : "—")),
        el("span", {}, "$100 each → ", el("b", { class: signClass(s.profit_per_100) }, s.n ? usd(s.profit_per_100, true) : "—")),
        el("a", { href: `https://gmgn.ai/sol/address/${w.address}`, target: "_blank", rel: "noopener" }, "GMGN"),
        el("a", { href: `https://solscan.io/account/${w.address}`, target: "_blank", rel: "noopener" }, "Solscan")),
      el("div", { class: "row" }, el("button", { class: "ghost small", onclick: () => { closeDrawer(); analyze(w.address); } }, "Analyze full history")),
      el("h2", { style: "margin-top:16px" }, "Simulated copies"), el("div", { class: "table-wrap" }, copies),
      el("h2", { style: "margin-top:16px" }, "Latest trades"), el("div", { class: "table-wrap" }, trades));
  }

  // ---------- wiring --------------------------------------------------------------------
  const renderers = { live: renderLive, hot: renderHot, whales: renderWhales, paper: renderPaper, trades: renderTrades, report: renderReport, exits: renderExits, stats: renderStats, find: renderStatus, settings: renderSettings };
  async function refresh() {
    try { await renderers[activeTab](); }
    catch (err) { $("status").replaceChildren(el("span", { class: "pill bad" }, el("span", { class: "dot" }), `Bot not reachable: ${err.message}`)); }
  }
  function showTab(name) {
    activeTab = name;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.id === `tab-${name}`));
    try { localStorage.setItem("fomo-tab", name); } catch (e) { /* storage unavailable */ }
    refresh();
  }
  $("tabs").addEventListener("click", (e) => { if (e.target.dataset.tab) showTab(e.target.dataset.tab); });
  $("coin-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const r = await post("/api/coins", { mint: f.get("mint"), entry_mc: f.get("entry_mc") });
      $("coin-msg").textContent = r.message; e.target.reset(); renderCoins();
    } catch (err) { $("coin-msg").textContent = err.message; }
  });
  $("hot-hours").addEventListener("change", renderHot);
  $("stats-days").addEventListener("change", renderStats);
  $("report-days").addEventListener("change", renderReport);

  $("add-whale").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try { const r = await post("/api/whales", { address: f.address.value.trim(), name: f.name.value.trim() }); $("add-msg").textContent = r.message; f.reset(); renderWhales(); }
    catch (err) { $("add-msg").textContent = err.message; }
  });
  $("trade-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const amount = f.usd.value.trim();
    const body = { mint: f.mint.value.trim(), side: f.side.value, mc: f.mc.value.trim() };
    if (amount.endsWith("%")) body.fraction = Number(amount.slice(0, -1)) / 100; else body.usd = Number(amount.replace(/[$,]/g, ""));
    try { const r = await post("/api/trades", body); $("trade-msg").textContent = `saved at ${mc(r.mc)} MC`; f.usd.value = ""; renderTrades(); }
    catch (err) { $("trade-msg").textContent = err.message; }
  });
  $("undo").addEventListener("click", async () => {
    const r = await post("/api/trades/undo");
    $("trade-msg").textContent = r.ok ? "last entry removed" : "nothing to undo";
    renderTrades();
  });
  $("find-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const mints = e.target.mints.value.split(/[\s,]+/).filter(Boolean);
    $("find-result").replaceChildren();
    try { const r = await post("/api/find", { mints }); $("find-msg").textContent = "started…"; pollFind(r.id); }
    catch (err) { $("find-msg").textContent = err.message; }
  });
  $("scout-run").addEventListener("click", async () => {
    try { await post("/api/scout/run"); $("scout-msg").textContent = "scouting… (a few minutes)"; $("scout-run").disabled = true; }
    catch (err) { $("scout-msg").textContent = err.message; }
  });
  $("analyze-form").addEventListener("submit", (e) => { e.preventDefault(); analyze(e.target.wallet.value.trim()); });

  $("theme").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("fomo-theme", document.documentElement.dataset.theme); } catch (e) { /* ignore */ }
    refresh();
  });
  try {
    const saved = localStorage.getItem("fomo-theme");
    if (saved) document.documentElement.dataset.theme = saved;
    const tab = localStorage.getItem("fomo-tab");
    if (tab && renderers[tab]) activeTab = tab;
  } catch (e) { /* storage unavailable */ }

  // show the last find result if there is one
  api("/api/find").then((job) => { if (job && job.result) $("find-result").replaceChildren(findTable(job.result)); }).catch(() => {});
  showTab(activeTab);
  setInterval(() => { if (!document.hidden && !$("drawer").classList.contains("open") && activeTab !== "find" && activeTab !== "settings") refresh(); }, 6000);
})();
