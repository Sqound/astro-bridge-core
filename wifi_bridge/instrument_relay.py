"""Bounded byte-transparent TCP/UDP relays. No manufacturer commands or payload logs."""
from collections import deque
from datetime import datetime, timezone
import select
import math
import selectors
import socket
import threading
import time

from .metrics import ForwardingMetrics, IngressTimings


COUNTER_MAX = 2**53 - 1


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class RelayCounters:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.last = self.started
        self.last_exchange = None
        self.sent = self.received = self.sessions = self.errors = 0
        self.pings = deque(maxlen=120)
        self.probe_error = None

    def exchange(self, size, upstream):
        with self.lock:
            if upstream: self.sent += size
            else: self.received += size
            self.last = time.monotonic()
            self.last_exchange = utc_now()

    def session(self, delta):
        with self.lock: self.sessions += delta

    def error(self):
        with self.lock: self.errors += 1

    def ping(self, latency):
        with self.lock:
            self.probe_error = None
            self.pings.append((time.monotonic(), latency))

    def snapshot(self, quiet=120, cooldown=60):
        import statistics
        with self.lock:
            now = time.monotonic()
            window = [value for stamp, value in self.pings if now - stamp <= 600]
            replies = [value for value in window if value is not None]
            age = max(0, now - self.last)
            return {'bytes_to_instrument': self.sent, 'bytes_from_instrument': self.received,
                    'bytes': self.sent + self.received, 'packets': None, 'tcp_established': self.sessions,
                    'relay_errors': self.errors, 'last_exchange': self.last_exchange,
                    'state': 'quiet' if age >= quiet else 'active' if self.last_exchange else 'unknown',
                    'reason': 'QUIET_WINDOW_OBSERVED' if age >= quiet else 'MOUNT_TRANSPORT_ACTIVE' if self.last_exchange else 'WARMING_UP',
                    'consultation': 'mount_control' if self.last_exchange and age < quiet else 'none',
                    'sample_age_seconds': 0, 'quiet_seconds': int(age), 'quiet_required_seconds': quiet,
                    'cooldown_seconds': cooldown, 'cooldown_remaining': max(0, math.ceil(cooldown-(now-self.started))), 'last_activity_age_seconds': int(age),
                    'classifier': 'isolated_relay_bytes_v1', 'calibrated': False,
                    'ping': {'method': 'icmp_echo', 'samples': len(window), 'replies': len(replies),
                             'median_ms': round(statistics.median(replies), 3) if replies else None,
                             'loss_percent': round(100 * (len(window)-len(replies))/len(window), 3) if window else None,
                             'window_seconds': 600, 'available': bool(replies) and self.probe_error is None,
                             'reason': self.probe_error or (None if replies else 'NO_ICMP_REPLY' if window else 'NOT_MEASURED')}}


class UdpOwner:
    """One controller per instrument, shared by every management ingress path.

    Hold lock through socket I/O so explicit release cannot race an in-flight
    forward. Epoch identity invalidates pending replies even when seq saturates.
    """
    def __init__(self, owner_idle_seconds=20, clock=time.monotonic):
        if (isinstance(owner_idle_seconds, bool) or not isinstance(owner_idle_seconds, (int, float))
                or not math.isfinite(owner_idle_seconds) or not 0 < owner_idle_seconds <= 3600):
            raise ValueError('Invalid UDP owner idle interval')
        self.owner_idle_seconds = owner_idle_seconds
        self.clock = clock
        self.lock = threading.RLock()
        self.origin = None
        self.last = 0
        self.seq = 0
        self.epoch = None
        self.refused_clients = 0

    def _expire(self):
        if self.origin is not None and self.clock() - self.last >= self.owner_idle_seconds:
            self.origin = self.epoch = None

    def accept(self, origin):
        with self.lock:
            self._expire()
            if self.origin is not None and self.origin != origin:
                # Count refused datagrams, never retain an unbounded client set.
                self.refused_clients = min(COUNTER_MAX, self.refused_clients + 1)
                return None
            if self.origin is None:
                self.seq = min(COUNTER_MAX, self.seq + 1)
                self.origin, self.epoch = origin, object()
            self.last = self.clock()
            return self.epoch

    def current(self, epoch):
        with self.lock:
            self._expire()
            return epoch is not None and epoch is self.epoch

    def release(self):
        with self.lock:
            self.origin = self.epoch = None
            return self.snapshot()

    def snapshot(self):
        with self.lock:
            self._expire()
            return {'owner': {'address': self.origin[0] + ':' + str(self.origin[1]),
                              'since_seq': self.seq} if self.origin else None,
                    'refused_clients': self.refused_clients,
                    'owner_idle_seconds': self.owner_idle_seconds}


