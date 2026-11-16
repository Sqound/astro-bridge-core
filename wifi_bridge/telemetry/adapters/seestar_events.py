"""Owner-key authenticated, bounded Seestar event listener. No control commands."""
from collections import deque
import hmac
import secrets
import json
import socket
import threading

from ...config import profiles, network_intent, role_for
from ...maker_keys.verify import Handshake, KEEPALIVE_METHODS, send
from ..engineio import Budget, PeerClosed
from ..settings import selected_adapters
from .vespera_status import number, STATES, BACKOFF, YIELD_SECONDS

KINDS = {'Stack':'stacking','Exposure':'capture','AutoGoto':'pointing','AutoFocus':'focusing'}

ADAPTER = 'seestar_events_v1'
MAX_LINE = 64 * 1024
EVENTS = frozenset(('PiStatus', 'Stack', 'Exposure', 'AutoGoto', 'AutoFocus'))
COUNT_NAMES = ('PiStatus','Stack','Exposure','AutoGoto','AutoFocus','unknown')
# Synthetic receipt assumptions, configurable at construction pending hardware review.
OPERATION_CADENCE = 20


def normalize(event):
    if not isinstance(event, dict): return {}
    name = event.get('Event')
    if not isinstance(name, str) or name not in EVENTS: return {}
    values = {}
    if name == 'PiStatus':
        for field, metric, low, high in (('temp','device.temperature_c',-100,150), ('battery_capacity','battery.percent',0,100)):
            if number(event.get(field),low,high): values[metric] = event[field]
        charging = event.get('charger_status')
        if type(charging) is bool or type(charging) is int and charging in (0,1): values['battery.charging'] = bool(charging)
        # battery_temp/is_overtemp have no reviewed metric; discard.
    if name == 'Stack':
        for field, metric in (('stacked_frame','stack.frames_accepted'),('dropped_frame','stack.frames_rejected')):
            if type(event.get(field)) is int and number(event[field]): values[metric] = event[field]
        code = event.get('frame_errcode')
        if type(code) is int and 1 <= code <= 255: values['stack.reject_reason.'+str(code)] = 1
    if name == 'Exposure':
        for field in ('exposure', 'duration'):
            if number(event.get(field),0,86400): values['exposure.seconds'] = event[field]; break
    if name != 'PiStatus':
        state = event.get('state')
        if isinstance(state,str) and state in STATES:
            values['operation.kind'] = KINDS[name]
            values['operation.state'] = state
        if name == 'AutoGoto' and state == 'failed' and type(event.get('code')) is int and 1 <= event['code'] <= 255:
            values['error.code'] = 'pointing_failed'
    return values


