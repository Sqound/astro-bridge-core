"""Optional independent HTTPS transport; certificate creation belongs to the OS helper."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import threading
import time


class CertificateState:
    def __init__(self, state, run=subprocess.run):
        self.state = Path(state)
        self.tls = self.state / 'tls'
        self.run = run

    @staticmethod
    def private(path, directory=False):
        info = path.lstat()
        if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or not directory and info.st_size > 32768):
            raise ValueError('Unsafe TLS state')

    def record(self):
        self.private(self.state, True)
        if not self.tls.exists() and not self.tls.is_symlink(): return None
        self.private(self.tls, True)
        path = self.tls / 'current.json'
        try: self.private(path)
        except FileNotFoundError: return None
        if path.stat().st_size > 4096: raise ValueError('Invalid TLS state')
        value = json.loads(path.read_bytes())
        if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
                or type(value.get('configured')) is not bool or type(value.get('enabled')) is not bool
                or value['enabled'] and not value['configured']):
            raise ValueError('Invalid TLS state')
        if value['configured'] and (not isinstance(value.get('generation'), str)
                                   or not re.fullmatch('[0-9a-f]{32}', value['generation'])):
            raise ValueError('Invalid TLS generation')
        return value

    @contextmanager
    def config_guard(self):
        # Held across validation AND Store.save. The OS helper holds the same
        # fixed lock while reading/signing/publishing the configured hostname.
        self.private(self.state, True)
        fd = os.open(self.state / 'configuration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError('Unsafe configuration lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally: os.close(fd)

    def busy(self):
        path = self.tls / 'operation.lock'
        try:
            self.private(self.state, True)
            if not self.tls.exists() and not self.tls.is_symlink(): return False
            self.private(self.tls, True)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    return True
                try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: return True
                return False
            finally: os.close(fd)
        except FileNotFoundError: return False
        except (OSError, ValueError): return True

    def rename_blocked(self):
        try:
            value = self.record()
            return self.busy() or bool(value and value['enabled'])
        except (OSError, ValueError): return True

    def context(self, record, hostname):
        folder = self.tls / 'generations'
        self.private(folder, True); folder /= record['generation']; self.private(folder, True)
        for name in ('ca.pem', 'server.pem', 'server.key', 'metadata.json'):
            self.private(folder / name)
        metadata = json.loads((folder / 'metadata.json').read_bytes())
        if (not isinstance(metadata, dict) or set(metadata) != {'hostname', 'expires', 'clock_evidence'}
                or metadata.get('hostname') != hostname or not isinstance(metadata.get('expires'), str)
                or metadata.get('clock_evidence') not in ('chrony-synchronized', 'owner-confirmed-current-utc')):
            raise ValueError('TLS hostname mismatch')
        def openssl(*args):
            return self.run(['/usr/bin/openssl', *args], check=True, capture_output=True,
                            text=True, timeout=5, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}).stdout.strip()
        openssl('verify', '-CAfile', str(folder / 'ca.pem'), '-purpose', 'sslserver',
                '-verify_hostname', hostname, str(folder / 'server.pem'))
        openssl('verify', '-CAfile', str(folder / 'ca.pem'), '-purpose', 'sslserver',
                '-verify_ip', '192.168.77.1', str(folder / 'server.pem'))
        enddate = openssl('x509', '-in', str(folder / 'server.pem'), '-enddate', '-noout').removeprefix('notAfter=')
        if metadata['expires'] != enddate: raise ValueError('TLS expiry mismatch')
        expiry = ssl.cert_time_to_seconds(enddate)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.options |= ssl.OP_NO_COMPRESSION
        context.load_cert_chain(folder / 'server.pem', folder / 'server.key')
        context.bridge_identity = hashlib.sha256(ssl.PEM_cert_to_DER_cert((folder / 'ca.pem').read_text())).hexdigest()
        return context, expiry


class HTTPSService:
    """Observe atomic OS generations; never restart app/network services to load TLS."""
    def __init__(self, http, bind, port=443, state=None):
        self.http, self.bind, self.port = http, bind, port
        self.state = state or http.controller.certificates
        self.server = None
        self.generation = None
        self.expires = 0
        self.hostname = None
        self.identity = None
        self.stop = threading.Event()
        self.thread = None
        self.error = None
        self.refresh_lock = threading.RLock()
        http.secure_access.service = self

    def status(self):
        # Never wait for certificate/OpenSSL work in a routine metadata poll.
        server, hostname, expires = self.server, self.hostname, self.expires
        ready = bool(server and server.context and time.time() < expires
                     and hostname == self.http.controller.store.config["hostname"] + ".local")
        port = server.server_port if ready else 443
        return {"ready": ready, "url": ("https://" + hostname + (":" + str(port) if port != 443 else "")) if ready else None,
                "error": self.error}

    def close_listener(self):
        if self.server:
            self.server.context = None
            self.server.shutdown(); self.server.server_close(); self.server = None
        self.generation = self.hostname = self.identity = None; self.expires = 0
        # TLS reset/disable/identity loss must not leave reusable secure sessions.
        with self.http.auth_lock:
            self.http.sessions = {k: v for k, v in self.http.sessions.items() if v.get('transport', 'http') != 'https'}

    def refresh(self):
        with self.refresh_lock:
            return self._refresh()

    def _refresh(self):
        from .web import SecureWebServer
        hostname = self.http.controller.store.config['hostname'] + '.local'
        try:
            record = self.state.record()
            if not record or not record['enabled']:
                self.close_listener(); self.error = None; return
            if self.hostname and (self.hostname != hostname or time.time() >= self.expires):
                self.close_listener()
            if self.generation == record['generation']:
                return
            context, expiry = self.state.context(record, hostname)
            if self.stop.is_set(): return
            if time.time() >= expiry: raise ValueError('TLS identity expired')
            if not self.server:
                server = SecureWebServer((self.bind, self.port), self.http, context, hostname)
                self.server = server
                threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True).start()
            else:
                with self.http.auth_lock:
                    if self.identity != context.bridge_identity:
                        self.http.sessions = {k: v for k, v in self.http.sessions.items() if v.get('transport', 'http') != 'https'}
                    self.server.context = context  # Existing sockets drain under request deadlines.
            self.generation, self.hostname, self.expires = record['generation'], hostname, expiry
            self.identity = context.bridge_identity
            self.error = None
        except (OSError, ValueError, subprocess.SubprocessError):
            self.error = 'TLS_UNAVAILABLE'  # No key/path/command diagnostic in public state/logs.
            if self.hostname != hostname or time.time() >= self.expires:
                self.close_listener()

    def start(self):
        def watch():
            while not self.stop.is_set():
                self.refresh(); self.stop.wait(1)
        self.thread = threading.Thread(target=watch, daemon=True); self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread: self.thread.join(timeout=20)
        self.close_listener()
