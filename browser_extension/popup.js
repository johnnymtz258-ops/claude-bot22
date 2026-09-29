const $ = (id) => document.getElementById(id);
const mc = (v) => { v = Number(v) || 0; if (v <= 0) return "?"; for (const [s, u] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) if (v >= s) return `$${(v / s).toFixed(1).replace(/\.0$/, "")}${u}`; return `$${v.toFixed(0)}`; };
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };

chrome.storage.local.get("port").then(({ port }) => { $("port").value = port || 8787; });
$("port").addEventListener("change", () => chrome.storage.local.set({ port: Number($("port").value) || 8787 }).then(load));
$("open").addEventListener("click", () => chrome.runtime.sendMessage({ type: "dashboard" }, (r) => r && r.ok && chrome.tabs.create({ url: r.url })));

function load() {
  chrome.runtime.sendMessage({ type: "feed" }, (r) => {
    if (!r || !r.ok) { $("state").textContent = "offline"; $("list").textContent = "FomoBot isn't running on this computer."; return; }
    $("state").textContent = "live";
    const buys = r.data.buys.slice(0, 10);
    if (!buys.length) { $("list").textContent = "No whale buys in the last 3 days."; return; }
    $("list").replaceChildren(...buys.map((b) => {
      const row = el("div", "row");
      const change = b.change === null ? "" : ` (${b.change > 0 ? "+" : ""}${b.change.toFixed(0)}%)`;
      row.append(el("span", "", `${b.grade} · ${b.whale} → $${b.symbol}`),
        el("span", b.change > 0 ? "up" : b.change < 0 ? "down" : "muted", `${mc(b.entry_mc)} → ${mc(b.now_mc)}${change}`));
      return row;
    }));
  });
}
load();
