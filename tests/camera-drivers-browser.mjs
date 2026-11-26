/** Camera drivers of several manufacturers on the Cameras page: real demo catalog, then fixtures. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,mkdir,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createServer} from 'node:net';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const folder=await mkdtemp(resolve(tmpdir(),'astro-drivers-ui-')),output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/camera-drivers');await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;const errors=[],results=[];server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
const LOCALES=['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru'];
const FIXTURE={supported:true,catalog_version:'2026.11.19.1',config_revision:'fixture',drivers:[
 {id:'zwo_asi',name:'ZWO ASI',verified_on_hardware:true,terms_url:'https://www.zwoastro.com/software/product-sdk/',state:'accepted',version:'1.42',accepted_utc:'2026-11-19T10:00:00Z',origin:'setup',reason:null,license_text:true},
 {id:'qhyccd',name:'QHYCCD',verified_on_hardware:false,terms_url:'https://www.qhyccd.com/html/prepub/log_en.html',state:'accepted',version:'26.07.28',accepted_utc:'2026-11-19T10:00:00Z',origin:'setup',reason:null,license_text:false},
 {id:'player_one',name:'Player One',verified_on_hardware:false,terms_url:'https://player-one-astronomy.com/service/software/',state:'available',version:'3.10.1',accepted_utc:null,origin:null,reason:null,license_text:false},
 {id:'moravian',name:'Moravian',verified_on_hardware:false,terms_url:'https://www.gxccd.com/cat?id=156&lang=409',state:'impossible',version:null,accepted_utc:null,origin:null,reason:'fingerprint_missing',license_text:false}]};
try{
 for(let n=0;n<150&&!logs.includes('SIMULATION');n++)await new Promise(r=>setTimeout(r,100));
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:1280,height:960}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${port}`);await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 await page.locator('#nav-configuration').click();await page.locator('#tab-cameras').click();await page.waitForFunction(()=>cameraDrivers!==null);
 const reload=async()=>{await page.evaluate(()=>loadCameras());await page.waitForTimeout(300);};
 // Real demo catalog: every manufacturer's fingerprints are pinned, so each can be installed and each carries the unverified mark.
 await page.locator('#camera-sdk').waitFor();
 assert.equal(await page.locator('#camera-sdk-title').innerText(),'Camera drivers');
 assert.deepEqual(await page.locator('#camera-driver-list > li:not([hidden]) .driver-name strong').allInnerTexts(),['ZWO ASI','QHYCCD','Player One','Atik','Moravian']);
 // ZWO was served once, with SDK 1.41 from an image: the SDK 1.42 it now downloads is not verified, so it carries the mark like the others.
 assert.equal(await page.locator('#camera-driver-zwo_asi .driver-unverified').count(),1);
 assert.equal(await page.locator('[data-driver-row] .driver-unverified').count(),5);
 const zwoRow=page.locator('[data-driver-row="zwo_asi"]');
 assert.match(await zwoRow.innerText(),/Not installed\. The bridge downloads it from the manufacturer\./);
 assert.equal(await zwoRow.locator('button').count(),1);assert.equal(await page.locator('#camera-driver-install-zwo_asi').isEnabled(),true);
 for(const row of await page.locator('[data-driver-row]').all()){
  assert.match(await row.innerText(),/Not installed\. The bridge downloads it from the manufacturer\./);assert.equal(await row.locator('button').count(),1);
  const link=row.locator('a.driver-terms');assert.equal(await link.innerText(),'Read the licence');
  assert.match(await link.getAttribute('href'),/^https:\/\//);assert.equal(await link.getAttribute('target'),'_blank');assert.equal(await link.getAttribute('rel'),'noopener noreferrer');
 }
 assert.ok((await page.locator('#camera-drivers-note').innerText()).includes('An unverified driver may not work with your camera.'));
 await page.locator('#camera-driver-zwo_asi').screenshot({path:resolve(output,'real-zwo-en.png')});await page.locator('#camera-sdk').screenshot({path:resolve(output,'real-en-1280.png')});
 results.push('Demo catalog: one line per manufacturer for this hardware, unverified mark, official terms page, every pinned manufacturer installable');
 // The target states of the design, with synthetic data.
 await page.route('**/api/cameras/drivers',r=>r.fulfill({status:200,json:FIXTURE}));
 await reload();
 assert.match(await page.locator('#camera-driver-zwo_asi').innerText(),/Installed at setup on .* · version 1\.42/);
 assert.match(await page.locator('#camera-driver-qhyccd').innerText(),/Installed at setup on .* · version 26\.07\.28/);
 assert.equal(await page.locator('#camera-driver-withdraw-qhyccd').isVisible(),true);
 assert.equal(await page.locator('#camera-driver-install-player_one').isDisabled(),false);
 assert.match(await page.locator('#camera-driver-player_one').innerText(),/The bridge needs Internet to download it\./);
 // Buttons of the rows share one right edge and one height, and never touch each other.
 const boxes=await page.locator('#camera-driver-list button:visible').evaluateAll(items=>items.map(i=>i.getBoundingClientRect()).map(r=>({right:Math.round(r.right),height:Math.round(r.height)})));
 assert.equal(new Set(boxes.map(b=>b.right)).size,1);assert.equal(new Set(boxes.map(b=>b.height)).size,1);
 for(const width of [320,375,1280]){
  await page.setViewportSize({width,height:960});
  for(const locale of LOCALES){
   await page.evaluate(l=>BridgeI18n.setLocale(l),locale);await page.waitForTimeout(80);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,locale+' '+width);
   const text=await page.locator('#camera-sdk').innerText();
   assert.doesNotMatch(text,/\{(?:version|date)\}/,locale);
   if(locale!=='en')assert.notEqual(await page.locator('#camera-sdk-title').innerText(),'Camera drivers',locale);
   if((locale==='fr'||locale==='en'||locale==='de')&&width!==320)await page.locator('#camera-sdk').screenshot({path:resolve(output,`fixture-${locale}-${width}.png`)});
  }
 }
 results.push('Design states: ZWO and another manufacturer installed at setup, installed, to install (needs Internet), impossible; aligned buttons; ten locales at 320, 375 and 1280 px');
 // Cameras of manufacturers whose adapters are not verified on hardware: the card says so, and what to do.
 const camera=(id,driver,discovery)=>({id,instrument_id:id,name:driver,driver,state:'error',alpaca:null,last_image:{bytes:0,seconds:0},
  usb:{vendor_id:1,product_id:1,device_class:0,interface_classes:[255],speed_mbps:480,bus:1,port_path:'1-2'},
  discovery:{state:discovery,disconnects:0,power_unstable:false},verified_on_hardware:false});
 await page.route('**/api/cameras',r=>r.request().method()==='GET'?r.fulfill({status:200,json:[camera('cam-p1','player_one','driver_missing'),camera('cam-q1','qhyccd','unsupported')]}):r.continue());
 await page.setViewportSize({width:1280,height:960});await page.evaluate(()=>BridgeI18n.setLocale('en'));
 await page.locator('#nav-telescopes').click();await reload();await page.locator('article[data-camera-id="cam-p1"]').waitFor();
 const p1=page.locator('article[data-camera-id="cam-p1"]'),q1=page.locator('article[data-camera-id="cam-q1"]');
 assert.match(await p1.innerText(),/Install the Player One driver in Camera drivers below, then reconnect the camera\./);
 assert.match(await p1.innerText(),/Driver not verified on hardware\. Tell us if it works\./);
 assert.match(await q1.innerText(),/Connect only one QHYCCD camera at a time\. Some models cannot be served yet\./);
 assert.equal(await page.locator('article [data-camera-unverified]').count(),2);
 for(const width of [320,375]){
  await page.setViewportSize({width,height:960});
  for(const locale of LOCALES){
   await page.evaluate(l=>BridgeI18n.setLocale(l),locale);await page.waitForTimeout(80);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,locale+' '+width);
   const text=await p1.innerText()+await q1.innerText();
   assert.doesNotMatch(text,/\{name\}/,locale);
   if(locale!=='en')assert.doesNotMatch(text,/Tell us if it works/,locale);
   if(locale==='fr'){assert.match(text,/Pilote non vérifié sur matériel\. Dites-nous si cela fonctionne\./);assert.match(text,/Installez le pilote Player One plus bas/);}
   if((locale==='fr'||locale==='en'||locale==='ja')&&width===375)await page.locator('#camera-cards').screenshot({path:resolve(output,`unverified-cameras-${locale}-375.png`)});
  }
 }
 await page.unroute('**/api/cameras');
 await page.setViewportSize({width:1280,height:960});await page.evaluate(()=>BridgeI18n.setLocale('en'));
 await page.locator('#nav-configuration').click();await page.locator('#tab-cameras').click();
 results.push('Cameras of unverified manufacturers: what to do and "not verified on hardware" on the card, ten locales at 320 and 375 px');
 // Hardware where the camera service never runs (Pi Zero W).
 await page.unroute('**/api/cameras/drivers');
 await page.route('**/api/cameras/drivers',r=>r.fulfill({status:200,json:{supported:false,catalog_version:'2026.11.19.1',config_revision:'f',drivers:[]}}));
 await page.evaluate(()=>BridgeI18n.setLocale('fr'));await reload();
 assert.equal((await page.locator('#camera-sdk').innerText()).replace(/\n+/g,'\n').trim(),'Pilotes de caméras\nNon supporté par ce matériel.');
 await page.locator('#camera-sdk').screenshot({path:resolve(output,'unsupported-fr.png')});
 results.push('Hardware without camera service: the block says it is not supported by this hardware');
 assert.deepEqual(errors,[]);
 for(const line of results)console.log('PASS '+line);
}catch(error){console.error(logs.slice(-4000));throw error;}
finally{await browser?.close();server.kill();await once(server,'exit').catch(()=>{});await rm(folder,{recursive:true,force:true});}
