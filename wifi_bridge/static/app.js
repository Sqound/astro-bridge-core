"use strict";
// Keep one-finger scrolling and desktop zoom; Safari may ignore viewport limits.
function preventTouchZoom(event){
  if(matchMedia("(any-pointer: coarse)").matches &&
      (event.type.startsWith("gesture") || event.touches.length>1))event.preventDefault();
}
for(const type of ["gesturestart","gesturechange","touchmove"])
  document.addEventListener(type,preventTouchZoom,{passive:false});
const $ = id => document.getElementById(id);
const t = (key, values) => BridgeI18n.t(key, values);
const instrumentKind = kind => t(pluginById(kind)?.name||"Generic");
const dynamicMessages=new Set();
let csrf = "", state = null, polling = false, editing = null, preferencesDirty = false, relocated = false;
const pendingRadios = new Set();
const routeDisclosure = new Map();
let discoveryEpoch=0, discoveryChoices=[], ensureView=null, ensurePolling=false, autoPreferencePending=false, autoPolicyDirty=false, autoPolicyRevision=null;
let nearbySignature="", instrumentSignature="", endpointInventory=null, connectionChoice=null, profileInfoOpener=null;
let sessionSelection=null,sessionRevision=null,makerSessionKeyState="absent",makerChipRefresh=0;
let makerKeyEpoch=0,makerKeyRevision=null,makerKeyTimer,makerKeyUnavailable=false;
let wizardEpoch=0,wifiSnapshot=null,wifiSnapshotAt=0,wifiRequest=null;
let feedbackTimer, scanning=false, statusRefresh=null, statusEpoch=0;
let accessReady=false, statusFailures=0, protectionDraft=null;
let hotspotNameDirty=false, hotspotNamePending=false, hotspotAdapterDirty=false;
let authenticating=false, loggedOut=false, profileInfoId=null, profileInfoSignature="";
// Help dialogs retain their own DOM and focus. Status polling never opens or rebuilds them.
for(const button of document.querySelectorAll("[data-help]"))button.addEventListener("click",()=>{
  if(!accessReady)return;
  const dialog=$(button.dataset.help);if(dialog&&!dialog.open)dialog.showModal();
});
for(const button of document.querySelectorAll("[data-close-help]"))button.addEventListener("click",()=>button.closest("dialog").close());
for(const dialog of document.querySelectorAll(".help-dialog"))dialog.addEventListener("click",event=>{
  if(event.target!==dialog)return;const bounds=dialog.getBoundingClientRect();
  if(event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom)dialog.close();
});
// Retain public leaf baselines, never a snapshot of authenticated HTML.
const publicLeaves=[...document.querySelectorAll("#dashboard [id], #protection-dialog [id], #editor [id], #move-dialog [id], #apply-notice [id], #error")]
  .filter(element=>!element.children.length&&!element.matches("input,select,textarea,button")&&!element.closest("#pwa-settings"))
  .map(element=>({element,text:element.textContent,href:element.getAttribute("href"),className:element.className}));
function privateReset(preserveEvents=false,preserveLogin=false){
  resetSessionCards();resetSessionPage();resetCameras();
  makerKeyEpoch++;clearTimeout(makerKeyTimer);makerKeyRevision=null;sessionSelection=null;sessionRevision=null;makerSessionKeyState="absent";makerChipRefresh=0;$("maker-key-file").value="";
  pairedAppsSignature="";$("paired-apps-list").replaceChildren();
  interfaceRoleDraft=null;interfaceRoleRevision=null;interfaceRoleSignature="";$("radios").replaceChildren();$("interface-role-result").textContent="";$("interface-role-warning").hidden=true;
  loggedOut=false;$("user-menu").open=false;
  for(const id of dynamicMessages)delete $(id).dataset.i18n;dynamicMessages.clear();
  autoPolicyDirty=false;autoPolicyRevision=null;$("pause-notice").hidden=true;clearProtectionDialog();$("protection-list").replaceChildren();$("protection-summary").hidden=true;
  wifiSnapshot=null;wifiSnapshotAt=0;wifiRequest=null;
  wizardEpoch++;$("discovery-results").replaceChildren();$("discovery-status").textContent="";
  discoveryEpoch++;discoveryChoices=[];ensureView=null;ensurePolling=false;$("ensure-notice").hidden=true;$("scan-results").replaceChildren();
  nearbySignature="";$("nearby-list").replaceChildren();$("nearby-telescopes").hidden=true;
  $("simple-connections").replaceChildren();$("connections-list").replaceChildren();$("connection-choices").replaceChildren();
  instrumentSignature="";endpointInventory=null;connectionChoice=null;profileInfoOpener=null;
  BridgeSecure.reset();BridgePush.reset();if(preserveEvents)BridgeEvents.suspend();else BridgeEvents.reset();csrf="";state=null;editing=null;requestedMove=null;updateState=null;preferencesDirty=false;
  pendingRadios.clear();routeDisclosure.clear();clearTimeout(feedbackTimer);
  modeDirty=false;modePending=false;hotspotNameDirty=false;hotspotNamePending=false;hotspotAdapterDirty=false;$("hotspot-name").value="";$("hotspot-interface").replaceChildren();delete $("hotspot-interface").dataset.options;$("hotspot-name-result").textContent="";
  $("connection-mode").value="private";$("hotspot-private-lan").checked=false;
  $("connection-mode-purpose").textContent="";$("connection-mode-status").textContent="";
  profileInfoId=null;profileInfoSignature="";pluginCatalog=[];pluginInfoId=null;pluginListSignature="";pluginDetailsSignature="";$("plugin-list").replaceChildren();$("plugin-info-body").replaceChildren();
  for(const {element,text,href,className} of publicLeaves){element.textContent=text;element.className=className;if(href!==null)element.setAttribute("href",href);}
  for(const input of document.querySelectorAll("#dashboard input, #protection-dialog input, #editor input")){input.value="";input.checked=false;delete input.dataset.bssid;}
  if(!preserveLogin)$("login-password").value="";
  $("network-interface").replaceChildren();$("network-password").placeholder=t("Network password");
  for(const dialog of document.querySelectorAll("dialog[open]"))dialog.close();
  $("apply-notice").hidden=true;$("error").hidden=true;$("dashboard").hidden=true;$("logout").hidden=true;
  BridgeI18n.translate();
}
function unverified(reason,clear=false){
  discoveryEpoch++;statusEpoch++;accessReady=false;$("dashboard").inert=true;
  if(clear){privateReset(reason!=="changed");$("login").hidden=false;$("login-form").inert=reason==="offline";}
  else {BridgeEvents.suspend();BridgeSecure.suspend();BridgePush.suspend();}
  BridgePWA.connection(reason);
}
function invalidateConnection(reason){unverified(reason,true);}
async function validateConnection(){
  if(BridgePWA.changed||authenticating)return false;
  unverified("checking");
  if(statusRefresh)await statusRefresh;
  return refresh();
}
const mainTabs = ["telescopes", "networks", "configuration"];
const configurationTabs = ["general", "pwa", "plugins", "telescopes", "radios", "sessions", "cameras"];
const routeScroll = new Map();
let lastConfiguration = "general";
let notificationFocusedHash="";
let currentRoute = "", hotspotPending = false, modePending=false, modeDirty=false, updatePreferencePending = false;
function navigate(destination, section=lastConfiguration) {
  const hash = "#" + destination + (destination === "configuration" ? "/" + section : "");
  if(location.hash === hash) applyNavigation(); else location.hash = hash;
}
function applyNavigation(event) {
  const raw=location.hash.slice(1).split("?"),parts=raw[0].split("/");
  const query=new URLSearchParams(raw[1]||"");
  let target="";
  if([...query.keys()].length===1){
    const profile=query.get("profile"),section=query.get("section");
    if(parts[0]==="telescopes"&&/^[a-z0-9][a-z0-9-]{0,31}$/.test(profile||""))target="?profile="+profile;
    else if(parts[0]==="networks"&&["hotspot","connection-mode"].includes(section))target="?section="+section;
    else if(parts[0]==="configuration"&&parts[1]==="general"&&section==="updates")target="?section=updates";
    else if(parts[0]==="configuration"&&parts[1]==="telescopes"&&/^[a-f0-9]{32}$/.test(query.get("add")||""))target="?add="+query.get("add");
  }
  const destination = parts[0]==="session"&&/^[a-z0-9-]{1,64}$/.test(parts[1]||"") ? "session" : [...mainTabs,"activity"].includes(parts[0]) ? parts[0] : "telescopes";
  const section = configurationTabs.includes(parts[1]) ? parts[1] : "general";
  const next = destination + (destination === "configuration" ? "/" + section : destination === "session" ? "/"+parts[1] : "");
  if(destination === "configuration")lastConfiguration=section;
  if(currentRoute && currentRoute !== next) routeScroll.set(currentRoute, window.scrollY);
  for(const name of mainTabs) {
    const selected = name === destination, button = $("nav-" + name);
    button.setAttribute("aria-selected", String(selected)); button.tabIndex = selected || (["activity","session"].includes(destination) && name === mainTabs[0]) ? 0 : -1;
    $("view-" + name).hidden = !selected;
  }
  $("view-session").hidden = destination !== "session";
  $("view-activity").hidden = destination !== "activity";
  if(destination === "activity" && currentRoute && currentRoute !== "activity")$("activity-back").href="#"+currentRoute;
  BridgeEvents.closePopover();BridgeEvents.setPage(destination === "activity");
  for(const name of configurationTabs) {
    const selected = name === section, button = $("tab-" + name);
    button.setAttribute("aria-selected", String(selected)); button.tabIndex = selected ? 0 : -1;
    $("panel-" + name).hidden = !selected;
  }
  if(location.hash !== "#" + next+target) history.replaceState(null, "", "#" + next+target);
  if(notificationFocusedHash!==location.hash||event?.type==="hashchange")notificationFocusedHash="";
  if(currentRoute !== next) requestAnimationFrame(() => window.scrollTo(0, routeScroll.get(next) || 0));
  currentRoute = next;
  syncSessionPage();
  focusNotificationTarget();
}
function focusNotificationTarget(){
  if(!state||!accessReady)return;
  const [page,search]=location.hash.slice(1).split("?"),params=new URLSearchParams(search||"");
  const profile=params.get("profile"),section=params.get("section");
  const observation=params.get("add");
  if(page==="configuration/telescopes"&&/^[a-f0-9]{32}$/.test(observation||"")){
    if(notificationFocusedHash!==location.hash){notificationFocusedHash=location.hash;openDetectedTelescope(observation);}return;
  }
  let element=null;
  if(page==="telescopes"&&/^[a-z0-9][a-z0-9-]{0,31}$/.test(profile||"")&&state.config.telescopes.some(p=>p.id===profile)){
    element=[...document.querySelectorAll("#simple-connections [data-profile-id]")].find(e=>e.dataset.profileId===profile);
  }else if(page==="networks")element=section==="hotspot"?$("simple-ap-url").closest("section"):section==="connection-mode"?$("connection-mode-form"):null;
  else if(page==="configuration/general"&&section==="updates")element=$("update-heading");
  if(!element||notificationFocusedHash===location.hash)return;
  const destination=location.hash;notificationFocusedHash=destination;element.setAttribute("tabindex","-1");
  requestAnimationFrame(()=>{if(state&&accessReady&&location.hash===destination&&element.isConnected){element.scrollIntoView({block:"center",behavior:"auto"});element.focus({preventScroll:true});}});
}
function selectConfiguration(tab, focus=false) {
  navigate("configuration", tab);
  if(focus) $("tab-" + tab).focus();
}
function tabKeyboard(event, names, name, select, prefix) {
  if(!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const index = event.key === "Home" ? 0 : event.key === "End" ? names.length - 1 :
    (names.indexOf(name) + (event.key === "ArrowRight" ? 1 : -1) + names.length) % names.length;
  select(names[index]); $(prefix + names[index]).focus();
}
// Header preferences remain available before sign-in and do not replace page content.
document.addEventListener("click",event=>{
  if(!$("user-menu").contains(event.target))$("user-menu").open=false;
});
document.addEventListener("keydown",event=>{
  if(event.key==="Escape"&&$("user-menu").open){$("locale-menu").open=false;$("user-menu").open=false;$("user-menu").querySelector("summary").focus();}
});
$("user-menu").addEventListener("toggle",()=>{if($("user-menu").open)BridgeEvents.closePopover();});
$("user-menu").addEventListener("focusout",event=>{if(event.relatedTarget&&!$("user-menu").contains(event.relatedTarget))$("user-menu").open=false;});
window.addEventListener("hashchange", applyNavigation);
if("scrollRestoration" in history) history.scrollRestoration = "manual";
applyNavigation();
function adapterLabel(device) {
  if(!device.usb) return t("Built-in Wi-Fi");
  const index = state.devices.filter(d => d.usb).findIndex(d => d.mac === device.mac);
  return t("USB adapter {letter}", {letter:String.fromCharCode(65 + index)});
}
function connectionLabel(group) {
  // Names the bridge's own Wi-Fi adapter that carries the link, never how the instrument is plugged.
  return group.device ? group.device.usb ? t("Wi-Fi adapter {letter}", {
    letter:String.fromCharCode(65 + state.devices.filter(d => d.usb).findIndex(d => d.mac === group.device.mac))
  }) : t("Built-in Wi-Fi") : t("Unavailable connection {number}", {number:group.number});
}

function simpleStatus(profile,live,s,available){
  if(live.reason?.startsWith("ISOLATION_"))return t("Isolated connection unavailable. Check namespace support, Wi-Fi driver and host tools.");
  if(live.retry_blocked)return t("Wi-Fi authentication failed. Check the password. Retries resume after you change it or reboot the bridge.");
  if(!available)return t("Connection unavailable");
  if(live.state==="conflict")return t("Routing conflict · forwarding suspended");
  if(live.connected&&!live.relay_ready)return t("Wi-Fi connected · forwarding unavailable");
  if(live.connected)return live.state==="waiting_service"?t("Connected · instrument service starting…"):t("Connected");
  if(!profile.enabled)return t("Ready to connect");
  if(["conflict","invalid_ip","error"].includes(live.state))return t("Check settings in the Instruments tab");
  return t("Connecting… Check that the instrument is switched on.");
}

function connectionGuard(profile,s){
  const info=s.connection_protection||{},policy=s.config.auto_policy||{};
  const measured=s.connection_activity?.profiles?.[profile.id];
  const fresh=!!s.connection_activity?.supported&&Number.isFinite(measured?.sample_age_seconds)&&measured.sample_age_seconds>=0&&measured.sample_age_seconds<=15;
  const active=fresh&&measured.state==="active";
  const classified=fresh&&(active||measured.state==="quiet"||measured.reason==="WARMING_UP");
  const activity=t(classified&&measured.consultation==="mount_control"?"Mount control or guiding":classified&&measured.consultation==="probable_content"?"Possible content transfer":classified&&measured.consultation==="background_control"?"Background status exchanges":active?"Traffic active":fresh&&measured.state==="quiet"?"No recent traffic":"Activity not verified");
  const result=(state,label)=>({state,label,activity});
  if(profile.owner_locked||info.owner_locks?.includes(profile.id))return result("protected",t("Protected until manually released"));
  const reservations=(info.leases||[]).filter(lease=>lease.profile_id===profile.id);
  if(reservations.length)return result("protected",t("Protected for {seconds} s",{seconds:Math.max(...reservations.map(lease=>lease.remaining_seconds))}));
  if(!profile.auto_eligible||!profile.auto_replaceable)return result("protected",t("Automatic replacement disabled"));
  if(!s.config.auto_connect)return result("off",t("Automatic switching is off."));
  if(policy.force_first_profile)return result("warning",t("Active connections may be replaced"));
  if(active)return result("active",t("Automatic switching blocked while active"));
  if(fresh&&(measured.state==="quiet"||measured.reason==="WARMING_UP")&&Number.isFinite(measured.quiet_seconds)){
    const remaining=Math.max(0,(policy.quiet_seconds||120)-measured.quiet_seconds);
    if(remaining)return result("waiting",t("Waiting for inactivity: {seconds} s remaining",{seconds:Math.ceil(remaining)}));
    if(Number.isFinite(measured.cooldown_remaining)&&measured.cooldown_remaining>0)return result("waiting",t("Waiting between switches: {seconds} s",{seconds:Math.ceil(measured.cooldown_remaining)}));
    if(measured.state==="quiet"&&measured.cooldown_remaining===0)return result("eligible",t("May be reused by automatic switching"));
  }
  return result("unknown",t("Automatic switching blocked: activity unverified"));
}

function renderNearby(s){
  const found=s.nearby_telescopes?.candidates||[];
  $("nearby-telescopes").hidden=!found.length;
  const signature=JSON.stringify([BridgeI18n.locale,found]);if(signature===nearbySignature)return;nearbySignature=signature;
  const focused=$("nearby-list").contains(document.activeElement)?document.activeElement.id:null;
  $("nearby-list").replaceChildren(...found.map(candidate=>{
    const row=node("article",undefined,"nearby-telescope"),name=node("div");name.append(node("h3",candidate.ssid),node("p",instrumentKind(candidate.kind),"small"));
    const button=node("button",t("Add instrument"));button.type="button";button.id="nearby-add-"+candidate.observation_id;
    button.addEventListener("click",()=>{
      const target="#configuration/telescopes?add="+candidate.observation_id;
      if(location.hash===target){notificationFocusedHash="";applyNavigation();}else location.hash=target;
    });row.append(name,button);return row;
  }));
  if(focused)$(focused)?.focus({preventScroll:true});
}

function renderSimple(s){
  const profiles=s.config.telescopes,live=new Map(s.telescopes.map(p=>[p.id,p]));
  renderNearby(s);
  renderSimpleNetwork(s);
  renderConnectionMode(s);
  const connected=s.telescopes.filter(p=>p.connected).length;
  $("simple-summary").textContent=connected?t("Connected instruments: {count}",{count:connected}):t("No instrument connected");
  $("simple-summary").className="badge"+(connected?" online":"");
  $("simple-empty").hidden=!!profiles.length;$("simple-help").hidden=!profiles.length;$("simple-connections").hidden=!profiles.length;
  const notice=!s.config.home.ssid?t("No private network is configured. You can use instruments through the hotspot, or add private-network access in Networks & hotspot."):s.last_error?t("A connection needs attention. Check profiles and network settings."):"";
  $("simple-notice").textContent=notice;$("simple-notice").hidden=!notice;
  const focused=$("simple-connections").contains(document.activeElement)?document.activeElement.id:null;
  const focusedProfile=focused?document.activeElement.closest("[data-profile-id]")?.dataset.profileId:null;
  const groups=connectionGroups(s);
  const telescopeGroups=[...groups].filter(([,group])=>!group.reserved);
  const occupied=telescopeGroups.filter(([mac,group])=>profiles.some(p=>(p.enabled&&p.mac===mac)||(live.get(p.id)?.connected&&live.get(p.id)?.interface===group.device?.name))).length;
  $("occupied-count").textContent=occupied+" / "+telescopeGroups.length;
  $("saved-count").textContent=profiles.length;
  $("home-count").textContent=s.home_ip?t("Connected"):t("Unavailable");
  $("initial-network-link").hidden=!!s.config.home.ssid;
  renderConnectionsStrip(s,groups);
  const selected=profiles.filter(p=>p.enabled||live.get(p.id)?.connected);
  const switching=$("auto-switch-card"),container=$("simple-connections");
  const switchingFocus=switching.contains(document.activeElement)?document.activeElement:null;
  if(connected){if(container.nextElementSibling!==switching)container.after(switching);}
  else if(switching.nextElementSibling!==$("simple-notice"))$("simple-notice").before(switching);
  if(switchingFocus&&document.activeElement!==switchingFocus)switchingFocus.focus({preventScroll:true});
  const signature=JSON.stringify([BridgeI18n.locale,profiles,s.telescopes,s.devices,s.management,endpointInventory,cardTelemetry,makerSessionKeyState,[...pendingRadios],profiles.map(p=>connectionGuard(p,s))]);
  if(signature!==instrumentSignature){
    instrumentSignature=signature;
    const cards=selected.map(p=>renderInstrumentCard(p,live.get(p.id)||{},s,groups));
    const saved=profiles.filter(p=>!selected.includes(p));
    if(saved.length){
      const card=node("section",undefined,"card simple-card saved-instruments");card.append(node("h3",t("Saved, not connected")));
      for(const p of saved){
        const row=node("div",undefined,"saved-instrument");row.dataset.profileId=p.id;
        const text=node("div");text.append(node("strong",p.name),node("span",instrumentKind(p.kind),"small"));
        const button=node("button",t("Connect"),"secondary connection-action");button.type="button";button.id="simple-connect-0-"+p.id;
        button.setAttribute("aria-label",t("Connect {name}",{name:p.name}));
        button.disabled=!s.management?.paths?.length||!telescopeGroups.some(([mac,g])=>g.available&&!pendingRadios.has(mac));
        button.addEventListener("click",()=>openConnectionChoice(p,button));row.append(text,button);card.append(row,renderRecentNights(p));
      }
      cards.push(card);
    }
    container.replaceChildren(...cards);
  }
  renderOtherInterfaces(s);
  if(focused)($(focused)||$("simple-connect-0-"+focusedProfile)||$("instrument-"+focusedProfile)?.querySelector(".connection-action"))?.focus({preventScroll:true});
}

function wiredHomePath(s){return s.management?.paths?.find(path=>path.role==="home"&&path.kind==="ethernet");}
function renderConnectionsStrip(s,groups){
  const rows=s.devices.map(device=>{
    const profile=s.config.telescopes.find(p=>(p.enabled&&p.mac===device.mac)||(s.telescopes.find(row=>row.id===p.id)?.connected&&s.telescopes.find(row=>row.id===p.id)?.interface===device.name));
    const role=profile?profile.name:device.mac===s.config.home.mac?t("Private Wi-Fi"):s.hotspot?.active&&s.hotspot.mac===device.mac?t("Hotspot"):t({home:"Private Wi-Fi",hotspot:"Hotspot",telescope:"Telescopes and mounts",auto:"Available"}[configuredRadioRole(s,device.mac)]||"Available");
    const label=device.usb?t("USB {letter}",{letter:String.fromCharCode(65+s.devices.filter(d=>d.usb).findIndex(d=>d.mac===device.mac))}):t("Built-in");
    const row=node("span",label+" → "+role,"connection-role");if(profile||device.mac===s.config.home.mac||s.hotspot?.active&&s.hotspot.mac===device.mac)row.classList.add("in-use");return row;
  });
  for(const [,group]of groups)if(!group.device)rows.push(node("span",connectionLabel(group)+" → "+t("Connection unavailable"),"connection-role"));
  if(wiredHomePath(s))rows.unshift(node("span",t("Ethernet")+" → "+t("Private network"),"connection-role in-use"));
  $("connections-list").replaceChildren(...rows);
}
function renderInstrumentCard(profile,live,s,groups){
  const group=(live.connected?[...groups.values()].find(g=>g.device?.name===live.interface):null)||groups.get(profile.mac)||{available:false,number:0},mac=profile.mac;
  const card=node("section",undefined,"card simple-card instrument-card");card.dataset.profileId=profile.id;card.dataset.connection=String(group.number);card.id="instrument-"+profile.id;
  const heading=node("div",undefined,"simple-card-heading"),identity=node("div",undefined,"instrument-identity"),icon=node("span",undefined,"symbol");icon.setAttribute("aria-hidden","true");icon.append(document.querySelector("#nav-telescopes .nav-icon").cloneNode(true));
  const text=node("div"),title=node("h3",profile.name),connection=connectionLabel(group);
  title.id="instrument-"+profile.id+"-title";card.setAttribute("aria-labelledby",title.id);
  text.append(title,node("p",instrumentKind(profile.kind)+" · "+connection+" · "+t("Target: {ip}",{ip:live.target_ip||profile.target_ip||"—"}),"small"));identity.append(icon,text);
  const badge=node("span",t(live.retry_blocked?"Check Wi-Fi password":live.connected?live.service?.state==="not_answering"?"No answer":"Connected":"Connecting"),"badge"+(live.connected?" online":""));
  const info=node("button","i","info-button");info.type="button";info.id="instrument-"+profile.id+"-info";info.setAttribute("aria-label",t("More information")+" · "+profile.name);info.setAttribute("aria-haspopup","dialog");info.setAttribute("aria-controls","profile-info");
  info.addEventListener("click",()=>{profileInfoOpener=info.id;profileInfoId=profile.id;profileInfoSignature="";renderProfileInfo();$("profile-info").showModal();});heading.append(identity,badge,info);card.append(heading);
  const paths=instrumentPaths(profile.id),primary=paths.find(path=>path.host===location.hostname&&!path.requires_client_route)||paths.find(path=>!path.requires_client_route)||paths[0];
  if(paths.length){
    const addresses=node("div",undefined,"instrument-endpoints");addresses.append(node("span",t("For your apps"),"small"));
    const list=node("div",undefined,"endpoint-list");
    for(const [index,path]of [primary].entries()){
      const row=node("div",undefined,"instrument-endpoint"),address=path.host+":"+path.port;
      row.append(node("span",t(path.path==="hotspot"?"Hotspot":"Private network")+" · "+path.transport.toUpperCase(),"small"),node("code",address));
      const copy=node("button",t("Copy"),"text-button endpoint-copy");copy.type="button";copy.id="endpoint-copy-"+profile.id+"-"+index;copy.setAttribute("aria-label",t("Copy address {address}",{address}));
      copy.addEventListener("click",()=>copyEndpoint(address,copy));row.append(copy);
      if(path.requires_client_route)row.append(node("span",t("Requires a route from your app’s network."),"small endpoint-route"));list.append(row);
    }
    addresses.append(list);card.append(addresses);
  }else card.append(node("p",t(endpointInventory?"No confirmed app endpoint.":"App addresses are unavailable. They update automatically."),"small endpoint-unavailable"));
  const message=live.connected&&live.service?.state==="not_answering"?t("Wi-Fi connected · not answering"):live.retry_blocked||!group.available||!live.connected||!live.relay_ready?simpleStatus(profile,live,s,group.available):live.service?.state==="ready"?t("Instrument service answers."):live.service?.state==="not_probed"?t("Instrument service is not checked for this family."):t("Instrument service has not been verified.");
  const detail=node("p",message,"small instrument-service");detail.id="simple-status-"+group.number+"-"+profile.id;card.append(detail);
  if(live.connected&&!live.relay_ready&&live.service?.state==="not_answering")card.append(node("p",simpleStatus(profile,live,s,group.available),"small"));
  const hasSession=live.connected&&!live.retry_blocked&&!!cardTelemetry?.card_data?.[profile.id]?.recent?.[0];
  card.append(renderSessionBlock(profile,hasSession));
  const guard=connectionGuard(profile,s);
  if(!["unknown","off","eligible"].includes(guard.state)){
    const summary=node("p",undefined,"connection-guard");summary.dataset.guardState=guard.state;
    if(guard.activity!==t("Activity not verified"))summary.append(node("span",guard.activity,"guard-activity"));summary.append(node("span",guard.label,"guard-status"));card.append(summary);
  }
  const actions=node("div",undefined,"actions instrument-actions"),button=node("button",t(live.retry_blocked?"Edit password":"Disconnect"),(hasSession?"secondary":"primary")+" connection-action");button.type="button";button.id="simple-connect-"+group.number+"-"+profile.id;
  button.disabled=pendingRadios.has(mac);button.setAttribute("aria-label",live.retry_blocked?t("Edit Wi-Fi password for {name}",{name:profile.name}):t("Disconnect {name} · {connection}",{name:profile.name,connection}));button.setAttribute("aria-describedby",detail.id);
  button.addEventListener("click",()=>{if(live.retry_blocked){openEditor(profile);$("network-password").focus();}else scopeAction(profile.id,"disconnect",button);});actions.append(button);
  if(live.retry_blocked){const disconnect=node("button",t("Disconnect"),"text-button");disconnect.type="button";disconnect.id="simple-disconnect-"+group.number+"-"+profile.id;disconnect.disabled=pendingRadios.has(mac);disconnect.addEventListener("click",()=>scopeAction(profile.id,"disconnect",disconnect));actions.append(disconnect);}
  const move=node("button",t("Move to another connection"),"text-button");move.type="button";move.id="instrument-"+profile.id+"-move";move.disabled=pendingRadios.has(mac)||![...groups].some(([destination,g])=>destination!==mac&&g.available&&!g.reserved&&!pendingRadios.has(destination))||!s.management?.paths?.length;move.addEventListener("click",()=>openConnectionChoice(profile,move));actions.append(move);card.append(actions);return card;
}
function instrumentPaths(id){return (endpointInventory?.endpoints?.find(row=>row.profile_id===id)?.paths||[]).filter(path=>path.ready);}

async function copyEndpoint(address,button){
  const epoch=statusEpoch,opener=button.id;
  let copied=false;
  try{if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(address);copied=true;}}catch(_){/* Plain HTTP uses the synchronous selection fallback below. */}
  if(epoch!==statusEpoch||!accessReady)return;
  if(!copied){
    const selection=document.getSelection(),ranges=selection?[...Array(selection.rangeCount)].map((_,index)=>selection.getRangeAt(index).cloneRange()):[];
    const focused=document.activeElement,start=focused?.selectionStart,end=focused?.selectionEnd;
    const input=node("textarea",address,"copy-fallback");input.readOnly=true;input.setAttribute("aria-label",t("For your apps"));document.body.append(input);
    try{input.focus({preventScroll:true});input.select();copied=!!document.execCommand?.("copy");}catch(_){copied=false;}
    finally{input.remove();if(focused?.isConnected)focused.focus({preventScroll:true});else $(opener)?.focus({preventScroll:true});if(typeof start==="number"&&typeof end==="number")focused?.setSelectionRange?.(start,end);if(selection){selection.removeAllRanges();for(const range of ranges)selection.addRange(range);}}
  }
  if(epoch!==statusEpoch||!accessReady)return;
  $("simple-feedback").textContent=t(copied?"Address copied.":"Could not copy. Select the address to copy it.");$("simple-feedback").hidden=false;
}

