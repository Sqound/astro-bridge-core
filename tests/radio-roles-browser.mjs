/** Mobile adapter roles through the actual demo server and guarded form. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,mkdir,rm,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {createServer} from 'node:net';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE).href);
const folder=await mkdtemp(resolve(tmpdir(),'astro-role-browser-')),output=resolve(root,process.env.UI_ARTIFACTS||'../release-artifacts/radio-roles-ui');await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});let logs='',browser;
server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
const results=[],errors=[];
const wait=async fn=>{for(let i=0;i<150;i++){if(await fn())return;await new Promise(r=>setTimeout(r,100));}throw new Error('Timeout: '+logs);};
try{
 await wait(()=>logs.includes('SIMULATION'));
 browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH});
 const page=await browser.newPage({viewport:{width:375,height:900},colorScheme:"dark"});page.setDefaultTimeout(15000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${port}`);await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 await page.goto(`http://127.0.0.1:${port}/#configuration/radios`);await page.locator('#panel-radios').waitFor({state:'visible'});
 const check=async(name,fn)=>{await fn();results.push(name);console.log('PASS',name);};
 let original,target,third;
 await check('default hotspot reservation shown separately from current use',async()=>{
  const data=await page.evaluate(()=>({ap:state.hotspot.mac,devices:state.devices}));original=data.ap;target=data.devices.find(d=>d.mac!==original).mac;third=data.devices.find(d=>![original,target].includes(d.mac)).mac;
  assert.equal(await page.locator(`select[data-role-mac="${original}"]`).inputValue(),'hotspot');assert.equal(await page.locator('#radios select').count(),4);
  assert.deepEqual(await page.locator('#radios select').first().locator('option').evaluateAll(items=>items.map(x=>x.value)),['auto','home','hotspot','telescope']);
 });
 await check('draft survives normal polling and transfers the unique hotspot choice',async()=>{
  await page.locator(`select[data-role-mac="${target}"]`).selectOption('hotspot');
  assert.equal(await page.locator(`select[data-role-mac="${original}"]`).inputValue(),'auto');
  await page.locator(`select[data-role-mac="${third}"]`).selectOption('telescope');
  await page.waitForTimeout(6500);assert.equal(await page.locator(`select[data-role-mac="${target}"]`).inputValue(),'hotspot');assert.equal(await page.locator(`select[data-role-mac="${third}"]`).inputValue(),'telescope');
  await page.locator('#interface-role-save').click();await page.waitForFunction(mac=>state.config.interface_roles?.[mac]==='hotspot',target);await page.waitForFunction(mac=>state.hotspot.mac===mac,target);
 });
 await check('saved purposes survive page reload and the hotspot is excluded from mount connections',async()=>{
  await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});await page.locator('#panel-radios').waitFor({state:'visible'});
  assert.equal(await page.locator(`select[data-role-mac="${target}"]`).inputValue(),'hotspot');
  assert.equal(await page.locator(`select[data-role-mac="${third}"]`).inputValue(),'telescope');
  const forbidden=await page.evaluate(mac=>[...connectionGroups(state).values()].some(g=>g.device?.mac===mac&&!g.reserved),target);assert.equal(forbidden,false);
  await page.evaluate(()=>{document.documentElement.dataset.theme='dark';});await page.locator('#panel-radios').screenshot({path:resolve(output,'adapter-roles-night-375.png')});
 });
 await check('private network form does not ask again for an adapter reserved for it',async()=>{
  await page.evaluate(mac=>openEditor(null,true),third);
  assert.equal(await page.locator('#radio-fields').isVisible(),true);await page.locator('#close-editor').click();
  await page.locator(`select[data-role-mac="${third}"]`).selectOption('home');await page.locator('#interface-role-save').click();
  await page.waitForFunction(mac=>state.config.interface_roles?.[mac]==='home',third);
  await page.evaluate(()=>openEditor(null,true));
  assert.equal(await page.locator('#radio-fields').isVisible(),false);assert.equal(await page.locator('#network-interface').inputValue(),third);
  await page.locator('#close-editor').click();
  await page.locator(`select[data-role-mac="${third}"]`).selectOption('telescope');await page.locator('#interface-role-save').click();
  await page.waitForFunction(mac=>state.config.interface_roles?.[mac]==='telescope',third);
 });
 await check('missing reserved adapters stay visible with a useful warning',async()=>{
  await page.evaluate(mac=>{const fake=structuredClone(state);fake.devices=fake.devices.filter(d=>d.mac!==mac);renderRadioRoles(fake);},target);
  assert.equal(await page.locator(`select[data-role-mac="${target}"]`).inputValue(),'hotspot');assert.equal(await page.locator('#interface-role-warning').isVisible(),true);assert.match(await page.locator('#interface-role-warning').innerText(),/missing/i);
  await page.locator('#panel-radios').screenshot({path:resolve(output,'adapter-missing-night-375.png')});await page.evaluate(()=>renderRadioRoles(state));
 });
 await check('an adapter behind a USB hub is flagged on its row and in the adapter notice',async()=>{
  // The bridge reports the hub in its status; every later poll keeps saying so.
  await page.route('**/api/status*',async route=>{const response=await route.fetch();const body=await response.json();body.devices=body.devices.map(d=>d.mac===third?{...d,usb:true,usb_hub:true}:d);await route.fulfill({response,json:body});});
  await page.waitForFunction(mac=>state.devices.some(d=>d.mac===mac&&d.usb_hub),third);
  const row=page.locator(`select[data-role-mac="${third}"]`).locator('xpath=ancestor::div[contains(@class,"radio-row")]');
  assert.match(await row.locator('.radio-hub-warning').innerText(),/USB hub/);assert.equal(await page.locator('.radio-hub-warning').count(),1);
  assert.match(await page.locator('#adapter-notice').textContent(),/USB hub/);
  await page.evaluate(()=>BridgeI18n.setLocale('fr'));assert.match(await row.locator('.radio-hub-warning').innerText(),/hub USB/);
  await page.locator('#panel-radios').screenshot({path:resolve(output,'adapter-usb-hub-night-fr-375.png')});await page.evaluate(()=>BridgeI18n.setLocale('en'));
  await page.unroute('**/api/status*');await page.waitForFunction(()=>!state.devices.some(d=>d.usb_hub));await page.waitForFunction(()=>!document.querySelector('.radio-hub-warning'));
 });
 await check('ten locales fit at 320px',async()=>{
  await page.setViewportSize({width:320,height:900});
  for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){await page.evaluate(tag=>BridgeI18n.setLocale(tag),locale);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,locale);}
  await page.evaluate(()=>BridgeI18n.setLocale('fr'));await page.locator('#panel-radios').screenshot({path:resolve(output,'adapter-roles-night-fr-320.png')});
 });
 assert.deepEqual(errors,[]);await writeFile(resolve(output,'results.json'),JSON.stringify({results,errors},null,2));
}finally{if(browser)await browser.close();server.kill('SIGTERM');await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});}
