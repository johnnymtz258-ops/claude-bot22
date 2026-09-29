// Talks to the FomoBot dashboard API on this computer. Content scripts can't reach
// localhost directly, so they ask this service worker.
const DEFAULT_PORT = 8787;

async function base() {
  const { port } = await chrome.storage.local.get("port");
  return `http://127.0.0.1:${port || DEFAULT_PORT}`;
}

async function getJSON(path) {
  const r = await fetch(`${await base()}${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function postJSON(path, body) {
  const { token } = await getJSON("/api/session");
  const r = await fetch(`${await base()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Fomo-Token": token },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.message || `HTTP ${r.status}`);
  return data;
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (async () => {
    try {
      if (msg.type === "lookup") reply({ ok: true, data: await getJSON(`/api/lookup/${encodeURIComponent(msg.address)}`) });
      else if (msg.type === "follow") reply({ ok: true, data: await postJSON("/api/whales", { address: msg.address, name: msg.name || "", source: "extension" }) });
      else if (msg.type === "feed") reply({ ok: true, data: await getJSON("/api/feed") });
      else if (msg.type === "overview") reply({ ok: true, data: await getJSON("/api/overview") });
      else if (msg.type === "dashboard") reply({ ok: true, url: await base() });
      else reply({ ok: false, error: "unknown request" });
    } catch (err) {
      reply({ ok: false, error: String(err && err.message || err) });
    }
  })();
  return true; // keep the channel open for the async reply
});