class Client(Handshake):
    def __init__(self, *args, budget, clock, keepalive=None, keepalive_seconds=30, **kwargs):
        if keepalive is not None and keepalive not in KEEPALIVE_METHODS: raise ValueError('Seestar keepalive refused')
        if type(keepalive_seconds) not in (int,float) or not 10 <= keepalive_seconds <= 120: raise ValueError('Invalid keepalive cadence')
        super().__init__(*args, **kwargs)
        self.budget, self.clock = budget, clock
        self.keepalive, self.keepalive_seconds = keepalive, keepalive_seconds
        self.established = False
        self.peer_closed = False
        self.unknown_events = 0  # Count occurrences, never retain unknown names.

    def listen(self, pem, receive, current, authenticated=lambda: None, counted=lambda name: None):
        def listener(stream, pending):
            self.established = True
            authenticated()
            stream.settimeout(.5)
            renewal = self.clock()+60
            heartbeat = self.clock()+self.keepalive_seconds
            try:
                while current():
                    now = self.clock()
                    if now >= renewal:
                        self.transport.allow([(self.target,4700)]); renewal = now+60
                    if self.keepalive is not None and now >= heartbeat:
                        send(stream,4,self.keepalive,None,keepalive=True)
                        heartbeat = now+self.keepalive_seconds
                    if b'\n' not in pending:
                        if len(pending) >= MAX_LINE: raise ValueError('Seestar event too large')
                        try: chunk = stream.recv(min(4096, MAX_LINE-len(pending)))
                        except socket.timeout: continue
                        if not chunk: raise PeerClosed()
                        self.budget.add(len(chunk)); pending.extend(chunk); chunk = None
                        continue
                    at = pending.index(10)
                    line = pending[:at]; del pending[:at+1]
                    try: event = json.loads(line)
                    finally: line[:] = b'\0'*len(line)
                    if not isinstance(event,dict):
                        event = None; continue
                    name = event.get('Event')
                    if not isinstance(name,str):
                        event = name = None; continue
                    if name not in EVENTS:
                        self.unknown_events = min(2**53,self.unknown_events+1)
                        counted("unknown")
                        event = name = None
                        continue
                    counted(name)
                    receive(event)
                    event = name = None
            except (PeerClosed, ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                self.peer_closed = current() and not (self.transport.permission and self.transport.permission.closed)
        return super().run(pem,current,listener=listener)


def signature(profile, row, secret):
    return (hmac.digest(secret,json.dumps(network_intent(profile),sort_keys=True).encode(),'sha256'),row.get('interface'),row.get('client_ip'))


def isolated_priority(controller, profile):
    """One isolated grant owner: reserve it for selected authenticated listening."""
    keys = getattr(controller,'maker_keys',None)
    return (not controller.demo and profile.get('isolated') and profile['kind']=='seestar'
            and keys is not None and not keys.closed and keys.store.status()['state'] in ('verified','unverified')
            and ADAPTER in selected_adapters(controller.telemetry_store.settings,profile))


class Observer:
    def __init__(self, owner, profile, row):
        self.owner, self.c = owner, owner.c
        self.ident = profile['id']
        self.signature = signature(profile,row,owner.signature_key)
        self.generation = self.c.maker_keys.generation
        self.c.telemetry_sessions.readings.clear(profile['id'], ADAPTER)
        self.halt = threading.Event()
        self.thread = self.client = None
        self.adapter_state = 'waiting'
        self.retry_at = self.yield_until = 0
        self.failures = 0
        self.closures = deque(maxlen=4)
        self.budget = Budget(owner.clock)
        self.previous = {}
        self.sampled = None
        self.operations = {}
        self.goto_failed = False
        self.unknown_events = 0
        self.event_buckets = deque(maxlen=61)
        self.handshakes = {'verified':0,'refused':0,'unverified':0}
        self.connection_closes = 0
        self.listening_since = None
        self.last_connection_seconds = 0

    def count(self, name):
        with self.c.lock:
            if self.selected() is None or name not in COUNT_NAMES: return
            now = int(self.owner.clock())
            while self.event_buckets and self.event_buckets[0][0] <= now-60: self.event_buckets.popleft()
            if not self.event_buckets or self.event_buckets[-1][0] != now: self.event_buckets.append((now,{}))
            counts = self.event_buckets[-1][1]
            counts[name] = min(2**53,counts.get(name,0)+1)

    def status(self):
        now = self.owner.clock()
        counts = {name:min(2**53,sum(bucket.get(name,0) for at,bucket in self.event_buckets if at>int(now)-60)) for name in COUNT_NAMES}
        return {'adapter_id':ADAPTER,'adapter_state':self.adapter_state,'retry_after':max(0,int(max(self.retry_at,self.yield_until)-now)),
                'unavailable_reason':None,'event_counts_minute':counts,'handshakes':dict(self.handshakes),
                'connection_closes':self.connection_closes,'connected_seconds':max(0,int(now-self.listening_since)) if self.listening_since is not None else 0,
                'last_connection_seconds':self.last_connection_seconds}

    def selected(self):
        if (self.halt.is_set() or self.owner.halt.is_set() or self.c.stop.is_set() or self.c.demo
                or self.c.telemetry_sessions.closed or self.c.maker_keys.closed
                or any(isinstance(t,Handshake) for t in self.c.maker_keys.transports)
                or self.generation != self.c.maker_keys.generation
                or self.c.maker_keys.store.status()['state'] not in ('verified','unverified')): return None
        p = next((p for p in profiles(self.c.store.config) if p['id'] == self.ident),None)
        row = next((r for r in self.c.state['telescopes'] if r['id'] == self.ident),{})
        if (not p or p['kind'] != 'seestar' or not p['enabled'] or not row.get('connected')
                or not row.get('interface') or not row.get('client_ip') or self.c.pauses.held(self.ident)
                or signature(p,row,self.owner.signature_key) != self.signature
                or network_intent(self.c.applied.get(role_for(p))) != network_intent(p)
                or ADAPTER not in selected_adapters(self.c.telemetry_store.settings,p)): return None
        return p

    def current(self):
        with self.c.lock: return self.selected() is not None

    def receive(self, event):
        if not isinstance(event,dict) or not isinstance(event.get('Event'),str) or event['Event'] not in EVENTS: return
        received = self.owner.clock()
        values = normalize(event)
        with self.c.lock:
            p = self.selected()
            if p is None: return
            # A recognized operation packet with an unknown state invalidates
            # prior operation evidence; PiStatus never changes its timestamp.
            if event.get('Event') != 'PiStatus':
                self.operations[event['Event']] = (values.get('operation.state'),received)
            if event['Event'] == 'AutoGoto':
                if 'error.code' in values:
                    if self.goto_failed: values.pop('error.code')
                    self.goto_failed = True
                elif values.get('operation.state') in STATES-{'failed'}:
                    self.goto_failed = False
                    self.previous.pop('error.code',None)
            sessions = self.c.telemetry_sessions
            counters = ('stack.frames_accepted','stack.frames_rejected')
            active = values.get('operation.state') == 'running' or any(k in self.previous and values.get(k,0)>self.previous[k] for k in counters)
            opened = active and p['id'] not in sessions.active
            if active: sessions.touch(p['id'],p['kind'],selected_adapters(self.c.telemetry_store.settings,p),location=p['mac'])
            due = opened or self.sampled is None or received-self.sampled >= 30
            for metric in list(values):
                if metric.startswith('stack.reject_reason.'):
                    prior = self.previous.get(metric,0)
                    changed = values.get('stack.frames_rejected') != self.previous.get('stack.frames_rejected')
                    values[metric] = min(2**53,prior+1) if changed or metric not in self.previous else prior
            for metric,value in values.items():
                if metric not in self.previous or value != self.previous[metric] or due and not metric.startswith(('error.','stack.reject_reason.')):
                    sessions.observe(p['id'],metric,value)
            # Only the finite normalized vocabulary persists; raw fields die here.
            sessions.readings.receive(p['id'], ADAPTER, values, self.owner.operation_cadence, received)
            self.previous.update(values)
            if due: self.sampled = received

    def failed(self, peer=False):
        with self.c.lock:
            if self.selected() is None: return
            now = self.owner.clock()
            if now < self.yield_until: return
            while self.closures and now-self.closures[0] >= 120: self.closures.popleft()
            if peer:
                self.closures.append(now)
                self.connection_closes = min(2**53,self.connection_closes+1)
            if len(self.closures)>3:
                self.adapter_state = 'yielded'; self.yield_until = now+YIELD_SECONDS
                self.closures.clear(); self.failures = 0
                if not self.c.event_store.closed:
                    self.c.event_store.native('telemetry.adapter_yielded',self.ident,adapter_id=ADAPTER,retry_after=YIELD_SECONDS)
            else:
                self.adapter_state = 'waiting'; self.retry_at = now+BACKOFF[min(self.failures,3)]
                self.failures = min(self.failures+1,3)

    def authenticated(self):
        with self.c.lock:
            if self.selected() is not None:
                self.adapter_state = 'listening'
                self.listening_since = self.owner.clock()
                self.handshakes['verified'] = min(2**53,self.handshakes['verified']+1)
                if self.c.maker_keys.store.status()['state'] != 'verified':
                    self.c.maker_keys.store.verdict('seestar','verified')

    def attempt(self):
        pem = client = None
        started = self.owner.clock()
        try:
            with self.c.lock:
                p = self.selected()
                if p is None: return
                row = next(r for r in self.c.state['telescopes'] if r['id']==self.ident)
                factory = permission = None
                if p.get('isolated'):
                    manager = getattr(self.c.net,'isolated',None)
                    device = next((d for d in self.c.state['devices'] if d['mac']==p['mac']),None)
                    record = manager.record_for(device) if manager and device else None
                    if not record: raise OSError()
                    permission = manager.observer_access(record,p,row['client_ip']); factory = permission.socket
                client = self.owner.client_factory(row['client_ip'],row['interface'],p['target_ip'],factory=factory,permission=permission,
                    budget=self.budget,clock=self.owner.clock,keepalive=self.owner.keepalive,keepalive_seconds=self.owner.keepalive_seconds)
                self.client = client
                pem = self.c.maker_keys.store.read('seestar')
                if pem is None: return
                self.adapter_state = 'connecting'
                self.operations.clear()
            def authenticated():
                pem[:] = b"\0"*len(pem)
                self.authenticated()
            verdict = client.listen(pem,self.receive,self.current,authenticated,self.count)
            with self.c.lock:
                if verdict == 'refused' and self.selected() is not None:
                    self.c.maker_keys.store.verdict('seestar','refused')
                    self.adapter_state = 'refused'
                    self.handshakes['refused'] = min(2**53,self.handshakes['refused']+1)
                    if not self.c.event_store.closed:
                        self.c.event_store.native('telemetry.adapter_refused',self.ident,adapter_id=ADAPTER)
                    return
            if not client.established and verdict == 'unverified':
                self.handshakes['unverified'] = min(2**53,self.handshakes['unverified']+1)
            if self.owner.clock()-started >= 120: self.failures = 0
            self.failed(client.established and client.peer_closed)
        except (OSError,ValueError): self.failed()
        finally:
            if pem is not None: pem[:] = b'\0'*len(pem)
            if client:
                self.unknown_events = min(2**53,self.unknown_events+client.unknown_events)
                client.close()
            with self.c.lock:
                if self.listening_since is not None:
                    self.last_connection_seconds = max(0,int(self.owner.clock()-self.listening_since))
                    self.listening_since = None
                self.client = None

    def loop(self):
        while self.current():
            wait = max(self.retry_at,self.yield_until)-self.owner.clock()
            if wait>0: self.halt.wait(min(wait,.5)); continue
            self.attempt()

    def start(self):
        self.thread = threading.Thread(target=self.loop,name='bridge-seestar-events',daemon=True); self.thread.start()

    def stop(self):
        self.halt.set()
        if self.client: self.client.close()

    def join(self):
        if self.thread: self.thread.join(timeout=3)


class SeestarCollector:
    def __init__(self, controller, *, client_factory=Client, operation_cadence=OPERATION_CADENCE, keepalive=None, keepalive_seconds=30):
        if type(operation_cadence) not in (int,float) or not 1 <= operation_cadence <= 120: raise ValueError('Invalid operation cadence')
        if keepalive is not None and keepalive not in KEEPALIVE_METHODS: raise ValueError('Seestar keepalive refused')
        if type(keepalive_seconds) not in (int,float) or not 10 <= keepalive_seconds <= 120: raise ValueError('Invalid keepalive cadence')
        self.c = controller
        self.clock = controller.telemetry_store.monotonic
        self.client_factory = client_factory
        self.signature_key = secrets.token_bytes(32)
        self.operation_cadence = operation_cadence
        self.keepalive, self.keepalive_seconds = keepalive, keepalive_seconds
        self.halt = threading.Event()
        self.thread = None
        self.workers = {}
        self.retired = {}

    def available(self, ident):
        worker = self.workers.get(ident)
        return (not self.c.demo and not self.halt.is_set() and not self.c.telemetry_sessions.closed
                and self.c.maker_keys.store.status()['state'] in ('verified','unverified')
                and (worker is None or worker.adapter_state not in ('yielded','refused') or self.clock() >= worker.yield_until and worker.adapter_state=='yielded'))

    def operation_state(self, profile):
        with self.c.lock:
            if self.c.demo or profile['kind']!='seestar' or ADAPTER not in selected_adapters(self.c.telemetry_store.settings,profile): return None
            worker = self.workers.get(profile['id'])
            if worker is None or worker.selected() is None or worker.adapter_state!='listening' or not worker.operations: return 'unknown'
            states = [state if state in STATES and 0 <= self.clock()-received <= 3*self.operation_cadence else 'unknown'
                      for state,received in worker.operations.values()]
            if 'running' in states: return 'running'
            if 'unknown' in states: return 'unknown'
            return next((state for state in states if state not in ('idle','completed','failed','stopped')), 'idle')

    def operation_kind(self, profile):
        """Kind of a fresh running event (stacking, capture, pointing, focusing), else None."""
        if self.operation_state(profile) != 'running': return None
        with self.c.lock:
            worker = self.workers[profile['id']]
            running = [name for name,(state,received) in worker.operations.items()
                       if state == 'running' and 0 <= self.clock()-received <= 3*self.operation_cadence]
        return KINDS.get(running[0]) if running else None

    def status(self):
        result = {}
        with self.c.lock:
            for p in profiles(self.c.store.config):
                if p['kind'] != 'seestar': continue
                selected = selected_adapters(self.c.telemetry_store.settings,p)
                worker = self.workers.get(p['id']) or self.retired.get(p['id'])
                reason = ('owner_disabled' if ADAPTER not in selected else 'demo' if self.c.demo else
                          'key_unavailable' if self.c.maker_keys.store.status()['state'] not in ('verified','unverified') else
                          'disconnected' if worker is None or worker.selected() is None else None)
                row = worker.status() if worker else {'adapter_id':ADAPTER,'adapter_state':'unavailable','retry_after':0,'unavailable_reason':reason}
                if reason and row['adapter_state'] != 'refused': row.update(adapter_state='unavailable',unavailable_reason=reason)
                result[p['id']] = [row]
                if 'alpaca_observer_v1' in selected and isolated_priority(self.c,p):
                    result[p['id']].append({'adapter_id':'alpaca_observer_v1','adapter_state':'unavailable','retry_after':0,'unavailable_reason':'isolated_priority'})
        return result

    def tick(self):
        retired = []
        with self.c.lock:
            if self.halt.is_set() or self.c.telemetry_sessions.closed: return
            self.c.maker_keys.enable_session_defaults()
            rows = {r['id']:r for r in self.c.state['telescopes']}
            configured = {p['id'] for p in profiles(self.c.store.config)}
            self.retired = {ident:value for ident,value in self.retired.items() if ident in configured}
            for ident,worker in list(self.workers.items()):
                if worker.selected() is None or worker.thread is not None and not worker.thread.is_alive():
                    self.retired[ident] = worker
                    worker.halt.set(); retired.append(worker); del self.workers[ident]
            for p in profiles(self.c.store.config):
                if p['kind']!='seestar' or p['id'] in self.workers: continue
                worker = Observer(self,p,rows.get(p['id'],{}))
                if worker.selected() is not None:
                    self.retired.pop(p['id'],None)
                    self.workers[p['id']] = worker; worker.start()
        for worker in retired: worker.stop()
        for worker in retired: worker.join()

    def loop(self):
        while not self.halt.is_set() and not self.c.stop.is_set():
            try: self.tick()
            except Exception: pass  # No raw payload, address or key logging.
            self.halt.wait(.5)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.loop,name='bridge-seestar-observers',daemon=True); self.thread.start()

    def close(self):
        self.halt.set()
        if self.thread: self.thread.join(timeout=2)
        with self.c.lock:
            workers = list(self.workers.values()); self.workers.clear()
            for worker in workers: worker.halt.set()
        for worker in workers: worker.stop()
        for worker in workers: worker.join()
