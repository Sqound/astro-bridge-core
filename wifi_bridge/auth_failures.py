"""Boot-scoped Wi-Fi credential rejection holds, private and never exposed in API data."""
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import threading

from .config import atomic_json


class AuthenticationFailures:
    def __init__(self, root, boot_id=None):
        self.path = root / 'wifi-auth-failures.json'
        self.lock = threading.RLock()
        if boot_id is None:
            try: boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            except OSError: boot_id = secrets.token_hex(16)
        self.boot_id = boot_id
        self.key = secrets.token_bytes(32)
        self.records = {}
        try:
            raw = self.path.read_bytes()
            if len(raw) > 16384: raise ValueError('Registry too large')
            data = json.loads(raw)
            if data.get('boot_id') != boot_id: return
            key, records = data['key'], data['holds']
            if (data.get('version') != 1 or not re.fullmatch('[0-9a-f]{64}', key)
                    or not isinstance(records, dict) or len(records) > 24
                    or any(not re.fullmatch('[a-zA-Z0-9_-]{1,64}', k)
                           or not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v)
                           for k, v in records.items())):
                raise ValueError('Invalid registry')
            self.key, self.records = bytes.fromhex(key), records
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass

    def fingerprint(self, profile):
        # No plaintext password or guessable unkeyed credential digest is persisted.
        # Automatic selection may temporarily choose an adapter/BSSID. That must
        # not release a credential hold when the failed operation rolls back.
        fields = {k: profile.get(k) for k in ('ssid', 'password', 'open')}
        return hmac.new(self.key, json.dumps(fields, sort_keys=True).encode(), hashlib.sha256).hexdigest()

    def held(self, profile):
        with self.lock:
            return hmac.compare_digest(self.records.get(profile['id'], ''), self.fingerprint(profile))

    def persist(self):
        try:
            atomic_json(self.path, {'version': 1, 'boot_id': self.boot_id,
                                   'key': self.key.hex(), 'holds': self.records})
        except OSError:
            pass  # The running process still suppresses retries on unwritable storage.

    def hold(self, profile):
        with self.lock:
            if len(self.records) < 24 or profile['id'] in self.records:
                self.records[profile['id']] = self.fingerprint(profile)
                self.persist()

    def prune(self, profiles):
        with self.lock:
            current = {p['id']: self.fingerprint(p) for p in profiles}
            keep = {k: v for k, v in self.records.items() if current.get(k) == v}
            removed = self.records.keys() - keep.keys()
            if keep != self.records:
                self.records = keep
                self.persist()
            return removed
