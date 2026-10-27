"""Bounded authenticated discovery observations, separate from connection intent."""
import copy
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import time

from .api import ApiError
from .config import Invalid, profiles, text, reserved_hotspot_mac, atomic_json, role_for
from .plugins import ssid_plugin, plugin, onboarding_defaults

MAC = re.compile(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}')


def alias(ssid, bssid):
    ssid = text(ssid, 32); bssid = text(bssid, 17).lower()
    if not 1 <= len(ssid.encode()) <= 32 or not MAC.fullmatch(bssid) or int(bssid[:2], 16) & 1 or bssid == '00:00:00:00:00:00':
        raise Invalid('Invalid physical access point.')
    return {'ssid': ssid, 'bssid': bssid}


def validate_binding(value):
    if value is None: return None
    if (not isinstance(value, dict) or set(value) != {'version', 'aliases', 'reported_identity', 'client_observation'}
            or type(value['version']) is not int or value['version'] != 1
            or value['client_observation'] is not None or value['reported_identity'] is not None or not isinstance(value['aliases'], list)
            or not 1 <= len(value['aliases']) <= 8): raise Invalid('Invalid physical binding.')
    normalized, seen = [], set()
    for item in value['aliases']:
        if not isinstance(item, dict) or set(item) - {'ssid', 'bssid', 'source', 'observed_at', 'frequency'} or not {'ssid', 'bssid', 'source', 'observed_at'} <= set(item): raise Invalid('Invalid physical alias.')
        entry = alias(item['ssid'], item['bssid'])
        if entry['bssid'] in seen or item['source'] not in ('owner-confirmed', 'wifi-observed'): raise Invalid('Invalid physical alias provenance.')
        stamp = item['observed_at']
        if stamp is not None and (type(stamp) is not int or not 0 <= stamp <= 4102444800): raise Invalid('Invalid observation time.')
        if item['source'] == 'owner-confirmed' and stamp is not None: raise Invalid('Owner confirmation is not a measured observation.')
        if item['source'] == 'wifi-observed' and stamp is None: raise Invalid('Observation time required.')
        frequency = item.get('frequency')
        if frequency is not None and (item['source'] != 'wifi-observed' or type(frequency) is not int or not 2300 <= frequency <= 7125):
            raise Invalid('Invalid last-seen frequency hint.')
        seen.add(entry['bssid']); normalized.append(dict(entry, source=item['source'], observed_at=stamp,
            **({'frequency': frequency} if frequency is not None else {})))
    return {'version': 1, 'aliases': normalized, 'reported_identity': None, 'client_observation': None}


def scan_candidate(value, observed):
    if not isinstance(value, dict): raise Invalid('Invalid scan result.')
    point = alias(value.get('ssid'), value.get('bssid'))
    signal, frequency = value.get('signal'), value.get('frequency')
    security = text(value.get('security'), 96)
    if type(signal) is not int or not 0 <= signal <= 100 or type(frequency) is not int or not 2300 <= frequency <= 7125:
        raise Invalid('Invalid radio observation.')
    return dict(point, mode='wifi', source='wifi-scan', observed_at=observed,
                clock_reliable=False, signal=signal, frequency=frequency, security=security,
                suggested=dict(onboarding_defaults(point['ssid']),source='ssid-hint-and-defaults'), reported_identity=None)


def seestar_probe(device, address, target):
    """One interface-bound read-only scan request, never TCP/control or a sweep."""
    interface = ipaddress.IPv4Interface(address); remote = ipaddress.IPv4Address(target)
    if (remote not in interface.network or remote in (interface.ip, interface.network.network_address,
            interface.network.broadcast_address) or not remote.is_private or remote.is_loopback or remote.is_link_local): return None
    ident = secrets.randbelow(2**30)
    payload = json.dumps({'id': ident, 'method': 'scan_iscope', 'params': ''}).encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device['name'].encode() + b'\0')
        sock.bind((str(interface.ip), 0)); sock.settimeout(0.75)
        sock.sendto(payload, (str(remote), 4720))
        data, origin = sock.recvfrom(4097)
    if origin != (str(remote), 4720) or len(data) > 4096: return None
    value = json.loads(data)
    if (not isinstance(value, dict) or value.get('method') != 'scan_iscope'
            or type(value.get('id')) is not int or value['id'] != ident or 'error' in value
            or 'code' in value and (type(value['code']) is not int or value['code'] != 0)): return None
    # Response schema for a stable serial/model is not established. Never invent it.
    return {'address': str(remote), 'source': 'seestar-scan-iscope', 'reported_identity': None,
            'identity_reason': 'IDENTITY_FIELDS_UNSUPPORTED'}


