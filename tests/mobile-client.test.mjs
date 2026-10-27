import assert from "node:assert/strict";
import test from "node:test";
import { AstroBridgeClient, AstroBridgeError } from "../examples/astro-bridge-client.mjs";

function response(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

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
  await client.connect("scope-one","revision",{mac:"02:00:00:00:00:02",expected_mac:"02:00:00:00:00:01"});
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
  await client.reserve("scope/one","revision",120);await client.renewLease("opaque/id",2,300);await client.releaseLease("opaque/id",3);
  await client.ownerReleaseLease("opaque/id",4,"current-password","revision");await client.ownerLock("scope/one",true,"current-password","revision");
  assert.match(calls[0].url,/telescopes\/scope%2Fone\/leases$/);assert.equal(calls[0].headers["If-Match"],'"revision"');assert.equal(JSON.parse(calls[1].body).version,2);
  assert.equal(calls[2].method,"DELETE");assert.deepEqual(JSON.parse(calls[3].body),{version:4,current_password:"current-password",confirm_release:true});assert.match(calls[4].url,/owner-lock$/);
});

test("replacement policy acknowledgement and pause recovery use exact revision without retaining password",async()=>{
  const calls=[];const client=new AstroBridgeClient("http://bridge.local",{token:"test-token",fetch:async(url,options)=>{calls.push({url,...options});return response({ok:true});}});
  await client.editSettings({auto_policy:{force_first_profile:true,seestar_demand:true,vespera_demand:true,dwarf_demand:true},acknowledge_interruption:true},"current");
  assert.equal(calls[0].headers["If-Match"],'"current"');assert.equal(JSON.parse(calls[0].body).acknowledge_interruption,true);assert.equal(JSON.parse(calls[0].body).auto_policy.vespera_demand,true);assert.equal(JSON.parse(calls[0].body).auto_policy.dwarf_demand,true);
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
