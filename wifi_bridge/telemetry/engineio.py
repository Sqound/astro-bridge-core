"""Bounded, binary-less Engine.IO 3 polling; never emits Socket.IO commands."""
from collections import deque
import http.client
import ipaddress
import json
import re
import socket
import threading
import time
from urllib.parse import urlencode

MAX_PAYLOAD = 256 * 1024
MINUTE_BYTES = 2 * 1024 * 1024
MINUTE_REQUESTS = 240


class ProtocolError(ValueError):
    pass


class PeerClosed(ConnectionError):
    pass


def frames(payload):
    """Yield text packets, using the protocol's JavaScript UTF-16 lengths."""
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_PAYLOAD:
        raise ProtocolError('Invalid polling payload size')
    try: text = payload.decode('utf-8')
    except UnicodeError: raise ProtocolError('Invalid polling text') from None
    offset = 0
    while offset < len(text):
        colon = text.find(':', offset, offset+8)
        if colon < 0 or not re.fullmatch(r'[0-9]{1,6}', text[offset:colon]):
            raise ProtocolError('Invalid polling frame')
        length = int(text[offset:colon])
        start = end = colon+1
        units = 0
        while end < len(text) and units < length:
            units += 2 if ord(text[end]) > 0xffff else 1
            end += 1
        if units != length or end == start or text[start] not in '0123456':
            raise ProtocolError('Invalid polling packet')
        yield text[start:end]
        offset = end