function openConnectionChoice(profile,opener){
  if(!state||!accessReady)return;
  const destinations=[...connectionGroups(state)].filter(([,g])=>g.available&&!g.reserved);
  if(!profile.enabled&&!state.telescopes.some(p=>p.id===profile.id&&p.connected)&&destinations.length===1){requestConnection(profile,destinations[0][0],opener);return;}
  connectionChoice={id:profile.id,opener:opener.id};renderConnectionChoice();$("connection-choice").showModal();
}
function renderConnectionChoice(){
  const profile=state?.config.telescopes.find(p=>p.id===connectionChoice?.id);if(!profile){$("connection-choice").close();return;}
  $("connection-choice-name").textContent=profile.name;
  const moving=profile.enabled||state.telescopes.some(p=>p.id===profile.id&&p.connected);
  $("connection-choices").replaceChildren(...[...connectionGroups(state)].filter(([mac,g])=>!g.reserved&&(!moving||mac!==profile.mac)).map(([mac,g])=>{
    const row=node("div",undefined,"connection-choice-row"),occupant=state.config.telescopes.find(p=>p.enabled&&p.mac===mac),label=node("div");label.append(node("strong",connectionLabel(g)),node("p",!g.available?t("Connection unavailable"):occupant?t("Connected to {name}",{name:occupant.name}):t("Available"),"small"));
    const button=node("button",t(moving?"Move here":"Connect"),"tertiary");button.type="button";button.id="choice-connect-"+g.number+"-"+profile.id;button.disabled=!g.available||pendingRadios.has(mac)||pendingRadios.has(profile.mac)||!state.management?.paths?.length;
    button.addEventListener("click",()=>{const opener=$(connectionChoice.opener),current=state.config.telescopes.find(p=>p.id===profile.id);$("connection-choice").close();if(opener&&current)requestConnection(current,mac,opener);});row.append(label,button);return row;
  }));
}
$("connection-choice").addEventListener("close",()=>{const opener=connectionChoice?.opener;connectionChoice=null;$("connection-choices").replaceChildren();if(opener)$(opener)?.focus({preventScroll:true});});

function renderOtherInterfaces(s){
  const cards=[];
  for(const device of s.devices){
    const roles=[];
    if(configuredRadioRole(s,device.mac)==="home"||device.mac===s.config.home.mac)roles.push(["home","Reserved for private Wi-Fi"]);
    if(configuredRadioRole(s,device.mac)==="hotspot"||s.hotspot?.active&&device.mac===s.hotspot.mac)roles.push(["hotspot","Reserved for hotspot"]);
    if(!roles.length)continue;
    const card=node("section",undefined,"card simple-card other-interface-card");
    const head=node("div",undefined,"simple-card-heading");head.append(node("h3",connectionLabel({device})));
    for(const [role,label] of roles){const badge=node("span",t(label),"badge");badge.dataset.reservedRole=role;head.append(badge);}
    card.append(head);cards.push(card);
  }
  $("other-connections").replaceChildren(...cards);
  $("other-network-interfaces").hidden=!cards.length;
}

function connectionGroups(s){
  const groups=new Map();
  for(const d of s.devices)if(d.mac!==s.config.home.mac&&radioAllows(s,d.mac,"telescope"))groups.set(d.mac,{available:true,device:d,reserved:!!s.hotspot?.active&&s.hotspot.mac===d.mac});
  for(const p of s.config.telescopes)if(p.enabled&&p.mac&&!groups.has(p.mac))groups.set(p.mac,{available:false});
  let number=0;for(const group of groups.values())group.number=++number;
  return groups;
}
let requestedMove=null;
function requestConnection(profile,mac,button){
  if(profile.enabled&&profile.mac===mac){scopeAction(profile.id,"disconnect",button);return;}
  if((profile.enabled||state.telescopes.find(p=>p.id===profile.id)?.connected)&&profile.mac!==mac){
    requestedMove={id:profile.id,mac,expected_mac:profile.mac,buttonId:button.id};
    renderMoveDescription();
    $("move-dialog").showModal();return;
  }
  scopeAction(profile.id,"connect",button,{mac,expected_mac:profile.mac});
}

function renderMoveDescription(){
  if(!requestedMove||!state)return;
  const profile=state.config.telescopes.find(p=>p.id===requestedMove.id);
  const mac=requestedMove.mac,groups=connectionGroups(state),source=groups.get(profile?.mac),target=groups.get(mac);
  if(!profile||!target)return;
    $("move-description").textContent=t("{name} is assigned to {source}. It will disconnect there and connect to {target}.",{name:profile.name,source:source?connectionLabel(source):t("Unavailable connection"),target:connectionLabel(target)});
    const occupant=state.config.telescopes.find(p=>p.enabled&&p.mac===mac&&p.id!==profile.id);
    $("move-replacement").textContent=occupant?t("{name} will also disconnect from {target}. Its settings stay saved.",{name:occupant.name,target:connectionLabel(target)}):t("The original connection will be freed. The private network stays connected.");
}

function renderManagement(s){
  const management=s.management||{},retry=management.home_retry||{};
  const homeUnavailable=management.mode==="ap_only"&&!!s.config.home.ssid;
  const messages={ap_only:"Hotspot access only. Instrument forwarding uses the hotspot; private-network access is unavailable.",none:"No confirmed management path. Instrument Wi-Fi may remain associated, but forwarding is unavailable."};
  let text=management.mode==="none"||homeUnavailable?t(messages[management.mode]):"";
  if(management.reason==="ROUTING_CONFLICT")text=t("A private-network route overlaps an instrument subnet. Affected forwarding and endpoints are suspended; instrument Wi-Fi is preserved. Correct the network overlap.");
  const reasons={HOME_RETRY_DEFERRED_AP_RESERVED:"Private-network retry is deferred while the hotspot has clients, protections, pending work or unknown vacancy. No connection is interrupted.",HOME_RETRY_DEFERRED_RADIO_BUSY:"The private-network adapter is busy. Automatic recovery will not take over its connection.",HOME_RETRY_FAILED_AP_RESTORED:"The private-network attempt failed; hotspot access was restored. Another attempt waits at least 45 seconds.",HOME_RETRY_FAILED_AP_RESTORE_FAILED:"Hotspot restoration failed. Check local console access; management availability is not confirmed."};
  if(reasons[retry.reason])text+=(text?" ":"")+t(reasons[retry.reason]);
  if(homeUnavailable)text+=" "+t("If the home adapter also hosts the hotspot, an empty hotspot may be briefly interrupted for a bounded retry. Any station or active protection defers it.");
  const notice=$("management-notice");if(notice.textContent!==text)notice.textContent=text;notice.hidden=!text;
}

