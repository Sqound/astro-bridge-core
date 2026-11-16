import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readdir, readFile } from 'node:fs/promises';
import { once } from 'node:events';
import test from 'node:test';
import { AstroBridgeClient, AstroBridgeError, NATIVE_CONTRACT_VERSION } from '../examples/astro-bridge-client.mjs';

const root = new URL('../', import.meta.url);
const directory = new URL('../contracts/native-client/v1/', import.meta.url);

async function startMock(name) {
  const child = spawn(process.env.PYTHON || 'python3', ['tools/contract-mock.py', '--scenario', name, '--port', '0'], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
  const port = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { child.kill(); reject(new Error('Replay startup timed out')); }, 10000);
    let output = '';
    child.stdout.on('data', data => {
      output += data;
      if (!output.includes('\n')) return;
      clearTimeout(timer);
      try { resolve(JSON.parse(output.split('\n')[0]).port); } catch (error) { reject(error); }
    });
    child.once('error', error => { clearTimeout(timer); reject(error); });
    child.once('exit', () => { clearTimeout(timer); reject(new Error('Replay exited before startup')); });
  });
  return { child, port };
}

function call(client, request) {
  const url = new URL(request.path, 'http://localhost');
  const path = url.pathname.replace('/api/v1', '');
  const parts = path.split('/');
  const body = request.body;
  const revision = request.headers['If-Match']?.slice(1, -1);
  if (path === '/info') return client.info();
  if (path === '/auth/token') return body.client_id ? client.loginClient(body.client_id, body.client_secret) : client.login(body.password);
  if (path === '/instruments') return client.instruments();
  if (parts[1] === 'instruments') {
    const id = parts[2];
    if (parts.length === 3) return client.instrument(id);
    if (parts[3] === 'connect') return client.connect(id, revision, body);
    if (parts[3] === 'endpoint') return client.endpoint(id);
    if (parts[3] === 'lease') return client.renewLease(id, body.lease_id, body.lease_seconds, body.reserve_seconds);
    if (parts[3] === 'release') return client.release(id, body.lease_id);
    if (parts[3] === 'disconnect') return client.disconnect(id, body.lease_id);
  }
  if (parts[1] === 'operations') return client.operation(parts[2]);
  if (path === '/events' && !url.searchParams.has('wait')) return client.request('GET', path + url.search);
  if (path === '/events') return client.waitEvents({ after: url.searchParams.has('after') ? Number(url.searchParams.get('after')) : undefined, bootId: url.searchParams.get('boot_id') ?? undefined, limit: Number(url.searchParams.get('limit')), wait: Number(url.searchParams.get('wait')) });
  return client.request(request.method, path + url.search, body ?? undefined, { revision });
}

for (const file of (await readdir(directory)).filter(name => name.endsWith('.json') && !['STATUS.json', 'openapi-snapshot.json'].includes(name)).sort()) {
  const flow = JSON.parse(await readFile(new URL(file, directory), 'utf8'));
  test('native helpers replay ' + flow.name, async () => {
    assert.equal(flow.contract_version, NATIVE_CONTRACT_VERSION);
    const { child, port } = await startMock(flow.name);
    try {
      const client = new AstroBridgeClient('http://127.0.0.1:' + port, { token: 'test-token-1' });
      for (let index = 0; index < flow.steps.length; index++) {
        let step = flow.steps[index];
        let result;
        // Exercise pair as one helper across the two protocol requests.
        const next = flow.steps[index + 1];
        if (step.request.path === '/api/v1/auth/token' && step.request.body?.password && next?.request.path === '/api/v1/auth/clients') {
          result = await client.pair(step.request.body.password, next.request.body.name, next.request.body.scopes);
          step = next; index++;
        } else if (step.response.status >= 400) {
          await assert.rejects(call(client, step.request), error => {
            assert.ok(error instanceof AstroBridgeError);
            assert.equal(error.status, step.response.status);
            assert.equal(error.code, step.response.body.error.code);
            if (step.response.body.error.replaceable) assert.deepEqual(error.details.replaceable, step.response.body.error.replaceable);
            return true;
          });
          continue;
        } else result = await call(client, step.request);
        assert.deepEqual(result, step.response.body);
      }
    } finally {
      const stopped = once(child, 'exit'); child.kill(); await stopped;
    }
  });
}

test('frozen native contract preserves unknown additive service outcomes', async () => {
  assert.equal(NATIVE_CONTRACT_VERSION, 1);
  const page = { events: ['not_answering', 'unknown', 'future_state'].map(outcome => ({ type: 'service_changed', outcome })) };
  const client = new AstroBridgeClient('http://bridge.local', { fetch: async () => new Response(JSON.stringify(page)) });
  assert.deepEqual(await client.waitEvents(), page);
});

test('native connect preserves bounded wait, replacement consent and paired lease duration', async () => {
  const calls=[];
  const client=new AstroBridgeClient('http://bridge.local',{token:'test-token-1',fetch:async(url,options)=>{
    calls.push({url,options});return new Response(JSON.stringify({ok:true}),{status:200,headers:{'Content-Type':'application/json'}});
  }});
  await client.connect('scope','revision-1',{lease_seconds:3600,wait_seconds:25,allow_replace:['victim']});
  assert.equal(calls[0].options.headers['If-Match'],'"revision-1"');
  assert.deepEqual(JSON.parse(calls[0].options.body),{lease_seconds:3600,wait_seconds:25,allow_replace:['victim']});
  await client.renewLease('scope','lease-1',3600);
  assert.deepEqual(JSON.parse(calls[1].options.body),{lease_id:'lease-1',lease_seconds:3600});
  await client.disconnect('scope');assert.deepEqual(JSON.parse(calls[2].options.body),{});
});


test('status preserves optional Ethernet kind and older paths without a kind', async () => {
  for(const path of [{role:'home',kind:'ethernet'},{role:'home'}]){
    const status={management:{paths:[path]}};
    const client=new AstroBridgeClient('http://bridge.local',{fetch:async()=>new Response(JSON.stringify(status))});
    assert.deepEqual(await client.status(),status);
  }
});
