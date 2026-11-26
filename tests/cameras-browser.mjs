/** Explicit disposable simulator, normalized error fixtures, and real owner release. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,mkdir,rm,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createServer} from 'node:net';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const folder=await mkdtemp(resolve(tmpdir(),'astro-cameras-ui-')),output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/cameras');await mkdir(output,{recursive:true});
async function freePort(){const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));return port;}
const port=await freePort(),cameraPort=await freePort();
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;const errors=[],results=[];server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
const wait=async fn=>{for(let n=0;n<150;n++){if(await fn())return;await new Promise(r=>setTimeout(r,100));}throw new Error('Camera wait timed out');};
try{
 await wait(()=>logs.includes('SIMULATION'));
 const origin=`http://127.0.0.1:${port}`;
 const login=await fetch(origin+'/api/v1/auth/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:'12345678'})}),token=(await login.json()).access_token;
 const request=async(method,path,body)=>{const revision=method==='GET'?null:(await request('GET','/cameras/settings')).config_revision;const response=await fetch(origin+'/api/v1'+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json',...(revision?{'If-Match':'"'+revision+'"'}:{})},...(body?{body:JSON.stringify(body)}:{})});const value=await response.json();assert.ok(response.ok,`Camera API ${path}: ${response.status}`);return value;};
 assert.deepEqual(await request('GET','/cameras'),[]);await request('PATCH','/cameras/settings',{port:cameraPort});
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:960}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(origin);await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 await page.locator('#nav-configuration').click();await page.locator('#tab-cameras').click();await page.waitForFunction(()=>cameraSettings!==null);
 await page.locator('#camera-demo-plugged').check();await wait(async()=>(await request('GET','/cameras'))[0]?.alpaca);await page.evaluate(()=>loadCameras());
 assert.equal(await page.locator('#camera-table-body tr').count(),1);await page.locator('#nav-telescopes').click();await page.locator('[data-camera-state="Ready"]').waitFor();
 assert.ok((await page.locator('#camera-cards').innerText()).includes(`http://127.0.0.1:${cameraPort}`));
 results.push('Production demo starts empty; explicit plug creates a real loopback-only simulator endpoint and ready card');
 const alpaca=async(member,params,client=7)=>{const response=await fetch(`http://127.0.0.1:${cameraPort}/api/v1/camera/0/${member}`,{method:'PUT',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({ClientID:String(client),...params})});return response.json();};
 assert.equal((await alpaca('connected',{Connected:'true'})).ErrorNumber,0);
 assert.equal((await alpaca('startexposure',{Duration:'3600',Light:'true'})).ErrorNumber,0);await page.evaluate(()=>loadCameras());await page.locator('[data-camera-state="In use"]').waitFor();
 assert.ok((await page.locator('#camera-cards').innerText()).includes('Alpaca ClientID 7 · 127.0.0.1'));
 assert.equal((await alpaca('startexposure',{Duration:'1',Light:'true'},8)).ErrorNumber,0x40B);await page.evaluate(()=>loadCameras());await page.locator('[data-camera-state="Busy"]').waitFor();
 await page.locator('#camera-release-demo-camera').click();assert.equal(await page.locator('#camera-release-cancel').evaluate(e=>e===document.activeElement),true);await page.keyboard.press('Escape');
 assert.equal(await page.locator('#camera-release-demo-camera').evaluate(e=>e===document.activeElement),true);assert.ok((await request('GET','/cameras'))[0].owner);
 await page.locator('#camera-release-demo-camera').click();
 const old=(await request('GET','/cameras'))[0].owner;
 await request('POST','/cameras/demo-camera/release-owner',{confirm_release:true,owner_generation:old.generation});
 await alpaca('connected',{Connected:'true'});await alpaca('startexposure',{Duration:'3600',Light:'true'});
 await page.locator('#camera-release-confirm').click();await page.waitForFunction(()=>document.querySelector('#camera-release-feedback').textContent.includes('Camera owner changed.'));
 assert.ok((await request('GET','/cameras'))[0].owner);await page.locator('#camera-release-cancel').click();await page.evaluate(()=>loadCameras());await page.locator('#camera-release-demo-camera').click();await page.locator('#camera-release-confirm').click();await page.locator('#camera-release-dialog').waitFor({state:'hidden'});
 assert.ok(!(await request('GET','/cameras'))[0].owner);results.push('Real Alpaca exposure and foreign-client refusal; cancel/Escape/focus; stale same-client owner rejected; confirmed current owner released');
 let fixture=(await request('GET','/cameras'))[0];const baseline=structuredClone(fixture);
 await page.route('**/api/cameras',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify([fixture])}));
 for(const [label,patch] of [['Detected',{alpaca:null,discovery:{state:'preparing'}}],['Driver missing',{alpaca:null,state:'error',discovery:{state:'driver_missing'}}],['Camera unsupported',{alpaca:null,state:'error',discovery:{state:'unsupported'}}],['Unstable power',{discovery:{state:'ready',power_unstable:true,disconnects:3}}],['Heavy images',{sensor:{width:4096,height:2048,bits:16,max_bin:2},defaults:{bin:2,gain:0}}]]){
  fixture={...structuredClone(baseline),...patch};await page.evaluate(label=>{if(label==='Heavy images')state.devices=state.devices.map(d=>({...d,bands:['2.4']}));},label);await page.evaluate(()=>loadCameras());await page.locator(`[data-camera-state="${label}"]`).waitFor();
  if(label==='Heavy images')assert.ok((await page.locator('#camera-cards').innerText()).includes('Binning 2 is the initial default'));
 }
 // No manufacturer file comes with an image: a camera waiting for its driver points to Camera drivers, ZWO included.
 for(const [stateName,description] of [['driver_missing','Install the ZWO ASI driver in Camera drivers below, then reconnect the camera.'],['unsupported','Connect a single ASI camera']]){
  fixture={...structuredClone(baseline),driver:'zwo_asi',alpaca:null,discovery:{state:stateName}};
  await page.evaluate(()=>loadCameras());
  await page.waitForFunction(text=>document.querySelector('#camera-cards').textContent.includes(text),description);
 }
 for(const state of ['ready','unavailable']){
  fixture={...structuredClone(baseline),driver:'zwo_asi',alpaca:null,indi:{port:7624,state},discovery:{state:'preparing'}};
  await page.evaluate(()=>loadCameras());
  const card=page.locator('[data-camera-state="'+(state==='ready'?'Ready':'INDI unavailable')+'"]');
  await card.waitFor();
  assert.equal((await card.innerText()).includes('INDI at 127.0.0.1:7624'),state==='ready');
 }
 results.push('Normalized INDI endpoint and unavailability, detected, driver-missing, unsupported-calibration, unstable-attachment and heavy-image fixtures remain distinct');
 fixture={...structuredClone(baseline),owner:{client_id:7,address:'192.0.2.2',since:1,generation:'a'.repeat(32),dhcp_name:'Fictional laptop'},refused:{client_id:8,address:'192.0.2.3',since:2}};
 await page.evaluate(()=>loadCameras());await page.locator('[data-camera-state="Busy"]').waitFor();
 for(const theme of ['light','dark']){await page.evaluate(theme=>{localStorage.setItem("smart-telescope.theme",theme);window.dispatchEvent(new StorageEvent("storage",{key:"smart-telescope.theme",newValue:theme}));},theme);await page.screenshot({path:resolve(output,`camera-home-375-${theme}.png`),fullPage:true});}
 await page.locator('#nav-configuration').click();await page.locator('#tab-cameras').click();
 assert.equal(await page.locator('#camera-indi').isChecked(),false);
 await page.locator('#camera-indi').check();await page.locator('#camera-save').click();await page.waitForFunction(()=>!cameraPending);assert.equal((await request('GET','/cameras/settings')).settings.indi,true);
 await page.locator('#camera-indi').uncheck();await page.locator('#camera-save').click();await page.waitForFunction(()=>!cameraPending);assert.equal((await request('GET','/cameras/settings')).settings.indi,false);
 await page.locator('#camera-discover').uncheck();await page.evaluate(()=>refresh());assert.equal(await page.locator('#camera-discover').isChecked(),false);
 await request('PATCH','/cameras/settings',{allowed_subnets:['127.0.0.0/8']});await page.evaluate(()=>loadCameras());await page.locator('#camera-save').click();await page.waitForFunction(()=>document.querySelector('#camera-feedback').textContent.includes('Configuration changed.'));assert.equal(await page.locator('#camera-discover').isChecked(),false);assert.equal((await request('GET','/cameras/settings')).settings.discover,true);
 await page.evaluate(()=>BridgeI18n.setLocale('de'));
 assert.equal(await page.locator('#camera-feedback').textContent(),await page.evaluate(()=>t('Configuration changed. Reload it.')));
 await page.evaluate(()=>BridgeI18n.setLocale('en'));
 await page.locator('#camera-reload').click();await page.waitForFunction(()=>document.querySelector('#camera-discover').checked);await page.locator('#camera-discover').uncheck();await page.locator('#camera-save').click();await page.waitForFunction(()=>document.querySelector('#camera-feedback').textContent==='Camera settings saved.');assert.equal((await request('GET','/cameras/settings')).settings.discover,false);
 await page.locator('#camera-serve').uncheck();await page.locator('#camera-save').click();await page.waitForFunction(()=>!cameraPending);assert.equal((await request('GET','/cameras/settings')).settings.serve,false);
 await page.locator('#camera-serve').check();await page.locator('#camera-discover').check();await page.locator('#camera-save').click();await page.waitForFunction(()=>!cameraPending);
 const supportedList=page.locator('#panel-cameras p').filter({has:page.locator('strong[data-i18n="Supported cameras"]')});
 assert.match(await supportedList.innerText(),/ZWO ASI \(SDK availability depends on installation; hardware testing pending\)/);
 await page.locator('[data-help="camera-supported-info"]').click();await page.keyboard.press('Escape');assert.equal(await page.locator('[data-help="camera-supported-info"]').evaluate(e=>e===document.activeElement),true);
 for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){
  await page.evaluate(locale=>BridgeI18n.setLocale(locale),locale);
  assert.equal(await page.locator('#camera-feedback').textContent(),await page.evaluate(()=>t('Camera settings saved.')),locale+' saved feedback');
  for(const width of [320,375]){await page.setViewportSize({width,height:960});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),locale+' '+width);assert.ok(await page.locator('#panel-cameras th').evaluateAll(cells=>cells.every(cell=>cell.scrollWidth<=cell.clientWidth+1)),locale+' camera headers '+width);if(width===320&&['de','ru'].includes(locale))await page.screenshot({path:resolve(output,`camera-settings-${locale}-320.png`),fullPage:true});}
 }
 await page.evaluate(()=>BridgeI18n.setLocale('en'));await page.setViewportSize({width:1280,height:960});await page.evaluate(()=>{localStorage.setItem('smart-telescope.theme','light');window.dispatchEvent(new StorageEvent('storage',{key:'smart-telescope.theme',newValue:'light'}));});await page.screenshot({path:resolve(output,'camera-settings-1280.png'),fullPage:true});
 results.push('Real INDI, serve and discovery saves; stale revision refuses write and retains draft; explicit reload; polling preserves draft; supported-camera SDK requirement, hardware status and dialog focus; ten complete locales at320px');
 // ZWO is a line like the others: not installed, then downloaded on request after the owner accepts the manufacturer's terms.
 await page.setViewportSize({width:375,height:960});
 await page.locator('#camera-sdk').waitFor();
 const zwoLine=page.locator('[data-driver-row="zwo_asi"]');await zwoLine.waitFor();
 assert.match(await zwoLine.innerText(),/Not installed\. The bridge downloads it from the manufacturer\./);
 assert.equal(await page.locator('#camera-driver-zwo_asi').count(),1);
 for(const id of ['camera-sdk-review','camera-sdk-disable','camera-sdk-dialog','camera-sdk-accept','camera-sdk-license'])assert.equal(await page.locator('#'+id).count(),0,id+' is gone');
 await page.locator('#camera-driver-install-zwo_asi').click();await page.locator('#camera-driver-dialog').waitFor();
 assert.match(await page.locator('#camera-driver-dialog-text').innerText(),/downloads version 1\.42 from the manufacturer's site/);
 assert.equal(await page.locator('#camera-driver-dialog-unverified').isVisible(),true,'ZWO SDK 1.42 is not verified on hardware');
 await page.locator('#camera-driver-later').click();assert.equal(await page.locator('#camera-driver-dialog').isVisible(),false);
 fixture={...structuredClone(baseline),id:'usb-zwo',name:'ZWO ASI',driver:'zwo_asi',alpaca:null,owner:null,discovery:{state:'driver_missing'}};
 await page.locator('#nav-telescopes').click();await page.evaluate(()=>loadCameras());await page.locator('[data-camera-id="usb-zwo"]').waitFor();
 assert.match(await page.locator('[data-camera-id="usb-zwo"]').innerText(),/Install the ZWO ASI driver in Camera drivers below/);
 assert.equal(await page.locator('#camera-sdk-activate-usb-zwo').count(),0);
 results.push('ZWO line offers the same download as the other manufacturers, no licence dialog for a supplied library, camera card points to Camera drivers');
 // gPhoto profile contract fixture: an absent Bayer value must never imply mono.
 await page.locator('#nav-telescopes').click();
 let generation='a'.repeat(32),profile={attachment_generation:generation,detected:{name:'Detected DSLR',width:4000},values:{name:'Detected DSLR',width:4000},missing:['height','pixel_um_x','pixel_um_y','bits','exposure_min','exposure_max','bayer_pattern'],ready:false},saved=null,failSave=false;
 fixture={...structuredClone(baseline),id:'gphoto-test',name:'Detected DSLR',driver:'gphoto',alpaca:null,owner:null,discovery:{state:'unsupported'},profile:{ready:false,probing:true,missing:profile.missing,attachment_generation:generation}};
 await page.route('**/api/cameras/gphoto-test/profile',async route=>{if(route.request().method()==='GET')return route.fulfill({status:200,json:profile});saved=route.request().postDataJSON();if(failSave)return route.fulfill({status:409,json:{error:{message:'Camera profile changed.'}}});profile={...profile,values:{...profile.values,...saved.values},missing:[],ready:true};fixture.profile.ready=true;return route.fulfill({status:200,json:profile});});
 await page.evaluate(()=>loadCameras());await page.locator('[data-camera-state="Detecting camera"]').waitFor();assert.equal(await page.locator('#camera-profile-gphoto-test').isDisabled(),true);fixture.profile.probing=false;await page.evaluate(()=>loadCameras());await page.locator('[data-camera-state="Profile needed"]').waitFor();await page.locator('#camera-profile-gphoto-test').click();
 await page.locator("#camera-profile-field-name").waitFor();assert.equal(await page.locator('#camera-profile-fields [data-i18n="Detected value"]').count(),2);assert.equal(await page.locator('#camera-profile-field-name').inputValue(),'Detected DSLR');assert.equal(await page.locator('#camera-profile-field-width').inputValue(),'4000');assert.equal(await page.locator('#camera-profile-field-height').inputValue(),'');assert.equal(await page.locator('#camera-profile-field-bayer_pattern').inputValue(),'__missing__');
 await page.locator('#camera-profile-field-height').fill('3000');await page.locator('#camera-profile-field-pixel_um_x').fill('3.75');await page.locator('#camera-profile-field-pixel_um_y').fill('3.75');await page.locator('#camera-profile-field-bits').selectOption('16');await page.locator('#camera-profile-field-exposure_min').fill('0.001');await page.locator('#camera-profile-field-exposure_max').fill('30');await page.locator('#camera-profile-field-bayer_pattern').selectOption('');
 fixture.profile.probing=true;await page.evaluate(()=>loadCameras());assert.equal(await page.locator('#camera-profile-save').isDisabled(),true);assert.equal(await page.locator('#camera-profile-field-height').inputValue(),'3000');fixture.profile.probing=false;await page.evaluate(()=>loadCameras());assert.equal(await page.locator('#camera-profile-save').isEnabled(),true);
 for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){await page.evaluate(locale=>BridgeI18n.setLocale(locale),locale);assert.equal(await page.locator('#camera-profile-field-height').inputValue(),'3000');await page.setViewportSize({width:320,height:960});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),locale+' profile width');}
 await page.evaluate(()=>BridgeI18n.setLocale('en'));await page.screenshot({path:resolve(output,'gphoto-profile-320.png'),fullPage:true});
 failSave=true;await page.locator('#camera-profile-save').click();await page.waitForFunction(()=>document.querySelector('#camera-profile-feedback').textContent.includes('Camera profile changed.'));assert.equal(await page.locator('#camera-profile-field-height').inputValue(),'3000');assert.equal(saved.values.bayer_pattern,'');assert.equal(Object.hasOwn(saved.values,'width'),false);assert.equal(Object.hasOwn(saved.values,'name'),false);assert.equal(saved.attachment_generation,generation);
 failSave=false;await page.locator('#camera-profile-save').click();await page.locator('#camera-profile-dialog').waitFor({state:'hidden'});assert.equal(saved.values.height,3000);
 await page.evaluate(()=>loadCameras());await page.locator('#camera-profile-gphoto-test').click();saved=null;await page.locator('#camera-profile-save').click();assert.equal(saved,null);assert.equal(await page.locator('#camera-profile-feedback').textContent(),'No changes to save.');await page.locator('#camera-profile-field-height').fill('3999');generation='b'.repeat(32);fixture.profile.attachment_generation=generation;await page.evaluate(()=>loadCameras());assert.equal(await page.locator('#camera-profile-field-height').inputValue(),'3999');assert.equal(await page.locator('#camera-profile-save').isDisabled(),true);await page.locator('#camera-profile-cancel').click();
 results.push('gPhoto detected and missing values, explicit monochrome, draft across polling and ten locales, conflict, save and stale attachment');
 await page.unroute('**/api/cameras/gphoto-test/profile');
 await page.unroute('**/api/cameras');await page.evaluate(()=>loadCameras());
 await page.locator('#nav-configuration').click();await page.locator('#tab-cameras').click();
 await page.locator('#camera-demo-plugged').uncheck();await page.waitForFunction(()=>!cameraPending);assert.deepEqual(await request('GET','/cameras'),[]);
 // Late authenticated inventory must never repopulate private UI after logout.
 let unblock,heldResolve;const held=new Promise(r=>heldResolve=r),gate=new Promise(r=>unblock=r);await page.route('**/api/cameras',async route=>{heldResolve();await gate;await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify([fixture])});});
 await page.evaluate(()=>{void loadCameras();});await held;await page.evaluate(()=>document.querySelector('#user-menu').open=true);await page.locator('#logout').click();await page.locator('#login').waitFor({state:'visible'});unblock();await page.waitForTimeout(100);assert.equal(await page.locator('#camera-cards article').count(),0);assert.equal(await page.locator('#camera-table-body tr').count(),0);assert.equal(await page.locator('#camera-release-description').textContent(),'');
 await page.evaluate(()=>BridgeI18n.setLocale('de'));
 for(const id of ['camera-feedback','camera-release-feedback'])assert.equal(await page.locator('#'+id).textContent(),'');
 results.push('Unplug removes simulator; delayed old authenticated response is discarded after logout');
 assert.deepEqual(errors,[]);await writeFile(resolve(output,'results.json'),JSON.stringify({results,errors,simulation:true},null,2));for(const result of results)console.log('PASS',result);
}finally{if(browser)await browser.close();server.kill('SIGTERM');await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});}
