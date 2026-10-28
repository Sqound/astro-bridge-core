import {clickUserControl} from "./browser-helpers.mjs";
/** Guided HTTPS UI with real temporary helper effects; no OS/browser trust changes. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdir,mkdtemp,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const temporary=await mkdtemp(resolve(tmpdir(),'astro-https-ui-')),output=resolve(root,'artifacts/p4b2-https');await mkdir(output,{recursive:true});
// Capability/root injection belongs only to this fixture. Production CLI/API has no path override.
const fixture=`
import json,sys,threading
from pathlib import Path
sys.path.insert(0,'tests')
from test_secure_access import SecureAccessTests
case=SecureAccessTests('test_real_setup_ca_download_fresh_tls_login_and_native_disable_enable')
case.setUp()
print(json.dumps({'port':case.http.server_port}),flush=True)
try: sys.stdin.read()
finally: case.doCleanups()
`;
const server=spawn('python3',['-u','-c',fixture],{cwd:root,stdio:['pipe','pipe','pipe']});let logs='',browser;
server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
const wait=async predicate=>{const end=Date.now()+20000;while(!await predicate()){if(Date.now()>end)throw new Error('Condition timeout: '+logs);await new Promise(r=>setTimeout(r,100));}};
const results=[],errors=[];
try{
  await wait(()=>logs.includes('"port"'));const {port}=JSON.parse(logs.split('\n')[0]);
  browser=await chromium.launch({headless:true,args:['--host-resolver-rules=MAP *.local 127.0.0.1','--no-proxy-server','--ignore-certificate-errors'],...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
  const context=await browser.newContext({viewport:{width:1280,height:960},acceptDownloads:true}),page=await context.newPage();page.setDefaultTimeout(15000);page.on('pageerror',e=>errors.push(e.message));
  const check=async(name,fn)=>{await fn();results.push(name);console.log('PASS',name);};
  await page.goto(`http://127.0.0.1:${port}`);await page.locator('#login-password').fill('synthetic-password');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});await page.locator('#nav-configuration').click();await page.locator('#tab-pwa').click();await page.locator('#setup-os').selectOption('macos');await page.locator('#https-maintenance>summary').click();
  await page.waitForFunction(()=>!document.getElementById('https-submit').disabled);
  await check('unchecked one-operation UTC draft survives a transient status failure',async()=>{
    assert.equal(await page.locator('#https-manual-clock').isChecked(),false);assert.equal(await page.locator('#https-reset-confirm').isChecked(),false);
    await page.locator('#https-manual-clock').check();await page.locator('#https-utc').fill('2026-10-21T00:00:00Z');await page.locator('#https-password').fill('PRIVATE HTTPS DRAFT');
    let fail=true;await context.route('**/api/status',r=>fail?(fail=false,r.abort()):r.continue());
    await page.waitForFunction(()=>document.getElementById('connection-message').textContent.includes('unverified'));
    assert.equal(await page.locator('#https-password').inputValue(),'PRIVATE HTTPS DRAFT');assert.equal(await page.locator('#https-manual-clock').isChecked(),true);
    assert.equal(await page.evaluate(()=>document.getElementById('dashboard').inert),true);await context.unroute('**/api/status');await page.waitForFunction(()=>!document.getElementById('dashboard').inert);
  });
  await check('reauthenticated real setup clears assertion and distinguishes listener from trust',async()=>{
    await page.locator('#https-password').fill('synthetic-password');await page.locator('#https-utc').fill(new Date().toISOString().replace(/\.\d{3}Z$/,'Z'));
    await page.locator('#https-submit').click();assert.equal(await page.locator('#https-password').inputValue(),'');assert.equal(await page.locator('#https-manual-clock').isChecked(),false);assert.equal(await page.locator('#https-utc').inputValue(),'');
    await page.waitForFunction(()=>document.getElementById('https-listener').textContent.includes('listener ready'));
    assert.match(await page.locator('#https-support').textContent(),/Trust on this browser has not been verified/);
    assert.match(await page.locator('#https-issued').textContent(),/not NTP proof/);assert.match(await page.locator('#https-fingerprint').textContent(),/^([a-f0-9]{2}:){31}[a-f0-9]{2}$/);
    assert.match(await page.locator('#https-ca-expiry').textContent(),/^20/);assert.match(await page.locator('#https-leaf-expiry').textContent(),/^20/);
    const download=page.waitForEvent('download');await page.locator('#https-download').click();const file=await download;assert.equal(file.suggestedFilename(),'astro-bridge-ca.crt');assert.match(await readFile(await file.path(),'utf8'),/^-----BEGIN CERTIFICATE-----/);
    for(const os of ['ios','android','macos','windows','linux']){await page.locator('#setup-os').selectOption(os);assert.equal(await page.locator(`[data-setup-os="${os}"]`).isVisible(),true);}
    await page.locator('#setup-os').selectOption('ios');assert.equal(await page.locator('#https-profile').isVisible(),true);const stages=await page.locator('.ios-trust-steps h4').allTextContents();assert.deepEqual(stages,['Install the downloaded profile','Enable certificate trust after installation']);assert.match(await page.locator('.ios-trust-steps li').first().innerText(),/Downloaded Profile.*confirm installation/s);assert.equal(await page.locator('#https-download').isVisible(),false);
    for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){await page.evaluate(locale=>BridgeI18n.setLocale(locale),locale);await page.setViewportSize({width:320,height:960});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,locale);}
    await page.evaluate(()=>BridgeI18n.setLocale('en'));
    assert.equal(await page.locator('#https-open').isVisible(),true);assert.equal(await page.locator('#https-recovery-public').isVisible(),false);
    await page.locator('[data-help="https-info"]').click();assert.match(await page.locator('#https-guide').textContent(),/not independent proof/);assert.match(await page.locator('#https-guide').textContent(),/never bypass/);
    await page.locator('#https-info [data-close-help]').click();await page.locator('#https-maintenance>summary').click();
    await page.locator('#https-settings').screenshot({path:resolve(output,'guided-en.png')});
    await page.locator('#https-maintenance>summary').click();
  });
  await check('notification category choices are visible and stable on temporary HTTPS in EN and FR mobile',async()=>{
    const address=await page.evaluate(async()=>(await(await fetch('/api/https')).json()).listener.url);
    // Disposable browser only: test CA acceptance also covers worker fetches.
    // No trust store is changed; actual certificate/phone trust remains a physical gate.
    const secure=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1280,height:960}});
    try{
      const view=await secure.newPage();view.on('pageerror',e=>errors.push(e.message));
      await view.goto(address+'/#configuration/pwa');await view.locator('#login-password').fill('synthetic-password');
      const loginReply=view.waitForResponse(r=>r.url().endsWith('/api/login'),{timeout:5000});await view.locator('#login-form button').click();
      assert.equal((await loginReply).status(),200);
      await view.locator('#dashboard').waitFor({state:'visible',timeout:10000});
      await view.locator('#push-category-boot').evaluate(e=>e.closest('details').open=true);
      assert.equal(await view.locator('#push-category-boot').isVisible(),true);
      assert.equal(await view.locator('[id^="push-category-"]').count(),8);
      await view.locator('#push-category-boot').uncheck();await view.evaluate(()=>refresh());await view.waitForFunction(()=>!polling);
      assert.equal(await view.locator('#push-category-boot').isChecked(),false);
      await view.locator('#pwa-settings').screenshot({path:resolve(output,'notification-categories-en.png')});
      await view.evaluate(()=>BridgeI18n.setLocale('fr'));await view.setViewportSize({width:375,height:960});
      await view.locator('#pwa-settings').scrollIntoViewIfNeeded();
      assert.equal(await view.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await view.screenshot({path:resolve(output,'notification-categories-fr-375.png'),fullPage:false});
      assert.match(await view.locator('#pwa-settings').innerText(),/Catégories de notifications/);
    }finally{await secure.close();}
  });
  await check('disable and enable preserve identity; reset requires confirmation and clears private UI',async()=>{
    const fingerprint=await page.locator('#https-fingerprint').textContent();
    for(const action of ['disable','enable']){
      await page.locator('#https-action').selectOption(action);await page.locator('#https-password').fill('synthetic-password');await page.locator('#https-submit').click();
      await page.waitForFunction(ready=>document.getElementById('https-listener').textContent.includes(ready?'listener ready':'listener unavailable'),action==='enable');
      await page.waitForFunction(()=>!document.getElementById('https-refresh').disabled);assert.equal(await page.locator('#https-fingerprint').textContent(),fingerprint);
    }
    await page.evaluate(()=>BridgeI18n.setLocale('fr'));await page.waitForFunction(()=>document.documentElement.lang==='fr');
    for(const width of [320,375,768]){await page.setViewportSize({width,height:960});await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}
    await page.setViewportSize({width:375,height:960});await page.locator('#https-settings').scrollIntoViewIfNeeded();await page.evaluate(()=>document.getElementById('https-settings').scrollIntoView({block:'start'}));await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));await page.screenshot({path:resolve(output,'guided-fr-375.png'),fullPage:false});
    await page.locator('[data-help="https-info"]').click();await page.locator('#https-guide').scrollIntoViewIfNeeded();await page.screenshot({path:resolve(output,'trust-fr-375.png'),fullPage:false});
    await page.locator('#https-info [data-close-help]').click();await page.evaluate(()=>BridgeI18n.setLocale('en'));await page.locator('#https-action').selectOption('reset');await page.locator('#https-password').fill('synthetic-password');
    assert.equal(await page.locator('#https-reset-confirm').isChecked(),false);await page.locator('#https-reset-confirm').check();await page.locator('#https-submit').click();assert.equal(await page.locator('#https-reset-confirm').isChecked(),false);
    await page.waitForFunction(()=>document.getElementById('https-identity').textContent==='Not configured');
    await clickUserControl(page, '#logout');await page.locator('#login').waitFor({state:'visible'});assert.ok(!(await page.content()).includes(fingerprint));assert.equal(await page.locator('#https-password').inputValue(),'');
  });
  assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:results.length,pageErrors:errors,output},null,2));
}finally{if(browser)await browser.close();server.stdin.end();await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');await rm(temporary,{recursive:true,force:true});}