function renderSimpleNetwork(s){
  const ap=s.hotspot||{},ready=!!s.home_ip,wired=wiredHomePath(s);
  badge("simple-home-badge",ready?t("Connected"):s.config.home.ssid?t("Unavailable"):t("Set up required"),ready);
  $("simple-home-name").textContent=wired?t("Ethernet"):s.config.home.ssid||t("No saved network");
  $("simple-home-ip").textContent=s.home_ip||"—";
  const homeDevice=s.devices.find(d=>d.mac===s.config.home.mac);
  $("simple-home-adapter-label").textContent=t(wired?"Network adapter":"Wi-Fi adapter");
  $("simple-home-adapter").textContent=wired?wired.interface:homeDevice?adapterLabel(homeDevice):t("Unavailable");
  if(!hotspotNameDirty&&!hotspotNamePending)$("hotspot-name").value=s.config.hotspot.ssid||"";
  const select=$("hotspot-interface"),chosen=hotspotAdapterDirty||hotspotNamePending?select.value:s.config.hotspot.mac||"";
  const options=[{value:"",label:t("Automatic")}];
  for(const d of s.devices){
    const scope=s.config.telescopes.find(p=>p.enabled&&p.mac===d.mac);
    const occupied=d.mac===s.config.home.mac?t("Reserved for private Wi-Fi"):scope?.name;
    options.push({value:d.mac,label:adapterLabel(d)+(occupied?" · "+occupied:""),disabled:!!occupied||d.ap===false||!radioAllows(s,d.mac,"hotspot")});
  }
  if(chosen&&!options.some(o=>o.value===chosen))options.push({value:chosen,label:chosen+" · "+t("Unavailable")});
  const signature=JSON.stringify(options);
  if(select.dataset.options!==signature){select.replaceChildren(...options.map(o=>{const option=node("option",o.label);option.value=o.value;option.disabled=!!o.disabled;return option;}));select.dataset.options=signature;}
  select.value=chosen;select.disabled=hotspotNamePending;
  $("hotspot-name-form").querySelector("button").disabled=hotspotNamePending;
  $("simple-relay-state").textContent=s.relay_ready?t("Active"):t("Waiting");
  const connected=s.telescopes.filter(p=>p.connected);
  const vespera=connected.some(p=>profilesFor(s,p.id)?.kind==="vespera");
  const other=connected.some(p=>!["vespera","seestar"].includes(profilesFor(s,p.id)?.kind));
  const nativeSeestar=connected.some(p=>p.seestar_proxy);
  $("simple-route-state").textContent=!ready?t("Private network unavailable"):vespera?t("Check required"):nativeSeestar&&!other?t("Not required for Seestar"):t("Depends on the app");
  $("simple-route-help").textContent=vespera?t("Singularity uses a router route for Vespera. The bridge cannot confirm that this route is configured. Other apps may have different requirements."):nativeSeestar&&!other?t("The Seestar relay allows its app to connect without adding a router route."):t("Access requirements depend on the instrument and its app. Router configuration cannot be verified from the bridge.");
  badge("simple-ap-badge",ap.active?t("Active"):["no_free_radio","connection_protected","protected_no_recovery_radio","selected_adapter_unavailable","start_failed"].includes(ap.reason)?t("Waiting"):t("Disabled"),!!ap.active);
  $("simple-ap-name").textContent=ap.active?s.setup_ssid:t("Emergency hotspot");
  $("simple-ap-detail").textContent=ap.reason==="start_failed"?t("Could not start the hotspot. Check its driver and service logs."):ap.reason==="selected_adapter_unavailable"?t("The selected hotspot adapter is unavailable or in use. Connect it or choose another free adapter. Any existing hotspot remains active."):ap.active?t("Join this Wi-Fi network to reach the device directly. The connection uses your hotspot password."):ap.reason==="connection_protected"?t("Optional hotspot change deferred while a connection is protected."):ap.reason==="protected_no_recovery_radio"?t("No unprotected radio is available for recovery access. The protected connection was not taken."):ap.reason==="no_free_radio"?t("All connections are in use. Instruments keep priority."):ap.reason==="subnet_conflict"?t("Direct access is waiting for a network settings correction."):t("It starts automatically if the private network becomes unavailable.");
  $("simple-ap-url").hidden=!ap.active;
  const clients=ap.clients||[];
  $("simple-client-title").textContent=t("Connected devices")+(ap.active&&ap.clients_available?" · "+clients.length:"");
  const rows=clients.map((c,index)=>{const row=node("div",undefined,"hotspot-client");row.append(node("span",c.hostname||t("Device {number}",{number:index+1})),node("span",c.ip||t("Address being assigned"),"small"));return row;});
  if(!ap.active)rows.splice(0,rows.length,node("p",t("Available when hotspot is active."),"small"));
  else if(!ap.clients_available)rows.push(node("p",t("The device list is temporarily unavailable."),"small"));
  else if(!clients.length)rows.push(node("p",t("No devices currently connected."),"small"));
  $("simple-ap-clients").replaceChildren(...rows);
}
function renderConnectionMode(s){
  const mode=s.config.connection_mode||"private", access=s.network_access||{};
  if(!modeDirty&&!modePending){$("connection-mode").value=mode;$("hotspot-private-lan").checked=!!s.config.hotspot_private_lan;}
  const selected=$("connection-mode").value;
  $("connection-mode-purpose").textContent=t(selected==="private"?"Private Wi-Fi: stay on your usual network with Internet. Vespera may need a route on your router.":selected==="hotspot_shared"?"Connect your phone to the Astro Bridge hotspot for instrument access and Internet. Astro Bridge gets Internet from your private Wi-Fi.":"Hotspot without Internet: join Astro Bridge for instrument access without configuring your router.");
  $("simple-home-purpose").textContent=t(selected==="hotspot_shared"?"Your private Wi-Fi supplies Internet to the Astro Bridge hotspot. Connect your phone to the hotspot to use your instruments and Internet.":selected==="hotspot_isolated"?"Your phone connects to the Astro Bridge hotspot to use your instruments. Private Wi-Fi is optional and does not provide Internet to this hotspot.":"Connect Astro Bridge to your usual Wi-Fi to use your instrument apps on that network.");
  $("simple-home-detail").textContent=selected==="hotspot_shared"?t(s.home_ip?"Private Wi-Fi connected. Internet access on the hotspot depends on this network.":"Connect Astro Bridge to a private Wi-Fi network with Internet to share it through the hotspot."):selected==="hotspot_isolated"?"":t(s.home_ip?"Astro Bridge is accessible from your private network.":"Direct access lets you reach the device when the private network is absent.");
  const wired=wiredHomePath(s);
  $("connection-mode").querySelector('[value="private"]').textContent=t(wired?"Private network":"Private Wi-Fi bridge");
  if(wired){
    $("simple-home-purpose").textContent=t("Ethernet connects Astro Bridge to your private network.");
    $("simple-home-detail").textContent=t("Astro Bridge is accessible from your private network.");
    if(selected!=="hotspot_isolated")$("connection-mode-purpose").textContent=t("Ethernet connects Astro Bridge to your private network.");
  }
  $("simple-home-detail").hidden=!$("simple-home-detail").textContent;
  $("optional-hotspot").hidden=selected!=="private";
  $("hotspot-mode-help").textContent=t(selected==="private"?"Instruments have priority. The emergency hotspot starts automatically when the private network is unavailable.":"The hotspot stays enabled in this mode. Connect your phone to it to use your instruments.");
  $("hotspot-private-lan").disabled=modePending||selected!=="hotspot_shared";
  $("connection-mode").disabled=modePending;$("connection-mode-save").disabled=modePending;
  const messages={HOTSPOT_UNAVAILABLE:"Sharing is waiting for the hotspot.",UPLINK_UNAVAILABLE:"Sharing is waiting for the private Wi-Fi connection.",DNS_UNAVAILABLE:"Sharing is waiting for a valid DNS server.",SUBNET_CONFLICT:"Sharing is unavailable because network addresses overlap."};
  $("connection-mode-status").textContent=mode!=="hotspot_shared"?"":access.available?t("Internet sharing is ready. Internet access still depends on your private network."):t(messages[access.reason]||"Internet sharing is not available yet. Instrument access is preserved.");
}
function profilesFor(s,id){return s.config.telescopes.find(p=>p.id===id);}
function localMessage(id,message){const element=$(id),key=BridgeI18n.sourceKey(message);if(key){element.dataset.i18n=key;dynamicMessages.add(id);}else{delete element.dataset.i18n;dynamicMessages.delete(id);}element.textContent=key?t(key):message;}
function error(message=""){localMessage("error",message);$("error").hidden=!message;}
function node(tag, text, cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
async function api(path,data,signal){
  if(data!==undefined&&!["/api/login","/api/logout"].includes(path)){
    // A status read also detects changed/rolled-back UI bytes before any write.
    if(!await refresh()||!accessReady||BridgePWA.changed)throw new Error(t("Fresh authenticated status is required before changing settings."));
  }
  if(path.startsWith("/api/scan")&&!accessReady)throw new Error(t("Fresh authenticated status is required before changing settings."));
  const epoch=statusEpoch;
  const options={cache:"no-store",credentials:"same-origin",...(signal?{signal}:{})};
  if(data!==undefined)Object.assign(options,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify(data)});
  const response=await fetch(path,options);
  const body=await response.json();
  if(epoch!==statusEpoch)throw new Error(t("Fresh authenticated status is required before changing settings."));
  if(!response.ok){if(response.status===401&&path!=="/api/login")showLogin();const error=new Error(body.error||"Action failed.");error.code=body.code;error.conflicts=body.conflicting_profile_ids;throw error;}
  return body;
}
function showLogin(force=false){
  const preserveLogin=!force&&!state&&!csrf&&$("dashboard").hidden;
  // Every 401 invalidates stale work, but repeated logged-out polls are not
  // network failures and must not erase a new login draft. A public first load
  // can already have typed input; still reset its old opaque event cursor.
  statusEpoch++;accessReady=false;statusFailures=0;
  if(force||!loggedOut||state||csrf)privateReset(false,preserveLogin);
  loggedOut=true;$("login").hidden=false;$("login-form").inert=false;
  BridgePWA.connection("login");
}
function radioName(mac){const d=state.devices.find(d=>d.mac===mac);return d?adapterLabel(d):t("Missing adapter · {mac}",{mac});}
function badge(id,text,online){$(id).textContent=text;$(id).className="badge"+(online?" online":"");}
function renderAdapterNotice(s){
  const count=s.devices.length;
  let message=count===0?t("No Wi-Fi adapter is detected. Check that your adapters are connected. At least two are needed to link an instrument to your private network. Built-in Wi-Fi plus one USB adapter is enough. You can continue configuring the bridge."):count===1?t("Only one Wi-Fi adapter is detected. At least two are needed to link an instrument to your private network. Built-in Wi-Fi plus one USB adapter is enough. You can continue configuring the bridge."):"";
  // An adapter behind a hub more often failed to get an address from a mount.
  const hub=s.devices.some(d=>d.usb&&d.usb_hub)?t("A Wi-Fi adapter is plugged through a USB hub. Plug it directly into a USB port of the Raspberry Pi if you can. Behind a hub shared with other devices, an adapter more often failed to get an address from a mount."):"";
  if(hub)message=message?message+" "+hub:hub;
  const notice=$("adapter-notice");
  // Keep the live region unchanged between identical status polls.
  if(notice.textContent!==message)notice.textContent=message;
  if(notice.hidden!==!message)notice.hidden=!message;
}
function renderProfileInfo(){
  const profile=state?.config.telescopes.find(p=>p.id===profileInfoId);
  if(!profile){$("profile-info").close();return;}
  const live=state.telescopes.find(p=>p.id===profile.id)||{},activity=state.connection_activity?.profiles?.[profile.id];
  const guard=connectionGuard(profile,state);
  const endpoints=instrumentPaths(profile.id);
  const signature=BridgeI18n.locale+JSON.stringify([profile.name,live,activity,guard,endpoints]);
  if(signature===profileInfoSignature)return;profileInfoSignature=signature;
  $("profile-info-name").textContent=profile.name;
  const paragraphs=[node("p",guard.activity,"small"),node("p",guard.label,"small")];
  if(endpoints.length){
    paragraphs.push(node("h3",t("For your apps")));
    for(const path of endpoints){const row=node("p",t(path.path==="hotspot"?"Hotspot":"Private network")+" · "+path.transport.toUpperCase()+" · ","small");row.append(node("code",path.host+":"+path.port));if(path.requires_client_route)row.append(node("span"," · "+t("Requires a route from your app’s network.")));paragraphs.push(row);}
  }
  if(live.client_ip)paragraphs.push(node("p",t("Pi: {ip}",{ip:live.client_ip}),"small"));
  const paths=(live.management_paths||[]).filter(path=>path.forwarding_ready);
  for(const path of paths)paragraphs.push(node("p",t("{path}: route {subnet} via {ip}",{path:t(path.role==="ap"?"Hotspot":"Private network"),subnet:live.subnet||"—",ip:path.host}),"small"));
  if(!paths.length)paragraphs.push(node("p",t("No confirmed forwarding path. Wi-Fi association alone does not provide access."),"small"));
  if(live.seestar_proxy)paragraphs.push(node("p",t("Seestar relay: automatic LAN discovery"),"small"));
  if(live.legacy_ports)paragraphs.push(node("p",t("Vespera relay: TCP 8082 / 8083"),"small"));
  const spec=pluginById(profile.kind),flag=profile.kind==="vespera"?"legacy_ports":profile.kind+"_proxy";
  if(spec?.relay&&live[flag]&&!["vespera","dwarf","seestar"].includes(profile.kind))for(const path of paths)paragraphs.push(node("p",t("App address: {ip} · {transport} {ports}",{ip:path.host,transport:spec.relay.transport.toUpperCase(),ports:spec.relay.ports.join(", ")}),"small"));
  if(live.dwarf_proxy)for(const path of paths)paragraphs.push(node("p",t("DWARF app IP: {ip} (TCP 9900 / 8082 / 554)",{ip:path.host}),"small"));
  paragraphs.push(node("p",activity?activity.state==="active"?t("Forwarded activity observed"):activity.state==="quiet"?t("Quiet window observed; this does not prove an exposure ended."):t("Activity coverage unknown; do not infer idle."):t("Activity measurement unavailable"),"small"));
  if(pluginById(profile.kind)?.category==="mount")paragraphs.push(node("p",t("Control and guiding traffic protect this mount from automatic switching. Use a manual lock to keep it connected when no traffic is visible."),"small"));
  else if(activity?.classifier)paragraphs.push(node("p",t("Activity uses traffic metadata, not image contents. Small status exchanges and silent connections do not indicate viewing. Other transfers remain protected. Cached images cannot be detected; use manual protection when needed."),"small"));
  if(activity&&activity.packets!==null)paragraphs.push(node("p",t("Path matches: {packets} packets / {bytes} bytes; established TCP: {count}.",{packets:activity.packets,bytes:activity.bytes,count:activity.tcp_established}),"small"));
  $("profile-info-body").replaceChildren(...paragraphs);
}
$("profile-info").addEventListener("close",()=>{const id=profileInfoId;profileInfoId=null;profileInfoSignature="";if(id)$(profileInfoOpener||"profile-"+id+"-info")?.focus({preventScroll:true});profileInfoOpener=null;});

