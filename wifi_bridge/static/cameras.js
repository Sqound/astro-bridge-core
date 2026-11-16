"use strict";
// Attached-camera UI. Bridge endpoint leases never transfer Alpaca ownership.
let cameraEpoch=0,cameraLoading=false,cameraRows=[],cameraRevision=null,cameraSettings=null,cameraDirty=false,cameraPending=false,cameraSignature="",cameraRelease=null,cameraProfile=null;
function resetCameras(){cameraEpoch++;cameraLoading=false;cameraRows=[];cameraRevision=null;cameraSettings=null;cameraDirty=false;cameraPending=false;cameraSignature="";cameraRelease=null;$("camera-cards").replaceChildren();$("camera-table-body").replaceChildren();for(const id of ["camera-feedback","camera-release-feedback"])localMessage(id,"");for(const id of ["camera-release-description","camera-alpaca-address"])$(id).textContent="";$("camera-release-dialog").close();$("camera-profile-dialog").close();cameraProfile=null;$("camera-profile-fields").replaceChildren();localMessage("camera-profile-feedback","");}
async function cameraRequest(path,method="GET",body,revision=cameraRevision){
 const epoch=cameraEpoch,auth=statusEpoch,abort=new AbortController(),timeout=setTimeout(()=>abort.abort(),1500);
 try{
  const response=await fetch('/api/cameras'+path,{method,credentials:"same-origin",cache:"no-store",signal:abort.signal,...(body?{headers:{"Content-Type":"application/json","X-CSRF-Token":csrf,"If-Match":'"'+revision+'"'},body:JSON.stringify(body)}:{})});
  const value=await response.json();if(epoch!==cameraEpoch||auth!==statusEpoch||!accessReady)throw new Error(t("Fresh authenticated status is required before changing settings."));
  if(!response.ok){if(response.status===401)showLogin();throw new Error(t(value.error?.message||value.error||"Action failed."));}return value;
 }finally{clearTimeout(timeout);}
}
async function loadCameras(){
 if(!accessReady||cameraLoading||cameraPending)return;cameraLoading=true;const epoch=cameraEpoch;
 try{const [rows,settings]=await Promise.all([cameraRequest(""),cameraRequest("/settings")]);if(epoch!==cameraEpoch)return;cameraRows=rows;cameraSettings=settings.settings;if(!cameraDirty)cameraRevision=settings.config_revision;renderCameras();}
 catch(failure){if(epoch===cameraEpoch){cameraRows=[];cameraSignature="";renderCameras();}}
 finally{if(epoch===cameraEpoch)cameraLoading=false;}
}
function cameraState(row){
 if(row.profile?.probing)return "Detecting camera";
 if(row.profile&&!row.profile.ready)return "Profile needed";
 if(row.indi?.state==="ready")return "Ready";
 if(row.indi?.state==="unavailable")return "INDI unavailable";
 const discovery=row.discovery?.state;
 if(discovery==="driver_missing")return "Driver missing";
 if(discovery==="unsupported")return "Camera unsupported";
 if(["detected","preparing"].includes(discovery))return "Detected";
 if(row.discovery?.power_unstable)return "Unstable power";
 if(row.refused&&row.owner)return "Busy";
 if(row.owner)return "In use";
 const only24=state?.devices?.length&&state.devices.filter(d=>!d.missing).every(d=>(d.bands||[]).length===1&&String(d.bands[0])==="2.4");
 if(only24&&row.sensor&&row.sensor.width*row.sensor.height*(row.sensor.bits>8?2:1)>3*1024*1024)return "Heavy images";
 return row.alpaca?"Ready":"Detected";
}
function cameraEndpoint(row){return row.alpaca?`http://${location.hostname}:${row.alpaca.port}`:null;}
function cameraOwner(owner){return [t("Alpaca ClientID {id}",{id:owner.client_id}),owner.dhcp_name,owner.address].filter(Boolean).join(" · ");}
function renderCameras(){
 $("camera-demo-controls").hidden=!state?.demo;
 if(!cameraPending)$("camera-demo-plugged").checked=cameraRows.some(row=>row.id==="demo-camera");$("camera-demo-plugged").disabled=cameraPending;
 if(cameraSettings&&!cameraDirty){$("camera-serve").checked=cameraSettings.serve;$("camera-discover").checked=cameraSettings.discover;$("camera-indi").checked=!!cameraSettings.indi;}
 for(const id of ["camera-serve","camera-discover","camera-indi"])$(id).disabled=cameraPending;
 $("camera-save").disabled=cameraPending||!cameraSettings;$("camera-reload").disabled=cameraPending;
 const signature=JSON.stringify([cameraRows,BridgeI18n.locale,state?.devices,cameraSettings?.serve]);
 if(signature===cameraSignature)return;cameraSignature=signature;
 const active=document.activeElement?.id,cards=[],rows=[];
 for(const row of cameraRows){
  const label=cameraState(row),card=node("article",undefined,"card camera-card");card.dataset.cameraState=label;card.dataset.cameraId=row.id;
  const heading=node("div",undefined,"card-heading");heading.append(node("h2",row.name),node("span",t(label),"badge"));card.append(heading);
  const sensor=row.sensor;card.append(node("p",t("Camera")+" · "+row.driver+(sensor?` · ${sensor.width} × ${sensor.height} · ${sensor.bits} ${t("bit")}`:""),"small"));
  const copy={"Detecting camera":"Detecting camera information. Please wait.","Profile needed":"Complete the camera profile before serving it.",Detected:cameraSettings?.serve?"Preparing the camera…":"Camera serving is off.",Ready:"Served to your apps · waiting for an app","Driver missing":"This bridge has no driver for this camera.","Camera unsupported":"This camera needs supported capture settings and owner calibration before it can be served.","Unstable power":"The camera disconnected repeatedly. Check its cable and use a powered USB hub.","Busy":"Another Alpaca client was refused. One app can use the camera at a time.","Heavy images":row.defaults?.bin===2?"Large images over 2.4 GHz. Binning 2 is the initial default; apps can choose supported settings.":"Large images over 2.4 GHz may transfer slowly. This camera keeps its supported binning."};
  if(row.driver==="zwo_asi"){
   copy["Driver missing"]="Install the ZWO ASI SDK on the bridge, then reconnect the camera.";
   copy["Camera unsupported"]="Connect a single ASI camera and check that the driver supports its capture format and sensor size.";
  }
  if(copy[label]&&row.indi?.state!=="ready")card.append(node("p",t(copy[label]),["Driver missing","Camera unsupported","Unstable power","Busy","Heavy images"].includes(label)?"notice":"small"));
  if(row.owner)card.append(node("p",t("Used by {owner}",{owner:cameraOwner(row.owner)})));
  if(row.refused)card.append(node("p",t("Refused: {client}",{client:cameraOwner(row.refused)}),"small"));
  if(row.owner&&row.current)card.append(node("p",t("{state} · {seconds} s · bin {bin}",{state:t(row.state),seconds:row.current.exposure_seconds,bin:row.current.bin}),"small"));
  const endpoint=cameraEndpoint(row);if(endpoint){const line=node("p",undefined,"camera-address");line.append(node("span",t("For your apps")),node("code",endpoint+" · "+t("Alpaca camera {number}",{number:row.alpaca.device_number})));card.append(line);}
  const indiAddress=row.indi?.state==="ready"?t("INDI at {address}",{address:location.hostname+":"+row.indi.port}):null;
  if(indiAddress)card.append(node("code",indiAddress));
  const actions=node("div",undefined,"table-actions"),settings=node("a",t("Camera settings"));settings.id="camera-settings-"+row.id;settings.href="#configuration/cameras";actions.append(settings);
  if(row.profile){const profile=node("button",t(row.profile.ready?"Edit profile":"Set up profile"),"text-button");profile.id="camera-profile-"+row.id;profile.type="button";profile.disabled=!!row.profile.probing;profile.addEventListener("click",()=>void openCameraProfile(row,profile.id));actions.append(profile);}
  if(row.owner){const release=node("button",t("Let this app use it"),"text-button");release.id="camera-release-"+row.id;release.type="button";release.addEventListener("click",()=>openCameraRelease(row,release.id));actions.append(release);}
  card.append(actions);cards.push(card);
  const tr=node("tr");for(const value of [row.name,row.driver,indiAddress||(row.alpaca?t("Alpaca camera {number}",{number:row.alpaca.device_number}):t(label)),row.defaults?t("bin {bin} · gain {gain}",{bin:row.defaults.bin,gain:row.defaults.gain}):"—"])tr.append(node("td",value));rows.push(tr);
 }
 $("camera-cards").replaceChildren(...cards);$("camera-table-body").replaceChildren(...rows);$("camera-empty").hidden=!!rows.length;
 const endpoints=[...new Set(cameraRows.map(cameraEndpoint).filter(Boolean))];$("camera-alpaca-address").textContent=endpoints.join(" · ")||t("No camera endpoint on this connection.");
 if(cameraProfile){const row=cameraRows.find(row=>row.id===cameraProfile.id);if(!row||row.profile?.attachment_generation!==cameraProfile.generation){cameraProfile.stale=true;localMessage("camera-profile-feedback","Camera attachment changed. Reopen the profile to load current values.");$("camera-profile-save").disabled=true;}else if(!cameraProfile.stale&&cameraProfile.loaded){cameraProfile.probing=!!row.profile.probing;$("camera-profile-save").disabled=cameraProfile.probing||cameraPending;if(cameraProfile.probing)localMessage("camera-profile-feedback","Detecting camera information. Please wait.");else if(cameraProfile.wasProbing)localMessage("camera-profile-feedback","");cameraProfile.wasProbing=cameraProfile.probing;}}
 if(active?.startsWith("camera-release-")||active?.startsWith("camera-profile-"))$(active)?.focus({preventScroll:true});
}
const cameraProfileFields=[
 ["name","Camera name","text"],["width","Sensor width (pixels)","integer"],["height","Sensor height (pixels)","integer"],
 ["pixel_um_x","Pixel width (µm)","number"],["pixel_um_y","Pixel height (µm)","number"],["bits","Bit depth","bits"],
 ["exposure_min","Minimum exposure (seconds)","number"],["exposure_max","Maximum exposure (seconds)","number"],["bayer_pattern","Sensor color pattern","pattern"]
];
async function openCameraProfile(row,opener){
 if(cameraPending||row.profile.probing)return;
 const session={id:row.id,generation:row.profile.attachment_generation,revision:cameraRevision,opener,stale:false};cameraProfile=session;
 $("camera-profile-fields").replaceChildren();localMessage("camera-profile-feedback","");$("camera-profile-save").disabled=true;
 $("camera-profile-dialog").showModal();$("camera-profile-cancel").focus();
 try{
  const profile=await cameraRequest('/'+encodeURIComponent(row.id)+'/profile');
  if(cameraProfile!==session||session.stale)return;
  if(profile.probing||cameraRows.find(item=>item.id===session.id)?.profile?.probing){session.stale=true;throw new Error(t("Detecting camera information. Please wait. Reopen the profile when detection finishes."));}
  if(profile.attachment_generation!==session.generation)throw new Error(t("Camera attachment changed. Reopen the profile to load current values."));
  for(const [key,label,type] of cameraProfileFields){
   const wrapper=node("div"),id="camera-profile-field-"+key,caption=node("label",t(label));caption.htmlFor=id;caption.dataset.i18n=label;let input;
   if(type==="bits"||type==="pattern"){
    input=node("select");const choices=type==="bits"?[["","Choose…"],["8","8"],["16","16"]]:[["__missing__","Choose…"],["","Monochrome"],["RGGB","RGGB"],["BGGR","BGGR"],["GRBG","GRBG"],["GBRG","GBRG"]];
    for(const [value,text] of choices){const option=node("option",t(text));option.value=value;option.dataset.i18n=text;input.append(option);}
   }else{input=node("input");input.type=type==="text"?"text":"number";if(type!=="text"){input.min=type==="integer"?"1":"0.000001";input.step=type==="integer"?"1":"any";}else input.maxLength=64;}
   input.id=id;input.required=type!=="pattern";const has=Object.hasOwn(profile.values,key);input.value=has?String(profile.values[key]):(type==="pattern"?"__missing__":"");
   input.dataset.initialValue=input.value;
   if(type==="pattern"){input.setCustomValidity(input.value==="__missing__"?t("Choose a sensor color pattern."):"");input.addEventListener("change",()=>input.setCustomValidity(input.value==="__missing__"?t("Choose a sensor color pattern."):""));}
   wrapper.append(caption,input);
   if(has&&Object.hasOwn(profile.detected||{},key)&&profile.detected[key]===profile.values[key]){const detected=node("p",t("Detected value"),"small");detected.dataset.i18n="Detected value";input.addEventListener("input",()=>{detected.hidden=input.value!==input.dataset.initialValue;});input.addEventListener("change",()=>{detected.hidden=input.value!==input.dataset.initialValue;});wrapper.append(detected);}
   $("camera-profile-fields").append(wrapper);
  }
  session.loaded=true;$("camera-profile-save").disabled=false;$("camera-profile-field-"+(profile.missing?.[0]||"name"))?.focus();
 }catch(failure){if(cameraProfile===session)localMessage("camera-profile-feedback",failure.message);}
}
async function saveCameraProfile(event){
 event.preventDefault();const session=cameraProfile;if(!session||session.stale||session.probing||cameraPending)return;
 const pattern=$("camera-profile-field-bayer_pattern");pattern.setCustomValidity(pattern.value==="__missing__"?t("Choose a sensor color pattern."):"");if(!event.target.reportValidity())return;
 const values={};for(const [key,,type] of cameraProfileFields){const input=$("camera-profile-field-"+key),value=input.value;if(value!==input.dataset.initialValue)values[key]=type==="integer"||type==="number"||type==="bits"?Number(value):value;}
 if(!Object.keys(values).length){localMessage("camera-profile-feedback","No changes to save.");return;}
 if(Number($("camera-profile-field-exposure_min").value)>Number($("camera-profile-field-exposure_max").value)){localMessage("camera-profile-feedback","Minimum exposure must not exceed maximum exposure.");return;}
 cameraPending=true;$("camera-profile-save").disabled=true;for(const input of $("camera-profile-fields").querySelectorAll("input,select"))input.disabled=true;
 try{
  if(!await refresh()||!accessReady)throw new Error(t("Fresh authenticated status is required before changing settings."));
  await cameraRequest('/'+encodeURIComponent(session.id)+'/profile',"PATCH",{attachment_generation:session.generation,values},session.revision);
  if(cameraProfile===session){$("camera-profile-dialog").close();localMessage("camera-feedback","Camera profile saved.");}
 }catch(failure){if(cameraProfile===session)localMessage("camera-profile-feedback",failure.message);}
 finally{cameraPending=false;if(cameraProfile===session){$("camera-profile-save").disabled=session.stale||session.probing;for(const input of $("camera-profile-fields").querySelectorAll("input,select"))input.disabled=false;}await loadCameras();}
}
function openCameraRelease(row,opener){
 cameraRelease={id:row.id,generation:row.owner.generation,revision:cameraRevision,opener};
 $("camera-release-description").textContent=t("Release {name} used by {owner}? This can interrupt its exposure. The other app must reconnect after release.",{name:row.name,owner:cameraOwner(row.owner)});localMessage("camera-release-feedback","");$("camera-release-dialog").showModal();$("camera-release-cancel").focus();
}
async function saveCameraAction(path,method,body,revision){
 if(cameraPending)return;cameraPending=true;cameraEpoch++;cameraLoading=false;renderCameras();
 try{if(!await refresh()||!accessReady)throw new Error(t("Fresh authenticated status is required before changing settings."));await cameraRequest(path,method,body,revision);if(path==="/settings")cameraDirty=false;return true;}
 catch(failure){localMessage("camera-feedback",failure.message);localMessage("camera-release-feedback",failure.message);return false;}
 finally{cameraPending=false;await loadCameras();renderCameras();}
}
function setupCameras(){
 for(const id of ["camera-serve","camera-discover","camera-indi"])$(id).addEventListener("change",()=>{cameraDirty=true;});
 $("camera-form").addEventListener("submit",async event=>{event.preventDefault();if(await saveCameraAction("/settings","PATCH",{serve:$("camera-serve").checked,discover:$("camera-discover").checked,indi:$("camera-indi").checked},cameraRevision)){cameraDirty=false;renderCameras();localMessage("camera-feedback","Camera settings saved.");}});
 $("camera-reload").addEventListener("click",()=>{cameraDirty=false;void loadCameras();});
 $("camera-demo-plugged").addEventListener("change",()=>void saveCameraAction("/demo","POST",{plugged:$("camera-demo-plugged").checked},cameraRevision));
 $("camera-profile-form").addEventListener("submit",saveCameraProfile);
 $("camera-profile-cancel").addEventListener("click",()=>$("camera-profile-dialog").close());
 $("camera-profile-dialog").addEventListener("close",()=>{const previous=cameraProfile;cameraProfile=null;if(previous)($(previous.opener)||$("camera-settings-"+previous.id))?.focus();});
 $("camera-release-cancel").addEventListener("click",()=>$("camera-release-dialog").close());
 $("camera-release-dialog").addEventListener("close",()=>{if(cameraRelease)($(cameraRelease.opener)||$("camera-settings-"+cameraRelease.id))?.focus();cameraRelease=null;});
 $("camera-release-confirm").addEventListener("click",async()=>{const consent=cameraRelease;if(!consent||cameraPending)return;$("camera-release-confirm").disabled=true;const ok=await saveCameraAction('/'+encodeURIComponent(consent.id)+'/release-owner',"POST",{confirm_release:true,owner_generation:consent.generation},consent.revision);$("camera-release-confirm").disabled=false;if(ok)$("camera-release-dialog").close();});
 document.addEventListener("bridge:localechange",()=>{BridgeI18n.translate($("camera-profile-fields"));const pattern=$("camera-profile-field-bayer_pattern");if(pattern)pattern.setCustomValidity(pattern.value==="__missing__"?t("Choose a sensor color pattern."):"");cameraSignature="";renderCameras();});
}
