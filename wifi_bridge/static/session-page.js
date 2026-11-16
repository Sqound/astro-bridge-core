"use strict";
// All views are bounded snapshots. Exports stream directly to the browser download manager.
let sessionPageRoute="",sessionPageEpoch=0,sessionPageData=null,sessionSettingsData=null;
let sessionSettingsCursor=0,sessionSettingsLoading=false,sessionDeleteTarget=null,sessionDeleteOpener=null;
const sessionNumber=value=>new Intl.NumberFormat(BridgeI18n.locale,{maximumFractionDigits:2}).format(value);
function sessionCopy(tag,key,className){const element=node(tag,t(key),className);element.dataset.i18n=key;return element;}
function renderSessionDate(element){
  const profile=element.dataset.sessionProfile,kind=element.dataset.sessionKind;
  const prefix=profile?(state.config.telescopes.find(row=>row.id===profile)?.name||instrumentKind(kind))+" · ":"";
  element.textContent=prefix+new Date(element.dataset.sessionStarted).toLocaleString(BridgeI18n.locale);
}
function sessionDateNode(tag,started,profile,kind){
  const element=node(tag);element.dataset.sessionStarted=started;
  if(profile){element.dataset.sessionProfile=profile;element.dataset.sessionKind=kind;}
  renderSessionDate(element);return element;
}
function sessionOpenLink(id){const link=sessionCopy("a","Open session","session-open");link.href="#session/"+encodeURIComponent(id);link.addEventListener("click",()=>$("session-history").close());return link;}
function sessionPageValid(epoch){return epoch===sessionPageEpoch&&accessReady&&!!state;}
function syncSessionPage(){
  const route=location.hash.slice(1),relevant=route.startsWith("session/")||route==="configuration/sessions";
  if(!accessReady||!state)return;
  if(route===sessionPageRoute){if(route==="configuration/sessions")renderSessionReaders();return;}
  sessionPageRoute=route;sessionPageEpoch++;sessionPageData=null;sessionSettingsData=null;
  $("session-delete-dialog").close();$("session-page-content").replaceChildren();$("session-page-feedback").textContent="";
  if(relevant){if(route.startsWith("session/"))loadSessionPage();else loadSessionSettings();}
}
function resetSessionPage(){
  sessionPageEpoch++;sessionPageRoute="";sessionPageData=sessionSettingsData=sessionDeleteTarget=null;sessionSettingsLoading=false;
  $("session-delete-dialog").close();
  for(const id of ["session-page-content","session-page-feedback","session-storage-facts","session-reader-apps","session-settings-list","session-retention-feedback","session-delete-feedback","session-delete-description"])$(id).replaceChildren();
}
async function loadSessionPage(){
  const id=sessionPageRoute.slice(8),epoch=++sessionPageEpoch;
  $("session-page-refresh").disabled=true;$("session-page-feedback").textContent=t("Loading session…");
  try{
    const [summary,page,status,analysis]=await Promise.all([sessionRequest("GET","/sessions/"+id),sessionRequest("GET","/samples?"+new URLSearchParams({session_id:id,limit:500})),sessionRequest("GET",""),sessionRequest("GET","/sessions/"+id+"/analysis")]);
    if(!sessionPageValid(epoch))return;
    sessionPageData={summary,page,status,analysis};$("session-page-feedback").textContent="";renderSessionPage();
  }catch(failure){if(sessionPageValid(epoch)){sessionPageData=null;$("session-page-content").replaceChildren();$("session-page-feedback").textContent=t("This session is unavailable. It may have been removed by retention or deletion.");}}
  finally{if(epoch===sessionPageEpoch)$("session-page-refresh").disabled=false;}
}
function sessionSection(title){const section=node("section",undefined,"card");section.append(node("h2",t(title)));return section;}
function sessionFact(parent,label,value){const row=node("div");row.append(node("dt",t(label)),node("dd",value));parent.append(row);}
function renderSessionPage(){
  if(!sessionPageData)return;
  const {summary:s,page,status,analysis}=sessionPageData,samples=page.samples,latest=metric=>samples.filter(row=>row.metric===metric).at(-1);
  const focused=document.activeElement?.id;
  const profile=state.config.telescopes.find(row=>row.id===s.profile_id),content=$("session-page-content");content.replaceChildren();
  $("session-page-title").textContent=(profile?.name||instrumentKind(s.kind))+" · "+new Date(s.started).toLocaleDateString(BridgeI18n.locale);
  content.append(node("p",cardTime(s.started)+" → "+(s.ended?cardTime(s.ended):t("In progress")),"small"));
  if(s.clock_status!=="synchronized"||status.clock_status!=="synchronized")content.append(node("p",t("Approximate times · the bridge clock is not verified. Readings remain ordered by sequence."),"notice small"));
  const stats=node("dl",undefined,"session-stat-strip");
  for(const [label,value]of [["Kept",s.frames_accepted],["Rejected",s.frames_rejected],["Exposure",analysis.exposure.observed_seconds],["Pointing failures",s.pointing_failures]])sessionFact(stats,label,value===undefined||value===null?"—":sessionNumber(value)+(label==="Exposure"?" s":""));
  content.append(stats,node("p",t("Frame counts are recorded maxima. Exposure counts only observed kept-frame increments with an unchanged recorded exposure length."),"small"));
  if(!analysis.exposure.complete)content.append(node("p",t("Observed exposure only · missing baselines, setting changes or counter resets prevent a complete total."),"small"));
  if(analysis.exposure.latest_seconds!==null)content.append(node("p",t("Last exposure length: {seconds} s",{seconds:sessionNumber(analysis.exposure.latest_seconds)}),"small"));
  const chart=sessionSection("Night readings");
  chart.append(node("p",t("All retained session frame readings. Each mark spans the lowest and highest readings in a time interval."),"small"));
  if(analysis.limited)chart.append(node("p",t("This session exceeds the chart work limit. Download an export to inspect its retained history."),"notice small"));
  if(!analysis.history_available)chart.append(node("p",t("Earlier history is unavailable on this bridge."),"notice small"));
  if(page.gap||s.gaps.length)chart.append(node("p",t("Some journal records are missing. Lines do not bridge known gaps."),"notice small"));
  chart.append(sessionChart(analysis,s));content.append(chart);
  const columns=node("div",undefined,"session-detail-columns"),events=sessionSection("Events"),table=node("table",undefined,"session-events");
  events.append(node("p",t("Recent events (up to 500 readings and lifecycle records). Download JSON for all retained events."),"small"));
  const head=node("thead"),heading=node("tr");for(const label of ["Time","Event"])heading.append(node("th",t(label)));head.append(heading);table.append(head);
  const body=node("tbody"),rows=[];
  for(const event of page.events)rows.push({seq:event.seq,time:event.type==="session.started"?s.started:s.ended,text:t(event.type==="session.started"?"Session started":"Session ended")});
  const labels={"operation.kind":"Operation","operation.state":"State","target.name":"Target","error.code":"Error","heater.state":"Heater","filter.name":"Filter"},prior={};
  for(const row of samples){if(!labels[row.metric]||prior[row.metric]===row.value)continue;prior[row.metric]=row.value;rows.push({seq:row.seq,time:row.utc_corrected||row.utc,text:t(labels[row.metric])+" · "+(row.metric==="target.name"?row.value:t(cardEnums[row.value]||({pointing_failed:"Pointing failed"}[row.value])||"Other recorded value"))});}
  rows.sort((a,b)=>a.seq-b.seq);
  for(const row of rows){const tr=node("tr");tr.append(node("td",row.time?cardTime(row.time):"—"),node("td",row.text));body.append(tr);}
  table.append(body);events.append(rows.length?table:node("p",t("No events in this reading window."),"small"));columns.append(events);
  const details=sessionSection("Rejection reasons"),max=Math.max(1,...Object.values(s.reject_reasons));
  const reasonNames={cloud:"Clouds",clouds:"Clouds",tracking:"Tracking",star_shape:"Star shape",quality:"Quality",other:"Other"};
  for(const [reason,count]of Object.entries(s.reject_reasons)){const row=node("div",undefined,"session-rejection"),bar=node("meter");bar.min=0;bar.max=max;bar.value=count;bar.setAttribute("aria-label",t(reasonNames[reason]||"Other rejection reason"));row.append(node("span",t(reasonNames[reason]||"Other rejection reason")),bar,node("strong",sessionNumber(count)));details.append(row);}
  if(!Object.keys(s.reject_reasons).length)details.append(node("p",t("No rejection reasons recorded."),"small"));
  details.append(node("h3",t("Environment")),node("p",t("Last readings in this window."),"small"));const environment=node("dl",undefined,"session-facts");
  for(const metric of ["env.temperature_c","env.humidity_pct","env.dew_point_c","device.temperature_c","battery.percent"]){const row=latest(metric);if(row)sessionFact(environment,cardLabels[metric],sessionNumber(row.value)+" "+cardUnits[metric]+" · "+cardTime(row.utc_corrected||row.utc));}
  details.append(environment.children.length?environment:node("p",t("No environment readings in this window."),"small"));columns.append(details);content.append(columns);
  const exported=sessionSection("Export");exported.append(node("p",t("CSV contains one row per retained reading. JSON also includes session metadata, events and paired-reader names."),"small"));
  const actions=node("div",undefined,"table-actions");
  for(const format of ["csv","json"]){const link=node("a",t(format==="csv"?"Download CSV":"Download JSON"),"session-download "+(format==="json"?"secondary":"primary"));link.id="session-download-"+format;link.href="/api/telemetry/sessions/"+s.id+"/export?format="+format;link.download="";actions.append(link);}
  const remove=node("button",t("Delete this session"),"text-button");remove.id="session-delete-one";remove.type="button";remove.addEventListener("click",()=>openSessionDelete(s.id,remove));actions.append(remove);exported.append(actions);content.append(exported);if(focused)$(focused)?.focus({preventScroll:true});
}
function sessionSvg(tag,attributes={},text){const element=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const [key,value]of Object.entries(attributes))element.setAttribute(key,String(value));if(text!==undefined)element.textContent=text;return element;}
function sessionChart(analysis,summary){
  const wrapper=node("div"),points=analysis.points.map(row=>({...row,seq:row.from_seq,value:row.high}));
  if(analysis.limited)return wrapper;
  if(!points.length){wrapper.append(node("p",t("No frame readings."),"small"));return wrapper;}
  // One session belongs to one boot. Monotonic receipt time avoids wall-clock reversals.
  const first=Math.min(...points.map(p=>p.mono_ms)),last=Math.max(...points.map(p=>p.mono_ms)),max=Math.max(1,...points.map(p=>p.value));
  const chartWidth=window.innerWidth<=700?320:760,right=chartWidth-22;
  const x=p=>48+(p.mono_ms-first)/Math.max(1,last-first)*(right-48),y=p=>190-p.value/max*164;
  const svg=sessionSvg("svg",{viewBox:"0 0 "+chartWidth+" 230",role:"img","aria-label":t("Kept and rejected frames over time"),class:"session-chart"});
  svg.append(sessionSvg("title",{},t("Kept and rejected frames over time")));
  const defs=sessionSvg("defs"),pattern=sessionSvg("pattern",{id:"session-reader-hatch",patternUnits:"userSpaceOnUse",width:8,height:8});pattern.append(sessionSvg("path",{d:"M-2 2L2-2M0 8L8 0M6 10L10 6",stroke:"currentColor","stroke-width":1,opacity:.3}));defs.append(pattern);svg.append(defs);
  const ordered=[...points].sort((a,b)=>a.seq-b.seq),readers=analysis.reader_coverage.fetched;
  let hatched=false;
  for(let i=1;i<ordered.length;i++){const left=ordered[i-1],right=ordered[i];if(analysis.reader_coverage.unknown_before_seq===null||left.seq<=analysis.reader_coverage.unknown_before_seq||readers.some(r=>r.from_seq<=right.to_seq&&r.to_seq>=left.seq))continue;
    if(right.mono_ms<=left.mono_ms)continue;hatched=true;svg.append(sessionSvg("rect",{x:x(left),y:26,width:Math.max(1,x(right)-x(left)),height:164,fill:"url(#session-reader-hatch)",class:"session-reader-gap"}));}
  for(const value of [0,max]){svg.append(sessionSvg("line",{x1:48,x2:right,y1:190-value/max*164,y2:190-value/max*164,stroke:"currentColor",opacity:.2,"stroke-dasharray":"3 4"}),sessionSvg("text",{x:40,y:194-value/max*164,"text-anchor":"end"},sessionNumber(value)));}
  for(const metric of ["stack.frames_accepted","stack.frames_rejected"]){let prior=null,path="";
    for(const point of points.filter(row=>row.metric===metric)){const gap=prior&&summary.gaps.some(g=>g.from_seq<=point.to_seq&&g.to_seq>=prior.seq);path+=(prior&&!gap?" L":" M")+x(point)+" "+y(point);svg.append(sessionSvg("line",{x1:x(point),x2:x(point),y1:y(point),y2:y({value:point.low}),"stroke-width":3,class:metric.endsWith("accepted")?"session-kept":"session-rejected"}));prior=point;}
    svg.append(sessionSvg("path",{d:path,fill:"none","stroke-width":2.5,class:metric.endsWith("accepted")?"session-kept":"session-rejected"}));}
  for(const [point,anchor]of [[ordered[0],"start"],[ordered.at(-1),"end"]])svg.append(sessionSvg("text",{x:x(point),y:216,"text-anchor":anchor},cardTime(point.utc_corrected||point.utc)));
  wrapper.append(svg,node("p",t("Kept — solid line · Rejected — dashed line"),"small"));
  if(hatched)wrapper.append(node("p",t("Hatched: no paired-reader fetch recorded for these ranges."),"small"));
  wrapper.append(node("p",t("Hatching uses exact recorded fetch ranges. Legacy or coarsened audit coverage stays unknown. Fetch history does not indicate whether an app was open."),"small"));return wrapper;
}
function renderSessionReaders(){
  const apps=(state?.paired_clients||[]).filter(row=>row.scopes.includes("telemetry")||row.scopes.includes("admin"));
  $("session-reader-apps").replaceChildren(...(apps.length?apps.map(row=>node("p",row.name,"small")):[node("p",t("No paired apps are allowed to read sessions."),"small")]));
}
function renderSessionSettingsFacts(){
  if(!sessionSettingsData)return;const s=sessionSettingsData,facts=$("session-storage-facts");facts.replaceChildren();
  sessionFact(facts,"Used",t("{size} MiB · {count} sessions",{size:sessionNumber(s.used_bytes/1024**2),count:s.sessions_count}));
  sessionFact(facts,"Clock",t(s.clock_status==="synchronized"?"Synchronized":"Unverified · approximate times"));
  if(!s.persistent)sessionFact(facts,"Storage",t("Temporary storage · history may be lost on restart"));renderSessionReaders();
}
async function loadSessionSettings(){
  const epoch=++sessionPageEpoch;sessionSettingsLoading=false;sessionSettingsData=null;sessionSettingsCursor=0;
  $("session-retention-save").disabled=true;$("session-delete-all").disabled=true;$("session-settings-list").replaceChildren();$("session-retention-feedback").textContent=t("Loading session…");
  try{const data=await sessionRequest("GET","");if(!sessionPageValid(epoch))return;sessionSettingsData=data;
    $("session-retention-nights").value=data.retention.nights;$("session-retention-size").value=data.retention.max_bytes/1024**2;
    renderSessionSettingsFacts();$("session-retention-feedback").textContent="";$("session-retention-save").disabled=false;$("session-delete-all").disabled=false;await loadSettingsSessions(epoch);
  }catch(failure){if(sessionPageValid(epoch))$("session-retention-feedback").textContent=failure.message;}
}
async function loadSettingsSessions(epoch=sessionPageEpoch){
  if(sessionSettingsLoading)return;sessionSettingsLoading=true;$("session-settings-more").disabled=true;
  try{const data=await sessionRequest("GET","/sessions?"+new URLSearchParams({after:sessionSettingsCursor,limit:20}));if(!sessionPageValid(epoch))return;
    for(const row of data.sessions){const item=node("p",undefined,"session-history-row");item.append(sessionDateNode("span",row.started,row.profile_id,row.kind),sessionOpenLink(row.id));$("session-settings-list").append(item);}
    if(!data.sessions.length&&sessionSettingsCursor===0)$("session-settings-list").append(sessionCopy("p","No sessions yet. Connect an instrument with session data enabled; recorded activity will appear here.","small"));
    sessionSettingsCursor=data.next_cursor;$("session-settings-more").hidden=!data.has_more;
  }catch(failure){if(sessionPageValid(epoch))$("session-retention-feedback").textContent=failure.message;}
  finally{if(epoch===sessionPageEpoch){sessionSettingsLoading=false;$("session-settings-more").disabled=false;}}
}
function openSessionDelete(id,opener){
  const revision=id?sessionPageData?.status.config_revision:sessionSettingsData?.config_revision;if(!revision||!accessReady)return;
  sessionDeleteTarget={id,revision,epoch:sessionPageEpoch};sessionDeleteOpener=opener.id;
  $("session-delete-description").dataset.i18n=id?"Delete this session and its readings?":"Delete every recorded session and its readings?";$("session-delete-description").textContent=t($("session-delete-description").dataset.i18n);$("session-delete-feedback").textContent="";$("session-delete-confirm").disabled=false;
  $("session-delete-dialog").showModal();$("session-delete-cancel").focus();
}
function setupSessionPage(){
  $("session-page-refresh").addEventListener("click",loadSessionPage);
  $("session-retention-reload").addEventListener("click",loadSessionSettings);
  $("session-settings-more").addEventListener("click",()=>loadSettingsSessions());
  $("session-delete-all").addEventListener("click",event=>openSessionDelete(null,event.currentTarget));
  $("session-retention-form").addEventListener("submit",async event=>{
    event.preventDefault();if(!sessionSettingsData)return;const epoch=sessionPageEpoch;$("session-retention-save").disabled=true;
    try{const data=await sessionRequest("PATCH","/settings",{retention_nights:Number($("session-retention-nights").value),max_bytes:Math.round(Number($("session-retention-size").value)*1024**2)},sessionSettingsData.config_revision);
      if(sessionPageValid(epoch)){sessionSettingsData=data;renderSessionSettingsFacts();$("session-retention-feedback").textContent=t("Session retention saved.");}
    }catch(failure){if(sessionPageValid(epoch))$("session-retention-feedback").textContent=t("Settings were not saved. Your draft is kept. Reload settings before trying again.");}
    finally{if(epoch===sessionPageEpoch)$("session-retention-save").disabled=false;}
  });
  const dialog=$("session-delete-dialog");for(const button of [$("session-delete-cancel"),dialog.querySelector(".dialog-close")])button.addEventListener("click",()=>dialog.close());
  dialog.addEventListener("close",()=>{if(dialog.open)return;sessionDeleteTarget=null;$(sessionDeleteOpener)?.focus({preventScroll:true});sessionDeleteOpener=null;});
  $("session-delete-confirm").addEventListener("click",async()=>{
    const target=sessionDeleteTarget;if(!target||!sessionPageValid(target.epoch))return;$("session-delete-confirm").disabled=true;
    try{await sessionRequest("DELETE","/sessions"+(target.id?"/"+target.id:""),{},target.revision);if(!sessionPageValid(target.epoch))return;
      dialog.close();cardTelemetry=null;instrumentSignature="";if(target.id){sessionPageData=null;$("session-page-content").replaceChildren();$("session-page-feedback").textContent=t("Session deleted.");}else await loadSessionSettings();
    }catch(failure){if(sessionPageValid(target.epoch)&&dialog.open)$("session-delete-feedback").textContent=t("Session data was not deleted. Close this dialog and refresh before trying again.");}
  });
  document.addEventListener("bridge:localechange",()=>{renderSessionPage();renderSessionSettingsFacts();for(const element of document.querySelectorAll("[data-session-started]"))renderSessionDate(element);});
  let resize;window.addEventListener("resize",()=>{cancelAnimationFrame(resize);resize=requestAnimationFrame(renderSessionPage);});
}
