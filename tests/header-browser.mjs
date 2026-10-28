/** Header activity, unread cursor and compact preferences against the real UI. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,mkdir,rm,writeFile} from 'node:fs/promises';
import {createServer} from 'node:net';
import {get} from 'node:http';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const temporary=await mkdtemp(resolve(tmpdir(),'astro-header-'));
const output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/header-ui');await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn(process.env.PYTHON||'python3',['-m','wifi_bridge','--demo','--state-dir',temporary,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let log='',browser;server.stdout.on('data',d=>log+=d);server.stderr.on('data',d=>log+=d);
const errors=[],results=[];
try {
 for(let i=0;i<100;i++){try{const ready=await new Promise((resolve,reject)=>{const request=get('http://127.0.0.1:'+port,response=>{response.resume();response.on('end',()=>resolve(response.statusCode===200));});request.on('error',reject);});if(ready)break;}catch{}if(i===99)throw Error(log);await new Promise(r=>setTimeout(r,100));}
 browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
 const context=await browser.newContext({viewport:{width:1280,height:900},colorScheme:'dark'}),page=await context.newPage();page.setDefaultTimeout(8000);page.on('pageerror',e=>errors.push(e.message));
 const boot='a'.repeat(32);let events=[],sequence=0;
 function append(count){for(let i=0;i<count;i++){sequence++;events.push({boot_id:boot,sequence,timestamp:'2026-01-01T00:00:00Z',clock_status:'unverified',operation_id:'op-'+sequence,profile_id:'demo',profile_name:'Scope '+sequence,type:'connecting',cause:'MANUAL',reason:'CONNECTING',outcome:'in_progress',associated:false,ready:false});}events=events.slice(-256);}
 append(64);
 await page.route('**/api/events?*',async route=>{
  const p=new URL(route.request().url()).searchParams,after=p.has('after')?Number(p.get('after')):null,limit=Number(p.get('limit')||50);
  const available=after===null?events:events.filter(e=>e.sequence>after),selected=after===null?available.slice(-limit):available.slice(0,limit);
  await route.fulfill({json:{boot_id:boot,oldest_sequence:events[0]?.sequence||sequence+1,latest_sequence:sequence,next_cursor:after===null?sequence:selected.at(-1)?.sequence??after,has_more:after!==null&&available.length>selected.length,gap:false,reset_reason:null,persistent:true,history_available:true,events:selected}});
 });
 const check=async(name,fn)=>{await fn();results.push(name);console.log('PASS',name);};
 await page.goto('http://127.0.0.1:'+port);await page.locator('#login-password').fill('demo-telescope-2026');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});await page.waitForFunction(()=>document.querySelectorAll('#activity-list li').length===50);
 await check('historical snapshot is read; new events alone increase the bell badge',async()=>{
  assert.equal(await page.locator('#activity-count').isVisible(),false);append(12);await page.evaluate(()=>BridgeEvents.refresh());await page.waitForFunction(()=>document.querySelector('#activity-count').textContent==='12');
  await page.evaluate(()=>BridgeEvents.refresh());assert.equal(await page.locator('#activity-count').textContent(),'12');
 });
 await check('popover shows the ten newest events, marks them read and closes with Escape',async()=>{
  await page.locator('#activity-toggle').click();assert.equal(await page.locator('#activity-preview-list li').count(),10);assert.match(await page.locator('#activity-preview-list li').first().innerText(),/Scope 76/);assert.match(await page.locator('#activity-preview-list li').last().innerText(),/Scope 67/);assert.equal(await page.locator('#activity-count').isVisible(),false);
  await page.evaluate(()=>BridgeEvents.refresh());assert.equal(await page.locator('#activity-popover').isVisible(),true);await page.keyboard.press('Escape');assert.equal(await page.locator('#activity-popover').isVisible(),false);assert.equal(await page.locator('#activity-toggle').evaluate(e=>e===document.activeElement),true);
 });
 await check('unread marker survives reload without storing event names',async()=>{
  append(1);await page.evaluate(()=>BridgeEvents.refresh());await page.waitForFunction(()=>document.querySelector('#activity-count').textContent==='1');await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});await page.waitForFunction(()=>!document.querySelector('#activity-count').hidden);
  assert.equal(await page.locator('#activity-count').textContent(),'1');const saved=await page.evaluate(()=>sessionStorage.getItem('astro-bridge.event-cursor'));assert.doesNotMatch(saved,/Scope|profile_name|connecting/);assert.equal(JSON.parse(saved).read_sequence,76);
 });
 await check('See more opens a deep-linked history page with older journal entries',async()=>{
  await page.locator('#activity-toggle').click();await page.locator('#activity-more').click();await page.waitForURL('**/#activity');await page.waitForFunction(()=>document.querySelectorAll('#activity-list li').length===77);assert.equal(await page.locator('#view-activity').isVisible(),true);assert.equal(await page.locator('#activity-popover').isVisible(),false);assert.match(await page.locator('#activity-list li').last().innerText(),/Scope 1:/);
  assert.ok(await page.locator('#activity-title').evaluate(e=>parseFloat(getComputedStyle(e).fontSize)<=20));await page.locator('#activity-back').click();await page.waitForURL('**/#telescopes');
 });
 await check('preferences are grouped in one menu; titles and decorative eyebrows are absent',async()=>{
  assert.equal(await page.locator('#theme-toggle').isVisible(),false);await page.locator('#user-menu>summary').click();assert.equal(await page.locator('#theme-toggle').isVisible(),true);assert.equal(await page.locator('#logout').isVisible(),true);
  await page.locator('#locale-menu>summary').click();await page.locator('button[data-locale="fr"]').click();assert.equal(await page.locator('#activity-toggle').getAttribute('aria-label'),'Activité');await page.locator('#locale-menu>summary').click();await page.locator('button[data-locale="en"]').click();
  await page.keyboard.press('Escape');await page.locator('#nav-configuration').click();assert.equal(await page.locator('#configuration-title').evaluate(e=>getComputedStyle(e).position),'absolute');assert.doesNotMatch(await page.locator('#view-configuration').innerText(),/YOUR DEVICE/);assert.equal(await page.locator('#activity-panel').count(),0);
 });
 await check('mobile popover and preferences fit a 320px screen',async()=>{
  await page.setViewportSize({width:320,height:780});await page.locator('#activity-toggle').click();let box=await page.locator('#activity-popover').boundingBox();assert.ok(box.x>=0&&box.x+box.width<=321,JSON.stringify(box));await page.locator('#user-menu>summary').click();assert.equal(await page.locator('#activity-popover').isVisible(),false);box=await page.locator('.user-menu-panel').boundingBox();assert.ok(box.x>=0&&box.x+box.width<=321,JSON.stringify(box));assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.keyboard.press('Escape');
  await page.locator('#activity-toggle').click();await page.screenshot({path:resolve(output,'activity-mobile-dark.png'),fullPage:false});await page.keyboard.press('Escape');
 });
 await check('logout clears activity content, badge and its opaque read cursor',async()=>{
  await page.locator('#user-menu>summary').click();await page.locator('#logout').click();await page.locator('#login').waitFor({state:'visible'});assert.equal(await page.locator('#activity-toggle').isVisible(),false);assert.equal(await page.locator('#activity-list li').count(),0);assert.equal(await page.locator('#activity-preview-list li').count(),0);assert.equal(await page.evaluate(()=>sessionStorage.getItem('astro-bridge.event-cursor')),null);assert.doesNotMatch(await page.locator('body').innerText(),/Scope 7/);
 });
 assert.deepEqual(errors,[]);await writeFile(resolve(output,'results.json'),JSON.stringify({results,errors},null,2)+'\n');
} finally {if(browser)await browser.close();server.kill('SIGTERM');await once(server,'exit');await rm(temporary,{recursive:true,force:true});}
