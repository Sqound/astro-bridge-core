"""One TCP access point for one serial device (for example a NexStar hand control).

Bytes are relayed untouched in both directions. Two safety rules:

1. One client per port. While a client holds the link every other connection is
   closed at once, without a single byte exchanged, and counted and journaled.
2. When the client disappears (close, network loss, idle timeout, unplugged
   adapter, bridge stopping) the bridge itself sends the profile's stop sequence,
   once, then frees the port. Never while a client holds the link. A client that
   never wrote to the device cannot have moved it: nothing is sent for it.

The relay never sends a command of its own except that stop sequence and the
profile's read-only identification, and the latter only while no client holds
the link. Payloads are never logged.
"""
from collections import deque
from datetime import datetime, timezone
import errno
import ipaddress
import os
import select
import socket
import threading
import time

from .serial_port import open_serial, PortBusy
from .serial_profile import PARSERS

JOURNAL_SIZE = 100
PENDING_STOP_SECONDS = 600


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def peer_allowed(address):
    """Private, loopback or link-local peers only: this relay has no authentication."""
    try: value = ipaddress.ip_address(address)
    except ValueError: return False
    return value.is_private or value.is_loopback


class SerialRelay:
    def __init__(self, ident, name, device, profile, tcp_port, *, bind='0.0.0.0', baud=None, fmt=None,
                 idle_seconds=120, log=None, opener=open_serial, clock=time.monotonic, auto_identify=True,
                 allowed=peer_allowed):
        self.id, self.name, self.device, self.profile = ident, name, device, profile
        self.tcp_port, self.bind = tcp_port, bind
        self.baud = baud or profile['serial']['baud']
        self.fmt = fmt or profile['serial']['format']
        self.idle_seconds = idle_seconds
        self.log, self.opener, self.clock, self.allowed = log, opener, clock, allowed
        self.auto_identify = auto_identify
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.listener = None
        self.client = None
        self.client_peer = None
        self.client_since = None
        self.client_wrote = False
        self.last_client_byte = 0
        self.fd = None
        self.state = 'starting'
        self.reason = None
        self.pending_stop = None
        self.port_present = None
        self.need_identify = auto_identify
        self.identify_request = None
        self.identity = None
        self.refused = 0
        self.last_refused = None
        self.stops = 0
        self.last_stop = None
        self.sent = self.received = self.discarded = 0
        self.journal = deque(maxlen=JOURNAL_SIZE)

    # -- journal --------------------------------------------------------------
    def event(self, kind, **detail):
        row = {'at': utc_now(), 'event': kind, **detail}
        with self.lock: self.journal.append(row)
        if self.log: self.log('serial-relay %s: %s %s' % (self.id, kind, ' '.join('%s=%s' % kv for kv in detail.items())))

    # -- lifecycle ------------------------------------------------------------
    def start(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.bind, self.tcp_port))
            listener.listen(4)
            listener.setblocking(False)
        except BaseException:
            listener.close(); raise
        self.listener = listener
        self.tcp_port = listener.getsockname()[1]
        self.thread = threading.Thread(target=self._run, name='serial-relay-' + self.id, daemon=True)
        self.thread.start()
        return self

    def close(self, timeout=15):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout)

    def healthy(self):
        return self.thread is not None and self.thread.is_alive() and not self.stop_event.is_set()

    # -- state ----------------------------------------------------------------
    def snapshot(self):
        with self.lock:
            return {'state': self.state, 'reason': self.reason, 'adapter_present': self.port_present,
                    'client': {'address': self.client_peer[0], 'since': self.client_since} if self.client else None,
                    'refused_clients': self.refused, 'last_refused': self.last_refused,
                    'stops_sent': self.stops, 'last_stop': self.last_stop, 'stop_pending': self.pending_stop is not None,
                    'bytes_to_device': self.sent, 'bytes_from_device': self.received, 'bytes_discarded': self.discarded,
                    'identity': self.identity, 'journal': list(self.journal)[-30:]}

    def _set_state(self, state, reason=None):
        with self.lock: self.state, self.reason = state, reason

    # -- device helpers ---------------------------------------------------------
    def _present(self):
        return os.path.exists(os.path.realpath(self.device))

    def _open(self):
        return self.opener(self.device, self.baud, self.fmt)

    def _close_fd(self):
        if self.fd is not None:
            try: os.close(self.fd)
            except OSError: pass
            self.fd = None

    def _write_fd(self, fd, data, timeout=2.0):
        deadline = self.clock() + timeout
        while data:
            try:
                size = os.write(fd, data); data = data[size:]
            except BlockingIOError:
                if self.clock() > deadline: raise TimeoutError('Serial write timeout')
                select.select([], [fd], [], .05)

    def _read_until(self, fd, terminator, timeout, limit=64):
        """Bytes up to and including `terminator`; returns what was read within `timeout`."""
        data = b''
        deadline = self.clock() + timeout
        while self.clock() < deadline and len(data) < limit:
            if not select.select([fd], [], [], max(0.0, min(.05, deadline - self.clock())))[0]: continue
            try: chunk = os.read(fd, 64)
            except BlockingIOError: continue
            if not chunk: break
            data += chunk
            if terminator in chunk: break
        return data

    # -- the stop sequence (safety rule 2) ---------------------------------------
    def _stop_sequence(self, fd, reason):
        sequence = self.profile['stop']
        acked = 0
        for frame in sequence['frames']:
            self._write_fd(fd, frame['data'])
            reply = self._read_until(fd, bytes([sequence['terminator']]), sequence['timeout'])
            if reply.endswith(bytes([sequence['terminator']])): acked += 1
        try: termios_flush(fd)
        except OSError: pass
        with self.lock:
            self.stops += 1
            self.last_stop = {'at': utc_now(), 'reason': reason, 'frames': len(sequence['frames']), 'acknowledged': acked}
        self.event('stop_sent', reason=reason, frames=len(sequence['frames']), acknowledged=acked)

    def _deliver_stop(self, reason):
        """Open the device if needed and send the stop once; keep it pending when impossible."""
        if not self.profile['stop']:
            self.pending_stop = None; return True
        try:
            fd = self.fd if self.fd is not None else self._open()
        except OSError as error:
            self.pending_stop = self.pending_stop or (self.clock(), reason)
            self.event('stop_pending', reason=reason, cause=errno.errorcode.get(error.errno, 'OSERROR'))
            return False
        own = fd is not self.fd
        self._set_state('stopping', reason)
        try:
            self._stop_sequence(fd, reason)
            self.pending_stop = None
            return True
        except OSError as error:
            self.pending_stop = self.pending_stop or (self.clock(), reason)
            self.event('stop_pending', reason=reason, cause=errno.errorcode.get(error.errno, 'OSERROR') if error.errno else 'TIMEOUT')
            return False
        finally:
            if own:
                try: os.close(fd)
                except OSError: pass

    # -- client loss ---------------------------------------------------------------
    def _lose_client(self, reason, device_ok=True):
        client, wrote = self.client, self.client_wrote
        with self.lock:
            self.client = self.client_peer = self.client_since = None
            self.client_wrote = False
        if client is not None:
            try: client.close()
            except OSError: pass
        self.event('client_lost', reason=reason, wrote=wrote)
        if not device_ok:
            self._close_fd()
            if wrote and self.profile['stop']:
                self.pending_stop = self.pending_stop or (self.clock(), reason)
                self.event('stop_pending', reason=reason, cause='ADAPTER_LOST')
            self._set_state('idle' if self.port_present else 'adapter_missing', reason)
            return
        if wrote: self._deliver_stop(reason)
        self._close_fd()
        self._set_state('idle' if self._present() else 'adapter_missing')

    def _retry_pending_stop(self):
        if self.pending_stop is None or self.client is not None: return
        if self.clock() - self.pending_stop[0] > PENDING_STOP_SECONDS:
            self.event('stop_expired', reason=self.pending_stop[1]); self.pending_stop = None; return
        if self._present():
            reason = self.pending_stop[1]
            if self._deliver_stop(reason):
                self._close_fd(); self._set_state('idle')

    # -- identification (read-only, only while nobody holds the link) -----------------
    def request_identify(self, timeout=8):
        with self.lock:
            if self.client is not None: raise PermissionError('CLIENT_CONNECTED')
            request = self.identify_request = threading.Event()
        request.wait(timeout)
        return self.identity

    def _identify(self):
        sequence = self.profile['identify_sequence']
        self.need_identify = False
        if not sequence: return
        self._set_state('identifying')
        replies, result = {}, None
        try:
            fd = self._open()
        except OSError as error:
            self.event('identify_failed', cause=errno.errorcode.get(error.errno, 'OSERROR'))
            self._set_state('idle' if self._present() else 'adapter_missing')
            return
        try:
            for frame in sequence['frames']:
                self._write_fd(fd, frame['data'])
                data = self._read_until(fd, bytes([sequence['terminator']]), sequence['timeout'])
                if data.endswith(bytes([sequence['terminator']])) and len(data) - 1 == frame['reply_length']:
                    replies[frame['id']] = data[:-1]
                elif frame['id'] == 'echo': break
            result = PARSERS[sequence['parser']](replies, self.profile['models'])
        except OSError as error:
            self.event('identify_failed', cause=errno.errorcode.get(error.errno, 'OSERROR') if error.errno else 'TIMEOUT')
        finally:
            try: os.close(fd)
            except OSError: pass
        identity = dict(result or {'responding': False}, read_at=utc_now(), source='bridge_read')
        with self.lock: self.identity = identity
        self.event('identified' if identity['responding'] else 'not_identified',
                   model=identity.get('model_name'), version=identity.get('hand_control_version'))
        self._set_state('idle')

    # -- main loop --------------------------------------------------------------------
    def _refuse(self, connection, peer, reason):
        # Record the refusal before closing: a client that sees the close can read it at once.
        with self.lock:
            self.refused += 1
            self.last_refused = {'at': utc_now(), 'address': peer[0], 'reason': reason}
        try: connection.close()
        except OSError: pass
        self.event('client_refused', address=peer[0], reason=reason)

    def _accept(self):
        try: connection, peer = self.listener.accept()
        except (BlockingIOError, InterruptedError): return
        if not self.allowed(peer[0]): return self._refuse(connection, peer, 'PEER_NOT_ALLOWED')
        if self.client is not None: return self._refuse(connection, peer, 'LEASED_BY_OTHER_CLIENT')
        self._retry_pending_stop()
        if self.pending_stop is not None: return self._refuse(connection, peer, 'STOP_PENDING')
        try:
            fd = self._open()
        except PortBusy: return self._refuse(connection, peer, 'PORT_BUSY')
        except OSError: return self._refuse(connection, peer, 'ADAPTER_UNAVAILABLE')
        # Stale bytes from before this client are never delivered to it.
        try: termios_flush(fd)
        except OSError: pass
        connection.setblocking(False)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for option, value in (('TCP_KEEPIDLE', 10), ('TCP_KEEPALIVE', 10), ('TCP_KEEPINTVL', 5), ('TCP_KEEPCNT', 3)):
            if hasattr(socket, option):
                try: connection.setsockopt(socket.IPPROTO_TCP, getattr(socket, option), value)
                except OSError: pass
        self.fd = fd
        with self.lock:
            self.client, self.client_peer, self.client_since = connection, peer, utc_now()
            self.client_wrote = False
            self.last_client_byte = self.clock()
        self._set_state('client')
        self.event('client_connected', address=peer[0])

    def _from_client(self):
        try: data = self.client.recv(4096)
        except (BlockingIOError, InterruptedError): return
        except OSError: data = b''
        if not data: return self._lose_client('CLIENT_CLOSED')
        self.last_client_byte = self.clock()
        try: self._write_fd(self.fd, data)
        except OSError: return self._lose_client('ADAPTER_LOST', device_ok=False)
        self.client_wrote = True
        self.sent += len(data)

    def _from_device(self):
        try: data = os.read(self.fd, 4096)
        except (BlockingIOError, InterruptedError): return
        except OSError: return self._lose_client('ADAPTER_LOST', device_ok=False)
        if not data: return
        self.received += len(data)
        try:
            self.client.settimeout(2); self.client.sendall(data); self.client.setblocking(False)
        except OSError: self._lose_client('CLIENT_LOST')

    def _run(self):
        try:
            while not self.stop_event.is_set():
                present = self._present()
                if present != self.port_present:
                    with self.lock: self.port_present = present
                    if present:
                        if self.auto_identify: self.need_identify = True
                        self.event('adapter_present')
                    else:
                        self.event('adapter_missing')
                    if self.client is None: self._set_state('idle' if present else 'adapter_missing')
                if self.client is not None and not present:
                    self._lose_client('ADAPTER_LOST', device_ok=False)
                if self.client is None:
                    self._retry_pending_stop()
                    if present and self.pending_stop is None and (self.need_identify or self.identify_request):
                        request = self.identify_request
                        self._identify()
                        if request is not None:
                            self.identify_request = None; request.set()
                    elif self.identify_request is not None:
                        self.identify_request.set(); self.identify_request = None
                elif self.idle_seconds and self.clock() - self.last_client_byte > self.idle_seconds:
                    self._lose_client('IDLE_TIMEOUT')
                waiting = [self.listener] + ([self.client, self.fd] if self.client is not None else [])
                try: ready = select.select(waiting, [], [], .2)[0]
                except (OSError, ValueError): ready = []
                for item in ready:
                    if item is self.listener: self._accept()
                    elif self.client is not None and item is self.client: self._from_client()
                    elif self.client is not None and item == self.fd: self._from_device()
        finally:
            if self.client is not None: self._lose_client('BRIDGE_STOPPING')
            self._close_fd()
            if self.listener: self.listener.close()
            self._set_state('stopped')


def termios_flush(fd):
    import termios
    termios.tcflush(fd, termios.TCIFLUSH)