let pairedAppsSignature="";
function renderPairedApps(clients,revision){
  const signature=BridgeI18n.locale+revision+JSON.stringify(clients);
  if(pairedAppsSignature===signature)return;pairedAppsSignature=signature;
  if(!clients.length){$("paired-apps-list").replaceChildren(node("p",t("No paired apps."),"small"));return;}
  const rows=clients.map(client=>{
    const row=node("div",undefined,"actions"),name=node("span",client.name),button=node("button",t("Revoke"),"secondary");
    name.style.overflowWrap="anywhere";button.type="button";
    button.setAttribute("aria-label",t("Revoke access for {name}",{name:client.name}));
    button.addEventListener("click",async()=>{
      button.disabled=true;
      try{await api("/api/auth/clients/revoke",{client_id:client.client_id,config_revision:revision});await refresh();}
      catch(failure){error(failure.message);}finally{button.disabled=false;}
    });row.append(name,button);return row;
  });$("paired-apps-list").replaceChildren(...rows);
}
// How long ago, in the interface language; the bridge only sends a duration it measured on its own monotonic clock.
function clockAgo(seconds){
  if(seconds<60)return t("less than a minute ago");
  const [count,unit]=seconds<3600?[Math.floor(seconds/60),"minute"]:seconds<86400?[Math.floor(seconds/3600),"hour"]:[Math.floor(seconds/86400),"day"];
  try{return new Intl.RelativeTimeFormat(BridgeI18n.locale,{numeric:"always"}).format(-count,unit);}catch{return `${count} ${unit}`;}
}
function clockDate(value){
  const date=new Date(value);
  return Number.isNaN(date.getTime())?value:date.toLocaleString(BridgeI18n.locale,{timeZone:"UTC",dateStyle:"medium",timeStyle:"short"})+" UTC";
}
// When the bridge last set its clock from network time, and what it cannot claim.
function renderClockSync(clock){
  const box=$("clock-sync"),sync=clock?.last_sync;if(!sync){box.hidden=true;return;}
  let line;
  if(sync.kind==="network"&&sync.age_seconds!==null)line=t("Clock last set from network time {when}.",{when:clockAgo(sync.age_seconds)});
  else if(sync.kind==="network")line=t("Not set from network time since the bridge started. Last known setting: {date}.",{date:clockDate(sync.last_network_sync_utc)});
  else if(sync.kind==="client_hint")line=t("Only an approximate time from a phone or browser was received {when}. It is not network time.",{when:clockAgo(sync.age_seconds)});
  else line=t("Clock never set from network time.");
  $("clock-sync-line").textContent=line;
  const verified=clock.clock_status==="synchronized";
  $("clock-sync-advice").textContent=verified?"":t("Clock not verified. Connect the bridge to the Internet or open the app so it can set its clock.");
  $("clock-sync-advice").hidden=verified;box.hidden=false;
}
let clockHintSent=false;
// The bridge has no battery clock: show its time when manifestly wrong and offer this browser's.
function renderBridgeClock(clock){
  renderClockSync(clock);
  const notice=$("bridge-clock");if(!clock){notice.hidden=true;return;}
  const shown=new Date(clock.system_utc),time=Number.isNaN(shown.getTime())?clock.system_utc:shown.toLocaleString(BridgeI18n.locale,{timeZone:"UTC",dateStyle:"medium",timeStyle:"short"})+" UTC";
  if(!clock.system_plausible){notice.textContent=t("The bridge clock reads {time}, which is wrong. Times shown by the bridge are approximate until it reaches network time.",{time});notice.hidden=false;}
  else if(clock.set_from_browser&&clock.clock_status!=="synchronized"){notice.textContent=t("The bridge clock was set from this browser. It uses network time as soon as it reaches the Internet.");notice.hidden=false;}
  else notice.hidden=true;
  if(!clock.system_plausible&&clock.clock_status!=="synchronized"&&!clockHintSent){
    clockHintSent=true;
    void fetch("/api/time-hint",{method:"POST",cache:"no-store",credentials:"same-origin",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify({utc_iso:new Date().toISOString(),uncertainty_ms:5000})})
      .then(response=>response.ok?response.json():null).then(result=>{if(result?.clock)renderBridgeClock(result.clock);}).catch(()=>{});
  }
}
function render(s){
  renderPairedApps(s.paired_clients||[],s.config_revision);
  if(s.deployment){
    $("retry-button").hidden=!s.deployment.host_reboot;
  }
  if(s.ui_revision!==BridgePWA.revision){BridgePWA.checkRevision(s.ui_revision||"unknown");return;}
  loggedOut=false;accessReady=true;statusFailures=0;$("dashboard").inert=false;$("login-form").inert=false;BridgePWA.connection("");
  state=s;csrf=s.csrf;syncSessionPage();$("login").hidden=true;$("dashboard").hidden=false;$("logout").hidden=false;$("demo").hidden=!s.demo;renderBridgeClock(s.bridge_clock);
  void loadCameras();renderAdapterNotice(s);renderManagement(s);renderSimple(s);renderProtection(s);renderAutomation(s);ensureView=s.connection_operation||null;renderEnsure();
  if(!autoPreferencePending)$("auto-connect").checked=!!s.config.auto_connect;$("auto-connect").disabled=autoPreferencePending||ensureView?.state==="running";BridgeEvents.start(s.event_context);BridgeSecure.start(s.event_context);BridgePush.start();if(!updateState)refreshUpdates();focusNotificationTarget();
  $("local-url").textContent=s.url;$("local-url").href=s.url;
  renderPassword(s);
  const ap=s.hotspot||{};
  if(!hotspotPending)$("ap-enabled").checked=!!s.config.hotspot.enabled||(s.config.connection_mode||"private")!=="private";$("ap-enabled").disabled=hotspotPending||!s.config.admin_configured||(s.config.connection_mode||"private")!=="private";
  $("scope-count").textContent=s.telescopes.filter(p=>p.connected).length+" / "+s.config.telescopes.length;
  $("empty-scopes").hidden=!!s.config.telescopes.length;
  for(const details of $("telescopes").querySelectorAll("details[data-profile-id]"))routeDisclosure.set(details.dataset.profileId,details.open);
  for(const id of routeDisclosure.keys())if(!s.config.telescopes.some(p=>p.id===id))routeDisclosure.delete(id);
  const rows=[];
  for(const p of s.config.telescopes){
    const live=s.telescopes.find(x=>x.id===p.id)||{};
    const row=node("tr"),instrument=node("td");
    const heading=node("div",undefined,"instrument-heading"),info=node("button","i","info-button");
    info.type="button";info.id="profile-"+p.id+"-info";info.setAttribute("aria-label",t("Connection details for {name}",{name:p.name}));info.setAttribute("aria-haspopup","dialog");info.setAttribute("aria-controls","profile-info");
    info.addEventListener("click",()=>{profileInfoOpener=info.id;profileInfoId=p.id;renderProfileInfo();$("profile-info").showModal();});
    heading.append(node("strong",p.name),info);instrument.append(heading,node("small",instrumentKind(p.kind)));
    const address=node("td",p.target_ip||"—");
    const paths=(live.management_paths||[]).filter(path=>path.forwarding_ready);
    if(p.kind==="vespera")address.append(node("p",t("Using Vespera from private Wi-Fi requires a route on your router. When connected to the Astro Bridge hotspot, no router route is needed."),"connection-info small"));
    if(live.subnet&&p.kind==="vespera"){
      const details=node("details");details.dataset.profileId=p.id;details.open=routeDisclosure.get(p.id)||false;
      const summary=node("summary",t("Router settings"));summary.id="profile-"+p.id+"-route";
      details.append(summary);
      const home=paths.find(path=>path.role==="home");
      if(home){details.append(node("p",t("Configure the route below on your private Wi-Fi router. The bridge cannot verify that router setting."),"small"),node("p",t("Destination network: {subnet}",{subnet:live.subnet}),"small"),node("p",t("Gateway: {ip}",{ip:home.host}),"small"));}
      else details.append(node("p",t("No confirmed forwarding path. Wi-Fi association alone does not provide access."),"small"));
      details.append(node("p",t("No router route is needed when using the Astro Bridge hotspot."),"small"));
      address.append(details);
    }
    if(live.seestar_proxy)address.append(node("p",t("Search for the telescope in the Seestar app."),"small"));
    if(live.dwarf_proxy)for(const path of paths)address.append(node("p",t(path.role==="ap"?"Hotspot":"Private network")+" · "+t("Connect by IP in DWARFLAB: {ip}",{ip:path.host}),"small"));
    const spec=pluginById(p.kind),flag=p.kind==="vespera"?"legacy_ports":p.kind+"_proxy";
    if(spec?.relay&&live[flag]&&!["vespera","dwarf","seestar"].includes(p.kind))for(const path of paths)address.append(node("p",t("App address: {ip} · {transport} {ports}",{ip:path.host,transport:spec.relay.transport.toUpperCase(),ports:spec.relay.ports.join(", ")}),"small"));
    if(spec?.category==="mount")address.append(node("p",t("Control and guiding traffic protect this mount from automatic switching. Use a manual lock to keep it connected when no traffic is visible."),"small"));
    if(p.kind!=="dwarf"&&live.native_proxy_reason==="NATIVE_PROXY_CONFLICT")address.append(node("p",t("Another device uses these app ports. Use its direct address or disconnect the other device."),"small"));
    if(p.kind==="dwarf"&&live.native_proxy_reason==="NATIVE_PROXY_CONFLICT")address.append(node("p",t("DWARF Pi proxy unavailable: another instrument owns the required ports. Direct target routing may still work."),"small"));
    row.append(instrument,node("td",p.ssid),address,node("td",p.enabled?radioName(p.mac):t("Unassigned")));
    const status=node("td");status.append(node("span",simpleStatus(p,live,s,!!s.devices.find(d=>d.mac===p.mac)||!p.enabled),"badge"+(live.connected?" online":"")));row.append(status);
    if(live.connected){const guard=connectionGuard(p,s);status.append(node("small",guard.activity),node("small",guard.label));}
    if(p.owner_locked)status.append(node("span",t("Owner locked"),"badge"));
    const actions=node("td"),group=node("div",undefined,"table-actions");
    const edit=node("button",t("Edit"),"secondary");edit.type="button";edit.id="profile-"+p.id+"-edit";edit.setAttribute("aria-label",t("Edit {name}",{name:p.name}));edit.addEventListener("click",()=>openEditor(p));
    const remove=node("button",t("Delete"),"text-button");remove.type="button";remove.id="profile-"+p.id+"-remove";remove.setAttribute("aria-label",t("Delete {name}",{name:p.name}));remove.addEventListener("click",()=>{if(confirm(t("Delete the profile {name} and its saved password?",{name:p.name})))scopeAction(p.id,"delete",remove);});
    const ensure=node("button",t("Ensure connection"),"secondary");ensure.type="button";ensure.id="profile-"+p.id+"-ensure";
    ensure.disabled=ensureView?.state==="running"||!live.relay_ready&&(!s.config.auto_connect||!p.auto_eligible);
    ensure.addEventListener("click",()=>ensureConnection(p.id));
    const reserve=node("button",t("Reserve 2 min"),"secondary");reserve.type="button";reserve.id="profile-"+p.id+"-reserve";reserve.disabled=!live.relay_ready||ensureView?.state==="running";reserve.addEventListener("click",()=>changeProtection("reserve",p.id,{ttl_seconds:120}));
    const lock=node("button",t(p.owner_locked?"Remove owner lock":"Owner lock"),"text-button");lock.type="button";lock.id="profile-"+p.id+"-lock";lock.addEventListener("click",()=>openProtection("owner_lock",p.id,{locked:!p.owner_locked}));
    group.append(edit,ensure,reserve,lock,remove);actions.append(group);row.append(actions);rows.push(row);
  }
  const headings=[...$("panel-telescopes").querySelectorAll("thead th")].map(cell=>cell.textContent);
  for(const row of rows)[...row.children].forEach((cell,index)=>cell.dataset.label=headings[index]);
  // Polling updates data without resetting tabs or keyboard focus.
  const focused=$("telescopes").contains(document.activeElement)?document.activeElement.id:null;
  $("telescopes").replaceChildren(...rows);
  if($("profile-info").open)renderProfileInfo();
  if(focused)$(focused)?.focus({preventScroll:true});
  renderRadioRoles(s);
  renderStations(s);
  renderSerial(s);
  renderPlugins(s);
  if(!preferencesDirty){$("hostname").value=s.config.hostname;$("country").value=s.config.country;}
}
// Declared devices (every link nature) and serial mounts. Writes use the same routes as paired apps.
const LINK_LABEL={wifi:"Wi-Fi",serial_usb:"Serial (USB)",usb:"USB"};
const linkLabel=type=>t(LINK_LABEL[type]||"Unknown");
async function serialRequest(method,path,body){
  const response=await fetch("/api"+path,{method,credentials:"same-origin",cache:"no-store",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf,"If-Match":'"'+state.config_revision+'"'},...body===undefined?{}:{body:JSON.stringify(body)}});
  const value=await response.json();if(!response.ok){if(response.status===401)showLogin();throw new Error(value.error?.message||"Action failed.");}return value;
}
function declaredState(row){
  const parts=[t(row.state==="online"?"Online":row.state==="offline"?"Offline":"Unknown")];
  if(row.client_connected!==null&&row.client_connected!==undefined)parts.push(t(row.client_connected?"Client connected":"No client"));
  return parts.join(" · ");
}
function renderSerial(s){
  const cameras=(typeof cameraRows!=="undefined"?cameraRows:[]).map(row=>({id:row.instrument_id||row.id,name:row.name,kind:"camera",link_type:"usb",state:row.alpaca||row.indi?.state==="ready"?"online":"offline",client_connected:null,endpoint:null}));
  const rows=(s.declared_devices||[]).concat(cameras).map(row=>{
    const item=node("div",undefined,"radio-row"),label=node("div");
    const family=row.link_type==="serial_usb"?t("NexStar hand control"):row.kind==="camera"?t("Camera"):instrumentKind(row.family);
    label.append(node("strong",row.name),node("small",family+" · "+linkLabel(row.link_type)));
    label.append(node("span",declaredState(row),"badge"+(row.state==="online"?" online":"")));
    if(row.endpoint?.port)label.append(node("small",t("Enter in the app: {host} · TCP {port}",{host:row.endpoint.host||location.hostname,port:row.endpoint.port})));
    else if(row.link_type==="wifi"&&row.access==="direct"){const spec=pluginById(row.family),live=(s.telescopes||[]).find(p=>p.id===row.id)||{};
      for(const path of (live.management_paths||[]).filter(p=>p.forwarding_ready))if(spec?.relay&&spec.category==="mount")label.append(node("small",t("App address: {ip} · {transport} {ports}",{ip:path.host,transport:spec.relay.transport.toUpperCase(),ports:spec.relay.ports.join(", ")})));}
    if(row.detail?.adapter_present===false)label.append(node("small",t("Adapter unplugged")));
    if(row.detail?.identity_mismatch)label.append(node("small",t("A different model answers on this adapter than the one declared.")));
    item.id="declared-"+row.id;item.append(label);return item;});
  $("declared-list").replaceChildren(...rows);$("declared-empty").hidden=rows.length>0;
  const found=(s.serial_detected||[]).map(d=>{
    const item=node("div",undefined,"radio-row"),label=node("div");
    label.append(node("strong",[d.manufacturer,d.product].filter(Boolean).join(" ")||t("Serial adapter")),node("small",t("Serial (USB)")+(d.serial_number?" · "+t("Adapter serial number {serial}",{serial:d.serial_number}):"")));
    label.append(node("span",d.identification==="identified"?t("NexStar hand control {model}, version {version}",{model:d.identity.model_name||"",version:d.identity.hand_control_version||"?"}):t(d.identification==="not_identified"?"No answer from a NexStar hand control":"Hand control not read yet"),"badge"+(d.identification==="identified"?" online":"")));
    if(d.stable_kind==="by-path")label.append(node("small",t("Identified by its USB socket: always plug it into the same socket.")));
    if(!d.can_add)label.append(node("small",t("Cannot be added: no stable link was found.")));
    const group=node("div",undefined,"table-actions");
    const add=node("button",t("Add as mount…"),"secondary");add.type="button";add.id="detected-add-"+d.node.split("/").pop();add.disabled=!d.can_add;add.addEventListener("click",()=>openSerial(null,d));group.append(add);
    if(d.identification!=="identified"){const read=node("button",t("Identify"),"text-button");read.type="button";read.addEventListener("click",async()=>{read.disabled=true;try{await serialRequest("POST","/serial-devices/identify",{device:d.stable_path});await refresh();}catch(e){error(t(e.message));}finally{read.disabled=false;}});group.append(read);}
    item.append(label,group);return item;});
  $("detected-list").replaceChildren(...found);$("detected-devices").hidden=!found.length;
  const mounts=(s.serial_mounts||[]).map(m=>{
    const item=node("div",undefined,"radio-row"),label=node("div"),group=node("div",undefined,"table-actions");
    label.append(node("strong",m.name),node("small",t("Serial (USB)")+" · TCP "+m.tcp_port+" · "+m.baud+" "+m.format),node("span",t(m.adapter_present===false?"Adapter missing":m.client?"Client connected":"Ready"),"badge"+(m.adapter_present!==false?" online":"")));
    const edit=node("button",t("Edit"),"secondary");edit.type="button";edit.id="serial-"+m.id+"-edit";edit.setAttribute("aria-label",t("Edit {name}",{name:m.name}));edit.addEventListener("click",()=>openSerial(m));
    const remove=node("button",t("Delete"),"text-button");remove.type="button";remove.id="serial-"+m.id+"-remove";remove.setAttribute("aria-label",t("Delete {name}",{name:m.name}));
    remove.addEventListener("click",async()=>{if(!confirm(t("Remove the serial mount {name}?",{name:m.name})))return;try{await serialRequest("DELETE","/serial-mounts/"+m.id);error();await refresh();}catch(e){error(t(e.message));}});
    group.append(edit,remove);item.append(label,group);return item;});
  $("serial-mounts").replaceChildren(...mounts);$("empty-serial").hidden=mounts.length>0;
}
let serialEditing=null;
function openSerial(mount,detected){
  serialEditing=mount?{mount}:{detected};const p=mount||detected.proposal;
  $("serial-editor-title").textContent=t(mount?"Edit serial mount":"Add serial mount");$("serial-error").hidden=true;
  $("serial-name").value=p.name;$("serial-adapter").textContent=mount?mount.device.split("/").pop():(detected.serial_number||detected.stable_path.split("/").pop());
  $("serial-settings").textContent=p.baud+" "+p.format;$("serial-port").textContent="TCP "+p.tcp_port;$("serial-idle").value=p.idle_timeout_seconds;
  $("serial-save").textContent=t(mount?"Save":"Confirm and add");$("serial-editor").showModal();
}
for(const id of ["serial-close","serial-cancel"])$(id).addEventListener("click",()=>$("serial-editor").close());
$("serial-form").addEventListener("submit",async event=>{
  event.preventDefault();const button=$("serial-save");button.disabled=true;$("serial-error").hidden=true;
  try{const name=$("serial-name").value.trim(),idle=Number($("serial-idle").value);
    if(serialEditing.mount){const body={name};if(idle!==serialEditing.mount.idle_timeout_seconds)body.idle_timeout_seconds=idle;await serialRequest("PATCH","/serial-mounts/"+serialEditing.mount.id,body);}
    else await serialRequest("POST","/serial-mounts",Object.assign({},serialEditing.detected.proposal,{name,idle_timeout_seconds:idle}));
    $("serial-editor").close();await refresh();}
  catch(e){localMessage("serial-error",e.message);$("serial-error").hidden=false;}finally{button.disabled=false;}
});
let interfaceRoleDraft=null,interfaceRoleRevision=null,interfaceRolePending=false,interfaceRoleSignature="";
// Station instruments join the bridge network: no adapter, SSID or password.
let stationEditing=null;
function renderStations(s){
  const stations=s.config.stations||[],live=Object.fromEntries((s.stations||[]).map(row=>[row.id,row]));
  const rows=stations.map(p=>{const row=node("div",undefined,"radio-row"),label=node("div");label.append(node("strong",p.name),node("small",instrumentKind(p.kind)+" · "+p.target_ip));
    const reachable=live[p.id]?.reachable;label.append(node("span",t(reachable==null?"Not checked yet":reachable?"Answering":"Not answering"),"badge"+(reachable?" online":"")));
    const group=node("div",undefined,"table-actions");
    const edit=node("button",t("Edit"),"secondary");edit.type="button";edit.id="station-"+p.id+"-edit";edit.setAttribute("aria-label",t("Edit {name}",{name:p.name}));edit.addEventListener("click",()=>openStation(p));
    const remove=node("button",t("Delete"),"text-button");remove.type="button";remove.id="station-"+p.id+"-remove";remove.setAttribute("aria-label",t("Delete {name}",{name:p.name}));
    remove.addEventListener("click",async()=>{if(!confirm(t("Remove the station instrument {name}?",{name:p.name})))return;try{const data=configPayload();data.stations=(data.stations||[]).filter(item=>item.id!==p.id);await save(data);}catch(e){error(e.message);}});
    group.append(edit,remove);row.append(label,group);return row;});
  $("stations").replaceChildren(...rows);$("empty-stations").hidden=rows.length>0;
}
function openStation(p=null){
  stationEditing={id:p?.id||"station-"+crypto.getRandomValues(new Uint32Array(1))[0].toString(36)};
  $("station-editor-title").textContent=t(p?"Edit station instrument":"Add station instrument");$("station-error").hidden=true;
  $("station-name").value=p?.name||"";$("station-kind").value=p?.kind||"seestar";$("station-address").value=p?.target_ip||"";$("station-client-connect").checked=p?.client_connect!==false;
  $("station-clients").replaceChildren(...(state?.hotspot?.clients||[]).filter(c=>c.ip).map(c=>{const option=node("option",c.hostname||c.ip);option.value=c.ip;return option;}));
  $("station-editor").showModal();
}
$("add-station").addEventListener("click",()=>openStation());
for(const id of ["station-close","station-cancel"])$(id).addEventListener("click",()=>$("station-editor").close());
$("station-form").addEventListener("submit",async event=>{
  event.preventDefault();const button=$("station-save");button.disabled=true;$("station-error").hidden=true;
  try{const data=configPayload(),entry={id:stationEditing.id,name:$("station-name").value,kind:$("station-kind").value,access:"station",target_ip:$("station-address").value.trim(),client_connect:$("station-client-connect").checked};
    const list=data.stations||[],index=list.findIndex(item=>item.id===entry.id);if(index<0)list.push(entry);else list[index]=entry;data.stations=list;await save(data,false,true);$("station-editor").close();}
  catch(e){localMessage("station-error",e.message);$("station-error").hidden=false;}finally{button.disabled=false;}
});
function configuredRadioRole(s,mac){return s.config.interface_roles?.[mac]||((s.config.hotspot.mac===mac)?"hotspot":s.config.home.mac===mac?"home":"auto");}
function radioAllows(s,mac,purpose){return ["auto",purpose].includes(configuredRadioRole(s,mac));}
function renderRadioRoles(s){
  const hotspotMac=Object.keys(s.config.interface_roles||{}).find(mac=>s.config.interface_roles[mac]==="hotspot")||s.config.hotspot.mac;
  const warning=!hotspotMac?"No adapter is reserved for the hotspot. Choose Hotspot on an available adapter to keep direct access after restart.":!s.devices.some(d=>d.mac===hotspotMac)?"Hotspot adapter missing. Reconnect it or choose another adapter. Its role remains reserved.":"";
  $("interface-role-warning").hidden=!warning;$("interface-role-warning").textContent=warning?t(warning):"";
  const devices=new Map(s.devices.map(d=>[d.mac,d]));
  for(const mac of Object.keys(s.config.interface_roles||{}))if(!devices.has(mac))devices.set(mac,{mac,missing:true});
  const signature=BridgeI18n.locale+JSON.stringify([...devices])+JSON.stringify(s.config.interface_roles)+JSON.stringify(s.telescopes.map(p=>[p.id,p.connected]))+JSON.stringify(s.hotspot);
  if(interfaceRoleDraft||signature===interfaceRoleSignature)return;interfaceRoleSignature=signature;
  const focused=$("radios").contains(document.activeElement)?document.activeElement.id:null;
  const labels={auto:"Auto",home:"Private Wi-Fi",hotspot:"Hotspot",telescope:"Telescopes and mounts"};
  $("radios").replaceChildren(...[...devices.values()].map(d=>{
    const row=node("div",undefined,"radio-row"),label=node("label",d.missing?d.mac:adapterLabel(d));
    const id="radio-role-"+d.mac.replaceAll(":","");label.htmlFor=id;label.append(node("small",d.mac+" · "+(d.missing?t("Adapter missing"):(d.bands||[]).join(" / ")+" GHz")));if(!d.missing&&d.usb&&d.usb_hub)label.append(node("small",t("Through a USB hub. Plug it directly into the Raspberry Pi if you can."),"radio-hub-warning"));
    const group=node("div",undefined,"radio-role-controls"),select=node("select");select.id=id;select.dataset.roleMac=d.mac;
    for(const [value,text]of Object.entries(labels)){const option=node("option",t(text));option.value=value;option.disabled=value==="hotspot"&&d.ap===false;select.append(option);}
    select.value=configuredRadioRole(s,d.mac);select.disabled=interfaceRolePending;
    select.addEventListener("change",()=>{
      if(!interfaceRoleDraft){interfaceRoleDraft=Object.fromEntries([...devices.keys()].map(mac=>[mac,configuredRadioRole(state,mac)]));interfaceRoleRevision=state.config_revision;}
      if(["home","hotspot"].includes(select.value))for(const mac of Object.keys(interfaceRoleDraft))if(mac!==d.mac&&interfaceRoleDraft[mac]===select.value){interfaceRoleDraft[mac]="auto";const other=$("radio-role-"+mac.replaceAll(":",""));if(other)other.value="auto";}
      interfaceRoleDraft[d.mac]=select.value;$("interface-role-result").textContent=t("Unsaved adapter roles");
    });
    const usage=[];if(s.config.home.mac===d.mac)usage.push(t("Private Wi-Fi"));if(s.hotspot?.active&&s.hotspot.mac===d.mac)usage.push(t("Hotspot"));
    for(const profile of s.config.telescopes)if(profile.enabled&&profile.mac===d.mac)usage.push(profile.name);
    group.append(select,node("small",d.missing?t("Adapter missing"):usage.length?t("Current use: {name}",{name:usage.join(" · ")}):t("Not in use")));row.append(label,group);return row;
  }));
  if(focused)$(focused)?.focus({preventScroll:true});
}
$("interface-role-form").addEventListener("submit",async event=>{
  event.preventDefault();if(interfaceRolePending||!state)return;
  const data=configPayload();data.interface_roles=interfaceRoleDraft||Object.fromEntries([...$("radios").querySelectorAll("select")].map(select=>[select.dataset.roleMac,select.value]));data.config_revision=interfaceRoleRevision||state.config_revision;
  interfaceRolePending=true;$("interface-role-save").disabled=true;for(const select of $("radios").querySelectorAll("select"))select.disabled=true;
  try{await save(data);interfaceRoleDraft=null;interfaceRoleRevision=null;interfaceRoleSignature="";$("interface-role-result").textContent=t("Adapter roles saved. Network connections are being updated.");}
  catch(e){$("interface-role-result").textContent=t(e.message);}
  finally{interfaceRolePending=false;$("interface-role-save").disabled=false;for(const select of $("radios").querySelectorAll("select"))select.disabled=false;if(state)renderRadioRoles(state);}
});
let pluginCatalog=[],pluginInfoId=null,pluginListSignature="",pluginDetailsSignature="";
const pluginPolicyId=id=>({seestar:"auto-demand",vespera:"auto-vespera-demand",dwarf:"auto-dwarf-demand",zwo_mount:"auto-zwo-mount-demand",synscan:"auto-synscan-demand"}[id]||"auto-plugin-"+id);
const pluginById=id=>pluginCatalog.find(p=>p.id===id);
function installPluginCatalog(catalog){
  if(catalog.schema_version!==1||!Array.isArray(catalog.plugins)||!catalog.plugins.length)throw new Error("Plugin catalog unavailable");
  pluginCatalog=catalog.plugins;
  $("profile-kind").replaceChildren(...pluginCatalog.map(p=>{const option=node("option",t(p.name));option.value=p.id;option.dataset.i18n=p.name;return option;}));
  const legend=node("legend",t("Instrument types included in automatic switching"));legend.dataset.i18n="Instrument types included in automatic switching";
  $("plugin-auto-choices").replaceChildren(legend,...pluginCatalog.filter(p=>p.discovery.engine!=="none").map(p=>{const label=node("label",undefined,"checkbox"),input=node("input"),name=node("span",t(p.name));input.type="checkbox";input.id=pluginPolicyId(p.id);name.dataset.i18n=p.name;label.append(input,name);return label;}));
}
function renderPluginDetails(){
  const p=pluginById(pluginInfoId);if(!p)return;
  const signature=BridgeI18n.locale+JSON.stringify(p);if(signature===pluginDetailsSignature)return;pluginDetailsSignature=signature;
  const wasOpen=$("plugin-info-body").querySelector("details")?.open;
  const body=[node("h3",t(p.name)),node("p",t(p.description))];
  const details=node("dl",undefined,"plugin-details");
  const field=(key,value)=>{details.append(node("dt",t(key)),node("dd",value));};
  field("Version",p.version);field("Default address",p.default_address||"—");
  field("Transport",p.relay?p.relay.transport.toUpperCase()+" · "+p.relay.ports.join(", "):t("Direct IP access"));
  const capabilities=node("ul");capabilities.append(node("li",t(p.relay?"Connect through the bridge":"Direct IP access")));if(p.wifi.length)capabilities.append(node("li",t("Recognizes its Wi-Fi network")));if(p.discovery.engine!=="none")capabilities.append(node("li",t("App-triggered switching available")));if(p.identification!=="none")capabilities.append(node("li",t("Hardware identification")));body.push(capabilities);
  body.push(details,node("h3",t("Declared test coverage")));
  const levels={partial_hardware:"Partly tested on hardware",simulation:"Tested in simulation",unverified:"Hardware not yet tested"};
  body.push(node("p",t(levels[p.validation.level])));
  if(p.validation.models.length)body.push(node("p",t("Tested models: {models}",{models:p.validation.models.join(", ")})));
  const checks={wifi:"Wi-Fi connection",identity:"Hardware identification",native_app:"Manufacturer app connection",preview:"Camera preview",switching:"Automatic switching",private_proxy:"Access from private Wi-Fi",hotspot_proxy:"Access from hotspot"};
  const list=node("ul");for(const check of p.validation.checks)list.append(node("li",t(checks[check])));if(list.children.length)body.push(list);
  const limits={physical_pending:"Physical hardware validation is pending.",autonomous_pending:"Autonomous observing has not been validated.",guiding_pending:"Guiding has not been validated.",first_discovery_unavailable:"Use the remembered bridge IP in the manufacturer app; first-time discovery is unavailable.",images_pending:"Stored-image access has not been validated.",switching_pending:"Automatic switching has not been validated on this hardware."};
  for(const limit of p.validation.limitations)body.push(node("p",t(limits[limit]),"small"));
  body.push(node("h3",t("Connection protection")),node("p",t(p.activity==="protect_all"?"All traffic protects the connection.":"Bounded background exchanges are excluded; transfers and unknown activity remain protected."),"small"));
  const advanced=node("details"),summary=node("summary",t("Technical details"));advanced.open=!!wasOpen;advanced.append(summary,node("p","ID: "+p.id+" · "+p.identification+" · "+p.discovery.engine,"small"));body.push(advanced);
  const references=node("ul");for(const reference of p.references){const li=node("li"),link=node("a",reference.title);if(new URL(reference.url).protocol!=="https:")continue;link.href=reference.url;link.target="_blank";link.rel="noopener noreferrer";li.append(link);references.append(li);}body.push(references);
  $("plugin-info-body").replaceChildren(...body);
}
function renderPlugins(s){
  if(Date.now()>makerChipRefresh){makerChipRefresh=Date.now()+5000;const epoch=makerKeyEpoch;makerKeyRequest("GET","").then(value=>{if(epoch===makerKeyEpoch)makerSessionKeyState=value.keys[0].state;}).catch(()=>{});}
  const signature=makerSessionKeyState+BridgeI18n.locale+JSON.stringify(s.config.telescopes.map(p=>[p.kind,p.id]))+JSON.stringify(s.telescopes.map(p=>[p.id,p.connected]));
  if(signature!==pluginListSignature){
    pluginListSignature=signature;
    const levels={partial_hardware:"Partly tested on hardware",simulation:"Tested in simulation",unverified:"Hardware not yet tested"};
    const cards=pluginCatalog.map(p=>{
      const card=node("section",undefined,"card"),heading=node("div",undefined,"card-heading"),name=node("h3",t(p.name)),info=node("button","i","info-button");
      card.dataset.pluginId=p.id;info.type="button";info.id="plugin-"+p.id+"-info";info.setAttribute("aria-label",t("Plugin details")+" · "+t(p.name));info.setAttribute("aria-haspopup","dialog");info.setAttribute("aria-controls","plugin-info");
      info.addEventListener("click",()=>{pluginInfoId=p.id;renderPluginDetails();$("plugin-info").showModal();});heading.append(name,info);
      card.append(heading,node("p",t(p.description)),node("span",t(levels[p.validation.level]),"badge"));
      card.append(node("span",t(sessionCapability(p)),"badge"));
      const features=node("ul",undefined,"plugin-features");if(p.discovery.engine==="none")features.append(node("li",t(p.relay?"Connect through the bridge":"Direct IP access")));
      if(p.discovery.engine!=="none")features.append(node("li",t("App-triggered switching available")));
      if(p.category==="mount")features.append(node("li",t("Protects control and guiding")));
      card.append(features);
      const saved=s.config.telescopes.filter(profile=>profile.kind===p.id),connected=saved.filter(profile=>s.telescopes.some(row=>row.id===profile.id&&row.connected)).length;
      card.append(node("p",t("Configured profiles: {count}",{count:saved.length})+" · "+t("Connected: {count}",{count:connected}),"small"));
      return card;
    });
    const focused=$("plugin-list").contains(document.activeElement)?document.activeElement.id:null;$("plugin-list").replaceChildren(...cards);if(focused)$(focused)?.focus({preventScroll:true});
  }
  if($("plugin-info").open)renderPluginDetails();
}
$("plugin-info").addEventListener("close",()=>{const id=pluginInfoId;pluginInfoId=null;pluginDetailsSignature="";$("plugin-info-body").replaceChildren();if(id)$("plugin-"+id+"-info")?.focus({preventScroll:true});});

