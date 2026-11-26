/** Installing a camera driver from the bridge, and what the first boot did with Setup's drivers, in an isolated demo. */
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
const folder=await mkdtemp(resolve(tmpdir(),'astro-driver-install-')),workspace=resolve(folder,'app'),state=resolve(folder,'state');
const output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/camera-driver-install');await mkdir(output,{recursive:true});await mkdir(workspace);
await cp(resolve(root,'wifi_bridge'),resolve(workspace,'wifi_bridge'),{recursive:true,filter:path=>!path.includes('__pycache__')});
// Synthetic fingerprints make Player One and Atik installable in this copy only; the demo never downloads.
const catalogPath=resolve(workspace,'wifi_bridge/static/drivers-catalog.json'),catalog=JSON.parse(await readFile(catalogPath,'utf8'));
for(const id of ['player_one','atik']){const build=catalog.drivers.find(d=>d.id===id).builds.find(b=>b.arch==='arm64');
 Object.assign(build,{version:build.version||'1.0',size:1000,sha256:'a'.repeat(64),files:[{member:'lib/libdemo.so',sha256:'b'.repeat(64),install_as:'libdemo.so'}],license_member:null,license_sha256:null});}
await writeFile(catalogPath,JSON.stringify(catalog));
// What a first boot wrote after Setup: ZWO installed (since withdrawn), Atik refused, an unknown manufacturer.
await mkdir(resolve(state,'demo-camera-drivers'),{recursive:true});
await writeFile(resolve(state,'demo-camera-drivers/setup-results.json'),JSON.stringify({zwo_asi:'installed',atik:'archive_changed',touptek:'unknown_driver'}));
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',state,'--port',String(port)],{cwd:workspace,stdio:['ignore','pipe','pipe']});
let logs='',browser;const errors=[],results=[];server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
const LOCALES=['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru'];
try{
 for(let n=0;!logs.includes('SIMULATION');n++){if(n===150)throw new Error('Server start: '+logs);await new Promise(r=>setTimeout(r,100));}
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:1280,height:1000}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${port}`);await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 await page.locator('#nav-configuration').click();await page.locator('#tab-cameras').click();await page.waitForFunction(()=>cameraDrivers!==null);
 await page.locator('#camera-driver-atik').waitFor();
 // Setup outcomes, in plain words, on the row they concern; an unknown manufacturer under the list.
 assert.match(await page.locator('#camera-driver-setup-atik').innerText(),/The file Astro Bridge Setup placed on the card is not the expected one\. It was not installed\./);
 assert.equal(await page.locator('#camera-driver-setup-player_one').count(),0);
 assert.match(await page.locator('#camera-driver-setup-zwo_asi').innerText(),/Ticked in Astro Bridge Setup, no longer active\./);
 assert.match(await page.locator('#camera-drivers-setup').innerText(),/touptek · Astro Bridge Setup placed a driver this bridge does not know\./);
 results.push('Setup outcomes: refused file on its row, ZWO installed by Setup and no longer active, unknown manufacturer under the list');
 // Install: the dialog names the version, the licence page and the need for Internet; Later changes nothing.
 await page.locator('#camera-driver-install-player_one').click();
 assert.equal(await page.locator('#camera-driver-dialog').evaluate(e=>e.open),true);
 assert.match(await page.locator('#camera-driver-dialog-title').innerText(),/Player One camera driver/);
 assert.match(await page.locator('#camera-driver-dialog-text').innerText(),/version 3\.10\.1/);
 assert.equal(await page.locator('#camera-driver-dialog-unverified').isVisible(),true);
 assert.equal(await page.locator('#camera-driver-dialog-terms').getAttribute('href'),catalog.drivers.find(d=>d.id==='player_one').terms_url);
 await page.locator('#camera-sdk').screenshot({path:resolve(output,'before-install-en.png')});await page.locator('#camera-driver-dialog').screenshot({path:resolve(output,'dialog-en.png')});
 const buttons=await page.locator('#camera-driver-dialog .actions button:visible').evaluateAll(items=>items.map(i=>i.getBoundingClientRect()).map(r=>({left:r.left,right:r.right,height:Math.round(r.height)})));
 assert.equal(new Set(buttons.map(b=>b.height)).size,1);assert.ok(buttons[1].left-buttons[0].right>=8,'buttons apart');
 await page.locator('#camera-driver-later').click();await page.waitForFunction(()=>document.activeElement?.id==='camera-driver-install-player_one');
 // Offline: plain explanation of what to do, and Try again.
 await page.evaluate(async()=>{await cameraRequest('/demo','POST',{internet:false},cameraRevision);});
 await page.locator('#camera-driver-install-player_one').click();await page.locator('#camera-driver-accept').click();
 await page.locator('#camera-driver-install-state-player_one').filter({hasText:'No Internet connection'}).waitFor();
 assert.match(await page.locator('#camera-driver-player_one').innerText(),/Connect it to a Wi-Fi network with Internet in Networks, then try again\. You can also write the card again with Astro Bridge Setup and tick this driver\./);
 assert.equal(await page.locator('#camera-driver-install-player_one').innerText(),'Try again');
 await page.locator('#camera-sdk').screenshot({path:resolve(output,'offline-en.png')});
 results.push('Offline: the row says what to do (Networks, or Setup) and offers Try again');
 // Online: progress, then installed by the owner, then Deactivate.
 await page.evaluate(async()=>{await cameraRequest('/demo','POST',{internet:true},cameraRevision);});
 await page.locator('#camera-driver-install-player_one').click();await page.locator('#camera-driver-accept').click();
 await page.locator('#camera-driver-player_one').filter({hasText:/Downloading from the manufacturer… \d+ %|Checking and installing…|Installed on/}).waitFor();
 await page.locator('#camera-driver-withdraw-player_one').waitFor({timeout:15000});
 assert.match(await page.locator('#camera-driver-player_one').innerText(),/Installed on .* · version 3\.10\.1/);
 assert.equal(await page.locator('#camera-driver-install-player_one').count(),0);
 await page.locator('#camera-sdk').screenshot({path:resolve(output,'installed-en.png')});
 results.push('Online: download progress, then installed with the version, then Deactivate');
 // Ten languages, narrow phones: no overflow, no raw placeholder.
 await page.evaluate(async()=>{await cameraRequest('/demo','POST',{internet:false},cameraRevision);});
 await page.locator('#camera-driver-install-atik').click();await page.locator('#camera-driver-accept').click();
 await page.locator('#camera-driver-install-state-atik').waitFor();
 for(const width of [320,375]){await page.setViewportSize({width,height:1000});
  for(const locale of LOCALES){await page.evaluate(l=>BridgeI18n.setLocale(l),locale);await page.waitForTimeout(80);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,locale+' '+width);
   const text=await page.locator('#camera-sdk').innerText();assert.doesNotMatch(text,/\{(?:version|date|percent|name)\}/,locale);
   if(locale!=='en')assert.doesNotMatch(text,/No Internet connection on the bridge/,locale);
   if(width===375&&['fr','de','ja'].includes(locale))await page.locator('#camera-sdk').screenshot({path:resolve(output,`offline-${locale}-375.png`)});
  }}
 results.push('Ten locales at 320 and 375 px: setup outcomes and offline explanation translated, nothing overflows');
 assert.deepEqual(errors,[]);
 for(const line of results)console.log('PASS '+line);
}catch(error){console.error(logs.slice(-4000));throw error;}
finally{await browser?.close();server.kill();await once(server,'exit').catch(()=>{});await rm(folder,{recursive:true,force:true});}
