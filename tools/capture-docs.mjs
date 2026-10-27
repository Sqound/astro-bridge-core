/** Capture the real demo UI with synthetic data; no hardware or saved credentials. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,mkdir,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createServer} from 'node:net';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const folder=await mkdtemp(resolve(tmpdir(),'astro-docs-')),output=resolve(root,'doc/images');await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const runner=resolve(folder,'run.py');
await writeFile(runner,`import sys\nfrom pathlib import Path\nsys.path.insert(0,${JSON.stringify(root)})\nfrom wifi_bridge.network import DemoNetwork\nfrom wifi_bridge.config import Store,validate_config\nstate=Path(${JSON.stringify(resolve(folder,'state'))})\nnet=DemoNetwork(4);devices=net.devices();store=Store(state,devices[0]['mac'])\nc=dict(store.config,hostname='astro-bridge',country='US',admin_password='demo-telescope-2026',hotspot_password='demo-hotspot-password',connection_mode='hotspot_shared',auto_connect=True,home={'ssid':'HomeLab','password':'demo-home-password','mac':devices[1]['mac']},hotspot={'enabled':True,'ssid':'AstroBridge-Demo','mac':devices[2]['mac']})\nc['telescopes']=[dict(id=ident,name=name,kind=kind,ssid=ssid,password='demo-instrument-password',mac=devices[0]['mac'] if enabled else '',enabled=enabled,target_ip=ip,auto_eligible=True,auto_replaceable=True,owner_locked=False) for ident,name,kind,ssid,ip,enabled in [('vespera','Vespera','vespera','vespera-DEMO001','10.0.0.1',True),('seestar','Seestar','seestar','s50 pro_DEMO','10.0.0.1',False),('dwarf','DWARF mini','dwarf','DWARF_mini_SAVED','192.168.88.1',False),('zwo-mount','ZWO mount','zwo_mount','AMH_DEMO','192.168.4.1',False)]]\nstore.save(validate_config(c,store.config,devices))\nDemoNetwork.scan=lambda self,name,**kwargs:[{'ssid':ssid,'bssid':bssid,'signal':signal,'frequency':2412,'security':'WPA2'} for ssid,bssid,signal in [('vespera-DEMO001','02:00:00:00:10:01',91),('SynScan_DEMO','02:00:00:00:20:01',85),('DWARF_mini_NEW001','02:00:00:00:30:01',78)]]\nfrom wifi_bridge.__main__ import main\nmain()\n`);
const server=spawn(process.env.PYTHON||'python3',[runner,'--demo','--demo-radios','4','--state-dir',resolve(folder,'state'),'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;server.stdout.on('data',d=>logs+=d);server.stderr.on('data',d=>logs+=d);const files=[];
try{
 for(let i=0;!logs.includes('SIMULATION');i++){if(i>120)throw new Error(logs);await new Promise(r=>setTimeout(r,100));}
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const context=await browser.newContext({viewport:{width:1280,height:920},deviceScaleFactor:1,colorScheme:'dark'});
 const page=await context.newPage();page.setDefaultTimeout(15000);const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${port}`);await page.locator('#login-password').fill('demo-telescope-2026');await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});
 await page.waitForFunction(()=>state.telescopes.some(p=>p.connected)&&state.hotspot.active&&state.home_ip);
 await page.evaluate(async()=>{await api('/api/protection',{action:'owner_lock',id:'vespera',locked:true,current_password:'demo-telescope-2026',config_revision:state.config_revision});await refresh();});
 const shot=async(name,selector)=>{await page.evaluate(()=>document.fonts.ready);await page.waitForTimeout(200);const path=resolve(output,name);await (selector?page.locator(selector):page).screenshot({path,animations:'disabled'});files.push(name);};
 await page.evaluate(()=>localStorage.setItem('smart-telescope.theme','dark'));await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});await shot('dashboard-dark.png','#view-telescopes');await shot('connections.png','#simple-connections');
 await page.evaluate(()=>localStorage.setItem('smart-telescope.theme','dark'));await page.reload();await page.locator('#dashboard').waitFor({state:'visible'});await page.setViewportSize({width:390,height:1040});await shot('dashboard-mobile.png');
 await page.setViewportSize({width:1100,height:950});await page.locator('#nav-networks').click();await shot('networks.png','#view-networks');
 await page.locator('#nav-configuration').click();await page.locator('#tab-plugins').click();await shot('plugins.png');
 await page.locator('#plugin-zwo_mount-info').click();await shot('plugin-details.png','#plugin-info');await page.keyboard.press('Escape');
 await page.locator('#tab-telescopes').click();await shot('saved-equipment.png');await page.locator('#add-scope').click();await page.waitForFunction(()=>document.querySelectorAll('#discovery-results [data-observation]').length>0||document.querySelectorAll('#discovery-results button').length>0);await shot('scan.png','#discovery-dialog');
 const add=page.locator('#discovery-results').getByRole('button',{name:'Add telescope',exact:true}).last();await add.click();await page.locator('#editor').waitFor({state:'visible'});await shot('add-equipment.png','#editor');await page.locator('#close-editor').click();
 await page.locator('#tab-general').click();await page.locator('#auto-advanced>summary').click();await shot('switching-settings.png','#auto-advanced');await page.locator('#auto-advanced>summary').click();
 await page.locator('#tab-pwa').click();await shot('pwa-notifications.png');// Synthetic certificate metadata exposes the real iOS guide; no HTTPS service/trust is created.
 await page.evaluate(()=>{BridgeSecure.suspend();BridgeSecure.configure({api:async()=>({supported:true,identity:{configured:true,enabled:true,state:'valid',hostname:'astro-bridge.local',fingerprint_sha256:'0'.repeat(64)},listener:{ready:true,url:'https://astro-bridge.local'},diagnostics_stale:false,diagnostics_age_seconds:0}),httpUrl:()=>location.origin});BridgeSecure.start('documentation-fixture');});
 await page.locator('#setup-os').selectOption('ios');await page.locator('[data-setup-os="ios"]').waitFor({state:'visible'});await shot('pwa-setup.png','[data-setup-os="ios"]');
 assert.deepEqual(errors,[]);assert.equal(await page.locator('html').getAttribute('lang'),'en');
 console.log(JSON.stringify({simulation:true,synthetic_data_only:true,files,page_errors:errors}));
}finally{if(browser)await browser.close();if(server.exitCode===null&&server.signalCode===null){const ended=once(server,'exit');server.kill('SIGTERM');await ended;}await rm(folder,{recursive:true,force:true});}
