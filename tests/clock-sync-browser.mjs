/** When the bridge clock was last set, in the web interface (demo server; the status reply is rewritten per state). */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,mkdir,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createServer} from 'node:net';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const before=Boolean(process.env.CLOCK_BEFORE);// Capture only: the code before this change has no such line.
const folder=await mkdtemp(resolve(tmpdir(),'astro-clock-ui-')),output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/clock-sync');await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;const errors=[],results=[];server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
const base={clock_status:'unverified',system_utc:'2026-11-20T09:41:00.000+00:00',corrected_utc:'2026-11-20T09:41:00.000+00:00',source:'lower_bound',system_plausible:true,set_from_browser:false};
const sync=(kind,age,known=null)=>({kind,age_seconds:age,this_run:age!==null,last_network_sync_utc:known});
// code, name, clock fields, then French / English lines expected (null = line absent).
const STATES=[
 ['H1','jamais',{},null,null],
 ['H2','synchronise-3-min',{clock_status:'synchronized',source:'ntp',last_sync:sync('network',200,'2026-11-20T09:37:40.000+00:00')},
  'Horloge mise à l’heure par Internet il y a 3 minutes.','Clock last set from network time 3 minutes ago.'],
 ['H3','perdu-2-h',{last_sync:sync('network',7500,'2026-11-20T07:36:00.000+00:00')},
  'Horloge mise à l’heure par Internet il y a 2 heures.','Clock last set from network time 2 hours ago.'],
 ['H4','perdu-5-jours',{last_sync:sync('network',5*86400+3000,'2026-11-15T08:50:00.000+00:00')},
  'Horloge mise à l’heure par Internet il y a 5 jours.','Clock last set from network time 5 days ago.'],
 ['H5','redemarre-sans-internet',{last_sync:sync('network',null,'2026-11-18T14:03:00.000+00:00')},
  'Horloge non mise à l’heure depuis le démarrage du pont. Dernière mise à l’heure connue : 18 nov. 2026, 14:03 UTC.','Not set from network time since the bridge started. Last known setting: Nov 18, 2026, 2:03 PM UTC.'],
 ['H6','heure-indicative-seule',{source:'client_hint',last_sync:sync('client_hint',45)},
  'Seule une heure approximative a été reçue d’un téléphone ou d’un navigateur il y a moins d’une minute. Ce n’est pas l’heure d’Internet.','Only an approximate time from a phone or browser was received less than a minute ago. It is not network time.'],
 ['H7','horloge-fausse',{system_utc:'1970-01-01T00:00:12.000+00:00',system_plausible:false,last_sync:sync('never',null)},
  'Horloge jamais mise à l’heure par Internet.','Clock never set from network time.'],
];
const ADVICE_FR='Horloge non vérifiée. Reliez le pont à Internet ou ouvrez l’app pour qu’il puisse la mettre à l’heure.';
const ADVICE_EN='Clock not verified. Connect the bridge to the Internet or open the app so it can set its clock.';
try{
 for(let n=0;n<150&&!logs.includes('SIMULATION');n++)await new Promise(r=>setTimeout(r,100));
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:430,height:960}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 let mock=null;
 await page.route('**/api/status',async route=>{const reply=await route.fetch();if(!mock)return route.fulfill({response:reply});
  const body=await reply.json();body.bridge_clock={...base,...mock};await route.fulfill({response:reply,json:body});});
 const open=async()=>{await page.goto(`http://127.0.0.1:${port}`);
  await page.locator('#dashboard,#login-form button').first().waitFor({state:'visible'});await page.waitForTimeout(400);
  if(!await page.locator('#dashboard').isVisible()){await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();}
  await page.locator('#dashboard').waitFor({state:'visible'});await page.locator('#nav-configuration').click();await page.locator('#tab-general').click();};
 const card=page.locator('section.card:has(#preferences)');
 const shot=async(file)=>{await card.evaluate(e=>e.scrollIntoView({block:'start'}));await page.evaluate(()=>scrollBy(0,-12));await page.waitForTimeout(150);
  const box=await card.boundingBox();await page.screenshot({path:file,clip:{x:Math.max(0,box.x-8),y:Math.max(0,box.y-8),width:Math.min(box.width+16,page.viewportSize().width),height:Math.min(box.height,330)}});};
 const locale=async tag=>{await page.evaluate(l=>BridgeI18n.setLocale(l),tag);};
 for(const [code,name,fields,fr,en] of STATES){
  mock=code==='H1'?null:fields;await open();await locale('fr');
  if(code==='H7'&&before)mock=fields;
  if(code==='H7'){await page.waitForFunction(()=>!document.getElementById('bridge-clock').hidden);}
  const line=page.locator('#clock-sync-line'),advice=page.locator('#clock-sync-advice');
  if(!before){
   await line.waitFor({state:'visible'});
   if(fr)assert.equal(await line.innerText(),fr,code);
   if(code==='H1'){assert.equal(await line.innerText(),'Horloge jamais mise à l’heure par Internet.');}
   assert.equal(await advice.isVisible(),code!=='H2',code+' advice');
   if(code!=='H2')assert.equal(await advice.innerText(),ADVICE_FR,code);
   await locale('en');assert.equal(await line.innerText(),en||'Clock never set from network time.',code);
   if(code!=='H2')assert.equal(await advice.innerText(),ADVICE_EN,code);
   // Nothing glued: the card keeps its inner rhythm and the page never scrolls sideways, in all ten languages.
   const edge=await page.evaluate(()=>{const box=document.getElementById('clock-sync').getBoundingClientRect(),form=document.getElementById('preferences').getBoundingClientRect();return form.top-box.bottom;});
   assert.ok(edge>=8,code+' spacing '+edge);
   for(const tag of ['fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){await locale(tag);assert.ok((await line.innerText()).trim().length>10,tag);assert.notEqual(await line.innerText(),en||'Clock never set from network time.',tag);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,tag+' '+code);}
   if(code==='H1'||code==='H5'||code==='H6')for(const tag of ['ja','de']){await locale(tag);await shot(resolve(output,`${code}-${name}-${tag}-apres.png`));}
   if(code==='H7')assert.ok((await page.locator('#bridge-clock').innerText()).includes('UTC'));
   await locale('fr');
  }
  await shot(resolve(output,`${code}-${name}-fr-${before?'avant':'apres'}.png`));
  if(code==='H7'){await page.evaluate(()=>scrollTo(0,0));await page.waitForTimeout(150);await page.screenshot({path:resolve(output,`${code}-${name}-fr-haut-de-page-${before?'avant':'apres'}.png`),clip:{x:0,y:0,width:430,height:330}});}
  results.push(`${code} ${name}`);
 }
 // H7 made the page send this browser's time (a manifestly wrong clock): the real server now reports a hint only, not network time.
 if(!before){mock=null;await open();await locale('en');
  assert.match(await page.locator('#clock-sync-line').innerText(),/^Only an approximate time from a phone or browser was received (less than a minute ago|1 minute ago)\. It is not network time\.$/);
  assert.equal(await page.locator('#clock-sync').count(),1);}
 assert.deepEqual(errors,[]);
 for(const line of results)console.log('PASS '+line);
}catch(error){console.error(logs.slice(-4000));throw error;}
finally{await browser?.close();server.kill();await once(server,'exit').catch(()=>{});await rm(folder,{recursive:true,force:true});}
