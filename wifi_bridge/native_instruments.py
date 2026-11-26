"""Native instrument actions over the existing scheduler and protection engine."""
import re
import threading
import time
from .api import ApiError
from .config import profiles, reserved_hotspot_mac, role_for
from .instruments import endpoint
from .mount_sleep import wifi_sleep
from .plugins import plugin
from .radio_roles import role_allows

# What an app may say when it disconnects an instrument on purpose.
DISCONNECT_REASONS = {'switched_off': 'SWITCHED_OFF'}


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

    def holders(self, target, client):
        """Enabled profiles that keep an instrument radio, and what each one is doing.

        state is the one of ConnectionOperations.holder_state. unreachable and standby
        give way to an explicit request by themselves; forceable says whether naming
        the others in force_replace would be accepted.
        """
        c = self.c; config = c.store.config
        busy = {config.get('home', {}).get('mac'), c.state['hotspot'].get('mac'), reserved_hotspot_mac(config)}
        result = []
        for p in profiles(config):
            device = next((d for d in c.state['devices'] if d['mac'] == p['mac']), None)
            if (not p['enabled'] or p['id'] == target['id'] or not device or device['mac'] in busy
                    or not role_allows(config, device['mac'], 'telescope')): continue
            blocker = None
            if c.pauses.held(p['id']): blocker = 'CONNECTION_PAUSED'
            elif p.get('owner_locked'): blocker = 'OWNER_LOCKED'
            elif any(v['profile_id'] == p['id'] and v['client'] != client for v in c.protection.leases.values()): blocker = 'LEASED_BY_OTHER_CLIENT'
            category = plugin(p['kind'])['category']
            state = c.operations.holder_state(p, device)
            result.append({'instrument_id': p['id'], 'name': p['name'],
                           'kind': {'mount': 'mount', 'telescope': 'smart_telescope'}.get(category, 'other'),
                           'state': state, 'current_operation': c.operation_detail(p), 'blocker': blocker, 'forceable': blocker is None})
            # A mount that may stop its Wi-Fi once left without a client: say so before anyone confirms.
            sleep = wifi_sleep(p['kind'])
            if sleep: result[-1]['wifi_sleep'] = sleep
        return result

    def admission(self, profile, allowed, forced=(), client=None):
        """Refuse before any change when no radio can serve this request.

        Returns the holder whose radio the request is going to take without the
        owner's replaceable consent (an unreachable one, one its app released, or
        one named in force_replace), or None when a radio is free or allow_replace applies.
        """
        c = self.c
        if any(row['id'] == profile['id'] and row.get('connected') for row in c.state['telescopes']): return None
        reserved = {c.store.config.get('home', {}).get('mac'), c.state['hotspot'].get('mac'), reserved_hotspot_mac(c.store.config)}
        reserved |= {p['mac'] for p in profiles(c.store.config) if p['enabled'] or p['id'] != profile['id'] and c.protection.protected(p['id'])}
        if any(d['mac'] not in reserved and role_allows(c.store.config, d['mac'], 'telescope') and c.net.radio_idle(d) for d in c.state['devices']): return None
        holders = self.holders(profile, client)
        usable = [h for h in holders if h['forceable']]
        # Same order as the worker: an instrument out of reach gives way before a confirmed replacement.
        chosen = next((dict(h, reason='UNREACHABLE') for h in usable if h['state'] == 'unreachable'),
                      next((dict(h, reason='STANDBY') for h in usable if h['state'] == 'standby'),
                           next((dict(h, reason='FORCED') for h in usable if h['instrument_id'] in forced), None)))
        if chosen: return chosen
        victims = [p for p in profiles(c.store.config) if p['enabled'] and p.get('auto_replaceable') and not c.protection.protected(p['id']) and not c.pauses.held(p['id'])]
        if any(p['id'] in allowed for p in victims): return None
        message = 'No confidently free radio is available; existing connections were preserved.'
        if any(h.get('wifi_sleep') for h in holders):
            message += ' A mount left without a client may switch its Wi-Fi off after about 11 minutes.'
        error = ApiError(409, 'NO_FREE_RADIO', message)
        error.details = {'replaceable': [{'instrument_id': p['id'], 'name': p['name']} for p in victims],
                         'free_radios': 0, 'simultaneous': False, 'holders': holders}
        raise error

    @staticmethod
    def displaced(holder):
        value = {'instrument_id': holder['instrument_id'], 'name': holder['name'], 'kind': holder['kind'], 'reason': holder['reason']}
        if holder.get('wifi_sleep'): value['wifi_sleep'] = holder['wifi_sleep']
        return value

    def connect(self, ident, data, expected, client, expires, path, local_address=None):
        self.api.fields(data, {'lease_seconds', 'allow_replace', 'force_replace', 'wait_seconds', 'reserve_seconds'})
        ttl = self.c.protection.native_ttl(data.get('lease_seconds', 900))
        reserve = 'reserve_seconds' in data
        if reserve: self.c.protection.require_reservations()
        if reserve: ttl = max(ttl, self.c.protection.reservation_ttl(data['reserve_seconds']))
        wait = data.get('wait_seconds', 0); allowed = data.get('allow_replace', []); forced = data.get('force_replace', [])
        if (type(wait) is not int or not 0 <= wait <= 25 or any(not isinstance(items, list) or len(items) > 24
                or any(not isinstance(item, str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,31}', item) for item in items) for items in (allowed, forced))):
            raise ApiError(400, 'INVALID_FIELDS', 'Invalid configuration.')
        c = self.c
        deadline = time.monotonic() + wait
        while True:
            try:
                return self._connect(ident, expected, client, expires, path, local_address, ttl, reserve, allowed, forced, deadline)
            except ApiError as error:
                # The periodic reconcile may hold the network for a while (a retry of an
                # absent instrument takes up to 40 s). Within wait_seconds, try again.
                if error.code != 'NETWORK_BUSY' or time.monotonic() + .25 >= deadline or c.stop.is_set(): raise
            c.stop.wait(.25)

    def _connect(self, ident, expected, client, expires, path, local_address, ttl, reserve, allowed, forced, deadline):
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
            if profile['enabled'] and not any(row['id'] == ident and row.get('connected') for row in c.state['telescopes']):
                # The bridge is already retrying this one: an explicit request ends the wait between attempts.
                c.attempts.pop(role_for(profile), None); c.retries.pop(role_for(profile), None)
            taken = self.admission(profile, allowed, forced, client)
            old = set(c.protection.leases)
            lease = c.protection.native_create(ident, client, ttl, reservation=reserve)
            try:
                code, value = c.operations.ensure(ident, expected, self.api.revision, client, expires,
                                                  native_client=client, allow_replace=allowed, force_replace=forced)
            except ApiError as error:
                if lease['id'] not in old: c.protection.leases.pop(lease['id'], None)
                if error.code == 'ENSURE_LIMITED': error.code = 'CONNECT_LIMITED'
                raise
            except Exception:
                if lease['id'] not in old: c.protection.leases.pop(lease['id'], None)
                raise
            c.standby.discard(ident)  # In use again.
            operation = {'id': value['id'], 'status_url': value['status_url']} if code == 202 else None
            worker = c.operations.thread if code == 202 else None
        wait = deadline - time.monotonic()
        if wait > 0 and worker and self.waiters.acquire(blocking=False):
            try: worker.join(wait)
            finally: self.waiters.release()
        with c.lock:
            # Say which instrument gives up its radio for this request, and why.
            extra = {'displaced': self.displaced(taken)} if taken and code == 202 else {}
            if code == 200 or operation and c.operations.read(operation['id'])['state'] == 'succeeded':
                return 200, {'state': 'ready', 'instrument_id': ident, 'lease': c.protection.native_public(lease), 'endpoint': self.endpoint(ident, path), **extra}
            return 202, {'state': 'connecting', 'operation': operation, 'lease': c.protection.native_public(lease), **extra}

    def action(self, ident, action, data, client):
        self.api.fields(data, {'lease_id', 'lease_seconds', 'reserve_seconds'} if action == 'lease' else
                        {'lease_id', 'reason'} if action == 'disconnect' else {'lease_id'})
        if 'lease_id' in data and (not isinstance(data['lease_id'], str) or not 1 <= len(data['lease_id']) <= 128):
            raise ApiError(400, 'INVALID_FIELDS', 'Invalid configuration.')
        # Optional on disconnect: the instrument was switched off on purpose (cameras and stations hold no radio).
        note = None
        if 'reason' in data:
            data = dict(data); reason = data.pop('reason')
            if not isinstance(reason, str) or reason not in DISCONNECT_REASONS:
                raise ApiError(400, 'INVALID_FIELDS', 'Invalid configuration.')
            note = DISCONNECT_REASONS[reason]
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
                result = c.protection.native_change(ident, data['lease_id'], client, ttl, data.get('reserve_seconds'))
                # The app is done with it: the link stays up for a quick return, but
                # its radio no longer needs a confirmation to serve another instrument.
                if action == 'release' and not c.stations.find(ident) and not any(v['profile_id'] == ident for v in c.protection.leases.values()):
                    c.standby.add(ident)
                return result
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
            # The profile is disabled: no more attempts, its radio is free, and the state says why.
            if note: c.release_notes.note(ident, note)
            return {'ok': True}

    def operation(self, ident, path):
        with self.c.lock:
            value = self.c.operations.read(ident)
            record = self.c.operations.records[ident]
            if record.get('native_client') is None: return dict(value, api_version='1')
            result = {key: value[key] for key in ('id', 'boot_id', 'profile_id', 'state', 'phase', 'reason', 'status_url')}
            moved = value.get('displaced')
            if moved and moved.get('reason') in ('UNREACHABLE', 'STANDBY', 'FORCED'):
                # Which instrument gave up its radio for this explicit request, and why.
                other = next((p for p in profiles(self.c.store.config) if p['id'] == moved['profile_id']), None)
                result['displaced'] = {'instrument_id': moved['profile_id'], 'name': other['name'] if other else None,
                                       'kind': {'mount': 'mount', 'telescope': 'smart_telescope'}.get(plugin(other['kind'])['category'], 'other') if other else 'other',
                                       'reason': moved['reason']}
                sleep = wifi_sleep(other['kind']) if other else None
                if sleep: result['displaced']['wifi_sleep'] = sleep
            if value.get('failure_stage') and value['state'] == 'failed': result['failure_stage'] = value['failure_stage']
            if value['state'] == 'failed' and value.get('reason') == 'NETWORK_UNOBSERVED':
                # Not heard: for a mount that stops its Wi-Fi without a client, say since when and why, probably.
                quiet = self.c.mount_silences().get(value['profile_id'])
                if quiet: result['silence'] = quiet
            current = endpoint(self.c.status(), value['profile_id'], path) if value['state'] == 'succeeded' and value.get('endpoint_current') else None
            return dict(result, endpoint=current, endpoint_current=current is not None,
                        progress=self.c.operations.progress(ident))