function refresh(){
  if(relocated||BridgePWA.changed||authenticating)return Promise.resolve(false);
  if(statusRefresh)return statusRefresh;
  const epoch=statusEpoch;polling=true;
  statusRefresh=(async()=>{
    try{const result=await api("/api/status");if(epoch!==statusEpoch)return false;if(!pluginCatalog.length){const catalog=await api("/api/plugins");if(epoch!==statusEpoch)return false;installPluginCatalog(catalog);}
      const endpointAbort=new AbortController(),endpointTimeout=setTimeout(()=>endpointAbort.abort(),1000);
      try{const endpoints=await api("/api/endpoints",undefined,endpointAbort.signal);if(epoch!==statusEpoch)return false;endpointInventory=endpoints.config_revision===result.config_revision?endpoints:null;}catch(_){if(epoch!==statusEpoch)return false;endpointInventory=null;}finally{clearTimeout(endpointTimeout);}
      const telemetryAbort=new AbortController(),telemetryTimeout=setTimeout(()=>telemetryAbort.abort(),1000);
      try{const snapshot=await api("/api/telemetry",undefined,telemetryAbort.signal);if(epoch!==statusEpoch)return false;cardTelemetry=snapshot.config_revision===result.config_revision?snapshot:null;}catch(_){if(epoch!==statusEpoch)return false;cardTelemetry=null;}finally{clearTimeout(telemetryTimeout);}
      render(result);return accessReady;}
    catch(e){
      if(epoch===statusEpoch){statusFailures++;accessReady=false;$("dashboard").inert=true;BridgeEvents.suspend();BridgeSecure.suspend();BridgePush.suspend();
        if(!navigator.onLine||statusFailures>=2)invalidateConnection("offline");
        else BridgePWA.connection("uncertain");
      }
      return false;
    }
    finally{polling=false;statusRefresh=null;}
  })();
  return statusRefresh;
}
async function resyncForEvents(){
  // An in-flight status read may predate the event: await it, then read again.
  if(statusRefresh)await statusRefresh;
  if(!csrf||relocated)return false;
  return refresh();
}

