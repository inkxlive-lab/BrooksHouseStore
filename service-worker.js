const CACHE = "brookshouse-offline-v6-1";
const OFFLINE_SEARCH = "/static/offline-inventory-search.html";
const OFFLINE_CENTER = "/static/offline-center.html";
const CORE = [OFFLINE_SEARCH, OFFLINE_CENTER, "/static/store.css", "/static/pwa.js?v=6.1", "/static/offline-mode.js?v=6.1", "/manifest.webmanifest"];

async function cacheCore() {
  const cache = await caches.open(CACHE), failures = [];
  for (const url of CORE) {
    try {
      const response = await fetch(url, {cache: "reload", credentials: "same-origin"});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await cache.put(url, response);
    } catch (error) { failures.push(`${url}: ${error.message || error}`); }
  }
  if (failures.length) throw new Error(failures.join("; "));
  return true;
}

self.addEventListener("install", event => event.waitUntil(cacheCore().then(() => self.skipWaiting())));
self.addEventListener("activate", event => event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim())));
self.addEventListener("message", event => {
  if (!event.data || event.data.type !== "BROOKSHOUSE_PREPARE_OFFLINE") return;
  const reply = event.ports && event.ports[0];
  event.waitUntil(cacheCore().then(() => reply && reply.postMessage({type:"BROOKSHOUSE_OFFLINE_READY",ok:true,version:"6.1"})).catch(error => reply && reply.postMessage({type:"BROOKSHOUSE_OFFLINE_READY",ok:false,error:String(error)})));
});

self.addEventListener("fetch", event => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  if (event.request.mode === "navigate") {
    event.respondWith((async () => {
      try {
        const response = await fetch(event.request);
        if (response.ok) caches.open(CACHE).then(cache => cache.put(event.request, response.clone())).catch(() => {});
        return response;
      } catch (_) {
        const cache = await caches.open(CACHE);
        if (["/offline/inventory-search", "/inventory/search"].includes(url.pathname)) return (await cache.match(OFFLINE_SEARCH)) || Response.error();
        if (["/offline", "/dashboard"].includes(url.pathname)) return (await cache.match(OFFLINE_CENTER)) || Response.error();
        return (await cache.match(event.request)) || (await cache.match(OFFLINE_CENTER)) || Response.error();
      }
    })());
    return;
  }
  event.respondWith(caches.match(event.request, {ignoreSearch:true}).then(cached => cached || fetch(event.request).then(response => {if(response.ok)caches.open(CACHE).then(cache => cache.put(event.request,response.clone())).catch(()=>{});return response;})));
});

self.addEventListener("push", event => {let data={title:"BrooksHouse",body:"New notification",url:"/dashboard"};try{data={...data,...event.data.json()}}catch(_){if(event.data)data.body=event.data.text()}event.waitUntil(self.registration.showNotification(data.title,{body:data.body,icon:"/static/icons/icon-192.png",data:{url:data.url}}));});
self.addEventListener("notificationclick", event => {event.notification.close();event.waitUntil(clients.matchAll({type:"window",includeUncontrolled:true}).then(list=>{for(const client of list){if("focus" in client){client.navigate(event.notification.data.url);return client.focus()}}return clients.openWindow(event.notification.data.url)}));});