class InstrumentRelay:
    """One explicit ingress address/device and one fixed upstream endpoint.

    socket_factory creates a socket in the instrument namespace. The socket stays
    there after its creating thread returns to the management namespace.
    """
    def __init__(self, host, port, transport, target, target_port, socket_factory,
                 counters=None, interface=None, subnet=None, max_clients=8,
                 owner=None, owner_idle_seconds=20):
        import ipaddress
        self.host, self.port, self.transport = host, port, transport
        self.target, self.target_port = target, target_port
        self.socket_factory = socket_factory
        self.counters = counters or RelayCounters()
        self.metrics = ForwardingMetrics()
        self.interface = interface
        self.subnet = ipaddress.ip_network(subnet) if subnet else None
        self.max_clients = max_clients
        self.owner = (owner or UdpOwner(owner_idle_seconds)) if transport == 'udp' else None
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.clients = set()
        self.thread = None
        self.listener = None

    def start(self):
        stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM if self.transport == 'tcp' else socket.SOCK_DGRAM)
        try:
            stream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if self.interface:
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.interface.encode()+b'\0')
            stream.bind((self.host, self.port))
            if self.transport == 'tcp': stream.listen(self.max_clients)
            stream.setblocking(False)
        except BaseException:
            stream.close()
            raise
        self.listener = stream
        self.port = stream.getsockname()[1]
        self.thread = threading.Thread(target=self._accept if self.transport == 'tcp' else self._udp,
                                       name='instrument-relay', daemon=True)
        self.thread.start()
        return self

    def _allowed(self, origin):
        import ipaddress
        return not self.subnet or (ipaddress.ip_address(origin[0]) in self.subnet
                                  and origin[0] != str(self.subnet.broadcast_address)
                                  and origin[0] != str(self.subnet.network_address))

    def _accept(self):
        while not self.stop.is_set():
            try:
                if not select.select([self.listener], [], [], .2)[0]: continue
                client, origin = self.listener.accept()
                with self.lock:
                    if not self._allowed(origin) or len(self.clients) >= self.max_clients:
                        self.metrics.drop(); client.close(); continue
                    self.clients.add(client)
                threading.Thread(target=self._tcp, args=(client,), name='instrument-tcp', daemon=True).start()
            except (OSError, ValueError):
                if not self.stop.is_set():
                    self.counters.error(); self.metrics.drop()
                break

    def _tcp(self, client):
        upstream = None
        counted = False
        try:
            upstream = self.socket_factory(socket.SOCK_STREAM)
            upstream.settimeout(3)
            upstream.connect((self.target, self.target_port))
            client.setblocking(False); upstream.setblocking(False)
            self.counters.session(1); counted = True
            # Each direction has a bounded pending buffer; do not interpret '#'.
            pairs = {client: upstream, upstream: client}
            buffers = {client: bytearray(), upstream: bytearray()}
            pending = {client: IngressTimings(), upstream: IngressTimings()}
            eof = set(); shut = set(); last = time.monotonic()
            while not self.stop.is_set() and time.monotonic() - last < 3600:
                for source, destination in pairs.items():
                    if source in eof and not buffers[destination] and destination not in shut:
                        try: destination.shutdown(socket.SHUT_WR)
                        except OSError: pass
                        shut.add(destination)
                if len(eof) == 2 and not any(buffers.values()): break
                readable = [stream for stream in pairs if stream not in eof and len(buffers[pairs[stream]]) < 65536]
                writable = [stream for stream, data in buffers.items() if data]
                reads, writes, _ = select.select(readable, writable, [], .2)
                for stream in reads:
                    try: data = stream.recv(min(16384, 65536-len(buffers[pairs[stream]])))
                    except BlockingIOError: continue
                    if not data: eof.add(stream)
                    else:
                        buffers[pairs[stream]].extend(data); last = time.monotonic()
                        pending[pairs[stream]].receive(len(data), last)
                for stream in writes:
                    try: size = stream.send(buffers[stream])
                    except BlockingIOError: continue
                    if size <= 0: raise OSError('Relay socket closed')
                    del buffers[stream][:size]
                    pending[stream].forward(size, self.metrics, stream is upstream)
                    self.counters.exchange(size, stream is upstream)
                    last = time.monotonic()
        except (OSError, ValueError):
            if not self.stop.is_set():
                self.counters.error(); self.metrics.drop()
        finally:
            if upstream is not None: upstream.close()
            client.close()
            if counted: self.counters.session(-1)
            with self.lock: self.clients.discard(client)

    def _udp(self):
        selector = selectors.DefaultSelector()
        upstream = None
        epoch = None
        selector.register(self.listener, selectors.EVENT_READ, None)
        def clear():
            nonlocal upstream, epoch
            if upstream is not None:
                selector.unregister(upstream); upstream.close()
            upstream = epoch = None
        try:
            while not self.stop.is_set():
                with self.owner.lock:
                    if not self.owner.current(epoch): clear()
                for event, _ in selector.select(.2):
                    try:
                        with self.owner.lock:
                            if event.fileobj is self.listener:
                                data, origin = self.listener.recvfrom(65535)
                                started = time.monotonic()
                                if not self._allowed(origin) or len(data) > 65507:
                                    self.metrics.drop(); continue
                                accepted = self.owner.accept(origin)
                                if accepted is None:
                                    self.metrics.drop(); continue
                                if accepted is not epoch:
                                    clear()
                                    upstream = self.socket_factory(socket.SOCK_DGRAM)
                                    try:
                                        upstream.connect((self.target, self.target_port))
                                        upstream.setblocking(False)
                                        selector.register(upstream, selectors.EVENT_READ, accepted)
                                    except BaseException:
                                        upstream.close(); upstream = None; raise
                                    epoch = accepted
                                size = upstream.send(data)
                                self.counters.exchange(size, True)
                                self.metrics.forward(size, True, started, datagram=True)
                            elif event.fileobj is upstream and self.owner.current(event.data):
                                # Connected sockets reject foreign sources. An old
                                # epoch's delayed reply can never reach a new owner.
                                data = upstream.recv(65535)
                                started = time.monotonic()
                                size = self.listener.sendto(data, self.owner.origin)
                                self.counters.exchange(size, False)
                                self.metrics.forward(size, False, started, datagram=True)
                    except (OSError, ValueError):
                        if not self.stop.is_set():
                            self.counters.error(); self.metrics.drop()
                        clear()
        finally:
            clear()
            selector.close()

    def healthy(self):
        return self.thread is not None and self.thread.is_alive() and not self.stop.is_set()

    def close(self):
        self.stop.set()
        if self.thread: self.thread.join(timeout=1)
        if self.listener: self.listener.close()
        with self.lock:
            for client in list(self.clients):
                try: client.shutdown(socket.SHUT_RDWR)
                except OSError: pass
        # TCP workers observe the stop event within their bounded connect/select.
