/** Mount profiles and switching settings against an isolated simulated server. */
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
const folder=await mkdtemp(resolve(tmpdir(),'astro-mounts-')),output=resolve(root,'artifacts/mounts-0630');
await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;const errors=[],results=[];
server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
try{
 for(let n=0;!logs.includes('SIMULATION');n++){if(n===100)throw new Error('Server start: '+logs);await new Promise(r=>setTimeout(r,100));}
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:960}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${port}`);await page.locator('#login-password').fill('demo-telescope-2026');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 const check=async(name,fn)=>{await fn();results.push(name);console.log('PASS',name);};
 const data=()=>page.evaluate(()=>state);
 await check('fresh switching settings include both mount families and retain explicit choices',async()=>{
  await page.locator('#nav-configuration').click();await page.locator('#tab-general').click();await page.locator('#auto-advanced>summary').click();
  assert.equal(await page.locator('#auto-zwo-mount-demand').isChecked(),true);assert.equal(await page.locator('#auto-synscan-demand').isChecked(),true);
  await page.locator('#auto-zwo-mount-demand').uncheck();await page.locator('#auto-policy-save').click();
  await page.waitForFunction(()=>state.config.auto_policy.zwo_mount_demand===false);
  assert.equal((await data()).config.auto_policy.synscan_demand,true);
  await page.locator('#auto-policy-reload').click();assert.equal(await page.locator('#auto-zwo-mount-demand').isChecked(),false);
 });
 await check('mount presets save without fabricated credentials or physical identity and retain draft edits',async()=>{
  await page.locator('#tab-telescopes').click();
  for(const [kind,name] of [['zwo_mount','AM5N synthetic'],['synscan','SynScan synthetic']]){
   await page.locator('#add-scope').click();await page.locator("#discovery-manual").click();await page.locator("#profile-advanced").evaluate(e=>e.open=true);await page.locator("#physical-advanced").evaluate(e=>e.open=true);await page.locator('#profile-kind').selectOption(kind);
   assert.equal(await page.locator('#target-ip').inputValue(),'192.168.4.1');assert.equal(await page.locator('#network-password').inputValue(),'');
   await page.locator('#profile-name').fill(name);await page.locator('#network-ssid').fill('Synthetic mount network');await page.locator('#network-password').fill('synthetic-mount-pass');
   await page.waitForTimeout(5500);assert.equal(await page.locator('#profile-name').inputValue(),name);
   await page.locator('#network-save').click();await page.locator('#editor').waitFor({state:'hidden'});
   const profile=(await data()).config.telescopes.find(p=>p.name===name);assert.equal(profile.kind,kind);assert.equal(profile.enabled,false);assert.equal(profile.mac,'');assert.ok(!profile.physical_binding);
  }
 });
 await check('simulated selected mount advertises exact native transport and modal explains guiding protection',async()=>{
  for(const [kind,port,transport] of [['zwo_mount',4030,'TCP'],['synscan',11880,'UDP']]){
   await page.evaluate(async kind=>{const config=configPayload(),device=state.devices.find(d=>d.mac!==state.hotspot.mac&&d.mac!==config.home.mac);for(const p of config.telescopes){p.enabled=p.kind===kind;if(p.enabled)p.mac=device.mac;}await save(config);},kind);
   await page.waitForFunction(kind=>state.telescopes.some(p=>p[kind+'_proxy']&&p.relay_ready),kind);
   const profile=(await data()).config.telescopes.find(p=>p.kind===kind);
   const row=page.locator(`#profile-${profile.id}-info`).locator('xpath=ancestor::tr');
   assert.match(await row.innerText(),new RegExp(`${transport} ${port}`));
   await page.locator(`#profile-${profile.id}-info`).click();assert.match(await page.locator('#profile-info-body').innerText(),/Control and guiding traffic/);
   assert.doesNotMatch(await page.locator('#profile-info-body').innerText(),/Small status exchanges/);await page.keyboard.press('Escape');
  }
 });
 await check('mount profiles fit narrow screens in every language',async()=>{
  for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){
   await page.locator('#locale-menu summary').click();await page.locator(`#locale-menu button[data-locale="${locale}"]`).click();
   await page.setViewportSize({width:320,height:960});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,locale);
  }
  await page.locator('#locale-menu summary').click();await page.locator('#locale-menu button[data-locale="en"]').click();await page.screenshot({path:resolve(output,'mounts-320.png'),fullPage:true});
 });
 assert.deepEqual(errors,[]);await writeFile(resolve(output,'browser.json'),JSON.stringify({results,errors,simulation:true},null,2));
}finally{
 if(browser)await browser.close();server.kill('SIGTERM');await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});
}
