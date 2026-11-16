"""Generic native endpoints/reservations; never operate a camera or change Wi-Fi."""
import hashlib
import math
import re
import uuid

from ..api import ApiError
from .alpaca import AlpacaError


class NativeCameras:
    def __init__(self, controller):
        self.c = controller
        self.identities = {}
        self.names = {}

    def inventory(self, local_address=None):
        with self.c.lock:
            return self._inventory(local_address)

    def _inventory(self, local_address):
        service = getattr(self.c, 'camera_service', None)
        if not service or not self.c.camera_supported:
            return []
        try:
            rows = service.inventory(local_address)
        except AlpacaError:
            return []
        used = {p['id'] for p in self.c.store.config['telescopes']}
        aliases = {}
        for row in sorted(rows, key=lambda item: item['id']):
            ident = row['id']
            if not re.fullmatch('[a-z0-9][a-z0-9-]{0,31}', ident) or ident in used:
                for attempt in range(33):
                    ident = 'cam-' + hashlib.sha256((row['id'] + ':' + str(attempt)).encode()).hexdigest()[:24]
                    if ident not in used:
                        break
            used.add(ident)
            previous = self.identities.get(row['id'])
            if previous is not None and previous != ident:
                for key, lease in list(self.c.protection.leases.items()):
                    if lease.get('camera') and lease['profile_id'] == previous:
                        self.c.protection.leases.pop(key)
                        self.c._lease_event('lease.ended', previous, key, 0, 'RELEASED')
            aliases[row['id']] = ident
            row['instrument_id'] = ident
            owner = row.get('owner')
            if owner:
                clients = self.c.state.get('hotspot', {}).get('clients', [])
                match = next((client for client in clients if client.get('ip') == owner['address']), None)
                if match and match.get('hostname'):
                    owner['dhcp_name'] = match['hostname'][:253]
        # Preserve the previous raw->native mapping long enough to name removal.
        self.identities = dict(list({**self.identities, **aliases}.items())[-32:])
        self.names.update({row['id']: row['name'] for row in rows})
        self.names = {key: value for key, value in self.names.items() if key in self.identities}
        return rows

    def find(self, ident, local_address=None):
        return next((row for row in self.inventory(local_address) if row['instrument_id'] == ident), None)

    def catalog(self, client=None, rows=None):
        from ..instruments import lease_summary
        p = self.c.protection
        result = []
        for row in self.inventory() if rows is None else rows:
            ident = row['instrument_id']
            held = [v for v in p.leases.values() if v['profile_id'] == ident and v['expires'] > p.clock()]
            mine = [v for v in held if v['client'] == client]
            enabled = self.c.camera_service.get_settings()['serve']
            discovery = row.get('discovery', {'state': 'ready'})['state']
            ready = discovery == 'ready'
            state = row['state']
            result.append({'id': ident, 'kind': 'camera', 'family': row['driver'], 'name': row['name'],
                           'protocol': 'indi' if row.get('indi', {}).get('state') == 'ready' else 'alpaca' if row.get('alpaca') else None,
                           'model': row['name'],
                           'verified_identity': {'model': row['name'], 'hardware_id': None}, 'identity_source': 'camera_driver',
                           'access': 'attached', 'simultaneous': True,
                           'link': 'disabled' if not enabled else 'connected' if ready else 'connecting' if discovery == 'preparing' else 'available',
                           'service': 'not_answering' if state == 'error' else 'ready' if ready else 'not_probed',
                           'since_seq': self.c.event_store.instrument_sequences.get(ident, 0),
                           'reason': None if ready else discovery.upper(), 'client_connect': enabled,
                           'blockers': (['CLIENT_CONNECT_DISABLED'] if not enabled else []) +
                                       (['LEASED_BY_OTHER_CLIENT'] if any(v['client'] != client for v in held) else []),
                           'lease': lease_summary(held, mine, p.clock()),
                           'capabilities': {'telemetry': [], 'maker_key': None}})
        return result

    def endpoint(self, ident, path, local_address=None, row=None):
        row = self.find(ident, local_address) if row is None else row
        if not row or row.get('discovery', {'state': 'ready'})['state'] != 'ready':
            return None
        service = self.c.camera_service
        if row.get('indi', {}).get('state') == 'ready':
            def port_at(host):
                status = service.indi_status(host)
                return status['port'] if status['state'] == 'ready' else None
            protocol = {'service': 'indi_camera', 'role': 'control', 'protocol': 'indi'}
        elif row.get('alpaca'):
            port_at = service.listening_port
            protocol = {'service': 'alpaca_camera', 'role': 'control', 'protocol': 'alpaca', 'device_number': row['alpaca']['device_number']}
        else:
            return None
        host = local_address
        if host is None:
            paths = self.c.state.get('management', {}).get('paths', [])
            candidates = [p for p in paths if ('hotspot' if p['role'] in ('ap', 'hotspot') else p['role']) == path]
            host = next((p['ip'] for p in candidates if port_at(p['ip']) is not None), None)
        port = port_at(host) if host is not None else None
        if port is None:
            return None
        return {'instrument_id': ident, 'request_path': path, 'mode': 'relay', 'requires_client_route': False,
                'target_ip': host, 'primary': {'host': host, 'ports': [dict(port=port, transport='tcp', **protocol)]},
                'alternatives': []}

    def connect(self, ident, client, ttl, path, local_address):
        endpoint = self.endpoint(ident, path, local_address)
        if endpoint is None:
            raise ApiError(409, 'NOT_CONNECTED', 'Connection did not reach readiness.')
        p = self.c.protection
        p.prune()
        if any(v['profile_id'] == ident and v['client'] != client for v in p.leases.values()):
            raise ApiError(409, 'LEASED_BY_OTHER_CLIENT', 'Release reservations and explicitly remove the owner lock before changing this connection.')
        lease = next((v for v in p.leases.values() if v['profile_id'] == ident and v['client'] == client), None)
        if lease is None:
            if len(p.leases) >= 32 or sum(v['client'] == client for v in p.leases.values()) >= 8:
                raise ApiError(429, 'LEASE_LIMITED', 'Reservation capacity reached.')
            lease = {'id': uuid.uuid4().hex, 'profile_id': ident, 'client': client,
                     'expires': p.clock() + ttl, 'version': 1, 'native': True, 'camera': True}
            p.leases[lease['id']] = lease
        return 200, {'state': 'ready', 'instrument_id': ident, 'lease': p.native_public(lease), 'endpoint': endpoint}

    def action(self, ident, action, data, client):
        p = self.c.protection
        p.prune()
        lease = p.leases.get(data.get('lease_id'))
        if action == 'disconnect' and 'lease_id' not in data:
            lease = next((v for v in p.leases.values() if v['profile_id'] == ident and v['client'] == client), None)
            if lease is None:
                return {'ok': True}
        if not lease or lease['profile_id'] != ident or not lease.get('camera'):
            raise ApiError(410, 'LEASE_EXPIRED', 'Reservation expired or service restarted. Check current protection.')
        if lease['client'] != client:
            raise ApiError(403, 'LEASE_NOT_OWNED', 'Only the owning session can renew or release this reservation.')
        if action == 'lease':
            ttl = p.native_ttl(data.get('lease_seconds'))
            lease.update(expires=p.clock() + ttl, version=lease['version'] + 1, warned=False)
            return p.native_public(lease)
        p.leases.pop(lease['id'])
        self.c._lease_event('lease.ended', ident, lease['id'], 0, 'RELEASED')
        return {'ok': True}

    def event(self, event):
        with self.c.lock:
            self.c.protection.prune()
            rows = self.inventory()
            ident = self.identities.get(event['camera_id'])
            if ident is None:
                return
            kind = event['type']
            self.c.event_store.track_instruments([p['id'] for p in self.c.store.config['telescopes']] +
                                                [row['instrument_id'] for row in rows])
            self.c.event_store.native(kind.replace('_', '.', 1), ident)
            self.c.event_store.append(kind, operation_id=uuid.uuid4().hex, profile_id=ident,
                                      profile_name=self.names.get(event['camera_id'], ident),
                                      radio_id=None, cause='RECONCILE', reason=kind.upper(),
                                      outcome='disconnected' if kind == 'camera_removed' else 'accepted')
            if kind == 'camera_removed':
                for key, lease in list(self.c.protection.leases.items()):
                    if lease['profile_id'] == ident and lease.get('camera'):
                        self.c.protection.leases.pop(key)
                        self.c._lease_event('lease.ended', ident, key, 0, 'RELEASED')
