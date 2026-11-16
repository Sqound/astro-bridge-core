"""Reconcile Alpaca listeners with confirmed home/hotspot management paths."""
import threading
import time

from .alpaca import AlpacaError
from .listener import AlpacaServer, Discovery, ManagementAccess


class CameraRuntime:
    """The provider returns sanitized paths/hosts; it never probes hardware.

    At most two management paths are served. Failed binds retry after five
    seconds; only successfully bound TCP listeners publish an Alpaca endpoint.
    """
    def __init__(self, service, snapshot, *, server_factory=AlpacaServer,
                 discovery_factory=Discovery, clock=time.monotonic):
        self.service, self.snapshot, self.clock = service, snapshot, clock
        self.server_factory, self.discovery_factory = server_factory, discovery_factory
        self.listeners = {}
        self.retry = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    @staticmethod
    def plans(snapshot, port):
        paths = snapshot.get('paths', [])
        hosts = snapshot.get('hosts', [])
        if not isinstance(paths, (list, tuple)) or len(paths) > 2 or not isinstance(hosts, (list, tuple, set, frozenset)) or len(hosts) > 32:
            return {}
        result = {}
        for path in paths:
            if not isinstance(path, dict) or path.get('role') not in ('home', 'ap', 'hotspot'):
                continue
            try:
                access = ManagementAccess(path['ip'], path['subnet'], hosts, path.get('interface'))
            except (KeyError, ValueError, TypeError):
                continue
            key = (access.address, str(access.subnet), access.interface, tuple(sorted(access.hosts)), port)
            if any(existing[0] == access.address for existing in result):
                continue
            result[key] = access
        return result

    def sync(self):
        with self._lock:
            settings = self.service.get_settings()
            paths = self.plans(self.snapshot(), settings['port']) if (self.service.enabled() or settings['indi']) and not self._stop.is_set() else {}
            if settings['indi']:
                try:
                    self.service.sync_indi(tuple(paths.values()))
                except (AlpacaError, OSError):
                    pass  # Worker watchdog withdraws listeners on missed heartbeats.
            desired = paths if self.service.enabled() else {}
            if self._stop.is_set():
                desired = {}
            if set(desired) != set(self.listeners):
                self.service.set_listening(None)
            for key in set(self.listeners) - set(desired):
                self._remove(key)
            self.retry = {key: deadline for key, deadline in self.retry.items() if key in desired}
            for key, access in desired.items():
                if self._stop.is_set():
                    break
                if key in self.listeners or self.clock() < self.retry.get(key, 0):
                    continue
                server = discovery = None
                thread = None
                try:
                    server = self.server_factory(access, self.service, port=settings['port'])
                    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1},
                                              name='alpaca-http', daemon=True)
                    thread.start()
                    discovery = self.discovery_factory(access, self.service)
                    discovery.start()
                except OSError:
                    # UDP discovery failure cannot advertise an unreachable server.
                    # Roll back the whole path; retry a bounded time later.
                    if discovery:
                        discovery.close()
                    if server:
                        if thread:
                            server.shutdown()
                        server.server_close()
                    if thread:
                        thread.join(timeout=0.5)
                    self.retry[key] = self.clock() + 5
                    continue
                self.listeners[key] = (server, thread, discovery)
            port = None if self._stop.is_set() else next((server.server_port for server, _, _ in self.listeners.values()), None)
            self.service.set_listening(port, [key[0] for key in self.listeners])

    def _remove(self, key):
        server, thread, discovery = self.listeners.pop(key)
        discovery.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=0.5)

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name='camera-listeners', daemon=True)
        self._thread.start()

    def _run(self):
        try:
            while not self._stop.is_set():
                self.sync()
                self._stop.wait(0.5)
        finally:
            with self._lock:
                self.service.set_listening(None)
                for key in list(self.listeners):
                    self._remove(key)

    def close(self):
        self._stop.set()
        self.service.set_listening(None)
        if self._thread:
            self._thread.join(timeout=2)
        elif self._lock.acquire(timeout=0.1):
            try:
                for key in list(self.listeners):
                    self._remove(key)
            finally:
                self._lock.release()


def management_snapshot(controller):
    """Reuse confirmed paths; explicit demo cameras can bind loopback only."""
    with controller.lock:
        paths = ([{'role':'home','ip':'127.0.0.1','subnet':'127.0.0.0/8'}] if controller.demo
                 else [dict(path) for path in controller.state.get('management', {}).get('paths', [])])
        hosts = [controller.store.config['hostname'] + '.local']
        hosts.extend(getattr(controller, 'management_hosts', ()))
        return {'paths': paths, 'hosts': hosts}
