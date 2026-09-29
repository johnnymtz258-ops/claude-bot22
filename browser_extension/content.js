// Adds a small FomoBot panel to coin and wallet pages. All data is rendered as text.
(() => {
  const ADDRESS = /[1-9A-HJ-NP-Za-km-z]{32,44}/g;
  let current = "";
  let hidden = new Set();
  let collapsed = false;

  const host = document.createElement("div");
  host.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;";
  const root = host.attachShadow({ mode: "closed" });
  root.innerHTML = `<style>
    .box{font:13px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;background:#1a1a19;color:#fff;border:1px solid rgba(255,255,255,.12);
      border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.35);width:300px;max-width:calc(100vw - 32px);overflow:hidden}
    .head{display:flex;align-items:center;gap:6px;padding:8px 10px;background:#232322;cursor:pointer;user-select:none}
    .head b{flex:1}
    .head button{all:unset;cursor:pointer;color:#c3c2b7;padding:0 4px}
    .body{padding:8px 10px;max-height:320px;overflow:auto}
    .row{display:flex;justify-content:space-between;gap:8px;padding:3px 0;border-bottom:1px solid #2c2c2a}
    .row:last-child{border-bottom:0}
    .muted{color:#898781}.up{color:#0ca30c}.down{color:#e66767}
    .btn{all:unset;cursor:pointer;background:#3987e5;color:#fff;border-radius:6px;padding:4px 10px;font-weight:600;margin-top:6px;display:inline-block}
  </style><div class="box"><div class="head"><span>🐋</span><b>FomoBot</b><button class="min" title="Minimize">–</button><button class="x" title="Hide for this page">✕</button></div><div class="body"></div></div>`;
  const body = root.querySelector(".body");
  root.querySelector(".head").addEventListener("click", () => { collapsed = !collapsed; body.style.display = collapsed ? "none" : ""; });
  root.querySelector(".x").addEventListener("click", (e) => { e.stopPropagation(); hidden.add(current); host.remove(); });

  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const mc = (v) => { v = Number(v) || 0; if (v <= 0) return "?"; for (const [s, u] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) if (v >= s) return `$${(v / s).toFixed(1).replace(/\.0$/, "")}${u}`; return `$${v.toFixed(0)}`; };
  const usd = (v, signed) => { v = Number(v) || 0; const s = signed ? (v > 0 ? "+" : v < 0 ? "−" : "") : ""; return `${s}$${Math.abs(v) >= 1000 ? Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 }) : Math.abs(v).toFixed(2)}`; };
  const mult = (v) => (Number(v) > 0 ? `${Number(v) < 10 ? Number(v).toFixed(1) : Number(v).toFixed(0)}x` : "?");
  const row = (left, right, cls) => { const r = el("div", "row"); r.append(el("span", "", left), el("span", cls || "", right)); return r; };

  function show(nodes) {
    body.replaceChildren(...nodes);
    if (!host.isConnected) document.documentElement.append(host);
  }

  function render(d, address) {
    if (d.type === "coin") {
      const sym = (d.info && d.info.symbol) || address.slice(0, 4);
      const inNow = d.holders.filter((h) => h.still_in).length;
      const nodes = [el("div", "", d.holders.length ? `$${sym} · ${d.holders.length} of your whales traded it, ${inNow} still in` : `$${sym} · none of your whales traded it`)];
      d.holders.forEach((h) => nodes.push(row(h.name, `in ${mc(h.entry_mc)} · ${h.still_in ? `holding ${Math.round(h.holding_pct)}%` : "sold out"}`, h.still_in ? "up" : "muted")));
      const p = d.position;
      if (p && p.open) nodes.push(row("You", `${usd(p.value)} (${usd(p.unrealized, true)})`, p.unrealized >= 0 ? "up" : "down"));
      collapsed = !d.holders.length && !(p && p.open);  // nothing to say: keep it to a small header
      body.style.display = collapsed ? "none" : "";
      show(nodes);
    } else if (d.type === "whale") {
      collapsed = false; body.style.display = "";
      const s = d.stats;
      show([el("div", "", `${d.whale.name} — ${s.status}${d.whale.muted ? " · muted" : ""}`),
        row("Copies", s.n ? `${s.n} · ${Math.round(s.win_rate * 100)}% won` : "none yet"),
        row("Average copy", s.n ? `${s.avg > 0 ? "+" : ""}${s.avg.toFixed(0)}%` : "—", s.avg >= 0 ? "up" : "down"),
        row("Winners dipped first", s.winners ? `${s.typical_dip.toFixed(0)}%` : "—")]);
    } else if (d.type === "wallet") {
      const btn = el("button", "btn", "Follow this wallet");
      btn.addEventListener("click", () => {
        const name = prompt("Name for this whale (optional):", "") || "";
        chrome.runtime.sendMessage({ type: "follow", address, name }, (r) => {
          btn.textContent = r && r.ok ? "Following ✓" : `Failed: ${(r && r.error) || "bot offline"}`;
        });
      });
      show([el("div", "", "You're not following this wallet."), btn]);
    } else {
      host.remove();
    }
  }

  function check(force) {
    const found = (location.href.match(ADDRESS) || [])[0] || "";
    if (!found || hidden.has(found)) { if (!found) host.remove(); current = found; return; }
    if (found === current && !force) return;
    current = found;
    chrome.runtime.sendMessage({ type: "lookup", address: found }, (r) => {
      if (chrome.runtime.lastError || found !== current) return;
      if (!r || !r.ok) { show([el("div", "muted", "FomoBot isn't running (start it to see whale info).")]); return; }
      render(r.data, found);
    });
  }

  setInterval(() => check(false), 1200);   // single-page sites change URL without reloading
  setInterval(() => check(true), 30000);   // refresh holdings while you watch a chart
  check(true);
})();
