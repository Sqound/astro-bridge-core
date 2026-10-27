/** Real service-worker lifecycle against a disposable production Python demo app.
 * Loopback is a browser development secure context, not phone TLS/trust proof.
 */
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {once} from "node:events";
import {cp,mkdir,mkdtemp,readFile,writeFile,rm} from "node:fs/promises";
import {createServer} from "node:net";
import {tmpdir} from "node:os";
import {resolve} from "node:path";
import {fileURLToPath,pathToFileURL} from "node:url";
const root=fileURLToPath(new URL("../",import.meta.url));
const {chromium,request}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:"playwright");
const temporary=await mkdtemp(resolve(tmpdir(),"astro-pwa-")), app=resolve(temporary,"app");
await mkdir(app);await cp(resolve(root,"wifi_bridge"),resolve(app,"wifi_bridge"),{recursive:true,filter:path=>!path.includes("__pycache__")});
// A test-only pre-PWA HTTP contract: no revision, no SW endpoint, plain public
// legacy shell. The production handler and auth/API run unchanged otherwise.
const webPath=resolve(app,"wifi_bridge/web.py");let web=await readFile(webPath,"utf8");
web=web.replace('        body = json.dumps(payload', '        if (Path(__file__).parent.parent / "legacy.flag").exists():\n            shell_revision = None\n            if isinstance(payload, dict):\n                payload = dict(payload)\n                payload.pop("ui_revision", None)\n        body = json.dumps(payload');
web=web.replace('        if path in ASSETS or path == "/sw.js":', '        if (Path(__file__).parent.parent / "legacy.flag").exists():\n            if path == "/":\n                self.reply(200, b"<!doctype html><title>Legacy contract</title><h1>Legacy interface</h1>", "text/html")\n                return\n            if path == "/sw.js":\n                self.reply(404, {"error": "No service worker"})\n                return\n        if path in ASSETS or path == "/sw.js":');
await writeFile(webPath,web);
const output=resolve(root,process.env.UI_ARTIFACTS||"artifacts/p4a-pwa");await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,"127.0.0.1");await once(socket,"listening");const port=socket.address().port;await new Promise(resolve=>socket.close(resolve));
let server,logs="",browser,control;
async function start(){
  logs="";server=spawn("python3",["-m","wifi_bridge","--demo","--demo-radios","3","--state-dir",resolve(temporary,"state"),"--port",String(port)],{cwd:app,stdio:["ignore","pipe","pipe"]});
  server.stdout.on("data",v=>logs+=v);server.stderr.on("data",v=>logs+=v);
  const end=Date.now()+10000;while(!logs.includes("SIMULATION")){if(Date.now()>end||server.exitCode!==null)throw new Error(logs);await new Promise(resolve=>setTimeout(resolve,50));}
}
async function stop(){server.kill("SIGTERM");await once(server,"exit");}
const errors=[],results=[];
try{
  await start();
  browser=await chromium.launch({headless:true,args:["--host-resolver-rules=MAP *.local 127.0.0.1","--no-proxy-server"],...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  const context=await browser.newContext({viewport:{width:1280,height:960}}),page=await context.newPage();page.setDefaultTimeout(15000);page.on("pageerror",e=>errors.push(e.message));
  const url=`http://127.0.0.1:${port}`;
  const wait=async predicate=>{const end=Date.now()+15000;while(!await predicate()){if(Date.now()>end)throw new Error("Condition timeout");await new Promise(resolve=>setTimeout(resolve,100));}};
  const check=async(name,fn)=>{await fn();results.push(name);console.log("PASS",name);};
  const login=async()=>{await page.locator("#login-password").fill("demo-telescope-2026");await page.locator("#login-form button").click();await page.locator("#dashboard").waitFor({state:"visible"});};
  let releaseInitial,initialEntered=false;
  await page.route('**/api/status',async route=>{
    const response=await route.fetch();assert.equal(response.status(),401);
    initialEntered=true;await new Promise(resolve=>releaseInitial=resolve);await route.fulfill({response});
  });
  await page.goto(url);
  await check("typing before a slow first unauthenticated status response is preserved",async()=>{
    await wait(()=>initialEntered);await page.locator('#login-password').pressSequentially('initial-draft',{delay:80});
    const response=page.waitForResponse(r=>r.url().endsWith('/api/status')&&r.status()===401);
    releaseInitial();await response;await page.waitForFunction(()=>!polling);
    assert.equal(await page.locator('#login-password').inputValue(),'initial-draft');
    assert.equal(await page.locator('#login-password').evaluate(e=>e===document.activeElement),true);
    await page.unroute('**/api/status');await page.locator('#login-password').fill('');
  });
  await page.waitForFunction(()=>loggedOut&&!polling);
  await check("progressive login typing survives three real unauthenticated polls and resume",async()=>{
    let unauthorized=0;
    const observe=response=>{if(response.url().endsWith('/api/status')&&response.status()===401)unauthorized++;};
    page.on('response',observe);
    const input=page.locator('#login-password');await input.focus();let expected='';
    for(const part of ['demo','-telescope','-2026']){
      const before=unauthorized;await input.pressSequentially(part,{delay:80});expected+=part;
      await wait(()=>unauthorized>before);await page.waitForFunction(()=>!polling);
      assert.equal(await input.inputValue(),expected);assert.equal(await input.evaluate(e=>e===document.activeElement),true);
      assert.equal(await page.evaluate(()=>statusFailures),0);
      assert.equal(await page.locator('#connection-state').isVisible(),false);
      assert.equal(await page.locator('#connection-retry').isVisible(),false);
      assert.equal(await page.locator('#dashboard').isVisible(),false);
    }
    await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));await page.waitForFunction(()=>!polling);
    assert.equal(await input.inputValue(),expected);assert.equal(await page.locator('#login-form').evaluate(e=>e.inert),false);
    assert.ok(unauthorized>=3);page.off('response',observe);
  });
  await check("login tolerates an earlier unauthenticated poll without stale error or auth bypass",async()=>{
    let releaseStatus,releaseLogin,statusEntered=false,loginEntered=false;
    await page.route('**/api/status',async route=>{
      const response=await route.fetch();assert.equal(response.status(),401);
      statusEntered=true;await new Promise(resolve=>releaseStatus=resolve);
      await route.fulfill({response});
    });
    await page.route('**/api/login',async route=>{
      loginEntered=true;await new Promise(resolve=>releaseLogin=resolve);await route.continue();
    });
    await page.evaluate(()=>{refresh();});await wait(()=>statusEntered);
    await page.locator('#login-password').fill('demo-telescope-2026');await page.locator('#login-form button').click();
    await wait(()=>loginEntered);releaseStatus();await page.waitForFunction(()=>!polling);
    await page.unroute('**/api/status');releaseLogin();await page.locator('#dashboard').waitFor({state:'visible'});
    assert.equal(await page.locator('#error').isVisible(),false);
    assert.equal(await page.evaluate(async()=>(await fetch('/api/status',{cache:'no-store'})).status),200);
    await page.unroute('**/api/login');
  });
  await check("notification destinations survive login and focus only after fresh authenticated status",async()=>{
    await page.locator('#logout').click();await page.locator('#login').waitFor({state:'visible'});
    await page.evaluate(()=>{location.hash='#networks?section=connection-mode';});
    assert.equal(await page.locator('#dashboard').isVisible(),false);
    await login();await page.waitForFunction(()=>document.activeElement?.id==='connection-mode-form');
    assert.equal(await page.locator('#nav-networks').getAttribute('aria-selected'),'true');
    await page.locator('#connection-mode').focus();await page.evaluate(()=>refresh());await page.waitForFunction(()=>!polling);
    assert.equal(await page.locator('#connection-mode').evaluate(e=>e===document.activeElement),true);
    await page.locator('#nav-telescopes').click();
    await page.evaluate(()=>{location.hash='#networks?section=connection-mode';});
    await page.waitForFunction(()=>document.activeElement?.id==='connection-mode-form');
    await page.locator('#connection-mode').focus();await page.evaluate(()=>refresh());await page.waitForFunction(()=>!polling);
    assert.equal(await page.locator('#connection-mode').evaluate(e=>e===document.activeElement),true);
    await page.evaluate(()=>{location.hash='#configuration/general?section=updates';});
    await page.waitForFunction(()=>document.activeElement?.id==='update-heading');
    assert.equal(await page.locator('#tab-general').getAttribute('aria-selected'),'true');
    await page.evaluate(()=>{location.hash='#configuration/pwa';});
    await page.locator('#push-category-boot').waitFor({state:'attached'});
    const labels=await page.locator('[id^="push-category-"]').count();assert.equal(labels,8);
    await page.locator('#push-category-boot').evaluate(e=>e.closest('details').open=true);
    assert.equal(await page.locator('#push-category-boot').isChecked(),true);
    await page.locator('#push-category-boot').evaluate(e=>e.checked=false);await page.evaluate(()=>refresh());await page.waitForFunction(()=>!polling);
    assert.equal(await page.locator('#push-category-boot').isChecked(),false);
    await page.evaluate(()=>{location.hash='#telescopes';});
  });
  await check("first home save without a new administrator code preserves browser authentication",async()=>{
    const before=await page.evaluate(async()=>(await(await fetch('/api/status')).json()).event_context);
    await page.locator('#nav-networks').click();await page.locator('#simple-home-settings').click();
    await page.locator('#network-interface').selectOption({label:'USB adapter A'});
    await page.locator('#network-ssid').fill('PWA FIRST HOME DEMO');await page.locator('#network-password').fill('synthetic-home-password');
    assert.equal(await page.locator('#initial-admin').inputValue(),'');
    const response=page.waitForResponse(r=>r.url().endsWith('/api/config')&&r.request().method()==='POST');
    await page.locator('#network-save').click();assert.equal((await(await response).json()).sessions_invalidated,false);
    await page.locator('#editor').waitFor({state:'hidden'});assert.equal(await page.locator('#login').isVisible(),false);
    assert.equal(await page.locator('#dashboard').isVisible(),true);
    const after=await page.evaluate(async()=>(await(await fetch('/api/status')).json()).event_context);assert.equal(after,before);
  });
  await check("real authentication loss clears drafts once while later login typing stays intact",async()=>{
    await page.locator('#nav-configuration').click();await page.locator('#tab-telescopes').click();await page.locator('#add-scope').click();await page.locator("#discovery-manual").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
    await page.locator('#network-password').fill('expired-private-draft');
    await page.evaluate(()=>document.getElementById('login-password').value='hidden-auth-canary');
    await context.clearCookies();await page.evaluate(()=>refresh());await page.locator('#login').waitFor({state:'visible'});
    assert.equal(await page.locator('#editor').isVisible(),false);
    assert.equal(await page.locator('#network-password').inputValue(),'');assert.equal(await page.locator('#login-password').inputValue(),'');
    assert.equal(await page.evaluate(()=>sessionStorage.getItem('astro-bridge.event-cursor')),null);
    await page.locator('#login-password').fill('new-login-draft');
    for(let i=0;i<3;i++)await page.evaluate(()=>refresh());
    assert.equal(await page.locator('#login-password').inputValue(),'new-login-draft');
    assert.equal(await page.evaluate(()=>statusFailures),0);await login();
  });
  await check("secure loopback lifecycle caches only fixed public bytes",async()=>{
    await page.evaluate(()=>navigator.serviceWorker.ready);await page.waitForFunction(()=>!!navigator.serviceWorker.controller);
    const cached=await page.evaluate(async()=>{const names=await caches.keys();return Promise.all(names.map(async name=>({name,keys:(await(await caches.open(name)).keys()).map(r=>new URL(r.url).pathname)})));});
    assert.equal(cached.length,1);assert.equal(cached[0].keys.length,13);assert.ok(cached[0].keys.includes("/manifest.webmanifest"));
    assert.ok(cached[0].keys.every(path=>!path.startsWith("/api")&&!path.includes("login")));
    assert.equal(await page.evaluate(()=>isSecureContext),true);
    for(const [scheme,color] of [["dark","#0a0d0b"],["light","#f7f4ee"]]){
      await page.emulateMedia({colorScheme:scheme});
      await page.waitForFunction(color=>document.querySelector('meta[name="theme-color"]').content===color,color);
    }
  });
  const hostname=await page.evaluate(async()=>(await(await fetch("/api/status")).json()).config.hostname);
  await check("ordinary LAN HTTP remains usable without SW registration",async()=>{
    const lan=await browser.newContext(),tab=await lan.newPage();await tab.goto(`http://${hostname}.local:${port}/`);
    await tab.locator("#login-password").fill("demo-telescope-2026");await tab.locator("#login-form button").click();await tab.locator("#dashboard").waitFor({state:"visible"});
    assert.equal(await tab.evaluate(()=>isSecureContext),false);
    assert.equal(await tab.evaluate(()=>"serviceWorker"in navigator?navigator.serviceWorker.getRegistrations().then(r=>r.length):0),0);
    await tab.locator("#nav-configuration").click();await tab.locator("#tab-pwa").click();assert.match(await tab.locator("#pwa-context").textContent(),/HTTPS/);await lan.close();
  });
  await check("one failed status poll preserves editor but disables writes",async()=>{
    await page.locator("#nav-configuration").click();await page.locator("#tab-telescopes").click();await page.locator("#add-scope").click();await page.locator("#discovery-manual").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
    await page.locator("#profile-name").fill("PRIVATE DRAFT P4A");await page.locator("#network-ssid").fill("PRIVATE SSID P4A");await page.locator("#network-password").fill("private-draft-pass");
    await page.evaluate(()=>{
      const option=document.createElement("option");option.value="PRIVATE SELECT CANARY";option.textContent="PRIVATE SELECT CANARY";document.getElementById("network-interface").append(option);
      document.getElementById("network-ssid").dataset.bssid="PRIVATE BSSID CANARY";
      document.getElementById("local-url").href="http://PRIVATE-LINK-CANARY.local/";
      const link=document.createElement("a");link.href="http://PRIVATE-LINK-CANARY.local/";link.textContent="PRIVATE LINK CANARY";document.getElementById("scan-results").append(link);
    });
    let fail=true;await context.route("**/api/status",route=>fail?(fail=false,route.abort()):route.continue());
    await page.waitForFunction(()=>document.getElementById("connection-message").textContent.includes("unverified"));
    assert.equal(await page.locator("#connection-retry").isVisible(),true);
    assert.equal(await page.locator("#editor").isVisible(),true);assert.equal(await page.locator("#profile-name").inputValue(),"PRIVATE DRAFT P4A");
    await context.unroute("**/api/status");
  });
  await check("confirmed offline clears private DOM/drafts and API has no cache fallback",async()=>{
    await context.setOffline(true);await page.waitForFunction(()=>document.getElementById("connection-message").textContent.includes("Bridge unavailable"));
    assert.equal(await page.locator("#connection-retry").isVisible(),true);
    assert.equal(await page.locator("#dashboard").isVisible(),false);assert.equal(await page.locator("#editor").isVisible(),false);
    assert.equal(await page.locator("#profile-name").inputValue(),"");assert.equal(await page.locator("#network-password").inputValue(),"");
    assert.equal(await page.evaluate(()=>["PRIVATE SSID P4A","PRIVATE SELECT CANARY","PRIVATE BSSID CANARY","PRIVATE-LINK-CANARY","PRIVATE LINK CANARY"].some(value=>document.documentElement.outerHTML.includes(value))),false);
    assert.equal(await page.evaluate(()=>fetch("/api/status",{cache:"no-store"}).then(()=>false,()=>true)),true);
    await page.reload();await page.waitForFunction(()=>document.getElementById("connection-message").textContent.includes("Bridge unavailable"));
    assert.equal(await page.locator("#dashboard").isVisible(),false);
    await page.setViewportSize({width:375,height:960});
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    const layout=await page.evaluate(()=>({headers:document.querySelectorAll("header").length,brands:document.querySelectorAll(".brand").length,viewport:[innerWidth,innerHeight],scroll:[document.documentElement.scrollWidth,document.documentElement.scrollHeight]}));
    assert.equal(layout.headers,1);assert.equal(layout.brands,1);assert.ok(layout.scroll[0]<=375);
    await writeFile(resolve(output,"offline-layout.json"),JSON.stringify(layout,null,2)+"\n");
    await page.screenshot({path:resolve(output,"offline-375.png"),fullPage:false});
  });
  await check("reconnect and resume require fresh authenticated status",async()=>{
    await context.setOffline(false);await page.locator("#dashboard").waitFor({state:"visible"});
    await page.locator("#nav-configuration").click();await page.locator("#tab-general").click();
    let release,held=false;await context.route("**/api/status",async route=>{if(!held){held=true;await new Promise(resolve=>release=resolve);}await route.continue();});
    await page.evaluate(()=>dispatchEvent(new Event("online")));await page.waitForFunction(()=>document.getElementById("dashboard").inert);
    const before=await page.evaluate(()=>document.getElementById("dashboard").inert);assert.equal(before,true);
    await wait(()=>!!release);const response=page.waitForResponse(r=>r.url().endsWith("/api/status")&&r.status()===200);release();await response;await context.unroute("**/api/status");await page.waitForFunction(()=>!document.getElementById("dashboard").inert);
  });
  control=await request.newContext({baseURL:url});
  const authorization=await(await control.post("/api/v1/auth/token",{data:{password:"demo-telescope-2026"}})).json();
  const rpc=async(method,path,data)=>{
    const response=await control.fetch("/api/v1"+path,{method,data,headers:{Authorization:"Bearer "+authorization.access_token}});
    assert.ok(response.ok(),path+" returned "+response.status());return response.json();
  };
  const live=async predicate=>wait(async()=>predicate(await rpc("GET","/status")));
  const counts=new Map();await page.exposeFunction("recordReadyOperation",operation=>counts.set(operation,(counts.get(operation)||0)+1));
  const observeReady=()=>{
    const install=()=>{const region=document.getElementById("connection-notifications");if(!region)return;new MutationObserver(records=>{
      for(const record of records)for(const node of record.addedNodes)if(node.nodeType===1&&node.matches(".connection-notice.ready"))window.recordReadyOperation(node.dataset.operationId);
    }).observe(region,{childList:true});};
    if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",install,{once:true});else install();
  };
  await page.addInitScript(observeReady);await page.evaluate(observeReady);
  const first="OUTAGE PRIVATE FIRST",second="OUTAGE PRIVATE SECOND";
  let radio;
  await check("offline reload replays confirmed grouped outcomes once after fresh status",async()=>{
    const state=await rpc("GET","/status");radio=state.devices[2].mac;
    await rpc("PATCH","/home",{ssid:"HOME REPLAY DEMO",open:true,mac:state.devices[1].mac});
    for(const [id,name,address] of [["replay-a",first,"10.20.0.1"],["replay-b",second,"10.21.0.1"]])
      await rpc("POST","/telescopes",{id,name,ssid:"REPLAY DEMO",open:true,target_ip:address,mac:"",enabled:false});
    const initial=await rpc("POST","/telescopes/replay-a/connect",{mac:radio});
    await live(state=>state.telescopes.some(profile=>profile.id==="replay-a"&&profile.relay_ready));
    await page.evaluate(()=>BridgeEvents.refresh());
    await page.waitForFunction(operation=>!!document.querySelector('.connection-notice.ready[data-operation-id="'+operation+'"]'),initial.operation_id);
    await wait(()=>(counts.get(initial.operation_id)||0)===1);
    await page.evaluate(()=>{location.hash='#telescopes?profile=replay-a';});
    await page.waitForFunction(()=>document.activeElement?.dataset.profileId==='replay-a');
    await page.locator('#nav-networks').click();
    await page.evaluate(()=>{location.hash='#telescopes?profile=replay-a';});
    await page.waitForFunction(()=>document.activeElement?.dataset.profileId==='replay-a');
    await page.locator('#nav-telescopes').focus();await page.evaluate(()=>refresh());await page.waitForFunction(()=>!polling);
    assert.equal(await page.locator('#nav-telescopes').evaluate(e=>e===document.activeElement),true);
    const before=await page.evaluate(()=>JSON.parse(sessionStorage.getItem("astro-bridge.event-cursor")));
    await context.setOffline(true);await page.locator("#dashboard").waitFor({state:"hidden"});
    assert.equal(await page.locator("#activity-list li").count(),0);assert.equal(await page.locator(".connection-notice").count(),0);
    assert.equal(await page.locator('#simple-connections [data-profile-id]').count(),0);
    assert.equal(await page.evaluate(names=>names.some(name=>document.documentElement.outerHTML.includes(name)),[first,second]),false);
    const changed=await rpc("POST","/telescopes/replay-b/connect",{mac:radio});
    await live(state=>state.telescopes.some(profile=>profile.id==="replay-b"&&profile.relay_ready));
    const journal=await rpc("GET","/events");
    assert.ok(journal.events.some(event=>event.operation_id===changed.operation_id&&event.type==="disconnected"));
    assert.ok(journal.events.some(event=>event.operation_id===changed.operation_id&&event.type==="connected"&&event.ready));
    await page.reload();await page.waitForFunction(()=>document.getElementById("connection-message").textContent.includes("Bridge unavailable"));
    const retained=await page.evaluate(()=>JSON.parse(sessionStorage.getItem("astro-bridge.event-cursor")));
    assert.equal(retained.sequence,before.sequence);assert.equal(retained.boot_id,before.boot_id);
    assert.deepEqual(Object.keys(retained).sort(),["boot_id","context","notified","sequence"]);
    assert.ok(retained.notified.length<=512&&retained.notified.every(id=>/^[0-9a-f]{32}:[1-9][0-9]*$/.test(id)));
    assert.equal(await page.evaluate(names=>names.some(name=>document.documentElement.outerHTML.includes(name)),[first,second]),false);
    let release,held=false;await context.route("**/api/status",async route=>{if(!held){held=true;await new Promise(resolve=>release=resolve);}await route.continue();});
    await context.setOffline(false);await wait(()=>!!release);
    assert.equal(await page.locator("#dashboard").isVisible(),false);assert.equal(await page.locator(".connection-notice").count(),0);
    const response=page.waitForResponse(r=>r.url().endsWith("/api/status")&&r.status()===200);release();await response;await context.unroute("**/api/status");
    const toast=page.locator('.connection-notice.ready[data-operation-id="'+changed.operation_id+'"]');await toast.waitFor();
    assert.match(await toast.innerText(),/OUTAGE PRIVATE FIRST disconnected.*OUTAGE PRIVATE SECOND connected/s);
    assert.equal(await page.locator("#simple-status-2-replay-b").textContent(),"Connected here");
    await wait(()=>(counts.get(changed.operation_id)||0)===1);
    await page.locator("#nav-telescopes").click();await page.setViewportSize({width:1280,height:960});
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    await page.screenshot({path:resolve(output,"replay-confirmed.png"),fullPage:false});await page.setViewportSize({width:375,height:960});
    await page.evaluate(()=>BridgeEvents.refresh());await page.waitForTimeout(3200);
    await page.evaluate(()=>dispatchEvent(new Event("online")));await page.waitForFunction(()=>!document.getElementById("dashboard").inert);
    await page.reload();await page.locator("#dashboard").waitFor({state:"visible"});await page.evaluate(()=>BridgeEvents.refresh());await page.waitForTimeout(500);
    assert.equal(counts.get(changed.operation_id),1);assert.equal(counts.get(initial.operation_id),1);
  });
  await check("expired authentication clears suspended cursor without stale replay or private resurrection",async()=>{
    await context.setOffline(true);await page.locator("#dashboard").waitFor({state:"hidden"});
    const changed=await rpc("POST","/telescopes/replay-a/connect",{mac:radio});
    await live(state=>state.telescopes.some(profile=>profile.id==="replay-a"&&profile.relay_ready));
    await context.clearCookies();await page.reload();await page.waitForFunction(()=>document.getElementById("connection-message").textContent.includes("Bridge unavailable"));
    assert.ok(await page.evaluate(()=>sessionStorage.getItem("astro-bridge.event-cursor")));
    await context.setOffline(false);await page.waitForFunction(()=>loggedOut&&!polling&&document.getElementById("connection-state").hidden);
    assert.equal(await page.evaluate(()=>sessionStorage.getItem("astro-bridge.event-cursor")),null);
    assert.equal(await page.locator('#simple-connections [data-profile-id]').count(),0);
    assert.equal(await page.evaluate(names=>names.some(name=>document.documentElement.outerHTML.includes(name)),[first,second]),false);
    assert.equal(await page.locator(".connection-notice").count(),0);assert.equal(await page.locator("#activity-list li").count(),0);
    assert.equal(await page.evaluate(names=>names.some(name=>document.documentElement.outerHTML.includes(name)),[first,second]),false);
    await login();await page.evaluate(()=>BridgeEvents.refresh());await page.waitForTimeout(3200);
    assert.equal(counts.get(changed.operation_id)||0,0);assert.equal(await page.locator(".connection-notice").count(),0);
  });
  await check("shared-cookie login rotation resets active replay context and rejects late private responses",async()=>{
    await page.evaluate(()=>BridgeEvents.refresh());
    const previous=(await page.evaluate(async()=>(await(await fetch("/api/status")).json()).event_context));
    await wait(()=>page.evaluate(value=>JSON.parse(sessionStorage.getItem("astro-bridge.event-cursor"))?.context===value,previous));
    let release,finish,held=false;const done=new Promise(resolve=>finish=resolve);
    await page.route("**/api/events?after=*",async route=>{
      if(held){await route.continue();return;}held=true;
      const response=await route.fetch();await new Promise(resolve=>release=resolve);
      // Aborted browser fetches can retire their route before this delayed real
      // response is released. No synthetic API/event response is substituted.
      if(!route.request().failure())try{await route.fulfill({response});}catch(error){if(!route.request().failure())throw error;}
      finish();
    });
    await page.evaluate(()=>{BridgeEvents.refresh();});await wait(()=>!!release);
    const changed=await rpc("POST","/telescopes/replay-b/connect",{mac:radio});
    await live(state=>state.telescopes.some(profile=>profile.id==="replay-b"&&profile.relay_ready));
    const other=await context.newPage();await other.goto(url);await other.locator("#dashboard").waitFor({state:"visible"});
    await other.evaluate(()=>api("/api/login",{password:"demo-telescope-2026"}));
    await page.evaluate(()=>refresh());
    const current=await page.evaluate(async()=>(await(await fetch("/api/status")).json()).event_context);
    assert.notEqual(current,previous);
    await wait(()=>page.evaluate(value=>JSON.parse(sessionStorage.getItem("astro-bridge.event-cursor"))?.context===value,current));
    release();await done;await page.unroute("**/api/events?after=*");await other.close();
    await page.evaluate(()=>BridgeEvents.refresh());await page.waitForTimeout(500);
    assert.equal(counts.get(changed.operation_id)||0,0);
    const next=await rpc("POST","/telescopes/replay-a/connect",{mac:radio});
    await live(state=>state.telescopes.some(profile=>profile.id==="replay-a"&&profile.relay_ready));
    await page.waitForFunction(operation=>!!document.querySelector('.connection-notice.ready[data-operation-id="'+operation+'"]'),next.operation_id);
    await wait(()=>(counts.get(next.operation_id)||0)===1);
  });
  const original=await readFile(resolve(app,"wifi_bridge/static/style.css"),"utf8");
  const initial=await page.locator('meta[name="bridge-shell-revision"]').getAttribute("content");
  await check("content change activates a new worker and requires safe reload",async()=>{
    await stop();await writeFile(resolve(app,"wifi_bridge/static/style.css"),original+"\n/* lifecycle upgrade fixture */\n");await start();
    await page.evaluate(async()=>{const r=await navigator.serviceWorker.getRegistration();await r.update();});
    await page.waitForFunction(()=>!document.getElementById("shell-reload").hidden);assert.equal(await page.locator("#dashboard").isVisible(),false);
    await page.locator("#shell-reload").click();await page.locator("#login").waitFor({state:"visible"});await login();
    assert.notEqual(await page.locator('meta[name="bridge-shell-revision"]').getAttribute("content"),initial);
    await wait(()=>page.evaluate(async()=>{const r=await navigator.serviceWorker.getRegistration();return !!r?.active&&!r.installing&&!r.waiting;}));
    assert.equal(await page.evaluate(async()=>(await caches.keys()).filter(n=>n.startsWith("astro-bridge-public-")).length),1);
  });
  await check("rollback restores original revision without reload loops",async()=>{
    await stop();await writeFile(resolve(app,"wifi_bridge/static/style.css"),original);await start();
    await page.evaluate(async()=>{const r=await navigator.serviceWorker.getRegistration();await r.update();});
    await page.waitForFunction(()=>!document.getElementById("shell-reload").hidden);await page.locator("#shell-reload").click();await page.locator("#login").waitFor({state:"visible"});await login();
    assert.equal(await page.locator('meta[name="bridge-shell-revision"]').getAttribute("content"),initial);
    assert.equal(await page.locator("#shell-reload").isVisible(),false);
    await page.screenshot({path:resolve(output,"online-install-375.png"),fullPage:true});
  });
  await check("logout and caches retain no private response/session data",async()=>{
    await page.evaluate(()=>document.getElementById('login-password').value='hidden-logout-canary');
    await page.locator("#logout").click();await page.locator("#login").waitFor({state:"visible"});
    assert.equal(await page.locator('#login-password').inputValue(),'');
    assert.equal(await page.evaluate(()=>sessionStorage.getItem("astro-bridge.event-cursor")),null);
    const bodies=await page.evaluate(async()=>{const all=[];for(const name of await caches.keys()){const c=await caches.open(name);for(const r of await c.keys()){const response=await c.match(r);if(!response.headers.get("content-type")?.startsWith("image/"))all.push(await response.text());}}return all.join("\n");});
    assert.ok(!bodies.includes("PRIVATE DRAFT P4A")&&!bodies.includes("private-draft-pass")&&!bodies.includes('"csrf":'));
  });
  await check("authentication alone cannot enable actions before fresh status",async()=>{
    let release,held=false;await context.route("**/api/status",async route=>{if(!held){held=true;await new Promise(resolve=>release=resolve);}await route.continue();});
    await page.locator("#login-password").fill("demo-telescope-2026");
    const response=page.waitForResponse(r=>r.url().endsWith("/api/login")&&r.status()===200);await page.locator("#login-form button").click();await response;
    await wait(()=>!!release);assert.equal(await page.locator("#dashboard").isVisible(),false);
    release();await page.locator("#dashboard").waitFor({state:"visible"});await context.unroute("**/api/status");
    await page.locator("#logout").click();await page.locator("#login").waitFor({state:"visible"});
  });
  await check("pre-PWA rollback blocks stale UI writes and retires public caches",async()=>{
    await writeFile(resolve(app,"legacy.flag"),"pre-PWA HTTP contract fixture");
    await page.locator("#login-password").fill("demo-telescope-2026");await page.locator("#login-form button").click();
    await page.locator("#shell-reload").waitFor({state:"visible"});assert.equal(await page.locator("#dashboard").isVisible(),false);
    assert.equal(await page.locator('#login-password').inputValue(),'');
    assert.equal(await page.evaluate(async()=>{try{await api("/api/reboot",{});return false;}catch(_){return true;}}),true);
    await page.evaluate(()=>fetch("/style.css",{cache:"no-store"}));
    await wait(()=>page.evaluate(async()=>(await navigator.serviceWorker.getRegistrations()).length===0));
    assert.equal(await page.evaluate(async()=>(await caches.keys()).filter(n=>n.startsWith("astro-bridge-public-")).length),0);
    await page.locator("#shell-reload").click();assert.equal(await page.locator("h1").textContent(),"Legacy interface");
    assert.equal(await page.evaluate(async()=>(await navigator.serviceWorker.getRegistrations()).length),0);
  });
  assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:results.length,pageErrors:errors,output},null,2));
}finally{
  if(control)await control.dispose();if(browser)await browser.close();server.kill("SIGTERM");await Promise.race([once(server,"exit"),new Promise(resolve=>setTimeout(resolve,5000))]);if(server.exitCode===null)server.kill("SIGKILL");await rm(temporary,{recursive:true,force:true});
}
