"""Synthetic-schema Vespera status allowlist; never sends instrument commands."""
from collections import deque
import http.client
import math
import threading

from ...config import profiles
from ..engineio import Budget, Client, PeerClosed, PollingHTTP
from ..metrics import bounded_text
from ..settings import selected_adapters

ADAPTER = 'vespera_status_v1'
BACKOFF = (2, 5, 10, 30)
YIELD_SECONDS = 600
KINDS = frozenset(('idle', 'initialization', 'observation', 'capture', 'stacking', 'mosaic', 'calibration', 'shutdown'))
STATES = frozenset(('idle', 'pending', 'running', 'paused', 'completed', 'failed', 'stopped'))
STEPS = frozenset(('initialization', 'pointing', 'astrometry', 'focusing', 'capture', 'stacking', 'completed', 'failed'))
REASONS = frozenset(('astrometry', 'tracking', 'cloud', 'blur', 'brightness', 'star_count', 'other'))


def object_at(value, key):
    child = value.get(key)
    return child if isinstance(child, dict) else {}


def number(value, minimum=0, maximum=2**53):
    return type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value)


def normalize(status):
    """Only explicit leaf paths leave this function; no source dict is retained.

    Field shapes are synthetic from the protocol description, not a firmware
    capture. Unrecognized variants are deliberately omitted until reviewed.
    """
    if not isinstance(status, dict): return {}
    values = {}
    operation = object_at(status, 'currentOperation')
    for field, allowed in (('kind', KINDS), ('state', STATES), ('step', STEPS)):
        value = operation.get(field)
        if isinstance(value, str) and value in allowed: values['operation.'+field] = value
    # A current step may also be selected from the operation's bounded steps list.
    steps, index = operation.get('steps'), operation.get('currentStep')
    if 'operation.step' not in values and isinstance(steps, list) and len(steps) <= 32 and type(index) is int and 0 <= index < len(steps):
        step = steps[index]
        value = step.get('kind') if isinstance(step, dict) else step
        if isinstance(value, str) and value in STEPS: values['operation.step'] = value
    stacking = object_at(operation, 'stacking')
    capture = object_at(operation, 'capture')
    for field, metric, source in (('acquired', 'frames_acquired', capture), ('accepted', 'frames_accepted', stacking), ('rejected', 'frames_rejected', stacking)):
        value = source.get(field)
        if type(value) is int and number(value): values['stack.'+metric] = value
    reasons = object_at(stacking, 'rejectReasons')
    for reason in REASONS:
        value = reasons.get(reason)
        if type(value) is int and number(value): values['stack.reject_reason.'+reason] = value
    astrometry = object_at(operation, 'astrometry')
    if astrometry.get('state') == 'failed': values['error.code'] = 'pointing_failed'
    battery, storage = object_at(status, 'battery'), object_at(status, 'storage')
    if number(battery.get('level'), 0, 100): values['battery.percent'] = battery['level']
    if type(battery.get('charging')) is bool: values['battery.charging'] = battery['charging']
    if number(storage.get('freeMb')): values['storage.free_mb'] = storage['freeMb']
    filter_name = status.get('filter')
    if isinstance(filter_name, str) and filter_name in ('none', 'dual_band', 'light_pollution', 'solar', 'dark'):
        values['filter.name'] = filter_name
    heater = object_at(status, 'settings').get('defog')
    if type(heater) is bool: values['heater.state'] = 'on' if heater else 'off'
    sensors = object_at(status, 'sensors')
    for field, metric, low, high in (('temperature', 'temperature_c', -100, 150),
                                    ('humidity', 'humidity_pct', 0, 100), ('dewPoint', 'dew_point_c', -100, 150)):
        if number(sensors.get(field), low, high): values['env.'+metric] = sensors[field]
    target = object_at(operation, 'target').get('name')
    if bounded_text(target): values['target.name'] = target
    motors = object_at(status, 'motors')
    for field, metric in (('tracking', 'tracking'), ('slewing', 'slewing'), ('atPark', 'at_park')):
        if type(motors.get(field)) is bool: values['mount.'+metric] = motors[field]
    if 'masterDeviceId' in status:
        master = status['masterDeviceId']
        if master is None or isinstance(master, str) and len(master) <= 128:
            values['device.master_present'] = bool(master)
    return values


