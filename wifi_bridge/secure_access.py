"""Bounded HTTPS management jobs; routine reads never execute OS/crypto helpers."""
from datetime import datetime
import hashlib
import hmac
import json
import os
import plistlib
import ssl
from pathlib import Path
import re
import signal
import stat
import subprocess
import threading
import time
import uuid

from .api import ApiError
from .config import password_matches
from .updates import Updates

HELPER = Path('/usr/libexec/astro-bridge-tls')
MUTATIONS = {'setup', 'renew', 'enable', 'disable', 'reset'}
CODES = {'HTTPS_BUSY', 'UPDATE_IN_PROGRESS', 'HTTPS_CLOCK_UNVERIFIED', 'HTTPS_OPERATION_FAILED'}
MESSAGES = {
    'HTTPS_UNSUPPORTED': 'Secure local access requires the diskless OS certificate helper version 2.',
    'HTTPS_BUSY': 'A secure access operation is already running.',
    'SYSTEM_SHUTTING_DOWN': 'The bridge is stopping or a reboot is already scheduled.',
    'HTTPS_REFRESH_LIMITED': 'Wait before refreshing certificate diagnostics again.',
    'UPDATE_IN_PROGRESS': 'An application update or recovery is in progress.',
    'REAUTHENTICATION_FAILED': 'The current web interface password is incorrect.',
    'REAUTHENTICATION_LIMITED': 'Too many password checks. Try again in a minute.',
    'HTTPS_CLOCK_UNVERIFIED': 'Synchronize current UTC or explicitly verify it for this operation.',
    'HTTPS_OPERATION_FAILED': 'The certificate operation failed. Refresh diagnostics before retrying.',
    'HTTPS_CERTIFICATE_UNAVAILABLE': 'A validated public CA certificate is not available.',
}


