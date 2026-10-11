/* TickerForge service worker.
 *
 * The rule this file exists to enforce: **never cache /api**.
 *
 * A service worker's default temptation is stale-while-revalidate on
 * everything, and on a trading app that is actively dangerous. A cached quote
 * looks exactly like a live one — same layout, same styling, no indication
 * that the number is from an hour ago — and you'd size a position against it.
 * A page that fails to load tells you something is wrong. A page that loads
 * with yesterday's price does not.
 *
 * So the split is:
 *   - **App shell** (HTML, CSS, icons): cache-first, refreshed in the
 *     background. This is what makes the icon on your home screen open
 *     instantly instead of showing a white rectangle on a slow connection.
 *   - **Everything under /api**: network only. No fallback, no cache, no
 *     exceptions. Offline means the page says so.
 *
 * Bump CACHE when the shell changes, or old clients keep the old CSS for ever.
 */
const CACHE = "tickerforge-shell-v2";

const SHELL = [
  "/desk",
  "/app.css",
  "/ui.js",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      // addAll is all-or-nothing: one 404 would leave the worker uninstalled
      // and the app permanently non-offline. Fetch individually and keep what
      // succeeds.
      .then((cache) =>
        Promise.all(
          SHELL.map((url) =>
            cache.add(url).catch(() => {
              /* a missing shell file is not worth failing the install over */
            }),
          ),
        ),
      )
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // --- market data: never served from cache, at any cost.
  if (url.pathname.startsWith("/api/")) return;

  // --- navigations: network first, so a deploy actually reaches you. The
  //     cached shell is the fallback for a dead connection, not the default.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() =>
          caches.match(req).then((hit) => hit || caches.match("/desk")),
        ),
    );
    return;
  }

  // --- static assets: cache first, refresh quietly behind it.
  event.respondWith(
    caches.match(req).then((hit) => {
      const network = fetch(req)
        .then((res) => {
          if (res && res.status === 200) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => hit);
      return hit || network;
    }),
  );
});