class Observer:
    """One profile's bounded polling/retry state; callbacks take controller first."""
    def __init__(self, collector, profile, interface, factory=None):
        self.owner, self.c = collector, collector.c
        self.profile = {key:profile.get(key) for key in ('id', 'kind', 'mac', 'target_ip')}
        self.interface, self.factory = interface, factory
        self.signature = (profile['mac'], profile['target_ip'], interface)
        self.c.telemetry_sessions.readings.clear(profile['id'], ADAPTER)
        self.halt = threading.Event()
        self.thread = None
        self.client = None
        self.budget = Budget(collector.clock)
        self.closures = deque(maxlen=4)
        self.adapter_state = 'waiting'
        self.retry_at = self.yield_until = 0
        self.failures = 0
        self.previous = {}
        self.sampled = None
        self.received_at = None

    def status(self):
        return {'adapter_state':self.adapter_state, 'retry_after':max(0, int(max(self.retry_at,self.yield_until)-self.owner.clock()))}

    def selected(self):
        if self.halt.is_set() or self.owner.halt.is_set() or self.c.stop.is_set() or self.c.telemetry_sessions.closed: return None
        profile = next((p for p in profiles(self.c.store.config) if p['id'] == self.profile['id']), None)
        row = next((r for r in self.c.state['telescopes'] if r['id'] == self.profile['id']), {})
        if (not profile or not profile['enabled'] or profile['kind'] != 'vespera' or not row.get('connected')
                or (profile['mac'], profile['target_ip'], row.get('interface')) != self.signature
                or ADAPTER not in selected_adapters(self.c.telemetry_store.settings, profile)): return None
        return profile

    def receive(self, status):
        # Raw fields exist only on this call stack and never enter state/history.
        received_at = self.owner.clock()
        values = normalize(status)
        with self.c.lock:
            profile = self.selected()
            if profile is None: return
            identities = getattr(self.c, 'identities', None)
            if identities is not None and isinstance(status, dict) and 'telescopeId' in status:
                # The raw telescopeId is reduced to a keyed digest here; it is never retained.
                identities.record(profile, 'vespera_status', status.get('model'), status.get('telescopeId'))
            self.adapter_state = 'listening'
            sessions = self.c.telemetry_sessions
            active = values.get('operation.state') == 'running' and values.get('operation.kind') not in (None, 'idle', 'shutdown')
            # Increasing capture/stack counters confirm activity even on sparse status.
            counters = ('stack.frames_acquired', 'stack.frames_accepted', 'stack.frames_rejected')
            active = active or any(key in self.previous and values.get(key,0) > self.previous[key] for key in counters)
            opened = active and profile['id'] not in sessions.active
            if active: sessions.touch(profile['id'], profile['kind'], selected_adapters(self.c.telemetry_store.settings, profile), location=profile['mac'])
            now = self.owner.clock()
            due = opened or self.sampled is None or now-self.sampled >= 30
            # Target transitions precede their counters in the flat journal.
            for metric,value in sorted(values.items(), key=lambda item:item[0]!='target.name'):
                # Pointing failure is a transition, never one count per repeated push.
                changed = metric not in self.previous or value != self.previous[metric]
                if changed or due and metric != 'error.code': sessions.observe(profile['id'], metric, value)
            sessions.readings.receive(profile['id'], ADAPTER, values, 1.5 if values.get('operation.state') == 'running' else 20, received_at)
            self.received_at = received_at
            self.previous = values
            if due: self.sampled = now

    def failed(self, error, established=False):
        now = self.owner.clock()
        peer = isinstance(error, (PeerClosed, ConnectionResetError, ConnectionAbortedError,
                                  BrokenPipeError, http.client.RemoteDisconnected, http.client.IncompleteRead))
        with self.c.lock:
            if self.selected() is None: return
            if now < self.yield_until: return
            while self.closures and now-self.closures[0] >= 120: self.closures.popleft()
            if peer and established: self.closures.append(now)
            if len(self.closures) > 3:
                self.adapter_state = 'yielded'
                self.yield_until = now+YIELD_SECONDS
                self.closures.clear()
                self.failures = 0
                events = self.c.event_store
                if not events.closed:
                    events.native('telemetry.adapter_yielded', self.profile['id'], adapter_id=ADAPTER, retry_after=YIELD_SECONDS)
            else:
                self.adapter_state = 'waiting'
                self.retry_at = now+BACKOFF[min(self.failures, len(BACKOFF)-1)]
                self.failures = min(self.failures+1, len(BACKOFF)-1)

    def attempt(self):
        with self.c.lock:
            if self.selected() is None: return
            self.adapter_state = 'connecting'
            self.client = self.owner.client_factory(PollingHTTP(self.profile['target_ip'], self.interface, factory=self.factory),
                                                     budget=self.budget, clock=self.owner.clock)
            client = self.client
        started = self.owner.clock()
        try: client.run(self.receive)
        except (OSError, ValueError, http.client.HTTPException) as error:
            if self.owner.clock()-started >= 120: self.failures = 0
            self.failed(error, client.sid is not None)
        finally:
            client.close()
            self.client = None

    def loop(self):
        while not self.halt.is_set() and not self.c.stop.is_set() and not self.owner.halt.is_set():
            wait = max(self.retry_at, self.yield_until)-self.owner.clock()
            if wait > 0:
                self.halt.wait(min(wait, 1))
                continue
            with self.c.lock:
                if self.selected() is None: break
            self.attempt()

    def start(self):
        self.thread = threading.Thread(target=self.loop, name='bridge-vespera', daemon=True)
        self.thread.start()

    def stop(self):
        self.halt.set()
        client = self.client
        if client: client.close()

    def join(self):
        if self.thread: self.thread.join(timeout=6)


