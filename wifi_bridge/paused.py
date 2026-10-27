"""Private bounded failed-restoration holds; no credentials or job replay on disk."""
import hashlib
import json
import os
import re
import stat

from .api import ApiError
from .config import atomic_json, profiles, role_for


_MISSING = object()


class PausedRoles:
    def __init__(self, root):
        self.path = root / 'paused-roles.json'
        self.marker = root / 'paused-roles.initialized'
        self.records = {}
        self.error = None
        try:
            marker = self.read(self.marker)
            if marker is not _MISSING and (not isinstance(marker, dict) or set(marker) != {'version','initialized'} or type(marker['version']) is not int or marker['version'] != 1 or marker['initialized'] is not True):
                raise ValueError('INVALID_MARKER')
            data = self.read(self.path)
            if data is _MISSING:
                if marker is not _MISSING: raise ValueError('REGISTRY_LOST')
                self.persist({})
            else:
                if (not isinstance(data, dict) or set(data) != {'version', 'holds'}
                        or type(data['version']) is not int or data['version'] != 1
                        or not isinstance(data['holds'], dict) or len(data['holds']) > 24):
                    raise ValueError('INVALID_REGISTRY')
                for ident, value in data['holds'].items():
                    if (not isinstance(ident, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', ident)
                            or not isinstance(value, dict) or set(value) != {'intent', 'reason'}
                            or not re.fullmatch('[0-9a-f]{64}', str(value['intent']))
                            or value['reason'] not in ('RESTORATION_FAILED', 'OWNER_RECOVERY')):
                        raise ValueError('INVALID_HOLD')
                self.records = data['holds']
                if marker is _MISSING: atomic_json(self.marker, {'version': 1, 'initialized': True})
        except (OSError, ValueError, TypeError):
            self.error = 'PAUSE_REGISTRY_RECOVERY_REQUIRED'

    @staticmethod
    def read(path):
        try: fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError: return _MISSING
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_size > 16384): raise ValueError('PRIVATE_REGISTRY_REQUIRED')
            raw = source.read(16385)
        if len(raw) > 16384: raise ValueError('REGISTRY_TOO_LARGE')
        return json.loads(raw)

    @staticmethod
    def fingerprint(profile):
        # This digest is deliberately NOT derived from a password or any credential.
        fields = ('id', 'mac', 'bssid', 'ssid', 'target_ip', 'kind', 'enabled', 'hidden')
        value = {key: profile.get(key) for key in fields} | {'role': role_for(profile)}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def persist(self, records):
        atomic_json(self.path, {'version': 1, 'holds': records})
        atomic_json(self.marker, {'version': 1, 'initialized': True})
        self.records = records
        self.error = None

    @staticmethod
    def changed(before, after):
        fields = ('id', 'mac', 'bssid', 'ssid', 'target_ip', 'kind', 'enabled', 'hidden', 'password')
        return {key:before.get(key) for key in fields} != ({key:after.get(key) for key in fields} if after else None)

    def held(self, ident):
        return bool(self.error or ident in self.records)

    def status(self, config):
        return {'recovery_required': bool(self.error), 'reason': self.error,
                'profiles': {p['id']: ('PAUSED_INTENT_CHANGED' if p['id'] in self.records
                    and self.records[p['id']]['intent'] != self.fingerprint(p)
                    else self.error or self.records[p['id']]['reason'])
                    for p in profiles(config) if self.held(p['id'])}}

    def apply(self, config):
        for profile in profiles(config):
            if self.held(profile['id']): profile['enabled'] = False
        return config

    def hold(self, profile):
        value = dict(self.records)
        value[profile['id']] = {'intent': self.fingerprint(profile), 'reason': 'RESTORATION_FAILED'}
        if len(value) > 24:
            self.error = 'PAUSE_REGISTRY_RECOVERY_REQUIRED'; return
        self.records = value  # RAM remains paused even if storage is unwritable.
        try: self.persist(value)
        except OSError: self.error = 'QUARANTINE_PERSISTENCE_FAILED'

    def clear(self, identifiers):
        if not identifiers: return
        if self.error: raise ApiError(409, 'PAUSE_REGISTRY_RECOVERY_REQUIRED', 'Recover the pause registry before retrying individual connections.')
        value = {key: val for key, val in self.records.items() if key not in identifiers}
        if value == self.records: return
        try: self.persist(value)
        except OSError:
            self.error = 'QUARANTINE_PERSISTENCE_FAILED'
            raise ApiError(409, self.error, 'Configuration saved, but the connection remains paused because its hold could not be cleared.') from None

    def recover(self, controller, data, expected, revision):
        if not isinstance(data, dict) or set(data) != {'confirm_recovery', 'current_password'} or data['confirm_recovery'] is not True:
            raise ApiError(400, 'CONFIRMATION_REQUIRED', 'Confirm recovery by pausing all current telescope profiles.')
        with controller.lock:
            controller.protection.precondition(expected, revision)
            if (controller.operations.pending or controller.discovery_pending or controller.certificate_job_pending
                    or controller.certificates.busy() or controller.action_at is not None or controller.stop.is_set()
                    or controller.net_lock.locked()):
                raise ApiError(409, 'NETWORK_BUSY', 'Connection or maintenance work is pending.')
            controller.protection.reject()  # Recovery must not interrupt any protected ready role.
            from .updates import Updates
            if Updates(controller.store.path.parent, controller.demo).busy(): raise ApiError(409, 'UPDATE_IN_PROGRESS', 'Application maintenance is pending.')
            controller.protection.reauthenticate(data['current_password'])
            value = {p['id']: {'intent': self.fingerprint(p), 'reason': 'OWNER_RECOVERY'} for p in profiles(controller.store.config)}
            try: self.persist(value)
            except OSError:
                self.records = value; self.error = 'QUARANTINE_PERSISTENCE_FAILED'
                raise ApiError(409, self.error, 'All connections remain paused; private state could not be persisted.') from None
            return self.status(controller.store.config)
