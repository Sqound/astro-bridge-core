/** Real offline demo source plus explicit normalized fixtures for card states. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,mkdir,rm,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {createServer} from 'node:net';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const folder=await mkdtemp(resolve(tmpdir(),'astro-session-cards-')),output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/session-cards');await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});let logs='',browser;
server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
const wait=async fn=>{for(let i=0;i<150;i++){if(await fn())return;await new Promise(r=>setTimeout(r,100));}throw new Error('Demo wait timed out');};
const results=[],errors=[];
try{
 await wait(()=>logs.includes('SIMULATION'));
 const origin=`http://127.0.0.1:${port}`;
 const login=await fetch(origin+'/api/v1/auth/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:'12345678'})});const token=(await login.json()).access_token;
 const request=async(method,path,body)=>{const rev=method==='GET'?null:(await(await fetch(origin+'/api/v1/status',{headers:{Authorization:'Bearer '+token}})).json()).config_revision;const response=await fetch(origin+'/api/v1'+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json',...(rev?{'If-Match':'"'+rev+'"'}:{})},...(body?{body:JSON.stringify(body)}:{})});const value=await response.json();assert.ok(response.ok,'Owner API '+path+' '+response.status);return value;};
 await request('POST','/telescopes',{id:'vespera-demo',name:'Vespera II',kind:'vespera',ssid:'Vespera-DEMO',target_ip:'10.0.0.1',open:true});
 const initial=await request('GET','/status'),mac=initial.devices.find(d=>d.mac!==initial.hotspot.mac).mac;
 await request('POST','/telescopes/vespera-demo/connect',{mac});
 await wait(async()=>!!(await request('GET','/telemetry')).card_data?.['vespera-demo']?.readings?.['stack.frames_accepted']);
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:960}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(origin);await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 await page.locator('#instrument-vespera-demo .session-values dd').first().waitFor();
 assert.ok((await page.locator('#instrument-vespera-demo .session-block').innerText()).includes('Stacking · M 27'));
 assert.equal(await page.locator('#instrument-vespera-demo .session-open.primary').count(),1);assert.equal(await page.locator('#instrument-vespera-demo .connection-action.secondary').count(),1);assert.equal(await page.locator('#instrument-vespera-demo .primary').count(),1);
 assert.ok(await page.locator('#instrument-vespera-demo .session-values>div').count()<=8);
 assert.ok((await page.locator('#instrument-vespera-demo .session-block').innerText()).includes('frames'));
 for(const width of [320,375,1280])for(const theme of ['light','dark']){
  await page.setViewportSize({width,height:1400});await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,width+' '+theme);
  await page.locator('#instrument-vespera-demo').screenshot({path:resolve(output,`card-${width}-${theme}.png`),animations:'disabled'});
 }
 await page.setViewportSize({width:375,height:960});await page.evaluate(()=>document.documentElement.dataset.theme='light');
 results.push('Actual offline demo produces stacking, rejections, units and Tonight without hardware; card captures at320/375/1280 in both themes');
 await page.locator('#session-info-vespera-demo').click();await page.keyboard.press('Escape');assert.equal(await page.locator('#session-info-vespera-demo').evaluate(e=>e===document.activeElement),true);
 await page.locator('#session-history-vespera-demo').click();await page.locator('#session-history-list strong').first().waitFor();
 for(const locale of ['de','ru','en']){await page.evaluate(tag=>BridgeI18n.setLocale(tag),locale);assert.equal(await page.locator('#session-history-list .session-open').first().innerText(),await page.evaluate(()=>t('Open session')));assert.ok((await page.locator('#session-history-title').innerText()).includes('Vespera II'));assert.ok(await page.locator('#session-history-list [data-session-started]').evaluateAll(elements=>elements.every(element=>element.textContent===new Date(element.dataset.sessionStarted).toLocaleString(BridgeI18n.locale))));}
 await page.keyboard.press('Escape');assert.equal(await page.locator('#session-history-vespera-demo').evaluate(e=>e===document.activeElement),true);
 results.push('Real sessions API powers history; information/history dialogs restore keyboard focus');
 const snapshot=await request('GET','/telemetry');
 let mode='stale';
 await page.route('**/api/telemetry',async route=>{const response=await route.fetch(),data=await response.json();if(mode==='stale'||mode==='paused'){
  for(const reading of Object.values(data.card_data['vespera-demo'].readings)){reading.age_seconds=120;reading.stale=true;}
  data.card_data['vespera-demo'].history_partial=true;data.card_data['vespera-demo'].tonight=[];
  if(mode==='paused')data.adapter_status['vespera-demo']=[{adapter_id:'vespera_status_v1',adapter_state:'yielded',retry_after:600,unavailable_reason:null}];
 }await route.fulfill({response,json:data});});
 await page.evaluate(()=>refresh());assert.ok(await page.locator('#instrument-vespera-demo .session-values .stale').count()>0);assert.ok((await page.locator('#instrument-vespera-demo .session-block').innerText()).includes('Last reading'));assert.ok((await page.locator('#instrument-vespera-demo .session-block').innerText()).includes('earlier targets or readings may be omitted.'));
 mode='paused';await page.evaluate(()=>refresh());assert.ok((await page.locator('#instrument-vespera-demo .session-headline').innerText()).includes('Paused'));
 await page.screenshot({path:resolve(output,'paused-375.png'),fullPage:true});await page.unroute('**/api/telemetry');
 results.push('Stale readings keep their values and last-reading time; yielded adapter says Paused');
 await request('POST','/telescopes',{id:'seestar-demo',name:'Seestar S50',kind:'seestar',ssid:'Seestar-DEMO',target_ip:'192.168.4.1',open:true});
 await request('POST','/telescopes',{id:'dwarf-demo',name:'DWARF mini',kind:'dwarf',ssid:'DWARF-DEMO',target_ip:'192.168.5.1',open:true});
 let keyState='absent';
 await page.route('**/api/maker-keys',async route=>{const response=await route.fetch(),data=await response.json();data.keys[0].state=keyState;await route.fulfill({response,json:data});});
 await page.route('**/api/status',async route=>{const response=await route.fetch(),data=await response.json();for(const p of data.config.telescopes)if(p.id!=='vespera-demo'){p.enabled=true;const row=data.telescopes.find(r=>r.id===p.id);if(row)row.connected=true;}await route.fulfill({response,json:data});});
 await page.route('**/api/telemetry',async route=>{const response=await route.fetch(),data=await response.json();if(keyState==='verified')data.card_data['seestar-demo']=structuredClone(snapshot.card_data['vespera-demo']);await route.fulfill({response,json:data});});
 await page.evaluate(()=>{makerChipRefresh=0;return refresh();});await wait(async()=>await page.locator('#session-key-seestar-demo').count());
 assert.ok((await page.locator('#instrument-seestar-demo').innerText()).includes('Position only. Stacking data needs the Seestar key.'));
 assert.ok((await page.locator('#instrument-dwarf-demo').innerText()).includes('Link only. A DWARF accepts one app at a time.'));
 await page.locator('#session-key-seestar-demo').click();await page.locator('#maker-key-section').waitFor({state:'visible'});await page.locator('#close-editor').click();
 for(const [state,text] of [['unverified','Key not yet confirmed'],['refused','The Seestar refused this key'],['verified','Stacking · M 27']]){keyState=state;await page.evaluate(()=>{makerChipRefresh=0;return refresh();});await page.waitForTimeout(100);await page.evaluate(()=>refresh());assert.ok((await page.locator('#instrument-seestar-demo').innerText()).includes(text));}
 results.push('Synthetic Seestar absent/pending/refused/verified key states and DWARF link-only limitation; key action opens profile');
 for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){await page.evaluate(tag=>BridgeI18n.setLocale(tag),locale);for(const width of [320,375]){await page.setViewportSize({width,height:960});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,locale+' '+width);if(width===320&&['de','ru'].includes(locale))await page.locator('#instrument-vespera-demo').screenshot({path:resolve(output,`cards-${locale}-320.png`),animations:'disabled'});}}
 await page.evaluate(()=>BridgeI18n.setLocale('en'));await page.screenshot({path:resolve(output,'live-320.png'),fullPage:true});
 await page.setViewportSize({width:1280,height:960});await page.locator('#instrument-vespera-demo').screenshot({path:resolve(output,'live-1280.png'),animations:'disabled'});
 await page.unroute('**/api/status');await page.unroute('**/api/telemetry');await page.unroute('**/api/maker-keys');
 await page.locator('#nav-configuration').click();await page.locator('#tab-telescopes').click();await page.locator('#profile-vespera-demo-edit').click();await page.locator('#session-adapter-vespera_status_v1').waitFor();
 await page.locator('#session-adapter-vespera_status_v1').uncheck();await page.waitForFunction(()=>document.querySelector('#session-settings-feedback').textContent==='Session data settings saved.');assert.ok(!(await request('GET','/telemetry')).profiles['vespera-demo'].adapters.includes('vespera_status_v1'));
 await page.locator('#close-editor').click();
 await request('PATCH','/telemetry/settings',{profiles:{'vespera-demo':{adapters:[],alpaca_interval_seconds:37}}});
 await page.locator('#profile-vespera-demo-edit').click();await page.locator('#link-session-enable').waitFor({state:'visible'});
 assert.ok((await page.locator('#link-session-state').innerText()).includes('off in your saved settings'));
 assert.deepEqual((await request('GET','/telemetry')).profiles['vespera-demo'],{adapters:[],alpaca_interval_seconds:37});
 await page.locator('#link-session-enable').click();await page.waitForFunction(()=>document.querySelector('#session-settings-feedback').textContent==='Session data settings saved.');
 assert.deepEqual((await request('GET','/telemetry')).profiles['vespera-demo'],{adapters:['link_v1'],alpaca_interval_seconds:37});
 results.push('Ten locales at320px; generic status switch persists opt-out; legacy link-off stays off until explicit enable, preserving cadence');
 await page.locator('#close-editor').click();await request('POST','/telescopes/vespera-demo/disconnect',{});await page.locator('#nav-telescopes').click();await page.evaluate(()=>refresh());await page.locator('.recent-nights').first().waitFor();assert.ok((await page.locator('.recent-nights').first().innerText()).includes('Latest session per night'));
 assert.deepEqual(errors,[]);await writeFile(resolve(output,'results.json'),JSON.stringify({results,errors,simulation:true},null,2));for(const result of results)console.log('PASS',result);
}finally{if(browser)await browser.close();server.kill('SIGTERM');await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});}