class CandidateDiscovery:
    def __init__(self, controller, clock=time.monotonic, wall=time.time):
        self.controller, self.clock, self.wall = controller, clock, wall
        self.last = None
        self.nearby_rows=()
        self.nearby_at=None
        self.background_reason='WAITING'
        self.next_background=0
        self.observations={}
        self.persisted_catalog=None
        self.catalog_path=controller.store.path.parent/'discovered-wifi.json'
        try:
            from .notifications import private_json
            saved=private_json(self.catalog_path)
            if set(saved)!={'format','networks'} or saved['format']!=1 or not isinstance(saved['networks'],list) or len(saved['networks'])>64:raise ValueError()
            for entry in saved['networks']:
                if set(entry)!={'ssid','bssid','signal','frequency','security','observed_at'} or type(entry['observed_at']) is not int or not 0<=entry['observed_at']<=4102444800:raise ValueError()
                self.remember(scan_candidate(entry,entry['observed_at']),persist=False)
        except (OSError,ValueError,TypeError,Invalid):self.observations={}

        self.notification_scans = ()
        self.candidates = {}  # <=64 entries; expires after 120 monotonic seconds.

    def prune(self):
        now = self.clock()
        self.candidates = {key: value for key, value in self.candidates.items() if value[0] > now}

    def run(self, data, cached=False, background=False):
        if not isinstance(data, dict) or set(data) != {'mode'} or data['mode'] not in ('wifi', 'reachable'):
            raise ApiError(400, 'INVALID_FIELDS', 'Choose wifi or reachable discovery.')
        from .network import NetworkError
        c = self.controller
        if not c.net_lock.acquire(blocking=False): raise ApiError(409, 'NETWORK_BUSY', 'A network operation is already running.')
        try:
            with c.lock:
                self.prune()
                if (c.operations.pending or c.stop.is_set() or c.action_at is not None or c.certificate_job_pending
                        or c.certificates.busy() or c.pending_deletions):
                    raise ApiError(409, 'NETWORK_BUSY', 'Management or connection maintenance is pending.')
                from .updates import Updates
                if Updates(c.store.path.parent, c.demo).busy(): raise ApiError(409, 'UPDATE_IN_PROGRESS', 'An application update is in progress.')
                if not cached and not background and self.last is not None and self.clock()-self.last < 15:
                    raise ApiError(429, 'DISCOVERY_LIMITED', 'Wait before discovering devices again.')
                config = copy.deepcopy(c.store.config)
                devices = copy.deepcopy(c.state.get("devices",[])) if cached or background else c.net.devices()
                if background:
                    occupied=any(row.get('connected') for row in c.state['telescopes']) or any(p['enabled'] for p in profiles(config))
                    recent=self.last is not None and self.clock()-self.last<15
                    if occupied or recent or not c.last_cycle_ok:cached=True
                    self.background_reason='CONNECTED' if occupied else 'RECENT_SCAN' if recent else 'WAITING'
                if not cached and not background:self.last = self.clock()
                c.discovery_pending = True
                live_ids = {row['id'] for row in c.state['telescopes'] if row.get('connected')}
                protected = {config.get('home', {}).get('mac'), c.state.get('hotspot', {}).get('mac'), reserved_hotspot_mac(config)}
                protected |= {p['mac'] for p in profiles(config) if p['enabled'] or c.protection.protected(p['id'])}
            if data['mode'] == 'wifi':
                selected = next((d for d in sorted(devices, key=lambda d:-len(d.get('bands', [])))
                                 if d['mac'] not in protected and c.net.radio_idle(d)), None) if not cached else None
                source='fresh'
                if not selected and not cached and not background:
                    selected=next((d for d in devices if d['mac']==config.get('home',{}).get('mac') and c.net.scan_station(d,config['home'])),None)
                    source='connected_station'
                if not selected and not cached and not background:
                    activity=c.net.activity_status().get('profiles',{}) if hasattr(c.net,'activity_status') else {}
                    for profile in profiles(config):
                        sample=activity.get(profile['id'],{})
                        if (not profile['enabled'] or c.protection.protected(profile['id']) or sample.get('state')!='quiet'
                                or sample.get('sample_age_seconds',100)>15 or sample.get('raw_active') or sample.get('tcp_established')!=0):continue
                        device=next((d for d in devices if d['mac']==profile['mac']),None)
                        if device and c.net.scan_station(device,profile,role_for(profile)):
                            selected=device;source='connected_station';break
                scan_failed=False
                if selected:
                    try:rows=c.net.scan(selected['name'],timeout=10) if background else c.net.scan(selected['name'])
                    except NetworkError:rows=[];source='cached';scan_failed=True
                else:rows=[];source='cached'
                if source=='cached':
                    for device in devices[:8]:
                        if device['mac']==c.state.get('hotspot',{}).get('mac'):continue
                        try:rows.extend(c.net.cached_scan(device['name']))
                        except (NetworkError,ValueError):continue
                if scan_failed and not rows:raise NetworkError('Scanning is unavailable.')
                candidates, seen = [], set()
                for row in rows[:256] if isinstance(rows, list) else []:
                    try: candidate = scan_candidate(row, max(0, min(int(self.wall()), 4102444800)))
                    except (Invalid, ValueError, TypeError): continue
                    if candidate['bssid'] in seen: continue
                    seen.add(candidate['bssid']); candidate['scan_source']=source;self.remember(candidate,persist=False);candidates.append(candidate)
                    if len(candidates) == 64: break
                with c.lock:
                    if source!='cached':
                        self.nearby_rows=tuple(copy.deepcopy(candidates));self.nearby_at=self.clock()
                    if background and self.background_reason not in ('CONNECTED','RECENT_SCAN'):
                        self.background_reason='NO_SPARE_RADIO' if source=='cached' else 'SCANNED'
                    if candidates:self.persist_observations()
                return self.result(candidates, 'OBSERVED' if candidates else 'NO_SPARE_RADIO' if source=='cached' else 'NO_CANDIDATES', wifi=source!='cached', preserve=cached or background, scan_source=source)
            candidates, attempted = [], 0
            for profile in profiles(config)[:24]:
                identity=plugin(profile['kind'])['identification']
                if identity=='none' or not profile['enabled'] or profile['id'] not in live_ids: continue
                device = next((d for d in devices if d['mac'] == profile['mac']), None)
                if not device or profile['mac'] == config.get('home', {}).get('mac'): continue
                attempted += 1
                if attempted > 4: break
                if not c.net.discovery_associated(device, profile): continue
                try: observation = c.net.discover_seestar(device, profile) if identity=='seestar_scan_iscope' else c.net.discover_mount(device, profile)
                except (OSError, ValueError, UnicodeError, RecursionError): observation = None
                if observation:
                    candidates.append(dict(observation, mode='reachable', profile_id=profile['id'],
                        observed_at=max(0, min(int(self.wall()), 4102444800)), clock_reliable=False))
                if attempted == 4: break
            return self.result(candidates, 'OBSERVED' if candidates else 'NO_SUPPORTED_REACHABLE_DEVICE')
        except NetworkError:
            raise ApiError(409, 'DISCOVERY_UNAVAILABLE', 'The radio state or scan could not be established safely.') from None
        finally:
            with c.lock: c.discovery_pending = False
            c.net_lock.release()

    def result(self, candidates, reason, wifi=False, preserve=False, scan_source=None):
        if wifi:
            points=tuple((p["ssid"],p["bssid"]) for p in candidates[:64])
            self.notification_scans=(self.notification_scans+((self.clock(),points),))[-4:]
        self.prune()
        # One snapshot only; older candidate choices expire rather than grow without bound.
        if not preserve:self.candidates.clear()
        public = []
        for candidate in candidates:
            key = secrets.token_hex(16); value = dict(candidate, candidate_id=key)
            if candidate['mode']=='wifi':value['observation_id']=self.observation_id(candidate['ssid'],candidate['bssid'])
            if not preserve:self.candidates[key] = (self.clock()+120, value)
            public.append(value)
        return {'candidates': public, 'reason': reason, 'expires_in': 120,'scan_source':scan_source,
                'physical_binding': {'supported': True, 'verification': 'observed-or-owner-confirmed'},
                'automatic_selection': {'supported': True, 'reason': 'EXPLICIT_PROFILE_ENSURE_ONLY', 'consent_required': True}}

    @staticmethod
    def observation_id(ssid,bssid):return hashlib.sha256((ssid+'\0'+bssid).encode()).hexdigest()[:32]

    def remember(self,candidate,persist=True):
        ident=self.observation_id(candidate['ssid'],candidate['bssid'])
        entry={k:candidate[k] for k in ('ssid','bssid','signal','frequency','security','observed_at')}
        with self.controller.lock:
            previous=self.observations.get(ident)
            if candidate.get('scan_source')=='cached' and previous and all(previous[k]==v for k,v in entry.items() if k!='observed_at'):return ident
            self.observations.pop(ident,None);self.observations[ident]=entry
            self.observations=dict(list(self.observations.items())[-64:])
            if persist:self.persist_observations()
        return ident

    def persist_observations(self):
        value={'format':1,'networks':list(self.observations.values())};serialized=json.dumps(value,sort_keys=True)
        if serialized==self.persisted_catalog:return
        try:atomic_json(self.catalog_path,value);self.persisted_catalog=serialized
        except OSError:pass

    def lookup(self,ident):
        with self.controller.lock:entry=copy.deepcopy(self.observations.get(ident))
        if not entry:raise ApiError(404,'OBSERVATION_UNAVAILABLE','This network observation is no longer available. Scan again.')
        candidate=scan_candidate(entry,entry['observed_at']);candidate['observation_id']=ident
        return candidate

    def nearby(self):
        # Authenticated status reads are memory-only and never start a scan.
        c=self.controller
        occupied=any(row.get('connected') for row in c.state['telescopes']) or any(p['enabled'] for p in profiles(c.store.config))
        age=max(0,int(self.clock()-self.nearby_at)) if self.nearby_at is not None else None
        choices=[];seen=set()
        if not occupied and age is not None and age<120:
            for row in sorted(self.nearby_rows,key=lambda p:-p['signal']):
                if row['suggested']['kind']=='generic' or row['ssid'] in seen:continue
                if any(p['ssid']==row['ssid'] and (not p.get('bssid') or p['bssid']==row['bssid']) for p in profiles(c.store.config)):continue
                seen.add(row['ssid']);choices.append({'observation_id':self.observation_id(row['ssid'],row['bssid']),
                    'ssid':row['ssid'],'kind':row['suggested']['kind'],'signal':row['signal']})
        return {'candidates':choices,'scan_age_seconds':age,'interval_seconds':45,
                'reason':'CONNECTED' if occupied else self.background_reason}

    def background_tick(self):
        if self.clock()<self.next_background:return
        try:self.run({'mode':'wifi'},background=True)
        except ApiError as error:
            self.background_reason=error.code
            self.next_background=self.clock()+(5 if error.code in ('NETWORK_BUSY','UPDATE_IN_PROGRESS') else 45)
        except (OSError,ValueError):
            self.background_reason='DISCOVERY_UNAVAILABLE';self.next_background=self.clock()+45
        else:self.next_background=self.clock()+45

    def loop(self):
        # Only an unused, positively idle radio can be scanned automatically.
        # Existing connections use cache-only observation, never station rescans.
        while not self.controller.stop.wait(5):self.background_tick()

    def binding(self, data):
        self.prune()
        if 'candidate_id' in data:
            key = data['candidate_id']
            if not isinstance(key, str) or key not in self.candidates:
                raise ApiError(409, 'CANDIDATE_EXPIRED', 'Discover again before binding this observation.')
            candidate = self.candidates[key][1]
            if candidate['mode'] != 'wifi':
                raise ApiError(409, 'IDENTITY_UNAVAILABLE', 'This response does not establish a physical identity or access point.')
            aliases = [dict(alias(candidate['ssid'], candidate['bssid']), source='wifi-observed', observed_at=candidate['observed_at'], frequency=candidate['frequency'])]
        else:
            if data.get('owner_confirmed') is not True or not isinstance(data.get('aliases'), list) or not 1 <= len(data['aliases']) <= 8:
                raise ApiError(400, 'INVALID_FIELDS', 'Confirm the physical access point aliases explicitly.')
            aliases = [dict(alias(item.get('ssid'), item.get('bssid')), source='owner-confirmed', observed_at=None)
                       for item in data['aliases'] if isinstance(item, dict) and set(item) == {'ssid', 'bssid'}]
            if len(aliases) != len(data['aliases']): raise Invalid('Invalid aliases.')
        return validate_binding({'version': 1, 'aliases': aliases, 'reported_identity': None, 'client_observation': None})
