/* Optional local HTTPS lifecycle. No trust installation, redirects or private storage. */
(() => {
  "use strict";
  const $=id=>document.getElementById(id),t=(key,values)=>BridgeI18n.t(key,values);
  let callbacks={},active=false,context=null,epoch=0,polling=false,value=null,submitting=false,feedback="";
  $("https-recovery-public").hidden=location.protocol!=="https:";$("https-recovery-public").href="http://"+location.hostname;
  const secure=()=>location.protocol==="https:"&&window.isSecureContext;
  const installed=()=>secure()&&(matchMedia("(display-mode: standalone)").matches||navigator.standalone===true);
  const detected=/iPad|iPhone|iPod/.test(navigator.userAgent)||(navigator.platform==="MacIntel"&&navigator.maxTouchPoints>1)?"ios":/Android/.test(navigator.userAgent)?"android":/Mac/.test(navigator.platform)?"macos":/Win/.test(navigator.platform)?"windows":/Linux/.test(navigator.platform)?"linux":"";
  $("setup-os").value=detected;
  function openGuide(){
    $("https-settings").scrollIntoView({behavior:matchMedia("(prefers-reduced-motion: reduce)").matches?"auto":"smooth",block:"start"});
    $("setup-os").focus({preventScroll:true});
  }
  for(const link of document.querySelectorAll("[data-setup-link]"))link.addEventListener("click",event=>{event.preventDefault();openGuide();});
  for(const link of document.querySelectorAll("[data-notifications-link]"))link.addEventListener("click",event=>{event.preventDefault();$("pwa-settings").scrollIntoView({block:"start"});$("push-enable").focus({preventScroll:true});});
  $("setup-os").addEventListener("change",render);
  const set=(id,text)=>{if($(id).textContent!==text)$(id).textContent=text;};
  const errors={SYSTEM_SHUTTING_DOWN:"The bridge is stopping or a reboot is already scheduled.",HTTPS_UNSUPPORTED:"This OS does not provide the required certificate helper. An app update cannot install it.",HTTPS_BUSY:"A secure access operation is already running.",HTTPS_REFRESH_LIMITED:"Wait before refreshing certificate diagnostics again.",UPDATE_IN_PROGRESS:"An application update or recovery is in progress.",REAUTHENTICATION_FAILED:"The current web interface password is incorrect.",REAUTHENTICATION_LIMITED:"Too many password checks. Try again in a minute.",HTTPS_CLOCK_UNVERIFIED:"Synchronize current UTC or independently verify it for this operation.",HTTPS_OPERATION_FAILED:"The certificate operation failed. Refresh diagnostics before retrying.",HTTPS_CERTIFICATE_UNAVAILABLE:"A validated public CA certificate is not available."};
  function render(){
    const identity=value?.identity,operation=value?.operation,running=operation?.state==="running",supported=!!value?.supported;
    set("https-support",!value?t("Checking secure access support…"):!supported?t("This OS does not provide the required certificate helper. An app update cannot install it."):value.diagnostics_stale?t("Certificate diagnostics need refreshing. HTTP recovery remains available."):t("Secure access is optional. Trust on this browser has not been verified."));
    set("https-age",value?.diagnostics_age_seconds!==null&&value?.diagnostics_age_seconds!==undefined?t("Diagnostics observed {seconds} seconds ago.",{seconds:value.diagnostics_age_seconds}):"");
    const states={unconfigured:"Not configured",valid:"Certificate chain valid at last diagnostic check",invalid:"Certificate identity is invalid",hostname_mismatch:"Certificate hostname differs from the bridge name",not_yet_valid:"Certificate is not yet valid according to the bridge clock",ca_expired:"CA expired; reset and renewed client trust are required",leaf_expired:"Server certificate expired; renewal requires verified current UTC"};
    set("https-identity",identity?(identity.hostname?identity.hostname+" · ":"")+t(states[identity.state]||"Certificate identity is invalid"):"—");
    set("https-listener",value?.listener?.ready?t("HTTPS listener ready. Browser trust still needs verification."):t("HTTPS listener unavailable. Use HTTP recovery."));
    set("https-ca-expiry",identity?.ca_expires||"—");set("https-leaf-expiry",identity?.leaf_expires||"—");
    set("https-issued",identity?.issuance_clock_evidence==="chrony-synchronized"?t("Chrony synchronized at last issuance"):identity?.issuance_clock_evidence==="owner-confirmed-current-utc"?t("Owner-confirmed UTC at last issuance (not NTP proof)"):"—");
    set("https-clock",identity?.current_clock==="synchronized"?t("Synchronized when diagnostics were refreshed"):t("Synchronization unverified at last diagnostic check"));
    set("https-fingerprint",identity?.fingerprint_sha256?.match(/../g).join(":")||"—");
    const platform=$("setup-os").value;
    $("setup-steps").hidden=!platform||!value?.listener?.ready;
    for(const section of document.querySelectorAll("[data-setup-os]"))section.hidden=section.dataset.setupOs!==platform;
    for(const section of document.querySelectorAll("[data-install-os]"))section.hidden=section.dataset.installOs!==platform||installed();
    $("setup-secure-done").hidden=!secure();$("setup-installed").hidden=!installed();
    const prepareNeeded=supported&&identity&&(!identity.configured||!identity.enabled&&identity.state==="valid");
    $("https-quick-form").hidden=!prepareNeeded;
    $("https-quick-submit").disabled=!active||running||submitting;
    const ready=!!value?.listener?.ready;
    $("setup-bridge-state").hidden=ready;
    set("setup-bridge-state",!value?t("Checking secure access support…"):!supported?t("HTTPS is not available on this bridge. Update its system image before setting up this device."):prepareNeeded?t("First, prepare HTTPS on the bridge using your administrator password."):running?t("Preparing secure access…"):t("The bridge certificate needs attention. Open Advanced certificate settings."));
    $("https-profile").hidden=!supported||!identity?.fingerprint_sha256||platform!=="ios";
    $("https-download").hidden=!supported||!identity?.fingerprint_sha256||!platform||platform==="ios";
    $("https-open").hidden=!value?.listener?.ready||secure();$("https-open").href=value?.listener?.url||"/";
    $("https-http").href=callbacks.httpUrl?.()||"/";
    const allowed={setup:!identity?.configured,renew:!!identity?.configured&&!["ca_expired","hostname_mismatch","not_yet_valid"].includes(identity.state),enable:!!identity?.configured&&!identity.enabled&&identity.state==="valid",disable:!!identity?.configured&&identity.enabled,reset:!!identity?.configured};
    for(const option of $("https-action").options)option.disabled=!supported||!allowed[option.value];
    const action=$("https-action").value,issuance=["setup","renew"].includes(action);
    $("https-clock-option").hidden=!issuance;$("https-utc-fields").hidden=!issuance||!$("https-manual-clock").checked;
    $("https-utc").required=issuance&&$("https-manual-clock").checked;
    $("https-reset-option").hidden=action!=="reset";$("https-reset-confirm").required=action==="reset";
    $("https-submit").disabled=!active||!supported||running||submitting||!allowed[action];
    $("https-refresh").disabled=!active||!supported||running||submitting;
    if(feedback)set("https-operation",feedback);
    else if(operation)set("https-operation",operation.state==="running"?t("Certificate operation is running. Accepted does not mean HTTPS is ready."):operation.state==="failed"?t(errors[operation.code]||errors.HTTPS_OPERATION_FAILED):t("Certificate operation completed. Check listener readiness and client trust separately."));
  }
  async function refresh(){
    if(!active||polling)return;polling=true;const generation=epoch;
    try{const result=await callbacks.api("/api/https");if(active&&generation===epoch){value=result;render();}}
    catch(_){/* Auth/offline lifecycle owns private reset; retry only while active. */}
    finally{if(generation===epoch)polling=false;}
  }
  function suspend(){active=false;epoch++;polling=false;submitting=false;}
  function reset(){
    suspend();context=null;value=null;submitting=false;feedback="";
    $("https-password").value="";$("https-quick-password").value="";set("https-quick-feedback","");$("https-utc").value="";$("https-manual-clock").checked=false;$("https-reset-confirm").checked=false;
    $("https-action").value="setup";set("https-operation","");render();
  }
  window.BridgeSecure={configure(options){callbacks=options;},start(next){if(context!==null&&context!==next)reset();context=next;if(active)return;active=true;refresh();},suspend,reset,refresh};
  $("https-quick-form").addEventListener("submit",async event=>{
    event.preventDefault();if(!active||submitting)return;
    const identity=value?.identity;
    if(!identity||identity.configured&&identity.enabled)return;
    const data={action:identity.configured?"enable":"setup",current_password:$("https-quick-password").value};
    $("https-quick-password").value="";submitting=true;set("https-quick-feedback","");render();const generation=epoch;
    try{const result=await callbacks.api("/api/https",data);if(active&&generation===epoch){value=result;render();}}
    catch(error){if(active&&generation===epoch)set("https-quick-feedback",t(errors[error.code]||errors.HTTPS_OPERATION_FAILED));}
    finally{if(generation===epoch){submitting=false;render();}}
  });
  $("https-action").addEventListener("change",()=>{$("https-reset-confirm").checked=false;feedback="";render();});
  $("https-manual-clock").addEventListener("change",render);
  $("https-refresh").addEventListener("click",async()=>{
    feedback="";submitting=true;render();const generation=epoch;
    try{const result=await callbacks.api("/api/https",{action:"refresh"});if(active&&generation===epoch){value=result;render();}}
    catch(error){if(active&&generation===epoch)feedback=t(errors[error.code]||errors.HTTPS_OPERATION_FAILED);}
    finally{if(generation===epoch){submitting=false;render();}}
  });
  $("https-form").addEventListener("submit",async event=>{
    event.preventDefault();const action=$("https-action").value;
    const data={action,current_password:$("https-password").value};$("https-password").value="";
    if(["setup","renew"].includes(action)&&$("https-manual-clock").checked){
      const text=$("https-utc").value;
      if(!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(text)||!Number.isFinite(Date.parse(text))||new Date(Date.parse(text)).toISOString().replace(".000Z","Z")!==text){feedback=t("Enter independently verified UTC in the displayed format.");render();return;}
      data.owner_confirmed_utc=Math.floor(Date.parse(text)/1000);
    }
    if(action==="reset")data.confirm_reset=$("https-reset-confirm").checked;
    $("https-manual-clock").checked=false;$("https-utc").value="";$("https-reset-confirm").checked=false;
    feedback="";submitting=true;render();const generation=epoch;
    try{const result=await callbacks.api("/api/https",data);if(active&&generation===epoch){value=result;render();}}
    catch(error){if(active&&generation===epoch)feedback=t(errors[error.code]||errors.HTTPS_OPERATION_FAILED);}
    finally{if(generation===epoch){submitting=false;render();}}
  });
  document.addEventListener("bridge:localechange",render);setInterval(refresh,5000);render();
})();
