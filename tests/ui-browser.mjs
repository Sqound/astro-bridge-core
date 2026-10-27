/** Production UI acceptance against a fresh, explicitly simulated Python server.
 * Requires Playwright and its browser; optionally set PLAYWRIGHT_MODULE and CHROME_PATH.
 * Run: node tests/ui-browser.mjs. Synthetic screenshots go to artifacts/p1-ui/.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : "playwright");
const output = resolve(root, process.env.UI_ARTIFACTS || "artifacts/p1-ui");
const temporary = await mkdtemp(resolve(tmpdir(), "astro-bridge-ui-"));
const socket = createServer(); socket.listen(0, "127.0.0.1"); await once(socket, "listening");
const port = socket.address().port; await new Promise(resolve => socket.close(resolve));
const server = spawn(process.env.PYTHON || "python3", ["-m", "wifi_bridge", "--demo", "--demo-radios", "4", "--state-dir", temporary, "--port", String(port)], { cwd: root, stdio: ["ignore", "pipe", "pipe"] });
let logs = ""; server.stdout.on("data", data => logs += data); server.stderr.on("data", data => logs += data);
let browser;
const failures = [], results = [];
try {
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("Demo server did not start: " + logs)), 10000);
    server.stdout.on("data", () => { if (logs.includes("SIMULATION")) { clearTimeout(timeout); resolve(); } });
    server.once("exit", code => { clearTimeout(timeout); reject(new Error("Demo server exited: " + code + " " + logs)); });
  });
  await mkdir(output, { recursive: true });
  browser = await chromium.launch({ headless: true, ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}) });
  const context = await browser.newContext({ viewport: { width: 1280, height: 960 }, colorScheme: "light" });
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  page.on("pageerror", error => failures.push(error.message));
  const url = `http://127.0.0.1:${port}`;
  const check = async (name, action) => { if(process.env.UI_LOCALIZATION_ONLY==="1"&&results.length>=3&&!name.startsWith("localization:"))return; if(process.env.UI_FIELD_ONLY==="1"&&results.length>=3&&!name.startsWith("field:"))return; if(process.env.UI_REPLACEMENT_ONLY==="1"&&results.length>=3&&!name.startsWith("replacement:"))return; if(process.env.UI_PROTECTION_ONLY==="1"&&results.length>=3&&!name.startsWith("protection:"))return; if(process.env.UI_ENSURE_ONLY==="1"&&results.length>=3&&!name.startsWith("ensure:"))return; if(process.env.UI_DISCOVERY_ONLY==="1"&&results.length>=3&&!name.startsWith("discovery:"))return; if(process.env.UI_EVENTS_ONLY==="1"&&results.includes("confirmed activity, grouped switch notification and independent tab reads"))return; await action(); results.push(name); console.log("PASS", name); };
  const login = async () => { await page.locator("#login-password").fill("demo-telescope-2026"); await page.locator("#login-form button").click(); await page.locator("#dashboard").waitFor({ state: "visible" }); };
  const data = () => page.evaluate(async () => (await fetch("/api/status")).json());
  const waitData = async predicate => {
    const end=Date.now()+10000;
    while(Date.now()<end){const state=await data();if(predicate(state))return;await new Promise(resolve=>setTimeout(resolve,100));}
    throw new Error("Server status did not reach the expected state");
  };
  const clickTab = async id => { await page.locator("#" + id).click(); await page.waitForFunction(id => document.getElementById(id).getAttribute("aria-selected") === "true", id); };
  await page.goto(url);
  await check("English default and authenticated actual demo state", async () => {
    assert.equal(await page.locator("html").getAttribute("lang"), "en");
    await page.locator("#login-password").fill("synthetic-wrong-password");await page.locator("#login-form button").click();await page.waitForFunction(()=>document.getElementById("error").textContent==="Incorrect password.");
    await page.locator("#locale-menu summary").click();await page.locator('[data-locale="fr"]').click();assert.equal(await page.locator("#error").textContent(),"Mot de passe incorrect.");
    await page.locator("#locale-menu summary").click();await page.locator('[data-locale="en"]').click();
    await login(); assert.equal((await data()).demo, true);
    // Fresh AP-only configuration is independent of an optional home network.
    await clickTab("nav-configuration");await clickTab("tab-telescopes");await page.locator("#add-scope").click();await page.locator("#discovery-manual").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
    await page.locator("#profile-kind").selectOption("seestar");await page.locator("#profile-name").fill("Fresh AP synthetic");
    await page.locator("#network-ssid").fill("Fresh-AP-DEMO");await page.locator("#network-password").fill("synthetic-fresh-password");await page.locator("#target-ip").fill("10.8.0.1");
    await page.locator("#network-save").click();await page.locator("#editor").waitFor({state:"hidden"});
    if(await page.locator("#login").isVisible())await login();
    const fresh=(await data()).config.telescopes[0];assert.equal((await data()).config.home.ssid,undefined);
    await clickTab("nav-telescopes");assert.equal(await page.locator(`#simple-connect-1-${fresh.id}`).count(),0);await page.locator(`[id^="simple-connect-"][id$="-${fresh.id}"]:enabled`).first().click();await waitData(s=>s.telescopes.find(p=>p.id===fresh.id)?.relay_ready);
    assert.equal((await data()).management.mode,"ap_only");assert.match(await page.locator("#simple-notice").textContent(),/You can use telescopes through the hotspot/);
    await page.waitForFunction(()=>state?.management?.mode==="ap_only");assert.equal(await page.locator("#management-notice").isVisible(),false);
    assert.equal(await page.locator("#other-network-interfaces").getAttribute("open"),null);
    await page.locator("#other-network-interfaces summary").click();assert.equal(await page.locator('[data-reserved-role="hotspot"]').textContent(),"Reserved for hotspot");assert.equal(await page.locator("#other-connections button").count(),0);await page.locator("#other-network-interfaces summary").click();
    await page.setViewportSize({width:375,height:960});await page.screenshot({path:resolve(output,"fresh-ap-only-375.png"),fullPage:true,timeout:10000});await page.setViewportSize({width:1280,height:960});
    await clickTab("nav-configuration");await clickTab("tab-telescopes");page.once("dialog",dialog=>dialog.accept());await page.locator(`#profile-${fresh.id}-remove`).click();await waitData(s=>s.config.telescopes.length===0&&s.telescopes.length===0);

  });
  await check("home Wi-Fi save preserves the initialized authenticated session", async () => {
    await clickTab("nav-networks"); await page.locator("#simple-home-settings").click();
    await page.locator("#network-interface").selectOption({ label: "USB adapter A" });
    await page.locator("#network-ssid").fill("Home-DEMO"); await page.locator("#network-password").fill("synthetic-home-pass");
    await page.locator("#network-save").click(); await page.locator("#editor").waitFor({state:"hidden"}); if(await page.locator("#login").isVisible())await login();
    await waitData(s => !!s.home_ip);
    await clickTab("nav-telescopes");await page.waitForFunction(()=>document.querySelectorAll('#other-connections [data-reserved-role]').length===2);
    await page.locator("#other-network-interfaces summary").click();
    assert.equal(await page.locator('[data-reserved-role="home"]').textContent(),"Reserved for private Wi-Fi");
    assert.equal(await page.locator('[data-reserved-role="hotspot"]').textContent(),"Reserved for hotspot");
    await page.evaluate(()=>refresh());assert.equal(await page.locator("#other-network-interfaces").getAttribute("open"),"");
    await page.setViewportSize({width:375,height:960});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    for(const badge of await page.locator('#other-connections .badge').all())assert.ok(await badge.evaluate(e=>e.getBoundingClientRect().width<=e.closest('.card').getBoundingClientRect().width-28));
    await page.screenshot({path:resolve(output,"other-network-interfaces-375.png"),fullPage:true});await page.setViewportSize({width:1280,height:960});
    await page.locator("#other-network-interfaces summary").click();
  });
  await check("preset, real API scan, save and edit profile without adapter assignment", async () => {
    await clickTab("nav-configuration"); await clickTab("tab-telescopes"); await page.locator("#add-scope").click();await page.locator("#discovery-manual").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
    await page.locator("#profile-kind").selectOption("seestar"); assert.equal(await page.locator("#target-ip").inputValue(), "10.0.0.1");
    await page.locator("#scan").click(); await page.locator("#scan-results button").first().waitFor();
    await page.locator("#scan-results button").first().click();
    await page.locator("#profile-name").fill("Seestar DEMO"); await page.locator("#target-ip").fill("10.1.0.1");
    await page.locator("#network-password").fill("synthetic-scope-pass"); await page.locator("#network-save").click(); await page.locator("#editor").waitFor({ state: "hidden" });
    const profile = (await data()).config.telescopes[0]; assert.equal(profile.enabled, false); assert.equal(profile.mac, "");
    await page.locator(`[id="profile-${profile.id}-edit"]`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true); await page.locator("#profile-name").fill("Seestar DEMO edited");
    await page.locator("#network-save").click(); await page.locator("#editor").waitFor({ state: "hidden" });
    assert.equal((await data()).config.telescopes[0].has_password, true);
    for (const [kind, name, ip] of [["vespera", "Vespera DEMO", "10.2.0.1"], ["dwarf", "DWARF DEMO — long instrument name for narrow screens", "10.3.0.1"]]) {
      await page.locator("#add-scope").click();await page.locator("#discovery-manual").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true); await page.locator("#profile-kind").selectOption(kind); await page.locator("#profile-name").fill(name);
      await page.locator("#network-ssid").fill("Telescope-DEMO"); await page.locator("#target-ip").fill(ip); await page.locator("#network-password").fill("synthetic-scope-pass");
      await page.locator("#network-save").click(); await page.locator("#editor").waitFor({ state: "hidden" });
    }
  });
  const profiles = (await data()).config.telescopes;
  if(process.env.UI_LOCALIZATION_ONLY==="1"){
    await check("localization: ten languages retain configuration roles and visible actions at four widths",async()=>{
      await clickTab("nav-telescopes");await page.locator(`#simple-connect-2-${profiles[0].id}`).click();await waitData(s=>s.telescopes.find(p=>p.id===profiles[0].id)?.relay_ready);
      await clickTab("nav-configuration");await clickTab("tab-telescopes");assert.equal(await page.locator(`[id="profile-${profiles[0].id}-info"]`).count(),1);
      for(const locale of ["en","fr","ja","de","es","it","pt-BR","zh-Hans","zh-Hant","ru"]){
        await page.locator("#locale-menu summary").click();await page.locator(`button[data-locale="${locale}"]`).click();
        assert.equal(await page.locator("html").getAttribute("lang"),locale);assert.equal(await page.locator("#locale-select").inputValue(),locale);assert.equal(await page.locator("#locale-fallback").isVisible(),false);
        for(const width of [320,375,768,1280]){
          await page.setViewportSize({width,height:960});await page.waitForTimeout(100);
          const theme=width===320||width===1280?"dark":"light";for(let i=0;i<3&&await page.locator("html").getAttribute("data-theme")!==theme;i++)await page.locator("#theme-toggle").click();assert.equal(await page.locator("html").getAttribute("data-theme"),theme);
          for(const [main,sub] of [["telescopes"],["networks"],["configuration","general"],["configuration","radios"],["configuration","telescopes"]]){await clickTab("nav-"+main);if(sub)await clickTab("tab-"+sub);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`${locale} ${width} ${main}/${sub}`);}
          const bounds=await page.locator("#telescopes button").evaluateAll(buttons=>buttons.map(button=>{const b=button.getBoundingClientRect();return {left:b.left,right:b.right,width:b.width};}));
          assert.ok(bounds.every(b=>b.left>=0&&b.right<=width&&b.width>20));assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
          assert.equal(await page.locator(`[id="profile-${profiles[0].id}-info"]`).count(),1);
          const ax=await page.locator("#panel-telescopes table").ariaSnapshot();assert.match(ax,/- table(?: "[^"]*")?:/);assert.match(ax,/row/);assert.match(ax,/cell/);assert.match(ax,/button/);
          if(width===320||width===375){await writeFile(resolve(output,`configuration-${locale}-${width}-aria.txt`),ax);for(const button of await page.locator("#telescopes tr").first().locator("button").all())assert.equal(await button.isVisible(),true);}
          if(width===375||width===1280)await page.screenshot({path:resolve(output,`configuration-${locale}-${width}.png`),fullPage:true,timeout:10000});
        }
        const row=page.locator("#telescopes tr").first();assert.equal(await row.locator("button").count(),5);assert.ok(await row.locator("td").nth(1).getAttribute("data-label"));
      }
    });
    await check("localization: open editor draft and focus survive synchronized language changes",async()=>{
      await page.locator(`[id="profile-${profiles[0].id}-edit"]`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator("#profile-name").fill("UNSENT PRIVATE LOCALIZATION DRAFT");await page.locator("#profile-name").focus();
      for(const locale of ["en","fr","ja","de","es","it","pt-BR","zh-Hans","zh-Hant","ru"]){await page.evaluate(locale=>BridgeI18n.setLocale(locale),locale);assert.equal(await page.locator("#profile-name").inputValue(),"UNSENT PRIVATE LOCALIZATION DRAFT");assert.equal(await page.locator("#profile-name").evaluate(e=>e===document.activeElement),true);assert.equal(await page.locator("#locale-select").inputValue(),locale);assert.equal(await page.locator("#editor-title").textContent(),await page.evaluate(()=>BridgeI18n.t("Edit telescope")));}
      await page.evaluate(()=>BridgeI18n.setLocale("fr"));assert.equal(await page.locator("#locale-select").inputValue(),"fr");assert.equal(await page.locator("#editor-title").textContent(),"Modifier le télescope");await page.locator("#close-editor").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
      await page.reload();await page.locator("#dashboard").waitFor({state:"visible"});assert.equal(await page.locator("html").getAttribute("lang"),"fr");
      assert.doesNotMatch(await page.locator("body").innerText(),/UNSENT PRIVATE LOCALIZATION DRAFT/);
    });
    await check("localization: error to success and closed editor cannot resurrect old messages",async()=>{
      await page.evaluate(()=>BridgeI18n.setLocale("en"));await clickTab("tab-general");
      await page.locator("#credential-target").selectOption("linux");
      const fill=async password=>{await page.locator("#current-password").fill(password);await page.locator("#new-password").fill("synthetic-new-linux-password");await page.locator("#confirm-password").fill("synthetic-new-linux-password");await page.locator("#credentials-form button").click();};
      await fill("synthetic-wrong-password");await page.waitForFunction(()=>document.getElementById("credentials-message").textContent==="Incorrect current administrator password.");
      await fill("demo-telescope-2026");await page.waitForFunction(()=>document.getElementById("credentials-message").textContent==="Password changed.");await page.evaluate(()=>BridgeI18n.setLocale("fr"));
      assert.equal(await page.locator("#credentials-message").textContent(),"Mot de passe modifié.");
      await clickTab("tab-telescopes");await page.locator(`[id="profile-${profiles[1].id}-edit"]`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator("#network-ssid").fill("😀".repeat(9));await page.locator("#network-password").fill("synthetic-scope-pass");await page.locator("#network-save").click();await page.locator("#editor-error").waitFor({state:"visible"});
      await page.locator("#close-editor").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.evaluate(()=>BridgeI18n.setLocale("en"));await page.locator(`[id="profile-${profiles[1].id}-edit"]`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
      assert.equal(await page.locator("#editor-error").textContent(),"");assert.equal(await page.locator("#editor-error").isVisible(),false);await page.locator("#close-editor").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
    });
  }
  if(process.env.UI_FIELD_ONLY==="1"){
    const token=await(await fetch(url+"/api/v1/auth/token",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:"demo-telescope-2026"})})).json();
    const native=async(method,path,body)=>{const response=await fetch(url+"/api/v1"+path,{method,headers:{"Content-Type":"application/json",Authorization:"Bearer "+token.access_token},...(body?{body:JSON.stringify(body)}:{})});const value=await response.json();assert.ok(response.ok,JSON.stringify(value));return value;};
    await check("field: real demo AP-only forwarding and per-path profile access",async()=>{
      const s=await data(),usb=s.devices.find(d=>d.usb&&d.mac!==s.config.home.mac);
      await native("POST","/telescopes/"+profiles[0].id+"/connect",{mac:usb.mac});await waitData(s=>s.telescopes.some(p=>p.relay_ready));
      await native("PATCH","/home",{ssid:"ECHEC",password:"synthetic-failure-pass"});await waitData(s=>s.management?.mode==="ap_only"&&s.telescopes.some(p=>p.relay_ready));
      await page.waitForFunction(()=>document.getElementById("management-notice").textContent.includes("Hotspot access only"));
      await clickTab("nav-configuration");await clickTab("tab-telescopes");await page.locator(`[id="profile-${profiles[0].id}-info"]`).click();
      assert.match(await page.locator("#profile-info-body").innerText(),/192\.168\.77\.1/);assert.doesNotMatch(await page.locator("#profile-info-body").innerText(),/Home access/);await page.locator("#profile-info [data-close-help]").click();
      await page.screenshot({path:resolve(output,"field-ap-only-en.png"),fullPage:true,timeout:10000});
    });
    await check("field: notice remains visible in all tabs and French mobile without overflow",async()=>{
      await page.locator("#locale-menu summary").click();await page.locator('[data-locale="fr"]').click();await page.setViewportSize({width:375,height:960});
      for(const tab of ["nav-telescopes","nav-networks","nav-configuration"]){await clickTab(tab);assert.equal(await page.locator("#management-notice").isVisible(),true);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);}
      assert.match(await page.locator("#management-notice").innerText(),/point d’accès uniquement/);await page.waitForTimeout(350);
      await page.screenshot({path:resolve(output,"field-ap-only-fr-375.png"),fullPage:true,timeout:10000});
    });
    await check("field: returning home retains occupied AP and common profile proxy on both paths",async()=>{
      await native("PATCH","/home",{ssid:"Home-DEMO",password:"synthetic-home-pass"});await waitData(s=>s.management?.mode==="dual");
      await page.waitForFunction(()=>state?.management?.mode==="dual"&&document.getElementById("management-notice").hidden);
      assert.equal(await page.locator("#management-notice").textContent(),"");
      const s=await data(),row=s.telescopes.find(p=>p.id===profiles[0].id);assert.equal(row.relay_ready,true);assert.deepEqual(row.management_paths.map(p=>p.role),["home","ap"]);
      await clickTab("tab-telescopes");await page.locator(`[id="profile-${profiles[0].id}-info"]`).click();assert.match(await page.locator("#profile-info-body").innerText(),/192\.168\.1\.42/);assert.match(await page.locator("#profile-info-body").innerText(),/192\.168\.77\.1/);await page.locator("#profile-info [data-close-help]").click();
      await page.screenshot({path:resolve(output,"field-dual-fr-375.png"),fullPage:true,timeout:10000});
    });
    await check("field: logout clears private paths and prevents stale activity resurrection",async()=>{
      await page.locator("#logout").click();await page.locator("#login").waitFor({state:"visible"});assert.equal(await page.locator("#management-notice").textContent(),"");
      assert.doesNotMatch(await page.locator("body").innerText(),/Home-DEMO|192\.168\.1\.42|Seestar DEMO edited/);assert.equal(await page.locator("#activity-list li").count(),0);
    });
  }
  if(process.env.UI_REPLACEMENT_ONLY==="1"){
    await check("replacement: explicit advanced consent and unsaved policy survives polling",async()=>{
      await clickTab("nav-telescopes");await page.locator("#auto-connect").check();await waitData(s=>s.config.auto_connect);await clickTab("nav-configuration");await clickTab("tab-general");await page.locator("#auto-advanced>summary").click();await page.locator("#auto-policy-form summary").click();
      await page.locator("#auto-quiet").fill("180");await page.locator("#auto-force").check();await page.locator("#auto-demand").check();await page.locator("#auto-vespera-demand").check();await page.locator("#auto-dwarf-demand").check();
      await page.locator("#auto-quiet").focus();await page.waitForTimeout(5500);assert.equal(await page.locator("#auto-quiet").inputValue(),"180");assert.equal(await page.locator("#auto-quiet").evaluate(e=>e===document.activeElement),true);
      await page.locator("#auto-policy-save").click();await page.waitForFunction(()=>document.getElementById("auto-policy-feedback").textContent.includes("not saved"));
      assert.equal((await data()).config.auto_policy.force_first_profile,false);
      await page.locator("#auto-ack").check();await page.locator("#auto-policy-save").click();await page.waitForFunction(()=>document.getElementById("auto-policy-feedback").textContent==="Automatic policy saved.");
      await clickTab("nav-telescopes");await page.locator('[data-help="auto-info"]').click();assert.match(await page.locator("#vespera-demand-status").innerText(),/unavailable/);assert.match(await page.locator("#dwarf-demand-status").innerText(),/unavailable/);assert.match(await page.locator("#auto-info").innerText(),/sends no discovery replies/);await page.locator("#auto-info [data-close-help]").click();await clickTab("nav-configuration");await clickTab("tab-general");const policy=(await data()).config.auto_policy;assert.deepEqual(policy,{quiet_seconds:180,cooldown_seconds:60,force_first_profile:true,seestar_demand:true,vespera_demand:true,dwarf_demand:true});assert.equal(await page.locator("#auto-ack").isChecked(),false);
      await page.screenshot({path:resolve(output,"replacement-policy-en.png"),fullPage:true,timeout:10000});
    });
    await check("replacement: stale policy draft refuses an unrelated client change without reassociation",async()=>{
      await page.locator("#auto-quiet").fill("210");await page.locator("#auto-ack").check();
      const before=await data();const token=await(await fetch(url+"/api/v1/auth/token",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:"demo-telescope-2026"})})).json();
      const response=await fetch(url+"/api/v1/settings",{method:"PATCH",headers:{"Content-Type":"application/json",Authorization:"Bearer "+token.access_token,"If-Match":'"'+before.config_revision+'"'},body:JSON.stringify({auto_policy:{quiet_seconds:240}})});assert.equal(response.status,200);
      await page.waitForTimeout(5500);assert.equal(await page.locator("#auto-quiet").inputValue(),"210");
      await page.locator("#auto-policy-save").click();await page.waitForFunction(()=>document.getElementById("auto-policy-feedback").textContent.includes("changed elsewhere"));
      const after=await data();assert.equal(after.config.auto_policy.quiet_seconds,240);assert.deepEqual(after.telescopes,before.telescopes);assert.equal(await page.locator("#auto-quiet").inputValue(),"210");assert.equal(await page.locator("#auto-ack").isChecked(),false);
      await page.locator("#auto-policy-reload").click();assert.equal(await page.locator("#auto-quiet").inputValue(),"240");
    });
    await check("replacement: victim consent remains separate from eligibility and survives full save",async()=>{
      await clickTab("tab-telescopes");await page.locator(`[id="profile-${profiles[0].id}-edit"]`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
      assert.equal(await page.locator("#profile-replaceable").isChecked(),false);await page.locator("#profile-auto-eligible").check();await page.locator("#profile-replaceable").check();
      await page.locator("#network-save").click();await page.locator("#editor").waitFor({state:"hidden"});
      const saved=(await data()).config.telescopes[0];assert.equal(saved.auto_eligible,true);assert.equal(saved.auto_replaceable,true);assert.equal(saved.enabled,false);
      await page.locator(`[id="profile-${profiles[0].id}-edit"]`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);assert.equal(await page.locator("#profile-replaceable").isChecked(),true);await page.locator("#close-editor").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
    });
    await check("replacement: DWARF guidance offers actual Pi app endpoint separately from target",async()=>{
      await clickTab("nav-telescopes");await page.locator(`#simple-connect-2-${profiles[2].id}`).click();await waitData(s=>s.telescopes.find(p=>p.id===profiles[2].id)?.dwarf_proxy);
      await clickTab("nav-configuration");await clickTab("tab-telescopes");await page.waitForFunction(()=>document.getElementById("telescopes").textContent.includes("Connect by IP in DWARFLAB: 192.168.1.42"));
      assert.doesNotMatch(await page.locator("#telescopes").innerText(),/TCP 9900/);await page.locator(`[id="profile-${profiles[2].id}-info"]`).click();
      const text=await page.locator("#profile-info-body").innerText();assert.match(text,/DWARF app IP: 192.168.1.42/);assert.match(text,/TCP 9900 \/ 8082 \/ 554/);assert.match(text,/10.3.0.0/);await page.locator("#profile-info [data-close-help]").click();
      const actual=(await data()).telescopes.find(p=>p.id===profiles[2].id);assert.equal(actual.target_ip,"10.3.0.1");assert.equal((await data()).maker_demands.dwarf.discovery_relay.maker_app_acceptance,"unverified");
      await page.setViewportSize({width:375,height:960});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await page.screenshot({path:resolve(output,"dwarf-proxy-endpoint-375.png"),fullPage:true,timeout:10000});
    });
    await check("replacement: French mobile guidance and logout clears policy/private profile state",async()=>{
      await page.locator("#locale-menu summary").click();await page.locator('[data-locale="fr"]').click();await clickTab("tab-general");
      await page.setViewportSize({width:375,height:960});await page.waitForTimeout(350);
      assert.match(await page.locator("#auto-policy-form").innerText(),/interrompre une pose/);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.screenshot({path:resolve(output,"replacement-policy-fr-375.png"),fullPage:true,timeout:10000});
      await page.locator("#logout").click();await page.locator("#login").waitFor({state:"visible"});
      assert.equal(await page.locator("#pause-notice").isVisible(),false);assert.equal(await page.locator("#auto-force").isChecked(),false);assert.equal(await page.locator("#auto-ack").isChecked(),false);
      assert.equal(await page.locator("#maker-demand-status").textContent(),"");assert.equal(await page.locator("#vespera-demand-status").textContent(),"");assert.equal(await page.locator("#auto-vespera-demand").isChecked(),false);assert.equal(await page.locator("#auto-dwarf-demand").isChecked(),false);assert.equal(await page.locator("#dwarf-demand-status").textContent(),"");
    });
  }
  if(process.env.UI_PROTECTION_ONLY==="1"){
    let ownLease,foreignLease,nativeToken;
    const native=async(method,path,body,revision)=>{const response=await fetch(url+"/api/v1"+path,{method,headers:{"Content-Type":"application/json",...(nativeToken?{Authorization:"Bearer "+nativeToken}:{}),...(revision?{"If-Match":'"'+revision+'"'}:{})},...(body!==undefined?{body:JSON.stringify(body)}:{})});return {code:response.status,data:await response.json()};};
    await check("protection: reserve ready connection, renew owned lease and show foreign protection honestly",async()=>{
      await clickTab("nav-telescopes");await page.locator(`#simple-connect-2-${profiles[0].id}`).click();await waitData(s=>s.telescopes[0]?.relay_ready);
      await clickTab("nav-configuration");await clickTab("tab-telescopes");await page.locator(`[id="profile-${profiles[0].id}-reserve"]`).click();await waitData(s=>s.connection_protection.leases.length===1);
      ownLease=(await data()).connection_protection.leases[0];assert.equal(ownLease.owned,true);
      nativeToken=(await native("POST","/auth/token",{password:"demo-telescope-2026"})).data.access_token;
      foreignLease=(await native("POST","/telescopes/"+profiles[0].id+"/leases",{ttl_seconds:300},(await data()).config_revision)).data;assert.ok(foreignLease.id);
      await clickTab("tab-general");await page.locator("#connection-protection-settings>summary").click();await page.locator(`#lease-${ownLease.id}-renew`).click();await waitData(s=>s.connection_protection.leases.find(l=>l.id===ownLease.id)?.version===2);
      await page.locator(`#lease-${foreignLease.id}-release`).waitFor();assert.match(await page.locator("#protection-list").textContent(),/Another client reservation/);
      for(const tab of ["nav-telescopes","nav-networks","nav-configuration"]){await clickTab(tab);assert.equal(await page.locator("#protection-summary").isVisible(),true);}
    });
    await check("protection: owner release refuses renewed stale view and captures credentials for one action",async()=>{
      await clickTab("tab-general");await page.locator(`#lease-${foreignLease.id}-release`).click();
      assert.equal((await native("PATCH","/leases/"+foreignLease.id,{version:1,ttl_seconds:300})).code,200);
      await page.locator("#protection-password").fill("demo-telescope-2026");await page.locator("#protection-confirm").check();await page.locator("#protection-form button[type=submit]").click();
      await page.waitForFunction(()=>document.getElementById("protection-error").textContent.includes("renewed"));assert.equal(await page.locator("#protection-password").inputValue(),"");assert.equal(await page.locator("#protection-confirm").isChecked(),false);
      await page.locator("#protection-cancel").click();await page.reload();await page.locator("#dashboard").waitFor({state:"visible"});await clickTab("nav-configuration");await clickTab("tab-general");
      await page.locator("#connection-protection-settings>summary").click();await page.locator("#locale-select").selectOption("fr");await page.locator(`#lease-${foreignLease.id}-release`).click();
      for(const width of [320,375,768]){await page.setViewportSize({width,height:960});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth&&document.getElementById("protection-dialog").scrollWidth<=document.getElementById("protection-dialog").clientWidth));}
      await page.setViewportSize({width:375,height:960});await page.screenshot({path:resolve(output,"owner-release-fr-375.png"),fullPage:false});
      await page.locator("#protection-password").fill("demo-telescope-2026");await page.locator("#protection-confirm").check();await page.locator("#protection-form button[type=submit]").click();await page.locator("#protection-dialog").waitFor({state:"hidden"});await waitData(s=>!s.connection_protection.leases.some(l=>l.id===foreignLease.id));
    });
    await check("protection: cancel and Escape clear secrets; late error cannot enter another dialog",async()=>{
      await page.setViewportSize({width:1280,height:960});await page.locator("#locale-select").selectOption("en");await clickTab("tab-telescopes");
      for(const dismiss of ["cancel","escape"]){await page.locator(`[id="profile-${profiles[0].id}-lock"]`).click();await page.locator("#protection-password").fill("synthetic-unsent-password");await page.locator("#protection-confirm").check();if(dismiss==="cancel")await page.locator("#protection-cancel").click();else await page.keyboard.press("Escape");await page.locator("#protection-dialog").waitFor({state:"hidden"});assert.equal(await page.locator("#protection-password").inputValue(),"");assert.equal(await page.locator("#protection-confirm").isChecked(),false);}
      let release,held=false,done;const completed=new Promise(r=>done=r);await context.route("**/api/protection",async route=>{try{const response=await route.fetch();held=true;await new Promise(r=>release=r);await route.fulfill({response});}finally{done();}});
      await page.locator(`[id="profile-${profiles[0].id}-lock"]`).click();await page.locator("#protection-password").fill("wrong-test-password");await page.locator("#protection-confirm").check();await page.locator("#protection-form button[type=submit]").click();while(!held)await new Promise(r=>setTimeout(r,25));
      await page.locator("#protection-cancel").click();await page.locator(`[id="profile-${profiles[1].id}-lock"]`).click();release();await completed;await context.unroute("**/api/protection");await page.waitForTimeout(150);assert.equal(await page.locator("#protection-error").isVisible(),false);await page.locator("#protection-cancel").click();
    });
    await check("protection: persistent lock blocks manual disconnect; logout clears DOM but keeps exposure protection",async()=>{
      await page.locator(`[id="profile-${profiles[0].id}-lock"]`).click();await page.locator("#protection-password").fill("demo-telescope-2026");await page.locator("#protection-confirm").check();await page.locator("#protection-form button[type=submit]").click();await page.locator("#protection-dialog").waitFor({state:"hidden"});await waitData(s=>s.config.telescopes[0].owner_locked);
      await clickTab("nav-telescopes");await page.locator(`#simple-connect-2-${profiles[0].id}`).click();await page.locator("#error").waitFor({state:"visible"});assert.equal((await data()).config.telescopes[0].enabled,true);
      await clickTab("nav-configuration");await clickTab("tab-general");await page.locator("#protection-list").scrollIntoViewIfNeeded();await page.locator('[data-help="protection-info"]').click();await page.screenshot({path:resolve(output,"protection-desktop.png"),fullPage:false});await page.locator("#protection-info [data-close-help]").click();await page.locator("#logout").click();await page.locator("#login").waitFor({state:"visible"});
      const html=await page.content();assert.ok(!html.includes(profiles[0].id)&&!html.includes(ownLease.id)&&!html.includes("Seestar DEMO edited"));assert.equal(await page.locator("#protection-list").textContent(),"");
      const stored=await page.evaluate(()=>JSON.stringify({...localStorage,...sessionStorage}));assert.ok(!stored.includes(ownLease.id)&&!stored.includes(profiles[0].id));
      nativeToken=(await native("POST","/auth/token",{password:"demo-telescope-2026"})).data.access_token;const protection=(await native("GET","/protection")).data;assert.ok(protection.leases.some(l=>l.id===ownLease.id));assert.ok(protection.owner_locks.includes(profiles[0].id));
    });
  }
  if(process.env.UI_ENSURE_ONLY==="1"){
    await check("ensure: consent defaults off; explicit policy and profile eligibility save independently",async()=>{
      assert.equal((await data()).config.auto_connect,false);assert.ok((await data()).config.telescopes.every(p=>p.auto_eligible===false));
      assert.equal(await page.locator(`[id="profile-${profiles[0].id}-ensure"]`).isDisabled(),true);
      await clickTab("nav-telescopes");await page.locator("#auto-connect").check();await waitData(s=>s.config.auto_connect===true);await clickTab("nav-configuration");
      await clickTab("tab-telescopes");await page.locator(`[id="profile-${profiles[0].id}-edit"]`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator("#profile-auto-eligible").check();await page.locator("#network-save").click();await page.locator("#editor").waitFor({state:"hidden"});
      assert.equal((await data()).config.telescopes[0].mac,"");assert.equal((await data()).config.telescopes[0].enabled,false);
    });
    await check("ensure: actual accepted operation becomes ready only after controller reconciliation",async()=>{
      const response=page.waitForResponse(r=>r.url().endsWith("/api/telescopes/ensure")&&r.request().method()==="POST");
      await page.locator(`[id="profile-${profiles[0].id}-ensure"]`).click();const accepted=await response;assert.equal(accepted.status(),202);const operation=await accepted.json();assert.equal(operation.state,"running");assert.ok(operation.id);
      await waitData(s=>s.connection_operation?.id===operation.id&&s.connection_operation.state==="succeeded"&&s.telescopes[0]?.relay_ready);
      await page.waitForFunction(id=>ensureView?.id===id&&ensureView.state==="succeeded"&&document.getElementById("ensure-notice").hidden,operation.id);
      const status=await data();assert.equal(status.config.telescopes[0].bssid,"02:00:00:11:22:33");assert.ok(status.config.telescopes[0].mac);assert.equal(status.connection_operation.endpoint.profile_id,profiles[0].id);
      assert.equal(await page.locator("#ensure-message").textContent(),"");assert.equal(await page.locator("#ensure-endpoint").textContent(),"");
      assert.ok((await page.evaluate(async id=>(await fetch("/api/operations?id="+encodeURIComponent(id))).json(),operation.id)).endpoint_current);
      await page.screenshot({path:resolve(output,"ensure-ready-desktop.png"),fullPage:false});
    });
    await check("ensure: occupied physical AP fails without interrupting the ready instrument or home",async()=>{
      await page.locator(`[id="profile-${profiles[1].id}-edit"]`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator("#profile-auto-eligible").check();await page.locator("#network-save").click();await page.locator("#editor").waitFor({state:"hidden"});
      await page.locator(`[id="profile-${profiles[1].id}-ensure"]`).click();await waitData(s=>s.connection_operation?.profile_id===profiles[1].id&&s.connection_operation.state==="failed");
      await page.waitForFunction(()=>document.getElementById("ensure-message").textContent.includes("already in use"));
      const status=await data();assert.equal(status.connection_operation.reason,"PHYSICAL_NETWORK_IN_USE");assert.equal(status.telescopes[0].relay_ready,true);assert.ok(status.home_ip);assert.equal(status.config.telescopes[1].enabled,false);assert.equal(status.config.telescopes[1].mac,"");
      await clickTab("tab-general");await page.locator("#locale-select").selectOption("fr");await page.waitForFunction(()=>document.getElementById("ensure-message").textContent.includes("déjà utilisé"));
      for(const width of [320,375,768]){await page.setViewportSize({width,height:960});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}
      await page.setViewportSize({width:375,height:960});await page.screenshot({path:resolve(output,"ensure-failure-fr-375.png"),fullPage:false});
    });
    await check("ensure: ready fast path needs no automatic consent; logout removes operation/private endpoints",async()=>{
      await clickTab("nav-telescopes");await page.locator("#auto-connect").uncheck();await waitData(s=>s.config.auto_connect===false);await clickTab("nav-configuration");await clickTab("tab-telescopes");
      const response=page.waitForResponse(r=>r.url().endsWith("/api/telescopes/ensure")&&r.request().method()==="POST");await page.locator(`[id="profile-${profiles[0].id}-ensure"]`).click();const ready=await response;assert.equal(ready.status(),200);assert.equal((await ready.json()).state,"ready");
      await page.locator("#logout").click();await page.locator("#login").waitFor({state:"visible"});assert.equal(await page.locator("#ensure-notice").isVisible(),false);assert.equal(await page.locator("#ensure-message").textContent(),"");assert.equal(await page.locator("#ensure-endpoint").textContent(),"");
      const html=await page.content();assert.ok(!html.includes("10.1.0.1")&&!html.includes("Seestar DEMO edited")&&!html.includes(profiles[0].id));
      const stored=await page.evaluate(()=>JSON.stringify({...localStorage,...sessionStorage}));assert.ok(!stored.includes("10.1.0.1")&&!stored.includes(profiles[0].id));
    });
  }
  if(process.env.UI_DISCOVERY_ONLY==="1"){
    await check("discovery: manual profile then binding, mismatch, correction and unlink keep selection and ID",async()=>{
      await page.locator(`[id="profile-${profiles[0].id}-edit"]`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);assert.equal(await page.locator("#radio-fields").isVisible(),false);
      await page.locator("#binding-bssid").fill("02:00:00:77:88:99");await page.locator("#binding-confirm").check();await page.locator("#bind-profile").click();
      await page.waitForFunction(()=>document.getElementById("binding-info").textContent.includes("Owner confirmed"));
      assert.match(await page.locator("#binding-info").textContent(),/differs from the saved Wi-Fi selector/);
      const bound=(await data()).config.telescopes[0];assert.equal(bound.id,profiles[0].id);assert.equal(bound.bssid,profiles[0].bssid);assert.equal(bound.enabled,false);assert.equal(bound.mac,"");
      assert.equal(await page.locator("#binding-append").isChecked(),true);await page.locator("#binding-bssid").fill("02:00:00:77:88:98");await page.locator("#binding-confirm").check();await page.locator("#bind-profile").click();
      await page.waitForFunction(()=>document.querySelectorAll("#binding-aliases button").length===2);page.once("dialog",dialog=>dialog.accept());await page.locator("#binding-aliases button").filter({hasText:"77:88:98"}).click();await page.waitForFunction(()=>document.querySelectorAll("#binding-aliases button").length===1);

      await page.locator("#close-editor").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator(`[id="profile-${profiles[1].id}-edit"]`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
      await page.locator("#binding-bssid").fill("02:00:00:77:88:99");await page.locator("#binding-confirm").check();
      let release,held=false,done;const completed=new Promise(r=>done=r);await context.route("**/api/telescopes/binding",async route=>{try{const response=await route.fetch();held=true;await new Promise(r=>release=r);await route.fulfill({response});}finally{done();}});
      let dialogs=0;page.on("dialog",async dialog=>{dialogs++;await dialog.dismiss();});
      await page.locator("#bind-profile").click();while(!held)await new Promise(r=>setTimeout(r,25));
      await page.locator("#close-editor").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator(`[id="profile-${profiles[2].id}-edit"]`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);release();await completed;await context.unroute("**/api/telescopes/binding");
      await page.waitForTimeout(250);assert.equal(dialogs,0);assert.equal(await page.locator("#editor-error").isVisible(),false);assert.doesNotMatch(await page.locator("#binding-info").textContent(),/77:88:99/);
      await page.locator("#close-editor").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator(`[id="profile-${profiles[0].id}-edit"]`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator("#unlink-profile").click();
      await page.waitForFunction(()=>document.getElementById("binding-info").textContent.includes("No physical identity"));
      assert.equal((await data()).config.telescopes[0].bssid,profiles[0].bssid);await page.locator("#close-editor").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
    });
    await check("discovery: profile save followed by refused binding is a partial failure with stable retry ID",async()=>{
      await page.locator("#add-scope").click();await page.locator("#discovery-manual").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator("#profile-name").fill("Partial save DEMO");await page.locator("#network-ssid").fill("Partial-DEMO");await page.locator("#network-password").fill("synthetic-partial-pass");await page.locator("#target-ip").fill("10.5.0.1");await page.locator("#binding-bssid").fill("invalid");await page.locator("#binding-confirm").check();await page.locator("#bind-after-save").check();await page.locator("#network-save").click();
      await page.waitForFunction(()=>document.getElementById("editor-error").textContent.includes("Profile saved, but physical binding failed"));
      const saved=(await data()).config.telescopes.find(p=>p.name==="Partial save DEMO"),count=(await data()).config.telescopes.length;assert.ok(saved);assert.equal(saved.physical_binding,undefined);
      await page.locator("#binding-bssid").fill("02:00:00:77:88:97");await page.locator("#binding-confirm").check();await page.locator("#bind-profile").click();await page.waitForFunction(()=>document.getElementById("binding-info").textContent.includes("77:88:97"));
      const retried=await data();assert.equal(retried.config.telescopes.length,count);assert.equal(retried.config.telescopes.find(p=>p.name==="Partial save DEMO").id,saved.id);await page.locator("#close-editor").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
    });
    await check("discovery: observed scan, stale response/editor barrier and optional post-save binding",async()=>{
      // Respect the real production discovery cooldown rather than override its clock.
      await page.waitForTimeout(15000);await page.locator("#add-scope").click();await page.locator("#discovery-manual").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator("#scan").click();await page.locator("#scan-results button").first().waitFor();
      await page.locator("#scan-results button").first().click();await page.locator("#profile-name").fill("Observed DEMO");await page.locator("#target-ip").fill("10.4.0.1");await page.locator("#network-password").fill("synthetic-observed-pass");
      await page.locator("#binding-confirm").check();await page.locator("#bind-after-save").check();await page.locator("#network-save").click();await page.locator("#editor").waitFor({state:"hidden"});
      await waitData(s=>s.config.telescopes.some(p=>p.name==="Observed DEMO"&&p.physical_binding));
      const observed=(await data()).config.telescopes.find(p=>p.name==="Observed DEMO");assert.equal(observed.enabled,false);assert.equal(observed.mac,"");assert.equal(observed.physical_binding.aliases[0].source,"wifi-observed");assert.equal(observed.physical_binding.aliases[0].frequency,2412);
      await clickTab("tab-general");await page.locator("#locale-select").selectOption("fr");await clickTab("tab-telescopes");await page.locator(`[id="profile-${observed.id}-edit"]`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
      assert.match(await page.locator("#binding-info").textContent(),/Observé en Wi-Fi/);
      for(const width of [320,375,768]){await page.setViewportSize({width,height:960});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth&&document.getElementById("editor").scrollWidth<=document.getElementById("editor").clientWidth));}
      await page.setViewportSize({width:375,height:960});await page.screenshot({path:resolve(output,"binding-375.png"),fullPage:false});
      await page.locator("#close-editor").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.setViewportSize({width:1280,height:960});await clickTab("tab-general");await page.locator("#locale-select").selectOption("en");await clickTab("tab-telescopes");
      await page.waitForTimeout(15000);let release,held=false,done;const completed=new Promise(r=>done=r);await context.route("**/api/discovery",async route=>{try{const response=await route.fetch();held=true;await new Promise(r=>release=r);await route.fulfill({response});}finally{done();}});
      await page.locator("#add-scope").click();await page.locator("#discovery-manual").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator("#scan").click();while(!held)await new Promise(r=>setTimeout(r,25));await page.locator("#close-editor").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator(`[id="profile-${profiles[0].id}-edit"]`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);release();await completed;await context.unroute("**/api/discovery");await page.waitForTimeout(250);
      assert.equal(await page.locator("#scan-results button").count(),0);assert.equal(await page.locator("#binding-bssid").inputValue(),"");await page.locator("#close-editor").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
    });
    await check("discovery: all occupied radios report manual entry; logout clears candidate/private binding DOM",async()=>{
      await clickTab("nav-telescopes");await page.locator(`#simple-connect-2-${profiles[0].id}`).click();await waitData(s=>s.telescopes.find(p=>p.id===profiles[0].id)?.connected);
      await page.locator(`#simple-connect-3-${profiles[1].id}`).click();await waitData(s=>s.telescopes.filter(p=>p.connected).length===2);
      await clickTab("nav-configuration");await clickTab("tab-telescopes");await page.waitForTimeout(15000);await page.locator("#add-scope").click();await page.locator("#discovery-manual").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator("#scan").click();await page.waitForFunction(()=>document.getElementById("scan-results").children.length>0);
      assert.ok((await data()).home_ip);assert.equal((await data()).telescopes.filter(p=>p.connected).length,2);
      await page.locator("#binding-bssid").fill("02:00:00:77:88:99");await page.locator("#close-editor").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator("#logout").click();await page.locator("#login").waitFor({state:"visible"});
      const html=await page.content();assert.ok(!html.includes("02:00:00:77:88:99")&&!html.includes("Telescope-DEMO"));assert.equal(await page.locator("#scan-results button").count(),0);assert.equal(await page.locator("#binding-bssid").inputValue(),"");
    });
  }
  await check("connect and confirmed move use server status and preserve home access", async () => {
    await clickTab("nav-telescopes"); await page.locator(`#simple-connect-2-${profiles[0].id}`).click();
    await waitData(s => s.telescopes[0]?.connected); await page.waitForFunction(() => document.getElementById("occupied-count").textContent === "1 / 2");
    await page.locator(`#simple-connect-3-${profiles[0].id}`).click(); await page.locator("#move-dialog").waitFor({ state: "visible" });
    const languageTab=await context.newPage(); await languageTab.goto(url+"/#configuration/general"); await languageTab.locator("#dashboard").waitFor({state:"visible"});
    await languageTab.locator("#locale-select").selectOption("fr"); await page.waitForFunction(() => document.getElementById("move-description").textContent.includes("utilise"));
    await languageTab.locator("#locale-select").selectOption("en"); await page.waitForFunction(() => document.getElementById("move-description").textContent.includes("is assigned")); await languageTab.close();
    await page.locator("#cancel-move").click(); assert.equal((await data()).config.telescopes[0].mac, "02:00:00:00:00:02");
    await page.locator(`#simple-connect-3-${profiles[0].id}`).click(); await page.locator("#confirm-move").click();
    await waitData(s => s.telescopes[0]?.connected && s.telescopes[0]?.interface === "wlan3");
    await page.locator(`#simple-connect-2-${profiles[1].id}`).click(); await waitData(s => s.telescopes.filter(p => p.connected).length === 2);
    await page.waitForFunction(() => document.getElementById("occupied-count").textContent === "2 / 2");
    assert.ok((await data()).home_ip); assert.equal(await page.locator(".connection-table tbody tr").count(), 6);
  });
  await check("confirmed activity, grouped switch notification and independent tab reads", async () => {
    await page.locator("#activity-panel summary").click();
    const actionResponse=page.waitForResponse(response=>response.url().endsWith("/api/telescopes/action")&&response.request().method()==="POST");
    await page.locator(`#simple-connect-2-${profiles[2].id}`).click();
    const accepted=await (await actionResponse).json();
    await waitData(s=>s.telescopes.find(p=>p.id===profiles[2].id)?.relay_ready);
    await page.waitForFunction(operation=>[...document.querySelectorAll(".connection-notice")].some(element=>element.dataset.operationId===operation&&element.textContent.includes("disconnected")&&element.textContent.includes("connected")),accepted.operation_id);
    assert.match(await page.locator(`[data-operation-id="${accepted.operation_id}"]`).innerText(),/Vespera DEMO disconnected/);
    assert.equal(await page.locator(`#simple-status-2-${profiles[2].id}`).textContent(),"Connected here");
    assert.notEqual(await page.locator(`#simple-status-2-${profiles[1].id}`).textContent(),"Connected here");
    assert.equal(await page.locator(".connection-notice").filter({hasText:"Vespera DEMO connected."}).count(),0);
    assert.match(await page.locator("#activity-list").innerText(),/Wi-Fi associated; checking services/);
    const readEvents=()=>page.evaluate(async()=> (await (await fetch("/api/events")).json()));
    const before=await readEvents();
    const other=await context.newPage();await other.goto(url);await other.locator("#dashboard").waitFor({state:"visible"});
    await page.waitForTimeout(3300);assert.equal((await readEvents()).latest_sequence,before.latest_sequence);await other.close();
    for(const main of ["networks","configuration","telescopes"]){await clickTab("nav-"+main);assert.equal(await page.locator("#activity-panel").isVisible(),true);}
    const ids=await page.locator("#activity-list li").evaluateAll(elements=>elements.map(element=>element.dataset.eventId));assert.equal(ids.length,new Set(ids).size);
    await page.screenshot({path:resolve(output,"events-desktop.png"),fullPage:true,timeout:10000});
    await page.reload();await page.locator("#dashboard").waitFor({state:"visible"});await page.waitForFunction(()=>document.querySelectorAll("#activity-list li").length>0);
    assert.equal((await readEvents()).latest_sequence,before.latest_sequence);
    assert.equal(await page.locator(".connection-notice").count(),0);
  });
  await check("polling preserves editor draft, profile details, focus and unsaved General values", async () => {
    await clickTab("nav-configuration"); await clickTab("tab-telescopes");
    await page.locator(`#profile-${profiles[0].id}-info`).click();await page.waitForTimeout(5200);assert.equal(await page.locator("#profile-info").evaluate(e=>e.open),true);await page.locator("#profile-info [data-close-help]").click();await page.waitForFunction(id=>document.activeElement===document.getElementById("profile-"+id+"-info"),profiles[0].id);
    await page.locator(`#profile-${profiles[0].id}-edit`).click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true); await page.locator("#profile-name").fill("Unsaved telescope draft");
    await page.locator("#network-password").fill("unsaved-synthetic-password"); await page.waitForTimeout(5200);
    assert.equal(await page.locator("#profile-name").inputValue(), "Unsaved telescope draft"); assert.equal(await page.locator("#network-password").inputValue(), "unsaved-synthetic-password");
    assert.equal(await page.evaluate(() => document.activeElement.id), "network-password");
    await page.locator("#close-editor").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true); assert.equal(await page.locator("#profile-info").evaluate(element=>element.open),false);assert.ok(!(await page.locator("#telescopes").innerText()).includes("Path matches:"));
    await clickTab("tab-general"); await page.locator("#hostname").fill("unsaved-device-draft"); await page.locator("#country").fill("FR");
    await clickTab("nav-networks"); await page.locator("#ap-enabled").check(); await waitData(s => s.config.hotspot.enabled);
    await clickTab("nav-configuration"); await page.locator("#country").focus(); await page.waitForTimeout(5200);
    assert.equal(await page.locator("#hostname").inputValue(), "unsaved-device-draft"); assert.equal(await page.locator("#country").inputValue(), "FR"); assert.equal(await page.evaluate(() => document.activeElement.id), "country");
  });
  await check("header and General locale controls synchronize and persist with complete Japanese", async () => {
    await page.locator("#locale-menu summary").click(); await page.locator('[data-locale="fr"]').click();
    assert.equal(await page.locator("#locale-select").inputValue(), "fr"); assert.equal(await page.locator("html").getAttribute("lang"), "fr");
    assert.equal(await page.locator("#hostname").inputValue(), "unsaved-device-draft");
    await page.locator("#locale-select").selectOption("ja"); assert.equal(await page.locator("#locale-current").textContent(), "JA");
    assert.equal(await page.locator("html").getAttribute("lang"), "ja"); assert.equal(await page.locator("#locale-fallback").isVisible(), false);
    await page.reload(); await page.locator("#dashboard").waitFor({ state: "visible" });
    assert.equal(await page.locator("#locale-select").inputValue(), "ja"); await page.locator("#locale-select").selectOption("fr");
    assert.equal(await page.locator("#locale-fallback").isVisible(), false);
    const other = await context.newPage(); await other.goto(url); await other.locator("#dashboard").waitFor({ state: "visible" });
    await page.locator("#locale-select").selectOption("en"); await other.waitForFunction(() => document.documentElement.lang === "en"); await other.close();
    await page.locator("#locale-select").selectOption("fr");
  });
  await check("direct links, history and keyboard tab navigation", async () => {
    await clickTab("nav-networks"); await clickTab("nav-configuration"); await clickTab("tab-radios");
    await page.goBack(); await page.waitForFunction(() => !document.getElementById("panel-general").hidden);
    await page.goBack(); await page.waitForFunction(() => !document.getElementById("view-networks").hidden);
    await page.goForward(); await page.waitForFunction(() => !document.getElementById("panel-general").hidden);
    await page.locator("#nav-configuration").focus(); await page.keyboard.press("Home"); await page.waitForFunction(() => !document.getElementById("view-telescopes").hidden);
    assert.equal(await page.evaluate(() => document.activeElement.id), "nav-telescopes");
    await page.goto(url + "/#configuration/radios"); await page.locator("#dashboard").waitFor({ state: "visible" }); assert.equal(await page.locator("#panel-radios").isVisible(), true);
    await page.locator("#tab-radios").focus(); await page.keyboard.press("Home"); await page.waitForFunction(() => !document.getElementById("panel-general").hidden);
  });
  await check("themes follow system and remember explicit choice", async () => {
    assert.equal(await page.locator("html").getAttribute("data-theme-mode"), "auto"); await page.emulateMedia({ colorScheme: "dark" }); await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
    await page.locator("#theme-toggle").click(); assert.equal(await page.locator("html").getAttribute("data-theme"), "light");
    await page.locator("#theme-toggle").click(); await page.reload(); await page.locator("#dashboard").waitFor({ state: "visible" }); assert.equal(await page.locator("html").getAttribute("data-theme-mode"), "dark");
  });
  await check("all destinations at 320/375/768/1280 have no global horizontal overflow", async () => {
    for (const width of [320, 375, 768, 1280]) {
      await page.setViewportSize({ width, height: 960 });
      for (const [main, sub] of [["telescopes"], ["networks"], ["configuration", "general"], ["configuration", "pwa"], ["configuration", "telescopes"], ["configuration", "radios"]]) {
        await clickTab("nav-" + main); if(sub) await clickTab("tab-" + sub);
        const dimensions = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth, visibleTabs: [...document.querySelectorAll(".main-nav [role=tab]")].filter(el => el.getAttribute("aria-selected") === "true").length }));
        assert.ok(dimensions.scroll <= dimensions.width, `${width} ${main}/${sub}: ${JSON.stringify(dimensions)}`); assert.equal(dimensions.visibleTabs, 1);
      }
    }
  });
  await check("synthetic desktop/mobile dark and light screenshots", async () => {
    for(const [width, theme, route, name] of [[1280,"dark","telescopes","desktop-dark"],[375,"dark","telescopes","mobile-375-dark"],[320,"light","telescopes","mobile-320-light"],[768,"light","networks","networks-768-light"],[1280,"dark","configuration","configuration-dark"]]) {
      await page.setViewportSize({width,height:960}); await clickTab("nav-"+route); if(route==="configuration")await clickTab("tab-general");
      for(let n=0;n<3 && await page.locator("html").getAttribute("data-theme")!==theme;n++)await page.locator("#theme-toggle").click();
      await page.screenshot({path:resolve(output,name+".png"),fullPage:true,timeout:10000});
    }
    await page.setViewportSize({width:375,height:960}); await page.locator("#locale-menu summary").click(); await page.screenshot({path:resolve(output,"locale-375.png"),fullPage:true,timeout:10000}); await page.keyboard.press("Escape");
  });
  await check("credentials and update controls remain backed by existing APIs", async () => {
    await page.setViewportSize({width:1280,height:960}); await clickTab("nav-configuration"); await clickTab("tab-general");
    assert.equal(await page.locator("#update-check").isDisabled(), true); assert.equal(await page.locator("#update-automatic").isDisabled(), true);
    await page.locator("#credential-target").selectOption("linux"); await page.locator("#current-password").fill("demo-telescope-2026");
    await page.locator("#new-password").fill("synthetic-linux-password"); await page.locator("#confirm-password").fill("synthetic-linux-password");
    await page.locator("#credentials-form button").click(); await page.waitForFunction(() => document.getElementById("credentials-message").textContent.length > 0);
    assert.equal(await page.locator("#current-password").inputValue(), ""); assert.equal(await page.locator("#new-password").inputValue(), "");
  });
  await check("logout clears authenticated event names and notifications",async()=>{
    await page.locator("#logout").click();await page.locator("#login").waitFor({state:"visible"});
    assert.equal(await page.locator("#activity-list li").count(),0);assert.equal(await page.locator(".connection-notice").count(),0);
  });
  assert.deepEqual(failures, [], "Page errors");
  console.log(JSON.stringify({passed:results.length,screenshots:output,pageErrors:failures},null,2));
} finally {
  if(browser)await browser.close();
  server.kill("SIGTERM"); await Promise.race([once(server,"exit"),new Promise(resolve=>setTimeout(resolve,5000))]); if(server.exitCode===null)server.kill("SIGKILL");
  await rm(temporary,{recursive:true,force:true});
}