class VesperaCollector:
    def __init__(self, controller, *, client_factory=Client):
        self.c = controller
        self.clock = controller.telemetry_store.monotonic
        self.client_factory = client_factory
        self.halt = threading.Event()
        self.thread = None
        self.workers = {}

    def available(self, ident):
        worker = self.workers.get(ident)
        return (not self.c.demo and not self.halt.is_set() and not self.c.telemetry_sessions.closed
                and (worker is None or worker.adapter_state != 'yielded' or self.clock() >= worker.yield_until))

    def operation_state(self, profile):
        """Cached normalized evidence only; never waits for polling/network work.

        Source pushes arrive about every 1.5 seconds while running, 20 at rest.
        The independent 30-second storage cadence is not freshness evidence.
        None means unsupported/disabled, unknown means selected but unconfirmed.
        """
        with self.c.lock:
            if (self.c.demo or profile['kind'] != 'vespera'
                    or ADAPTER not in selected_adapters(self.c.telemetry_store.settings, profile)):
                return None
            worker = self.workers.get(profile['id'])
            if worker is None or worker.selected() is None or worker.adapter_state != 'listening': return 'unknown'
            state = worker.previous.get('operation.state')
            cadence = 1.5 if state == 'running' else 20
            if (worker.received_at is None or not 0 <= self.clock()-worker.received_at <= 3*cadence
                    or state not in STATES): return 'unknown'
            return state

    def operation_kind(self, profile):
        """Kind of the running operation from the same fresh evidence, else None."""
        if self.operation_state(profile) != 'running': return None
        with self.c.lock:
            kind = self.workers[profile['id']].previous.get('operation.kind')
        return kind if kind in KINDS and kind not in ('idle', 'shutdown') else None

    def status(self):
        with self.c.lock: return {ident:worker.status() for ident,worker in self.workers.items()}

    def tick(self):
        retired = []
        with self.c.lock:
            if self.halt.is_set() or self.c.telemetry_sessions.closed: return
            rows = {r['id']:r for r in self.c.state['telescopes'] if r.get('connected')}
            # An isolated Vespera radio lives in its own namespace: only its relays reach it.
            selected = {p['id']:p for p in profiles(self.c.store.config) if not self.c.demo and p['enabled'] and not p.get('isolated')
                        and p['kind'] == 'vespera' and p['id'] in rows and rows[p['id']].get('interface')
                        and ADAPTER in selected_adapters(self.c.telemetry_store.settings, p)}
            for ident,worker in list(self.workers.items()):
                p = selected.get(ident)
                if (p is None or worker.signature != (p['mac'], p['target_ip'], rows[ident]['interface'])
                        or worker.thread is not None and not worker.thread.is_alive()):
                    worker.halt.set()
                    retired.append(worker)
                    del self.workers[ident]
            for ident,p in selected.items():
                if ident in self.workers: continue
                factory = None
                if p.get('isolated'):
                    manager = getattr(self.c.net, 'isolated', None)
                    device = next((d for d in self.c.state['devices'] if d['mac'] == p['mac']), None)
                    if not manager or not device: continue
                    record = manager.record_for(device)
                    if not record: continue
                    factory = manager.socket_factory(record)
                worker = Observer(self, p, rows[ident]['interface'], factory)
                self.workers[ident] = worker
                worker.start()
        # Blocking socket cancellation/join is always outside controller.lock.
        for worker in retired: worker.stop()
        for worker in retired: worker.join()

    def loop(self):
        while not self.halt.is_set() and not self.c.stop.is_set():
            try: self.tick()
            except Exception: pass  # Never log source payloads, addresses or IDs.
            self.halt.wait(1)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.loop, name='bridge-observers', daemon=True)
            self.thread.start()

    def close(self):
        self.halt.set()
        if self.thread: self.thread.join(timeout=2)
        with self.c.lock:
            workers = list(self.workers.values())
            self.workers.clear()
            for worker in workers: worker.halt.set()
        for worker in workers: worker.stop()
        for worker in workers: worker.join()
