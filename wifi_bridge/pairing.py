"""Persistent paired-client credentials, separate from the Linux password helper."""
from pathlib import Path
import datetime
import json
import secrets

from .config import atomic_json, device_identity, password_hash, password_matches
from .api import ApiError

SCOPES = ('read', 'connect', 'telemetry', 'camera', 'admin')


class PairedClients:
    """Bounded private credential store. Caller holds the shared auth lock."""
    def __init__(self, directory):
        self.path = Path(directory) / 'paired-clients.json'
        self.device_id = device_identity(directory)
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {'sequence': 0, 'clients': []}
        if self.path.exists():
            self.path.chmod(0o600)
        # Unknown clients still pay the normal hash cost, without storing a secret.
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))

    def public(self):
        return [{key: row[key] for key in ('client_id', 'name', 'scopes', 'created_seq', 'last_used_utc_best_effort')}
                for row in self.state['clients']]

    def create(self, data):
        name, scopes = data.get('name'), data.get('scopes')
        if (set(data) != {'name', 'scopes'} or not isinstance(name, str) or not 1 <= len(name.strip()) <= 48
                or len(name) > 48 or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or not isinstance(scopes, list) or not 1 <= len(scopes) <= len(SCOPES)
                or any(not isinstance(scope, str) or scope not in SCOPES for scope in scopes)
                or len(set(scopes)) != len(scopes)):
            raise ApiError(400, 'INVALID_CLIENT_FIELDS', 'Provide an app name and valid unique scopes.')
        if len(self.state['clients']) >= 16:
            raise ApiError(409, 'CLIENT_LIMIT', 'Revoke a paired app before adding another.')
        secret = secrets.token_urlsafe(32)
        ident = 'client-' + secrets.token_hex(12)
        sequence = self.state['sequence'] + 1
        row = {'client_id': ident, 'name': name, 'scopes': scopes[:], 'created_seq': sequence,
               'last_used_utc_best_effort': None, 'secret_hash': password_hash(secret)}
        state = {'sequence': sequence, 'clients': self.state['clients'] + [row]}
        atomic_json(self.path, state)
        self.state = state
        return {'client_id': ident, 'client_secret': secret, 'scopes': scopes[:], 'device_id': self.device_id}

    def authenticate(self, ident, secret):
        row = next((row for row in self.state['clients'] if row['client_id'] == ident), None)
        matches = password_matches(secret, row['secret_hash'] if row else self.dummy_hash)
        if not row or not matches:
            raise ApiError(401, 'INVALID_CLIENT', 'INVALID_CLIENT')
        row['last_used_utc_best_effort'] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        atomic_json(self.path, self.state)
        return row

    def revoke(self, ident=None):
        rows = [row for row in self.state['clients'] if ident is not None and row['client_id'] != ident]
        if ident is not None and len(rows) == len(self.state['clients']):
            raise ApiError(404, 'CLIENT_NOT_FOUND', 'Paired app not found.')
        state = {'sequence': self.state['sequence'] + 1, 'clients': rows}
        atomic_json(self.path, state)
        self.state = state
