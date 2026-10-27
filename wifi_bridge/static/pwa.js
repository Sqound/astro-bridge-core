/* Public installation/lifecycle state. No session, private status or API cache. */
(() => {
  "use strict";
  const $=id=>document.getElementById(id),t=(key,values)=>BridgeI18n.t(key,values);
  const revision=document.querySelector('meta[name="bridge-shell-revision"]').content;
  let callbacks={},registration=null,prompt=null,connection="",changed=false,failed=false,prepared=false;
  const supported=window.isSecureContext&&"serviceWorker" in navigator;
  const development=["localhost","127.0.0.1","[::1]"].includes(location.hostname);
  function render(){
    const secure=location.protocol==="https:"&&window.isSecureContext;
    const ios=/iPad|iPhone|iPod/.test(navigator.userAgent)||(navigator.platform==="MacIntel"&&navigator.maxTouchPoints>1);
    const installed=matchMedia("(display-mode: standalone)").matches||navigator.standalone===true;
    const needsSetup=!secure||(ios&&!installed);
    $("pwa-prerequisite").hidden=!needsSetup;$("pwa-tools").hidden=needsSetup;
    $("pwa-context").textContent=t(!secure?"Set up trusted HTTPS to install the app and enable notifications.":"Add the HTTPS app to your Home Screen to enable notifications.");
    $("pwa-status").hidden=needsSetup||!failed&&supported;
    $("pwa-status").textContent=t(failed?"Offline support could not be prepared. The online interface remains available.":"This browser does not support offline installation. The online interface remains available.");
    $("pwa-install").hidden=!prompt||!supported||!secure||installed;
    const messages={checking:t("Checking the bridge. Actions stay disabled until fresh status is received."),
      uncertain:t("Connection check failed. Displayed data is unverified; actions are disabled. Your open draft is preserved."),
      offline:t("Bridge unavailable. Private data and unsaved drafts were cleared. Reconnect to load fresh status."),
      changed:t("Application files changed. Private data and unsaved drafts were cleared. Reload before continuing.")};
    $("connection-state").hidden=!connection||connection==="login";
    const message=messages[connection]||"";if($("connection-message").textContent!==message)$("connection-message").textContent=message;
    $("connection-retry").hidden=changed||!["uncertain","offline"].includes(connection);$("shell-reload").hidden=!changed;
  }
  function shell(value){if(value&&value!==revision&&!changed){changed=true;connection="changed";callbacks.invalidate?.("changed");render();}}
  async function publicRevision(){
    try{
      const response=await fetch("/manifest.webmanifest",{cache:"no-store",credentials:"omit"});
      if(response.ok)shell(response.headers.get("X-Bridge-Shell")||"removed");
    }catch(_){/* Fresh authenticated status still gates every action. */}
  }
  async function update(){if(registration)try{await registration.update();}catch(_){} }
  async function prepare(){
    if(!supported){render();return;}
    try{
      registration=await navigator.serviceWorker.register("/sw.js",{scope:"/",updateViaCache:"none"});
      const installing=registration.installing;
      installing?.addEventListener("statechange",()=>{if(installing.state==="redundant"&&!prepared){failed=true;render();}});
      navigator.serviceWorker.controller?.postMessage({type:"BRIDGE_REVISION"});render();
      await navigator.serviceWorker.ready;prepared=true;render();
    }catch(_){failed=true;render();}
  }
  window.BridgePWA={
    configure(value){callbacks=value;},
    connection(value){if(!changed){connection=value;render();}},
    checkRevision:shell,
    get changed(){return changed;},
    get revision(){return revision;}
  };
  window.addEventListener("beforeinstallprompt",event=>{event.preventDefault();prompt=event;render();});
  window.addEventListener("appinstalled",()=>{prompt=null;render();});
  $("pwa-install").addEventListener("click",async()=>{const event=prompt;if(!event)return;prompt=null;await event.prompt();await event.userChoice;render();});
  $("connection-retry").addEventListener("click",()=>callbacks.validate?.());
  $("shell-reload").addEventListener("click",()=>location.reload());
  if("serviceWorker" in navigator){
    navigator.serviceWorker.addEventListener("message",event=>{if(event.data?.type==="BRIDGE_SHELL"&&event.data.revision!==revision){callbacks.validate?.();publicRevision();}if(event.data?.type==="BRIDGE_SHELL_REMOVED")shell("removed");});
    navigator.serviceWorker.addEventListener("controllerchange",()=>navigator.serviceWorker.controller?.postMessage({type:"BRIDGE_REVISION"}));
  }
  window.addEventListener("offline",()=>{connection="offline";callbacks.invalidate?.("offline");render();});
  window.addEventListener("online",()=>{callbacks.validate?.();update();});
  window.addEventListener("pageshow",event=>{if(event.persisted)callbacks.validate?.();});
  document.addEventListener("visibilitychange",()=>{if(document.visibilityState==="visible"){callbacks.validate?.();update();}});
  document.addEventListener("bridge:localechange",render);
  prepare();render();
})();
