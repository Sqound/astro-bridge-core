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
  let renderedHistory="", renderedPreview="", readSequence=null, pageOpen=false;
  let oldestSequence=0, fullHistoryBoot=null, historyLoading=false, historyController=null;
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
      return {context:authContext,boot_id:value.boot_id,sequence:value.sequence,read_sequence:Number.isSafeInteger(value.read_sequence)&&value.read_sequence>=0&&value.read_sequence<=value.sequence?value.read_sequence:value.sequence,notified:Array.isArray(value.notified)?value.notified.filter(id=>opaqueId(id)&&id.startsWith(value.boot_id+":")&&Number(id.split(":")[1])<=value.sequence).slice(-512):[]};
    } catch (_) { return null; }
  }
  function memoryCursor() {
    return boot!==null&&Number.isSafeInteger(cursor)?{context:authContext,boot_id:boot,sequence:cursor,read_sequence:readSequence,notified:[...toastEvents]}:savedCursor();
  }
  function saveCursor() {
    if(!authContext||boot===null||!Number.isSafeInteger(cursor))return;
    try {sessionStorage.setItem(key,JSON.stringify({context:authContext,boot_id:boot,sequence:cursor,read_sequence:readSequence,notified:[...toastEvents].slice(-512)}));} catch (_) { /* In-memory replay still works. */ }
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
      WIFI_AUTHENTICATION_FAILED:"Wi-Fi authentication failed. Check the password. Retries resume after you change it or reboot the bridge.",INVALID_TARGET:"Check the telescope IP address.",ASSOCIATION_FAILED:"Wi-Fi association failed; enabled profiles retry automatically.",
      RECONCILE_FAILED:"Network reconciliation failed. Check device status."};
    return descriptions[reason]?t(descriptions[reason]):"";
  }
  function closePopover(focus=false) {
    $("activity-popover").hidden=true;$("activity-toggle").setAttribute("aria-expanded","false");
    if(focus&&active)$("activity-toggle").focus();
  }
  function markRead() {
    if(!active||!Number.isSafeInteger(cursor))return;
    readSequence=cursor;saveCursor();
  }
  function eventItem(event) {
    const item=document.createElement("li");item.dataset.eventId=event.boot_id+":"+event.sequence;
    const description=document.createElement("span");description.textContent=message(event);
    const detail=reasonText(event.reason);if(detail){const small=document.createElement("small");small.textContent=detail;description.append(small);}
    const sequence=document.createElement("small");sequence.className="event-sequence";sequence.textContent="#"+event.sequence;
    sequence.title=event.timestamp+" · "+t("Device time unverified");item.append(description,sequence);return item;
  }
  function renderList(id,events) {
    const list=$(id),scroll=list.scrollTop;list.replaceChildren(...events.map(eventItem));list.scrollTop=scroll;
  }
  function render() {
    $("activity-menu").hidden=!active;
    if(active&&(pageOpen||!$("activity-popover").hidden))markRead();
    const unread=active&&readSequence!==null?history.filter(e=>e.boot_id===boot&&e.sequence>readSequence).length:0;
    const badge=$("activity-count");badge.textContent=unread>99?"99+":String(unread);badge.hidden=unread===0;
    const label=unread?t("{count} unread events",{count:unread}):t("Activity");
    $("activity-toggle").setAttribute("aria-label",label);
    $("activity-toggle").setAttribute("aria-current",pageOpen?"page":"false");
    const announcement=unread?label:"";
    if($("activity-announcement").textContent!==announcement)$("activity-announcement").textContent=announcement;
    $("activity-empty").hidden=history.length>0;$("activity-preview-empty").hidden=history.length>0;
    const warnings={BOOT_CHANGED:"The bridge restarted. Activity and current status were resynchronized.",
      RETENTION_GAP:"Some older activity is no longer available. Current status was resynchronized.",
      CURSOR_AHEAD:"The activity cursor was reset. Current status was resynchronized.",
      HISTORY_UNAVAILABLE:"Earlier activity could not be loaded. New events are still available.",
      VOLATILE:"Activity is temporarily held in memory because local storage is unavailable.",
      UNREACHABLE:"Activity is temporarily unavailable. Reconnecting automatically…"};
    const text=warning?t(warnings[warning]||warnings.RETENTION_GAP):"";
    for(const id of ["activity-warning","activity-preview-warning"]){$(id).hidden=!warning;if($(id).textContent!==text)$(id).textContent=text;}
    const ordered=history.slice().reverse(),preview=ordered.slice(0,10);
    const signature=BridgeI18n.locale+":"+history.map(e=>e.boot_id+":"+e.sequence).join(",");
    const previewSignature=BridgeI18n.locale+":"+preview.map(e=>e.boot_id+":"+e.sequence).join(",");
    if(signature!==renderedHistory){renderedHistory=signature;renderList("activity-list",ordered);}
    if(previewSignature!==renderedPreview){renderedPreview=previewSignature;renderList("activity-preview-list",preview);}
  }
  async function loadHistory() {
    if(!active||!pageOpen||!initialized||historyLoading||fullHistoryBoot===boot)return;
    historyLoading=true;const generation=epoch,selectedBoot=boot,target=cursor,request=new AbortController();historyController=request;
    try {
      let after=Math.max(0,oldestSequence-1);const events=[];
      for(let page=0;page<3;page++) {
        const result=await read("/api/events?after="+after+"&boot_id="+encodeURIComponent(selectedBoot)+"&limit=100",generation,request.signal);
        if(!active||epoch!==generation)return;
        if(result.gap||result.boot_id!==selectedBoot){warning=result.reset_reason||"BOOT_CHANGED";return;}
        events.push(...result.events.filter(e=>e.sequence<=target));after=result.next_cursor;
        if(!result.has_more||after>=target)break;
      }
      merge(events);fullHistoryBoot=selectedBoot;render();
    } catch (_) {if(active&&epoch===generation){warning="UNREACHABLE";render();}}
    finally {if(epoch===generation){historyLoading=false;historyController=null;}}
  }
  $("activity-toggle").addEventListener("click",()=>{
    if(!active)return;
    const opening=$("activity-popover").hidden;closePopover();
    if(opening){$("user-menu").open=false;$("activity-popover").hidden=false;$("activity-toggle").setAttribute("aria-expanded","true");markRead();render();}
  });
  $("activity-more").addEventListener("click",()=>closePopover());
  document.addEventListener("click",event=>{if(!$("activity-menu").contains(event.target))closePopover();});
  document.addEventListener("keydown",event=>{if(event.key==="Escape"&&!$("activity-popover").hidden){event.preventDefault();closePopover(true);}});
  $("activity-menu").addEventListener("focusout",event=>{if(event.relatedTarget&&!$("activity-menu").contains(event.relatedTarget))closePopover();});
  function merge(events, reset=false) {
    const known=new Map((reset?[]:history).map(event=>[event.boot_id+":"+event.sequence,event]));
    for(const event of events)known.set(event.boot_id+":"+event.sequence,event);
    history=[...known.values()].sort((a,b)=>a.sequence-b.sequence).slice(-256);
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
        boot=snapshot.boot_id;cursor=snapshot.next_cursor;oldestSequence=snapshot.oldest_sequence;fullHistoryBoot=null;readSequence=snapshot.latest_sequence;merge(snapshot.events,true);
        warning=snapshot.reset_reason||(!snapshot.persistent?"VOLATILE":"");
        if(saved && saved.boot_id===boot && saved.sequence<=snapshot.latest_sequence){cursor=saved.sequence;readSequence=saved.read_sequence;toastEvents.clear();for(const id of saved.notified)toastEvents.add(id);}
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
        merge(result.events,reset);boot=result.boot_id;cursor=result.next_cursor;oldestSequence=result.oldest_sequence;
        if(reset){readSequence=cursor;fullHistoryBoot=null;}
        if(reset){warning=result.reset_reason||"BOOT_CHANGED";}
        else{if(warning==="UNREACHABLE"||(warning==="VOLATILE"&&result.persistent))warning="";notify(result.events);}
        if(!result.persistent)warning="VOLATILE";
        saveCursor();render();if(!result.has_more)break;
      }
    } catch (_) {
      if(active && epoch===generation){warning="UNREACHABLE";render();}
    } finally {if(epoch===generation){polling=false;readController=null;if(pageOpen)loadHistory();}}
  }
  function suspend() {
    saveCursor();active=false;polling=false;epoch++;initialized=false;
    readController?.abort();readController=null;historyController?.abort();historyController=null;historyLoading=false;fullHistoryBoot=null;closePopover();
    for(const timer of toastTimers)clearTimeout(timer);toastTimers.clear();
    history=[];notices=[];warning="";renderedHistory="";renderedPreview="";render();renderNotices();
  }
  window.BridgeEvents={
    configure(value){callbacks=value;},
    closePopover,
    setPage(value){pageOpen=!!value;if(pageOpen){closePopover();markRead();loadHistory();}render();},
    start(context){
      const next=typeof context==="string"&&/^[0-9a-f]{64}$/.test(context)?context:null;
      if(!next||(authContext!==null&&next!==authContext))this.reset();
      authContext=next;if(active)return;active=true;render();refresh();
    },
    suspend,
    reset(){
      suspend();boot=null;cursor=null;readSequence=null;authContext=null;toastEvents.clear();
      try{sessionStorage.removeItem(key);}catch(_){}render();renderNotices();
    },
    refresh
  };
  document.addEventListener("bridge:localechange",()=>{render();renderNotices();});
  setInterval(refresh,3000);
})();