async function scopeAction(id,action,button,assignment={}){
  const profile=state.config.telescopes.find(p=>p.id===id),mac=profile?.mac;
  const restoreFocus=document.activeElement===button,buttonId=button.id;
  if(pendingRadios.has(mac))return;
  clearTimeout(feedbackTimer);
  const radios=new Set([mac,assignment.mac].filter(Boolean));
  for(const radio of radios)pendingRadios.add(radio);button.disabled=true;error();$("simple-feedback").hidden=true;renderSimple(state);
  try{await api("/api/telescopes/action",{id,action,config_revision:state.config_revision,...assignment});await refresh();
    $("simple-feedback").textContent=action==="connect"?t("Connection to {name} requested. Its actual status updates automatically.",{name:profile.name}):action==="disconnect"?t("Connection to {name} disabled.",{name:profile.name}):t("Instrument deleted.");
    $("simple-feedback").hidden=false;
    feedbackTimer=setTimeout(()=>{$("simple-feedback").hidden=true;},8000);
  }catch(e){error(e.code==="CONNECTION_PROTECTED"?protectionError(e.code):e.message);}finally{for(const radio of radios)pendingRadios.delete(radio);button.disabled=false;if(state)renderSimple(state);if(restoreFocus)$(buttonId)?.focus({preventScroll:true});}
}
function configPayload(){return structuredClone(state.config);}
async function save(data, savePreferences=false, keepEditor=false){
  data.config_revision??=state.config_revision;
  const changedName=data.hostname!==state.config.hostname;
  const changedHome=data.home.ssid!==state.config.home.ssid||data.home.mac!==state.config.home.mac;
  const result=await api("/api/config",data);if(!keepEditor)$("editor").close();if(savePreferences)preferencesDirty=$("hostname").value!==data.hostname||$("country").value!==data.country;
  if(changedName||changedHome){$("apply-notice").hidden=false;$("apply-message").textContent=t("Settings saved. Join the private network if necessary, then open after a few seconds:");$("apply-url").textContent=result.url;$("apply-url").href=result.url;}
  if(changedName){relocated=true;showLogin();$("login").hidden=true;return;}
  if(result.sessions_invalidated){showLogin();error(t("Settings saved. Sign in with your new administrator password."));}
  else{error();await refresh();}
}
function openEditor(profile=null,home=false){
  localMessage("editor-error","");
  if(!state)return;discoveryEpoch++;discoveryChoices=[];editing={candidate:null,home,presetKind:profile?.kind||"generic",id:profile?.id||"scope-"+crypto.getRandomValues(new Uint32Array(1))[0].toString(36)};
  $("editor-step").hidden=!!profile||home;$("editor-step").dataset.i18n="Step 2 of 2 — check the details, then add your instrument.";$("editor-step").textContent=t($("editor-step").dataset.i18n);$("choose-telescope").hidden=!!profile||home;
  $("profile-advanced").open=false;$("physical-advanced").open=false;$("scan").hidden=false;$("factory-password-help").hidden=true;
  $("profile-replaceable-row").hidden=home;$("profile-auto-row").hidden=home;$("profile-client-row").hidden=home;$("physical-advanced").hidden=home;
  $("network-save").dataset.i18n=!profile&&!home?"Add instrument":"Save";$("network-save").textContent=t($("network-save").dataset.i18n);
  const p=home?state.config.home:profile||{};$("editor-title").textContent=home?t("Private network"):profile?t("Edit instrument"):t("Add instrument");$("scope-fields").hidden=home;$("scope-ip").hidden=home;$("first-admin").hidden=state.config.admin_configured;$("initial-admin").required=false;$("initial-admin").value="";
  $("profile-name").value=p.name||"";$("profile-name").required=!home;$("profile-kind").value=p.kind||"generic";$("profile-client-connect").checked=p.client_connect!==false;$("profile-auto-eligible").checked=!!p.auto_eligible;$("profile-replaceable").checked=!!p.auto_replaceable;
  const select=$("network-interface");select.replaceChildren();
  for(const d of state.devices){if(!radioAllows(state,d.mac,home?"home":"telescope")||!home&&d.mac===state.config.home.mac)continue;const occupants=state.config.telescopes.filter(p=>p.enabled&&p.mac===d.mac);const option=node("option",adapterLabel(d)+(occupants.length?" · "+occupants.map(p=>p.name).join(", "):""));option.value=d.mac;select.append(option);}
  if(p.mac&&!state.devices.some(d=>d.mac===p.mac)){const o=node("option",t("Missing adapter · {mac}",{mac:p.mac}));o.value=p.mac;select.append(o);}
  select.value=p.mac||[...select.options].find(o=>!state.config.telescopes.some(p=>p.enabled&&p.mac===o.value))?.value||select.options[0]?.value||"";
  // An adapter already reserved for the private network in Wi-Fi adapters is not asked again.
  const reservedHome=home?state.devices.find(d=>state.config.interface_roles?.[d.mac]==="home"):null;
  if(reservedHome)select.value=reservedHome.mac;
  $("network-ssid").value=p.ssid||"";$("network-ssid").dataset.bssid=p.bssid||"";$("network-password").required=false;$("network-password").value="";$("network-password").placeholder=p.has_password?t("Saved · leave empty to keep"):t("Network password");$("network-open").checked=!!p.ssid&&!p.has_password;$("network-hidden").checked=!!p.hidden;$("target-ip").value=p.target_ip||"";$("target-ip").required=!home;$("radio-fields").hidden=!home||!!reservedHome;$("network-interface").required=home&&!reservedHome;
  $("profile-preset-help").textContent=t("Choose a type to prefill known values. Select your instrument’s actual Wi-Fi network.");$("editor-error").hidden=true;$("scan-results").replaceChildren();$("physical-fields").hidden=home;$("scan").disabled=false;$("discover-reachable").disabled=false;scanning=false;$("binding-bssid").value="";$("binding-confirm").checked=false;$("bind-after-save").checked=false;$("discovery-info").textContent="";renderBinding(profile);radioHelp();loadMakerKey();if(!$("editor").open)$("editor").showModal();
}
function radioHelp(){const mac=$("network-interface").value;const other=state.config.telescopes.find(p=>p.enabled&&p.mac===mac&&p.id!==editing.id);$("radio-help").textContent=editing.home?t("Changing the home adapter may interrupt access to the interface."):other?t("Adapter used by {name}. Connect this profile to replace it, or save without connecting.",{name:other.name}):t("This adapter will be assigned to the instrument when connected.");}
$("network-interface").addEventListener("change",radioHelp);
$("binding-bssid").addEventListener("input",()=>{if(editing)editing.candidate=null;$("binding-confirm").checked=false;});
$("network-ssid").addEventListener("input",()=>{$("network-ssid").dataset.bssid="";});
// Only replace empty fields or values still supplied by the previous preset.
const telescopePreset=kind=>{const p=pluginById(kind);return {name:kind==="generic"?"":p?.name||"",ip:p?.default_address||""};};
$("profile-kind").addEventListener("change",()=>{
  const kind=$("profile-kind").value,previous=telescopePreset(editing.presetKind),preset=telescopePreset(kind);
  for(const [id,key] of [["profile-name","name"],["target-ip","ip"]]){
    const field=$(id);if(!field.value||field.value===previous?.[key])field.value=preset[key];
  }
  editing.presetKind=kind;loadMakerKey();
  $("profile-preset-help").textContent=kind==="dwarf"?t("Name prefilled. The IP address depends on the DWARF model: check its app, then choose its Wi-Fi network."):kind==="generic"?t("Enter your instrument settings and choose its Wi-Fi network."):t("Suggested name and usual IP address. Verify them and choose your instrument’s actual Wi-Fi network. Custom values are preserved.");
});
$("editor").addEventListener("close",()=>{makerKeyEpoch++;clearTimeout(makerKeyTimer);$("maker-key-file").value="";localMessage("editor-error","");discoveryEpoch++;});
$("add-scope").addEventListener("click",()=>openDiscovery());$("close-editor").addEventListener("click",()=>{discoveryEpoch++;discoveryChoices=[];$("editor").close();});
$("network-form").addEventListener("invalid",event=>{const details=event.target.closest("details");if(details)details.open=true;},true);
$("network-form").addEventListener("submit",async event=>{
  event.preventDefault();const button=$("network-save");button.disabled=true;$("editor-error").hidden=true;let errorSnapshot=null;
  try{const data=configPayload();data.admin_password=$("initial-admin").value;const network={mac:editing.home?$("network-interface").value:(state.config.telescopes.find(p=>p.id===editing.id)?.enabled?state.config.telescopes.find(p=>p.id===editing.id).mac:""),ssid:$("network-ssid").value,password:$("network-password").value,open:$("network-open").checked,hidden:$("network-hidden").checked,bssid:$("network-ssid").dataset.bssid||""};
    if(editing.home)data.home=network;else{const p={...network,client_connect:$("profile-client-connect").checked,auto_eligible:$("profile-auto-eligible").checked,auto_replaceable:$("profile-replaceable").checked,id:editing.id,name:$("profile-name").value,kind:$("profile-kind").value,target_ip:$("target-ip").value,enabled:!!state.config.telescopes.find(p=>p.id===editing.id)?.enabled};const index=data.telescopes.findIndex(p=>p.id===editing.id);if(p.enabled)for(const other of data.telescopes)if(other.mac===p.mac)other.enabled=false;if(index<0)data.telescopes.push(p);else data.telescopes[index]=p;}
    const bindAfter=!editing.home&&$("bind-after-save").checked, savedId=editing.id;
    const bindingSnapshot=bindAfter?captureBinding():null;errorSnapshot=bindingSnapshot;
    // Keep this editor generation alive until the explicitly requested binding finishes.
    // User close/Escape/editor replacement still invalidate it, including while save waits.
    await save(data,false,bindAfter);if(bindAfter&&!bindingCurrent(bindingSnapshot))return;
    $("network-password").value="";
    if(bindAfter){try{await bindIdentity(bindingSnapshot);}catch(failure){await refresh();if(bindingCurrent(bindingSnapshot)){openEditor(state.config.telescopes.find(p=>p.id===savedId));errorSnapshot={editor:editing,generation:discoveryEpoch};throw new Error(t("Profile saved, but physical binding failed. Edit this existing profile to retry; do not add it again.")+" "+t(failure.message));}}
      if(!bindingCurrent(bindingSnapshot))return;$("editor").close();}
    $("initial-admin").value="";
  }catch(e){if(errorSnapshot&&!bindingCurrent(errorSnapshot))return;localMessage("editor-error",e.message);$("editor-error").hidden=false;$("editor-error").scrollIntoView({block:"nearest"});}finally{button.disabled=false;}
});
function protectionError(code){
  const reasons={CONNECTION_PROTECTED:"This connection is protected. Release reservations and remove its owner lock first.",LEASE_NOT_OWNED:"Only the owning session can renew or release this reservation.",LEASE_VERSION_CHANGED:"This reservation was renewed. Refresh before releasing it.",LEASE_EXPIRED:"Reservation expired or the bridge restarted. Check current protection.",REAUTHENTICATION_FAILED:"Current administrator password is incorrect.",REAUTHENTICATION_LIMITED:"Too many password attempts. Try again later.",CONNECTION_NOT_READY:"Reserve an already ready connection; a reservation does not connect it.",LEASE_LIMITED:"Reservation capacity reached. Wait for expiry or release an existing reservation."};
  return t(reasons[code]||"Protection could not be changed. Refresh current status and try again.");
}
function renderProtection(s){
  $("protection-purpose").textContent=t("Automatic switching normally waits until a connection has had no traffic for {quiet} seconds. Switches are at least {cooldown} seconds apart. Manual protection lasts until released or expired.",{quiet:s.config.auto_policy?.quiet_seconds||120,cooldown:s.config.auto_policy?.cooldown_seconds||60});
  $("activity-status").textContent=t(s.demo?"Demonstration does not measure Linux traffic.":!s.connection_activity?.supported?"Linux activity measurement is unavailable. OS support may be missing; application OTA cannot install it.":Object.values(s.connection_activity.profiles).some(p=>p.state==="unknown")?"Coverage is uncertain. Read the bootstrap and counter-history guidance before relying on quiet measurements.":"Cached Linux observations use one shared collector across browser tabs.");
  const info=s.connection_protection||{leases:[],owner_locks:[]};
  const summary=t("Client reservations: {leases} · Owner locks: {locks}",{leases:info.leases.length,locks:info.owner_locks.length});
  $("protection-summary").hidden=!info.leases.length&&!info.owner_locks.length;
  if($("protection-summary").textContent!==summary)$("protection-summary").textContent=summary;
  const rows=info.leases.map(lease=>{const p=s.config.telescopes.find(p=>p.id===lease.profile_id),row=node("div",undefined,"radio-row"),label=node("div",p?.name||t("Instrument"));
    label.append(node("small",t(lease.owned?"Your session reservation · {seconds}s remaining":"Another client reservation · {seconds}s remaining",{seconds:lease.remaining_seconds})));
    const buttons=node("div",undefined,"table-actions");
    if(lease.owned){const renew=node("button",t("Renew 2 min"),"secondary");renew.type="button";renew.id="lease-"+lease.id+"-renew";renew.addEventListener("click",()=>changeProtection("renew",lease.id,{version:lease.version,ttl_seconds:120}));buttons.append(renew);}
    const release=node("button",t(lease.owned?"Release reservation":"Owner release…"),"text-button");release.type="button";release.id="lease-"+lease.id+"-release";
    release.addEventListener("click",()=>lease.owned?changeProtection("release",lease.id,{version:lease.version}):openProtection("owner_release",lease.id,{version:lease.version}));buttons.append(release);row.append(label,buttons);return row;});
  for(const id of info.owner_locks){const profile=s.config.telescopes.find(p=>p.id===id),row=node("div",undefined,"radio-row"),button=node("button",t("Remove owner lock"),"text-button");button.type="button";button.id="owner-"+id+"-remove";button.addEventListener("click",()=>openProtection("owner_lock",id,{locked:false}));row.append(node("span",(profile?.name||t("Instrument"))+" · "+t("Owner locked")),button);rows.push(row);}
  if(!rows.length)rows.push(node("p",t("No reservations or owner locks."),"small"));
  const focus=$("protection-list").contains(document.activeElement)?document.activeElement.id:null;$("protection-list").replaceChildren(...rows);if(focus)$(focus)?.focus({preventScroll:true});
}
function openProtection(action,id,body){
  protectionDraft={action,id,body,epoch:statusEpoch,revision:state.config_revision};
  renderProtectionDialog();
  $("protection-password").value="";$("protection-confirm").checked=false;$("protection-error").hidden=true;$("protection-dialog").showModal();
}
function renderProtectionDialog(){
  if(!protectionDraft)return;const {action,body}=protectionDraft;
  $("protection-title").textContent=t(action==="recover_pauses"?"Recover paused connection state…":action==="owner_release"?"Release another client’s reservation":body.locked?"Owner lock":"Remove owner lock");
  $("protection-explanation").textContent=t(action==="recover_pauses"?"Recovery keeps all instruments paused. Release reservations and owner locks first. Then explicitly Connect or Disconnect each saved instrument; no connection is retried automatically.":action==="owner_release"?"This may remove protection for another app’s exposure. Confirm only after checking that it is safe. A renewed reservation requires a fresh confirmation.":"An owner lock survives restart and blocks conflicting connection and maintenance actions until explicitly removed.");
}
async function changeProtection(action,id,body,revision=state?.config_revision,draft=null){
  const epoch=statusEpoch;
  try{await api("/api/protection",{action,id,...body,config_revision:revision});if(epoch!==statusEpoch||!state)return false;await refresh();return epoch===statusEpoch&&!!state;}
  catch(failure){if(epoch===statusEpoch&&state){const message=protectionError(failure.code);if(draft&&protectionDraft===draft&&$("protection-dialog").open){$("protection-error").textContent=message;$("protection-error").hidden=false;}else if(!draft&&!protectionDraft)error(message);}return false;}
}
function clearProtectionDialog(){protectionDraft=null;$("protection-password").value="";$("protection-confirm").checked=false;}
$("protection-cancel").addEventListener("click",()=>{clearProtectionDialog();$("protection-dialog").close();});
$("protection-dialog").addEventListener("cancel",clearProtectionDialog);$("protection-dialog").addEventListener("close",()=>{if(!$("protection-dialog").open)clearProtectionDialog();});
$("protection-form").addEventListener("submit",async event=>{event.preventDefault();const draft=protectionDraft;if(!draft||draft.epoch!==statusEpoch||!$("protection-confirm").checked)return;
  const body={...draft.body,current_password:$("protection-password").value,...(draft.action==="owner_release"?{confirm_release:true}:draft.action==="recover_pauses"?{confirm_recovery:true}:{})};$("protection-password").value="";$("protection-confirm").checked=false;
  const success=await changeProtection(draft.action,draft.id,body,draft.revision,draft);if(success&&protectionDraft===draft){protectionDraft=null;$("protection-dialog").close();}
});
function ensureReason(code){
  const messages={OPERATION_RUNNING:"A running instrument operation prevents automatic replacement.",WIFI_AUTHENTICATION_FAILED:"Wi-Fi authentication failed. Check the password. Retries resume after you change it or reboot the bridge.",NO_REPLACEABLE_CONNECTION:"No consenting, unprotected and freshly quiet connection is available.",REPLACEMENT_COOLDOWN:"A replacement cooldown is active. No retry is queued.",TARGET_BACKOFF:"A recent failed request is cooling down. Check the instrument before retrying.",CONNECTION_PAUSED:"This instrument is paused after an unconfirmed restoration. Use an explicit manual Connect or Disconnect.",PHYSICAL_NETWORK_IN_USE:"This physical access point is already in use by another enabled instrument.",PHYSICAL_NETWORK_UNCERTAIN:"Another enabled instrument has an unverified selector for this network. Select exact access points first.",AUTO_CONNECT_DISABLED:"Enable global Auto-connect and this instrument eligibility first.",NO_FREE_RADIO:"No confidently free radio is available; existing connections were preserved.",AMBIGUOUS_NETWORK:"Several access points match. Select and save the exact BSSID first.",NETWORK_UNOBSERVED:"The selected network was not observed. Check that the instrument is switched on.",INCOMPATIBLE_BAND:"No free radio supports the observed network band.",OVERLAPPING_TARGET_NETWORK:"The target overlaps an active network; use these instruments separately.",BINDING_SELECTOR_CONFLICT:"The saved Wi-Fi selector differs from the physical binding. Correct it explicitly.",PHYSICAL_SELECTION_MISMATCH:"The associated access point differs from the selected observation. Forwarding was withheld.",MANUAL_CONNECTION_PENDING:"This enabled instrument already has manual reconnect intent. Wait or disconnect it explicitly.",ROLE_CLEANUP_PENDING:"An old association still needs cleanup before allocation.",TIMED_OUT:"The connection did not become ready before its deadline.",ASSOCIATION_FAILED:"The Wi-Fi association failed; no automatic retry was left enabled.",CONNECTION_OPERATION_PENDING:"A connection operation is already pending.",PRECONDITION_REQUIRED:"Reload the current configuration before requesting a connection.",PRECONDITION_FAILED:"Configuration changed. Reload it before requesting a connection.",MANAGEMENT_UNAVAILABLE:"No confirmed private-network or hotspot management path is available.",ENSURE_LIMITED:"Too many connection operations. Try again later.",OPERATION_EXPIRED:"Operation record expired. Check current connection status.",OPERATION_RUN_CHANGED:"The service restarted. Check current connection status.",SUPERSEDED:"Configuration changed during the operation; the automatic attempt stopped."};
  return t(messages[code]||"Connection readiness could not be confirmed. Check current status.");
}
function renderEnsure(){
  const operation=ensureView;
  $("ensure-notice").hidden=!operation||operation.state==="succeeded"&&operation.restoration?.state!=="failed";
  $("ensure-endpoint").textContent="";
  if($("ensure-notice").hidden){$("ensure-message").textContent="";return;}
  const name=state?.config.telescopes.find(p=>p.id===operation.profile_id)?.name||t("Instrument");
  const phases={restoring:"Restoring the previous instrument; readiness is not yet confirmed…",disconnecting_victim:"Releasing the selected consenting instrument…",selecting:"Selecting a genuinely free connection…",associating:"Associating Wi-Fi; readiness is not confirmed…",checking_services:"Wi-Fi associated; checking forwarding and services…",cleaning_up:"Cleaning up the automatic attempt…"};
  $("ensure-message").textContent=name+" · "+(operation.state==="running"?t(phases[operation.phase]||phases.selecting):ensureReason(operation.reason)+" "+t(operation.restoration?.state==="failed"?"Cleanup is unconfirmed. Keep management access and check diagnostics.":"Original desired settings were preserved; the automatic attempt stopped."));
  const displaced=state?.config.telescopes.find(p=>p.id===operation.displaced?.profile_id);
  if(operation.displaced)$("ensure-message").textContent+=" · "+t(operation.displaced.reason==="FORCED_INTERRUPTION"?"Forced interruption of {name}":"Quiet replacement of {name}",{name:displaced?.name||t("Instrument")});
  if(operation.restoration?.state==="restored"&&operation.displaced)$("ensure-message").textContent+=" · "+t("Previous connection restored and ready.");
  if(operation.restoration?.state==="failed"&&operation.displaced)$("ensure-message").textContent+=" · "+t("Restoration unconfirmed: instrument paused until explicit owner action.");
}
async function ensureConnection(id){
  const epoch=statusEpoch;
  try{const result=await api("/api/telescopes/ensure",{id,config_revision:state.config_revision});if(epoch!==statusEpoch||!state)return;
    if(result.id){ensureView=result;renderEnsure();await refreshEnsure();}else{await refresh();}
  }catch(failure){if(epoch===statusEpoch)error(ensureReason(failure.code));}
}
async function refreshEnsure(){
  if(!state||!csrf||!accessReady||ensurePolling||ensureView?.state!=="running")return;
  ensurePolling=true;const epoch=statusEpoch,id=ensureView.id;
  try{const result=await api("/api/operations?id="+encodeURIComponent(id));if(epoch!==statusEpoch||!state)return;
    if(result.state!=="running")await refresh();
    if(epoch===statusEpoch&&state){ensureView=result.state!=="running"&&state.connection_operation?.id===id?state.connection_operation:result;renderEnsure();}
  }catch(failure){if(epoch===statusEpoch&&state&&["OPERATION_EXPIRED","OPERATION_RUN_CHANGED"].includes(failure.code)){ensureView=null;$("ensure-notice").hidden=true;error(ensureReason(failure.code));}}
  finally{if(epoch===statusEpoch)ensurePolling=false;}
}
setInterval(refreshEnsure,2000);
function renderAutomation(s){
  const enabledApps=pluginCatalog.filter(p=>p.discovery.engine!=="none"&&s.config.auto_policy?.[p.id+"_demand"]).map(p=>t(p.name));
  $("auto-switch-summary").textContent=!s.config.auto_connect?t("Automatic switching is off."):enabledApps.length?t("Included instrument types: {types}",{types:enabledApps.join(", ")}):t("Choose at least one instrument type in Advanced switching settings.");
  $("auto-switch-risk").hidden=!s.config.auto_connect||!s.config.auto_policy?.force_first_profile;
  const policy=s.config.auto_policy||{quiet_seconds:120,cooldown_seconds:60,force_first_profile:false,seestar_demand:false,vespera_demand:false,dwarf_demand:false};
  if(!autoPolicyDirty){$("auto-quiet").value=policy.quiet_seconds;$("auto-cooldown").value=policy.cooldown_seconds;$("auto-force").checked=policy.force_first_profile;$("auto-protect-operations").checked=!!policy.protect_running_operations;$("auto-night-reservations").checked=policy.night_reservations!==false;for(const p of pluginCatalog)if(p.discovery.engine!=="none")$(pluginPolicyId(p.id)).checked=!!policy[p.id+"_demand"];}
  $("auto-policy-save").disabled=s.connection_operation?.state==="running"||autoPreferencePending;
  const reasons={DISABLED:"Maker discovery trigger is disabled.",SELECTION_REQUIRED:"Several eligible Seestars: select an instrument explicitly.",READY_CONNECTION_PRESERVED:"Existing reachable Seestar preserved.",REQUEST_ACCEPTED:"Seestar discovery requested a connection; readiness is not confirmed.",NO_ELIGIBLE_SEESTAR:"No eligible Seestar is configured."};
  $("maker-demand-status").textContent=t(reasons[s.maker_demand?.reason]||"Maker discovery is bounded by cooldown and current connection safety checks.");
  const vespera=s.maker_demands?.vespera,vesperaReasons={SELECTION_REQUIRED:"Several eligible Vesperas at this target: select an instrument explicitly.",READY_CONNECTION_PRESERVED:"Existing reachable Vespera preserved.",REQUEST_ACCEPTED:"Vespera connection requested; readiness is not confirmed.",NO_ELIGIBLE_VESPERA:"No eligible Vespera is configured at this target."};
  const vesperaMessage=!s.config.auto_connect||!policy.vespera_demand?"Vespera connection trigger is disabled.":vespera?.observer==="MANAGEMENT_UNAVAILABLE"?"Passive Vespera detection is waiting for a confirmed management interface.":!vespera?.supported?"Passive Vespera detection is unavailable here.":vesperaReasons[vespera.reason]||(vespera.reason==="DISABLED"?"Listening for new Vespera connection requests.":"Maker discovery is bounded by cooldown and current connection safety checks.");
  $("vespera-demand-status").textContent=t(vesperaMessage);
  const dwarf=s.maker_demands?.dwarf,dwarfReasons={SELECTION_REQUIRED:"Several eligible DWARFs: select an instrument explicitly.",READY_CONNECTION_PRESERVED:"Existing reachable DWARF preserved.",REQUEST_ACCEPTED:"DWARF connection requested; native-app acceptance is not confirmed.",NO_ELIGIBLE_DWARF:"No eligible DWARF is configured."};
  const dwarfMessage=!s.config.auto_connect||!policy.dwarf_demand?"DWARF discovery trigger is disabled.":dwarf?.observer==="MANAGEMENT_UNAVAILABLE"?"Passive DWARF detection is waiting for a confirmed management interface.":!dwarf?.supported?"Passive DWARF detection is unavailable here.":dwarfReasons[dwarf.reason]||(dwarf.reason==="DISABLED"?"Listening for DWARF discovery requests.":"Maker discovery is bounded by cooldown and current connection safety checks.");
  $("dwarf-demand-status").textContent=t(dwarfMessage);
  const pauses=s.connection_pauses||{profiles:{},recovery_required:false},ids=Object.keys(pauses.profiles);$("pause-notice").hidden=!ids.length&&!pauses.recovery_required;
  $("pause-message").textContent=t(pauses.recovery_required?"Connection pause state is unavailable. Recover it before retrying instruments. All instruments will remain paused.":"Paused instruments: {names}. Use an explicit manual Connect or Disconnect to retry safely.",{names:ids.map(id=>s.config.telescopes.find(p=>p.id===id)?.name||t("Instrument")).join(", ")});
  $("pause-recover").hidden=!pauses.recovery_required;
}
$("pause-recover").addEventListener("click",()=>openProtection("recover_pauses","all",{}));
$("auto-policy-reload").addEventListener("click",()=>{autoPolicyDirty=false;autoPolicyRevision=null;$("auto-ack").checked=false;$("auto-policy-feedback").textContent="";if(state)renderAutomation(state);});
$("auto-policy-form").addEventListener("input",()=>{if(!autoPolicyDirty)autoPolicyRevision=state.config_revision;autoPolicyDirty=true;});
$("auto-policy-form").addEventListener("submit",async event=>{
  event.preventDefault();const epoch=statusEpoch,data=configPayload();data.config_revision=autoPolicyRevision||state.config_revision;data.auto_policy={quiet_seconds:Number($("auto-quiet").value),cooldown_seconds:Number($("auto-cooldown").value),force_first_profile:$("auto-force").checked,protect_running_operations:$("auto-protect-operations").checked,night_reservations:$("auto-night-reservations").checked,...Object.fromEntries(pluginCatalog.filter(p=>p.discovery.engine!=="none").map(p=>[p.id+"_demand",$(pluginPolicyId(p.id)).checked]))};data.acknowledge_interruption=$("auto-ack").checked;
  $("auto-ack").checked=false;autoPreferencePending=true;$("auto-policy-save").disabled=true;$("auto-policy-feedback").textContent="";
  try{await save(data);if(epoch===statusEpoch&&state){autoPolicyDirty=false;autoPolicyRevision=null;renderAutomation(state);$("auto-policy-feedback").textContent=t("Automatic policy saved.");}}
  catch(failure){if(epoch===statusEpoch&&state)$("auto-policy-feedback").textContent=t(failure.code==="PRECONDITION_FAILED"?"Policy changed elsewhere. Your draft was kept. Reload the current policy before editing again.":"Policy was not saved. Enabling advanced options requires explicit risk acknowledgement and the current configuration.");}
  finally{autoPreferencePending=false;if(epoch===statusEpoch&&state)renderAutomation(state);}
});
$("auto-settings-open").addEventListener("click",()=>{
  navigate("configuration","general");applyNavigation();$("auto-advanced").open=true;
  requestAnimationFrame(()=>{$("auto-advanced").scrollIntoView({block:"start"});$("auto-advanced").querySelector("summary").focus({preventScroll:true});});
});
$("auto-connect").addEventListener("change",async()=>{autoPreferencePending=true;$("auto-connect").disabled=true;const enabled=$("auto-connect").checked;try{const data=configPayload();data.auto_connect=enabled;await save(data);}catch(failure){error(failure.message);}finally{autoPreferencePending=false;if(state)render(state);}});
function renderBinding(profile){
  const binding=profile?.physical_binding,aliases=binding?.aliases||[];
  const summary=aliases.map(a=>a.bssid+(a.frequency?" · "+a.frequency+" MHz · "+t("Last-seen frequency hint"):"")+" · "+t(a.source==="wifi-observed"?"Wi-Fi observed (not manufacturer verified)":"Owner confirmed (not manufacturer verified)")).join("; ");
  $("binding-append-row").hidden=!binding;$("binding-append").checked=!!binding;
  $("binding-aliases").replaceChildren(...aliases.map(alias=>{const button=node("button",t("Remove alias {bssid}",{bssid:alias.bssid}),"text-button");button.type="button";button.addEventListener("click",async()=>{if(editing?.id!==profile.id||!$("editor").open||!confirm(t("Remove alias {bssid}?",{bssid:alias.bssid})))return;const snapshot={editor:editing,generation:discoveryEpoch,data:{id:profile.id,action:"bind",remove_bssid:alias.bssid}};try{await bindIdentity(snapshot);}catch(failure){if(bindingCurrent(snapshot)){localMessage("editor-error",failure.message);$("editor-error").hidden=false;}}});return button;}));
  $("binding-info").textContent=summary||t("No physical identity bound. This profile can still be selected by its profile ID.");
  if(binding&&!aliases.some(a=>a.ssid===profile.ssid&&a.bssid===profile.bssid))$("binding-info").textContent+=" "+t("Binding differs from the saved Wi-Fi selector. Binding alone does not change which access point is joined.");
  $("bind-profile").hidden=!profile;$("unlink-profile").hidden=!binding;$("bind-after-row").hidden=!!profile;
}
function discoveryError(failure){
  const messages={NETWORK_BUSY:"A network operation is already running.",DISCOVERY_LIMITED:"Wait before discovering devices again.",DISCOVERY_UNAVAILABLE:"The radio state or scan could not be established safely.",CANDIDATE_EXPIRED:"Discover again before binding this observation.",IDENTITY_UNAVAILABLE:"This response does not establish a physical identity or access point.",PRECONDITION_REQUIRED:"Reload the current configuration before binding.",PRECONDITION_FAILED:"Configuration changed. Reload it before binding.",UPDATE_IN_PROGRESS:"An application update or recovery is in progress."};
  return t(messages[failure.code]||"Check the physical BSSID, SSID and confirmations, then try again.");
}
function captureBinding(unlink=false){
  const editor=editing,generation=discoveryEpoch,id=editor.id;
  const data={id,action:unlink?"unlink":"bind"};
  if(!unlink){
    if(!$("binding-confirm").checked)throw new Error(t("Confirm the physical access point before binding."));
    if($("binding-append").checked)data.append_aliases=true;
    if(editor.candidate)data.candidate_id=editor.candidate;
    else {data.aliases=[{ssid:$("network-ssid").value,bssid:$("binding-bssid").value}];data.owner_confirmed=true;}
  }
  return {data,editor,generation};
}
function bindingCurrent(snapshot,allowClosed=false){return !!state&&!!csrf&&editing===snapshot.editor&&discoveryEpoch===snapshot.generation&&(allowClosed||$("editor").open);}
async function bindIdentity(snapshot,allowClosed=false){
  const {data}=snapshot,id=data.id,profile=state?.config.telescopes.find(p=>p.id===id);
  if(!profile)throw new Error(t("Save the profile before binding its identity."));
  data.config_revision=state.config_revision;
  try{await api("/api/telescopes/binding",data);}
  catch(failure){
    if(!bindingCurrent(snapshot,allowClosed))return;
    if(failure.code!=="BINDING_CONFLICT"){failure.message=discoveryError(failure);throw failure;}
    if(!$("editor").open||!confirm(t("Replace this binding and unlink it from conflicting profiles: {profiles}?",{profiles:(failure.conflicts||[]).join(", ")||profile.name})))throw failure;
    data.confirm_replace=true;data.conflicting_profile_ids=failure.conflicts||[];await api("/api/telescopes/binding",data);
  }
  await refresh();
  if(!bindingCurrent(snapshot,allowClosed)||!$("editor").open)return;
  $("binding-confirm").checked=false;$("bind-after-save").checked=false;renderBinding(state.config.telescopes.find(p=>p.id===id));
  $("discovery-info").textContent=t("Physical binding saved. Wi-Fi selection and active connections are unchanged.");
}
for(const [id,unlink] of [["bind-profile",false],["unlink-profile",true]])$(id).addEventListener("click",async()=>{
  let snapshot;try{snapshot=captureBinding(unlink);await bindIdentity(snapshot);}catch(failure){if(!snapshot||bindingCurrent(snapshot)){localMessage("editor-error",failure.message);$("editor-error").hidden=false;}}
});

