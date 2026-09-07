// Estrategia: network-first.
//
// Antes era cache-first, y como el nombre del caché no se bumpeaba al cambiar
// index.html o app.js, la ventana de Electron seguía sirviendo archivos viejos
// indefinidamente (la sesión es `persist:congreso` y sobrevive reinicios).
// El servidor es localhost, así que la red siempre está disponible y el caché
// solo tiene sentido como respaldo si el backend todavía no levantó.
const CACHE = 'diana-v7';
const ASSETS = ['/', '/static/style.css', '/static/app.js'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;   // /chat y demás POST van directo a red

  e.respondWith(
    fetch(e.request)
      .then(resp => {
        // Refrescar el caché con la copia buena, para el arranque en frío.
        if (resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy)).catch(() => {});
        }
        return resp;
      })
      .catch(() => caches.match(e.request))   // sin red: servir lo último bueno
  );
});
