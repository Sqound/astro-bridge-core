/** Exercise a rejected Wi-Fi password and its correction through the real mobile UI. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,mkdir,readFile,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createServer} from 'node:net';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const folder=await mkdtemp(resolve(tmpdir(),'astro-auth-')),output=resolve(root,'artifacts/wifi-auth');await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const counter=resolve(folder,'attempts.json'),runner=resolve(folder,'run.py');
await writeFile(runner,`import sys,json\nfrom pathlib import Path\nsys.path.insert(0,${JSON.stringify(root)})\nfrom wifi_bridge.network import DemoNetwork,NetworkAuthenticationError\nfrom wifi_bridge.config import Store,validate_config\nnet=DemoNetwork(3);devices=net.devices();store=Store(${JSON.stringify(resolve(folder,'state'))},devices[0]['mac'])\nc=dict(store.config,admin_password='demo-telescope-2026',hotspot_password='demo-hotspot-password',home={'ssid':'DemoHome','password':'demo-home-password','mac':devices[1]['mac']},hotspot={'enabled':True,'ssid':'DemoHotspot','mac':devices[2]['mac']})\nc['telescopes']=[dict(id='password-demo',name='Demo Telescope',kind='generic',ssid='Auth-DEMO',password='wrong-demo-password',target_ip='10.2.0.1',mac=devices[0]['mac'],enabled=True)]\nstore.save(validate_config(c,store.config,devices))\noriginal=DemoNetwork.connect\nattempts=0\ndef connect(self,role,device,config):\n global attempts\n if role.startswith('telescope'):\n  attempts+=1;Path(${JSON.stringify(counter)}).write_text(json.dumps(attempts))\n  if config['password']!='fixed-demo-password':raise NetworkAuthenticationError()\n return original(self,role,device,config)\nDemoNetwork.connect=connect\nfrom wifi_bridge.__main__ import main\nmain()\n`);
const server=spawn(process.env.PYTHON||'python3',['-u',runner,'--demo','--demo-radios','3','--state-dir',resolve(folder,'state'),'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;server.stdout.on('data',d=>logs+=d);server.stderr.on('data',d=>logs+=d);
try{
 for(let i=0;!logs.includes('SIMULATION');i++){if(i>150)throw new Error(logs);await new Promise(r=>setTimeout(r,100));}
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:950},colorScheme:'dark'});page.setDefaultTimeout(15000);
 await page.addInitScript(()=>localStorage.setItem('smart-telescope.theme','dark'));
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${port}`);await page.locator('#login-password').fill('demo-telescope-2026');await page.locator('#login-form button').click();
 await page.waitForFunction(()=>state?.telescopes.some(p=>p.id==='password-demo'&&p.retry_blocked));
 assert.match(await page.locator('#simple-connections').innerText(),/Wi-Fi authentication failed/);
 assert.doesNotMatch(await page.locator('#simple-connections').innerText(),/Connecting to Demo Telescope/);
 assert.equal(await page.locator('[id^="simple-disconnect-"][id$="-password-demo"]').count(),1);
 await page.waitForTimeout(5500);assert.equal(JSON.parse(await readFile(counter,'utf8')),1);
 await page.evaluate(()=>document.fonts.ready);await page.locator('#simple-connections').evaluate(el=>el.scrollIntoView({block:'center'}));await page.waitForTimeout(250);
 await page.screenshot({path:resolve(output,'password-rejected-page.png'),fullPage:true});
 await page.locator('#simple-connections').screenshot({path:resolve(output,'password-rejected.png')});
 await page.locator('[id^="simple-connect-"][id$="-password-demo"]').first().click();
 await page.locator('#editor').waitFor({state:'visible'});
 assert.equal(await page.locator('#network-password').evaluate(el=>el===document.activeElement),true);
 await page.locator('#network-password').fill('fixed-demo-password');await page.locator('#network-save').click();
 await page.waitForFunction(()=>state.telescopes.some(p=>p.id==='password-demo'&&p.connected&&p.relay_ready));
 assert.equal(JSON.parse(await readFile(counter,'utf8')),2);assert.deepEqual(errors,[]);
 await page.locator('#simple-connections').screenshot({path:resolve(output,'password-corrected.png')});
 console.log('PASS real mobile demo: explicit authentication message, stopped retries, password edit/focus and successful reconnect; zero browser errors');
}finally{
 if(browser)await browser.close();
 if(server.exitCode===null&&server.signalCode===null){const ended=once(server,'exit');server.kill('SIGTERM');await ended;}
 await rm(folder,{recursive:true,force:true});
}