$("discover-reachable").addEventListener("click",()=>discoverCandidates("reachable"));
$("binding-bssid").addEventListener("input",()=>{if(editing)editing.candidate=null;$("binding-confirm").checked=false;});
$("network-ssid").addEventListener("input",()=>{delete $("network-ssid").dataset.bssid;$("network-password").value="";$("network-password").placeholder=t("Network password");editing.candidate=null;$("binding-confirm").checked=false;});
function savedObservation(candidate){
  return state?.config.telescopes.find(p=>p.ssid===candidate.ssid&&(!p.bssid||p.bssid===candidate.bssid));
}
function chooseDetectedTelescope(candidate){
  const saved=savedObservation(candidate);$("discovery-dialog").close();openEditor(saved||null);
  if(saved)return;
  const suggestion=candidate.suggested;
  $("profile-kind").value=suggestion.kind;editing.presetKind=suggestion.kind;
  $("profile-name").value=candidate.ssid;$("network-ssid").value=candidate.ssid;$("network-ssid").dataset.bssid=candidate.bssid;
  $("target-ip").value=suggestion.target_ip||"";$("network-open").checked=!candidate.security||candidate.security==="--";
  editing.factoryPassword=suggestion.password||"";$("network-password").value=$("network-open").checked?"":editing.factoryPassword;
  $("factory-password-help").hidden=!$("network-password").value;$("network-password").required=!$("network-open").checked;
  $("binding-bssid").value=candidate.bssid;editing.candidate=candidate.candidate_id||null;
  $("profile-preset-help").textContent=t("Settings filled in for this Wi-Fi network. You can add the instrument now or edit the details.");
  $("scan").hidden=true;
  if(!suggestion.target_ip){$("profile-advanced").open=true;$("profile-preset-help").textContent=t("This model's address is unknown. Enter the instrument IP shown in its app.");}
}
function openDiscovery(scan=true){
  if(!state||!accessReady)return;
  if($("editor").open)$("editor").close();wizardEpoch++;$("discovery-results").replaceChildren();$("discovery-status").textContent="";$("discovery-scan").disabled=false;
  if(!$("discovery-dialog").open)$("discovery-dialog").showModal();if(scan)scanTelescopes();
}
async function wifiCandidates(){
  if(wifiSnapshot&&Date.now()-wifiSnapshotAt<15000)return wifiSnapshot;
  if(wifiRequest)return wifiRequest;
  const session=csrf;
  const request=api("/api/discovery",{mode:"wifi"}).then(result=>{
    if(session===csrf&&state){wifiSnapshot=result;wifiSnapshotAt=Date.now();}return result;
  });
  wifiRequest=request;
  try{return await request;}finally{if(wifiRequest===request)wifiRequest=null;}
}
async function scanTelescopes(){
  const generation=++wizardEpoch,session=csrf;$("discovery-scan").disabled=true;localMessage("discovery-status",t("Looking for instrument Wi-Fi networks…"));
  try{
    const result=await wifiCandidates();
    if(generation!==wizardEpoch||session!==csrf||!$("discovery-dialog").open)return;
    const found=new Map();for(const item of result.candidates)if(item.mode==="wifi"&&item.suggested?.kind!=="generic"&&!found.has(item.ssid))found.set(item.ssid,item);
    const cards=[];
    for(const candidate of found.values()){
      const card=node("article",undefined,"discovery-candidate"),heading=node("div"),title=node("h3",candidate.ssid),type=node("p",instrumentKind(candidate.suggested.kind),"small");type.dataset.i18n=pluginById(candidate.suggested.kind)?.name||"Generic";heading.append(title,type);
      const saved=savedObservation(candidate),button=node("button",t(saved?"Already added — edit":"Add instrument"),saved?"secondary":"");button.type="button";button.dataset.i18n=saved?"Already added — edit":"Add instrument";
      button.addEventListener("click",()=>{if(generation===wizardEpoch&&session===csrf)chooseDetectedTelescope(candidate);});
      const signal=node("p",t("Signal: {signal}%",{signal:candidate.signal}),"small");signal.dataset.signal=String(candidate.signal);card.append(heading,signal,button);cards.push(card);
    }
    $("discovery-results").replaceChildren(...cards);
    const message=result.scan_source==="cached"?"All adapters are in use. Showing networks already known to the bridge; their current availability is not confirmed.":cards.length?"Choose your instrument below. Its settings will be filled in for you.":"No supported instrument Wi-Fi found. Turn on the instrument and scan again, or add it manually.";
    localMessage("discovery-status",t(message));
  }catch(failure){if(generation===wizardEpoch&&session===csrf)localMessage("discovery-status",discoveryError(failure));}
  finally{if(generation===wizardEpoch&&session===csrf)$("discovery-scan").disabled=false;}
}
async function openDetectedTelescope(ident){
  const session=csrf,destination=location.hash;
  try{
    const candidate=await api("/api/discovery/"+ident);
    if(!state||!accessReady||session!==csrf||location.hash!==destination)return;
    chooseDetectedTelescope(candidate);
  }catch(failure){
    if(!state||!accessReady||session!==csrf||location.hash!==destination)return;
    openDiscovery(false);localMessage("discovery-status",t("This network observation is no longer available. Scan again."));
  }
}
$("network-password").addEventListener("input",()=>{if(editing?.factoryPassword)$("factory-password-help").hidden=$("network-password").value!==editing.factoryPassword;});
$("discovery-scan").addEventListener("click",scanTelescopes);
$("discovery-close").addEventListener("click",()=>$("discovery-dialog").close());
$("discovery-dialog").addEventListener("close",()=>{wizardEpoch++;});
$("discovery-manual").addEventListener("click",()=>{$("discovery-dialog").close();openEditor();});
$("choose-telescope").addEventListener("click",()=>openDiscovery());
$("network-open").addEventListener("change",()=>{$("network-password").required=!$("network-open").checked&&!state.config.telescopes.find(p=>p.id===editing?.id)?.has_password;});
async function discoverCandidates(mode){
  const generation=++discoveryEpoch,id=editing.id,button=mode==="wifi"?$("scan"):$("discover-reachable");scanning=true;button.disabled=true;$("discovery-info").textContent=t("Scanning…");
  try{
    const result=mode==="wifi"?await wifiCandidates():await api("/api/discovery",{mode});
    if(generation!==discoveryEpoch||!editing||editing.id!==id||!$("editor").open)return;
    discoveryChoices=result.candidates;const elements=[];
    const reasons={NO_SPARE_RADIO:"No confidently spare radio is available. Management and instrument connections were preserved. Enter settings manually.",NO_CANDIDATES:"No networks found. You can enter the SSID manually.",NO_SUPPORTED_REACHABLE_DEVICE:"No supported directly attached device replied. Serial/model identity remains unknown."};
    $("discovery-info").textContent=t(reasons[result.reason]||"Observed candidates; device identity and observation time are unverified.");
    for(const candidate of discoveryChoices){
      if(candidate.mode==="reachable"){elements.push(node("p",candidate.address+" · "+(candidate.protocol_identity?t("Mount protocol response: {model}. This does not establish a physical binding.",{model:candidate.protocol_identity.model||instrumentKind(candidate.protocol_identity.family)}):t("Seestar scan response; serial/model unknown. This is not a physical binding.")),"small"));continue;}
      const choice=node("button",candidate.ssid+" · "+candidate.bssid+" · "+candidate.frequency+" MHz · "+candidate.signal+" % · "+candidate.security+" · "+t("Wi-Fi observation")+" · "+new Date(candidate.observed_at*1000).toISOString()+" · "+t("Device clock unverified"));choice.type="button";
      choice.addEventListener("click",()=>{
        if(generation!==discoveryEpoch||!editing||editing.id!==id||!$("editor").open)return;
        if($("network-ssid").value!==candidate.ssid){$("network-password").value="";$("network-password").placeholder=t("Network password");}
        $("network-ssid").value=candidate.ssid;$("network-ssid").dataset.bssid=candidate.bssid;$("network-open").checked=!candidate.security||candidate.security==="--";$("binding-bssid").value=candidate.bssid;editing.candidate=candidate.candidate_id;$("binding-confirm").checked=false;
        const suggestion=candidate.suggested;if($("profile-kind").value==="generic")$("profile-kind").value=suggestion.kind;if(!$("profile-name").value)$("profile-name").value=suggestion.name;if(!$("target-ip").value&&suggestion.target_ip)$("target-ip").value=suggestion.target_ip;
        $("profile-preset-help").textContent=t("Type/name/default IP are suggestions only. A scan does not verify the instrument or supply credentials.");
      });elements.push(choice);
    }
    $("scan-results").replaceChildren(...elements);
  }catch(error){if(generation===discoveryEpoch){localMessage("editor-error",discoveryError(error));$("editor-error").hidden=false;}}
  finally{if(generation===discoveryEpoch){scanning=false;button.disabled=false;$("scan").textContent=t("↻ Scan");}}
}
$("scan").addEventListener("click",async()=>{
  if(!editing.home){await discoverCandidates("wifi");return;}
  const generation=++discoveryEpoch;try{const name=state.devices.find(d=>d.mac===$("network-interface").value)?.name;if(!name)return;const result=await api("/api/scan?interface="+encodeURIComponent(name));if(generation!==discoveryEpoch||!editing?.home)return;const buttons=result.networks.map(n=>{const button=node("button",n.ssid+" · "+n.signal+" %");button.type="button";button.addEventListener("click",()=>{$("network-ssid").value=n.ssid;$("network-open").checked=!n.security||n.security==="--";});return button;});$("scan-results").replaceChildren(...buttons);}catch(error){localMessage("editor-error",error.message);$("editor-error").hidden=false;}
});
$("connection-mode").addEventListener("change",()=>{modeDirty=true;if(state)renderConnectionMode(state);});
$("hotspot-private-lan").addEventListener("change",()=>{modeDirty=true;});
$("connection-mode-form").addEventListener("submit",async event=>{
  event.preventDefault();const mode=$("connection-mode").value,lan=$("hotspot-private-lan").checked;
  modePending=true;if(state)renderConnectionMode(state);
  try{const data=configPayload();data.connection_mode=mode;data.hotspot_private_lan=lan;await save(data);modeDirty=false;}
  catch(failure){error(failure.message);}
  finally{modePending=false;if(state)render(state);}
});
$("ap-enabled").addEventListener("change",async()=>{const button=$("ap-enabled");hotspotPending=true;button.disabled=true;try{const data=configPayload();data.hotspot={...data.hotspot,enabled:button.checked};await save(data);}catch(e){error(e.message);}finally{hotspotPending=false;if(state)render(state);}});
$("hotspot-name").addEventListener("input",()=>{hotspotNameDirty=true;$("hotspot-name-result").textContent="";});
$("hotspot-interface").addEventListener("change",()=>{hotspotAdapterDirty=true;$("hotspot-name-result").textContent="";});
$("hotspot-name-form").addEventListener("submit",async event=>{
  event.preventDefault();const name=$("hotspot-name").value,mac=$("hotspot-interface").value;
  if(new TextEncoder().encode(name).length>32){error(t("The hotspot name must contain 1 to 32 bytes, or be empty for the automatic name."));return;}
  hotspotNamePending=true;event.submitter.disabled=true;
  try{const data=configPayload();data.hotspot={...data.hotspot,ssid:name,mac};const secret=$("hotspot-name-password").value;if(secret)data.current_password=secret;$("hotspot-name-password").value="";await save(data);hotspotNameDirty=$("hotspot-name").value!==name;hotspotAdapterDirty=$("hotspot-interface").value!==mac;$("hotspot-name-result").textContent=t("Hotspot settings saved. Reconnect to the hotspot if necessary.");}
  catch(e){error(e.message);}
  finally{hotspotNamePending=false;if(state)render(state);}
});
$("preferences").addEventListener("input",()=>{preferencesDirty=true;});
$("preferences").addEventListener("submit",async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{const data=configPayload();data.hostname=$("hostname").value;data.country=$("country").value;await save(data,true);}catch(e){error(e.message);}finally{button.disabled=false;}});
$("login-form").addEventListener("submit",async event=>{
  event.preventDefault();if(authenticating)return;
  authenticating=true;statusEpoch++;accessReady=false;$("dashboard").inert=true;
  BridgeEvents.suspend();BridgeSecure.suspend();BridgePush.suspend();event.submitter.disabled=true;error();
  try{
    const result=await api("/api/login",{password:$("login-password").value});
    // Old unauthenticated polls cannot reset this login. Only a fresh status may
    // expose private state; offline/revision/logout still invalidate its epoch.
    authenticating=false;BridgeEvents.reset();csrf=result.csrf;statusEpoch++;$("login-password").value="";
    await resyncForEvents();
  }catch(e){error(e.message);}finally{authenticating=false;event.submitter.disabled=false;}
});
$("logout").addEventListener("click",async()=>{try{await api("/api/logout",{});showLogin(true);}catch(e){error(e.message);}});
$("retry-button").addEventListener("click",async()=>{try{await api("/api/reboot",{});error(t("Restart requested. Sign in again in a few moments."));}catch(e){error(e.message);}});
async function loadPublicMode(){
  // Loopback addresses also serve real Linux VMs and native installations.
  // Keep demo credentials hidden unless this server explicitly confirms them.
  try{
    const response=await fetch("/api/ui-info",{cache:"no-store",credentials:"omit"});
    if(!response.ok)return;
    const info=await response.json();if(typeof info.demo!=="boolean")return;
    $("demo-hint").hidden=!info.demo;$("demo").hidden=!info.demo;
  }catch(_){/* Unknown or offline mode never advertises demo credentials. */}
}
loadPublicMode();
for(const name of mainTabs) {
  $("nav-"+name).addEventListener("click",()=>navigate(name));
  $("nav-"+name).addEventListener("keydown",event=>tabKeyboard(event,mainTabs,name,navigate,"nav-"));
}
for(const name of configurationTabs) {
  $("tab-"+name).addEventListener("click",()=>selectConfiguration(name));
  $("tab-"+name).addEventListener("keydown",event=>tabKeyboard(event,configurationTabs,name,selectConfiguration,"tab-"));
}
$("simple-configure").addEventListener("click",()=>{navigate("configuration","telescopes");openDiscovery();});
$("simple-home-settings").addEventListener("click",()=>openEditor(null,true));
setInterval(refresh,5000);refresh();

