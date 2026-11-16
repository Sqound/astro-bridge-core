"""Owner actions, bounded fetch jobs and stale-verdict protection."""
from collections import deque
import copy
import json
import threading
import time
import uuid

from ..api import ApiError
from ..config import profiles, network_intent, role_for
from .fetch import Fetcher, FetchError
from .rsa import parse, Unusable
from .store import KeyStore, StorageUnavailable
from .verify import Handshake


class MakerKeys:
    def __init__(self, controller, *, store=None, fetch_factory=Fetcher, handshake_factory=Handshake, clock=time.monotonic):
        self.c = controller
        self.store = store or KeyStore(controller.store.path.parent)
        self.fetch_factory, self.handshake_factory, self.clock = fetch_factory, handshake_factory, clock
        self.jobs = {}
        self.attempts = deque(maxlen=3)
        self.generation = 0
        self.workers = set()
        self.transports = set()
        self.closed = False

    def enable_session_defaults(self):
        """Supplying a key enables unconfigured profiles, preserving every opt-out."""
        telemetry = self.c.telemetry_store
        from ..telemetry.settings import prune_settings
        prune_settings(telemetry, profiles(self.c.store.config))
        if self.closed or self.store.status()['state'] not in ('verified','unverified'): return
        from ..telemetry.settings import selected_adapters
        with telemetry.lock:
            settings = copy.deepcopy(telemetry.settings)
            changed = False
            for profile in profiles(self.c.store.config):
                if profile['kind'] != 'seestar' or profile['id'] in settings['profiles']: continue
                adapters = selected_adapters(settings,profile)
                if 'seestar_events_v1' not in adapters: adapters.append('seestar_events_v1')
                settings['profiles'][profile['id']] = {'adapters':adapters}
                changed = True
            if changed:
                with telemetry.db:
                    telemetry.db.execute("INSERT OR REPLACE INTO metadata VALUES ('settings', ?)",(json.dumps(settings,sort_keys=True),))
                telemetry.settings = settings

    def status(self): return {'keys':[self.store.status()]}

    def inventory(self): return {'seestar':self.store.status()['state']}

    def source(self, family):
        if self.closed or self.c.stop.is_set() or self.c.demo: return None
        for profile in profiles(self.c.store.config):
            row = next((r for r in self.c.state['telescopes'] if r['id'] == profile['id']),{})
            if (profile['kind'] == family and profile['enabled'] and row.get('connected') and row.get('interface') and row.get('client_ip')
                    and network_intent(self.c.applied.get(role_for(profile))) == network_intent(profile)
                    and not self.c.pauses.held(profile['id'])):
                return (profile['id'],profile['kind'],profile['mac'],profile['target_ip'],bool(profile.get('isolated')),row['interface'],row['client_ip'],profile['ssid'],profile.get('bssid',''))
        return None

    def verify(self, family, generation):
        try: self._verify(family,generation)
        except (OSError,ValueError): pass  # Unreachable or retired source stays unverified.

    def _verify(self, family, generation):
        with self.c.lock:
            source = self.source(family)
            if source is None or generation != self.generation: return
            ident,_,mac,target,isolated,interface,address,_,_ = source
            factory = permission = None
            if isolated:
                manager = getattr(self.c.net,'isolated',None)
                device = next((d for d in self.c.state['devices'] if d['mac'] == mac),None)
                record = manager.record_for(device) if manager and device else None
                if not record: return
                profile = next(p for p in profiles(self.c.store.config) if p['id'] == ident)
                permission = manager.observer_access(record,profile,address);factory = permission.socket
            handshake = self.handshake_factory(address,interface,target,factory=factory,permission=permission)
            self.transports.add(handshake)
            try: pem = self.store.read(family)
            except (OSError,ValueError):
                handshake.close();self.transports.discard(handshake)
                return
        if pem is None:
            handshake.close()
            with self.c.lock: self.transports.discard(handshake)
            return
        def current():
            with self.c.lock: return generation == self.generation and source == self.source(family)
        try:
            verdict = handshake.run(pem,current)
            with self.c.lock:
                if current() and verdict in ('verified','refused'): self.store.verdict(family,verdict)
        except (OSError,ValueError): pass
        finally:
            pem[:] = b'\0'*len(pem)
            handshake.close()
            with self.c.lock: self.transports.discard(handshake)

    def start(self, function):
        if self.closed or len(self.workers) >= 4: return False
        def run():
            try: function()
            except Exception: pass  # Never let a transport exception render private data.
            finally:
                with self.c.lock: self.workers.discard(threading.current_thread())
        worker = threading.Thread(target=run,name='bridge-maker-key',daemon=True)
        self.workers.add(worker)
        try: worker.start()
        except RuntimeError:
            self.workers.discard(worker)
            return False
        return True

    def import_key(self, family, pem):
        if self.closed: raise StorageUnavailable()
        if not isinstance(pem,str) or len(pem) > 8192: raise Unusable()
        try: data = bytearray(pem,'ascii')
        except UnicodeError: raise Unusable() from None
        try:
            parse(data)
            self.cancel()
            self.store.put(family,data,'import')
            self.enable_session_defaults()
            generation = self.generation
            if self.source(family) is not None: self.start(lambda: self.verify(family,generation))
        finally: data[:] = b'\0'*len(data)

    def cancel(self):
        self.generation += 1
        for transport in list(self.transports): transport.close()
        for job in self.jobs.values():
            if job['state'] == 'running': job.update(state='failed',verdict='cancelled')

    def fetch(self, family):
        if self.closed or not self.store.available: raise StorageUnavailable()
        if any(j['state'] == 'running' for j in self.jobs.values()):
            raise ApiError(409,'FETCH_RUNNING','A key download is already running.')
        if (not self.c.state.get('home_ip') or not any(p.get('role') == 'home' for p in self.c.state.get('management',{}).get('paths',[]))
                or self.c.demo and self.fetch_factory is Fetcher):
            raise ApiError(409,'NO_INTERNET','Internet access is required on the bridge.')
        now = self.clock()
        while self.attempts and self.attempts[0] <= now-3600: self.attempts.popleft()
        if len(self.attempts) >= 3: raise ApiError(429,'FETCH_LIMITED','Too many key downloads. Try again later.')
        self.attempts.append(now)
        self.cancel();generation = self.generation
        ident = uuid.uuid4().hex
        job = {'id':ident,'state':'running','verdict':None,'steps':[], 'status_url':'/api/v1/maker-keys/operations/'+ident}
        self.jobs = {ident:job}
        fetcher = self.fetch_factory();self.transports.add(fetcher)
        def perform():
            pem = None
            try:
                pem = fetcher.fetch()
                with self.c.lock:
                    if generation != self.generation or self.closed: return
                    self.store.put(family,pem,'maker_package')
                    self.enable_session_defaults()
                self.verify(family,generation)
                with self.c.lock:
                    if generation == self.generation and not self.closed:
                        verdict = self.store.status(family)['state']
                        job.update(state='failed' if verdict in ('refused','storage_unavailable') else 'succeeded',verdict=verdict)
            except FetchError as error:
                with self.c.lock:
                    if generation == self.generation: job.update(state='failed',verdict=error.reason)
            except (OSError,ValueError):
                with self.c.lock:
                    if generation == self.generation: job.update(state='failed',verdict='storage_unavailable')
            finally:
                if pem is not None: pem[:] = b'\0'*len(pem)
                fetcher.close()
                with self.c.lock:
                    job['steps'] = copy.deepcopy(fetcher.journal)
                    if job['state'] == 'running': job.update(state='failed',verdict='network')
                    self.transports.discard(fetcher)
        if not self.start(perform):
            fetcher.close();self.transports.discard(fetcher)
            job.update(state='failed',verdict='busy')
        return copy.deepcopy(job)

    def close(self):
        with self.c.lock:
            if self.closed: return
            self.closed = True;self.cancel()
            workers = list(self.workers)
        for worker in workers: worker.join(timeout=2)
        self.store.close()
