"""Opt-in Alpaca instrument observations with normalized, allowlisted metrics."""
import hashlib
import http.client
import math
import threading

from ...config import profiles
from ..alpaca import MEMBERS, Transport
from ..settings import selected_adapters
from .seestar_events import isolated_priority

ADAPTER = 'alpaca_observer_v1'
CAMERA_STATES = ('idle', 'waiting', 'exposing', 'reading', 'downloading', 'error')
ERROR_SECONDS = 600


def interval(settings, ident):
    value = settings['profiles'].get(ident, {}).get('alpaca_interval_seconds', 15)
    return value if type(value) is int and 10 <= value <= 120 else 15


def normalize(kind, member, value):
    if kind == 'telescope':
        if member in ('tracking', 'slewing', 'atpark'):
            return ({'tracking':'mount.tracking', 'slewing':'mount.slewing', 'atpark':'mount.at_park'}[member], value) if type(value) is bool else None
        limits = {'rightascension': ('target.ra_hours', 0, 24), 'declination': ('target.dec_deg', -90, 90)}
        if member not in limits: return None
        metric, low, high = limits[member]
    elif kind == 'camera' and member == 'camerastate':
        return ('camera.state', CAMERA_STATES[value]) if type(value) is int and 0 <= value < len(CAMERA_STATES) else None
    elif kind == 'camera' and member == 'ccdtemperature': metric, low, high = 'device.temperature_c', -273.15, 150
    else: return None
    if type(value) in (int, float) and low <= value <= high and math.isfinite(value):
        return metric, value
    return None


class Observer:
    def __init__(self, owner, profile, row, factory=None, permission=None):
        self.owner, self.c = owner, owner.c
        self.profile = {key:profile.get(key) for key in ('id', 'kind', 'mac', 'target_ip')}
        self.signature = (profile['mac'], row['interface'], row['client_ip'], bool(profile.get('isolated')), profile['target_ip'])
        self.transport = owner.transport_factory(row['client_ip'], row['interface'], factory, target=profile['target_ip'], **({'permission':permission} if permission else {}))
        self.c.telemetry_sessions.readings.clear(profile['id'], ADAPTER)
        self.halt = threading.Event()
        self.thread = None
        self.devices = []
        self.unavailable = {}
        self.next_poll = self.next_discovery = 0

    def selected(self):
        if self.halt.is_set() or self.owner.halt.is_set() or self.c.stop.is_set() or self.c.telemetry_sessions.closed or self.c.demo: return None
        p = next((p for p in profiles(self.c.store.config) if p['id'] == self.profile['id']), None)
        row = next((r for r in self.c.state['telescopes'] if r['id'] == self.profile['id']), {})
        if (not p or not p['enabled'] or p['kind'] != self.profile['kind'] or not row.get('connected')
                or (p['mac'], row.get('interface'), row.get('client_ip'), bool(p.get('isolated')), p['target_ip']) != self.signature
                or isolated_priority(self.c,p)
                or ADAPTER not in selected_adapters(self.c.telemetry_store.settings, p)): return None
        return p

    def read(self, endpoint, kind, number, member):
        if endpoint[0] != self.profile['target_ip']: return None
        key = (endpoint, kind, number, member)
        now = self.owner.clock()
        if now < self.unavailable.get(key, 0) or len(self.unavailable) >= 512 and key not in self.unavailable: return None
        with self.c.lock:
            if self.selected() is None: return None
        try:
            error, value = self.transport.get(endpoint, kind, number, member, self.owner.client_id, self.owner.transaction())
            if error:
                self.unavailable[key] = self.owner.clock()+ERROR_SECONDS
                return None
            return value
        except (OSError, ValueError, http.client.HTTPException): return None

    def poll(self):
        with self.c.lock:
            p = self.selected()
            now = self.owner.clock()
            if p is None or now < self.next_poll: return
            self.next_poll = now+interval(self.c.telemetry_store.settings, p['id'])
        self.unavailable = {k:v for k,v in self.unavailable.items() if v > now}
        if now >= self.next_discovery:
            self.next_discovery = now+60
            try: endpoints = [e for e in self.transport.discover() if e[0] == self.profile['target_ip']]
            except (OSError, ValueError): return
            try: self.transport.allow(endpoints[:4])
            except (OSError, ValueError): return
            devices = []
            for endpoint in endpoints[:4]:
                rows = self.read(endpoint, None, None, 'configureddevices')
                if not isinstance(rows, list) or len(rows) > 32: continue
                for row in rows:
                    if not isinstance(row, dict): continue
                    row = {key.lower():value for key,value in row.items()}
                    kind, number = row.get('devicetype'), row.get('devicenumber')
                    kind = kind.lower() if isinstance(kind, str) else None
                    if kind in MEMBERS and type(number) is int and 0 <= number <= 65535:
                        devices.append((endpoint, kind, number))
            # One deterministic device of each type avoids ambiguous merged metrics
            # and bounds each cycle to seven read-only member requests.
            self.devices = []
            for kind in MEMBERS:
                candidates = sorted(set(d for d in devices if d[1] == kind))
                if candidates: self.devices.append(candidates[0])
            if self.devices: self.next_discovery = now+600
        try: self.transport.allow(sorted(set(device[0] for device in self.devices)))
        except (OSError, ValueError): return
        values = {}
        for endpoint, kind, number in self.devices:
            for member in MEMBERS[kind]:
                value = self.read(endpoint, kind, number, member)
                normalized = normalize(kind, member, value)
                if normalized: values[normalized[0]] = normalized[1]
        with self.c.lock:
            p = self.selected()
            if p is None: return
            sessions = self.c.telemetry_sessions
            active = (values.get('mount.tracking') is True or values.get('mount.slewing') is True
                      or values.get('camera.state') in CAMERA_STATES[1:5])
            if active: sessions.touch(p['id'], p['kind'], selected_adapters(self.c.telemetry_store.settings, p), location=p['mac'])
            for metric, value in values.items(): sessions.observe(p['id'], metric, value)
            sessions.readings.receive(p['id'], ADAPTER, values, interval(self.c.telemetry_store.settings, p['id']))

    def loop(self):
        while not self.halt.is_set() and not self.owner.halt.is_set() and not self.c.stop.is_set():
            with self.c.lock:
                if self.selected() is None: break
            self.poll()
            self.halt.wait(.25)

    def start(self):
        self.thread = threading.Thread(target=self.loop, name='bridge-alpaca-observer', daemon=True)
        self.thread.start()

    def stop(self):
        self.halt.set()
        self.transport.close()

    def join(self):
        if self.thread: self.thread.join(timeout=3)


