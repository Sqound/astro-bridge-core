/** Service failure copy on existing connection cards; synthetic state, no mount traffic. */
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
const folder=await mkdtemp(resolve(tmpdir(),'astro-readiness-'));
const output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/p0-02-readiness');
await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');
const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;const errors=[],results=[];
server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
try{
 for(let n=0;!logs.includes('SIMULATION');n++){if(n===100)throw new Error('Server start: '+logs);await new Promise(r=>setTimeout(r,100));}
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:960}});page.setDefaultTimeout(10000);
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${port}`);
 await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();
 await page.locator('#dashboard').waitFor({state:'visible'});
 const render=async(service,relayReady=true)=>page.evaluate(({service,relayReady})=>{
  const fake=structuredClone(state),device=fake.devices.find(d=>d.mac!==fake.hotspot.mac);
  fake.config.telescopes=[{id:'synthetic-mount',name:'Synthetic mount',kind:'zwo_mount',enabled:true,mac:device.mac,target_ip:'192.168.4.1',ssid:'Synthetic mount'}];
  fake.telescopes=[{id:'synthetic-mount',connected:true,relay_ready:relayReady,interface:device.name,state:'online',service:{state:service,method:'tcp_connect',checked_at_seq:1,failures:service==='not_answering'?1:0}}];
  renderSimple(fake);
 },{service,relayReady});
 for(const relayReady of [true,false]){
  await render('not_answering',relayReady);
  assert.ok(await page.locator('.instrument-card .instrument-service').allTextContents().then(rows=>rows.includes('Wi-Fi connected · not answering')));
 }
 results.push('connected card shows not answering independently of forwarding readiness');
 await page.screenshot({path:resolve(output,'service-not-answering-375.png'),fullPage:true});
 for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){
  await page.evaluate(tag=>BridgeI18n.setLocale(tag),locale);await render('not_answering');
  const label=await page.locator('.instrument-card .instrument-service').first().textContent();
  assert.ok(label.length>0);if(locale!=='en')assert.notEqual(label,'Wi-Fi connected · not answering');
  await page.setViewportSize({width:320,height:960});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,locale);
 }
 results.push('ten translated service labels fit at 320px');
 await page.evaluate(()=>BridgeI18n.setLocale('en'));await render('ready');
 assert.equal(await page.locator('.instrument-card .instrument-service').first().textContent(),'Instrument service answers.');
 await render('not_probed');
 assert.equal(await page.locator('.instrument-card .instrument-service').first().textContent(),'Instrument service is not checked for this family.');
 results.push('ready and unprobed states preserve association and explain actual probe coverage');
 assert.deepEqual(errors,[]);
 await writeFile(resolve(output,'results.json'),JSON.stringify({results,errors,simulation:true},null,2));
 for(const name of results)console.log('PASS',name);
}finally{
 if(browser)await browser.close();server.kill('SIGTERM');
 await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);
 if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});
}
