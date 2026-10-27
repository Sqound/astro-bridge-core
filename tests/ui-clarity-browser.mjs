/** User-facing controls, OS onboarding and modal privacy against a disposable demo. */
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
const folder=await mkdtemp(resolve(tmpdir(),'astro-clarity-')),output=resolve(root,'artifacts/ui-clarity');await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','3','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});let logs='',browser;
server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
const wait=async fn=>{for(let i=0;i<150;i++){if(await fn())return;await new Promise(r=>setTimeout(r,100));}throw new Error('Timeout: '+logs);};
const results=[],errors=[];
try{
 await wait(()=>logs.includes('SIMULATION'));
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:900}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${port}`);await page.locator('#login-password').fill('demo-telescope-2026');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 const check=async(name,fn)=>{await fn();results.push(name);console.log('PASS',name);};
 await check('home switch updates only the master preference, with supported-app summary',async()=>{
  assert.equal(await page.locator('#auto-connect').getAttribute('role'),'switch');await page.locator('#auto-connect').check();
  await page.waitForFunction(()=>document.getElementById('auto-switch-summary').textContent.includes('Seestar'));
  const value=await page.evaluate(async()=>await(await fetch('/api/status')).json());assert.equal(value.config.auto_connect,true);assert.equal(value.config.auto_policy.dwarf_demand,true);assert.equal(value.config.auto_policy.vespera_demand,true);assert.equal(value.config.auto_policy.seestar_demand,true);assert.equal(value.config.auto_policy.force_first_profile,false);
  await page.locator('#auto-switch-card').screenshot({path:resolve(output,'home-switch-375.png')});
 });
 await check('saved-instrument count opens telescope configuration',async()=>{
  await page.locator('#saved-instruments-link').click();await page.locator('#panel-telescopes').waitFor({state:'visible'});assert.equal(await page.locator('#tab-telescopes').getAttribute('aria-selected'),'true');await page.locator('#nav-telescopes').click();
 });
 await check('brand explanation is optional, modal, stable across polling and keyboard accessible',async()=>{
  const opener=page.locator('[data-help="auto-info"]');await opener.click();assert.equal(await page.locator('#auto-info').evaluate(e=>e.open),true);
  for(const brand of ['Seestar','Vespera','DWARF'])assert.ok((await page.locator('#auto-info').innerText()).includes(brand));
  assert.equal(await page.locator('#auto-info input').count(),0);await page.waitForTimeout(5500);assert.equal(await page.locator('#auto-info').evaluate(e=>e.open),true);
  await page.locator('#auto-info').screenshot({path:resolve(output,'brands-info-375.png')});await page.keyboard.press('Escape');assert.equal(await opener.evaluate(e=>e===document.activeElement),true);
 });
 await check('home settings link opens and focuses advanced switching controls',async()=>{
  await page.locator('#auto-settings-open').click();
  assert.equal(await page.locator('#auto-advanced').evaluate(e=>e.open),true);
  await page.waitForFunction(()=>document.activeElement===document.querySelector('#auto-advanced>summary'));
  assert.equal(await page.locator('#tab-general').getAttribute('aria-selected'),'true');
  await page.locator('#auto-advanced>summary').click();
 });
 await page.locator('#tab-pwa').click();
 await check('one HTTP prerequisite links to a single OS-specific procedure',async()=>{
  assert.equal(await page.locator('#pwa-tools').isVisible(),false);assert.ok((await page.locator('#pwa-context').innerText()).includes('HTTPS'));
  assert.ok(!(await page.locator('#pwa-settings').innerText()).includes('Opened as an application'));
  assert.ok(!(await page.locator('#pwa-settings').innerText()).includes('HTTP access still works'));
  await page.locator('#pwa-prerequisite [data-setup-link]').click();assert.equal(await page.locator('#setup-os').evaluate(e=>e===document.activeElement),true);
  await page.locator('#setup-os').selectOption('ios');assert.equal(await page.locator('[data-setup-os="ios"]').evaluate(e=>e.hidden),false);assert.equal(await page.locator('[data-setup-os="android"]').evaluate(e=>e.hidden),true);assert.equal(await page.locator('#setup-steps').isVisible(),false);
  await page.waitForTimeout(5500);assert.equal(await page.locator('#setup-os').inputValue(),'ios');
  await page.locator('#https-settings').screenshot({path:resolve(output,'setup-ios-375.png')});
  for(const os of ['android','macos','windows','linux']){await page.locator('#setup-os').selectOption(os);assert.equal(await page.locator(`[data-setup-os="${os}"]`).evaluate(e=>e.hidden),false);}
 });
 await page.locator('#tab-general').click();
 await check('advanced controls stay collapsed and preserve an open draft through polling',async()=>{
  assert.equal(await page.locator('#auto-advanced').evaluate(e=>e.open),false);assert.equal(await page.locator('#https-maintenance').evaluate(e=>e.open),false);
  await page.locator('#auto-advanced>summary').click();await page.locator('#auto-quiet').fill('180');await page.locator('#auto-quiet').focus();await page.waitForTimeout(5500);
  assert.equal(await page.locator('#auto-quiet').inputValue(),'180');assert.equal(await page.locator('#auto-quiet').evaluate(e=>e===document.activeElement),true);
  await page.locator('#auto-advanced>summary').click();
 });
 await check('dedicated PWA tab follows General and has an introduction; protection has one help button',async()=>{
  const order=await page.locator('.configuration [role=tab]').evaluateAll(items=>items.map(e=>e.id));assert.deepEqual(order,['tab-general','tab-pwa','tab-telescopes','tab-radios']);
  await page.locator('#tab-general').focus();await page.keyboard.press('ArrowRight');await page.waitForFunction(()=>!document.getElementById('panel-pwa').hidden);
  assert.match(await page.locator('#pwa-introduction').innerText(),/progressive web app.*Home Screen.*notifications/s);
  assert.equal(await page.locator('#panel-general #pwa-settings').count(),0);
  await page.locator('#tab-general').click();await page.locator('#connection-protection-settings>summary').click();
  assert.equal(await page.locator('#connection-protection-settings [data-help]').count(),1);
  assert.match(await page.locator('#protection-purpose').innerText(),/120.*60.*released or expired/);
  await page.locator('[data-help="protection-info"]').click();assert.equal(await page.locator('#protection-info #activity-status').count(),1);
  const close=page.locator('#protection-info [data-close-help]');assert.equal(await close.innerText(),'✕');assert.equal(await close.getAttribute('aria-label'),'Close');
  const geometry=await close.evaluate(e=>{const b=e.getBoundingClientRect(),h=e.parentElement.getBoundingClientRect();return{width:b.width,height:b.height,right:h.right-b.right}});assert.equal(geometry.width,40);assert.equal(geometry.height,40);assert.ok(geometry.right>=-1&&geometry.right<8);
  await close.click();await page.locator('#connection-protection-settings>summary').click();
 });
 await check('mobile notification actions remain separated when stacked and update help sits in the heading',async()=>{
  await page.setViewportSize({width:375,height:900});await page.locator('#tab-pwa').click();await page.locator('#pwa-settings').waitFor({state:'visible'});
  const layout=await page.evaluate(()=>{
   const ids=['pwa-tools','push-enable','push-disable','push-test'],original=ids.map(id=>document.getElementById(id).hidden);
   try{ids.forEach(id=>document.getElementById(id).hidden=id==='push-enable');
    const a=document.getElementById('push-disable').getBoundingClientRect(),b=document.getElementById('push-test').getBoundingClientRect();
    return {gap:b.left>=a.right?b.left-a.right:b.top-a.bottom,widthA:a.width,widthB:b.width};
   }finally{ids.forEach((id,i)=>document.getElementById(id).hidden=original[i]);}
  });assert.ok(layout.widthA>0&&layout.widthB>0&&layout.gap>=10,'wrapped action buttons must have a visible gap: '+JSON.stringify(layout));
  await page.locator('#tab-general').click();assert.equal(await page.locator('[data-help="update-info"]').evaluate(e=>e.parentElement.classList.contains('card-heading')),true);
 });
 await check('home distinguishes traffic, manual protection, quiet delay and forced replacement without changing policy',async()=>{
  await page.locator('#nav-telescopes').click();
  const before=await page.evaluate(async()=>await(await fetch('/api/status')).json());
  const show=async(fixture)=>page.evaluate(fixture=>{
   const s=structuredClone(state),d=s.devices[0],p={id:'guard-fixture',name:'Guard fixture',kind:'vespera',mac:d.mac,enabled:true,auto_eligible:true,auto_replaceable:true,...fixture.profile};
   s.config.telescopes=[p];s.config.home={};s.config.auto_connect=true;s.config.auto_policy={quiet_seconds:120,cooldown_seconds:60,force_first_profile:false,...fixture.policy};s.hotspot={active:false};
   s.telescopes=[{id:p.id,connected:true,interface:d.name,relay_ready:true}];s.connection_protection={owner_locks:[],leases:[],...fixture.protection};s.connection_activity={supported:true,profiles:{[p.id]:{sample_age_seconds:0,state:'active',quiet_seconds:0,cooldown_remaining:0,...fixture.activity}}};renderSimple(s);
  },fixture);
  const badge=page.locator('[data-guard-state]').first();
  await show({});assert.match(await badge.innerText(),/Traffic active.*blocked while active/s);
  await show({profile:{owner_locked:true}});assert.equal(await badge.getAttribute('data-guard-state'),'protected');assert.match(await badge.innerText(),/until manually released/);
  await show({protection:{leases:[{profile_id:'guard-fixture',remaining_seconds:85}]}});assert.match(await badge.innerText(),/Protected for 85 s/);
  await show({activity:{state:'unknown',reason:'WARMING_UP',quiet_seconds:30}});assert.match(await badge.innerText(),/90 s remaining/);
  await show({activity:{state:'quiet',quiet_seconds:130,cooldown_remaining:20}});assert.match(await badge.innerText(),/between switches: 20 s/);
  await show({activity:{state:'quiet',quiet_seconds:130}});assert.match(await badge.innerText(),/No recent traffic.*May be reused/s);
  await show({activity:{state:'quiet',quiet_seconds:130,sample_age_seconds:16}});assert.match(await badge.innerText(),/Activity not verified.*activity unverified/s);
  await show({policy:{force_first_profile:true}});assert.equal(await badge.getAttribute('data-guard-state'),'warning');assert.match(await badge.innerText(),/may be replaced/);
  await show({profile:{owner_locked:true},policy:{force_first_profile:true}});assert.equal(await badge.getAttribute('data-guard-state'),'protected');
  const after=await page.evaluate(async()=>await(await fetch('/api/status')).json());assert.deepEqual(after.config,before.config);await page.evaluate(()=>renderSimple(state));
 });
 await check('main pages and help dialogs fit small screens',async()=>{
  for(const width of [320,375,768,1280]){
   await page.setViewportSize({width,height:900});
   for(const name of ['telescopes','networks','configuration']){
    await page.locator('#nav-'+name).click();if(name==='configuration')await page.locator('#tab-pwa').click();
    const overflowing=await page.evaluate(()=>[...document.querySelectorAll('body *')].filter(e=>{const r=e.getBoundingClientRect();return r.width&&r.right>innerWidth+1}).map(e=>({id:e.id,tag:e.tagName,width:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right,text:e.textContent.slice(0,50)})).slice(0,30));
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)){await writeFile(resolve(output,'overflow.json'),JSON.stringify({width,name,overflowing},null,2));await page.screenshot({path:resolve(output,'overflow.png')});}
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,JSON.stringify({width,name,overflowing}));
   }
  }
 });
 await page.locator('#tab-pwa').click();
 await check('logout closes an open information dialog and clears private diagnostic leaves',async()=>{
  await page.locator('[data-help="https-info"]').click();assert.equal(await page.locator('#https-info').evaluate(e=>e.open),true);
  await page.evaluate(async()=>{await fetch('/api/logout',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':(await(await fetch('/api/status')).json()).csrf},body:'{}'});});
  await page.waitForFunction(()=>document.getElementById('login').hidden===false);assert.equal(await page.locator('#https-info').evaluate(e=>e.open),false);assert.equal(await page.locator('#https-password').inputValue(),'');
 });
 assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:results.length,pageErrors:errors},null,2));
}finally{if(browser)await browser.close();server.kill('SIGTERM');await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});}
