/** Real offline demo source plus explicit normalized fixtures for card states. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,mkdir,rm,writeFile,readFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {createServer} from 'node:net';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const folder=await mkdtemp(resolve(tmpdir(),'astro-session-page-')),output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/session-page');await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','tests.helpers.session_demo','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});let logs='',browser;
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
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:960}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(origin);await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 const sessions=(await request('GET','/telemetry/sessions')).sessions;assert.equal(sessions.length,2);
 const first=sessions[0].id,second=sessions[1].id;
 await page.locator('#session-history-vespera-demo').click();await page.locator('#session-history-list .session-open').first().click();
 await page.locator('.session-chart').waitFor();assert.equal(new URL(page.url()).hash,'#session/'+first);
 assert.ok((await page.locator('#session-page-content').innerText()).includes('Approximate times'));
 assert.equal(await page.locator('.session-reader-gap').count(),0);
 assert.ok((await page.locator('#session-page-content').innerText()).includes('6,000'));
 const analysis=await request('GET','/telemetry/sessions/'+first+'/analysis');assert.ok(analysis.sample_count>500);assert.ok(analysis.points.length<=480);assert.equal(analysis.exposure.complete,true);
 assert.equal(Math.min(...analysis.points.filter(p=>p.metric==='stack.frames_accepted').map(p=>p.low)),0);assert.equal(Math.max(...analysis.points.map(p=>p.high)),600);
 results.push('Real retained-session analysis covers >500 samples from first to last; full observed integration; history opens route');
 await page.goto(origin+'/#session/'+second);await page.locator('.session-reader-gap').first().waitFor();
 results.push('Exact reader intervals show internal unread hatch; fully fetched session has none; explicit injected unverified clock');
 await page.route('**/api/telemetry/sessions/*/analysis',async route=>{const response=await route.fetch(),data=await response.json();data.reader_coverage.unknown_before_seq=null;await route.fulfill({response,json:data});});
 await page.locator('#session-page-refresh').click();await page.waitForFunction(()=>!document.querySelector('#session-page-refresh').disabled);assert.equal(await page.locator('.session-reader-gap').count(),0);
 await page.unroute('**/api/telemetry/sessions/*/analysis');await page.locator('#session-page-refresh').click();await page.locator('.session-reader-gap').first().waitFor();

 const secondAnalysis=await request('GET','/telemetry/sessions/'+second+'/analysis');
 const affected=secondAnalysis.points.filter(row=>row.metric==='stack.frames_accepted').find(row=>row.to_seq>row.from_seq+1);
 await page.route('**/api/telemetry/sessions/'+second,async route=>{const response=await route.fetch(),data=await response.json();data.gaps=[{from_seq:affected.from_seq+1,to_seq:affected.from_seq+1,reason:'RETENTION_GAP'}];await route.fulfill({response,json:data});});
 await page.locator('#session-page-refresh').click();await page.waitForFunction(()=>!document.querySelector('#session-page-refresh').disabled);
 assert.ok((await page.locator('.session-chart path.session-kept').getAttribute('d')).split('M').length>=3);
 assert.ok((await page.locator('#session-page-content').innerText()).includes('Some journal records are missing.'));
 await page.unroute('**/api/telemetry/sessions/'+second);await page.locator('#session-page-refresh').click();await page.waitForFunction(()=>!document.querySelector('#session-page-refresh').disabled);
 results.push('Synthetic journal loss inside an aggregated bucket breaks chart continuity and shows its warning');
 for(const format of ['csv','json']){
  const [download]=await Promise.all([page.waitForEvent('download'),page.locator('#session-download-'+format).click()]);
  const response=await page.request.get(origin+'/api/telemetry/sessions/'+second+'/export?format='+format);
  assert.equal(download.suggestedFilename(),'vespera-2026-11-04.'+format);
  assert.equal(response.headers()['content-type'],format==='csv'?'text/csv; charset=utf-8':'application/json');
  const content=await readFile(await download.path(),'utf8');
  if(format==='csv'){assert.ok(content.startsWith('seq,utc,utc_corrected,metric,value\r\n'));assert.ok(content.includes('stack.frames_accepted,600'));}
  else {const data=JSON.parse(content);assert.equal(data.session.id,second);assert.ok(data.samples.length>500);assert.ok(data.events.length>=2);}
 }
 results.push('Actual cookie-authenticated CSV/JSON attachments have exact filenames, content types and complete retained content');
 for(const width of [375,1280])for(const theme of ['light','dark']){
  await page.setViewportSize({width,height:1100});await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,width+' '+theme);
  await page.screenshot({path:resolve(output,`session-${width}-${theme}.png`),fullPage:true});
 }
 await page.setViewportSize({width:375,height:960});
 await page.locator('#session-delete-one').click();assert.equal(await page.locator('#session-delete-cancel').evaluate(e=>e===document.activeElement),true);
 await page.keyboard.press('Escape');await page.waitForTimeout(50);assert.equal(await page.locator('#session-delete-one').evaluate(e=>e===document.activeElement),true);
 assert.equal((await request('GET','/telemetry/sessions')).sessions.length,2);
 await page.locator('#session-delete-one').click();await page.locator('#session-delete-confirm').click();await page.waitForFunction(()=>document.querySelector('#session-page-feedback').textContent==='Session deleted.');
 assert.equal((await request('GET','/telemetry/sessions')).sessions.length,1);
 results.push('Single-delete dialog defaults to Cancel, Escape restores focus, confirmation removes real stored data');
 await request('POST','/auth/clients',{name:'Synthetic session reader',scopes:['telemetry']});
 await request('POST','/auth/clients',{name:'Synthetic status reader',scopes:['read']});
 await page.evaluate(()=>refresh());
 await page.locator('#nav-configuration').click();await page.locator('#tab-sessions').click();await page.waitForFunction(()=>document.querySelector('#session-retention-nights').value==='30');
 assert.ok((await page.locator('#session-reader-apps').innerText()).includes('Synthetic session reader'));assert.ok(!(await page.locator('#session-reader-apps').innerText()).includes('Synthetic status reader'));
 await page.locator('#session-retention-nights').fill('17');await page.locator('#session-retention-size').fill('32');
 await page.evaluate(()=>refresh());assert.equal(await page.locator('#session-retention-nights').inputValue(),'17');
 for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){
  await page.evaluate(tag=>BridgeI18n.setLocale(tag),locale);assert.equal(await page.locator('#session-retention-nights').inputValue(),'17');
  assert.equal(await page.locator('#session-settings-list .session-open').first().innerText(),await page.evaluate(()=>t('Open session')));
  assert.ok(await page.locator('#session-settings-list [data-session-started]').evaluateAll(elements=>elements.every(element=>element.textContent.endsWith(new Date(element.dataset.sessionStarted).toLocaleString(BridgeI18n.locale)))));
  for(const width of [320,375]){await page.setViewportSize({width,height:960});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,locale+' settings '+width);if(width===320&&['de','ru'].includes(locale))await page.screenshot({path:resolve(output,`settings-${locale}-320.png`),fullPage:true});}
 }
 await page.evaluate(()=>BridgeI18n.setLocale('en'));await page.locator('#session-retention-save').click();await page.waitForFunction(()=>document.querySelector('#session-retention-feedback').textContent==='Session retention saved.');
 assert.deepEqual((await request('GET','/telemetry')).retention,{nights:17,max_bytes:32*1024**2});
 await page.screenshot({path:resolve(output,'settings-375.png'),fullPage:true});
 // Revision conflict must keep the user's draft; Reload settings is explicit.
 await page.locator('#session-retention-nights').fill('21');await request('PATCH','/telemetry/settings',{retention_nights:18});
 await page.locator('#session-retention-save').click();await page.waitForFunction(()=>document.querySelector('#session-retention-feedback').textContent.includes('draft is kept'));
 assert.equal(await page.locator('#session-retention-nights').inputValue(),'21');assert.equal((await request('GET','/telemetry')).retention.nights,18);
 await page.locator('#session-retention-reload').click();await page.waitForFunction(()=>document.querySelector('#session-retention-nights').value==='18');
 results.push('Real retention save; conflict preserves draft; reload explicit; polling and all ten locales preserve draft at375px');
 await page.locator('#session-settings-list .session-open').first().click();await page.locator('.session-chart').waitFor();
 for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){await page.evaluate(tag=>BridgeI18n.setLocale(tag),locale);for(const width of [320,375]){await page.setViewportSize({width,height:960});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,locale+' session '+width);if(width===320&&['de','ru'].includes(locale))await page.screenshot({path:resolve(output,`session-${locale}-320.png`),fullPage:true});}}
 await page.evaluate(()=>BridgeI18n.setLocale('en'));await page.locator('#nav-configuration').click();
 await page.locator('#session-delete-all').click();await page.locator('#session-delete-cancel').click();assert.equal((await request('GET','/telemetry/sessions')).sessions.length,1);
 await page.locator('#session-delete-all').click();await page.locator('#session-delete-confirm').click();await page.waitForFunction(()=>document.querySelector('#session-settings-list').textContent.includes('No sessions yet.'));
 assert.equal((await request('GET','/telemetry')).sessions_count,0);
 await page.goto(origin+'/#session/'+first);await page.waitForFunction(()=>document.querySelector('#session-page-feedback').textContent.includes('unavailable'));
 assert.equal(await page.locator('#session-download-csv').count(),0);
 results.push('Ten session locales fit375px; delete-all cancel/confirm; empty and removed-session states contain no stale export action');
 // A delayed read from the previous route must not repopulate private leaves.
 let release;const barrier=new Promise(r=>release=r);let started;
 const observed=new Promise(r=>started=r);
 await page.route('**/api/telemetry/sessions/*/analysis',async route=>{started();await barrier;await route.fulfill({json:analysis});});
 await page.reload();await observed;await page.locator('#nav-telescopes').click();release();await page.waitForTimeout(100);
 assert.equal(await page.locator('#session-page-content').innerText(),'');await page.unroute('**/api/telemetry/sessions/*/analysis');
 await request('POST','/telescopes/vespera-demo/connect',{mac});
 await wait(async()=>!!(await request('GET','/telemetry')).card_data?.['vespera-demo']?.readings?.['stack.frames_accepted']);
 await page.evaluate(()=>refresh());await page.locator('#instrument-vespera-demo .session-open').waitFor();
 const opened=await page.locator('#instrument-vespera-demo .session-open').getAttribute('href');
 assert.ok((await request('GET','/telemetry/sessions')).sessions.some(row=>'#session/'+row.id===opened));
 assert.equal(await page.locator('#instrument-vespera-demo .primary').count(),1);assert.equal(await page.locator('#instrument-vespera-demo .session-open.primary').count(),1);assert.equal(await page.locator('#instrument-vespera-demo .connection-action.secondary').count(),1);
 await page.locator('#instrument-vespera-demo .session-open').click();await page.locator('.session-chart').waitFor();
 await page.locator('#session-download-csv').focus();await page.evaluate(()=>BridgeI18n.setLocale('fr'));assert.equal(await page.locator('#session-download-csv').evaluate(e=>e===document.activeElement),true);
 await page.locator('#user-menu>summary').click();await page.locator('#logout').click();await page.locator('#login').waitFor({state:'visible'});
 assert.equal(await page.locator('#session-page-content').innerText(),'');
 results.push('Real connected-card Open session preserves one primary action; locale keeps export focus; logout clears session leaves; paired-app list filters scopes');
 assert.deepEqual(errors,[]);await writeFile(resolve(output,'results.json'),JSON.stringify({results,errors,simulation:true},null,2));for(const result of results)console.log('PASS',result);
}finally{if(browser)await browser.close();server.kill('SIGTERM');await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});}
