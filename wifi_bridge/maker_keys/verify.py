"""Fresh, bounded Seestar authentication only; never request instrument state."""
import json
import socket
import threading
import time

from ..telemetry.alpaca import Transport
from .rsa import parse


AUTH_METHODS = frozenset(('get_verify_str', 'verify_client', 'pi_is_verified'))
# This reviewed list may be populated after hardware review; no heartbeat is currently authorized.
KEEPALIVE_METHODS = frozenset()


def send(stream, ident, method, params, *, keepalive=False):
    allowed = KEEPALIVE_METHODS if keepalive else AUTH_METHODS
    if method not in allowed: raise ValueError('Seestar method refused')
    payload = bytearray(json.dumps({'id':ident,'method':method,'params':params},ensure_ascii=False,separators=(',',':')).encode()+b'\r\n')
    try: stream.sendall(payload)
    finally: payload[:] = b'\0'*len(payload)


class Handshake:
    def __init__(self, address, interface, target, *, factory=None, permission=None):
        self.transport = Transport(address,interface,factory,target=target,permission=permission)
        self.target = target

    def close(self): self.transport.close()

    def run(self, pem, current=lambda: True, *, listener=None):
        stream = None
        pending = bytearray()
        try:
            key = parse(pem)
            self.transport.allow([(self.target,4700)])
            stream = self.transport.stream(socket.SOCK_STREAM)
            watchdog = threading.Timer(8,self.transport.interrupt,args=(stream,));watchdog.daemon=True
            watchdog.start()
            deadline = time.monotonic()+8
            try:
                if not current(): return 'unverified'
                stream.connect((self.target,4700))
                def request(ident,method,params):
                    if not current() or time.monotonic() >= deadline: raise OSError()
                    send(stream, ident, method, params)
                    for _ in range(32):
                        while b'\n' not in pending:
                            if len(pending) >= 16384 or not current() or time.monotonic() >= deadline: raise OSError()
                            chunk = stream.recv(min(4096,16384-len(pending)))
                            if not chunk: raise OSError()
                            pending.extend(chunk)
                        at = pending.index(10)
                        line = pending[:at]; del pending[:at+1]
                        try: value = json.loads(line)
                        finally: line[:] = b'\0'*len(line)
                        if isinstance(value,dict) and type(value.get('id')) is int and value['id'] == ident:
                            return value
                    raise OSError()
                challenge = request(1,'get_verify_str','verify').get('result',{})
                challenge = challenge.get('str') if isinstance(challenge,dict) else None
                if not isinstance(challenge,str) or not 1 <= len(challenge.encode('utf-8')) <= 4096: return 'unverified'
                accepted = request(2,'verify_client',{'sign':key.sign(challenge),'data':challenge})
                if type(accepted.get('code')) is not int: return 'unverified'
                if accepted['code'] != 0: return 'refused'
                confirmed = request(3,'pi_is_verified','verify')
                if type(confirmed.get('code')) is not int: return 'unverified'
                if confirmed['code'] != 0: return 'refused'
                watchdog.cancel()
                key = challenge = accepted = confirmed = None
                # Authentication deadline must not terminate the listening phase.
                # Pass any coalesced post-verification event bytes exactly once.
                if listener is not None and current(): listener(stream, pending)
                return 'verified'
            finally: watchdog.cancel()
        except (OSError, ValueError, UnicodeError, RecursionError): return 'unverified'
        finally:
            pending[:] = b'\0'*len(pending)
            if stream is not None: self.transport.release(stream)
            self.close()
