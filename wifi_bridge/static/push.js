/* Explicit per-device Web Push consent. No bearer, private status or subscription in storage. */
(() => {
  "use strict";
  const $=id=>document.getElementById(id),t=key=>BridgeI18n.t(key);
  const categories=["detected","connections","switching","hotspot","mode","updates","boot","failures"];
  const choices=()=>Object.fromEntries(categories.map(key=>[key,$("push-category-"+key).checked]));
  const setChoices=value=>{for(const key of categories)$("push-category-"+key).checked=value?.[key]??true;};
  let api=null,active=false,epoch=0,registration=null,subscription=null,value=null,busy=false,subscribed=false,feedback="";
  const standalone=()=>matchMedia("(display-mode: standalone)").matches||navigator.standalone===true;
  const ios=()=>/iPad|iPhone|iPod/.test(navigator.userAgent)||(navigator.platform==="MacIntel"&&navigator.maxTouchPoints>1);
  const supported=()=>window.isSecureContext&&"PushManager" in window&&"Notification" in window&&"serviceWorker" in navigator;
  function render(){
    let message=!window.isSecureContext?"Open trusted HTTPS to set up notifications.":ios()&&!standalone()?"On iPhone, add this HTTPS page to the Home Screen, then open its icon.":!supported()?"Push notifications are unavailable in this browser.":!value?.available?"Notification support is not installed on this bridge.":Notification.permission==="denied"?"Notifications are blocked. Allow them in this device’s notification settings.":subscribed?"Notifications are enabled on this device.":"Notifications are off on this device.";
    $("push-state").textContent=t(message);
    $("push-enable").hidden=subscribed;$("push-disable").hidden=!subscribed;$("push-test").hidden=!subscribed;
    $("push-enable").disabled=!active||busy||!supported()||!value?.public_key||!registration||Notification.permission==="denied"||(ios()&&!standalone());
    $("push-disable").disabled=$("push-test").disabled=!active||busy;
    $("push-preferences").disabled=!active||busy||!subscribed;
    $("push-feedback").textContent=feedback?t(feedback):"";
  }
  function reset(){active=false;epoch++;registration=null;subscription=null;value=null;subscribed=false;busy=false;feedback="";setChoices(null);render();}
  async function start(){
    if(active)return;if(!value)setChoices(null);active=true;const generation=++epoch;
    try{
      value=await api("/api/push");if(!active||generation!==epoch)return;
      if(supported()&&value.available&&(!ios()||standalone())){
        const ready=await navigator.serviceWorker.ready;if(!active||generation!==epoch)return;registration=ready;
        const state=await api("/api/push",{action:"prepare"});if(!active||generation!==epoch)return;value=state;
        const sub=await registration.pushManager.getSubscription();if(!active||generation!==epoch)return;subscription=sub;
        if(subscription){const result=await api("/api/push",{action:"state",subscription:subscription.toJSON()});if(!active||generation!==epoch)return;subscribed=result.subscribed;setChoices(result.categories);}
      }
    }catch(_){if(active&&generation===epoch)active=false;/* Retry after the next fresh authenticated status, including post-OTA HTTPS diagnostics. */}
    if(active&&generation===epoch)render();
  }
  function key(value){const raw=atob(value.replace(/-/g,"+").replace(/_/g,"/"));return Uint8Array.from(raw,c=>c.charCodeAt(0));}
  $("push-enable").addEventListener("click",async()=>{
    if(!active||busy||!registration||!value?.public_key)return;
    const generation=epoch;busy=true;feedback="";render();
    let fresh=null;
    try{
      // This must be the first asynchronous call, directly from the user gesture.
      const permission=await Notification.requestPermission();if(!active||generation!==epoch)return;
      if(permission!=="granted"){feedback="Notifications are blocked. Allow them in this device’s notification settings.";return;}
      fresh=await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:key(value.public_key)});
      if(!active||generation!==epoch){await fresh.unsubscribe();return;}
      await api("/api/push",{action:"subscribe",subscription:fresh.toJSON(),locale:BridgeI18n.locale,categories:choices()});
      if(!active||generation!==epoch){await fresh.unsubscribe();return;}
      subscription=fresh;subscribed=true;
    }catch(_){if(fresh&&!subscribed)await fresh.unsubscribe().catch(()=>{});if(active&&generation===epoch)feedback="Notification setup failed. Check HTTPS trust and Internet access, then try again.";}
    finally{if(active&&generation===epoch){busy=false;render();}}
  });
  $("push-disable").addEventListener("click",async()=>{
    if(!subscription||busy)return;const generation=epoch;busy=true;feedback="";render();
    try{await api("/api/push",{action:"unsubscribe",subscription:subscription.toJSON()});await subscription.unsubscribe();if(active&&generation===epoch){subscription=null;subscribed=false;}}
    catch(_){if(active&&generation===epoch)feedback="Notification setup failed. Check HTTPS trust and Internet access, then try again.";}
    finally{if(active&&generation===epoch){busy=false;render();}}
  });
  $("push-preferences").addEventListener("click",async()=>{
    if(!subscription||busy||!active)return;const generation=epoch;busy=true;feedback="";const selected=choices();render();
    try{await api("/api/push",{action:"preferences",subscription:subscription.toJSON(),categories:selected});if(active&&generation===epoch)feedback="Notification choices saved.";}
    catch(_){if(active&&generation===epoch)feedback="Notification setup failed. Check HTTPS trust and Internet access, then try again.";}
    finally{if(active&&generation===epoch){busy=false;render();}}
  });
  $("push-test").addEventListener("click",async()=>{
    if(!subscription||busy)return;const generation=epoch;busy=true;render();
    try{await api("/api/push",{action:"test",subscription:subscription.toJSON()});if(active&&generation===epoch)feedback="Test queued. Check this device; provider acceptance does not confirm receipt.";}
    catch(error){if(active&&generation===epoch)feedback=error.code==="PUSH_RATE_LIMITED"?"Wait before sending another test notification.":"Notification setup failed. Check HTTPS trust and Internet access, then try again.";}
    finally{if(active&&generation===epoch){busy=false;render();}}
  });
  window.BridgePush={configure(options){api=options.api;},start,suspend(){active=false;epoch++;busy=false;},reset};
  document.addEventListener("bridge:localechange",()=>{render();if(active&&subscribed&&subscription)api("/api/push",{action:"subscribe",subscription:subscription.toJSON(),locale:BridgeI18n.locale}).catch(()=>{});});
  setChoices(null);render();
})();
