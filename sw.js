// Cache the app shell up front and keep it fresh: the shell is fetched from the network
// first and the cache is only the offline fallback. Audio clips (served from R2) are
// cached the first time each is heard, so the app still works offline.
// Bump SHELL/CLIPS when the caching logic changes so installed copies drop the old cache.
const SHELL = 'nghe-shell-v4';
const CLIPS = 'nghe-clips-v3';
const FILES = ['./', 'index.html', 'data.json', 'manifest.webmanifest', 'icon.svg', 'theme.js'];

// where the audio lives now — clips from this host are cached like local /audio/ used to be
const AUDIO_HOST = 'pub-02e9ae05e89a4e768502c5de99c7a3d9.r2.dev';

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(
    ks.filter(k => k !== SHELL && k !== CLIPS).map(k => caches.delete(k))
  )).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  const isClip = url.hostname === AUDIO_HOST ||
                 (url.origin === location.origin && url.pathname.includes('/audio/'));

  if (isClip) {                                          // clips: stale-while-revalidate
    if (e.request.headers.has('range')) return;          // let range requests pass through uncached
    e.respondWith((async () => {
      const c = await caches.open(CLIPS);
      const hit = await c.match(e.request);
      // always refetch in the background so a re-recorded clip refreshes next time
      const net = fetch(e.request).then(res => {
        // cache full responses (same-origin ok, or cross-origin opaque); skip 206 partials
        if (res && res.status !== 206 && (res.ok || res.type === 'opaque')) c.put(e.request, res.clone());
        return res;
      }).catch(() => null);
      e.waitUntil(net);                                  // keep the worker alive to finish it
      return hit || (await net) || Response.error();
    })());
    return;
  }

  if (url.origin !== location.origin) return;            // other cross-origin (fonts) → network

  e.respondWith(fetch(e.request).then(res => {           // shell: network first, cache if offline
    if (res.ok) { const copy = res.clone(); caches.open(SHELL).then(c => c.put(e.request, copy)); }
    return res;
  }).catch(() => caches.match(e.request)));
});