def encode(packet):
    return (str(len(packet.encode('utf-16-le'))//2)+':'+packet).encode('utf-8')


class Budget:
    """At most 61 second buckets; a reconnect does not reset the rolling budget."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.buckets = deque()
        self.requests = deque()
        self.lock = threading.Lock()

    def add(self, size):
        with self.lock:
            now = int(self.clock())
            while self.buckets and self.buckets[0][0] < now-60: self.buckets.popleft()
            if size < 0 or sum(row[1] for row in self.buckets)+size > MINUTE_BYTES:
                raise ProtocolError('Polling byte budget exceeded')
            if self.buckets and self.buckets[-1][0] == now: self.buckets[-1][1] += size
            else: self.buckets.append([now, size])

    def reserve(self, maximum):
        with self.lock:
            now = int(self.clock())
            while self.buckets and self.buckets[0][0] < now-60: self.buckets.popleft()
            count = min(maximum, MINUTE_BYTES-sum(row[1] for row in self.buckets))
            if count <= 0: raise ProtocolError('Polling byte budget exceeded')
            if not self.buckets or self.buckets[-1][0] != now: self.buckets.append([now,0])
            bucket = self.buckets[-1]
            bucket[1] += count
            return bucket, count

    def refund(self, bucket, count):
        with self.lock: bucket[1] -= count

    def request(self):
        with self.lock:
            now = int(self.clock())
            while self.requests and self.requests[0][0] < now-60: self.requests.popleft()
            if sum(row[1] for row in self.requests) >= MINUTE_REQUESTS:
                raise ProtocolError('Polling request budget exceeded')
            if self.requests and self.requests[-1][0] == now: self.requests[-1][1] += 1
            else: self.requests.append([now,1])


class PollingHTTP:
    """Direct IPv4 sockets only: no DNS, proxy, redirect or cookie handling."""
    def __init__(self, target, interface=None, *, port=8083, factory=None):
        self.target = str(ipaddress.IPv4Address(target))
        self.port = port
        self.interface = interface
        if interface is not None and not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', interface):
            raise ValueError('Invalid observation interface')
        self.factory = factory
        self.lock = threading.Lock()
        self.connections = {}
        self.closed = False
        self.budget = None

    def request(self, method, path, body=None, *, timeout=5):
        connection = http.client.HTTPConnection(self.target, self.port, timeout=5)
        def refuse_reconnect():
            # http.client must never replace our bound socket with an unbound one.
            raise ConnectionError('Polling socket unavailable')
        connection.connect = refuse_reconnect
        with self.lock:
            if self.closed: raise ConnectionError('Polling stopped')
            self.connections[connection] = None
        try:
            stream = self.factory(socket.SOCK_STREAM) if self.factory else socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            with self.lock:
                if self.closed:
                    stream.close()
                    raise ConnectionError('Polling stopped')
                connection.sock = stream
                self.connections[connection] = stream
            if self.interface and self.factory is None:
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.interface.encode()+b'\0')
            stream.settimeout(5)
            stream.connect((self.target, self.port))
            stream.settimeout(timeout)
            connection.request(method, path, body, {'Content-Type':'text/plain;charset=UTF-8', 'Connection':'close'})
            response = connection.getresponse()
            if response.status != 200: raise ProtocolError('Polling HTTP refused')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdigit() or int(length) > MAX_PAYLOAD):
                raise ProtocolError('Polling HTTP size exceeded')
            payload = bytearray()
            while len(payload) <= MAX_PAYLOAD:
                bucket, count = self.budget.reserve(min(8192, MAX_PAYLOAD+1-len(payload)))
                try: chunk = response.read(count)
                except BaseException:
                    # Keep the reservation charged when partial I/O is unknowable.
                    raise
                self.budget.refund(bucket, count-len(chunk))
                payload.extend(chunk)
                if len(chunk) < count: break
            if len(payload) > MAX_PAYLOAD: raise ProtocolError('Polling HTTP size exceeded')
            return bytes(payload)
        finally:
            with self.lock:
                self.connections.pop(connection, None)
                connection.close()

    def close(self):
        with self.lock:
            self.closed = True
            for stream in self.connections.values():
                if stream is not None:
                    try: stream.shutdown(socket.SHUT_RDWR)
                    except OSError: pass


class Client:
    def __init__(self, transport, *, budget=None, clock=time.monotonic):
        self.transport, self.clock = transport, clock
        self.budget = budget or Budget(clock)
        if isinstance(transport, PollingHTTP): transport.budget = self.budget
        self.sid = None
        self.interval = self.timeout = None
        self.next_ping = self.deadline = None
        self.lock = threading.RLock()
        self.posts = threading.Lock()
        self.done = threading.Event()
        self.failure = None
        self.heartbeat_thread = None

    def path(self):
        args = {'EIO':3, 'transport':'polling', 'b64':1}
        if self.sid is not None: args['sid'] = self.sid
        return '/socket.io/?'+urlencode(args)

    def request(self, packet=None):
        if packet is not None and packet not in ('2', '3'):
            raise ProtocolError('Observation write refused')
        self.budget.request()
        body = encode(packet) if packet is not None else None
        if body: self.budget.add(len(body))
        payload = self.transport.request('POST' if body else 'GET', self.path(), body,
                                         timeout=5 if body or self.sid is None else self.interval+self.timeout+5)
        if not isinstance(self.transport, PollingHTTP): self.budget.add(len(payload))
        if body:
            if payload != b'ok': raise ProtocolError('Polling POST refused')
            return None
        return payload

    def send(self, packet):
        # Keep the write surface narrower than a general Socket.IO client.
        if packet not in ('2', '3'): raise ProtocolError('Observation write refused')
        with self.posts: self.request(packet)

    def open(self, packet):
        if self.sid is not None: raise ProtocolError('Duplicate polling open')
        try: data = json.loads(packet[1:])
        except (ValueError, RecursionError): raise ProtocolError('Invalid polling open') from None
        if not isinstance(data, dict) or not isinstance(data.get('sid'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', data['sid']):
            raise ProtocolError('Invalid polling session')
        for key in ('pingInterval', 'pingTimeout'):
            if type(data.get(key)) is not int or not 250 <= data[key] <= 120000:
                raise ProtocolError('Invalid polling heartbeat')
        self.sid = data['sid']
        self.interval, self.timeout = data['pingInterval']/1000, data['pingTimeout']/1000
        self.next_ping = self.clock()+self.interval

    def receive(self, payload, callback):
        if not payload: raise PeerClosed('Observation peer closed')
        for packet in frames(payload):
            if self.sid is None:
                if packet[0] != '0': raise ProtocolError('Missing polling open')
                self.open(packet)
            elif packet == '1' or packet == '41': raise PeerClosed('Observation peer closed')
            elif packet == '3':
                with self.lock:
                    if self.deadline is not None:
                        self.deadline = None
                        self.next_ping = self.clock()+self.interval
            elif packet == '2': self.send('3')
            elif packet.startswith('42'):
                try: data = json.loads(packet[2:])
                except (ValueError, RecursionError): raise ProtocolError('Invalid observation event') from None
                # No namespace/ack/binary support and no arbitrary event fallback.
                if isinstance(data, list) and len(data) == 2 and data[0] == 'STATUS_UPDATED' and isinstance(data[1], dict):
                    callback(data[1])
            elif packet not in ('40', '6'): raise ProtocolError('Unsupported polling packet')

    def heartbeat(self):
        with self.lock:
            now = self.clock()
            if self.deadline is not None:
                if now >= self.deadline: raise TimeoutError('Polling pong timeout')
            elif self.sid is not None and now >= self.next_ping:
                self.deadline = now+self.timeout
                self.send('2')

    def _heartbeats(self):
        while not self.done.wait(min(.1, self.interval/2, self.timeout/2)):
            try: self.heartbeat()
            except (OSError, ValueError, http.client.HTTPException) as exc:
                self.failure = exc
                self.close()

    def run(self, callback):
        try:
            self.receive(self.request(), callback)
            self.heartbeat_thread = threading.Thread(target=self._heartbeats, name='bridge-poll-heartbeat', daemon=True)
            self.heartbeat_thread.start()
            while not self.done.is_set(): self.receive(self.request(), callback)
            if self.failure: raise self.failure
        except (OSError, ValueError, http.client.HTTPException):
            if self.failure: raise self.failure
            raise
        finally: self.close()

    def close(self):
        self.done.set()
        self.transport.close()
        thread = self.heartbeat_thread
        if thread and thread is not threading.current_thread(): thread.join(timeout=6)
