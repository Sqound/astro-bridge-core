/** Synthetic AP event pages and failure copy on a disposable production demo. */
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
const folder=await mkdtemp(resolve(tmpdir(),'astro-hotspot-recovery-'));
const output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/p0-05-hotspot-recovery');
await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');
const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;const errors=[],results=[];let events=[];
server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
try{
 for(let n=0;!logs.includes('SIMULATION');n++){if(n===100)throw new Error('Server start: '+logs);await new Promise(r=>setTimeout(r,100));}
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:960}});page.setDefaultTimeout(10000);
 page.on('pageerror',e=>errors.push(e.message));
 const boot='1'.repeat(32);
 await page.route('**/api/events*',async route=>{
  const url=new URL(route.request().url()),after=Number(url.searchParams.get('after')||0);
  await route.fulfill({json:{boot_id:boot,oldest_sequence:1,latest_sequence:events.length,next_cursor:events.length,
   has_more:false,gap:false,reset_reason:null,persistent:true,history_available:true,clock_status:'unverified',events:events.filter(e=>e.sequence>after)}});
 });
 await page.goto(`http://127.0.0.1:${port}`);
 await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();
 await page.locator('#dashboard').waitFor({state:'visible'});
 await page.waitForTimeout(300);
 const event=(sequence,type,reason,ready)=>({sequence,type,reason,ready,associated:ready,boot_id:boot,profile_id:'hotspot',profile_name:'',radio_id:'radio-synthetic',
  operation_id:String(sequence).repeat(32),timestamp:'2026-11-04T00:00:00Z',clock_status:'unverified',cause:'RECONCILE',outcome:ready?'ready':'failed'});
 events=[event(1,'hotspot_unavailable','HOTSPOT_START_FAILED',false)];
 await page.locator('#connection-notifications').getByText('Hotspot unavailable.',{exact:true}).waitFor();
 events.push(event(2,'hotspot_restored','HOTSPOT_RESTORED',true));
 await page.locator('#connection-notifications').getByText('Hotspot access restored.',{exact:true}).waitFor();
 results.push('confirmed unavailable/restored events render foreground notifications');
 await page.evaluate(()=>{BridgeEvents.setPage(true);navigate('activity');});
 await page.locator('#activity-list li').first().waitFor();
 for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){
  await page.evaluate(tag=>BridgeI18n.setLocale(tag),locale);
  const text=await page.locator('#activity-list').textContent();
  assert.ok(text.trim().length>0);
  if(locale!=='en'){assert.ok(!text.includes('Hotspot unavailable.'),locale);assert.ok(!text.includes('Hotspot access restored.'),locale);}
  await page.setViewportSize({width:320,height:960});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,locale);
 }
 results.push('ten translated recovery event labels fit at 320px');
 await page.evaluate(()=>{
  BridgeI18n.setLocale('en');navigate('networks');
  const fake=structuredClone(state);fake.hotspot={active:false,reason:'start_failed'};renderSimpleNetwork(fake);
 });
 assert.equal(await page.locator('#simple-ap-badge').textContent(),'Waiting');
 assert.equal(await page.locator('#simple-ap-detail').textContent(),'Could not start the hotspot. Check its driver and service logs.');
 results.push('failed AP start stays Waiting with actionable localized copy');
 for(const button of await page.locator('#connection-notifications button').all())await button.click();
 await page.screenshot({path:resolve(output,'hotspot-retry-320.png'),fullPage:true});
 assert.deepEqual(errors,[]);
 await writeFile(resolve(output,'results.json'),JSON.stringify({results,errors,simulation:true},null,2));
 for(const name of results)console.log('PASS',name);
}finally{
 if(browser)await browser.close();server.kill('SIGTERM');
 await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);
 if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});
}
