/**
 * Nivora Service Worker — offline queue support
 *
 * Strategy:
 *   - Static assets  → Cache-first (long-lived CSS/fonts/images)
 *   - /visits/queue-status → Network-first, fall back to cache (stale-while-offline)
 *   - Everything else → Network-only (authenticated HTML pages should not be cached)
 */

const CACHE_NAME    = "nivora-v1";
const QUEUE_CACHE   = "nivora-queue-v1";

// Static assets to pre-cache on install
const PRECACHE_URLS = [
  "/static/css/main.css",
  "/static/js/drugs.js",
];

// ── Install ──────────────────────────────────────────────────────────────────
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS))
  );
  self.skipWaiting();
});

// ── Activate — clear old caches ───────────────────────────────────────────────
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== CACHE_NAME && k !== QUEUE_CACHE)
          .map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// ── Fetch ─────────────────────────────────────────────────────────────────────
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // 1. Queue status — network-first, fall back to last cached JSON
  if (
    url.pathname === "/visits/queue-status" ||
    url.pathname.startsWith("/queue/") && url.pathname.endsWith("/status")
  ) {
    event.respondWith(networkFirstQueueFetch(event.request));
    return;
  }

  // 2. Static assets — cache-first
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(cacheFirstFetch(event.request));
    return;
  }

  // 3. Everything else — network-only (authenticated routes must not be cached)
});

// ── Helpers ───────────────────────────────────────────────────────────────────

async function networkFirstQueueFetch(request) {
  const cache = await caches.open(QUEUE_CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) {
      // Update the cache with fresh data
      cache.put(request, response.clone());
    }
    return response;
  } catch (_) {
    // Offline — return cached copy if available
    const cached = await cache.match(request);
    if (cached) {
      // Add a custom header so the app can detect stale data
      const headers = new Headers(cached.headers);
      headers.set("X-Clinicos-Offline", "1");
      const body = await cached.blob();
      return new Response(body, { status: cached.status, headers });
    }
    // No cache — return a minimal offline JSON payload matching queue status shape
    return new Response(
      JSON.stringify({ offline: true, now_serving: null, up_next: [], queue_length: 0, avg_wait_mins: 0 }),
      { status: 200, headers: { "Content-Type": "application/json", "X-Clinicos-Offline": "1" } }
    );
  }
}

async function cacheFirstFetch(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, response.clone());
    }
    return response;
  } catch (_) {
    return new Response("Offline", { status: 503 });
  }
}
