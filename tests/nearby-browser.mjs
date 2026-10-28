import {clickUserControl} from "./browser-helpers.mjs";
/** Idle background Wi-Fi suggestions: no browser scan request or connection side effects. */
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
const folder=await mkdtemp(resolve(tmpdir(),'astro-nearby-')),output=resolve(root,'artifacts/nearby-0635');await mkdir(output,{recursive:true});
const rowsFile=resolve(folder,'wifi.json'),rows=[['S50_NEW_NEARBY','02:00:00:77:88:01','WPA2'],['vespera-NEARBY','02:00:00:77:88:02','--'],['Office-unrelated','02:00:00:77:88:03','WPA2']].map(([ssid,bssid,security])=>({ssid,bssid,security,signal:90,frequency:2412}));await writeFile(rowsFile,JSON.stringify(rows));
const runner=resolve(folder,'run.py');await writeFile(runner,`import sys,json\nfrom pathlib import Path\nsys.path.insert(0,${JSON.stringify(root)})\nfrom wifi_bridge.network import DemoNetwork\nDemoNetwork.scan=lambda self,name,**kwargs: json.loads(Path(${JSON.stringify(rowsFile)}).read_text())\nfrom wifi_bridge.__main__ import main\nmain()\n`);
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u',runner,'--demo','--demo-radios','3','--state-dir',resolve(folder,'state'),'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;const results=[],errors=[];server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
try{
 for(let n=0;!logs.includes('SIMULATION');n++){if(n===100)throw new Error('Server start: '+logs);await new Promise(r=>setTimeout(r,100));}
 const origin=`http://127.0.0.1:${port}`;
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:960}});page.setDefaultTimeout(15000);page.on('pageerror',e=>errors.push(e.message));
 let explicitScans=0;page.on('request',r=>{if(r.url().endsWith('/api/discovery')&&r.method()==='POST')explicitScans++;});
 const check=async(name,fn)=>{await fn();results.push(name);console.log('PASS',name);};
 const login=async()=>{await page.locator('#login-password').fill('demo-telescope-2026');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});};
 await page.goto(origin);await login();
 await check('recognized unsaved Wi-Fi appears on the home page without opening scan or adding a profile',async()=>{
  await page.locator('#nearby-telescopes').waitFor({state:'visible'});assert.equal(await page.locator('.nearby-telescope').count(),2);assert.doesNotMatch(await page.locator('#nearby-list').innerText(),/Office/);
  assert.equal(explicitScans,0);assert.equal(await page.evaluate(()=>state.config.telescopes.length),0);assert.equal(await page.evaluate(()=>state.nearby_telescopes.interval_seconds),45);
 });
 await check('nearby card fits every language at 320 pixels and retains focus through polls',async()=>{
  for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){
   await page.evaluate(locale=>BridgeI18n.setLocale(locale),locale);await page.setViewportSize({width:320,height:960});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,locale);
  }
  await page.evaluate(()=>BridgeI18n.setLocale('fr'));const button=page.locator('.nearby-telescope button').first(),id=await button.getAttribute('id');await button.focus();await page.evaluate(()=>refresh());assert.equal(await page.evaluate(()=>document.activeElement.id),id);
  await page.locator('#nearby-telescopes').screenshot({path:resolve(output,'nearby-320-fr.png')});await page.evaluate(()=>BridgeI18n.setLocale('en'));
 });
 let profile;
 await check('Add opens the existing prefilled second step; saved telescope leaves the suggestions immediately',async()=>{
  await page.locator('.nearby-telescope').filter({hasText:'S50_NEW_NEARBY'}).locator('button').click();await page.locator('#editor').waitFor({state:'visible'});
  assert.equal(await page.locator('#network-ssid').inputValue(),'S50_NEW_NEARBY');assert.equal(await page.locator('#profile-kind').inputValue(),'seestar');assert.equal(await page.locator('#target-ip').inputValue(),'10.0.0.1');assert.equal(await page.locator('#network-password').inputValue(),'12345678');assert.equal(explicitScans,0);
  await page.locator('#network-save').click();await page.waitForFunction(()=>state.config.telescopes.length===1);profile=await page.evaluate(()=>state.config.telescopes[0]);assert.equal(profile.enabled,false);
  await page.locator('#nav-telescopes').click();assert.equal(await page.locator('.nearby-telescope').count(),1);assert.doesNotMatch(await page.locator('#nearby-list').innerText(),/S50_NEW_NEARBY/);
 });
 await check('starting a saved telescope hides nearby suggestions; disconnecting restores recent results',async()=>{
  await page.locator(`[id^="simple-connect-"][id$="-${profile.id}"]:enabled`).first().click();await page.waitForFunction(()=>state.config.telescopes[0].enabled);assert.equal(await page.locator('#nearby-telescopes').isVisible(),false);
  await page.waitForFunction(()=>state.telescopes.some(t=>t.relay_ready));await page.locator(`[id^="simple-connect-"][id$="-${profile.id}"]:enabled`).first().click();await page.waitForFunction(()=>!state.config.telescopes[0].enabled&&!state.telescopes.some(t=>t.connected));await page.locator('#nearby-telescopes').waitFor({state:'visible'});
 });
 await check('logout clears nearby private names; login restores only current authenticated suggestions',async()=>{
  await clickUserControl(page, '#logout');await page.locator('#login').waitFor({state:'visible'});assert.equal(await page.locator('#nearby-list').innerText(),'');assert.equal(await page.locator('#nearby-telescopes').isVisible(),false);
  await login();await page.locator('#nearby-telescopes').waitFor({state:'visible'});assert.equal(await page.locator('.nearby-telescope').count(),1);
 });
 await check('next real scheduled scan withdraws missing Wi-Fi without creating profiles or browser scan requests',async()=>{
  await writeFile(rowsFile,'[]');await page.waitForFunction(()=>state.nearby_telescopes.candidates.length===0&&state.nearby_telescopes.scan_age_seconds<10,{},{timeout:65000});
  assert.equal(await page.locator('#nearby-telescopes').isVisible(),false);assert.equal(explicitScans,0);assert.equal(await page.evaluate(()=>state.config.telescopes.length),1);assert.equal(await page.evaluate(()=>state.config.telescopes[0].enabled),false);
 });
 assert.deepEqual(errors,[]);await writeFile(resolve(output,'browser.json'),JSON.stringify({results,errors,simulation:true,no_browser_scan_requests:explicitScans===0},null,2)+'\n');
}finally{if(browser)await browser.close();server.kill('SIGTERM');await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});}
