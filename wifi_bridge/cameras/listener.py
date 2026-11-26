"""Dedicated management-only Alpaca TCP/UDP listeners. No Bearer authentication."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import re
import socket
import struct
import sys
import threading
import time
from urllib.parse import parse_qsl, urlsplit

from .alpaca import AlpacaError, envelope, image_bytes, image_json, number
from .frames import Frame


class ManagementAccess:
    """Explicit IPv4 home/hotspot binding and ingress networks; never wildcard.

    Runtime supplies fresh management paths. Optional subnet restrictions only
    narrow those paths, never grant access to an instrument-facing network.
    """
    def __init__(self, address, subnet, hosts, interface=None):
        if interface is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface):
            raise ValueError("invalid_camera_management_interface")
        self.interface = interface
        self.address = str(ipaddress.IPv4Address(address))
        self.subnet = ipaddress.IPv4Network(subnet, strict=False)
        ip = ipaddress.IPv4Address(self.address)
        if ip.is_unspecified or ip.is_multicast or ip not in self.subnet:
            raise ValueError('invalid_camera_management_binding')
        self.hosts = frozenset(str(host).lower() for host in hosts) | {self.address}

    def peer(self, address, allowed):
        try:
            ip = ipaddress.IPv4Address(address)
            return ip in self.subnet and (not allowed or any(ip in ipaddress.ip_network(net) for net in allowed))
        except ValueError:
            return False

    def host(self, values, port):
        if len(values) != 1:
            return False
        value = values[0]
        if not value or len(value) > 255 or any(c.isspace() for c in value):
            return False
        try:
            parsed = urlsplit('//' + value)
            return (parsed.username is None and parsed.password is None and not parsed.path and
                    not parsed.query and not parsed.fragment and not value.endswith(':') and
                    parsed.hostname in self.hosts and (parsed.port or 80) == port)
        except ValueError:
            return False


class AlpacaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, access, service, port=None):
        self.access, self.service = access, service
        self._slots = threading.BoundedSemaphore(8)
        self._transactions = threading.Lock()
        self._transaction = 0
        super().__init__((access.address, service.get_settings()['port'] if port is None else port), AlpacaHandler)

    def next_transaction(self):
        with self._transactions:
            self._transaction = (self._transaction % 4294967295) + 1
            return self._transaction

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        pass  # No request, raw payload or client address logs.


class AlpacaHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def send(self, status, payload):
        data = json.dumps(payload, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.handle_alpaca('GET')

    def do_PUT(self):
        self.handle_alpaca('PUT')

    def handle_alpaca(self, method):
        client_transaction, transaction = 0, self.server.next_transaction()
        try:
            service, access = self.server.service, self.server.access
            current = service.get_settings()
            if not service.enabled() or not access.peer(self.client_address[0], current['allowed_subnets']):
                self.send(403, envelope(0, transaction, error=0x501))
                return
            if not access.host(self.headers.get_all('Host', []), self.server.server_port):
                self.send(421, envelope(0, transaction, error=0x401))
                return
            if len(self.path) > 2048 or self.headers.get('Transfer-Encoding'):
                raise AlpacaError(0x401)
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise AlpacaError(0x401)
            raw = parsed.query
            if method == 'PUT':
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1 or self.headers.get_content_type() != 'application/x-www-form-urlencoded' or raw:
                    raise AlpacaError(0x401)
                length = number(lengths[0], 0, 4096)
                raw = self.rfile.read(length).decode('ascii')
            params = {}
            for key, value in parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=24):
                key = key.lower()
                if key in params:
                    raise AlpacaError(0x401)
                params[key] = value
            client_transaction = number(params.get('clienttransactionid', '0'))
            client = number(params.get('clientid', '0'))
            if method == 'GET' and parsed.path == '/management/apiversions':
                value = [1]
            elif method == 'GET' and parsed.path == '/management/v1/description':
                value = dict(ServerName='Astro Bridge', Manufacturer='Astro Bridge', ManufacturerVersion='1.0', Location='Local')
            elif method == 'GET' and parsed.path in ('/management/v1/configureddevices', '/configureddevices'):
                value = service.configured_devices()
            else:
                match = re.fullmatch(r'/api/v1/camera/([0-9]{1,10})/([a-z]+)', parsed.path)
                if not match:
                    self.send(404, envelope(client_transaction, transaction, error=0x400))
                    return
                value = service.execute(number(match[1]), method, match[2], params, client, self.client_address[0])
            if isinstance(value, Frame):
                binary = accepts_imagebytes(self.headers.get('Accept', ''))
                if not binary and value.width * value.height > 1_000_000:
                    raise AlpacaError(0x500)
                self.send_response(200)
                self.send_header('Content-Type', 'application/imagebytes' if binary else 'application/json')
                self.send_header('Cache-Control', 'no-store')
                if binary:
                    self.send_header('Content-Length', str(44 + len(value.data)))
                self.end_headers()
                chunks = image_bytes(value, client_transaction, transaction) if binary else image_json(value, client_transaction, transaction)
                for chunk in chunks:
                    self.wfile.write(chunk)
                return
            self.send(200, envelope(client_transaction, transaction, value, include_value=method == 'GET'))
        except AlpacaError as error:
            if method == 'GET' and urlsplit(self.path).path.endswith('/imagearray') and accepts_imagebytes(self.headers.get('Accept', '')):
                message = str(error).encode('utf-8')
                data = struct.pack('<11I', 1, error.number, client_transaction, transaction, 44, 0, 0, 0, 0, 0, 0) + message
                self.send_response(200)
                self.send_header('Content-Type', 'application/imagebytes')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send(200, envelope(client_transaction, transaction, error=error.number, message=str(error)))
        except (ValueError, UnicodeError):
            self.send(400, envelope(client_transaction, transaction, error=0x401))
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass


def accepts_imagebytes(value):
    for part in value.lower().split(','):
        media, *options = part.strip().split(';')
        if media == 'application/imagebytes':
            try:
                return all(float(option.strip()[2:]) > 0 for option in options if option.strip().startswith('q='))
            except ValueError:
                return False
    return False


class Discovery:
    """One responder on each explicitly supplied management address, UDP 32227."""
    def __init__(self, access, service, *, clock=time.monotonic, port=32227):
        self.access, self.service, self.clock = access, service, clock
        self.port = port
        self.socket = None
        self.thread = None
        self.stop = threading.Event()
        self._last = {}
        self._window, self._count = 0.0, 0

    def response(self, payload, address):
        settings = self.service.get_settings()
        port = self.service.listening_port(self.access.address)
        if not settings['discover'] or port is None or payload != b'alpacadiscovery1' or not self.service.enabled() or not self.access.peer(address, settings['allowed_subnets']):
            return None
        now = self.clock()
        if now - self._window >= 1:
            self._window, self._count = now, 0
        if self._count >= 8 or now - self._last.get(address, -1e9) < 1:
            return None
        if len(self._last) >= 64:
            self._last = {key: stamp for key, stamp in self._last.items() if now - stamp < 1}
        self._last[address] = now
        self._count += 1
        return json.dumps({'AlpacaPort': port}, separators=(',', ':')).encode()

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if self.access.interface is not None:
                if not sys.platform.startswith('linux'):
                    raise OSError('camera_interface_binding_unavailable')
                # Broadcast discovery needs the wildcard destination on this one
                # management interface; SO_BINDTODEVICE excludes every other NIC.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.SOL_SOCKET, getattr(socket, 'SO_BINDTODEVICE', 25),
                                self.access.interface.encode() + b'\0')
                sock.bind(('', self.port))
            else:
                sock.bind((self.access.address, self.port))
            sock.settimeout(0.25)
        except OSError:
            sock.close()
            raise
        self.socket = sock
        self.thread = threading.Thread(target=self._run, name='alpaca-discovery', daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                data, address = self.socket.recvfrom(64)
                reply = self.response(data, address[0])
                if reply:
                    self.socket.sendto(reply, address)
            except socket.timeout:
                continue
            except (OSError, AlpacaError):
                return

    def close(self):
        self.stop.set()
        if self.socket:
            self.socket.close()
        if self.thread:
            self.thread.join(timeout=1)
