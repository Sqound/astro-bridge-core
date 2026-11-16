import assert from "node:assert/strict";
import test from "node:test";
import { AstroBridgeClient, AstroBridgeError } from "../examples/astro-bridge-client.mjs";

test("public info returns native identity without sending a remembered token", async () => {
  const identity = {product:"astro-bridge",contract_version:1,device_id:"0123456789abcdef",
    name:"Test bridge",capabilities:["pairing"],legacy_capabilities:["telescope_profiles"],
    request_path:"hotspot",pairing:{available:true,method:"admin_password"}};
  const client = new AstroBridgeClient("http://bridge.local", {token:"test-token-1",fetch:async(url,options)=>{
    assert.equal(url,"http://bridge.local/api/v1/info");
    assert.equal(options.method,"GET");
    assert.equal(options.headers.Authorization,undefined);
    assert.equal(options.body,undefined);
    return response(identity);
  }});
  assert.deepEqual(await client.info(),identity);
});

test("endpoint inventory preserves paths and encodes optional profile without writes", async () => {
  const calls = [], inventory = {config_revision:"revision",boot_id:"0".repeat(32),endpoints:[{
    profile_id:"mount",kind:"synscan",target:{ip:"192.168.4.1",ports:[11880]},paths:[{
      path:"hotspot",host:"192.168.77.1",port:21180,transport:"udp",service:"synscan",
      ready:true,service_state:"not_answering",requires_client_route:false,owner:null,refused_clients:7,
    }],
  }]};
  const client = new AstroBridgeClient("http://bridge.local", {token:"test",fetch:async(url,options)=>{
    calls.push({url,...options});return response(inventory);
  }});
  assert.deepEqual(await client.endpoints(),inventory);
  assert.deepEqual(await client.endpoints("mount/one"),inventory);
  assert.equal(calls[0].url,"http://bridge.local/api/v1/endpoints");
  assert.equal(calls[1].url,"http://bridge.local/api/v1/endpoints/mount%2Fone");
  for (const call of calls) {
    assert.equal(call.method,"GET");assert.equal(call.body,undefined);
    assert.equal(call.headers.Authorization,"Bearer test");
  }
});

