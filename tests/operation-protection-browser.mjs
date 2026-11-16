/** Advanced operation protection: real consent, revision, save/reload and mobile layout. */
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
const folder=await mkdtemp(resolve(tmpdir(),'astro-operation-policy-'));
const output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/p1-06-protect');await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;const errors=[],results=[];server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
try{
 for(let n=0;!logs.includes('SIMULATION');n++){if(n===100)throw new Error('Demo startup timed out');await new Promise(r=>setTimeout(r,100));}
 const origin=`http://127.0.0.1:${port}`;
 const login=await fetch(origin+'/api/v1/auth/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:'12345678'})});const token=(await login.json()).access_token;
 const request=async(method,path,body)=>{const r=await fetch(origin+'/api/v1'+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});assert.ok(r.ok);return r.json();};
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:960}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(origin);await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 await page.locator('#auto-settings-open').click();
 const flag=page.getByLabel('Protect running instrument operations',{exact:true});
 assert.equal(await flag.isChecked(),false);
 await flag.check();await page.locator('#auto-policy-save').click();
 await page.waitForFunction(()=>document.querySelector('#auto-policy-feedback').textContent.includes('not saved'));
 assert.equal((await request('GET','/settings')).settings.auto_policy.protect_running_operations,false);
 await page.locator('#auto-ack').check();await page.locator('#auto-policy-save').click();
 await page.waitForFunction(()=>document.querySelector('#auto-policy-feedback').textContent==='Automatic policy saved.');
 assert.equal((await request('GET','/settings')).settings.auto_policy.protect_running_operations,true);
 results.push('default off; enabling without acknowledgement is rejected; acknowledged save persists');
 await flag.uncheck();await request('PATCH','/settings',{country:'FR'});
 await page.locator('#auto-policy-save').click();
 await page.waitForFunction(()=>document.querySelector('#auto-policy-feedback').textContent.includes('changed elsewhere'));
 assert.equal(await flag.isChecked(),false);
 assert.equal((await request('GET','/settings')).settings.auto_policy.protect_running_operations,true);
 await page.locator('#auto-policy-reload').click();
 await page.waitForFunction(()=>document.querySelector('#auto-protect-operations').checked);
 results.push('stale revision rejected; draft preserved; explicit reload restores current policy');
 for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){
  await page.evaluate(tag=>BridgeI18n.setLocale(tag),locale);await page.setViewportSize({width:320,height:960});
  assert.equal(await page.locator('#auto-protect-operations').isVisible(),true);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,locale);
 }
 await page.evaluate(()=>BridgeI18n.setLocale('en'));
 await page.locator('#auto-protect-operations').scrollIntoViewIfNeeded();
 await page.screenshot({path:resolve(output,'operation-protection-320.png')});
 await flag.uncheck();await page.locator('#auto-policy-save').click();
 await page.waitForFunction(()=>document.querySelector('#auto-policy-feedback').textContent==='Automatic policy saved.');
 assert.equal((await request('GET','/settings')).settings.auto_policy.protect_running_operations,false);
 await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});
 await page.locator('#nav-telescopes').click();await page.locator('#auto-settings-open').click();
 assert.equal(await page.locator('#auto-protect-operations').isChecked(),false);
 results.push('ten complete catalogs at320px; disabling without acknowledgement survives page reload');
 assert.deepEqual(errors,[]);
 await writeFile(resolve(output,'results.json'),JSON.stringify({results,errors,simulation:true},null,2));for(const result of results)console.log('PASS',result);
}finally{if(browser)await browser.close();server.kill('SIGTERM');await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});}