class SecureAccess:
    def __init__(self, controller, capable=None, invoke=None):
        self.controller = controller
        self.state = controller.certificates
        self.updates = Updates(controller.store.path.parent, controller.demo)
        self.capable = capable if capable is not None else self.os_supported
        self.invoke = invoke or self.helper
        self.service = None
        self.lock = threading.RLock()
        self.operation = None
        self.metadata = None
        self.stale = True
        self.last_refresh = None
        self.observed = None
        self.thread = None
        self.process = None
        self.closed = False
        self.bootstrap_stop = threading.Event()
        self.bootstrap_thread = None

    @staticmethod
    def os_supported():
        try:
            info = HELPER.lstat()
            return (Path('/etc/wifi-bridge/tls-helper-version').read_text().strip() == '2'
                    and stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
                    and os.access(HELPER, os.X_OK) and Path('/usr/bin/openssl').is_file())
        except OSError: return False

    def initialize(self):
        if self.capable():
            try: self.action({'action': 'refresh'}, initial=True)
            except ApiError as error:
                # During OTA verification, the new app must become healthy before
                # maintenance jobs may start. Retry the initial diagnosis only
                # after that gate opens; ordinary metadata polls remain cheap.
                if error.code in ('UPDATE_IN_PROGRESS', 'HTTPS_BUSY'):
                    self.bootstrap_thread = threading.Thread(target=self.bootstrap, daemon=True, name='bridge-https-initial')
                    self.bootstrap_thread.start()

    def bootstrap(self):
        for _ in range(120):
            if self.bootstrap_stop.wait(1): return
            try:
                self.action({'action': 'refresh'}, initial=True)
                return
            except ApiError as error:
                if error.code not in ('UPDATE_IN_PROGRESS', 'HTTPS_BUSY'): return

    def helper(self, action, request):
        with self.lock:
            if self.closed: raise ValueError("Operation closed")
        process = subprocess.Popen(['/usr/bin/python3', str(HELPER), action], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                   env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        with self.lock:
            self.process = process
            if self.closed: os.killpg(process.pid, signal.SIGKILL)
        try:
            payload = json.dumps(request).encode() if action in ('setup', 'renew') else b''
            try: output, _ = process.communicate(payload, timeout=90)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.communicate()
                raise ApiError(409, 'HTTPS_OPERATION_FAILED', MESSAGES['HTTPS_OPERATION_FAILED']) from None
            if len(output) > 32768: raise ValueError('Invalid helper response')
            value = json.loads(output)
            if not isinstance(value, dict): raise ValueError('Invalid helper response')
            if process.returncode:
                code = value.get('error', {}).get('code') if isinstance(value.get('error'), dict) else None
                code = code if code in CODES else 'HTTPS_OPERATION_FAILED'
                raise ApiError(409, code, MESSAGES[code])
            return value
        finally:
            with self.lock: self.process = None

    @staticmethod
    def public_metadata(value):
        if (not isinstance(value, dict) or type(value.get('configured')) is not bool
                or type(value.get('enabled')) is not bool): raise ValueError('Invalid diagnosis')
        def expiry(field):
            text = value.get(field)
            if text is None: return None
            if not isinstance(text, str) or len(text) > 40: raise ValueError('Invalid expiry')
            stamp = datetime.fromisoformat(text)
            if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0: raise ValueError('Invalid expiry')
            return text
        hostname = value.get('hostname')
        if hostname is not None and (not isinstance(hostname, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.local', hostname)):
            raise ValueError('Invalid hostname')
        fingerprint = value.get('fingerprint_sha256')
        if fingerprint is not None and (not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint)):
            raise ValueError('Invalid fingerprint')
        state = value.get('state')
        if state not in ('unconfigured', 'valid', 'invalid', 'hostname_mismatch', 'not_yet_valid', 'ca_expired', 'leaf_expired'):
            raise ValueError('Invalid diagnostic state')
        issuance = value.get('issuance_clock_evidence')
        if issuance not in (None, 'chrony-synchronized', 'owner-confirmed-current-utc'): raise ValueError('Invalid clock state')
        return {'configured': value['configured'], 'enabled': value['enabled'], 'state': state,
                'hostname': hostname, 'fingerprint_sha256': fingerprint, 'ca_expires': expiry('ca_expires'),
                'leaf_expires': expiry('leaf_expires'), 'issuance_clock_evidence': issuance,
                'current_clock': 'synchronized' if value.get('current_clock') == 'synchronized' else 'unverified'}

    def status(self):
        with self.lock:
            supported = bool(self.capable())
            result = {'supported': supported, 'reason': None if supported else 'OS_HELPER_UNAVAILABLE',
                      'identity': dict(self.metadata) if self.metadata else None, 'diagnostics_stale': self.stale,
                      'operation': dict(self.operation) if self.operation else None,
                      'diagnostics_age_seconds': int(time.monotonic()-self.observed) if self.observed is not None else None,
                      'listener': {'ready': False, 'url': None, 'error': None}}
        if self.service: result['listener'] = self.service.status()
        return result

    def reauthenticate(self, password):
        c = self.controller
        now = time.monotonic()
        while c.password_attempts and now - c.password_attempts[0] >= 60: c.password_attempts.popleft()
        if len(c.password_attempts) >= 5:
            raise ApiError(429, 'REAUTHENTICATION_LIMITED', MESSAGES['REAUTHENTICATION_LIMITED'])
        c.password_attempts.append(now)
        if not isinstance(password, str) or len(password) > 128:
            raise ApiError(403, 'REAUTHENTICATION_FAILED', MESSAGES['REAUTHENTICATION_FAILED'])
        encoded = c.store.config['admin_hash']
        valid = password_matches(password, encoded) if encoded else hmac.compare_digest(password.encode(), c.bootstrap['password'].encode())
        if not valid: raise ApiError(403, 'REAUTHENTICATION_FAILED', MESSAGES['REAUTHENTICATION_FAILED'])

    def action(self, data, initial=False):
        if not isinstance(data, dict) or not isinstance(data.get('action'), str) or data['action'] not in MUTATIONS | {'refresh'}:
            raise ApiError(400, 'INVALID_FIELDS', 'Choose a fixed secure access action.')
        action = data['action']; allowed = {'action'}
        if action in MUTATIONS: allowed.add('current_password')
        if action in ('setup', 'renew'): allowed.add('owner_confirmed_utc')
        if action == 'reset': allowed.add('confirm_reset')
        if set(data) - allowed or action == 'reset' and data.get('confirm_reset') is not True:
            raise ApiError(400, 'INVALID_FIELDS', 'Unknown fields or missing explicit reset confirmation.')
        if 'owner_confirmed_utc' in data and (type(data['owner_confirmed_utc']) is not int or not 1577836800 <= data['owner_confirmed_utc'] <= 4102444800):
            raise ApiError(400, 'INVALID_FIELDS', 'Provide an integer current UTC epoch for this operation only.')
        with self.controller.lock, self.lock:
            if self.closed or not self.capable(): raise ApiError(409, 'HTTPS_UNSUPPORTED', MESSAGES['HTTPS_UNSUPPORTED'])
            if self.controller.operations.pending or self.controller.discovery_pending:
                raise ApiError(409, 'HTTPS_BUSY', MESSAGES['HTTPS_BUSY'])
            if self.controller.stop.is_set() or self.controller.action_at is not None:
                raise ApiError(409, 'SYSTEM_SHUTTING_DOWN', MESSAGES['SYSTEM_SHUTTING_DOWN'])
            if self.operation and self.operation['state'] == 'running':
                if action == 'refresh': return self.status()  # Coalesce, no queue.
                raise ApiError(409, 'HTTPS_BUSY', MESSAGES['HTTPS_BUSY'])
            if self.updates.busy(): raise ApiError(409, 'UPDATE_IN_PROGRESS', MESSAGES['UPDATE_IN_PROGRESS'])
            now = time.monotonic()
            if action == 'refresh' and not initial and self.last_refresh is not None and now - self.last_refresh < 30:
                raise ApiError(429, 'HTTPS_REFRESH_LIMITED', MESSAGES['HTTPS_REFRESH_LIMITED'])
            if action in MUTATIONS: self.controller.protection.reject()
            if action in MUTATIONS: self.reauthenticate(data.get('current_password'))
            ident = uuid.uuid4().hex
            self.operation = {'id': ident, 'action': action, 'state': 'running', 'code': None}
            self.controller.certificate_job_pending = True
            if action == 'refresh': self.last_refresh = now
            request = {'owner_confirmed_utc': data['owner_confirmed_utc']} if 'owner_confirmed_utc' in data else {}
            self.thread = threading.Thread(target=self.run, args=(ident, action, request), daemon=True)
            try: self.thread.start()
            except Exception:
                self.controller.certificate_job_pending = False
                self.operation.update(state='failed', code='HTTPS_OPERATION_FAILED')
                self.thread = None
                raise ApiError(409, 'HTTPS_OPERATION_FAILED', MESSAGES['HTTPS_OPERATION_FAILED']) from None
            return self.status()

    def run(self, ident, action, request):
        code = None
        try:
            if action != 'refresh': self.invoke(action, request)
            value = self.public_metadata(self.invoke('diagnose', {}))
            with self.lock: self.metadata, self.stale, self.observed = value, False, time.monotonic()
        except ApiError as error: code = error.code
        except Exception: code = 'HTTPS_OPERATION_FAILED'
        finally:
            try:
                if self.service: self.service.refresh()
            except Exception: code = code or 'HTTPS_OPERATION_FAILED'
            with self.controller.lock, self.lock:
                self.controller.certificate_job_pending = False
                if self.operation and self.operation['id'] == ident:
                    self.operation.update(state='failed' if code else 'succeeded', code=code)
                    if code: self.stale = True

    def profile(self):
        """Public CA only; user installation and SSL trust remain explicit iOS actions."""
        ca = self.certificate()
        der = ssl.PEM_cert_to_DER_cert(ca.decode())
        fingerprint = hashlib.sha256(der).hexdigest()
        def ident(kind): return str(uuid.uuid5(uuid.NAMESPACE_URL, 'astro-bridge:' + fingerprint + ':' + kind))
        certificate = dict(PayloadType='com.apple.security.root', PayloadVersion=1,
                           PayloadIdentifier='space.planastro.bridge.ca.' + fingerprint,
                           PayloadUUID=ident('ca'), PayloadDisplayName='Astro Bridge local CA',
                           PayloadContent=der)
        return plistlib.dumps(dict(PayloadType='Configuration', PayloadVersion=1,
            PayloadIdentifier='space.planastro.bridge.trust.' + fingerprint, PayloadUUID=ident('profile'),
            PayloadDisplayName='Astro Bridge HTTPS', PayloadDescription='Local HTTPS trust for this Astro Bridge only.',
            PayloadRemovalDisallowed=False, PayloadContent=[certificate]))

    def certificate(self):
        with self.lock:
            fingerprint = self.metadata.get('fingerprint_sha256') if self.metadata else None
        if not self.capable() or not fingerprint:
            raise ApiError(409, 'HTTPS_CERTIFICATE_UNAVAILABLE', MESSAGES['HTTPS_CERTIFICATE_UNAVAILABLE'])
        try:
            record = self.state.record()
            if not record or not record['configured']: raise ValueError('No identity')
            folder = self.state.tls / 'generations'; self.state.private(folder, True)
            folder /= record['generation']; self.state.private(folder, True)
            path = folder / 'ca.pem'; self.state.private(path)
            data = path.read_bytes()
            import ssl
            if hashlib.sha256(ssl.PEM_cert_to_DER_cert(data.decode())).hexdigest() != fingerprint: raise ValueError('Identity changed')
            return data
        except (OSError, ValueError):
            raise ApiError(409, 'HTTPS_CERTIFICATE_UNAVAILABLE', MESSAGES['HTTPS_CERTIFICATE_UNAVAILABLE']) from None

    def close(self):
        with self.lock:
            self.closed = True; self.bootstrap_stop.set(); process = self.process
        if process and process.poll() is None:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        if self.bootstrap_thread: self.bootstrap_thread.join(timeout=2)
        if self.thread: self.thread.join(timeout=5)
