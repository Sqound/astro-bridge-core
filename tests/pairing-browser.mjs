/** Real paired-app list and revocation on a disposable production demo. */
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
const folder=await mkdtemp(resolve(tmpdir(),'astro-pairing-'));
const output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/p0-06-pairing');
await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');
const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;const errors=[],results=[];
server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
try{
 for(let n=0;!logs.includes('SIMULATION');n++){if(n===100)throw new Error('Demo startup timed out');await new Promise(r=>setTimeout(r,100));}
 const origin=`http://127.0.0.1:${port}`;
 const request=async(method,path,data,token)=>{
  const response=await fetch(origin+'/api/v1'+path,{method,headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},...(data?{body:JSON.stringify(data)}:{})});
  return {status:response.status,body:await response.json()};
 };
 const admin=(await request('POST','/auth/token',{password:'12345678'})).body.access_token;
 const paired=(await request('POST','/auth/clients',{name:'Synthetic observing app',scopes:['read','connect','telemetry','camera']},admin)).body;
 const scoped=(await request('POST','/auth/token',{client_id:paired.client_id,client_secret:paired.client_secret})).body.access_token;
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:960}});page.setDefaultTimeout(10000);
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(origin);
 await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();
 await page.locator('#dashboard').waitFor({state:'visible'});
 await page.evaluate(()=>navigate('configuration','general'));
 await page.locator('#paired-apps-list').getByText('Synthetic observing app',{exact:true}).waitFor();
 const revoke=page.locator('#paired-apps-list button');
 assert.ok((await revoke.getAttribute('class')).includes('secondary'));
 await revoke.focus();await page.waitForTimeout(2500);
 assert.equal(await revoke.evaluate(el=>el===document.activeElement),true);
 assert.ok(!(await page.locator('body').textContent()).includes(paired.client_secret));
 results.push('real paired-app list, secondary revoke, focus survives polling, no secret in DOM');
 for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){
  await page.evaluate(tag=>BridgeI18n.setLocale(tag),locale);
  await page.setViewportSize({width:320,height:960});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,locale);
  if(locale!=='en')assert.notEqual(await page.locator('#paired-apps h2').textContent(),'Paired apps');
  assert.equal(await page.locator('#paired-apps-list button').getAttribute('aria-label'),await page.evaluate(()=>BridgeI18n.t('Revoke access for {name}',{name:'Synthetic observing app'})));
 }
 results.push('ten translated app lists and accessible revoke controls fit at 320px');
 await page.evaluate(()=>BridgeI18n.setLocale('en'));
 await page.locator('#paired-apps').evaluate(el=>el.scrollIntoView({block:'center'}));
 await page.locator('#paired-apps').screenshot({path:resolve(output,'paired-apps-320.png')});
 await page.locator('#paired-apps-list button').click();
 await page.locator('#paired-apps-list').getByText('No paired apps.',{exact:true}).waitFor();
 assert.equal((await request('GET','/status',undefined,scoped)).status,401);
 assert.equal((await request('POST','/auth/token',{client_id:paired.client_id,client_secret:paired.client_secret})).body.error.code,'INVALID_CLIENT');
 results.push('browser CSRF revoke removes credentials and invalidates issued scoped token');
 await request('POST','/auth/clients',{name:'Synthetic privacy check',scopes:['read']},admin);
 await page.locator('#paired-apps-list').getByText('Synthetic privacy check',{exact:true}).waitFor();
 await page.locator('#user-menu > summary').click();await page.locator('#logout').click();
 await page.locator('#login').waitFor({state:'visible'});
 assert.equal(await page.locator('#paired-apps-list').textContent(),'');
 assert.ok(!(await page.locator('body').textContent()).includes('Synthetic privacy check'));
 results.push('logout clears paired-app names from the private DOM');
 assert.deepEqual(errors,[]);
 await writeFile(resolve(output,'results.json'),JSON.stringify({results,errors,simulation:true},null,2));
 for(const name of results)console.log('PASS',name);
}finally{
 if(browser)await browser.close();server.kill('SIGTERM');
 await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);
 if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});
}
