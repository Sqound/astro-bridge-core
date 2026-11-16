"""Offline, normalized observation simulation; no sockets or maker credentials."""
import threading

from ..config import profiles
from .settings import selected_adapters


class DemoTelemetry:
    def __init__(self, controller):
        if not controller.demo: raise ValueError('Simulation requires demo mode')
        self.c = controller
        self.started = controller.telemetry_store.monotonic()
        self.last = {}
        self.states = {}
        self.thread = None

    def status(self): return {k:[dict(v)] for k,v in self.states.items()}

    def tick(self):
        c = self.c
        with c.lock, c.telemetry_sessions.lock, c.telemetry_store.lock:
            now = c.telemetry_store.monotonic()
            configured = profiles(c.store.config)
            ids = {p['id'] for p in configured}
            self.last = {k:v for k,v in self.last.items() if k in ids}
            self.states = {}
            live = {r['id'] for r in c.state['telescopes'] if r.get('connected')}
            for p in configured:
                adapter = {'vespera':'vespera_status_v1','seestar':'seestar_events_v1'}.get(p['kind'])
                adapters = selected_adapters(c.telemetry_store.settings,p)
                if not adapter or adapter not in adapters or not p['enabled'] or p['id'] not in live: continue
                # A synthetic Seestar listener follows the owner's explicit choice;
                # it never changes or claims a real manufacturer key verdict.
                if p['kind']=='seestar' and c.maker_keys.store.status()['state'] not in ('verified','unverified'): continue
                elapsed = int(now-self.started)
                phase = elapsed % 120
                state = 'yielded' if 85<=phase<95 else 'listening'
                self.states[p['id']] = {'adapter_id':adapter,'adapter_state':state,'retry_after':95-phase if state=='yielded' else 0,'unavailable_reason':None}
                # Ten seconds without readings demonstrates stale values, then a
                # separate yielded interval. Neither gap writes zero counters.
                if 65<=phase<75 or state=='yielded' or now-self.last.get(p['id'], -10)<1.5: continue
                self.last[p['id']]=now
                sessions=c.telemetry_sessions
                sessions.touch(p['id'],p['kind'],adapters,location=p['mac'])
                frames=142+elapsed//2
                values={'target.name':'M 27','operation.kind':'stacking','operation.state':'running',
                        'stack.frames_accepted':frames,'stack.frames_rejected':9+elapsed//20,
                        'exposure.seconds':10,'battery.percent':78,'env.dew_point_c':4.1,
                        'env.humidity_pct':71,'filter.name':'dual_band','heater.state':'on',
                        'stack.reject_reason.cloud':6,'stack.reject_reason.tracking':3}
                for metric,value in values.items(): sessions.observe(p['id'],metric,value)
                sessions.readings.receive(p['id'],adapter,values,1.5,now)

    def loop(self):
        while not self.c.stop.is_set():
            self.tick()
            self.c.stop.wait(.5)

    def start(self):
        self.thread=threading.Thread(target=self.loop,name='bridge-demo-telemetry',daemon=True)
        self.thread.start()

    def close(self):
        if self.thread: self.thread.join(timeout=2)
