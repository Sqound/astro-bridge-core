/** Profile connection consent, real save/reload and narrow localized layouts. */
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
const folder=await mkdtemp(resolve(tmpdir(),'astro-instruments-'));
const output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/p0-10-instruments');await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;const errors=[],results=[];server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
try{
 for(let n=0;!logs.includes('SIMULATION');n++){if(n===100)throw new Error('Demo startup timed out');await new Promise(r=>setTimeout(r,100));}
 const origin=`http://127.0.0.1:${port}`;
 const login=await fetch(origin+'/api/v1/auth/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:'12345678'})});const token=(await login.json()).access_token;
 const request=async(method,path,body)=>{const r=await fetch(origin+'/api/v1'+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});assert.ok(r.ok);return r.json();};
 await request('POST','/telescopes',{id:'native-fixture',name:'Synthetic telescope',kind:'seestar',ssid:'Synthetic telescope',open:true,target_ip:'198.51.100.1'});
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:960}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(origin);await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 await page.evaluate(()=>openEditor(state.config.telescopes.find(p=>p.id==='native-fixture')));await page.locator('#profile-advanced > summary').click();
 assert.equal(await page.locator('#profile-client-connect').isChecked(),true);
 for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){
  await page.evaluate(tag=>BridgeI18n.setLocale(tag),locale);await page.setViewportSize({width:320,height:960});
  await page.locator('#profile-client-row .info-button').click();assert.equal(await page.locator('#profile-client-info').evaluate(el=>el.open),true);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,locale);
  await page.locator('#profile-client-info [data-close-help]').click();
 }
 results.push('ten complete catalogs, default enabled consent and accessible information dialog at 320px');
 await page.evaluate(()=>BridgeI18n.setLocale('en'));await page.locator('#profile-client-connect').uncheck();await page.locator('#network-save').click();await page.locator('#editor').waitFor({state:'hidden'});
 assert.equal((await request('GET','/telescopes/native-fixture')).telescope.client_connect,false);
 await page.evaluate(()=>openEditor(state.config.telescopes.find(p=>p.id==='native-fixture')));await page.locator('#profile-advanced > summary').click();assert.equal(await page.locator('#profile-client-connect').isChecked(),false);
 await page.locator('#profile-client-row').screenshot({path:resolve(output,'profile-consent-320.png')});
 await page.locator('#profile-client-connect').check();await page.locator('#network-save').click();await page.locator('#editor').waitFor({state:'hidden'});
 assert.equal((await request('GET','/telescopes/native-fixture')).telescope.client_connect,true);
 results.push('production browser save and reopen preserve disabled/enabled per-profile consent');assert.deepEqual(errors,[]);
 await writeFile(resolve(output,'results.json'),JSON.stringify({results,errors,simulation:true},null,2));for(const result of results)console.log('PASS',result);
}finally{if(browser)await browser.close();server.kill('SIGTERM');await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});}
