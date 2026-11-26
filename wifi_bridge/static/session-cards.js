"use strict";
let cardTelemetry=null,cardHistoryEpoch=0,cardHistoryOpener=null,cardHistoryProfile=null,cardHistoryCursor=0;
const cardLabels={"stack.frames_accepted":"Kept","stack.frames_rejected":"Rejected","exposure.seconds":"Exposure","battery.percent":"Battery","device.temperature_c":"Board temperature","env.temperature_c":"Temperature","env.dew_point_c":"Dew point","env.humidity_pct":"Humidity","filter.name":"Filter","heater.state":"Heater","target.ra_hours":"Right ascension","target.dec_deg":"Declination","link.rssi_dbm":"Signal","link.ping_ms":"Delay"};
const cardUnits={"stack.frames_accepted":"frames","stack.frames_rejected":"frames","exposure.seconds":"s","battery.percent":"%","device.temperature_c":"°C","env.temperature_c":"°C","env.dew_point_c":"°C","env.humidity_pct":"%","target.ra_hours":"h","target.dec_deg":"°","link.rssi_dbm":"dBm","link.ping_ms":"ms"};
const cardEnums={stacking:"Stacking",capture:"Capturing",observation:"Observing",mosaic:"Mosaic",calibration:"Calibration",initialization:"Initializing",shutdown:"Shutting down",idle:"Idle",goto:"Pointing",pointing:"Pointing",focus:"Focusing",focusing:"Focusing",exposure:"Exposing",running:"Running",paused:"Paused",completed:"Completed",failed:"Failed",stopped:"Stopped",pending:"Waiting",dual_band:"Dual band",light_pollution:"Light pollution",none:"None",solar:"Solar",dark:"Dark",on:"On",off:"Off"};
function cardTime(utc){const date=new Date(utc);return Number.isFinite(date.getTime())?date.toLocaleTimeString(BridgeI18n.locale,{hour:"2-digit",minute:"2-digit"}):"—";}
function sessionAdapters(plugin){return (plugin?.telemetry?.adapters||[]).map(a=>typeof a==="string"?a:a.id);}
function sessionCapability(plugin){const ids=sessionAdapters(plugin);return ids.includes("seestar_events_v1")?["verified","unverified"].includes(makerSessionKeyState)?"Session: full (with your key)":"Session: position only":ids.includes("vespera_status_v1")?"Session: full":ids.includes("alpaca_observer_v1")?"Session: position only":"Session: link only";}
function sessionInfo(profile,opener){
  cardHistoryOpener=opener.id;const plugin=pluginById(profile.kind);$("session-info-body").replaceChildren(sessionCopy("p",plugin?.telemetry?.limitation||"Read-only observations. Available values depend on the instrument and enabled adapters."),sessionCopy("p","A reading becomes stale after three source cadences. Paused means the adapter yielded; it does not mean the instrument stopped. No instrument controls are sent."));
  $("session-info").showModal();
}
function renderSessionBlock(profile,primary=false){
  const block=node("section",undefined,"session-block"),data=cardTelemetry?.card_data?.[profile.id],readings=data?.readings||{},plugin=pluginById(profile.kind);
  block.setAttribute("aria-label",t("Session data"));
  const diagnostic=cardTelemetry?.adapter_status?.[profile.id]||[],paused=diagnostic.some(row=>row.adapter_state==="yielded");
  const operation=readings["operation.kind"],target=readings["target.name"],opState=readings["operation.state"];
  const headline=node("div",undefined,"session-headline");
  const label=paused?t("Paused"):operation?t(cardEnums[operation.value]||"Instrument status"):readings["mount.tracking"]?.value?t("Tracking"):t("Session data");
  headline.append(node("strong",label+(target?" · "+target.value:"")));
  if(operation)headline.append(node("span",t("since {time}",{time:cardTime(target&&target.since>operation.since?target.since:operation.since)}),"small"));
  if(operation?.stale||opState?.stale||target?.stale||paused)headline.classList.add("stale");
  block.append(headline);
  const grid=node("dl",undefined,"session-values");
  const keyLimited=profile.kind==="seestar"&&makerSessionKeyState!=="verified";
  const entries=Object.entries(cardLabels).filter(([metric])=>readings[metric]&&(!keyLimited||metric.startsWith("link.")||metric.startsWith("target."))).slice(0,8);
  for(const [metric,label]of entries){const reading=readings[metric],cell=node("div",undefined,reading.stale||paused?"stale":"");
    let value=typeof reading.value==="number"?new Intl.NumberFormat(BridgeI18n.locale,{maximumFractionDigits:2}).format(reading.value):t(cardEnums[reading.value]||reading.value);
    const units=cardUnits[metric];if(units)value+=" "+(units==="frames"?t("frames"):units);
    cell.append(node("dt",t(label)),node("dd",value),node("small",reading.stale||paused?t("Last reading {time}",{time:cardTime(reading.utc)}):t("updated {seconds} s ago",{seconds:Math.floor(reading.age_seconds)})));grid.append(cell);
  }
  if(entries.length)block.append(grid);else block.append(node("p",t(cardTelemetry?"No session reading yet.":"Session readings are unavailable. They update automatically."),"small"));
  if(operation)block.append(node("p",operation.stale||paused?t("Last reading {time}",{time:cardTime(operation.utc)}):t("updated {seconds} s ago",{seconds:Math.floor(operation.age_seconds)}),"small"));
  let limitation=profile.kind==="seestar"&&!keyLimited?"Read-only observations. Available values depend on the instrument and enabled adapters.":plugin?.telemetry?.limitation;
  if(keyLimited){
    const keyCopy={absent:"Position only. Stacking data needs the Seestar key.",unverified:"Key not yet confirmed",refused:"The Seestar refused this key",storage_unavailable:"Encrypted key storage is unavailable on this bridge."};
    limitation=keyCopy[makerSessionKeyState]||keyCopy.absent;
    if(makerSessionKeyState==="unverified")block.append(node("p",t("The bridge is waiting for the Seestar to confirm the supplied key."),"small"));
    if(makerSessionKeyState==="refused")block.append(node("p",t("Stacking data is unavailable. Get or import another key in this profile."),"small"));
  }
  if(limitation){const line=node("p",undefined,"session-limitation");line.append(node("span",t(limitation)));const info=node("button","i","info-button");info.type="button";info.id="session-info-"+profile.id;info.setAttribute("aria-label",t("Session data information"));info.setAttribute("aria-haspopup","dialog");info.addEventListener("click",()=>sessionInfo(profile,info));line.append(info);
    if(keyLimited){const key=node("button",t("Get the key"),"text-button");key.type="button";key.id="session-key-"+profile.id;key.addEventListener("click",()=>{openEditor(profile);$("maker-key-section").scrollIntoView({block:"center"});$("maker-key-fetch").focus();});line.append(key);}block.append(line);}
  if(data?.tonight?.length||data?.history_partial){const tonight=node("div",undefined,"session-tonight");tonight.append(node("h4",t("Tonight")));for(const row of data.tonight)tonight.append(node("p",row.target+" · "+t("{kept} kept · {rejected} rejected",{kept:row.frames_accepted??"—",rejected:row.frames_rejected??"—"})));if(data.history_partial)tonight.append(node("p",t("Recent recorded readings only; earlier targets or readings may be omitted."),"small"));block.append(tonight);}
  if(cardTelemetry?.clock_status==="unverified"&&data?.tonight?.length)block.append(node("p",t("Bridge clock is not synchronized."),"small"));
  const latest=data?.recent?.[0];if(latest){const link=sessionOpenLink(latest.id);link.id="session-open-"+profile.id;if(primary)link.classList.add("primary");block.append(link);}
  block.append(sessionHistoryButton(profile));return block;
}
function sessionHistoryButton(profile){const button=node("button",t("All sessions"),"text-button");button.type="button";button.id="session-history-"+profile.id;button.addEventListener("click",()=>openSessionHistory(profile,button));return button;}
function renderRecentNights(profile){
  const block=node("div",undefined,"recent-nights"),rows=cardTelemetry?.card_data?.[profile.id]?.recent||[];block.append(node("h4",t("Recent nights")),node("p",t("Latest session per night"),"small"));
  for(const row of rows.slice(0,3))block.append(node("p",new Date(row.started).toLocaleDateString(BridgeI18n.locale)+" · "+(row.frames_accepted===null&&row.frames_rejected===null?t("No frame readings."):t("{kept} kept · {rejected} rejected",{kept:row.frames_accepted??"—",rejected:row.frames_rejected??"—"})),"small"));
  if(!rows.length)block.append(node("p",t("No recorded sessions."),"small"));block.append(sessionHistoryButton(profile));return block;
}
async function openSessionHistory(profile,opener){cardHistoryProfile=profile.id;cardHistoryOpener=opener.id;cardHistoryCursor=0;$("session-history-list").replaceChildren();delete $("session-history-title").dataset.i18n;$("session-history-title").replaceChildren(sessionCopy("span","All sessions"),document.createTextNode(" · "+profile.name));$("session-history").showModal();await loadSessionHistory();}
async function loadSessionHistory(){
  const epoch=++cardHistoryEpoch;$("session-history-more").disabled=true;
  try{const result=await sessionRequest("GET","/sessions?"+new URLSearchParams({profile_id:cardHistoryProfile,after:cardHistoryCursor,limit:20}));if(epoch!==cardHistoryEpoch||!accessReady)return;
    for(const row of result.sessions){const item=node("p");item.append(sessionDateNode("strong",row.started),document.createTextNode(" · "),sessionCopy("span",row.ended?"Completed":"In progress"));item.append(document.createTextNode(" · "),sessionOpenLink(row.id));$("session-history-list").append(item);}if(!result.sessions.length&&cardHistoryCursor===0)$("session-history-list").append(sessionCopy("p","No recorded sessions."));cardHistoryCursor=result.next_cursor;$("session-history-more").hidden=!result.has_more;
  }catch(failure){if(epoch===cardHistoryEpoch)$("session-history-list").append(node("p",failure.message,"error"));}finally{if(epoch===cardHistoryEpoch)$("session-history-more").disabled=false;}
}
function setupSessionCards(){
  $("session-history-more").addEventListener("click",loadSessionHistory);
  for(const id of ["session-history","session-info"]){const dialog=$(id);dialog.querySelector(".dialog-close").addEventListener("click",()=>dialog.close());dialog.addEventListener("close",()=>{cardHistoryEpoch++;$(cardHistoryOpener)?.focus({preventScroll:true});});}
}
function resetSessionCards(){cardTelemetry=null;cardHistoryEpoch++;for(const id of ["session-history","session-info"])$(id).close();$("session-history-list").replaceChildren();$("session-history-title").dataset.i18n="All sessions";$("session-history-title").textContent=t("All sessions");$("session-info-body").replaceChildren();cardHistoryProfile=cardHistoryOpener=null;}
