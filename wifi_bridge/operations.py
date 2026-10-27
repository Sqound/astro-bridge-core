"""One bounded connection/replacement/restoration operation, never replayed on boot."""
from collections import deque
import copy
import ipaddress
import hashlib
import hmac
import json
import secrets
import re
import threading
import time
import uuid

from .api import ApiError
from .candidates import scan_candidate
from .config import profiles, role_for, network_intent, auto_policy, reserved_hotspot_mac
from .network import NetworkError
from .plugins import relay_plugins, proxy_flag, plugin, demand_plugins
from .updates import Updates


def refused(code, message):
    raise ApiError(409, code, message)


class ConnectionOperations:
    def __init__(self, controller, clock=time.monotonic):
        self.c = controller
        self.clock = clock
        self.epoch = controller.event_store.boot_id
        self.records = {}
        self.clients = {}
        self.active = None
        self.overlay = None
        self.allocated = None
        self.thread = None
        self.last = None
        self.deadline = 0
        self.attempted = False
        self.failure = None
        self.revision = None
        self.expected = None
        self.victim = None
        self.restoring = False
        self.restore_attempted = False
        self.restore_failed = False
        self.negative = {}
        self.cooldowns = {}
        self.started = self.clock()
        self.last_replacement = self.started
        self.restore_deadline = 0
        self.force_observed = {}
        self.demand_key = secrets.token_bytes(32)
        self.demand_next = 0
        self.demand_last = {'reason':'DISABLED','observed':self.clock()}
        self.vespera_last = dict(self.demand_last)
        self.dwarf_last = dict(self.demand_last)
        self.zwo_mount_last = dict(self.demand_last)
        self.synscan_last = dict(self.demand_last)



    @property
    def cause(self):
        return self.records[self.active]['public']['cause'] if self.active else 'AUTOMATIC'

    @property
    def victim_id(self):
        return self.victim['id'] if self.victim else None

    @property
    def pending(self):
        return self.active is not None

    def prune(self):
        now = self.clock()
        self.records = {key: value for key, value in self.records.items()
                        if key == self.active or now - value['observed'] < 600}
        while len(self.records) > 64:
            oldest = next(key for key in self.records if key != self.active)
            self.records.pop(oldest)
        self.clients = {key: value for key, value in self.clients.items() if value['expires'] > now}
        self.negative = dict(list((key, until) for key, until in self.negative.items() if until > now)[-64:])
        current = {p['id'] for p in profiles(self.c.store.config)}
        self.force_observed = {key:value for key,value in self.force_observed.items() if key in current}
        self.cooldowns = dict(sorted(self.cooldowns.items(), key=lambda item:item[1])[-72:])

    def retain_clients(self, live):
        with self.c.lock: self.clients = {key:value for key,value in self.clients.items() if key in live}

    def public(self, ident):
        self.prune()
        value = self.records.get(ident)
        if value is None: return None
        result = copy.deepcopy(value['public']) | {'age_seconds': max(0, int(self.clock()-value['observed']))}
        result['endpoint_current'] = False
        if result['state'] == 'succeeded':
            profile = next((p for p in profiles(self.c.store.config) if p['id'] == result['profile_id']), None)
            row = next((r for r in self.c.state['telescopes'] if r['id'] == result['profile_id']), None)
            usable = (profile and row and self.c.last_cycle_ok and row.get('relay_ready')
                      and row.get('connected') and profile['bssid'] == result['physical_selection']['bssid']
                      and network_intent(profile) == network_intent(self.c.applied.get(role_for(profile))))
            result['endpoint'] = self.endpoint(profile, row, self.c.state['home_ip']) if usable else None
            result['endpoint_current'] = bool(usable)
        return result

    def read(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{32}\.[0-9a-f]{32}', ident):
            raise ApiError(400, 'INVALID_OPERATION_ID', 'Provide a service-run operation ID.')
        with self.c.lock:
            if ident.split('.')[0] != self.epoch:
                raise ApiError(410, 'OPERATION_RUN_CHANGED', 'This operation belongs to an earlier service run. Reload current status.')
            value = self.public(ident)
            if value is None: raise ApiError(410, 'OPERATION_EXPIRED', 'Operation record expired. Reload current connection status.')
            return value

    def latest(self):
        with self.c.lock: return self.public(self.active or self.last)

    def current(self, profile, row, device):
        """A ready row alone cannot prove a physical selector or current ownership."""
        if not row or not row.get('relay_ready') or not row.get('connected') or not device: return False
        actual = self.c.net.link_observation(device, profile)
        if not actual: return False
        aliases = (profile.get('physical_binding') or {}).get('aliases', [])
        if profile.get('bssid') and actual['bssid'] != profile['bssid']: return False
        if aliases and not any(a['ssid'] == profile['ssid'] and a['bssid'] == actual['bssid'] for a in aliases): return False
        return True

    @staticmethod
    def endpoint(profile, row, home_ip):
        proxy = None
        paths=[path for path in row.get('management_paths',[]) if path.get('forwarding_ready')]
        protocol=next((p['id'] for p in relay_plugins() if row.get(proxy_flag(p['id']))),None)
        ports=plugin(protocol)['relay']['ports'] if protocol else None
        alternatives=[{'role':path['role'],'host':path['host'],'subnet':path['subnet'],
                       'proxy':{'host':path['host'],'ports':ports,'protocol':protocol,**({'transport':plugin(protocol)['relay']['transport']} if protocol not in ('vespera','dwarf','seestar') else {})} if protocol else None}
                      for path in paths]
        preferred=next((path for path in alternatives if path['role']=='home'),next(iter(alternatives),None))
        if protocol and preferred:proxy=preferred['proxy']
        return {'profile_id': profile['id'], 'target_ip': profile['target_ip'], 'subnet': row.get('subnet'),
                'bridge_instrument_ip': row.get('client_ip'), 'proxy': proxy, 'native_proxy_reason':row.get('native_proxy_reason'), 'management_endpoints':alternatives,
                'direct_route': {'routability': 'unknown', 'route_may_be_required': True},
                'manufacturer_identity': 'unknown', 'maker_app_acceptance': 'unverified',
                'service_readiness': 'checked' if plugin(profile['kind'])['readiness']=='vespera_tcp' else 'not_probed'}

    def ensure(self, ident, expected, revision, client, expires, *, internal=False, origin=None):
        c = self.c
        with c.lock:
            self.prune()
            if self.active:
                active = self.records[self.active]['public']
                if active['profile_id'] == ident and expected in ('"'+self.expected+'"', '"'+revision()+'"'):
                    return 202, self.public(self.active)
                refused('CONNECTION_OPERATION_PENDING', 'Another connection operation is pending.')
        if not c.net_lock.acquire(blocking=False): refused('NETWORK_BUSY', 'A network operation is already running.')
        try:
            with c.lock:
                profile = next((p for p in profiles(c.store.config) if p['id'] == ident), None)
                if profile is None: raise ApiError(404, 'TELESCOPE_NOT_FOUND', 'Unknown telescope profile.')
                device = next((d for d in c.state['devices'] if d['mac'] == profile['mac']), None)
                row = next((r for r in c.state['telescopes'] if r['id'] == ident), None)
                aliases = (profile.get('physical_binding') or {}).get('aliases', [])
                if aliases and profile['bssid'] and not any(a['ssid'] == profile['ssid'] and a['bssid'] == profile['bssid'] for a in aliases):
                    refused('BINDING_SELECTOR_CONFLICT', 'Saved selector differs from physical binding.')
                if c.last_cycle_ok and self.current(profile, row, device):
                    return 200, {'state': 'ready', 'profile_id': ident, 'endpoint': self.endpoint(profile, row, c.state['home_ip'])}
                c.protection.reject(ident)
                if c.pauses.held(ident): refused('CONNECTION_PAUSED', 'Resolve this paused connection explicitly before automatic allocation.')
                if expected is None or expected in ('*', '"*"'):
                    raise ApiError(428, 'PRECONDITION_REQUIRED', 'Provide the actual current configuration revision.')
                if expected != '"'+revision()+'"': raise ApiError(412, 'PRECONDITION_FAILED', 'Configuration changed. Reload it before ensuring.')
                if not c.store.config.get('auto_connect', False) or not profile.get('auto_eligible', False):
                    refused('AUTO_CONNECT_DISABLED', 'Enable global Auto-connect and this profile eligibility before automatic allocation.')
                if profile['enabled']: refused('MANUAL_CONNECTION_PENDING', 'This enabled profile has existing manual reconnect intent. Wait or disconnect it explicitly.')
                if any(c.net.connected(role_for(profile), d) for d in c.state['devices']):
                    refused('ROLE_CLEANUP_PENDING', 'An unwanted association for this profile still needs cleanup.')
                if self.negative.get(ident, 0) > self.clock(): refused('TARGET_BACKOFF', 'A recent failed attempt is cooling down; no automatic retry is queued.')
                if self.clock()-self.cooldowns.get(ident, -1e9) < auto_policy(c.store.config)['cooldown_seconds']: refused('REPLACEMENT_COOLDOWN', 'This instrument is cooling down after a replacement.')
                if (self.active or c.discovery_pending or c.certificate_job_pending or c.certificates.busy()
                        or c.action_at is not None or c.stop.is_set() or c.pending_deletions or c.cleanup_required):
                    refused('CONNECTION_OPERATION_PENDING', 'Management or connection work is pending.')
                if Updates(c.store.path.parent, c.demo).busy(): refused('UPDATE_IN_PROGRESS', 'Application maintenance is pending.')
                if not c.state.get('management',{}).get('paths'): refused('MANAGEMENT_UNAVAILABLE', 'A confirmed home or hotspot management path is required.')
                if not internal:
                    accounting = self.clients.setdefault(client, {'expires': expires, 'times': deque()})
                    while accounting['times'] and self.clock()-accounting['times'][0] >= 60: accounting['times'].popleft()
                    if len(accounting['times']) >= 5: raise ApiError(429, 'ENSURE_LIMITED', 'Too many connection operations. Try again later.')
                    accounting['times'].append(self.clock())
                    while len(self.clients) > 32: self.clients.pop(next(iter(self.clients)))
                operation = self.epoch+'.'+uuid.uuid4().hex
                self.records[operation] = {'observed': self.clock(), 'started': self.clock(), 'public': {
                    'id': operation, 'boot_id': self.epoch, 'profile_id': ident, 'state': 'running', 'phase': 'selecting',
                    'cause':({'seestar':'SEESTAR_DISCOVERY','vespera':'VESPERA_CONNECTION','dwarf':'DWARF_DISCOVERY','zwo_mount':'ZWO_MOUNT_CONNECTION','synscan':'SYNSCAN_DISCOVERY'}.get(origin,'SEESTAR_DISCOVERY' if origin is None else 'PLUGIN_CONNECTION')) if internal else 'AUTOMATIC',
                    'reason': 'SELECTING_FREE_RADIO', 'endpoint': None, 'physical_selection': None,
                    'restoration': {'state': 'not_needed', 'reason': None}, 'status_url': '/api/v1/operations/'+operation}}
                self.active = self.last = operation
                self.expected, self.revision = revision(), revision
                self.deadline = self.clock()+120
                self.attempted = False; self.failure = None; self.overlay = None; self.allocated = None
                self.victim = None; self.restoring = False; self.restore_attempted = False; self.restore_failed = False
                self.thread = threading.Thread(target=self.run, args=(operation,), daemon=True)
                try: self.thread.start()
                except Exception:
                    self.active = None; self.thread = None
                    self.records[operation]['public'].update(state='failed', phase='finished', reason='WORKER_UNAVAILABLE')
                    self.records[operation]['finished'] = self.clock()
                    raise ApiError(409, 'WORKER_UNAVAILABLE', 'Connection worker could not start.') from None
                return 202, self.public(operation)
        finally: c.net_lock.release()

    def demand_revision(self):
        raw = json.dumps(self.c.store.config, sort_keys=True, separators=(',', ':')).encode()
        return hmac.new(self.demand_key, raw, 'sha256').hexdigest()

    def demand_status(self, kind='seestar'):
        last = getattr(self,kind+'_last',self.demand_last)
        return {'reason':last['reason'], 'age_seconds':max(0, int(self.clock()-last['observed'])),
                'next_attempt_seconds':max(0, int(self.demand_next-self.clock())),
                'protocol':{'vespera':'vespera-tcp-syn','dwarf':'dwarf-udp-ping','zwo_mount':'zwo-mount-tcp-syn','synscan':'synscan-version-query'}.get(kind,'seestar-scan-iscope' if kind=='seestar' else plugin(kind)['discovery']['engine']), 'manufacturer_acceptance':'unverified'}

    def demand(self, management, source, *, kind='seestar', target=None):
        """A fixed read-only LAN discovery trigger; never an authenticated HTTP actor."""
        c = self.c
        if kind not in {p['id'] for p in demand_plugins()}: return 'UNSUPPORTED_PROTOCOL'
        with c.lock:
            try:
                address = ipaddress.ip_interface(management[1]); remote = ipaddress.ip_address(source)
                allowed = (management in c.management_plans
                           and remote in address.network and remote not in (address.ip, address.network.network_address, address.network.broadcast_address)
                           and not remote.is_multicast and not remote.is_unspecified)
            except (ValueError, TypeError, IndexError): allowed = False
            if not allowed: return 'UNTRUSTED_MANAGEMENT_SOURCE'
            policy = auto_policy(c.store.config)
            if not c.store.config.get('auto_connect') or not policy[kind+'_demand']: return 'DISABLED'
            # Retain a bounded passive intent until reconciliation has finished;
            # do not consume its quota or allocate from an in-progress snapshot.
            if not c.last_cycle_ok or c.net_lock.locked(): return 'RECONCILE_PENDING'
            if self.clock() < self.demand_next: return 'DEMAND_COOLDOWN'
            # Independent quota survives HTTP session pruning. No per-source identity/session is minted.
            self.demand_next = self.clock()+max(120, policy['cooldown_seconds'])
            ready = {r['id'] for r in c.state['telescopes'] if r.get('relay_ready') and r.get('connected')}
            saved = profiles(c.store.config)
            original=plugin(kind)['discovery'].get('target_mode')=='original'
            tcp=plugin(kind)['discovery']['engine']=='tcp_syn'
            matching = [p for p in saved if p['kind']==kind and (not original or p['target_ip']==target)
                        and (not tcp or target is None or p['target_ip']==target)]
            if any(p['id'] in ready for p in matching):
                reason = 'READY_CONNECTION_PRESERVED'
            else:
                choices = [p for p in matching if p.get('auto_eligible') and not c.pauses.held(p['id'])]
                reason = 'SELECTION_REQUIRED' if len(choices)>1 else {'vespera':'NO_ELIGIBLE_VESPERA','dwarf':'NO_ELIGIBLE_DWARF','zwo_mount':'NO_ELIGIBLE_ZWO_MOUNT','synscan':'NO_ELIGIBLE_SYNSCAN'}.get(kind,'NO_ELIGIBLE_SEESTAR' if kind=='seestar' else 'NO_ELIGIBLE_PLUGIN') if not choices else None
                ident = choices[0]['id'] if len(choices)==1 else None
            expected = '"'+self.demand_revision()+'"'
        if reason is None:
            try:
                self.ensure(ident, expected, self.demand_revision, None, 0, internal=True, origin=kind)
                reason = 'REQUEST_ACCEPTED'
            except ApiError as error: reason = error.code
            except (NetworkError, OSError, ValueError): reason = 'NETWORK_UNAVAILABLE'
        with c.lock:
            if kind!='seestar': setattr(self,kind+'_last',{'reason':reason, 'observed':self.clock()})
            else: self.demand_last = {'reason':reason, 'observed':self.clock()}
        return reason

    def intent(self, config):
        if self.victim and not self.restoring:
            for profile in config['telescopes']:
                if profile['id'] == self.victim['id']: profile['enabled'] = False
        if self.overlay:
            for profile in config['telescopes']:
                if profile['id'] == self.overlay['id']: profile.update(self.overlay)
        return config

    def before_association(self, profile):
        if self.restoring and profile['id'] == self.victim_id:
            if self.restore_attempted: self.restore_failed = True; return False
            self.restore_attempted = True
            return True
        if not self.overlay or self.overlay['id'] != profile['id']: return True
        if self.attempted: self.failure = self.failure or 'ASSOCIATION_LOST'; return False
        self.attempted = True
        self.records[self.active]['public'].update(phase='associating', reason='AWAITING_ASSOCIATION')
        return True

    def fail_association(self, profile):
        if self.restoring and profile['id'] == self.victim_id: self.restore_failed = True
        if self.overlay and self.overlay['id'] == profile['id']: self.failure = 'ASSOCIATION_FAILED'

    def verify_association(self, profile, device):
        if self.restoring and profile['id'] == self.victim_id:
            actual = self.c.net.link_observation(device, profile)
            if not actual or actual['bssid'] != self.victim['actual_bssid']:
                self.restore_failed = True; return False
        if not self.overlay or self.overlay['id'] != profile['id']: return True
        if self.clock() >= self.deadline: self.failure = 'TIMED_OUT'; return False
        actual = self.c.net.link_observation(device, profile)
        if not actual or actual['bssid'] != self.overlay['bssid']:
            self.failure = 'PHYSICAL_SELECTION_MISMATCH'; return False
        return True

    def gate_cycle(self):
        """Called after forwarding reconciles, before any ready event is emitted."""
        if self.restoring and self.clock() >= self.restore_deadline:
            self.restore_failed = True
            row = next((r for r in self.c.state['telescopes'] if r['id'] == self.victim_id), None)
            if row: row.update(relay_ready=False, state='error')
        if not self.overlay or not self.active: return
        c = self.c; value = self.records[self.active]['public']
        row = next((r for r in c.state['telescopes'] if r['id'] == self.overlay['id']), None)
        if not row: self.failure = 'RADIO_UNAVAILABLE'; return
        if self.clock() >= self.deadline: self.failure = 'TIMED_OUT'
        if row['state'] in ('missing', 'conflict', 'invalid_ip'):
            self.failure = {'missing':'RADIO_UNAVAILABLE','conflict':'OVERLAPPING_TARGET_NETWORK','invalid_ip':'INVALID_TARGET'}[row['state']]
        if not c.state.get('management',{}).get('paths'): self.failure = 'MANAGEMENT_UNAVAILABLE'
        if self.failure:
            row.update(relay_ready=False, state='error'); return
        profile = next(p for p in profiles(c.store.config) if p['id'] == self.overlay['id'])
        effective = dict(profile, **self.overlay)
        device = next((d for d in c.state['devices'] if d['mac'] == effective['mac']), None)
        if row.get('relay_ready'):
            if not self.current(effective, row, device): self.failure = 'PHYSICAL_SELECTION_MISMATCH'
            elif self.clock() >= self.deadline: self.failure = 'TIMED_OUT'
            elif self.revision() != self.expected: self.failure = 'SUPERSEDED'
            else:
                saved = copy.deepcopy(c.store.config)
                next(p for p in saved['telescopes'] if p['id'] == effective['id']).update(self.overlay)
                if self.victim:
                    next(p for p in saved['telescopes'] if p['id'] == self.victim_id)['enabled'] = False
                try: c.store.save(saved)
                except OSError: self.failure = 'PERSISTENCE_FAILED'
                else:
                    value.update(state='succeeded', phase='finished', reason='READY',
                                 endpoint=self.endpoint(effective, row, c.state['home_ip']),
                                 restoration={'state':'not_needed','reason':'VICTIM_INTENT_REPLACED' if self.victim else None})
                    return
            row.update(relay_ready=False, state='error')
        else: value.update(phase='checking_services' if row.get('connected') else 'associating', reason='SERVICE_UNAVAILABLE' if row.get('connected') else 'AWAITING_ASSOCIATION')

    def choose(self, profile):
        c = self.c; devices = c.net.devices()
        protected = {c.store.config['home'].get('mac'), c.state['hotspot'].get('mac'), reserved_hotspot_mac(c.store.config)}
        protected |= {p['mac'] for p in profiles(c.store.config) if p['enabled'] or c.protection.protected(p['id']) or c.pauses.held(p['id'])}
        target = ipaddress.IPv4Address(profile['target_ip'])
        management_networks=c.occupied_management_networks(devices)
        occupied=list(management_networks)
        paths=c.current_management(devices)
        if not paths:refused('MANAGEMENT_UNAVAILABLE','No advertised management path is currently confirmed.')
        home=paths[0]['device']
        occupied += [ipaddress.ip_interface(path['address']).network for path in paths]
        occupied += [ipaddress.ip_network(row['subnet']) for row in c.state['telescopes'] if row.get('connected') and row.get('subnet')]
        if any(target in subnet for subnet in management_networks):
            refused('OVERLAPPING_TARGET_NETWORK','Target overlaps management or emergency AP.')
        # A displaced victim's actual subnet may be removed; every other network stays protected.
        aliases = (profile.get('physical_binding') or {}).get('aliases', [])
        if aliases and profile['bssid'] and not any(a['ssid'] == profile['ssid'] and a['bssid'] == profile['bssid'] for a in aliases):
            refused('BINDING_SELECTOR_CONFLICT', 'Saved selector differs from physical binding. Correct it explicitly first.')
        free = [device for device in devices if device['mac'] not in protected and c.net.radio_idle(device)]
        eligible_free = [device for device in free if self.clock()-self.cooldowns.get(device['mac'], -1e9) >= auto_policy(c.store.config)['cooldown_seconds']]
        if free and not eligible_free: refused('REPLACEMENT_COOLDOWN', 'The free adapter is cooling down after replacement; no victim was taken.')
        free = eligible_free
        if not free:
            device = self.replace(profile, devices, home)
            # Only this actually displaced network may overlap the requested target.
            occupied=c.occupied_management_networks(devices)
            occupied += [ipaddress.ip_network(row['subnet']) for row in c.state['telescopes']
                         if row['id'] != self.victim_id and row.get('connected') and row.get('subnet')]
            free = [device]
        if any(target in network for network in occupied): refused('OVERLAPPING_TARGET_NETWORK', 'Target overlaps an active management or telescope network.')
        hints = [a.get('frequency') for a in aliases if not profile['bssid'] or a['bssid'] == profile['bssid']]
        free.sort(key=lambda d:-(('5' in d.get('bands', [])) if any(f and f >= 4900 for f in hints) else len(d.get('bands', []))))
        in_use = set()
        for other in profiles(c.store.config):
            if not other['enabled'] or other['id'] in (profile['id'], self.victim_id) or other['ssid'] != profile['ssid']: continue
            owner = next((d for d in devices if d['mac'] == other['mac']), None)
            actual = c.net.link_observation(owner, other) if owner else None
            if actual: in_use.add(actual['bssid'])
            elif other['bssid']: in_use.add(other['bssid'])
            else: refused('PHYSICAL_NETWORK_UNCERTAIN', 'Another enabled profile has an unverified selector for this SSID.')
        observed = False
        for device in free[:4]:
            if self.clock() >= self.deadline: refused('TIMED_OUT', 'Discovery deadline elapsed.')
            matches = {}
            for raw in c.net.scan(device['name'])[:256]:
                try: candidate = scan_candidate(raw, 0)
                except (ValueError, TypeError): continue
                if candidate['ssid'] != profile['ssid']: continue
                if profile['bssid'] and candidate['bssid'] != profile['bssid']: continue
                if aliases and not any(a['ssid'] == profile['ssid'] and a['bssid'] == candidate['bssid'] for a in aliases): continue
                observed = True
                band = '2.4' if candidate['frequency'] < 3000 else '5' if candidate['frequency'] < 5925 else '6'
                if band in device.get('bands', []): matches[candidate['bssid']] = candidate
            if len(matches) > 1: refused('AMBIGUOUS_NETWORK', 'Several physical access points match. Save an explicit BSSID selection first.')
            if len(matches) == 1:
                selected = next(iter(matches.values()))
                if selected['bssid'] in in_use: refused('PHYSICAL_NETWORK_IN_USE', 'This observed physical access point already belongs to an enabled connection.')
                return device, selected
        refused('INCOMPATIBLE_BAND' if observed else 'NETWORK_UNOBSERVED', 'No compatible observed access point was found; no association was attempted.')

    @staticmethod
    def quiet_eligible(measured, policy, elapsed=0):
        try:
            return (measured.get('state') == 'quiet' and 0 <= measured.get('sample_age_seconds', 16) + max(0, elapsed) <= 15
                    and measured.get('quiet_seconds', -1) >= policy['quiet_seconds']
                    and measured.get('cooldown_remaining', 1) == 0)
        except TypeError:
            return False

    def replace(self, target, devices, home):
        c = self.c; policy = auto_policy(c.store.config)
        if self.clock() - max(self.started, self.last_replacement) < policy['cooldown_seconds']:
            refused('REPLACEMENT_COOLDOWN', 'Replacement cooldown has not elapsed since service start.')
        if not c.last_cycle_ok: refused('RECONCILE_UNCERTAIN', 'A fresh successful network reconcile is required.')
        if hasattr(c.net, 'sample_activity'): c.net.sample_activity(force=True, policy=policy)
        if self.clock() >= self.deadline: refused('TIMED_OUT', 'Fresh measurement exceeded the operation deadline.')
        hints = [a.get('frequency') for a in (target.get('physical_binding') or {}).get('aliases', []) if not target['bssid'] or a['bssid'] == target['bssid']]
        required_bands = {'2.4' if f < 3000 else '5' if f < 5925 else '6' for f in hints if f}
        choices = []
        for index, profile in enumerate(profiles(c.store.config)):
            if self.clock() >= self.deadline: refused('TIMED_OUT', 'Selection exceeded the operation deadline.')
            if (not profile['enabled'] or not profile.get('auto_eligible') or not profile.get('auto_replaceable')
                    or c.pauses.held(profile['id']) or c.protection.protected(profile['id'])): continue
            device = next((d for d in devices if d['mac'] == profile['mac']), None)
            row = next((r for r in c.state['telescopes'] if r['id'] == profile['id']), None)
            if device and required_bands and not required_bands.intersection(device.get('bands', [])): continue
            if (not device or device['mac'] in (home['mac'], c.state['hotspot'].get('mac'), reserved_hotspot_mac(c.store.config))
                    or network_intent(c.applied.get(role_for(profile))) != network_intent(profile)
                    or not self.current(profile, row, device)): continue
            address = ipaddress.ip_interface(c.net.address(device))
            if str(address.network) != row.get('subnet') or ipaddress.ip_address(profile['target_ip']) not in address.network: continue
            actual = c.net.link_observation(device, profile)
            if not actual: continue
            token = c.net.association_identity(device) if hasattr(c.net, 'association_identity') else None
            identity = (profile['mac'], actual['bssid'], str(address), token)
            prior = self.force_observed.get(profile['id'])
            if not prior or prior[0] != identity: self.force_observed[profile['id']] = (identity, self.clock())
            measured = c.net.selection_activity(profile, device, home) if hasattr(c.net, 'selection_activity') else {}
            cooldown = max(self.cooldowns.get(profile['id'], -1e9), self.cooldowns.get(device['mac'], -1e9))
            if self.clock() - cooldown < policy['cooldown_seconds']: continue
            quiet = self.quiet_eligible(measured, policy)
            force = policy['force_first_profile'] and (measured.get('cooldown_remaining') == 0
                     or token and self.clock() - self.force_observed[profile['id']][1] >= policy['cooldown_seconds'])
            if quiet or force:
                choices.append((not quiet, -measured.get('last_activity_age_seconds', 0) if quiet else index,
                                profile, device, actual, str(address)))
        if not choices: refused('NO_REPLACEABLE_CONNECTION', 'No consenting, unprotected, freshly quiet compatible connection is available; unknown is not idle.')
        choices.sort(key=lambda item: item[:2])
        forced, _, profile, device, actual, address = choices[0]
        # Other candidate checks may have aged the initial snapshot. Re-measure
        # only the chosen quiet victim; never silently promote it to forced work.
        fresh = None; observed = self.clock(); token = None
        if not forced:
            token = c.net.association_identity(device) if hasattr(c.net, 'association_identity') else None
            if hasattr(c.net, 'sample_activity'): c.net.sample_activity(force=True, policy=policy)
            fresh = c.net.selection_activity(profile, device, home) if hasattr(c.net, 'selection_activity') else {}
            observed = self.clock()
        # Recheck after measurement, while network lock and then controller lock exclude admissions.
        with c.lock:
            c.protection.reject(profile['id'])
            if self.revision() != self.expected or not self.current(profile, next(r for r in c.state['telescopes'] if r['id']==profile['id']), device):
                refused('SUPERSEDED', 'Victim changed during fresh measurement.')
            if c.net.address(device) != address or c.net.link_observation(device, profile) != actual:
                refused('ASSOCIATION_CHANGED', 'Victim association changed during selection.')
            if not forced:
                same_association = not hasattr(c.net, 'association_identity') or c.net.association_identity(device) == token
                if not same_association or not self.quiet_eligible(fresh, auto_policy(c.store.config), self.clock()-observed):
                    refused('QUIET_ELIGIBILITY_CHANGED', 'Selected connection is no longer freshly quiet; nothing was disconnected.')
            if self.clock() >= self.deadline: refused('TIMED_OUT', 'Selection exceeded the operation deadline; no victim was disconnected.')
            self.victim = {'id':profile['id'], 'intent': c.pauses.fingerprint(profile), 'actual_bssid':actual['bssid']}
            self.allocated = device['mac']
            self.records[self.active]['public'].update(phase='disconnecting_victim', reason='FORCED_INTERRUPTION' if forced else 'QUIET_REPLACEMENT',
                displaced={'profile_id': profile['id'], 'reason':'FORCED_INTERRUPTION' if forced else 'QUIET_REPLACEMENT'})
            effective = self.intent(copy.deepcopy(c.store.config))
            c.connection_events.request(c.store.config, effective, self.cause, operation_id=self.active)
            self.cooldowns[profile['id']] = self.cooldowns[device['mac']] = self.clock()
        c.cycle()  # Preserves unrelated validated relays; removes only the displaced route.
        if c.net.connected(role_for(profile), device) or not c.net.radio_idle(device):
            refused('VICTIM_DISCONNECT_FAILED', 'Displaced connection did not release its radio.')
        return device

    def restore(self, value):
        try: return self._restore(value)
        except Exception:
            # Enumeration/identity checks can fail independently of reconcile.
            # Never release the pending claim with a silently retryable victim.
            with self.c.lock:
                victim = next((p for p in profiles(self.c.store.config) if p['id'] == self.victim_id), None)
                if victim: self.c.pauses.hold(victim)
                self.restoring = False
                for row in self.c.state['telescopes']:
                    if row['id'] == self.victim_id: row.update(relay_ready=False, state='paused')
            return 'failed', self.c.pauses.error or 'RESTORATION_FAILED_PAUSED'

    def _restore(self, value):
        c = self.c
        self.overlay = None
        value.update(phase='restoring', restoration={'state':'in_progress','reason':None})
        victim = next((p for p in profiles(c.store.config) if p['id'] == self.victim_id), None)
        if victim is None or c.pauses.fingerprint(victim) != self.victim['intent']:
            c.cycle()
            return 'superseded', 'CURRENT_INTENT_PRESERVED'
        self.restoring = True; self.restore_attempted = False; self.restore_failed = False
        c.attempts.pop(role_for(victim), None)
        context = c.connection_events.contexts.setdefault(victim['id'], {})
        context.update(operation_id=self.active, cause=self.cause)
        state = c.connection_events.states.setdefault(victim['id'], {})
        state.update(recovering=True, intentional=False)
        deadline = self.restore_deadline = self.clock() + 90
        while not c.stop.is_set() and self.clock() < deadline:
            try: c.cycle()
            except (NetworkError, OSError): self.restore_failed = True
            row = next((r for r in c.state['telescopes'] if r['id'] == victim['id']), None)
            device = next((d for d in c.net.devices() if d['mac'] == victim['mac']), None)
            if c.last_cycle_ok and not self.restore_failed and self.current(victim, row, device):
                return 'restored', 'PREVIOUS_CONNECTION_READY'
            if self.restore_failed or not device or row and row.get('state') in ('conflict','invalid_ip','missing'): break
            c.stop.wait(.25)
        # Durable hold is published before terminal failure and before pending releases.
        with c.lock: c.pauses.hold(victim)
        self.restoring = False
        try: c.cycle()
        except Exception: pass  # Hold remains authoritative; readiness is never inferred from cleanup.
        return 'failed', c.pauses.error or 'RESTORATION_FAILED_PAUSED'

    def run(self, operation):
        c = self.c; acquired = False
        try:
            # Admission already checked the request under the network lock, but
            # a normal reconcile can win the handoff to this worker. On the Zero
            # W it may exceed five seconds. Wait within the original operation
            # deadline, with stop checks; do not treat contention as target failure.
            while not acquired:
                remaining = self.deadline-self.clock()
                if c.stop.is_set() or remaining <= 0:
                    refused('TIMED_OUT', 'Operation ended before network work became available.')
                acquired = c.net_lock.acquire(timeout=min(.25, remaining))
            with c.lock:
                if c.stop.is_set() or self.clock() >= self.deadline:
                    refused('TIMED_OUT', 'Operation ended during network handoff.')
                profile = copy.deepcopy(next(p for p in profiles(c.store.config) if p['id'] == self.records[operation]['public']['profile_id']))
                if self.revision() != self.expected: refused('SUPERSEDED', 'Configuration changed before selection.')
            device, selected = self.choose(profile)
            if c.stop.is_set() or self.clock() >= self.deadline: refused('TIMED_OUT', 'Operation deadline elapsed.')
            if not c.net.radio_idle(device): refused('NO_FREE_RADIO', 'Selected radio is no longer confidently free.')
            with c.lock:
                self.allocated = device['mac']
                self.overlay = {'id': profile['id'], 'mac': device['mac'], 'bssid': selected['bssid'], 'enabled': True}
                effective = self.intent(copy.deepcopy(c.store.config))
                c.connection_events.request(c.store.config, effective, self.cause, selected=profile['id'], operation_id=operation)
                self.records[operation]['public']['physical_selection'] = {'bssid':selected['bssid'], 'frequency':selected['frequency'], 'verification':'wifi-observed-not-manufacturer-verified'}
            while not c.stop.is_set() and self.clock() < self.deadline:
                c.cycle()
                if self.records[operation]['public']['state'] == 'succeeded': return
                if self.failure: refused(self.failure, 'Connection did not reach readiness.')
                c.stop.wait(.25)
            refused('TIMED_OUT', 'Connection did not become ready before its deadline.')
        except Exception as error:
            if self.records[operation]['public']['state'] == 'succeeded': return
            reason = error.code if isinstance(error, ApiError) else 'NETWORK_UNAVAILABLE' if isinstance(error, NetworkError) else 'RECONCILE_FAILED'
            with c.lock:
                value = self.records[operation]['public']; value.update(phase='cleaning_up', reason=reason)
                affected = self.overlay is not None
                self.overlay = None
            restored = not affected
            if acquired and affected:
                try:
                    c.cycle()
                    old = next(p for p in profiles(c.store.config) if p['id'] == value['profile_id'])
                    restored = not any(c.net.connected(role_for(old), d) for d in c.net.devices())
                except Exception: restored = False
            with c.lock:
                if affected and not restored and self.allocated:
                    c.cleanup_required['telescope' if value['profile_id'] == 'primary' else 'telescope-'+value['profile_id']] = self.allocated
                if not self.victim:
                    value.update(state='failed', phase='finished', endpoint=None,
                                 restoration={'state':'restored' if restored else 'failed', 'reason':'ORIGINAL_INTENT_PRESERVED' if restored else 'CLEANUP_UNCONFIRMED'})
            if acquired and self.victim:
                state, restored_reason = self.restore(value)
                with c.lock: value.update(state='failed', phase='finished', restoration={'state':state,'reason':restored_reason})
            with c.lock:
                self.negative[value['profile_id']] = self.clock()+max(120, auto_policy(c.store.config)['cooldown_seconds'])
                self.negative = {key:until for key,until in self.negative.items() if until > self.clock()}
        finally:
            with c.lock:
                if self.victim:
                    self.last_replacement = self.clock()
                    for key in (self.victim_id, self.records[operation]['public']['profile_id'], self.allocated):
                        if key: self.cooldowns[key] = self.clock()
                self.overlay = None; self.active = None; self.allocated = None; self.victim = None; self.restoring = False
                self.records[operation]['finished'] = self.clock()
                self.records[operation]['observed'] = self.clock(); self.prune()
            if acquired: c.net_lock.release()
