/** Deactivate, then a way back: Reactivate for files still on the bridge, Install otherwise. Isolated demo, no download. */
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
const folder=await mkdtemp(resolve(tmpdir(),'astro-driver-reactivate-')),workspace=resolve(folder,'app'),state=resolve(folder,'state');
const output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/camera-driver-reactivate');await mkdir(output,{recursive:true});await mkdir(workspace);
await cp(resolve(root,'wifi_bridge'),resolve(workspace,'wifi_bridge'),{recursive:true,filter:path=>!path.includes('__pycache__')});
// Synthetic fingerprints make Player One installable in this copy only; ZWO's are the real ones. The demo never downloads.
const catalogPath=resolve(workspace,'wifi_bridge/static/drivers-catalog.json'),catalog=JSON.parse(await readFile(catalogPath,'utf8'));
for(const id of ['player_one']){const build=catalog.drivers.find(d=>d.id===id).builds.find(b=>b.arch==='arm64');
 Object.assign(build,{version:build.version||'1.0',size:1000,sha256:'a'.repeat(64),files:[{member:'lib/libdemo.so',sha256:'b'.repeat(64),install_as:'libdemo.so'}],license_member:null,license_sha256:null});}
await writeFile(catalogPath,JSON.stringify(catalog));
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',state,'--port',String(port)],{cwd:workspace,stdio:['ignore','pipe','pipe']});
let logs='',browser;const errors=[],results=[];server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
const LOCALES=['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru'];
const AFTER=process.env.BEFORE!=='1';
try{
 for(let n=0;!logs.includes('SIMULATION');n++){if(n===150)throw new Error('Server start: '+logs);await new Promise(r=>setTimeout(r,100));}
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:1280,height:1000}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${port}`);await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 await page.locator('#nav-configuration').click();await page.locator('#tab-cameras').click();await page.waitForFunction(()=>cameraDrivers!==null);
 const row=id=>page.locator('#camera-driver-'+id),shot=(name,locator=page.locator('#camera-sdk'))=>locator.screenshot({path:resolve(output,name)});
 const install=async id=>{await page.locator('#camera-driver-install-'+id).click();await page.locator('#camera-driver-accept').click();await page.locator('#camera-driver-withdraw-'+id).waitFor({timeout:15000});};
 // Install two lines: Player One (synthetic build) and ZWO (real catalog build).
 await install('player_one');await install('zwo_asi');
 await shot('P1-installed-en.png');
 // Deactivate: the line keeps a gesture to come back.
 // BEFORE=1 runs this scenario on the code before the change. The old demo went back to Install after Deactivate, while a real bridge answered
 // state 'installed' (files kept, no record) and drew no button: that real answer is replayed here.
 if(!AFTER)await page.route('**/api/cameras/drivers',async r=>{const res=await r.fetch();const json=await res.json();json.drivers.find(d=>d.id==='player_one').state='installed';await r.fulfill({response:res,json});});
 await page.locator('#camera-driver-withdraw-player_one').click();
 await page.waitForFunction(()=>cameraDrivers.drivers.find(d=>d.id==='player_one').state!=='accepted');
 await page.waitForTimeout(300);
 await shot('P2-deactivated-en.png');
 if(!AFTER){console.log('BEFORE: buttons on the deactivated Player One line: '+await row('player_one').locator('button').count());console.log(JSON.stringify(await row('player_one').innerText()));}
 else{
 const text=await row('player_one').innerText();
 assert.match(text,/Installed on this bridge · version 3\.10\.1/);assert.match(text,/Camera driver deactivated\. Cameras that use it are no longer served\./);
 assert.equal(await row('player_one').locator('button').count(),1);assert.equal(await page.locator('#camera-driver-reactivate-player_one').innerText(),'Reactivate');
 assert.equal(await page.locator('#camera-driver-withdraw-player_one').count(),0);assert.equal(await page.locator('#camera-driver-install-player_one').count(),0);
 // The dialog asks for the terms again, says nothing is downloaded, never mentions Internet; Later changes nothing.
 await page.locator('#camera-driver-reactivate-player_one').click();
 assert.equal(await page.locator('#camera-driver-dialog').evaluate(e=>e.open),true);
 assert.match(await page.locator('#camera-driver-dialog-title').innerText(),/Player One camera driver/);
 assert.match(await page.locator('#camera-driver-dialog-text').innerText(),/Version 3\.10\.1 is already on this bridge and nothing is downloaded\. Reactivating the driver means you accept the manufacturer's licence again\./);
 assert.equal(await page.locator('#camera-driver-dialog-internet').isVisible(),false);assert.equal(await page.locator('#camera-driver-accept').isVisible(),false);
 assert.equal(await page.locator('#camera-driver-reactivate-accept').innerText(),'Accept and reactivate');
 assert.equal(await page.locator('#camera-driver-dialog-terms').getAttribute('href'),catalog.drivers.find(d=>d.id==='player_one').terms_url);
 await shot('P3-dialog-en.png',page.locator('#camera-driver-dialog'));
 const buttons=await page.locator('#camera-driver-dialog .actions button:visible').evaluateAll(items=>items.map(i=>i.getBoundingClientRect()).map(r=>({left:r.left,right:r.right,height:Math.round(r.height)})));
 assert.equal(buttons.length,2);assert.equal(new Set(buttons.map(b=>b.height)).size,1);assert.ok(buttons[1].left-buttons[0].right>=8,'buttons apart');
 await page.locator('#camera-driver-later').click();await page.waitForFunction(()=>document.activeElement?.id==='camera-driver-reactivate-player_one');
 assert.equal(await page.locator('#camera-driver-reactivate-player_one').count(),1);
 // Files changed since: the answer says to install again, the dialog stays usable.
 await page.route('**/api/cameras/drivers/player_one/reactivate',r=>r.fulfill({status:409,json:{error:{code:'DRIVER_NOT_DEACTIVATED',message:'The driver files on this bridge have changed. Install the driver again.'}}}));
 await page.locator('#camera-driver-reactivate-player_one').click();await page.locator('#camera-driver-reactivate-accept').click();
 await page.locator('#camera-driver-dialog-feedback').filter({hasText:'Install the driver again.'}).waitFor();
 assert.equal(await page.locator('#camera-driver-reactivate-accept').isEnabled(),true);
 await shot('P4-files-changed-en.png',page.locator('#camera-driver-dialog'));
 await page.unroute('**/api/cameras/drivers/player_one/reactivate');
 await page.locator('#camera-driver-later').click();
 // Reactivate for real: accepted again, same version, Deactivate comes back.
 await page.locator('#camera-driver-reactivate-player_one').click();await page.locator('#camera-driver-reactivate-accept').click();
 await page.locator('#camera-driver-withdraw-player_one').waitFor();
 assert.match(await row('player_one').innerText(),/Installed on .* · version 3\.10\.1/);assert.equal(await page.locator('#camera-driver-deactivated-player_one').count(),0);
 await shot('P5-reactivated-en.png');
 results.push('Deactivate leaves Reactivate; the dialog asks the terms again without Internet; Later changes nothing; a refusal is said in the dialog; Reactivate brings Deactivate back');
 // Two deactivated lines: buttons share one right edge and one height, never touch, ten locales at three widths.
 await page.locator('#camera-driver-withdraw-player_one').click();await page.locator('#camera-driver-reactivate-player_one').waitFor();
 await page.locator('#camera-driver-withdraw-zwo_asi').click();await page.locator('#camera-driver-reactivate-zwo_asi').waitFor();
 const boxes=await page.locator('#camera-driver-list button:visible').evaluateAll(items=>items.map(i=>i.getBoundingClientRect()).map(r=>({right:Math.round(r.right),height:Math.round(r.height)})));
 assert.equal(new Set(boxes.map(b=>b.right)).size,1);assert.equal(new Set(boxes.map(b=>b.height)).size,1);
 for(const width of [320,375,1280]){await page.setViewportSize({width,height:1000});
  for(const locale of LOCALES){await page.evaluate(l=>BridgeI18n.setLocale(l),locale);await page.waitForTimeout(80);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,locale+' '+width);
   const all=await page.locator('#camera-sdk').innerText();assert.doesNotMatch(all,/\{(?:version|date|percent|name)\}/,locale);
   if(locale!=='en'){assert.doesNotMatch(all,/Reactivate|deactivated/,locale);assert.notEqual(await page.locator('#camera-driver-reactivate-zwo_asi').innerText(),'Reactivate',locale);}
   if(width===375&&['fr','de','ja'].includes(locale)){await shot(`P6-deactivated-${locale}-375.png`);}
   if(width===1280&&locale==='fr')await shot('P7-deactivated-fr-1280.png');
  }}
 await page.setViewportSize({width:375,height:1000});
 for(const locale of ['fr','ja']){await page.evaluate(l=>BridgeI18n.setLocale(l),locale);await page.locator('#camera-driver-reactivate-zwo_asi').click();
  const dialog=await page.locator('#camera-driver-dialog').innerText();assert.doesNotMatch(dialog,/\{version\}|downloaded|Reactivat/,locale);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,locale);
  await shot(`P8-dialog-${locale}-375.png`,page.locator('#camera-driver-dialog'));await page.locator('#camera-driver-later').click();}
 await page.setViewportSize({width:1280,height:1000});await page.evaluate(()=>BridgeI18n.setLocale('en'));
 results.push('Ten locales at 320, 375 and 1280 px: Reactivate translated, aligned buttons, no overflow, dialog in French and Japanese');
 }
 assert.deepEqual(errors,[]);
 for(const line of results)console.log('PASS '+line);
}catch(error){console.error(logs.slice(-4000));throw error;}
finally{await browser?.close();server.kill();await once(server,'exit').catch(()=>{});await rm(folder,{recursive:true,force:true});}