function response(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

test("instrument collection preserves24 profiles and8 cameras and bounds membership retries", async () => {
  const rows = Array.from({length:32},(_,i)=>({id:`instrument-${i}`}));
  const calls=[];
  const client=new AstroBridgeClient("http://bridge.local", {token:"test",fetch:async(url)=>{
    calls.push(url);
    const second=new URL(url).searchParams.has("after");
    return response({instruments:rows.slice(second?24:0,second?32:24),boot_id:"a".repeat(32),
      next_cursor:second?null:"opaque-cursor",has_more:!second});
  }});
  assert.deepEqual((await client.allInstruments()).instruments,rows);
  assert.equal(calls.length,2);
  assert.equal(calls[0],"http://bridge.local/api/v1/instruments");
  assert.equal(calls[1],"http://bridge.local/api/v1/instruments?after=opaque-cursor");
  let attempts=0;
  const changing=new AstroBridgeClient("http://bridge.local",{fetch:async()=>{
    attempts++;return response({error:{code:"CATALOG_CHANGED",message:"restart"}},409);
  }});
  await assert.rejects(changing.allInstruments(),error=>error.code==="CATALOG_CHANGED");
  assert.equal(attempts,3);
});

test("instrument collection restarts only for catalog changes and resets collected rows", async () => {
  let calls=0;
  const client=new AstroBridgeClient("http://bridge.local",{fetch:async()=>{
    calls++;
    if(calls===1)return response({instruments:[{id:"stale"}],boot_id:"a",has_more:true,next_cursor:"one"});
    if(calls===2)return response({error:{code:"CATALOG_CHANGED",message:"restart"}},409);
    return response({instruments:[{id:"current"}],boot_id:"a",has_more:false,next_cursor:null});
  }});
  assert.deepEqual((await client.allInstruments()).instruments,[{id:"current"}]);
  assert.equal(calls,3);
  let invalidCalls=0;
  const invalid=new AstroBridgeClient("http://bridge.local",{fetch:async()=>{
    invalidCalls++;return response({error:{code:"INVALID_CURSOR",message:"invalid"}},400);
  }});
  await assert.rejects(invalid.allInstruments(),error=>error.code==="INVALID_CURSOR");
  assert.equal(invalidCalls,1);
});

test("mobile login, encoded resource paths, conditional writes and logout", async () => {
  const calls = [];
  const client = new AstroBridgeClient("http://bridge.local", { fetch: async (url, options) => {
    calls.push({ url, ...options });
    return response(url.endsWith("/auth/token") && options.method === "POST" ? { access_token: "test-token" } : { ok: true });
  }});
  await client.login("test-password");
  await client.editTelescope("scope-one", { name: "Renamed" }, "revision");
  assert.equal(calls[0].headers.Authorization, undefined);
  assert.equal(calls[1].headers.Authorization, "Bearer test-token");
  assert.equal(calls[1].headers["If-Match"], '"revision"');
  assert.equal(calls[1].credentials, "omit");
  assert.deepEqual(JSON.parse(calls[1].body), { name: "Renamed" });
  await client.networks("wlan 1");
  assert.match(calls[2].url, /interface=wlan%201$/);
  await client.logout();
  await client.status();
  assert.equal(calls.at(-1).headers.Authorization, undefined);
});

test("structured errors and expired sessions are distinguishable", async () => {
  const calls = [];
  const client = new AstroBridgeClient("http://bridge.local", { token: "expired", fetch: async (url, options) => {
    calls.push(options);
    return response({ error: { code: "UNAUTHORIZED", message: "Expired" } }, 401);
  }});
  await assert.rejects(client.telescopes(), error => error instanceof AstroBridgeError && error.code === "UNAUTHORIZED" && error.status === 401);
  await assert.rejects(client.telescopes());
  assert.equal(calls[1].headers.Authorization, undefined);
});

test("password rotation clears local token and errors never echo request secrets", async () => {
  const calls = [];
  const client = new AstroBridgeClient("http://bridge.local", { token: "old", fetch: async (url, options) => {
    calls.push(options);
    return response({ ok: true, sessions_invalidated: true });
  }});
  await client.changePassword("web", "old-password", "new-password");
  await client.status();
  assert.equal(calls[1].headers.Authorization, undefined);
  const failed = new AstroBridgeClient("http://bridge.local", { fetch: async () => { throw new Error("secret transport data"); }});
  await assert.rejects(failed.login("private-password"), error => !error.message.includes("private-password") && !error.message.includes("secret transport"));
  assert.throws(() => new AstroBridgeClient("http://user:password@bridge.local"));
});

test("connect can atomically select a destination radio with source precondition", async () => {
  let request;
  const client = new AstroBridgeClient("http://bridge.local", { token:"test-token", fetch: async (url,options) => {
    request={url,...options};return response({ok:true});
  }});
  await client.connectTelescope("scope-one","revision",{mac:"02:00:00:00:00:02",expected_mac:"02:00:00:00:00:01"});
  assert.equal(request.headers["If-Match"], '"revision"');
  assert.deepEqual(JSON.parse(request.body), {mac:"02:00:00:00:00:02",expected_mac:"02:00:00:00:00:01"});
});


test("physical binding uses dedicated revision-checked paths and explicit discovery mode", async () => {
  const calls=[];
  const client=new AstroBridgeClient("http://bridge.local",{token:"test",fetch:async(url,options)=>{calls.push({url,...options});return response({ok:true});}});
  await client.discover("reachable");await client.binding("scope/a");await client.bind("scope-one",{candidate_id:"opaque"},"current");await client.unlink("scope-one","next");
  assert.equal(calls[0].url,"http://bridge.local/api/v1/discovery");assert.deepEqual(JSON.parse(calls[0].body),{mode:"reachable"});
  assert.ok(calls[1].url.endsWith("/scope%2Fa/binding"));assert.equal(calls[2].headers["If-Match"],'"current"');assert.equal(calls[3].method,"DELETE");assert.deepEqual(JSON.parse(calls[3].body),{});
});


test("ensure returns an operation and polls only its encoded authenticated resource",async()=>{
  const calls=[];const client=new AstroBridgeClient("http://bridge.local",{token:"test",fetch:async(url,options)=>{calls.push({url,...options});return response({id:"run.operation",state:"running"},202);}});
  await client.ensure("scope-one","actual-revision");await client.operation("run.operation");
  assert.ok(calls[0].url.endsWith("/scope-one/ensure"));assert.equal(calls[0].headers["If-Match"],'"actual-revision"');assert.deepEqual(JSON.parse(calls[0].body),{});assert.ok(calls[1].url.endsWith("/operations/run.operation"));assert.equal(calls[1].headers.Authorization,"Bearer test");
});


test("reservation versions, explicit owner release and owner locks use fixed native resources", async () => {
  const calls=[];const client=new AstroBridgeClient("http://bridge.local",{fetch:async(url,options)=>{calls.push({url,...options});return response({ok:true});}});
  await client.reserve("scope/one","revision",120);await client.renewReservation("opaque/id",2,300);await client.releaseLease("opaque/id",3);
  await client.ownerReleaseLease("opaque/id",4,"current-password","revision");await client.ownerLock("scope/one",true,"current-password","revision");
  assert.match(calls[0].url,/telescopes\/scope%2Fone\/leases$/);assert.equal(calls[0].headers["If-Match"],'"revision"');assert.equal(JSON.parse(calls[1].body).version,2);
  assert.equal(calls[2].method,"DELETE");assert.deepEqual(JSON.parse(calls[3].body),{version:4,current_password:"current-password",confirm_release:true});assert.match(calls[4].url,/owner-lock$/);
});

test("replacement policy acknowledgement and pause recovery use exact revision without retaining password",async()=>{
  const calls=[];const client=new AstroBridgeClient("http://bridge.local",{token:"test-token",fetch:async(url,options)=>{calls.push({url,...options});return response({ok:true});}});
  await client.editSettings({auto_policy:{protect_running_operations:true,force_first_profile:true,seestar_demand:true,vespera_demand:true,dwarf_demand:true},acknowledge_interruption:true},"current");
  assert.equal(calls[0].headers["If-Match"],'"current"');assert.equal(JSON.parse(calls[0].body).acknowledge_interruption,true);assert.equal(JSON.parse(calls[0].body).auto_policy.vespera_demand,true);assert.equal(JSON.parse(calls[0].body).auto_policy.dwarf_demand,true);
  assert.equal(JSON.parse(calls[0].body).auto_policy.protect_running_operations,true);
  await client.recoverConnectionPauses("temporary-password","new-current");
  assert.match(calls[1].url,/system\/connection-pauses\/recover$/);assert.equal(calls[1].headers["If-Match"],'"new-current"');
  assert.deepEqual(JSON.parse(calls[1].body),{current_password:"temporary-password",confirm_recovery:true});
  await client.status();assert.equal(calls[2].body,undefined);
});

test("real 307/308 redirects never forward credentials or retry login/writes", async () => {
  const { createServer } = await import("node:http");
  const { once } = await import("node:events");
  let destinationRequests = 0, originRequests = 0, sameOriginDestination = 0;
  const destination = createServer((request, reply) => { destinationRequests++; request.resume(); reply.end('{}'); });
  destination.listen(0,"127.0.0.1");await once(destination,"listening");
  let location, status;
  const origin = createServer((request, reply) => {
    originRequests++;request.resume();
    if(request.url==='/destination'){sameOriginDestination++;reply.setHeader('Content-Type','application/json');reply.end('{"ok":true}');return;}
    reply.writeHead(status,{Location:location});reply.end();
  });
  origin.listen(0,"127.0.0.1");await once(origin,"listening");
  try {
    for(status of [307,308])for(const remote of [false,true])for(const login of [true,false]){
      location=remote?`http://127.0.0.1:${destination.address().port}/destination`:'/destination';
      const client=new AstroBridgeClient(`http://127.0.0.1:${origin.address().port}`,{token:'synthetic-token',timeoutMs:2000});
      const before=originRequests;
      await assert.rejects(login?client.login('synthetic-password'):client.editTelescope('synthetic-profile',{name:'Synthetic name'},'revision'),
        error=>error instanceof AstroBridgeError&&error.code==='NETWORK_ERROR'&&!/synthetic-(?:password|token)/.test(error.message));
      assert.equal(originRequests,before+1,'The request must not be retried or followed');
      assert.equal(destinationRequests,0);assert.equal(sameOriginDestination,0);
    }
  } finally {
    origin.closeAllConnections();destination.closeAllConnections();
    await Promise.all([new Promise(resolve=>origin.close(resolve)),new Promise(resolve=>destination.close(resolve))]);
  }
});


test("plugin catalog preserves optional observation descriptors without activating profiles", async () => {
  const catalog = { schema_version: 1, plugins: [{ id: "seestar",
    telemetry: { adapters: ["link_v1", { id: "seestar_events_v1", requires: "maker_key" }], default_enabled: [] },
    maker_key: { family: "seestar", algorithm: "rsa-pkcs1v15-sha1", sources: ["maker_package", "import"] },
  }] };
  const calls = [];
  const client = new AstroBridgeClient("http://bridge.local", { fetch: async (url, options) => {
    calls.push({ url, ...options }); return response(catalog);
  }});
  assert.deepEqual(await client.plugins(), catalog);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://bridge.local/api/v1/plugins");
  assert.equal(calls[0].method, "GET");
  assert.equal(calls[0].body, undefined);
});

test("status preserves service readiness independently of forwarding readiness", async () => {
  const service = { state: "not_answering", checked_at_seq: 7, method: "tcp_connect", failures: 2 };
  const client = new AstroBridgeClient("http://bridge.local", { fetch: async () => response({
    telescopes: [{ id: "mount", connected: true, relay_ready: true, service }],
  }) });
  const result = await client.status();
  assert.equal(result.telescopes[0].connected, true);
  assert.equal(result.telescopes[0].relay_ready, true);
  assert.deepEqual(result.telescopes[0].service, service);
});


test("UDP owner release encodes the profile id and sends an actual revision with no control payload", async () => {
  let call;
  const client = new AstroBridgeClient("http://bridge.local", {token:"test",fetch:async(url,options)=>{
    call={url,...options};return response({ok:true,owner:null,refused_clients:2});
  }});
  const result = await client.releaseEndpointOwner("mount/one", "current");
  assert.equal(call.url,"http://bridge.local/api/v1/endpoints/mount%2Fone/release-owner");
  assert.equal(call.method,"POST");assert.equal(call.headers.Authorization,"Bearer test");
  assert.equal(call.headers["If-Match"],'"current"');assert.deepEqual(JSON.parse(call.body),{});
  assert.equal(result.owner,null);assert.equal(result.refused_clients,2);
});

test("short event polling preserves hotspot reasons and uses no planned wait parameter", async () => {
  let request;
  const page={events:[{type:"hotspot_unavailable",profile_id:"hotspot",profile_name:"",reason:"HOTSPOT_START_FAILED",ready:false},
    {type:"hotspot_restored",profile_id:"hotspot",profile_name:"",reason:"HOTSPOT_RESTORED",ready:true}],next_cursor:12};
  const client=new AstroBridgeClient("http://bridge.local",{token:"test",fetch:async(url,options)=>{
    request={url,...options};return response(page);
  }});
  assert.deepEqual(await client.events({after:10,bootId:"0".repeat(32)}),page);
  const url=new URL(request.url);
  assert.equal(url.pathname,"/api/v1/events");assert.equal(url.searchParams.get("after"),"10");
  assert.equal(url.searchParams.get("boot_id"),"0".repeat(32));assert.equal(url.searchParams.get("wait"),null);
  assert.equal(request.method,"GET");assert.equal(request.body,undefined);
});


test("paired client inventory and conditional revocation never send a secret", async () => {
  const calls=[];
  const client=new AstroBridgeClient("http://bridge.local",{token:"test-token",fetch:async(url,options)=>{
    calls.push({url,...options});return response({ok:true});
  }});
  await client.clients();await client.revokeClient("client/one","current-revision");
  assert.equal(calls[0].url,"http://bridge.local/api/v1/auth/clients");
  assert.equal(calls[0].method,"GET");
  assert.equal(calls[1].url,"http://bridge.local/api/v1/auth/clients/client%2Fone");
  assert.equal(calls[1].method,"DELETE");
  assert.equal(calls[1].headers["If-Match"],'"current-revision"');
  for(const call of calls)assert.equal(call.body,undefined);
});

test('time helpers preserve UTC hints and need only the existing token', async () => {
  const calls = [];
  const client = new AstroBridgeClient('http://bridge.test', {fetch: async (url, options) => {
    calls.push({url, ...options});
    return new Response(JSON.stringify({source: 'client_hint'}), {status: 200});
  }});
  client.token = 'test-token';
  await client.systemTime();
  await client.timeHint('2026-11-04T00:00:00Z', 500);
  assert.ok(calls[0].url.endsWith('/system/time'));
  assert.equal(calls[0].method, 'GET');
  assert.ok(calls[1].url.endsWith('/system/time-hint'));
  assert.equal(calls[1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[1].body), {utc_iso: '2026-11-04T00:00:00Z', uncertainty_ms: 500});
});


test("system metrics uses the authenticated read-only administration route", async () => {
  const metrics = {sample_sequence: 3, relay_coverage: "userspace_only"};
  const client = new AstroBridgeClient("http://bridge.local", {token: "test", fetch: async (url, options) => {
    assert.equal(url, "http://bridge.local/api/v1/system/metrics");
    assert.equal(options.method, "GET");
    assert.equal(options.headers.Authorization, "Bearer test");
    assert.equal(options.body, undefined);
    return response(metrics);
  }});
  assert.deepEqual(await client.systemMetrics(), metrics);
});


test("event waits retain empty timeout cursors", async () => {
  const calls=[];
  const body={events:[],next_cursor:42,latest_sequence:42,has_more:false,reset_reason:null};
  const client=new AstroBridgeClient("http://bridge.test",{token:"test-token-1",fetch:async(url,options)=>{
    calls.push(url);return {ok:true,status:200,json:async()=>body};
  }});
  assert.deepEqual(await client.waitEvents({after:42,bootId:"0".repeat(32),wait:25}),body);
  const query=new URL(calls[0]).searchParams;
  assert.equal(query.get("wait"),"25");assert.equal(query.get("after"),"42");
});


test("telemetry helpers encode cursors and preserve streaming export responses", async () => {
  const calls = [];
  const streamed = { ok: true, body: { marker: "stream" }, json() { throw Error("must stream"); } };
  const client = new AstroBridgeClient("http://bridge.local", {token:"test-token-1",fetch:async(url,options)=>{
    calls.push([url,options]);
    return url.includes("/export?") ? streamed : response({ samples: [], events: [], next_cursor: 2 });
  }});
  await client.telemetry();
  await client.sessions({profileId:"test profile",after:2,limit:10});
  await client.samples({sessionId:"test/session",after:3,bootId:"0".repeat(32),limit:500});
  assert.equal(await client.exportSession("test/session","csv"),streamed);
  assert.equal(calls[0][0],"http://bridge.local/api/v1/telemetry");
  assert.match(calls[1][0],/profile_id=test\+profile/);
  assert.match(calls[2][0],/session_id=test%2Fsession/);
  assert.match(calls[3][0],/test%2Fsession\/export\?format=csv/);
  for (const [,options] of calls) { assert.equal(options.method,"GET"); assert.equal(options.headers.Authorization,"Bearer test-token-1"); }
});


test("telemetry samples preserve link deltas, protection and activity values", async () => {
  const samples = [
    {metric:"link.bytes_to", value:120},
    {metric:"link.protected", value:true},
    {metric:"link.activity", value:"background_control"},
  ];
  const page = {samples, events:[], next_cursor:3, has_more:false, gap:false, reset_reason:null};
  const client = new AstroBridgeClient("http://bridge.local", {fetch:async()=>response(page)});
  assert.deepEqual(await client.samples({sessionId:"synthetic"}),page);
});

test("camera inventory and owner settings use separate bridge routes and revisions", async () => {
  const calls = [];
  const client = new AstroBridgeClient("http://bridge.local", {token:"test",fetch:async(url,options)=>{
    calls.push({url,...options});return response([]);
  }});
  assert.deepEqual(await client.cameras(), []);
  await client.cameraSettings();
  await client.editCameraSettings({serve:false,indi:true,port:12000,allowed_subnets:["192.0.2.0/24"]}, "camera-revision");
  await client.releaseCameraOwner("cam-one", "a".repeat(32), "release-revision");
  await client.plugDemoCamera(true, "demo-revision");
  assert.deepEqual(calls.map(call => [call.method,call.url]), [
    ["GET","http://bridge.local/api/v1/cameras"],
    ["GET","http://bridge.local/api/v1/cameras/settings"],
    ["PATCH","http://bridge.local/api/v1/cameras/settings"],
    ["POST","http://bridge.local/api/v1/cameras/cam-one/release-owner"],
    ["POST","http://bridge.local/api/v1/cameras/demo"],
  ]);
  assert.equal(calls[3].headers["If-Match"], '"release-revision"');
  assert.deepEqual(JSON.parse(calls[3].body), {confirm_release:true,owner_generation:"a".repeat(32)});
  assert.equal(calls[4].headers["If-Match"], '"demo-revision"');
  assert.deepEqual(JSON.parse(calls[4].body), {plugged:true});
  assert.equal(calls[2].headers["If-Match"], '"camera-revision"');
  assert.equal(calls[0].headers.Authorization, "Bearer test");
  assert.deepEqual(JSON.parse(calls[2].body), {serve:false,indi:true,port:12000,allowed_subnets:["192.0.2.0/24"]});
});

test("Alpaca owner cadence settings keep revision and normalized values", async () => {
  const calls = [];
  const settings = {profiles:{one:{adapters:["alpaca_observer_v1"],alpaca_interval_seconds:10}}};
  const client = new AstroBridgeClient("http://bridge.local", {token:"test",fetch:async(url,options)=>{
    calls.push({url,...options}); return response(settings);
  }});
  assert.deepEqual(await client.updateTelemetrySettings(settings,"revision-1"),settings);
  assert.equal(calls[0].url,"http://bridge.local/api/v1/telemetry/settings");
  assert.equal(calls[0].method,"PATCH");
  assert.equal(calls[0].headers["If-Match"],'"revision-1"');
  assert.deepEqual(JSON.parse(calls[0].body),settings);
});

test("maker keys expose admin metadata, write-only import and revision-guarded deletion", async () => {
  const calls=[];
  const client=new AstroBridgeClient("http://bridge.local",{token:"test",fetch:async(url,options)=>{
    calls.push({url,...options});return response({keys:[]});
  }});
  await client.makerKeys();await client.fetchMakerKey("seestar");
  await client.importMakerKey("seestar","deliberately unusable test input");
  await client.removeMakerKey("seestar","revision");await client.makerKeyOperation("operation");
  assert.deepEqual(calls.map(call=>call.method),["GET","POST","PUT","DELETE","GET"]);
  assert.equal(calls[3].headers["If-Match"],'"revision"');
  assert.equal(calls[1].url,"http://bridge.local/api/v1/maker-keys/seestar/fetch");
  assert.deepEqual(JSON.parse(calls[2].body),{pem:"deliberately unusable test input"});
});

test("Seestar listener choice preserves other adapters and revision", async () => {
 const calls=[];const client=new AstroBridgeClient("http://bridge.local",{fetch:async(url,init)=>{calls.push({url,init});return new Response(init.body,{status:200,headers:{"Content-Type":"application/json"}});}});
 const settings={profiles:{scope:{adapters:["link_v1","seestar_events_v1"],alpaca_interval_seconds:20}}};
 assert.deepEqual(await client.updateTelemetrySettings(settings,"session-revision"),settings);
 assert.equal(calls[0].init.headers["If-Match"],'"session-revision"');
});

test("telemetry adapter diagnostics preserve bounded normalized counters", async () => {
 const expected={adapter_status:{scope:[{adapter_id:"seestar_events_v1",adapter_state:"yielded",retry_after:600,unavailable_reason:null,event_counts_minute:{Stack:4,unknown:2},handshakes:{verified:4,refused:0,unverified:0},connection_closes:4,connected_seconds:0,last_connection_seconds:15}]}};
 const client=new AstroBridgeClient("http://bridge.local",{fetch:async()=>new Response(JSON.stringify(expected),{status:200,headers:{"Content-Type":"application/json"}})});
 assert.deepEqual(await client.telemetry(),expected);
});

test("session card receipts retain source clocks and partial history", async()=>{
 const expected={card_data:{scope:{readings:{"stack.frames_accepted":{value:142,utc:"2026-11-04T21:00:00+00:00",since:"2026-11-04T21:00:00+00:00",adapter_id:"vespera_status_v1",cadence_seconds:1.5,age_seconds:5,stale:true}},recent:[],tonight:[],history_partial:true}}};
 const client=new AstroBridgeClient("http://bridge.local",{fetch:async()=>new Response(JSON.stringify(expected),{status:200,headers:{"Content-Type":"application/json"}})});
 assert.deepEqual(await client.telemetry(),expected);
});


test("session analysis keeps bounded coverage and incomplete exposure semantics", async () => {
 const calls=[],value={limited:false,reader_coverage:{unknown_before_seq:null,fetched:[]},exposure:{observed_seconds:20,observed_frames:2,complete:false,latest_seconds:10}};
 const client=new AstroBridgeClient("http://bridge.local",{fetch:async(url,options)=>{calls.push({url,options});return response(value);}});
 assert.deepEqual(await client.sessionAnalysis("scope/test"),value);
 assert.equal(calls[0].url,"http://bridge.local/api/v1/telemetry/sessions/scope%2Ftest/analysis");
 assert.equal(calls[0].options.method,"GET");
});