// One bridge password: web interface, hotspot and Linux account (the SSH line comes from the bridge).
function renderPassword(s){
  const personal=!(s.default_credentials||{}).password;
  $("default-warning").hidden=personal;$("default-warning").textContent=t("The default password 12345678 is still in use. Choose your own below.");
  const ssh=s.ssh||{},line=$("ssh-state");line.hidden=typeof ssh.password_login!=="boolean"||!ssh.account;
  line.textContent=line.hidden?"":ssh.password_login?t("SSH: open, account {account}, with this password.",{account:ssh.account}):t("SSH: closed while the default password is in use.");
  $("hotspot-name-password-field").hidden=!personal;$("hotspot-name-password").required=false;
}
$("credentials-form").addEventListener("submit",async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;localMessage("credentials-message","");
  try{if($("new-password").value!==$("confirm-password").value)throw new Error(t("The new passwords do not match."));
    const result=await api("/api/credentials",{current_password:$("current-password").value,password:$("new-password").value});
    for(const id of ["current-password","new-password","confirm-password"])$(id).value="";
    if(result.sessions_invalidated){showLogin();error(t("Password changed. Join the hotspot again with the new password, then sign in."));}
    else{localMessage("credentials-message","Password changed.");await refresh();}
  }catch(e){localMessage("credentials-message",e.message);}finally{button.disabled=false;}
});

$("cancel-move").addEventListener("click",()=>$("move-dialog").close());
$("move-dialog").addEventListener("close",()=>{const target=requestedMove?.buttonId;requestedMove=null;if(target)$(target)?.focus({preventScroll:true});});
$("confirm-move").addEventListener("click",()=>{const move=requestedMove;if(!move)return;const button=$(move.buttonId);$("move-dialog").close();if(button)scopeAction(move.id,"connect",button,{mac:move.mac,expected_mac:move.expected_mac});});

let updateState=null,updatePolling=false;
async function refreshUpdates(){
  if(!csrf||updatePolling)return;updatePolling=true;
  try{
    updateState=await api("/api/updates");
    renderUpdates();
  }catch(_){/* Normal while the application restarts; retry on the next poll. */}
  finally{updatePolling=false;}
}
function renderUpdates(){
  if(!updateState)return;
    $("update-version").textContent="v"+updateState.current_version;
    $("running-version").textContent="v"+updateState.current_version;
    const descriptions={recovery_required:t("Interrupted update needs console recovery."),unsupported:t("The OS updater prerequisites are not installed. Application updates require the matching platform updater."),unchecked:t("No update check yet."),current:t("The application is up to date."),available:t("Version {version} available.",{version:updateState.version}),installing:t("Installing. The interface will reconnect…"),installed:t("Update installed."),deferred:t("Installation deferred until management is available, all instrument profiles are disabled and pending operations have finished."),rolled_back:t("Previous application files restored."),error:t("Update check or installation failed. The repository may be private, have no release or be unreachable.")};
    $("update-status").textContent=updateState.state==="rolled_back"&&updateState.rollback_service_ready===false?t("Previous files restored. The service needs a restart."):updateState.demo?t("Updates are unavailable in demonstration mode."):updateState.supported===false?descriptions.unsupported:descriptions[updateState.state]||updateState.state;
    if(!updatePreferencePending)$("update-automatic").checked=!!updateState.automatic;
    $("update-automatic").disabled=updatePreferencePending||!!updateState.demo||updateState.supported===false||updateState.maintenance;
    $("update-check").disabled=!!updateState.demo||updateState.supported===false||updateState.maintenance||updateState.state==="installing";
    $("update-install").hidden=!updateState.available||updateState.maintenance||updateState.supported===false||updateState.state==="installing";
}
$("update-check").addEventListener("click",async()=>{try{await api("/api/updates",{action:"check"});$("update-status").textContent=t("Update check requested…");}catch(e){error(e.message);}});
$("update-automatic").addEventListener("change",async()=>{updatePreferencePending=true;$("update-automatic").disabled=true;try{await api("/api/updates",{action:"preferences",automatic:$("update-automatic").checked});await refreshUpdates();}catch(e){error(e.message);}finally{updatePreferencePending=false;await refreshUpdates();}});
$("update-install").addEventListener("click",async()=>{if(!confirm(t("Install this version? Connections will be briefly interrupted.")))return;try{await api("/api/updates",{action:"install",release_id:updateState.release_id});$("update-status").textContent=t("Installation requested…");}catch(e){error(e.message);}});
setInterval(refreshUpdates,10000);refreshUpdates();

// Locale changes update display text without resetting form fields or navigating.
document.addEventListener("bridge:localechange",()=>{
  for(const signal of document.querySelectorAll("#discovery-results [data-signal]"))signal.textContent=t("Signal: {signal}%",{signal:signal.dataset.signal});
  if(state && csrf){render(state);renderBridgeClock(state.bridge_clock);}
  renderUpdates();if(state)renderAutomation(state);if($("protection-dialog").open)renderProtectionDialog();
  $("scan").textContent=t(scanning?"Scanning…":"↻ Scan");
  if($("move-dialog").open)renderMoveDescription();
  if($("connection-choice").open)renderConnectionChoice();
  if(editing && $("editor").open){
    $("editor-title").textContent=editing.home?t("Private network"):state.config.telescopes.some(p=>p.id===editing.id)?t("Edit instrument"):t("Add instrument");
    const profile=editing.home?state.config.home:state.config.telescopes.find(p=>p.id===editing.id);
    $("network-password").placeholder=profile?.has_password?t("Saved · leave empty to keep"):t("Network password");
    radioHelp();
  }
});

BridgePush.configure({api});
BridgeSecure.configure({api,httpUrl:()=>state?.url||"/"});
BridgeEvents.configure({unauthorized:showLogin,resync:resyncForEvents});
BridgePWA.configure({invalidate:invalidateConnection,validate:validateConnection});

// Key contents are never rendered, cached, put into form drafts or logged.
const makerKeyLabels={absent:"No key · stacking data unavailable",unverified:"Key ready, not yet confirmed by the Seestar",verified:"Key confirmed by the Seestar",refused:"The Seestar refused this key",storage_unavailable:"Encrypted key storage is unavailable on this bridge."};
async function makerKeyRequest(method,path,data,revision){
  if(method!=="GET")await refresh();
  if(!accessReady||BridgePWA.changed)throw new Error(t("Fresh authenticated status is required before changing settings."));
  const epoch=statusEpoch;
  const response=await fetch("/api/maker-keys"+path,{method,cache:"no-store",credentials:"same-origin",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf,...revision?{"If-Match":'"'+revision+'"'}:{}},...data===undefined?{}:{body:JSON.stringify(data)}});
  const value=await response.json();
  if(epoch!==statusEpoch)throw new Error(t("Fresh authenticated status is required before changing settings."));
  if(!response.ok){if(response.status===401)showLogin();throw new Error(t(value.error?.message||"Action failed."));}
  return value;
}
function renderMakerKey(value){
  makerKeyRevision=value.config_revision;
  const key=value.keys[0];makerSessionKeyState=key.state;makerKeyUnavailable=key.state==="storage_unavailable";localMessage("maker-key-state",t(makerKeyLabels[key.state]||makerKeyLabels.absent));
  for(const id of ["fetch","import"])$("maker-key-"+id).disabled=key.state==="storage_unavailable";
  $("maker-key-remove").hidden=!["unverified","verified"].includes(key.state);
}
async function loadMakerKey(){
  const epoch=++makerKeyEpoch;clearTimeout(makerKeyTimer);makerKeyRevision=null;
  $("session-settings-feedback").textContent="";
  sessionSelection=null;sessionRevision=null;$("seestar-session-enabled").disabled=true;$("seestar-session-enabled").checked=false;$("seestar-session-feedback").textContent="";
  $("maker-key-file").value="";$("maker-key-state").textContent="";$("maker-key-feedback").textContent="";
  const visible=editing&&!editing.home&&$("profile-kind").value==="seestar";
  $("maker-key-section").hidden=!visible;$("session-settings").hidden=!editing||editing.home;
  $("adapter-session-switches").replaceChildren();$("link-session-state").textContent="";$("link-session-enable").hidden=true;
  if(!visible){if(editing&&!editing.home)await loadSessionSelection(epoch).catch(failure=>localMessage("session-settings-feedback",failure.message));return;}
  for(const id of ["fetch","import","remove"])$("maker-key-"+id).disabled=true;
  try{const value=await makerKeyRequest("GET","");if(epoch!==makerKeyEpoch)return;renderMakerKey(value);await loadSessionSelection(epoch);if(epoch!==makerKeyEpoch)return;$("maker-key-remove").disabled=false;if(value.keys[0].state==="unverified")makerKeyTimer=setTimeout(()=>{if(epoch===makerKeyEpoch&&$("editor").open)loadMakerKey();},2500);}
  catch(failure){if(epoch===makerKeyEpoch)localMessage("maker-key-feedback",failure.message);}
}
async function makerKeyAction(method,path,data,revision){
  const epoch=++makerKeyEpoch;clearTimeout(makerKeyTimer);$("seestar-session-enabled").disabled=true;
  for(const id of ["fetch","import","remove"])$("maker-key-"+id).disabled=true;
  try{
    let value=await makerKeyRequest(method,path,data,revision);
    if(epoch!==makerKeyEpoch)return;
    if(value.operation){
      localMessage("maker-key-feedback",t("Getting the key…"));
      while(value.operation.state==="running"){
        await new Promise(resolve=>setTimeout(resolve,750));if(epoch!==makerKeyEpoch)return;
        value=await makerKeyRequest("GET","/operations/"+encodeURIComponent(value.operation.id));
      }
      if(value.operation.state==="failed")throw new Error(t("The key could not be downloaded. Try again later or import a key file."));
      value=await makerKeyRequest("GET","");
    }
    if(epoch!==makerKeyEpoch)return;renderMakerKey(value);await loadSessionSelection(epoch);if(epoch!==makerKeyEpoch)return;
    localMessage("maker-key-feedback",method==="DELETE"?t("The key was removed."):"");
    if(method==="PUT")makerKeyTimer=setTimeout(()=>{if(epoch===makerKeyEpoch)loadMakerKey();},1500);
  }catch(failure){if(epoch===makerKeyEpoch){
    try{const current=await makerKeyRequest("GET","");if(epoch===makerKeyEpoch){renderMakerKey(current);await loadSessionSelection(epoch);}}catch(_){}
    if(epoch===makerKeyEpoch)localMessage("maker-key-feedback",failure.message);
  }}
  finally{if(epoch===makerKeyEpoch)for(const id of ["fetch","import","remove"])$("maker-key-"+id).disabled=id!=="remove"&&makerKeyUnavailable;}
}
$("maker-key-fetch").addEventListener("click",()=>makerKeyAction("POST","/seestar/fetch",{}));
$("maker-key-import").addEventListener("click",()=>$("maker-key-file").click());
$("maker-key-file").addEventListener("change",async()=>{
  const file=$("maker-key-file").files[0],epoch=makerKeyEpoch;$("maker-key-file").value="";if(!file)return;
  if(file.size>8192){localMessage("maker-key-feedback",t("This key file cannot be used."));return;}
  let pem=await file.text();if(epoch===makerKeyEpoch)await makerKeyAction("PUT","/seestar",{pem});pem="";
});
$("maker-key-remove").addEventListener("click",()=>{if(confirm(t("Remove the shared key for every Seestar profile on this bridge?")))makerKeyAction("DELETE","/seestar",undefined,makerKeyRevision);});

async function sessionRequest(method,path,data,revision){
  const response=await fetch("/api/telemetry"+path,{method,cache:"no-store",credentials:"same-origin",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf,...revision?{"If-Match":'"'+revision+'"'}:{}},...data===undefined?{}:{body:JSON.stringify(data)}});
  const value=await response.json();
  if(!response.ok){if(response.status===401)showLogin();throw new Error(t(value.error?.message||"Action failed."));}
  return value;
}
async function loadSessionSelection(epoch){
  const value=await sessionRequest("GET","");if(epoch!==makerKeyEpoch)return;
  sessionSelection=value.profiles[editing.id]||null;sessionRevision=value.config_revision;makerKeyRevision=value.config_revision;
  $("seestar-session-enabled").checked=!!sessionSelection?.adapters.includes("seestar_events_v1");
  $("seestar-session-enabled").disabled=!sessionSelection;
  renderSessionSettings();
  localMessage("seestar-session-feedback",sessionSelection?"":t("Save this instrument to choose session data."));
}
$("seestar-session-enabled").addEventListener("change",async()=>{
  const epoch=++makerKeyEpoch,checked=$("seestar-session-enabled").checked;clearTimeout(makerKeyTimer);
  if(!sessionSelection||!editing)return;$("seestar-session-enabled").disabled=true;
  const adapters=sessionSelection.adapters.filter(id=>id!=="seestar_events_v1");if(checked)adapters.push("seestar_events_v1");
  try{
    await sessionRequest("PATCH","/settings",{profiles:{[editing.id]:{...sessionSelection,adapters}}},sessionRevision);
    if(epoch===makerKeyEpoch){await loadSessionSelection(epoch);if(epoch===makerKeyEpoch)localMessage("seestar-session-feedback",t("Session data settings saved."));}
  }catch(failure){if(epoch===makerKeyEpoch){await loadSessionSelection(epoch).catch(()=>{});if(epoch===makerKeyEpoch)localMessage("seestar-session-feedback",failure.message);}}
});

setupSessionCards();setupSessionPage();setupCameras();

function renderSessionSettings(){
  const plugin=pluginById($("profile-kind").value),enabled=sessionSelection?.adapters||[];
  $("link-session-state").textContent=t(!sessionSelection?"Save this instrument to choose session data.":enabled.includes("link_v1")?"Link quality · Always on":"Link quality is off in your saved settings. Your choice is preserved.");
  $("link-session-enable").hidden=!sessionSelection||enabled.includes("link_v1");
  $("adapter-session-switches").replaceChildren(...sessionAdapters(plugin).filter(id=>!["link_v1","seestar_events_v1"].includes(id)).map(id=>{
    const label=node("label",undefined,"checkbox"),input=node("input");input.type="checkbox";input.setAttribute("role","switch");input.id="session-adapter-"+id;input.checked=enabled.includes(id);input.disabled=!sessionSelection;
    input.addEventListener("change",()=>saveSessionAdapter(id,input.checked));label.append(input,node("span",t(id==="alpaca_observer_v1"?"Instrument status · Alpaca":"Instrument status")));return label;
  }));
}
async function saveSessionAdapter(id,checked){
  if(!editing||!sessionSelection)return;
  const epoch=++makerKeyEpoch;clearTimeout(makerKeyTimer);
  for(const input of $("session-settings").querySelectorAll("input,button"))input.disabled=true;
  const adapters=sessionSelection.adapters.filter(value=>value!==id);if(checked)adapters.push(id);
  try{await sessionRequest("PATCH","/settings",{profiles:{[editing.id]:{...sessionSelection,adapters}}},sessionRevision);
    if(epoch===makerKeyEpoch){await loadSessionSelection(epoch);if(epoch===makerKeyEpoch)localMessage("session-settings-feedback",t("Session data settings saved."));}
  }catch(failure){if(epoch===makerKeyEpoch){await loadSessionSelection(epoch).catch(()=>{});if(epoch===makerKeyEpoch)localMessage("session-settings-feedback",failure.message);}}
  finally{if(epoch===makerKeyEpoch)$("link-session-enable").disabled=false;}
}
$("link-session-enable").addEventListener("click",()=>saveSessionAdapter("link_v1",true));
