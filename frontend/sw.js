// Service worker: приложение открывается и без связи (в колодце, в подвале, за городом).
//
// Что кэшируется:
//  - оболочка (HTML, CSS, JS, Leaflet, иконки) — «сначала сеть»: после обновления системы
//    сразу работает новая версия, а без связи открывается последняя загруженная;
//  - слои карты и справочники (/api/map/…) — тоже «сначала сеть»: без связи монтажник видит
//    сеть такой, какой она была при последнем открытии карты (с пометкой «нет связи»);
//  - плитки OpenStreetMap — «сначала кэш», не более MAX_TILES штук (просмотренные районы).
// Остальные запросы к API (изменения, поиск, карточки, файлы) не кэшируются: изменения
// без связи не принимаются — иначе возможны конфликты правок.
// При выходе из системы кэш данных сети удаляется (данные не остаются на чужом телефоне).

const VERSION = "v1";
const SHELL = `shell-${VERSION}`;
const DATA = "network-data";
const TILES = "osm-tiles";
const MAX_TILES = 3000;
const PRECACHE = [
  "/", "/index.html", "/login.html", "/incidents.html", "/css/app.css",
  "/js/app.js", "/js/api.js", "/js/card.js", "/js/cluster.js", "/js/draw.js", "/js/edit.js", "/js/field.js",
  "/js/impact.js", "/js/layers.js", "/js/locate.js", "/js/login.js", "/js/overlay-layer.js", "/js/pwa.js",
  "/js/incidents.js",
  "/vendor/leaflet/leaflet.css", "/vendor/leaflet/leaflet.js",
  "/manifest.webmanifest", "/img/icons/icon-192.png", "/img/icons/favicon-32.png",
];

self.addEventListener("install", (event) => {
  // allSettled: отсутствие одного файла не должно срывать установку
  event.waitUntil(caches.open(SHELL).then((c) => Promise.allSettled(PRECACHE.map((u) => c.add(u)))));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith("shell-") && key !== SHELL) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

async function networkFirst(request, cacheName, mark) {
  const cache = await caches.open(cacheName);
  try {
    const resp = await fetch(request);
    if (resp.ok) cache.put(request, resp.clone());
    return resp;
  } catch (err) {
    const hit = await cache.match(request, { ignoreVary: true });
    if (!hit) {
      if (request.mode === "navigate") {
        const index = await caches.match("/index.html");
        if (index) return index;
      }
      throw err;
    }
    if (!mark) return hit;
    // помечаем ответ из кэша — страница покажет «нет связи, данные на момент …»
    const headers = new Headers(hit.headers);
    headers.set("X-From-Cache", hit.headers.get("date") || "1");
    return new Response(await hit.blob(), { status: hit.status, headers });
  }
}

let tileCount = null;

async function trimTiles(cache) {
  const keys = await cache.keys();
  tileCount = keys.length;
  if (keys.length <= MAX_TILES) return;
  const drop = keys.slice(0, keys.length - MAX_TILES);   // самые старые — в начале списка
  await Promise.all(drop.map((k) => cache.delete(k)));
  tileCount = MAX_TILES;
}

async function tileFirst(request) {
  const cache = await caches.open(TILES);
  const hit = await cache.match(request);
  if (hit) return hit;
  const resp = await fetch(request);
  if (resp.ok || resp.type === "opaque") {
    await cache.put(request, resp.clone());
    tileCount = (tileCount ?? 0) + 1;
    if (tileCount % 200 === 0 || tileCount > MAX_TILES) trimTiles(cache);
  }
  return resp;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (url.hostname.endsWith("tile.openstreetmap.org")) {
    event.respondWith(tileFirst(req));
    return;
  }
  if (url.origin !== location.origin) return;
  if (url.pathname === "/api/auth/logout") {
    event.waitUntil(caches.delete(DATA));
    return;
  }
  if (req.method !== "GET") return;
  if (url.pathname.startsWith("/api/")) {
    if (url.pathname.startsWith("/api/map/") || url.pathname === "/api/auth/me") {
      event.respondWith(networkFirst(req, DATA, true));
    }
    return;                                   // остальное API — только сеть
  }
  event.respondWith(networkFirst(req, SHELL, false));
});
