/* REVISION and ASSETS are supplied from fixed public bytes by the server. */
"use strict";
const PREFIX="astro-bridge-public-", CACHE=PREFIX+REVISION;
const allowed=new Set(ASSETS);
async function publicFetch(url){
  const response=await fetch(new Request(url,{cache:"no-store",credentials:"omit",redirect:"error"}));
  if(!response.ok||response.headers.get("X-Bridge-Shell")!==REVISION)throw new Error("SHELL_CHANGED");
  return response;
}
self.addEventListener("install",event=>event.waitUntil((async()=>{
  const cache=await caches.open(CACHE);
  try{
    for(const path of ASSETS)await cache.put(path,await publicFetch(path));
    await self.skipWaiting();
  }catch(error){await caches.delete(CACHE);throw error;}
})()));
self.addEventListener("activate",event=>event.waitUntil((async()=>{
  for(const key of await caches.keys())if(key.startsWith(PREFIX)&&key!==CACHE)await caches.delete(key);
  await self.clients.claim();
  for(const client of await self.clients.matchAll({type:"window"}))client.postMessage({type:"BRIDGE_SHELL",revision:REVISION});
})()));
self.addEventListener("message",event=>{
  if(event.data?.type==="BRIDGE_REVISION")event.source?.postMessage({type:"BRIDGE_SHELL",revision:REVISION});
});
self.addEventListener("fetch",event=>{
  const request=event.request,url=new URL(request.url);
  // No API, credentials, queries, cross-origin, mutations or arbitrary navigation
  // enters CacheStorage. The browser's normal network path handles them.
  if(request.method!=="GET"||url.origin!==self.location.origin||url.search||!allowed.has(url.pathname))return;
  event.respondWith((async()=>{
    try{
      // Network-first serves new/rolled-back bytes immediately. Only this exact
      // coherent revision can update this worker's fallback cache.
      const response=await fetch(new Request(url.href,{cache:"no-store",credentials:"omit",redirect:"error"}));
      if(response.ok){
        if(!response.headers.has("X-Bridge-Shell")){
          // A rollback to a server predating PWA support must not retain this
          // newer shell forever. Return its live bytes and retire our cache.
          for(const key of await caches.keys())if(key.startsWith(PREFIX))await caches.delete(key);
          await self.registration.unregister();
          for(const client of await self.clients.matchAll({type:"window"}))client.postMessage({type:"BRIDGE_SHELL_REMOVED"});
        }
        if(response.headers.get("X-Bridge-Shell")===REVISION){const cache=await caches.open(CACHE);await cache.put(url.pathname,response.clone());}
        return response;
      }
      return response;
    }catch(_){return (await caches.open(CACHE)).match(url.pathname);}
  })());
});
// Only fixed internal destinations; never trust a URL supplied by a push payload.
function notificationPath(value){
  if(typeof value!=="string"||value.length>160)return "/#telescopes";
  try{
    const url=new URL(value,self.location.origin);
    if(url.origin!==self.location.origin||url.pathname!=="/"||url.search||url.username||url.password)return "/#telescopes";
    if(/^#telescopes(?:\?profile=[a-z0-9][a-z0-9-]{0,31})?$/.test(url.hash)||
       /^#networks\?section=(?:hotspot|connection-mode)$/.test(url.hash)||
       url.hash==="#configuration/general?section=updates"||
       /^#configuration\/telescopes\?add=[a-f0-9]{32}$/.test(url.hash))return "/"+url.hash;
  }catch(_){}
  return "/#telescopes";
}
// Encrypted Web Push needs no API request, token or private CacheStorage entry.
self.addEventListener("push",event=>event.waitUntil((async()=>{
  let data=null;
  try{const text=event.data?.text()||"";if(text.length<=4096)data=JSON.parse(text);}catch(_){}
  const now=Date.now()/1000;
  // Every userVisibleOnly push must show a notification, including unusable
  // payloads. Never replay their private text or destination in this fallback.
  if(!data||typeof data!=="object"||!Number.isFinite(data.expires)||data.expires<now||data.expires>now+120||typeof data.body!=="string"){
    await self.registration.showNotification("Astro Bridge",{
      body:"Open Astro Bridge to check its current status.",tag:"astro-bridge-status",
      icon:"/icon-192.png",badge:"/icon-192.png",data:{path:"/#telescopes",expires:now+30}
    });
    return;
  }
  const categories=["detected","connections","switching","hotspot","mode","updates","boot","failures","test"];
  const category=categories.includes(data.category)?data.category:"connections";
  const tag=typeof data.tag==="string"&&/^astro-bridge-[a-z]+-[a-f0-9]{16}$/.test(data.tag)?data.tag:"astro-bridge-"+category;
  await self.registration.showNotification("Astro Bridge",{
    body:data.body.slice(0,800),tag,icon:"/icon-192.png",badge:"/icon-192.png",
    data:{path:notificationPath(data.path),expires:data.expires}
  });
})()));
self.addEventListener("notificationclick",event=>{
  event.notification.close();event.waitUntil((async()=>{
    const data=event.notification.data;
    // A displayed notice keeps its allowlisted destination. Only the app
    // authenticates and loads current state; old notice contents are never replayed.
    const path=notificationPath(data?.path);
    const windows=await self.clients.matchAll({type:"window",includeUncontrolled:true});
    const existing=windows.find(client=>{try{return new URL(client.url).origin===self.location.origin;}catch(_){return false;}});
    if(existing){await existing.navigate(path);await existing.focus();}
    else await self.clients.openWindow(path);
  })());
});
