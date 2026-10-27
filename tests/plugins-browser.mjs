/** Catalog-driven settings and an extra community manifest in an isolated demo. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {cp,mkdtemp,mkdir,readFile,rm,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {createServer} from 'node:net';
import {fileURLToPath,pathToFileURL} from 'node:url';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const folder=await mkdtemp(resolve(tmpdir(),'astro-plugins-')),workspace=resolve(folder,'app'),output=resolve(root,'artifacts/plugins-0633');await mkdir(output,{recursive:true});await mkdir(workspace);
await cp(resolve(root,'wifi_bridge'),resolve(workspace,'wifi_bridge'),{recursive:true,filter:path=>!path.includes('__pycache__')});
const catalog=JSON.parse(await readFile(resolve(workspace,'wifi_bridge/static/plugins.json'),'utf8'));
const extra=structuredClone(catalog.plugins.find(p=>p.id==='zwo_mount'));
Object.assign(extra,{id:'community_mount',name:'Community mount',description:'Synthetic community fixture.',default_address:'192.168.5.1',identification:'none',relay:{transport:'tcp',ports:[12345]},wifi:[{prefix:'Community_',min_suffix:1,suffix:'any'}],validation:{level:'simulation',models:[],checks:[],limitations:['physical_pending']}});
catalog.plugins.push(extra);await writeFile(resolve(workspace,'wifi_bridge/static/plugins.json'),JSON.stringify(catalog));
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',resolve(folder,'state'),'--port',String(port)],{cwd:workspace,stdio:['ignore','pipe','pipe']});
let logs='',browser;const results=[],errors=[];
server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
try{
 for(let n=0;!logs.includes('SIMULATION');n++){if(n===100)throw new Error('Server start: '+logs);await new Promise(r=>setTimeout(r,100));}
 const origin=`http://127.0.0.1:${port}`;
 for(const path of ['/api/plugins','/api/v1/plugins'])assert.equal((await fetch(origin+path)).status,401);
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:960}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 const check=async(name,fn)=>{await fn();results.push(name);console.log('PASS',name);};
 const login=async()=>{await page.locator('#login-password').fill('demo-telescope-2026');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});};
 await page.goto(origin);await login();await page.locator('#nav-configuration').click();await page.locator('#tab-plugins').click();
 await check('authenticated catalog lists registered families plus a community fixture with honest evidence',async()=>{
  assert.equal(await page.locator('[data-plugin-id]').count(),7);
  assert.match(await page.locator('[data-plugin-id="synscan"]').innerText(),/Tested in simulation/);
  assert.match(await page.locator('[data-plugin-id="zwo_mount"]').innerText(),/Partly tested on hardware/);
  assert.equal(await page.locator('[data-plugin-id] .info-button').count(),7);
  assert.equal(await page.locator('[data-plugin-id] input').count(),0);
 });
 await check('one detail modal preserves its expanded section during polling and closes with an accessible cross',async()=>{
  await page.locator('#plugin-dwarf-info').click();assert.equal(await page.locator('#plugin-info').evaluate(e=>e.open),true);
  assert.match(await page.locator('#plugin-info-body').innerText(),/first-time discovery is unavailable/);
  assert.equal(await page.locator('#plugin-info [data-close-help]').innerText(),'✕');
  await page.locator('#plugin-info details summary').click();await page.waitForTimeout(5500);
  assert.equal(await page.locator('#plugin-info details').evaluate(e=>e.open),true);
  await page.keyboard.press('Escape');assert.equal(await page.locator('#plugin-dwarf-info').evaluate(e=>e===document.activeElement),true);
 });
 await check('extra manifest creates a type, preset and persisted profile without core or HTML edits',async()=>{
  await page.locator('#tab-telescopes').click();await page.locator('#add-scope').click();await page.locator("#discovery-manual").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);
  assert.equal(await page.locator('#profile-kind option').count(),7);await page.locator('#profile-kind').selectOption('community_mount');
  assert.equal(await page.locator('#target-ip').inputValue(),'192.168.5.1');await page.locator('#profile-name').fill('My community mount');await page.locator('#network-ssid').fill('Community_DEMO');await page.locator('#network-open').check();
  await page.waitForTimeout(5500);assert.equal(await page.locator('#profile-name').inputValue(),'My community mount');
  await page.locator('#network-save').click();await page.locator('#editor').waitFor({state:'hidden'});
  await page.locator('#tab-plugins').click();assert.match(await page.locator('[data-plugin-id="community_mount"]').innerText(),/Configured profiles: 1.*Connected: 0/s);
  await page.locator('#plugin-community_mount-info').click();assert.equal(await page.locator('#plugin-info details').evaluate(e=>e.open),false);assert.match(await page.locator('#plugin-info-body').innerText(),/TCP.*12345/s);await page.keyboard.press('Escape');
 });
 await check('community manifest exposes the actual connected app endpoint and updates usage',async()=>{
  await page.evaluate(async()=>{const config=configPayload(),profile=config.telescopes.find(p=>p.kind==='community_mount');profile.mac=state.devices.find(d=>d.mac!==state.hotspot.mac&&d.mac!==config.home.mac).mac;profile.enabled=true;await save(config);});
  await page.waitForFunction(()=>state.telescopes.some(p=>p.community_mount_proxy&&p.relay_ready));
  await page.locator('#tab-telescopes').click();const profile=await page.evaluate(()=>state.config.telescopes.find(p=>p.kind==='community_mount'));
  await page.locator(`#profile-${profile.id}-info`).click();assert.match(await page.locator('#profile-info-body').innerText(),/App address: 192\.168\.77\.1 · TCP 12345/);await page.keyboard.press('Escape');
  await page.locator('#tab-plugins').click();assert.match(await page.locator('[data-plugin-id="community_mount"]').innerText(),/Connected: 1/);
 });
 await check('generated automatic choices preserve settings and enforce acknowledgement for a newly enabled plugin',async()=>{
  await page.locator('#tab-general').click();await page.locator('#auto-advanced>summary').click();
  assert.equal(await page.locator('#auto-plugin-community_mount').isChecked(),true);await page.locator('#auto-plugin-community_mount').uncheck();await page.locator('#auto-policy-save').click();
  await page.waitForFunction(()=>state.config.auto_policy.community_mount_demand===false);
  await page.locator('#auto-plugin-community_mount').check();await page.locator('#auto-policy-save').click();
  await page.waitForFunction(()=>document.getElementById('auto-policy-feedback').textContent.includes('not saved'));assert.equal(await page.evaluate(()=>state.config.auto_policy.community_mount_demand),false);
  await page.locator('#auto-ack').check();await page.locator('#auto-policy-save').click();await page.waitForFunction(()=>state.config.auto_policy.community_mount_demand===true);
  assert.equal(await page.evaluate(()=>state.config.auto_policy.force_first_profile),false);
 });
 await check('mobile plugin settings and modals fit all ten languages with stable routes',async()=>{
  await page.locator('#tab-plugins').click();
  for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){
   await page.locator('#locale-menu summary').click();await page.locator(`#locale-menu button[data-locale="${locale}"]`).click();
   await page.setViewportSize({width:320,height:960});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,locale);
   await page.locator('#plugin-synscan-info').click();assert.equal(await page.locator('#plugin-info').evaluate(e=>e.scrollWidth<=e.clientWidth),true,'modal '+locale);await page.keyboard.press('Escape');
  }
  await page.locator('#locale-menu summary').click();await page.locator('#locale-menu button[data-locale="fr"]').click();await page.screenshot({path:resolve(output,'plugins-320-fr.png'),fullPage:true});
  await page.locator('#plugin-zwo_mount-info').click();await page.screenshot({path:resolve(output,'plugin-details-320-fr.png')});await page.keyboard.press('Escape');
  await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});assert.equal(new URL(page.url()).hash,'#configuration/plugins');assert.equal(await page.locator('[data-plugin-id]').count(),7);
 });
 await check('logout clears usage and modal content; login restores catalog-driven controls',async()=>{
  await page.locator('#logout').click();await page.locator('#login').waitFor({state:'visible'});assert.equal(await page.locator('[data-plugin-id]').count(),0);assert.equal(await page.locator('#plugin-info-body').innerText(),'');
  await login();await page.locator('#nav-configuration').click();await page.locator('#tab-plugins').click();assert.equal(await page.locator('[data-plugin-id]').count(),7);
 });
 assert.deepEqual(errors,[]);await writeFile(resolve(output,'browser.json'),JSON.stringify({results,errors,synthetic_community_plugin:true},null,2));
}finally{if(browser)await browser.close();server.kill('SIGTERM');await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});}
