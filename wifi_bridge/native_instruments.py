"""Native instrument actions over the existing scheduler and protection engine."""
import re
import threading
from .api import ApiError
from .config import profiles, reserved_hotspot_mac
from .instruments import endpoint
from .radio_roles import role_allows


class NativeInstruments:
    def __init__(self, api):
        self.api, self.c = api, api.controller
        self.waiters = threading.BoundedSemaphore(4)

    def read(self, ident=None, client=None, after=None):
        c = self.c
        with c.lock:
            c.protection.prune()
            items = c.instrument_catalog(c.status(), client)
            if ident is not None:
                item = next((item for item in items if item['id'] == ident), None)
                if item is None: raise ApiError(404, 'TELESCOPE_NOT_FOUND', 'Unknown telescope profile.')
                return item
            from .instrument_pages import page, InstrumentPageError
            try:
                result = page(items, c.event_store.boot_id, after)
            except InstrumentPageError as error:
                raise ApiError(409 if error.code == 'CATALOG_CHANGED' else 400, error.code,
                               'Instrument catalog changed. Restart listing instruments.' if error.code == 'CATALOG_CHANGED' else 'Invalid instrument cursor.') from None
            return dict(result, config_revision=self.api.revision(), boot_id=c.event_store.boot_id,
                        latest_event_seq=c.event_store.sequence)

    def endpoint(self, ident, path, local_address=None):
        with self.c.lock:
            camera = self.c.camera_native.find(ident, local_address)
            if camera:
                value = self.c.camera_native.endpoint(ident, path, local_address, row=camera)
            elif self.c.stations.find(ident):
                value = self.c.stations.endpoint(ident, path)
            else:
                self.c.protection.profile(ident)
                value = endpoint(self.c.status(), ident, path)
            if not value: raise ApiError(409, 'NOT_CONNECTED', 'Connection did not reach readiness.')
            return value

    def admission(self, profile, allowed):
        c = self.c
        if any(row['id'] == profile['id'] and row.get('connected') for row in c.state['telescopes']): return
        reserved = {c.store.config.get('home', {}).get('mac'), c.state['hotspot'].get('mac'), reserved_hotspot_mac(c.store.config)}
        reserved |= {p['mac'] for p in profiles(c.store.config) if p['enabled'] or p['id'] != profile['id'] and c.protection.protected(p['id'])}
        if any(d['mac'] not in reserved and role_allows(c.store.config, d['mac'], 'telescope') and c.net.radio_idle(d) for d in c.state['devices']): return
        victims = [p for p in profiles(c.store.config) if p['enabled'] and p.get('auto_replaceable') and not c.protection.protected(p['id']) and not c.pauses.held(p['id'])]
        if any(p['id'] in allowed for p in victims): return
        error = ApiError(409, 'NO_FREE_RADIO', 'No confidently free radio is available; existing connections were preserved.')
        error.details = {'replaceable': [{'instrument_id': p['id'], 'name': p['name']} for p in victims],
                         'free_radios': 0, 'simultaneous': False}
        raise error

    def connect(self, ident, data, expected, client, expires, path, local_address=None):
        self.api.fields(data, {'lease_seconds', 'allow_replace', 'wait_seconds', 'reserve_seconds'})
        ttl = self.c.protection.native_ttl(data.get('lease_seconds', 900))
        reserve = 'reserve_seconds' in data
        if reserve: self.c.protection.require_reservations()
        if reserve: ttl = max(ttl, self.c.protection.reservation_ttl(data['reserve_seconds']))
        wait = data.get('wait_seconds', 0); allowed = data.get('allow_replace', [])
        if (type(wait) is not int or not 0 <= wait <= 25 or not isinstance(allowed, list) or len(allowed) > 24
                or any(not isinstance(item, str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,31}', item) for item in allowed)):
            raise ApiError(400, 'INVALID_FIELDS', 'Invalid configuration.')
        c = self.c
        with c.lock:
            c.protection.precondition(expected, self.api.revision)
            if c.camera_native.find(ident):
                if reserve: raise ApiError(400, 'INVALID_FIELDS', 'Invalid configuration.')  # Cameras use no radio to reserve.
                return c.camera_native.connect(ident, client, ttl, path, local_address)
            if c.stations.find(ident):
                return c.stations.connect(ident, client, ttl, reserve, path)
            profile = c.protection.native_guard(ident, client)
            if not profile.get('client_connect', True):
                raise ApiError(403, 'CLIENT_CONNECT_DISABLED', 'Paired apps cannot connect this telescope.')
            self.admission(profile, allowed)
            old = set(c.protection.leases)
            lease = c.protection.native_create(ident, client, ttl, reservation=reserve)
            try:
                code, value = c.operations.ensure(ident, expected, self.api.revision, client, expires,
                                                  native_client=client, allow_replace=allowed)
            except ApiError as error:
                if lease['id'] not in old: c.protection.leases.pop(lease['id'], None)
                if error.code == 'ENSURE_LIMITED': error.code = 'CONNECT_LIMITED'
                raise
            except Exception:
                if lease['id'] not in old: c.protection.leases.pop(lease['id'], None)
                raise
            operation = {'id': value['id'], 'status_url': value['status_url']} if code == 202 else None
            worker = c.operations.thread if code == 202 else None
        if wait and worker and self.waiters.acquire(blocking=False):
            try: worker.join(wait)
            finally: self.waiters.release()
        with c.lock:
            if code == 200 or operation and c.operations.read(operation['id'])['state'] == 'succeeded':
                return 200, {'state': 'ready', 'instrument_id': ident, 'lease': c.protection.native_public(lease), 'endpoint': self.endpoint(ident, path)}
            return 202, {'state': 'connecting', 'operation': operation, 'lease': c.protection.native_public(lease)}

    def action(self, ident, action, data, client):
        self.api.fields(data, {'lease_id', 'lease_seconds', 'reserve_seconds'} if action == 'lease' else {'lease_id'})
        if 'lease_id' in data and (not isinstance(data['lease_id'], str) or not 1 <= len(data['lease_id']) <= 128):
            raise ApiError(400, 'INVALID_FIELDS', 'Invalid configuration.')
        c = self.c
        with c.lock:
            if c.camera_native.find(ident) or any(v.get('camera') and v['profile_id'] == ident for v in c.protection.leases.values()):
                if action in ('lease', 'release') and (not isinstance(data.get('lease_id'), str) or action == 'lease' and 'lease_seconds' not in data):
                    raise ApiError(400, 'INVALID_FIELDS', 'Invalid configuration.')
                return c.camera_native.action(ident, action, data, client)
            if action in ('lease', 'release'):
                if not isinstance(data.get('lease_id'), str) or action == 'lease' and 'lease_seconds' not in data:
                    raise ApiError(400, 'INVALID_FIELDS', 'Invalid configuration.')
                ttl = c.protection.native_ttl(data['lease_seconds']) if action == 'lease' else None
                return c.protection.native_change(ident, data['lease_id'], client, ttl, data.get('reserve_seconds'))
            if c.stations.find(ident):
                c.protection.native_guard(ident, client, owner=False)
                return c.stations.disconnect(ident, data, client)
            c.protection.native_guard(ident, client)
            own = {key: value for key, value in c.protection.leases.items() if value['profile_id'] == ident}
            if 'lease_id' in data and data['lease_id'] not in own:
                raise ApiError(409, 'INSTRUMENT_PROTECTED', 'Release reservations and explicitly remove the owner lock before changing this connection.')
            # Remove only this holder's reservation atomically with an explicit disconnect;
            # restore it unchanged if any existing controller guard refuses the action.
            for key in own: c.protection.leases.pop(key)
            try: c.telescope_action({'id': ident, 'action': 'disconnect'}, cause='API', expected='"'+self.api.revision()+'"', revision=self.api.revision)
            except Exception:
                c.protection.leases.update(own)
                raise
            if c.protection.lease_hook:
                for key in own: c.protection.lease_hook('lease.ended', ident, key, 0, 'RELEASED')
            return {'ok': True}

    def operation(self, ident, path):
        with self.c.lock:
            value = self.c.operations.read(ident)
            record = self.c.operations.records[ident]
            if record.get('native_client') is None: return dict(value, api_version='1')
            result = {key: value[key] for key in ('id', 'boot_id', 'profile_id', 'state', 'phase', 'reason', 'status_url')}
            current = endpoint(self.c.status(), value['profile_id'], path) if value['state'] == 'succeeded' and value.get('endpoint_current') else None
            return dict(result, endpoint=current, endpoint_current=current is not None,
                        progress=self.c.operations.progress(ident))
