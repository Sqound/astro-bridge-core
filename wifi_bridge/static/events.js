/* Foreground notifications consume confirmed server events, never action clicks. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const t = (key, values) => BridgeI18n.t(key, values);
  const key = "astro-bridge.event-cursor";
  let active=false, polling=false, epoch=0, boot=null, cursor=null, initialized=false,authContext=null;
  let callbacks={}, warning="", history=[], notices=[];
  const toastEvents=new Set(), toastTimers=new Set();
  let readController=null;
  let renderedHistory="";
  function opaqueId(value) {
    if(typeof value!=="string"||!/^([0-9a-f]{32}):[1-9][0-9]{0,15}$/.test(value))return false;
    return Number.isSafeInteger(Number(value.split(":")[1]));
  }
  function savedCursor() {
    try {
      const value=JSON.parse(sessionStorage.getItem(key));
      if(!value||value.context!==authContext||!authContext){sessionStorage.removeItem(key);return null;}
      if(typeof value.boot_id!=="string"||! /^[0-9a-f]{32}$/.test(value.boot_id)||!Number.isSafeInteger(value.sequence)||value.sequence<0)return null;
      // Whitelist opaque fields; never restore response bodies, names or tokens.
      return {context:authContext,boot_id:value.boot_id,sequence:value.sequence,notified:Array.isArray(value.notified)?value.notified.filter(id=>opaqueId(id)&&id.startsWith(value.boot_id+":")&&Number(id.split(":")[1])<=value.sequence).slice(-512):[]};
    } catch (_) { return null; }
  }
  function memoryCursor() {
    return boot!==null&&Number.isSafeInteger(cursor)?{context:authContext,boot_id:boot,sequence:cursor,notified:[...toastEvents]}:savedCursor();
  }
  function saveCursor() {
    if(!authContext||boot===null||!Number.isSafeInteger(cursor))return;
    try {sessionStorage.setItem(key,JSON.stringify({context:authContext,boot_id:boot,sequence:cursor,notified:[...toastEvents].slice(-512)}));} catch (_) { /* In-memory replay still works. */ }
  }
  function message(event) {
    const values={name:event.profile_name};
    if(event.type==="connected" && event.ready)return t("{name} connected.",values);
    if(event.type==="restored" && event.ready)return t("{name} connection restored.",values);
    const messages={
      requested:"{name}: connection change requested.", disconnecting:"{name}: disconnecting…",
      disconnected:"{name} disconnected.", connecting:"{name}: connecting…",
      associated:"{name}: Wi-Fi associated; checking services.",
      failed:"{name}: connection attempt failed.", automatic_switch_deferred:"{name}: automatic switch deferred."
    };
    if(event.type==="waiting") {
      return t(event.reason==="HOME_UNAVAILABLE"?"{name}: waiting for the home network.":event.associated?
        "{name}: Wi-Fi associated; waiting for services.":"{name}: waiting for the telescope.",values);
    }
    return t(messages[event.type]||"{name}: connection status changed.",values);
  }
  function reasonText(reason) {
    const descriptions={RADIO_UNAVAILABLE:"Wi-Fi adapter unavailable.",CONFIGURATION_CONFLICT:"Network subnet conflict.",
      INVALID_TARGET:"Check the telescope IP address.",ASSOCIATION_FAILED:"Wi-Fi association failed; enabled profiles retry automatically.",
      RECONCILE_FAILED:"Network reconciliation failed. Check device status."};
    return descriptions[reason]?t(descriptions[reason]):"";
  }
  function render() {
    $("activity-count").textContent=String(history.length);
    $("activity-empty").hidden=history.length>0;
    $("activity-warning").hidden=!warning;
    const warnings={BOOT_CHANGED:"The bridge restarted. Activity and current status were resynchronized.",
      RETENTION_GAP:"Some older activity is no longer available. Current status was resynchronized.",
      CURSOR_AHEAD:"The activity cursor was reset. Current status was resynchronized.",
      HISTORY_UNAVAILABLE:"Earlier activity could not be loaded. New events are still available.",
      VOLATILE:"Activity is temporarily held in memory because local storage is unavailable.",
      UNREACHABLE:"Activity is temporarily unavailable. Reconnecting automatically…"};
    const text=warning?t(warnings[warning]||warnings.RETENTION_GAP):"";
    if($("activity-warning").textContent!==text)$("activity-warning").textContent=text;
    const signature=BridgeI18n.locale+":"+history.map(event=>event.boot_id+":"+event.sequence).join(",");
    if(signature===renderedHistory)return;renderedHistory=signature;
    const list=history.slice().reverse().map(event=>{
      const item=document.createElement("li");item.dataset.eventId=event.boot_id+":"+event.sequence;
      const description=document.createElement("span");description.textContent=message(event);
      const detail=reasonText(event.reason);if(detail){const small=document.createElement("small");small.textContent=detail;description.append(small);}
      const sequence=document.createElement("small");sequence.className="event-sequence";sequence.textContent="#"+event.sequence;
      sequence.title=event.timestamp+" · "+t("Device time unverified");item.append(description,sequence);return item;
    });
    const scroll=$("activity-list").scrollTop;$("activity-list").replaceChildren(...list);$("activity-list").scrollTop=scroll;
  }
  function merge(events, reset=false) {
    const known=new Map((reset?[]:history).map(event=>[event.boot_id+":"+event.sequence,event]));
    for(const event of events)known.set(event.boot_id+":"+event.sequence,event);
    history=[...known.values()].sort((a,b)=>a.sequence-b.sequence).slice(-50);
  }
  function renderNotices() {
    const region=$("connection-notifications");
    for(const child of [...region.children])if(!notices.some(notice=>notice.node===child))child.remove();
    for(const notice of notices){
      if(!notice.node){
        notice.node=document.createElement("div");notice.node.className="connection-notice "+notice.kind;notice.node.dataset.operationId=notice.operation;
        notice.content=document.createElement("span");notice.close=document.createElement("button");notice.close.type="button";notice.close.className="text-button";notice.close.textContent="×";
        notice.close.addEventListener("click",()=>{notices=notices.filter(n=>n!==notice);renderNotices();});
        notice.node.append(notice.content,notice.close);region.append(notice.node);
      }
      const text=notice.events.map(message).join(" ");if(notice.content.textContent!==text)notice.content.textContent=text;
      notice.close.setAttribute("aria-label",t("Dismiss notification"));
    }
  }
  function notify(events) {
    const groups=new Map();
    for(const event of events) {
      const identity=event.boot_id+":"+event.sequence;
      if(toastEvents.has(identity))continue;toastEvents.add(identity);
      if(!groups.has(event.operation_id))groups.set(event.operation_id,[]);groups.get(event.operation_id).push(event);
    }
    // Limit deduplication memory independently of the retained recent panel.
    while(toastEvents.size>512)toastEvents.delete(toastEvents.values().next().value);
    for(const [operation,changes] of groups) {
      const ready=changes.filter(e=>["connected","restored"].includes(e.type)&&e.ready);
      const failed=changes.filter(e=>e.type==="failed");
      const disconnected=changes.filter(e=>e.type==="disconnected");
      const waiting=changes.filter(e=>e.type==="waiting");
      const selected=ready.length?[...history.filter(e=>e.operation_id===operation&&e.type==="disconnected"),...ready]:failed.length?failed:disconnected.length?disconnected:waiting;
      if(!selected.length)continue;
      const notice={operation,events:selected,kind:ready.length?"ready":failed.length?"failed":"pending"};
      notices=notices.filter(n=>n.operation!==operation&&!n.events.some(old=>selected.some(event=>event.profile_id===old.profile_id)));notices.push(notice);notices=notices.slice(-3);renderNotices();
      const timer=setTimeout(()=>{toastTimers.delete(timer);notices=notices.filter(n=>n!==notice);renderNotices();},12000);toastTimers.add(timer);
    }
  }
  async function read(path,generation,signal) {
    const response=await fetch(path,{cache:"no-store",credentials:"same-origin",signal});
    if(!active||epoch!==generation)throw new Error("STALE_CONTEXT");
    if(response.status===401){callbacks.unauthorized?.();throw new Error("UNAUTHORIZED");}
    if(!response.ok)throw new Error("EVENTS_UNAVAILABLE");return response.json();
  }
  async function refresh() {
    if(!active||polling)return;polling=true;const generation=epoch,request=new AbortController();readController=request;
    try {
      if(!initialized) {
        const saved=memoryCursor();
        const snapshot=await read("/api/events?limit=50",generation,request.signal);if(!active||epoch!==generation)return;
        boot=snapshot.boot_id;cursor=snapshot.next_cursor;merge(snapshot.events,true);
        warning=snapshot.reset_reason||(!snapshot.persistent?"VOLATILE":"");
        if(saved && saved.boot_id===boot && saved.sequence<=snapshot.latest_sequence){cursor=saved.sequence;toastEvents.clear();for(const id of saved.notified)toastEvents.add(id);}
        else if(saved){toastEvents.clear();warning=saved.boot_id!==boot?"BOOT_CHANGED":"CURSOR_AHEAD";}
        initialized=true;render();
      }
      // Each request is short and bounded. Three pages cover the 256-event journal.
      for(let page=0;page<3;page++) {
        const result=await read("/api/events?after="+cursor+"&boot_id="+encodeURIComponent(boot)+"&limit=100",generation,request.signal);
        if(!active||epoch!==generation)return;
        const reset=result.gap||result.boot_id!==boot;
        if(reset||result.events.some(event=>["connected","restored","disconnected","failed"].includes(event.type))){
          const synchronized=await callbacks.resync?.();
          if(!active||epoch!==generation)return;
          if(synchronized===false)throw new Error("STATUS_UNAVAILABLE");
        }
        merge(result.events,reset);boot=result.boot_id;cursor=result.next_cursor;
        if(reset){warning=result.reset_reason||"BOOT_CHANGED";}
        else{if(warning==="UNREACHABLE"||(warning==="VOLATILE"&&result.persistent))warning="";notify(result.events);}
        if(!result.persistent)warning="VOLATILE";
        saveCursor();render();if(!result.has_more)break;
      }
    } catch (_) {
      if(active && epoch===generation){warning="UNREACHABLE";render();}
    } finally {if(epoch===generation){polling=false;readController=null;}}
  }
  function suspend() {
    saveCursor();active=false;polling=false;epoch++;initialized=false;
    readController?.abort();readController=null;
    for(const timer of toastTimers)clearTimeout(timer);toastTimers.clear();
    history=[];notices=[];warning="";renderedHistory="";render();renderNotices();
  }
  window.BridgeEvents={
    configure(value){callbacks=value;},
    start(context){
      const next=typeof context==="string"&&/^[0-9a-f]{64}$/.test(context)?context:null;
      if(!next||(authContext!==null&&next!==authContext))this.reset();
      authContext=next;if(active)return;active=true;refresh();
    },
    suspend,
    reset(){
      suspend();boot=null;cursor=null;authContext=null;toastEvents.clear();
      try{sessionStorage.removeItem(key);}catch(_){}render();renderNotices();
    },
    refresh
  };
  document.addEventListener("bridge:localechange",()=>{render();renderNotices();});
  setInterval(refresh,3000);
})();
