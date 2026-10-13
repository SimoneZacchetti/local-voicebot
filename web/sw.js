// Service worker: makes the app installable and fast to reopen on the phone.
// - /vendor/* (VAD model + 11 MB wasm): cache-first, bump VENDOR_VERSION when vendor changes.
// - app shell: network-first, cached copy when the Mac is unreachable (the page then says so).
// - /ws, /api, /healthz: never cached.
const VENDOR_VERSION = "v1";
const VENDOR_CACHE = `vendor-${VENDOR_VERSION}`;
const SHELL_CACHE = "shell";
const SHELL = ["./", "app.js", "viz.js", "style.css", "player-worklet.js", "manifest.json", "favicon.svg", "icons/apple-touch-icon.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("vendor-") && k !== VENDOR_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (/^\/(ws|api|healthz)(\/|$)/.test(url.pathname)) return;
  if (url.pathname.startsWith("/vendor/")) {
    e.respondWith(
      caches.open(VENDOR_CACHE).then(async (c) => {
        const hit = await c.match(e.request);
        if (hit) return hit;
        const res = await fetch(e.request);
        if (res.ok) c.put(e.request, res.clone());
        return res;
      }),
    );
    return;
  }
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok) caches.open(SHELL_CACHE).then((c) => c.put(e.request, res.clone()));
        return res;
      })
      .catch(async () => (await caches.match(e.request, { ignoreSearch: true })) || offline()),
  );
});

function offline() {
  const html = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voicebot</title><body style="font:17px -apple-system,sans-serif;display:grid;place-items:center;height:100vh;margin:0;background:#07080c;color:#f2f4f8;text-align:center">
<div><p style="font-size:20px;font-weight:600">Mac unreachable</p><p style="opacity:.6">Check that the Mac is on and Tailscale is running,<br>then reopen the app.</p></div>`;
  return new Response(html, { status: 503, headers: { "content-type": "text/html; charset=utf-8" } });
}
