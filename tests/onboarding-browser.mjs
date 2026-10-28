import {clickUserControl} from "./browser-helpers.mjs";
/** Two-step Wi-Fi addition and notification destinations against the actual demo API. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,mkdir,rm,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {createServer} from 'node:net';
import {fileURLToPath,pathToFileURL} from 'node:url';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const folder=await mkdtemp(resolve(tmpdir(),'astro-onboarding-')),output=resolve(root,'artifacts/onboarding-0634');await mkdir(output,{recursive:true});
const rows=[['S50_NEW123','02:00:00:77:88:01','WPA2'],['vespera-NEW456','02:00:00:77:88:02','--'],['DWARF_mini_NEW789','02:00:00:77:88:03','WPA2'],['Office-unrelated','02:00:00:77:88:04','WPA2']].map(([ssid,bssid,security])=>({ssid,bssid,security,signal:90,frequency:2412}));
const runner=resolve(folder,'run.py');await writeFile(runner,`import sys\nsys.path.insert(0,${JSON.stringify(root)})\nfrom wifi_bridge.network import DemoNetwork\nDemoNetwork.scan=lambda self,name,**kwargs: ${JSON.stringify(rows)}\nfrom wifi_bridge.__main__ import main\nmain()\n`);
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u',runner,'--demo','--demo-radios','4','--state-dir',resolve(folder,'state'),'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;const results=[],errors=[];server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
try{
 for(let n=0;!logs.includes('SIMULATION');n++){if(n===100)throw new Error('Server start: '+logs);await new Promise(r=>setTimeout(r,100));}
 const origin=`http://127.0.0.1:${port}`;
 for(const path of ['/api/discovery/'+'0'.repeat(32),'/api/v1/discovery/'+'0'.repeat(32)])assert.equal((await fetch(origin+path)).status,401);
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const context=await browser.newContext({viewport:{width:375,height:960}}),page=await context.newPage();page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 const check=async(name,fn)=>{await fn();results.push(name);console.log('PASS',name);};
 const login=async tab=>{await tab.locator('#login-password').fill('demo-telescope-2026');await tab.locator('#login-form button').click();await tab.locator('#dashboard').waitFor({state:'visible'});};
 await page.goto(origin);await login(page);await page.locator('#nav-configuration').click();await page.locator('#tab-telescopes').click();
 let observed;
 await check('first step scans recognized Wi-Fi types, excludes unrelated networks and leaves profiles untouched',async()=>{
  const response=page.waitForResponse(r=>r.url().endsWith('/api/discovery'));await page.locator('#add-scope').click();observed=await(await response).json();
  await page.locator('.discovery-candidate').first().waitFor();assert.equal(await page.locator('.discovery-candidate').count(),3);
  assert.equal(await page.locator('#editor').isVisible(),false);assert.equal(await page.locator('#discover-scopes').count(),0);
  assert.doesNotMatch(await page.locator('#discovery-results').innerText(),/Office-unrelated/);assert.match(await page.locator('#discovery-results').innerText(),/Seestar/);
  assert.equal(await page.evaluate(()=>state.config.telescopes.length),0);
 });
 await check('second step adds an open Vespera with one unchanged submit and no association',async()=>{
  await page.locator('.discovery-candidate').filter({hasText:'vespera-NEW456'}).locator('button').click();
  assert.equal(await page.locator('#network-ssid').inputValue(),'vespera-NEW456');assert.equal(await page.locator('#profile-kind').inputValue(),'vespera');assert.equal(await page.locator('#target-ip').inputValue(),'10.0.0.1');assert.equal(await page.locator('#network-open').isChecked(),true);
  assert.equal(await page.locator('#profile-advanced').evaluate(e=>e.open),false);
  await page.locator('#network-save').click();await page.locator('#editor').waitFor({state:'hidden'});
  await page.waitForFunction(()=>state.config.telescopes.some(p=>p.ssid==='vespera-NEW456'));const profile=await page.evaluate(()=>state.config.telescopes[0]);assert.equal(profile.ssid,'vespera-NEW456');assert.equal(profile.enabled,false);assert.equal(profile.mac,'');assert.equal(profile.bssid,'02:00:00:77:88:02');
 });
 const dwarf=observed.candidates.find(p=>p.ssid==='DWARF_mini_NEW789');
 await check('returning to the first step reuses a recent scan instead of hitting the radio rate limit',async()=>{
  let requests=0;const watch=r=>{if(r.url().endsWith('/api/discovery'))requests++;};page.on('request',watch);
  await page.locator('#add-scope').click();await page.locator('.discovery-candidate').first().waitFor();assert.equal(requests,0);await page.locator('#discovery-close').click();page.off('request',watch);
 });
 await check('notification route after login opens durable prefill; defaults and an edited draft survive polling',async()=>{
  const fresh=await browser.newContext({viewport:{width:375,height:960}}),tab=await fresh.newPage();tab.setDefaultTimeout(10000);tab.on('pageerror',e=>errors.push(e.message));
  await tab.goto(origin+'/#configuration/telescopes?add='+dwarf.observation_id);assert.equal(await tab.locator('#editor').isVisible(),false);await login(tab);await tab.locator('#editor').waitFor({state:'visible'});
  assert.equal(await tab.locator('#profile-kind').inputValue(),'dwarf');assert.equal(await tab.locator('#target-ip').inputValue(),'192.168.88.1');assert.equal(await tab.locator('#network-password').inputValue(),'DWARF_12345678');assert.equal(await tab.locator('#factory-password-help').isVisible(),true);
  await tab.locator('#network-password').fill('changed-owner-password');await tab.waitForTimeout(5500);assert.equal(await tab.locator('#network-password').inputValue(),'changed-owner-password');
  await tab.locator('#close-editor').click();await tab.reload();await tab.locator('#editor').waitFor({state:'visible'});assert.equal(await tab.locator('#network-password').inputValue(),'DWARF_12345678');
  await tab.locator('#network-save').click();await tab.locator('#editor').waitFor({state:'hidden'});await tab.waitForFunction(()=>state.config.telescopes.some(p=>p.kind==='dwarf'));const profile=await tab.evaluate(()=>state.config.telescopes.find(p=>p.kind==='dwarf'));assert.equal(profile.has_password,true);assert.equal(profile.enabled,false);
  await fresh.close();await page.evaluate(()=>refresh());
 });
 await check('already saved observation opens its existing profile rather than creating a duplicate',async()=>{
  const vespera=observed.candidates.find(p=>p.ssid==='vespera-NEW456');await page.goto(origin+'/#configuration/telescopes?add='+vespera.observation_id);await page.locator('#editor').waitFor({state:'visible'});
  assert.equal(await page.locator('#editor-title').innerText(),'Edit telescope');assert.equal(await page.evaluate(()=>state.config.telescopes.length),2);await page.locator('#close-editor').click();
 });
 await check('scan failure is visible at the first-step top; a late result cannot replace a manual draft',async()=>{
  await page.evaluate(()=>{wifiSnapshot=null;wifiSnapshotAt=0;});
  await page.route('**/api/discovery',route=>route.fulfill({status:409,json:{error:'Busy',code:'NETWORK_BUSY'}}));
  await page.locator('#add-scope').click();await page.waitForFunction(()=>document.getElementById('discovery-status').textContent.includes('running'));
  assert.equal(await page.locator('#discovery-status').isVisible(),true);assert.ok((await page.locator('#discovery-status').boundingBox()).y<(await page.locator('#discovery-scan').boundingBox()).y);assert.equal(await page.locator('#editor').isVisible(),false);
  await page.locator('#discovery-close').click();await page.unroute('**/api/discovery');
  await page.evaluate(()=>{wifiSnapshot=null;wifiSnapshotAt=0;});
  let release,held=false;await page.route('**/api/discovery',async route=>{held=true;await new Promise(r=>release=r);await route.fulfill({json:observed});});
  await page.locator('#add-scope').click();while(!held)await new Promise(r=>setTimeout(r,25));await page.locator('#discovery-manual').click();await page.locator('#profile-name').fill('Do not replace this draft');release();await page.waitForTimeout(200);
  assert.equal(await page.locator('#profile-name').inputValue(),'Do not replace this draft');assert.equal(await page.locator('#network-ssid').inputValue(),'');await page.locator('#close-editor').click();await page.unroute('**/api/discovery');
 });
 await check('cached fallback is explained; wizard and prefill fit every language at 320 pixels',async()=>{
  await page.evaluate(()=>{wifiSnapshot=null;wifiSnapshotAt=0;});
  await page.route('**/api/discovery',route=>route.fulfill({json:{...observed,scan_source:'cached'}}));
  await page.locator('#add-scope').click();await page.locator('.discovery-candidate').first().waitFor();assert.match(await page.locator('#discovery-status').innerText(),/current availability is not confirmed/);
  for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){
   await page.evaluate(locale=>{BridgeI18n.setLocale(locale);BridgeI18n.translate();},locale);
   await page.setViewportSize({width:320,height:960});assert.equal(await page.locator('#discovery-dialog').evaluate(e=>e.scrollWidth<=e.clientWidth),true,locale);
  }
  await page.evaluate(()=>{BridgeI18n.setLocale('fr');BridgeI18n.translate();});await page.screenshot({path:resolve(output,'find-320-fr.png')});
  await page.locator('.discovery-candidate').filter({hasText:'S50_NEW123'}).locator('button').click();assert.equal(await page.locator('#network-password').inputValue(),'12345678');assert.equal(await page.locator('#editor').evaluate(e=>e.scrollWidth<=e.clientWidth),true);
  await page.screenshot({path:resolve(output,'add-320-fr.png')});await page.locator('#close-editor').click();await page.unroute('**/api/discovery');
 });
 await check('expired notification destination offers a fresh scan; logout clears detected network DOM',async()=>{
  await page.goto(origin+'/#configuration/telescopes?add='+'0'.repeat(32));await page.locator('#discovery-dialog').waitFor({state:'visible'});assert.ok(await page.locator('#discovery-status').innerText());
  await page.locator('#discovery-close').click();await clickUserControl(page, '#logout');await page.locator('#login').waitFor({state:'visible'});
  assert.equal(await page.locator('#discovery-results').innerText(),'');assert.equal(await page.locator('#network-ssid').inputValue(),'');assert.equal(await page.locator('#network-password').inputValue(),'');
 });
 assert.deepEqual(errors,[]);await writeFile(resolve(output,'browser.json'),JSON.stringify({results,errors,simulation:true},null,2)+'\n');
}finally{if(browser)await browser.close();server.kill('SIGTERM');await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});}
