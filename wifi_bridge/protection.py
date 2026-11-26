"""Bounded authenticated reservations and persistent owner protection, no eviction."""
import copy
import hmac
import ipaddress
import math
import re
import time
import uuid

from .api import ApiError
from .config import network_intent, password_matches, profiles, role_for


class ConnectionProtection:
    def __init__(self, controller, clock=time.monotonic):
        self.c, self.clock = controller, clock
        self.lease_hook = None  # Redacted event consumer; never includes the owning client.
        self.leases = {}  # RAM only. Revoking authentication does not revoke exposure protection.

    def prune(self):
        now = self.clock()
        for value in list(self.leases.values()):
            if value['expires'] <= now:
                if self.lease_hook: self.lease_hook('lease.ended', value['profile_id'], value['id'], 0, 'EXPIRED')
            elif value['expires'] - now <= 60 and not value.get('warned'):
                value['warned'] = True
                if self.lease_hook: self.lease_hook('lease.expiring', value['profile_id'], value['id'], math.ceil(value['expires'] - now))
        self.leases = {key:value for key,value in self.leases.items() if value['expires'] > now}

    def protected(self, ident=None):
        with self.c.lock:
            self.prune()
            # Station leases hold no radio: they never protect network changes.
            stations = {p['id'] for p in self.c.store.config.get('stations', [])}
            return any(p.get('owner_locked', False) and (ident is None or p['id'] == ident)
                       for p in profiles(self.c.store.config)) or any((ident is None or v['profile_id'] == ident) and v['profile_id'] not in stations
                                                                       for v in self.leases.values())

    def reject(self, ident=None):
        if self.protected(ident):
            raise ApiError(409, 'CONNECTION_PROTECTED', 'Release reservations and explicitly remove the owner lock before changing this connection.')

    def guard(self, previous, value, reboot=False):
        if reboot or any(previous.get(key) != value.get(key) for key in ('home','country','hostname','hotspot','hotspot_key','admin_hash')):
            self.reject()
        before = {p['id']:p for p in profiles(previous)}
        after = {p['id']:p for p in profiles(value)}
        for ident, old in before.items():
            new = after.get(ident)
            if network_intent(old) != network_intent(new) or old.get('physical_binding') != (new or {}).get('physical_binding'):
                self.reject(ident)

        # Reject ordinary reconnect/create edits before association when their
        # target already falls inside a protected, validated live subnet.
        protected=[row for row in self.c.state['telescopes'] if row.get('connected') and row.get('subnet') and self.protected(row['id'])]
        for ident,new in after.items():
            if not new['enabled'] or new.get('isolated') or network_intent(before.get(ident))==network_intent(new):continue
            if any(row['id']!=ident and not after.get(row['id'],{}).get('isolated') and ipaddress.ip_address(new['target_ip']) in ipaddress.ip_network(row['subnet']) for row in protected):
                raise ApiError(409,'CONNECTION_PROTECTED','The requested target overlaps a protected instrument subnet. Release its protection before changing networks.')

    def reauthenticate(self, password):
        c = self.c; now = time.monotonic()
        while c.password_attempts and now-c.password_attempts[0] >= 60: c.password_attempts.popleft()
        if len(c.password_attempts) >= 5: raise ApiError(429, 'REAUTHENTICATION_LIMITED', 'Too many password attempts. Try again later.')
        c.password_attempts.append(now)
        if not isinstance(password,str) or len(password)>128:
            raise ApiError(403,'REAUTHENTICATION_FAILED','Current administrator password is required.')
        encoded = c.store.config['admin_hash']
        valid = password_matches(password,encoded) if encoded else hmac.compare_digest(password.encode(),c.bootstrap['password'].encode())
        if not valid: raise ApiError(403,'REAUTHENTICATION_FAILED','Current administrator password is required.')

    def profile(self, ident):
        value = next((p for p in profiles(self.c.store.config) if p['id'] == ident),None)
        if value is None: raise ApiError(404,'TELESCOPE_NOT_FOUND','Unknown telescope profile.')
        return value

    def native_profile(self, ident):
        """A radio profile or a station instrument (native leases cover both)."""
        value = next((p for p in self.c.store.config.get('stations', []) if p['id'] == ident), None)
        return value if value is not None else self.profile(ident)

    def require_reservations(self):
        """Owner switch auto_policy.night_reservations (allowed by default)."""
        if self.c.store.config.get('auto_policy', {}).get('night_reservations', True) is False:
            raise ApiError(403, 'RESERVATIONS_DISABLED', 'The owner does not allow night reservations on this bridge.')

    def precondition(self, expected, revision):
        if expected is None or expected in ('*','"*"'): raise ApiError(428,'PRECONDITION_REQUIRED','Provide the actual current configuration revision.')
        if expected != '"'+revision()+'"': raise ApiError(412,'PRECONDITION_FAILED','Configuration changed. Reload it.')

    def public(self, value, client):
        return {'id':value['id'],'profile_id':value['profile_id'],'version':value['version'],
                'remaining_seconds':max(0,int(value['expires']-self.clock())), 'owned':value['client']==client}

    def status(self, client=None):
        with self.c.lock:
            self.prune()
            return {'leases':[self.public(v,client) for v in self.leases.values()],
                    'owner_locks':[p['id'] for p in profiles(self.c.store.config) if p.get('owner_locked',False)],
                    'restart_boundary':'RAM_LEASES_LOST_OWNER_LOCKS_PERSIST'}

    def create(self, ident, data, client, expected, revision):
        if not isinstance(data,dict) or set(data)-{'ttl_seconds'}: raise ApiError(400,'INVALID_FIELDS','Provide only ttl_seconds.')
        ttl = self.ttl(data.get('ttl_seconds',120))
        with self.c.lock:
            self.precondition(expected,revision);self.prune();self.profile(ident)
            if self.c.operations.pending or self.c.action_at is not None or self.c.stop.is_set() or self.c.certificate_job_pending or self.c.certificates.busy() or self.c.net_lock.locked():
                raise ApiError(409,'NETWORK_BUSY','Connection or maintenance work is pending.')
            from .updates import Updates
            if Updates(self.c.store.path.parent,self.c.demo).busy(): raise ApiError(409,'UPDATE_IN_PROGRESS','An application update is in progress.')
            row=next((r for r in self.c.state['telescopes'] if r['id']==ident),None)
            profile=self.profile(ident)
            if (not self.c.last_cycle_ok or not row or not row.get('relay_ready') or not profile['enabled']
                    or network_intent(profile)!=network_intent(self.c.applied.get(role_for(profile)))):
                raise ApiError(409,'CONNECTION_NOT_READY','A reservation protects an already ready connection; it does not connect it.')
            existing=next((v for v in self.leases.values() if v['client']==client and v['profile_id']==ident),None)
            if existing: return self.public(existing,client)
            if len(self.leases)>=32 or sum(v['client']==client for v in self.leases.values())>=8:
                raise ApiError(429,'LEASE_LIMITED','Reservation capacity reached.')
            ident_lease=uuid.uuid4().hex
            value={'id':ident_lease,'profile_id':ident,'client':client,'expires':self.clock()+ttl,'version':1}
            self.leases[ident_lease]=value
            return self.public(value,client)

    @staticmethod
    def ttl(value):
        if type(value) is not int or not 30<=value<=900: raise ApiError(400,'INVALID_FIELDS','Reservation duration must be 30 to 900 seconds.')
        return value

    def change(self, ident, data, client, *, release=False, owner=False, expected=None, revision=None):
        allowed={'version','confirm_release','current_password'} if owner else {'version'} if release else {'version','ttl_seconds'}
        if not isinstance(data,dict) or set(data)-allowed or type(data.get('version')) is not int or data['version']<1:
            raise ApiError(400,'INVALID_FIELDS','Provide the current reservation version and fixed action fields.')
        if not isinstance(ident,str) or not re.fullmatch('[0-9a-f]{32}',ident):raise ApiError(400,'INVALID_LEASE_ID','Provide a reservation ID.')
        ttl=self.ttl(data.get('ttl_seconds',120)) if not release else None
        with self.c.lock:
            self.prune();value=self.leases.get(ident)
            if value is None: raise ApiError(410,'LEASE_EXPIRED','Reservation expired or service restarted. Check current protection.')
            if owner:
                self.precondition(expected,revision)
                if data.get('confirm_release') is not True: raise ApiError(400,'CONFIRMATION_REQUIRED','Confirm release of this reservation explicitly.')
                self.reauthenticate(data.get('current_password'))
            elif value['client']!=client: raise ApiError(403,'LEASE_NOT_OWNED','Only the owning session can renew or release this reservation.')
            if data['version']!=value['version']: raise ApiError(412,'LEASE_VERSION_CHANGED','Reservation was renewed. Reload before releasing it.')
            if release:
                self.leases.pop(ident)
                if self.lease_hook: self.lease_hook('lease.ended', value['profile_id'], ident, 0, 'RELEASED')
                return {'released':True,'id':ident}
            value.update(expires=self.clock()+ttl,version=value['version']+1,warned=False)
            return self.public(value,client)

    @staticmethod
    def native_ttl(value):
        if type(value) is not int or not 60 <= value <= 3600:
            raise ApiError(400, 'INVALID_FIELDS', 'Invalid configuration.')
        return value

    # Contract 1.1 night reservation: an ordinary native lease with a longer horizon.
    RESERVATION_MAX = 14 * 3600

    @classmethod
    def reservation_ttl(cls, value):
        if type(value) is not int or not 60 <= value <= cls.RESERVATION_MAX:
            raise ApiError(400, 'INVALID_FIELDS', 'Invalid configuration.')
        return value

    def native_guard(self, ident, client, owner=True):
        self.prune()
        profile = self.native_profile(ident)
        if any(v['profile_id'] == ident and v['client'] != client for v in self.leases.values()):
            raise ApiError(409, 'LEASED_BY_OTHER_CLIENT', 'Release reservations and explicitly remove the owner lock before changing this connection.')
        if owner and profile.get('owner_locked'):
            error = ApiError(409, 'INSTRUMENT_PROTECTED', 'Release reservations and explicitly remove the owner lock before changing this connection.')
            error.details = {'reason': 'OWNER_LOCKED'}
            raise error
        return profile

    def native_create(self, ident, client, ttl, reservation=False):
        # Caller holds the same controller lock through admission and lease creation.
        self.native_guard(ident, client)
        existing = next((v for v in self.leases.values() if v['profile_id'] == ident and v['client'] == client), None)
        if existing:
            if reservation:
                # Requesting a reservation on an existing lease extends it, never shortens it.
                existing.update(expires=max(existing['expires'], self.clock() + ttl), reservation=True,
                                version=existing['version'] + 1, warned=False)
            return existing
        if len(self.leases) >= 32 or sum(v['client'] == client for v in self.leases.values()) >= 8:
            raise ApiError(429, 'LEASE_LIMITED', 'Reservation capacity reached.')
        value = {'id': uuid.uuid4().hex, 'profile_id': ident, 'client': client,
                 'expires': self.clock() + ttl, 'version': 1, 'native': True}
        if reservation: value['reservation'] = True
        self.leases[value['id']] = value
        return value

    def native_change(self, ident, lease_id, client, ttl=None, reserve=None):
        with self.c.lock:
            self.prune(); self.native_profile(ident)
            if reserve is not None: self.require_reservations()
            value = self.leases.get(lease_id) if isinstance(lease_id, str) else None
            if not value or value['profile_id'] != ident:
                raise ApiError(410, 'LEASE_EXPIRED', 'Reservation expired or service restarted. Check current protection.')
            if value['client'] != client:
                raise ApiError(403, 'LEASE_NOT_OWNED', 'Only the owning session can renew or release this reservation.')
            # Version selection and mutation are atomic under the legacy protection lock.
            if ttl is None:
                self.leases.pop(value['id'])
                if self.lease_hook: self.lease_hook('lease.ended', ident, value['id'], 0, 'RELEASED')
                return {'ok': True}
            ttl = self.native_ttl(ttl)
            if reserve is not None:
                expires = self.clock() + max(ttl, self.reservation_ttl(reserve)); value['reservation'] = True
            elif value.get('reservation'):
                # Ordinary renewals of a reservation keep its horizon (1.0 clients renew within an hour).
                expires = max(value['expires'], self.clock() + ttl)
            else:
                expires = self.clock() + ttl
            value.update(expires=expires, version=value['version'] + 1, warned=False)
            return self.native_public(value)

    def native_public(self, value):
        remaining = max(0, math.ceil(value['expires'] - self.clock()))
        # expires_in keeps its 1.0 bound (3600); a reservation reports its horizon separately.
        result = {'id': value['id'], 'expires_in': min(remaining, 3600)}
        if value.get('reservation'): result['reservation_expires_in'] = remaining
        return result

    def owner_lock(self, ident, data, expected, revision):
        if not isinstance(data,dict) or set(data)!={'locked','current_password'} or type(data['locked']) is not bool:
            raise ApiError(400,'INVALID_FIELDS','Provide locked and current_password.')
        with self.c.lock:
            self.precondition(expected,revision);self.profile(ident)
            if self.c.operations.pending or self.c.action_at is not None or self.c.stop.is_set() or self.c.certificate_job_pending or self.c.certificates.busy() or self.c.net_lock.locked():
                raise ApiError(409,'NETWORK_BUSY','Connection or maintenance work is pending.')
            from .updates import Updates
            if Updates(self.c.store.path.parent,self.c.demo).busy(): raise ApiError(409,'UPDATE_IN_PROGRESS','An application update is in progress.')
            self.reauthenticate(data['current_password'])
            value=copy.deepcopy(self.c.store.config)
            next(p for p in value['telescopes'] if p['id']==ident)['owner_locked']=data['locked']
            self.c.store.save(value)
            return {'profile_id':ident,'locked':data['locked']}
