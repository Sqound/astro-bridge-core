import {clickUserControl} from "./browser-helpers.mjs";
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
const folder=await mkdtemp(resolve(tmpdir(),'astro-network-modes-')),output=resolve(root,'artifacts/network-0622');await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});let logs='',browser;
server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
const wait=async fn=>{for(let i=0;i<150;i++){if(await fn())return;await new Promise(r=>setTimeout(r,100));}throw new Error('Timeout: '+logs);};
const results=[],errors=[];
try{
 await wait(()=>logs.includes('SIMULATION'));
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:900}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${port}`);await page.locator('#login-password').fill('demo-telescope-2026');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 const check=async(name,fn)=>{await fn();results.push(name);console.log('PASS',name);};
 await page.locator('#nav-networks').click();
 await check('three explanatory modes, collapsed advanced access and one stable help modal',async()=>{
  assert.deepEqual(await page.locator('#connection-mode option').evaluateAll(items=>items.map(e=>e.value)),['private','hotspot_isolated','hotspot_shared']);
  assert.equal(await page.locator('#connection-mode-card details').evaluate(e=>e.open),false);
  assert.equal(await page.locator('#optional-hotspot').isVisible(),true);
  await page.locator('#connection-mode').selectOption('hotspot_shared');
  assert.equal(await page.locator('#optional-hotspot').isVisible(),false);
  assert.match(await page.locator('#simple-home-purpose').innerText(),/private Wi-Fi supplies Internet.*phone to the hotspot/s);
  assert.match(await page.locator('#connection-mode-purpose').innerText(),/Connect your phone.*hotspot.*Internet.*private Wi-Fi/s);
  await page.waitForTimeout(5500);assert.equal(await page.locator('#connection-mode').inputValue(),'hotspot_shared');
  await page.locator('[data-help="connection-mode-info"]').click();
  assert.match(await page.locator('#connection-mode-info').innerText(),/separate available adapters/);await page.waitForTimeout(5500);
  assert.equal(await page.locator('#connection-mode-info').evaluate(e=>e.open),true);await page.keyboard.press('Escape');
 });
 await check('save isolated then shared mode through actual guarded config endpoint without changing home intent',async()=>{
  await page.locator('#connection-mode').selectOption('hotspot_isolated');await page.locator('#connection-mode-save').click();
  await page.waitForFunction(()=>state.config.connection_mode==='hotspot_isolated');
  assert.equal(await page.locator('#connection-mode').inputValue(),'hotspot_isolated');
  assert.equal(await page.locator('#optional-hotspot').isVisible(),false);
  assert.match(await page.locator('#simple-home-purpose').innerText(),/does not provide Internet/);
  await page.locator('#connection-mode').selectOption('hotspot_shared');await page.locator('#connection-mode-save').click();
  await page.waitForFunction(()=>state.config.connection_mode==='hotspot_shared');
  assert.equal(await page.evaluate(()=>state.config.hotspot_private_lan),false);
  assert.equal(await page.evaluate(()=>state.network_access.internet_verified),false);
  await page.locator('#connection-mode-card details summary').click();await page.locator('#hotspot-private-lan').check();
  await page.waitForTimeout(5500);assert.equal(await page.locator('#hotspot-private-lan').isChecked(),true);
 });
 await check('hotspot rename draft survives polling and saving preserves mode and other hotspot settings',async()=>{
  await page.locator('#hotspot-name').fill('Mon observatoire');
  await page.waitForTimeout(5500);assert.equal(await page.locator('#hotspot-name').inputValue(),'Mon observatoire');
  await page.locator('#hotspot-name-form button').click();
  await page.waitForFunction(()=>state.config.hotspot.ssid==='Mon observatoire');
  assert.equal(await page.evaluate(()=>state.config.connection_mode),'hotspot_shared');
  await page.locator('#connection-mode').selectOption('private');
  assert.equal(await page.locator('#optional-hotspot').isVisible(),true);
  assert.match(await page.locator('#simple-home-purpose').innerText(),/use your telescope apps on that network/);
  await page.locator('#connection-mode').selectOption('hotspot_shared');
  await page.locator('#hotspot-name-form').screenshot({path:resolve(output,'hotspot-name-375.png')});
 });
 await check('hotspot adapter choice survives polling, saves by MAC and excludes occupied radios',async()=>{
  await page.evaluate(async()=>{const data=configPayload();data.home={mac:state.devices[1].mac,ssid:'Demo home network',open:true};await save(data);});
  const radios=await page.evaluate(()=>({free:state.devices.filter(d=>d.ap!==false&&d.mac!==state.config.home.mac&&!state.config.telescopes.some(p=>p.enabled&&p.mac===d.mac)),home:state.config.home.mac,profile:state.config.telescopes.find(p=>p.enabled)?.mac}));
  const target=radios.free.at(-1).mac;
  assert.equal(await page.locator(`#hotspot-interface option[value="${radios.home}"]`).isDisabled(),true);
  if(radios.profile)assert.equal(await page.locator(`#hotspot-interface option[value="${radios.profile}"]`).isDisabled(),true);
  await page.locator('#hotspot-interface').selectOption(target);
  await page.waitForTimeout(5500);
  assert.equal(await page.locator('#hotspot-interface').inputValue(),target);
  await page.locator('#hotspot-name-form button').click();
  await page.waitForFunction(mac=>state.config.hotspot.mac===mac&&state.hotspot.mac===mac,target);
  assert.equal(await page.locator('#hotspot-name').inputValue(),'Mon observatoire');
  await page.locator('#hotspot-name-form').screenshot({path:resolve(output,'hotspot-adapter-375.png')});
 });
 await check('traffic vocabulary reflects content versus background, preserving protected unknown',async()=>{
  const values=await page.evaluate(()=>{
   const p={id:'synthetic',auto_eligible:true,auto_replaceable:true},s={config:{auto_connect:true,auto_policy:{}},connection_activity:{supported:true,profiles:{synthetic:{state:'active',sample_age_seconds:0,consultation:'probable_content'}}}};
   const content=connectionGuard(p,s);s.connection_activity.profiles.synthetic.consultation='background_control';s.connection_activity.profiles.synthetic.state='quiet';s.connection_activity.profiles.synthetic.quiet_seconds=120;s.connection_activity.profiles.synthetic.cooldown_remaining=0;
   const background=connectionGuard(p,s);s.connection_activity.profiles.synthetic.state='unknown';s.connection_activity.profiles.synthetic.consultation='unknown';return{content,background,unknown:connectionGuard(p,s)};
  });
  assert.equal(values.content.activity,'Possible content transfer');assert.equal(values.content.state,'active');assert.equal(values.background.activity,'Background status exchanges');assert.equal(values.background.state,'eligible');assert.equal(values.unknown.state,'unknown');
 });
 await check('ten locales fit at 320px and do not erase the mode draft',async()=>{
  await page.setViewportSize({width:320,height:900});
  for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){
   await clickUserControl(page, '#locale-menu summary');await page.locator(`button[data-locale="${locale}"]`).click();
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.equal(await page.locator('#connection-mode').inputValue(),'hotspot_shared');
  }
  await page.locator('#connection-mode-card').screenshot({path:resolve(output,'network-modes-320.png')});
 });
 await check('hotspot adapter options repopulate after logout and login',async()=>{
  const mac=await page.locator('#hotspot-interface').inputValue();
  await clickUserControl(page, '#logout');await page.locator('#login').waitFor({state:'visible'});
  await page.locator('#login-password').fill('demo-telescope-2026');await page.locator('#login-form button').click();
  await page.locator('#dashboard').waitFor({state:'visible'});await page.locator('#nav-networks').click();
  assert.equal(await page.locator('#hotspot-interface').inputValue(),mac);
  assert.ok(await page.locator('#hotspot-interface option').count()>1);
 });
 assert.deepEqual(errors,[]);await writeFile(resolve(output,'network-modes.json'),JSON.stringify({results,errors},null,2));
}finally{if(browser)await browser.close();server.kill('SIGTERM');await Promise.race([once(server,'exit'),new Promise(r=>setTimeout(r,5000))]);if(server.exitCode===null)server.kill('SIGKILL');await rm(folder,{recursive:true,force:true});}
