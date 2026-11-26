/** One bridge password in the web interface: banner, card, SSH line, change and sign-in again (demo server). */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,mkdir,readFile,readdir,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve,join} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createServer} from 'node:net';
const root=fileURLToPath(new URL('../',import.meta.url));
const {chromium}=await import(process.env.PLAYWRIGHT_MODULE?pathToFileURL(process.env.PLAYWRIGHT_MODULE).href:'playwright');
const folder=await mkdtemp(resolve(tmpdir(),'astro-password-ui-')),output=resolve(root,process.env.UI_ARTIFACTS||'artifacts/password');await mkdir(output,{recursive:true});
const socket=createServer();socket.listen(0,'127.0.0.1');await once(socket,'listening');const port=socket.address().port;await new Promise(r=>socket.close(r));
const server=spawn('python3',['-u','-m','wifi_bridge','--demo','--demo-radios','4','--state-dir',folder,'--port',String(port)],{cwd:root,stdio:['ignore','pipe','pipe']});
let logs='',browser;const errors=[],results=[];server.stdout.on('data',v=>logs+=v);server.stderr.on('data',v=>logs+=v);
const NEW='Synthetic-bridge-77';
try{
 for(let n=0;n<150&&!logs.includes('SIMULATION');n++)await new Promise(r=>setTimeout(r,100));
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}});
 const page=await browser.newPage({viewport:{width:375,height:960}});page.setDefaultTimeout(10000);page.on('pageerror',e=>errors.push(e.message));
 const login=async password=>{await page.locator('#login-password').fill(password);await page.locator('#login-form button').click();await page.locator('#dashboard').waitFor({state:'visible'});};
 const general=async()=>{await page.locator('#nav-configuration').click();await page.locator('#tab-general').click();};
 await page.goto(`http://127.0.0.1:${port}`);await login('12345678');await general();
 assert.equal(await page.locator('#default-warning').innerText(),'The default password 12345678 is still in use. Choose your own below.');
 assert.equal(await page.locator('#credential-target').count(),0);
 assert.equal(await page.locator('#ssh-state').innerText(),'SSH: closed while the default password is in use.');
 assert.equal(await page.locator('label[for=current-password]').innerText(),'Current password');
 for(const locale of ['fr','en']){await page.evaluate(l=>BridgeI18n.setLocale(l),locale);await page.locator('#credentials-form').screenshot({path:resolve(output,`default-${locale}-375.png`)});}
 results.push('Default password: banner names it once, single card without account choice, SSH closed line');
 const change=async(current,password)=>{await page.locator('#current-password').fill(current);await page.locator('#new-password').fill(password);await page.locator('#confirm-password').fill(password);await page.locator('#credentials-form button').click();};
 await change('12345678','accentué-pass');await page.waitForFunction(()=>document.getElementById('credentials-message').textContent.startsWith('The password must have 8 to 63 characters'));
 await change('12345678',NEW);await page.locator('#login').waitFor({state:'visible'});
 assert.ok((await page.locator('body').innerText()).includes('Password changed. Join the hotspot again with the new password, then sign in.'));
 await page.locator('#login-password').fill('12345678');await page.locator('#login-form button').click();await page.waitForTimeout(500);assert.equal(await page.locator('#dashboard').isVisible(),false);
 await login(NEW);await general();
 assert.equal(await page.locator('#default-warning').isVisible(),false);
 assert.equal(await page.locator('#ssh-state').innerText(),'SSH: open, account root, with this password.');
 for(const locale of ['fr','en']){await page.evaluate(l=>BridgeI18n.setLocale(l),locale);await page.locator('#credentials-form').screenshot({path:resolve(output,`changed-${locale}-375.png`)});}
 // Nothing on disk holds the new password in clear (the demo state folder).
 const walk=async dir=>(await Promise.all((await readdir(dir,{withFileTypes:true})).map(e=>e.isDirectory()?walk(join(dir,e.name)):[join(dir,e.name)]))).flat();
 for(const file of await walk(folder))assert.ok(!(await readFile(file)).includes(Buffer.from(NEW)),file);
 assert.ok(!logs.includes(NEW));
 results.push('One change: rule refused before any change, sessions end, old password refused, banner gone, SSH open line, no clear password on disk or in the log');
 for(const width of [320,375,1280]){await page.setViewportSize({width,height:960});for(const locale of ['en','fr','ja','de','es','it','pt-BR','zh-Hans','zh-Hant','ru']){await page.evaluate(l=>BridgeI18n.setLocale(l),locale);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,locale+' '+width);if(locale!=='en')assert.notEqual(await page.locator('#ssh-state').innerText(),'SSH: open, account root, with this password.',locale);}}
 results.push('Ten locales at 320, 375 and 1280 px without horizontal overflow');
 assert.deepEqual(errors,[]);
 for(const line of results)console.log('PASS '+line);
}catch(error){console.error(logs.slice(-4000));throw error;}
finally{await browser?.close();server.kill();await once(server,'exit').catch(()=>{});await rm(folder,{recursive:true,force:true});}
