import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';
const source=readFileSync(new URL('../wifi_bridge/static/push.js',import.meta.url),'utf8');
const worker=readFileSync(new URL('../wifi_bridge/static/sw.js',import.meta.url),'utf8');
const turn=()=>new Promise(resolve=>setImmediate(resolve));
function client({secure=true,iphone=false,standalone=true}={}){
  const elements={},calls=[],requests=[],listeners={};
  const element=id=>elements[id]??=( {hidden:false,disabled:false,textContent:'',addEventListener(type,fn){this[type]=fn;}} );
  const sub={toJSON:()=>({endpoint:'synthetic'}),unsubscribe:async()=>calls.push('unsubscribe')};
  const registration={pushManager:{getSubscription:async()=>null,subscribe:async()=>{calls.push('subscribe');return sub;}}};
  const context={document:{getElementById:element,addEventListener(type,fn){listeners[type]=fn;}},
    navigator:{standalone,platform:'',maxTouchPoints:0,userAgent:iphone?'iPhone':'Test',serviceWorker:{ready:Promise.resolve(registration)}},
    matchMedia:()=>({matches:standalone}),BridgeI18n:{t:x=>x,locale:'fr'},Uint8Array,atob:x=>Buffer.from(x,'base64').toString('binary'),
    Notification:{permission:'default',requestPermission(){calls.push('permission');return Promise.resolve('granted');}}};
  context.window=context;context.isSecureContext=secure;context.PushManager=function(){};
  runInNewContext(source,context);
  context.BridgePush.configure({api:async(path,data)=>{calls.push(data?.action||'status');requests.push(data);return {available:true,public_key:'BA'+ 'A'.repeat(85),subscribed:false};}});
  return {context,elements,calls,requests,registration};
}

test('permission comes from an explicit click, before asynchronous subscription work',async()=>{
  const c=client();await c.context.BridgePush.start();assert.ok(!c.calls.includes('permission'));
  assert.equal(c.elements['push-enable'].disabled,false);
  c.calls.length=0;const pending=c.elements['push-enable'].click();
  assert.equal(c.calls[0],'permission');await pending;
  assert.deepEqual(c.calls,['permission','subscribe','subscribe']);
  assert.equal(c.elements['push-disable'].hidden,false);
  await c.elements['push-disable'].click();assert.ok(c.calls.includes('unsubscribe'));
});

test('insecure access and iPhone browser tab explain the missing prerequisite',async()=>{
  for(const options of [{secure:false},{iphone:true,standalone:false}]){
    const c=client(options);await c.context.BridgePush.start();
    assert.equal(c.elements['push-enable'].disabled,true);assert.ok(!c.calls.includes('prepare'));
    assert.ok(!c.calls.includes('permission'));
  }
});

test('logout during permission request cannot register a late subscription',async()=>{
  const c=client();await c.context.BridgePush.start();
  let resolve;c.context.Notification.requestPermission=()=>new Promise(r=>resolve=r);
  const pending=c.elements['push-enable'].click();c.context.BridgePush.reset();resolve('granted');await pending;
  assert.ok(!c.calls.includes('subscribe'));assert.equal(c.elements['push-enable'].disabled,true);
});

test('post-OTA diagnostics delay retries on fresh status without requesting permission',async()=>{
  const c=client();let attempts=0;
  c.context.BridgePush.configure({api:async(path,data)=>{
    if(data?.action==='prepare'&&++attempts===1)throw new Error('PUSH_HTTPS_REQUIRED');
    return {available:true,public_key:'BA'+'A'.repeat(85)};
  }});
  await c.context.BridgePush.start();await c.context.BridgePush.start();
  assert.equal(attempts,2);assert.equal(c.elements['push-enable'].disabled,false);
  assert.ok(!c.calls.includes('permission'));
});

test('new explicit subscriptions include chosen categories; existing choices do not silently expand',async()=>{
  const c=client();await c.context.BridgePush.start();
  for(const key of ['detected','connections','switching','hotspot','mode','updates','boot','failures'])assert.equal(c.elements['push-category-'+key].checked,true);
  c.elements['push-category-boot'].checked=false;
  await c.elements['push-enable'].click();
  const subscribe=c.requests.find(request=>request?.action==='subscribe');
  assert.equal(subscribe.categories.boot,false);assert.equal(subscribe.categories.switching,true);
  c.elements['push-category-detected'].checked=false;await c.elements['push-preferences'].click();
  assert.equal(c.requests.at(-1).action,'preferences');assert.equal(c.requests.at(-1).categories.detected,false);
  assert.equal(c.elements['push-feedback'].textContent,'Notification choices saved.');
  c.context.BridgePush.reset();assert.equal(c.elements['push-preferences'].disabled,true);
  const old=client();old.registration.pushManager.getSubscription=async()=>({toJSON:()=>({endpoint:'synthetic'})});
  old.context.BridgePush.configure({api:async(path,data)=>data?.action==='state'?{subscribed:true,categories:{connections:true,boot:false,switching:false,detected:false,hotspot:false,mode:false,updates:false,failures:false}}:{available:true,public_key:'BA'+'A'.repeat(85)}});
  await old.context.BridgePush.start();assert.equal(old.elements['push-category-connections'].checked,true);
  assert.equal(old.elements['push-category-switching'].checked,false);assert.equal(old.elements['push-category-boot'].checked,false);
});

