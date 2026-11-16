"""Bounded instrument-network Alpaca discovery and GET-only transport."""
import http.client
import ipaddress
import json
import re
import socket
import threading
import time
from urllib.parse import urlencode

MEMBERS = {'telescope': ('rightascension', 'declination', 'tracking', 'slewing', 'atpark'),
           'camera': ('camerastate', 'ccdtemperature')}
MAX_BODY = 64 * 1024


def fields(payload):
    try: value = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError): raise ValueError('Invalid Alpaca response') from None
    if not isinstance(value, dict): raise ValueError('Invalid Alpaca response')
    return {key.lower(): item for key, item in value.items()}



class Transport:
    """IPv4 only, interface/namespace bound, cancellable; no proxy or redirects."""
    def __init__(self, address, interface, factory=None, *, target=None, permission=None):
        self.address = ipaddress.IPv4Interface(address)
        self.target = str(ipaddress.IPv4Address(target)) if target is not None else None
        if not isinstance(interface, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", interface):
            raise ValueError("Invalid observation interface")
        self.interface, self.factory = interface, factory
        self.permission = permission
        self.lock = threading.Lock()
        self.sockets = set()
        self.closed = False

    def stream(self, kind):
        stream = self.factory(kind) if self.factory else socket.socket(socket.AF_INET, kind)
        with self.lock:
            if self.closed:
                stream.close()
                raise ConnectionError('Alpaca observation stopped')
            self.sockets.add(stream)
        try:
            if self.factory is None:
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.interface.encode()+b'\0')
            stream.settimeout(2)
            stream.bind((str(self.address.ip), 0))
            return stream
        except BaseException:
            self.release(stream)
            raise

    def release(self, stream):
        with self.lock: self.sockets.discard(stream)
        stream.close()
        if self.permission: self.permission.release(stream)

    def peer(self, host):
        host = ipaddress.IPv4Address(host)
        network = self.address.network
        return (self.target is None or str(host) == self.target) and host in network and host not in (self.address.ip, network.network_address, network.broadcast_address)

    def discover(self):
        stream = self.stream(socket.SOCK_DGRAM)
        endpoints = set()
        granted = False
        try:
            stream.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if self.permission:
                self.permission.discovery(stream.getsockname()[1])
                granted = True
            stream.sendto(b'alpacadiscovery1', (str(self.address.network.broadcast_address), 32227))
            deadline = time.monotonic()+2
            for _ in range(32):
                remaining = deadline-time.monotonic()
                if remaining <= 0: break
                stream.settimeout(remaining)
                try: payload, peer = stream.recvfrom(1025)
                except socket.timeout: break
                if len(payload) > 1024 or not self.peer(peer[0]): continue
                try: port = fields(payload).get('alpacaport')
                except ValueError: continue
                if type(port) is int and 1 <= port <= 65535:
                    endpoints.add((str(ipaddress.IPv4Address(peer[0])), port))
                if len(endpoints) >= 4: break
            return sorted(endpoints)
        finally:
            self.release(stream)
            if self.permission and granted:
                try: self.permission.discovery()
                except (OSError, ValueError): pass

    def allow(self, endpoints):
        if self.permission: self.permission.endpoints(endpoints)

    def get(self, endpoint, kind, number, member, client_id, transaction):
        if kind is None:
            if member != 'configureddevices' or number is not None: raise ValueError('Alpaca member refused')
            path = '/management/v1/configureddevices'
        else:
            if kind not in MEMBERS or member not in MEMBERS[kind] or type(number) is not int or not 0 <= number <= 65535:
                raise ValueError('Alpaca member refused')
            path = '/api/v1/'+kind+'/'+str(number)+'/'+member
        host, port = endpoint
        if not self.peer(host) or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Alpaca endpoint refused')
        path += '?'+urlencode({'ClientID':client_id, 'ClientTransactionID':transaction})
        connection = http.client.HTTPConnection(host, port, timeout=2)
        def refused(): raise ConnectionError('Alpaca bound socket unavailable')
        connection.connect = refused
        stream = self.stream(socket.SOCK_STREAM)
        response = None
        watchdog = threading.Timer(5, self.interrupt, args=(stream,))
        watchdog.daemon = True
        try:
            watchdog.start()
            stream.connect(endpoint)
            # Keep our cancellable descriptor even when HTTP/1.0 detaches the
            # connection socket into the response's buffered reader.
            connection.sock = stream.dup()
            connection.request('GET', path, headers={'Accept':'application/json', 'Connection':'close'})
            response = connection.getresponse()
            if response.status != 200: raise ValueError('Alpaca HTTP unavailable')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdigit() or len(length) > 9 or int(length) > MAX_BODY):
                raise ValueError('Alpaca response too large')
            # read1 gives cancellation/absolute-deadline checks even with a slow peer.
            payload = bytearray()
            deadline = time.monotonic()+3
            while len(payload) <= MAX_BODY:
                remaining = deadline-time.monotonic()
                if remaining <= 0: raise TimeoutError('Alpaca response timeout')
                chunk = response.read1(min(8192, MAX_BODY+1-len(payload)))
                if not chunk: break
                payload.extend(chunk)
            if len(payload) > MAX_BODY: raise ValueError('Alpaca response too large')
            result = fields(payload)
            if type(result.get('clienttransactionid')) is not int or result['clienttransactionid'] != transaction:
                raise ValueError('Alpaca transaction mismatch')
            error = result.get('errornumber')
            if type(error) is not int: raise ValueError('Invalid Alpaca error')
            # ErrorMessage and every other unknown field die on this call stack.
            return error, result.get('value')
        finally:
            watchdog.cancel()
            if response is not None: response.close()
            connection.close()
            self.release(stream)

    @staticmethod
    def interrupt(stream):
        try: stream.shutdown(socket.SHUT_RDWR)
        except OSError: pass

    def close(self):
        with self.lock:
            self.closed = True
            streams = list(self.sockets)
        for stream in streams:
            self.interrupt(stream)
            stream.close()
        if self.permission: self.permission.close()