class AlpacaCollector:
    def __init__(self, controller, *, transport_factory=Transport):
        self.c = controller
        self.clock = controller.telemetry_store.monotonic
        self.transport_factory = transport_factory
        self.client_id = int.from_bytes(hashlib.sha256(getattr(controller, 'device_id', 'astro-bridge').encode()).digest()[:2], 'big') or 1
        self.counter = 0
        self.counter_lock = threading.Lock()
        self.halt = threading.Event()
        self.thread = None
        self.workers = {}

    def transaction(self):
        with self.counter_lock:
            self.counter = self.counter % (2**32-1)+1
            return self.counter

    def available(self, ident):
        profile = next((p for p in profiles(self.c.store.config) if p['id']==ident),None)
        return (not self.c.demo and not self.halt.is_set() and not self.c.telemetry_sessions.closed
                and not (profile and isolated_priority(self.c,profile)))

    def tick(self):
        retired = []
        with self.c.lock:
            if self.halt.is_set() or self.c.telemetry_sessions.closed: return
            rows = {r['id']:r for r in self.c.state['telescopes'] if r.get('connected') and r.get('interface') and r.get('client_ip')}
            selected = {p['id']:p for p in profiles(self.c.store.config) if not self.c.demo and p['enabled'] and p['id'] in rows
                        and ADAPTER in selected_adapters(self.c.telemetry_store.settings, p) and not isolated_priority(self.c,p)}
            for ident, worker in list(self.workers.items()):
                p, row = selected.get(ident), rows.get(ident, {})
                if (p is None or worker.profile['kind'] != p['kind'] or worker.signature != (p['mac'],row['interface'],row['client_ip'],bool(p.get('isolated')),p['target_ip'])
                        or worker.thread is not None and not worker.thread.is_alive()):
                    worker.halt.set(); retired.append(worker); del self.workers[ident]
            for ident, p in selected.items():
                if ident in self.workers: continue
                factory = permission = None
                if p.get('isolated'):
                    manager = getattr(self.c.net, 'isolated', None)
                    device = next((d for d in self.c.state['devices'] if d['mac'] == p['mac']), None)
                    record = manager.record_for(device) if manager and device else None
                    if not record: continue
                    permission = manager.observer_access(record, p, rows[ident]['client_ip'])
                    factory = permission.socket
                worker = Observer(self, p, rows[ident], factory, permission)
                self.workers[ident] = worker
                worker.start()
        for worker in retired: worker.stop()
        for worker in retired: worker.join()

    def loop(self):
        while not self.halt.is_set() and not self.c.stop.is_set():
            try: self.tick()
            except Exception: pass  # Never log source payloads or network identifiers.
            self.halt.wait(.5)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.loop, name='bridge-alpaca-observers', daemon=True)
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
