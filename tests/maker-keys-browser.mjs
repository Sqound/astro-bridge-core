/** Disposable loopback UI: runtime key import, scope placement, delete and privacy. */
import assert from 'node:assert/strict';
import {spawn,execFileSync} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,mkdir,rm,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {createServer} from 'node:net';
import {fileURLToPath,pathToFileURL} from 'node:url';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const folder=await mkdtemp(resolve(tmpdir(),'astro-maker-key-ui-'));
const output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/p1-07a-browser');await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;const errors=[],results=[];server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
const key=execFileSync('python3',['-c','import sys;sys.path.insert(0,"tests");from helpers.maker_keys import pem;sys.stdout.buffer.write(pem())'],{cwd:root,stdio:['ignore','pipe','pipe']});
try{
 for(let n=0;!logs.includes('SIMULATION');n++){if(n===100)throw new Error('Demo startup timed out');await new Promise(r=>setTimeout(r,100));}
 const origin=`http://127.0.0.1:${port}`;
 const login=await fetch(origin+'/api/v1/auth/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:'12345678'})});const token=(await login.json()).access_token;
 const request=async(method,path,body)=>{const r=await fetch(origin+'/api/v1'+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});assert.ok(r.ok,'Owner API response');return r.json();};
 await request('POST','/telescopes',{id:'seestar-demo',name:'Test Seestar',kind:'seestar',ssid:'Seestar-DEMO',target_ip:'10.0.0.1',open:true});
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:960}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(origin);await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 await page.locator('#nav-configuration').click();
 assert.equal(await page.locator('#maker-key-section').isVisible(),false);
 await page.locator('#tab-telescopes').click();
 await page.locator('#profile-seestar-demo-edit').click();
 await page.waitForFunction(()=>document.querySelector('#maker-key-state').textContent==='No key · stacking data unavailable');
 await page.locator('[data-help="maker-key-info"]').click();
 assert.equal(await page.locator('#maker-key-info').isVisible(),true);
 assert.ok((await page.locator('#maker-key-info').textContent()).includes('someone with the whole memory card could still recover it'));
 await page.keyboard.press('Escape');
 assert.equal(await page.locator('[data-help="maker-key-info"]').evaluate(el=>document.activeElement===el),true);
 await page.locator('#maker-key-fetch').click();
 await page.waitForFunction(()=>document.querySelector('#maker-key-feedback').textContent==='Internet access is required on the bridge.');
 results.push('Profile-only placement; accessible information dialog; demo never fetches the maker package');
 await page.locator('#maker-key-file').setInputFiles({name:'unusable.pem',mimeType:'text/plain',buffer:Buffer.from('unusable')});
 await page.waitForFunction(()=>document.querySelector('#maker-key-feedback').textContent==='This key file cannot be used.');
 await page.locator('#maker-key-file').setInputFiles({name:'runtime-key.pem',mimeType:'text/plain',buffer:key});
 await page.waitForFunction(()=>document.querySelector('#maker-key-state').textContent==='Key ready, not yet confirmed by the Seestar');
 assert.ok(!(await page.content()).includes(key.toString()),'Key never rendered into DOM');
 assert.equal((await request('GET','/maker-keys')).keys[0].state,'unverified');
 results.push('Unusable file rejected; runtime-only key import encrypted on disposable bridge; key absent from DOM');
 const holdTelemetry=async()=>{
  let unblock,captured;const held=new Promise(resolve=>{captured=resolve;}),gate=new Promise(resolve=>{unblock=resolve;});let first=true;
  const handler=async route=>{if(!first){await route.continue();return;}first=false;const response=await route.fetch();captured();await gate;await route.fulfill({response});};
  await page.route('**/api/telemetry',handler);
  await page.evaluate(()=>{window.sessionBarrierDone=false;void loadSessionSelection(makerKeyEpoch).then(()=>{window.sessionBarrierDone=true;});});
  await held;
  return async()=>{unblock();await page.waitForFunction(()=>window.sessionBarrierDone);await page.unroute('**/api/telemetry',handler);};
 };
 await page.waitForFunction(()=>document.querySelector('#seestar-session-enabled').checked&&!document.querySelector('#seestar-session-enabled').disabled);
 let releaseOld=await holdTelemetry();
 await page.locator('#seestar-session-enabled').uncheck();
 await page.waitForFunction(()=>document.querySelector('#seestar-session-feedback').textContent==='Session data settings saved.').catch(async error=>{console.log('SESSION_UI',await page.evaluate(()=>({feedback:document.querySelector('#seestar-session-feedback').textContent,checked:document.querySelector('#seestar-session-enabled').checked,disabled:document.querySelector('#seestar-session-enabled').disabled})));throw error;});
 const disabledState=await request('GET','/telemetry');assert.ok(!disabledState.profiles['seestar-demo'].adapters.includes('seestar_events_v1'));
 await releaseOld();assert.equal(await page.locator('#seestar-session-enabled').isChecked(),false);assert.equal(await page.evaluate(()=>sessionRevision),disabledState.config_revision);
 releaseOld=await holdTelemetry();
 const repeatedImport=page.waitForResponse(r=>r.url().endsWith('/api/maker-keys/seestar')&&r.request().method()==='PUT');
 await page.locator('#maker-key-file').setInputFiles({name:'runtime-key.pem',mimeType:'text/plain',buffer:key});await repeatedImport;
 await page.waitForFunction(()=>!document.querySelector('#seestar-session-enabled').disabled);
 const importedState=await request('GET','/telemetry');await releaseOld();
 assert.equal(await page.locator('#seestar-session-enabled').isChecked(),false);assert.equal(await page.evaluate(()=>sessionRevision),importedState.config_revision);
 await page.evaluate(()=>{clearTimeout(makerKeyTimer);sessionRevision='stale';});await page.locator('#seestar-session-enabled').check();
 await page.waitForFunction(()=>document.querySelector('#seestar-session-feedback').textContent.includes('Configuration changed.'));
 assert.equal(await page.locator('#seestar-session-enabled').isChecked(),false);
 await page.locator('#seestar-session-enabled').check();
 await page.waitForFunction(()=>document.querySelector('#seestar-session-feedback').textContent==='Session data settings saved.').catch(async error=>{console.log('SESSION_UI',await page.evaluate(()=>({feedback:document.querySelector('#seestar-session-feedback').textContent,checked:document.querySelector('#seestar-session-enabled').checked,disabled:document.querySelector('#seestar-session-enabled').disabled})));throw error;});
 assert.ok((await request('GET','/telemetry')).profiles['seestar-demo'].adapters.includes('seestar_events_v1'));
 results.push('Visible automatic session default; off survives reimport; delayed old GETs cannot overwrite switch/reimport revisions; stale revision refuses change; re-enabled setting persists');
 await page.locator('#close-editor').click();await page.locator('#tab-plugins').click();
 await page.locator('[data-plugin-id="seestar"]').getByText('Session: full (with your key)',{exact:true}).waitFor();
 await page.locator('#tab-telescopes').click();await page.locator('#profile-seestar-demo-edit').click();
 await page.waitForFunction(()=>!document.querySelector('#seestar-session-enabled').disabled);


 for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){
  await page.evaluate(tag=>BridgeI18n.setLocale(tag),locale);await page.setViewportSize({width:320,height:960});
  assert.equal(await page.locator('#maker-key-section').isVisible(),true);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,locale);
 }
 await page.evaluate(()=>BridgeI18n.setLocale('en'));
 await page.locator('#maker-key-section').scrollIntoViewIfNeeded();await page.screenshot({path:resolve(output,'maker-key-320.png')});
 page.once('dialog',dialog=>dialog.dismiss());await page.locator('#maker-key-remove').click();
 assert.equal((await request('GET','/maker-keys')).keys[0].state,'unverified');
 page.once('dialog',dialog=>dialog.accept());await page.locator('#maker-key-remove').click();
 await page.waitForFunction(()=>document.querySelector('#maker-key-state').textContent==='No key · stacking data unavailable');
 assert.equal((await request('GET','/maker-keys')).keys[0].state,'absent');
 results.push('Ten locales at320px; remove cancellation preserves key; confirmed removal clears shared key');
 await page.locator('#close-editor').click();await page.locator('#tab-plugins').click();
 await page.locator('[data-plugin-id="seestar"]').getByText('Session: position only',{exact:true}).waitFor();
 await page.locator('#tab-telescopes').click();await page.locator('#profile-seestar-demo-edit').click();
 results.push('Plugin chip reflects key-supported full session capability and position-only state after removal');

 for(const [state,label] of [['verified','Key confirmed by the Seestar'],['refused','The Seestar refused this key'],['storage_unavailable','Encrypted key storage is unavailable on this bridge.']]){
  await page.route('**/api/maker-keys',async route=>{const actual=await request('GET','/maker-keys');actual.keys[0].state=state;await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(actual)});});
  await page.locator('#close-editor').click();await page.locator('#profile-seestar-demo-edit').click();
  await page.waitForFunction(text=>document.querySelector('#maker-key-state').textContent===text,label);
  if(state==='storage_unavailable'){assert.equal(await page.locator('#maker-key-fetch').isDisabled(),true);assert.equal(await page.locator('#maker-key-import').isDisabled(),true);}
  await page.unroute('**/api/maker-keys');
 }
 results.push('Synthetic confirmed/refused/unavailable metadata renders truthful states; unavailable storage disables writes');
 await page.locator('#close-editor').click();
 await page.evaluate(()=>{document.querySelector('#user-menu').open=true;});await page.locator('#logout').click();
 await page.locator('#login').waitFor({state:'visible'});
 assert.equal(await page.locator('#maker-key-file').inputValue(),'');
 assert.equal(await page.locator('#editor').isVisible(),false);
 assert.ok(key.toString().split('\n').filter(Boolean).every(line=>!logs.includes(line)),'No key line in process logs');
 assert.deepEqual(errors,[]);
 await writeFile(resolve(output,'results.json'),JSON.stringify({results,errors,simulation:true},null,2));for(const result of results)console.log('PASS',result);
}finally{key.fill(0);if(browser)await browser.close();server.kill('SIGTERM');await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});}
