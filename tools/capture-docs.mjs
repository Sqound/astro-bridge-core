/** Capture dark production UI using disposable offline demo data only. */
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
const folder=await mkdtemp(resolve(tmpdir(),'astro-docs-')),output=resolve(root,'docs/assets');
await mkdir(output,{recursive:true});
async function freePort(){const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));return port;}
const port=await freePort(),cameraPort=await freePort();
// The shared fixture seeds only fictional local history and an unverified clock.
const server=spawn(process.env.PYTHON||'python3',['-u','-m','tests.helpers.session_demo','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;server.stdout.on('data',d=>logs+=d);server.stderr.on('data',d=>logs+=d);
const files=[],errors=[];
const wait=async predicate=>{for(let n=0;n<150;n++){if(await predicate())return;await new Promise(r=>setTimeout(r,100));}throw new Error('Documentation demo timed out');};
try{
 await wait(()=>logs.includes('SIMULATION'));
 const origin=`http://127.0.0.1:${port}`;
 const login=await fetch(origin+'/api/v1/auth/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:'12345678'})});assert.ok(login.ok);
 const token=(await login.json()).access_token;
 const request=async(method,path,body)=>{const revision=method==='GET'?null:(await request('GET','/status')).config_revision;const response=await fetch(origin+'/api/v1'+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json',...(revision?{'If-Match':'"'+revision+'"'}:{})},...(body?{body:JSON.stringify(body)}:{})});assert.ok(response.ok,`Demo API ${path}: ${response.status}`);return response.json();};
 await request('POST','/telescopes',{id:'vespera-demo',name:'Vespera · Demo',kind:'vespera',ssid:'Vespera-DEMO',target_ip:'10.0.0.1',open:true});
 await request('POST','/telescopes',{id:'seestar-demo',name:'Seestar · Saved',kind:'seestar',ssid:'Seestar-DEMO',target_ip:'192.168.4.1',open:true});
 const initial=await request('GET','/status'),mac=initial.devices.find(d=>d.mac!==initial.hotspot.mac).mac;
 await request('POST','/telescopes/vespera-demo/connect',{mac});
 await wait(async()=>!!(await request('GET','/telemetry')).card_data?.['vespera-demo']?.readings?.['stack.frames_accepted']);
 await request('PATCH','/cameras/settings',{port:cameraPort});
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:1280,height:1100},deviceScaleFactor:1,colorScheme:'dark'});page.setDefaultTimeout(15000);page.on('pageerror',error=>errors.push(error.message));
 await page.addInitScript(()=>localStorage.setItem('smart-telescope.theme','dark'));
 await page.goto(origin);await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 const shot=async name=>{await page.evaluate(()=>scrollTo({top:0,left:0,behavior:"instant"}));await page.evaluate(()=>document.fonts.ready);await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));assert.equal(await page.evaluate(()=>scrollY),0,name+' starts at the top');assert.equal(await page.locator('html').getAttribute('lang'),'en');assert.equal(await page.evaluate(()=>document.documentElement.dataset.theme),'dark');assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),name);await page.screenshot({path:resolve(output,name),animations:'disabled'});files.push(name);};
 await page.locator('#instrument-vespera-demo .session-values dd').first().waitFor();
 await shot('interface-dark.png');
 await page.locator('#nav-configuration').click();await page.locator('#tab-cameras').click();await page.waitForFunction(()=>cameraSettings!==null);
 await page.locator('#camera-demo-plugged').check();await wait(async()=>!!(await request('GET','/cameras'))[0]?.alpaca);await page.evaluate(()=>loadCameras());await shot('cameras-dark.png');
 const session=(await request('GET','/telemetry/sessions')).sessions.find(row=>row.ended);
 assert.ok(session);await page.setViewportSize({width:1280,height:1100});await page.goto(origin+'/#session/'+session.id);await page.locator('.session-chart').waitFor();await shot('session-dark.png');
 // Capture desktop views before resizing to avoid stale Chrome compositor layers.
 await request('POST','/cameras/demo',{plugged:false});await page.setViewportSize({width:375,height:960});await page.goto(origin);await page.locator('#dashboard').waitFor({state:'visible'});await page.locator('#instrument-vespera-demo .session-values dd').first().waitFor();await shot('interface-mobile.png');
 assert.deepEqual(errors,[]);console.log(JSON.stringify({simulation:true,synthetic_data_only:true,theme:'dark',files,page_errors:errors}));
}finally{if(browser)await browser.close();if(server.exitCode===null&&server.signalCode===null){const ended=once(server,'exit');server.kill('SIGTERM');await Promise.race([ended,new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');}await rm(folder,{recursive:true,force:true});}