test('worker always shows a neutral fallback for unusable pushes and strictly routes valid notices',async()=>{
  const events={},shown=[],opened=[],navigated=[],focused=[];
  let windows=[];
  const self={location:{origin:'https://bridge.test'},addEventListener(name,fn){events[name]=fn;},
    registration:{showNotification:async(title,options)=>shown.push({title,...options})},
    clients:{matchAll:async()=>windows,openWindow:async url=>opened.push(url)}};
  runInNewContext(worker,{REVISION:'synthetic',ASSETS:[],self,URL,Set,Date,JSON});
  let done;
  const push=async data=>{events.push({data:{text:()=>typeof data==='string'?data:JSON.stringify(data)},waitUntil:p=>done=p});await done;};
  const click=async data=>{events.notificationclick({notification:{close(){},data},waitUntil:p=>done=p});await done;};
  for(const path of ['/#configuration/telescopes?add=0123456789abcdef0123456789abcdef','/#telescopes?profile=scope-a','/#networks?section=hotspot','/#networks?section=connection-mode','/#configuration/general?section=updates']){
    await push({body:'Synthetic notice',expires:Date.now()/1000+30,path,category:'updates',tag:'astro-bridge-updates-0123456789abcdef'});
    assert.equal(shown.at(-1).data.path,path);await click(shown.at(-1).data);assert.equal(opened.at(-1),path);
  }
  const before=shown.length;
  const invalid=[null,'{broken','PRIVATE_OVERSIZE'.repeat(4096),
    {body:'PRIVATE STALE',expires:Date.now()/1000-1,path:'/#telescopes?profile=stale'},
    {body:'PRIVATE FUTURE',expires:Date.now()/1000+3600,path:'https://evil.test'}];
  for(const bad of invalid){
    await push(bad);const notice=shown.at(-1);
    assert.equal(notice.body,'Open Astro Bridge to check its current status.');
    assert.equal(notice.tag,'astro-bridge-status');assert.equal(notice.data.path,'/#telescopes');
    assert.ok(!JSON.stringify(notice).includes('PRIVATE'));await click(notice.data);assert.equal(opened.at(-1),'/#telescopes');
  }
  assert.equal(shown.length,before+invalid.length);
  events.push({data:null,waitUntil:p=>done=p});await done;
  assert.equal(shown.length,before+invalid.length+1);
  assert.equal(shown.at(-1).body,'Open Astro Bridge to check its current status.');
  await click(shown.at(-1).data);assert.equal(opened.at(-1),'/#telescopes');
  for(const path of ['/#configuration/telescopes?add=../private','/#configuration/telescopes?add=0123456789abcdef0123456789abcdef&secret=x','https://evil.test/#networks?section=hotspot','//evil.test/','/api/config','#networks?section=evil','/#telescopes?profile=../private','/#telescopes?profile=a&secret=x']){
    await push({body:'Synthetic',expires:Date.now()/1000+30,path});assert.equal(shown.at(-1).data.path,'/#telescopes');
  }
  windows=[{url:'https://unrelated.test/',navigate:async()=>assert.fail('Cross-origin client')},{url:'https://bridge.test/#networks',navigate:async path=>navigated.push(path),focus:async()=>focused.push(true)}];
  await click({path:'/#configuration/general?section=updates',expires:Date.now()/1000+30});
  assert.deepEqual(navigated,['/#configuration/general?section=updates']);assert.equal(focused.length,1);
  for(const path of ['/#networks?section=hotspot','/#networks?section=connection-mode','/#configuration/general?section=updates']){
    await push({body:'Synthetic current notice',path,expires:Date.now()/1000+30});
    await click({...shown.at(-1).data,expires:Date.now()/1000-60});assert.equal(navigated.at(-1),path);
  }
  await click({path:'https://evil.test/#configuration/general?section=updates',expires:Date.now()/1000-60});
  assert.equal(navigated.at(-1),'/#telescopes');
});
