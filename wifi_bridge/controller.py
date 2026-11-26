"""Reconcile independent home, telescope and optional access-point radios."""
import copy
import hashlib
import json
import hmac
import re
import ipaddress
import threading
import time
import traceback
import uuid
from collections import deque
from pathlib import Path

from .config import device_identity, Invalid, profiles, public_config, role_for, validate_config, password_hash, password_matches, text, network_intent, auto_policy, hotspot_ssid, reserved_hotspot_mac
from .network import NetworkError, NetworkAuthenticationError, NetworkIsolationError, hotspot_route_targets
from .plugins import relay_plugins, proxy_flag, plugin, require_config_plugins
from .events import EventStore, ConnectionEvents
from .tls import CertificateState
from .api import ApiError
from .readiness import ServiceReadiness, empty_service
from .mount_sleep import WIFI_SLEEP
from .radio_roles import role_allows, interface_roles

# A radio whose hotspot failed this many times in a row is tried after the
# other free radios, without changing the owner's saved adapter choice.
HOTSPOT_FAILURE_LIMIT = 2
# A recorded hotspot that stays unconfirmed this long is stopped and restarted.
HOTSPOT_UNCONFIRMED_GRACE = 20
# A hotspot confirmed for this long clears its radio's failure count.
HOTSPOT_STABLE = 120
# An instrument that does not come back is retried after 15 s, then each wait
# doubles up to five minutes. A long retry keeps the radio and the network lock
# busy, so an absent instrument must not be retried at full pace forever.
RETRY_FIRST = 15
RETRY_CAP = 300


def retry_interval(failures):
    """Seconds to wait before the next association attempt after this many failures in a row."""
    return min(RETRY_CAP, RETRY_FIRST * 2 ** max(0, min(failures, 16) - 1))


def hotspot_log(message):
    """Plain service-log line; never includes network names or passwords."""
    print('Astro Bridge hotspot: ' + message, flush=True)


def home_wifi_log(message):
    """Plain service-log line about the home Wi-Fi retry, never with a network name or password."""
    print('Astro Bridge home Wi-Fi: ' + message, flush=True)


HOME_RETRY_MESSAGES = {
    'HOME_RETRY_ATTEMPT': 'trying the home Wi-Fi on {name}.',
    'HOME_RETRY_FAILED': 'the home Wi-Fi retry on {name} failed. It is tried again every 45 seconds.',
    'HOME_RETRY_FAILED_AP_RESTORED': 'the home Wi-Fi retry on {name} failed. The recovery hotspot is back.',
    'HOME_RETRY_FAILED_AP_RESTORE_FAILED': 'the home Wi-Fi retry on {name} failed and the recovery hotspot could not start again.',
    'HOME_RETRY_DEFERRED_AP_RESERVED': 'the home Wi-Fi retry on {name} is deferred: its radio carries the recovery hotspot, which is in use.',
    'HOME_RETRY_DEFERRED_RADIO_BUSY': 'the home Wi-Fi retry on {name} is deferred: the radio is busy ({radio}).',
    'HOME_RADIO_MISSING': 'the home Wi-Fi adapter is missing.',
    'HOME_READY': 'the home Wi-Fi is connected on {name}.',
}


class Controller:
    def __init__(self, store, network, bootstrap, demo=False):
        require_config_plugins(store.config)
        self.store, self.net, self.bootstrap, self.demo = store, network, bootstrap, demo
        from .config import migrate_hotspot_password
        # Earlier versions kept the hotspot password in clear: keep only its key.
        migrated = migrate_hotspot_password(store.config, getattr(store, 'mac', ''), bootstrap['password'])
        if migrated is not None:
            store.save(migrated)
        from .cameras.discovery import platform_supported
        self.camera_supported = platform_supported(demo)
        self.device_id = device_identity(store.path.parent)
        from .identity import Identities
        self.identities = Identities(store.path.parent)
        self.lock = threading.RLock()
        self.net_lock = threading.Lock()
        self.stop = threading.Event()
        self.state = {"mode": "starting", "message": "Starting the bridge…", "devices": [],
                      "home_ip": "", "telescope_connected": False, "relay_ready": False,
                      "setup_ssid": "", "demo": demo, "last_error": "", "telescopes": [],
                      "hotspot": {"active": False, "reason": "starting"},
                      "management": {"mode":"none","paths":[],"reason":"STARTING","home_retry":{"reason":"STARTING","remaining_seconds":0}}}
        self.action_at = None
        # The network backend asks this before each fallback address it would try.
        if hasattr(network, 'halting'): network.halting = self.halting
        self.configured_this_boot = False
        self.last_home_attempt = -1000
        self.next_home_attempt = 0
        self.attempts = {}
        # Failed association attempts in a row per role, since its last association.
        self.retries = {}
        # Last failed step of joining an instrument's Wi-Fi, by role (see network.link_failure_stage).
        self.link_stages = {}
        # Connected instruments whose app released its reservation: the link stays up
        # for a quick return, but the next explicit request may take the radio.
        self.standby = set()
        # Sleep-prone Wi-Fi mounts: cycle time each one was last joined and answering.
        self.mount_contact = {}
        self.cycle_now = None
        from .released import ReleaseNotes
        self.release_notes = ReleaseNotes(store.path.parent)
        from .auth_failures import AuthenticationFailures
        self.auth_failures = AuthenticationFailures(store.path.parent)
        self.applied = {}
        self.managed_roles = {"telescope"}
        self.country = None
        self.pending_deletions = set()
        self.blocked = {}
        self.topology = None
        self.password_attempts = deque()
        self.default_hash_cache = (None, False)
        self.ap_password = None
        # Recent hotspot failures per radio MAC, the recorded AP owner that is not
        # confirmed working (interface, since) and the confirmed AP (mac, since).
        # Demo mode simulates its radios: it never writes hotspot log lines.
        self.log = (lambda message: None) if demo else hotspot_log
        self.home_log = (lambda message: None) if demo else home_wifi_log
        # Last home Wi-Fi retry reason written to the service log: one line per change, never per cycle.
        self.home_retry_logged = None
        self.ap_failures = {}
        self.ap_unconfirmed = None
        self.ap_confirmed = None
        self.routing_conflicts = set()
        self.proxy_owners = {proxy_flag(p['id']):None for p in relay_plugins()}
        self.discovery_plan = None
        self.management_plan = None
        self.management_plans = ()
        self.discovery_plans = ()
        self.cleanup_required = {}
        from .paused import PausedRoles
        self.pauses = PausedRoles(store.path.parent)
        self.certificates = CertificateState(store.path.parent)
        self.discovery_pending = False
        self.certificate_job_pending = False
        from .candidates import CandidateDiscovery
        self.candidates = CandidateDiscovery(self)

        self.event_store = EventStore(store.path.parent / "events.json")
        # Only a platform that owns its host clock may set it from a signed-in browser.
        from .clock import set_system_clock
        from .runtime import platform
        from .cameras.discovery import pi5_appliance
        if not demo and (pi5_appliance() or platform() in ('raspberry', 'live-usb')):
            self.event_store.clock.set_system = set_system_clock
        from .telemetry import TelemetryStore, Sessions
        self.telemetry_store = TelemetryStore(store.path.parent / 'telemetry.sqlite3',
                                              clock=self.event_store.clock, boot_id=self.event_store.boot_id)
        from .telemetry.settings import prune_settings
        prune_settings(self.telemetry_store, profiles(store.config))
        self.telemetry_sessions = Sessions(self.telemetry_store, events=self.event_store)
        self.event_store.track_instruments(p['id'] for p in profiles(store.config))
        self.connection_events = ConnectionEvents(self.event_store)
        self._event_config = None
        self._event_contexts = {}
        self.last_cycle_ok = False
        from .operations import ConnectionOperations
        self.operations = ConnectionOperations(self)
        from .plugin_runtime import adapters
        self.plugin_adapters=adapters(self)
        # Stable aliases for existing internal/native integrations.
        self.vespera_demand=self.plugin_adapters['vespera']
        self.dwarf_demand=self.plugin_adapters['dwarf']
        self.zwo_mount_demand=self.plugin_adapters['zwo_mount']
        self.synscan_demand=self.plugin_adapters['synscan']
        from .protection import ConnectionProtection
        self.protection = ConnectionProtection(self)
        self.protection.lease_hook = self._lease_event
        from .cameras.native import NativeCameras
        self.camera_native = NativeCameras(self)
        self.connection_events.leased = lambda ident: any(value['profile_id'] == ident and value['expires'] > self.protection.clock() for value in self.protection.leases.values())
        self.service_readiness = ServiceReadiness(network, demo)
        from .telemetry.link import LinkCollector
        self.link_collector = LinkCollector(self)
        from .telemetry.adapters.vespera_status import VesperaCollector
        self.vespera_collector = VesperaCollector(self)
        from .telemetry.adapters.alpaca_observer import AlpacaCollector
        self.alpaca_collector = AlpacaCollector(self)
        from .maker_keys.service import MakerKeys
        self.maker_keys = MakerKeys(self)
        from .telemetry.adapters.seestar_events import SeestarCollector
        self.seestar_collector = SeestarCollector(self)
        from .telemetry.adapters.dwarf_capture import DwarfCapture
        self.dwarf_capture = DwarfCapture(self)
        self.operation_since = {}
        from .stations import Stations
        self.stations = Stations(self)
        from .serial_mounts import SerialMounts
        self.serial_mounts = SerialMounts(self)
        if demo:
            from .telemetry.demo import DemoTelemetry
            self.demo_telemetry = DemoTelemetry(self)

    def operation_state(self, profile):
        """Operation reporters may strengthen protection, never infer quiet."""
        states = [collector.operation_state(profile) for collector in
                  (self.vespera_collector, self.alpaca_collector, self.seestar_collector, getattr(self, 'dwarf_capture', None))
                  if hasattr(collector, 'operation_state')]
        if 'running' in states: return 'running'
        if 'unknown' in states: return 'unknown'
        return next((state for state in states if state is not None), None)

    def operation_detail(self, profile):
        """Contract 1.2 projection {state, kind, since} of the same evidence, or None.

        since is the bridge time the current state and kind were first observed.
        """
        state = self.operation_state(profile)
        if state is None:
            self.operation_since.pop(profile['id'], None); return None
        kind = None
        if state == 'running':
            kind = next((k for k in (getattr(collector, 'operation_kind', lambda _: None)(profile) for collector in
                         (self.vespera_collector, self.seestar_collector, getattr(self, 'dwarf_capture', None)) if collector)
                         if k is not None), None)
        previous = self.operation_since.get(profile['id'])
        if previous is None or previous[:2] != (state, kind):
            from .clock import utc
            previous = self.operation_since[profile['id']] = (state, kind, utc(time.time()))
        return {'state': state, 'kind': kind, 'since': previous[2]}

    def _lease_event(self, kind, ident, lease_id, expires_in, reason=None):
        fields = {'expires_in': expires_in} if kind == 'lease.expiring' else {'reason': reason or 'EXPIRED'}
        self.event_store.native(kind, ident, lease_id=lease_id, **fields)

    def instrument_catalog(self, snapshot, client=None, camera_rows=None):
        from .instruments import catalog
        cameras = self.camera_native.catalog(client, camera_rows)
        self.event_store.track_instruments([p['id'] for p in profiles(self.store.config)] + [p['id'] for p in self.store.config.get('stations', [])]
                                           + [row['id'] for row in cameras])
        discovery = self.candidates
        age = discovery.clock() - discovery.nearby_at if discovery.nearby_at is not None else None
        from .telemetry.settings import selected_adapters
        collector = getattr(self, 'link_collector', None)
        available = bool(collector and not collector.halt.is_set() and not self.telemetry_sessions.closed)
        vespera = getattr(self, 'vespera_collector', None)
        alpaca = getattr(self, 'alpaca_collector', None)
        seestar = getattr(self, 'seestar_collector', None)
        telemetry = {p['id']:[a for a in selected_adapters(self.telemetry_store.settings, p)
                              if (available and a == 'link_v1') or (a == 'vespera_status_v1' and vespera and vespera.available(p['id']))
                              or (a == 'alpaca_observer_v1' and alpaca and alpaca.available(p['id']))
                              or (a == 'seestar_events_v1' and seestar and seestar.available(p['id']))]
                     for p in profiles(self.store.config)}
        from .instrument_pages import ordered_catalog
        self.identities.prune({p['id'] for p in profiles(self.store.config)})
        return ordered_catalog(self._radio_catalog(snapshot, client, telemetry, discovery, age)
                               + self.stations.catalog(client, self.event_store.events, self.event_store.instrument_sequences), cameras)

    def _radio_catalog(self, snapshot, client, telemetry, discovery, age):
        from .instruments import catalog
        return (catalog(snapshot, self.protection.leases.values(), client, now=self.protection.clock(), telemetry=telemetry, maker_keys=self.maker_keys.inventory(), identities=self.identities, free_radios=self.free_radios(),
                       released=self.release_notes.status(),
                       silences=self.mount_silences(),
                       operations={p['id']: self.operation_detail(p) for p in profiles(self.store.config)},
                       observations=discovery.nearby_rows, observation_age=age, events=self.event_store.events, sequences=self.event_store.instrument_sequences))

    def mount_silences(self):
        """Sleep-prone Wi-Fi mounts the bridge no longer hears, with the probable cause."""
        from .mount_sleep import silence
        result = {}
        discovery = self.candidates
        wall = discovery.wall()
        for p in profiles(self.store.config):
            if p['kind'] not in WIFI_SLEEP: continue
            contact = self.mount_contact.get(p['id'])
            link_age = self.cycle_now - contact if contact is not None and self.cycle_now is not None else None
            heard = [entry['observed_at'] for entry in discovery.observations.values()
                     if entry.get('ssid') == p['ssid'] and (not p.get('bssid') or entry.get('bssid') == p['bssid'])]
            scan_age = max(0, wall - max(heard)) if heard else None
            value = silence(p['kind'], link_age, scan_age)
            if value: result[p['id']] = value
        return result

    def _native_event_snapshot(self):
        from .instruments import endpoint
        snapshot = self.status()
        camera_rows = self.camera_native.inventory()
        by_id = {row['instrument_id']: row for row in camera_rows}
        for item in self.instrument_catalog(snapshot, camera_rows=camera_rows):
            ident = item['id']
            self.connection_events.native_state(ident, item['link'], item['service'])
            if item['link'] != 'connected': continue
            state = self.connection_events.states.setdefault(ident, {})
            # Compare both ingress projections without retaining endpoint addresses.
            primary = []
            for path in ('home', 'hotspot', 'other'):
                value = (self.camera_native.endpoint(ident, path, row=by_id[ident]) if item['kind'] == 'camera'
                         else endpoint(snapshot, ident, path))
                primary.append(None if value is None else {key: value[key] for key in ('primary', 'mode', 'requires_client_route')})
            signature = hashlib.sha256(json.dumps(primary, sort_keys=True).encode()).hexdigest()
            if 'endpoint_signature' in state and signature != state['endpoint_signature']:
                self.event_store.native('instrument.endpoint_changed', ident)
            state['endpoint_signature'] = signature

    def _switch_events(self):
        """Emit one progress event per phase change and one completion per operation."""
        ops = self.operations
        seen = self.__dict__.setdefault('_switch_seen', {})
        for ident in dict.fromkeys(filter(None, (ops.active, ops.last))):
            record = ops.records.get(ident)
            if record is None: continue
            value, current = record['public'], ops.progress(ident)
            previous = seen.get(ident)
            if current['phase'] in ('ready', 'failed'):
                if previous != 'completed':
                    reason = value.get('reason') if isinstance(value.get('reason'), str) and re.fullmatch('[A-Z][A-Z0-9_]{0,63}', value['reason']) else 'UNKNOWN'
                    self.event_store.native('instrument.switch_completed', value['profile_id'], operation=ident,
                                            outcome=current['phase'], reason=reason)
                    seen[ident] = 'completed'
            elif previous != current['phase']:
                self.event_store.native('instrument.switch_progress', value['profile_id'], operation=ident,
                                        phase=current['phase'], eta_seconds=current['eta_seconds'])
                seen[ident] = current['phase']
        while len(seen) > 16: seen.pop(next(iter(seen)))

    def free_radios(self):
        """Instrument-capable radios with no management role and no active profile."""
        from .radio_roles import role_allows
        with self.lock:
            config = self.store.config
            used = {config.get('home', {}).get('mac'), self.state['hotspot'].get('mac'), reserved_hotspot_mac(config)}
            used |= {p['mac'] for p in profiles(config) if p['enabled']}
            used |= {row.get('mac') for row in self.state['telescopes'] if row.get('connected')}
            return min(16, sum(1 for d in self.state['devices'] if d.get('mac') not in used
                               and d.get('kind', 'wifi') != 'ethernet' and role_allows(config, d['mac'], 'telescope')))

    def announcement(self):
        """Shared public identity for DNS-SD and HTTP; no manufacturer probing."""
        from .announce import display_name
        with self.lock:
            # Add future capabilities only when their routes/runtime become available.
            runtime = {'pairing': hasattr(self, 'paired_clients'), 'instruments': True, 'connect': True, 'events-wait': True,
                       'telemetry': not self.telemetry_sessions.closed, 'maker-keys': not self.maker_keys.closed}
            cameras = getattr(self, 'camera_service', None)
            runtime['alpaca-camera'] = bool(cameras and cameras.listening_port() is not None)
            secure = getattr(self, 'secure_access', None)
            if secure:
                status = secure.status()
                if status['listener']['ready']:
                    runtime['https_port'] = 443
                    fingerprint = (status.get('identity') or {}).get('fingerprint_sha256')
                    if isinstance(fingerprint, str) and re.fullmatch('[0-9a-f]{64}', fingerprint):
                        runtime['tls_fingerprint'] = fingerprint
            # Contract 1.1 hints: radios a client could use without replacing anything.
            runtime['free_radios'] = self.free_radios()
            runtime['hotspot'] = bool(self.state.get('hotspot', {}).get('active'))
            boot = re.sub('[^0-9a-f]', '', str(self.event_store.boot_id))[:8]
            if len(boot) == 8: runtime['boot'] = boot
            return {'name': display_name(self.store.config['hostname']),
                    'device_id': self.device_id}, runtime

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def status(self):
        with self.lock:
            result = copy.deepcopy(self.state)
            result["config"] = public_config(self.store.config)
            manager = getattr(self.net, 'isolated', None)
            if manager:
                for row in result['telescopes']:
                    count = manager.counters.get(row['id'])
                    if (row.get('client_endpoint') or {}).get('transport') == 'udp':
                        ownership = manager.owner_status(row['id'])
                        row['client_endpoint'].update(ownership)
                        for path in row['client_endpoint'].get('alternatives', []): path.update(ownership)
                    if row.get('client_endpoint') and count and not self.demo:
                        row['transport_metrics'] = count.snapshot(**manager._policy_args())
            result["nearby_telescopes"] = self.candidates.nearby()
            saved_profiles = {p['id']:p for p in profiles(self.store.config)}
            for row in result['telescopes']:
                saved = saved_profiles.get(row['id'])
                if row.get('client_endpoint') and (not saved or not saved['enabled']
                        or network_intent(saved) != network_intent(self.applied.get(role_for(saved)))):
                    row['client_endpoint'].update(ready=False, reason='CONFIGURATION_PENDING')
                    if saved and saved.get('client_port'): row['client_endpoint']['port'] = saved['client_port']
                    from .isolated import SERVICES, client_ports
                    ports = dict(zip((p for p, _ in SERVICES.get(saved['kind'], ())), client_ports(saved))) if saved and saved.get('client_port') else {}
                    for path in row['client_endpoint'].get('alternatives', []) + row['client_endpoint'].get('services', []):
                        path.update(ready=False, reason='CONFIGURATION_PENDING')
                        if saved and saved.get('client_port'): path['port'] = ports.get(path.get('target_port'), saved['client_port'])
            result['connection_operation'] = self.operations.latest()
            result['stations'] = self.stations.public()
            result['serial_mounts'] = self.serial_mounts.public()
            result['serial_detected'] = self.serial_mounts.detected()
            from .devices import declared_devices
            result['declared_devices'] = declared_devices(result)
            result['maker_demand'] = self.operations.demand_status()
            result['maker_demands'] = {'seestar':dict(result['maker_demand']),**{kind:adapter.status() for kind,adapter in self.plugin_adapters.items()}}
            result['connection_protection'] = self.protection.status()
            result['connection_pauses'] = self.pauses.status(self.store.config)
            result['radio_releases'] = self.release_notes.status()
            result['connection_activity'] = self.net.activity_status() if hasattr(self.net, 'activity_status') else {'supported':False,'profiles':{},'reason':'SIMULATION_NOT_MEASURED','idle_selection_implemented':True}
        from .runtime import capabilities
        result["deployment"] = capabilities(self.demo)
        encoded = self.store.config["admin_hash"]
        if self.default_hash_cache[0] != encoded:
            self.default_hash_cache = (encoded, password_matches("12345678", encoded) if encoded else self.bootstrap["password"] == "12345678")
        from .password import default_in_use
        from .linux_account import ssh_status
        account = result["deployment"]["linux_account"]
        default = default_in_use(self.store.config, self.bootstrap["password"], account, self.default_hash_cache[1])
        # One password: the three historical flags report the same single answer.
        result["default_credentials"] = {"password": default, "web": default, "hotspot": default, "linux": default}
        result["ssh"] = (dict(account=account, password_login=not default) if self.demo and account else ssh_status(account))
        access = result.setdefault('network_access', {'available':False,'reason':'NOT_RECONCILED'})
        mode = result['config']['connection_mode']
        if access.get('mode',mode) != mode or access.get('private_lan_allowed',False) != result['config']['hotspot_private_lan']:
            access.update(available=False,reason='CONFIGURATION_PENDING')
        access.update(mode=mode,configured=mode=='hotspot_shared',private_lan_allowed=result['config']['hotspot_private_lan'],internet_verified=False)
        result["url"] = "http://" + result["config"]["hostname"] + ".local"
        return result

    def serial_events(self, rows):
        """Appearance and removal of serial adapters reach native clients as events."""
        import hashlib
        seen = {'ser-' + hashlib.sha256(r['node'].encode()).hexdigest()[:12]: r for r in rows}
        previous = getattr(self, '_serial_seen', None)
        self._serial_seen = set(seen)
        if previous is None: return
        for key in sorted(set(seen) - previous): self.event_store.native('serial.detected', key)
        for key in sorted(previous - set(seen)): self.event_store.native('serial.removed', key)

    def update_safe(self):
        """App-owned maintenance gate; future reservations/locks extend this hook."""
        with self.lock:
            return (not self.protection.protected() and not self.pauses.error and not self.pauses.records and not self.cleanup_required and not self.operations.pending and not self.certificate_job_pending and not self.certificates.busy() and not self.stop.is_set() and self.action_at is None
                    and not self.pending_deletions and not self.net_lock.locked()
                    and not any(p['enabled'] for p in self.store.config['telescopes'])
                    and not any(p.get('connected') for p in self.state['telescopes']))

    def save(self, payload, cause="CONFIGURATION", expected=None, revision=None):
        try:
            with self.certificates.config_guard():
                return self._save_locked(payload, cause, expected, revision)
        except (OSError, ValueError) as error:
            if isinstance(error, Invalid): raise
            raise Invalid("Configuration or certificate maintenance is already active or unavailable.") from None

    def guard_management(self, config):
        ap_mac=self.state.get('hotspot',{}).get('mac') if self.state.get('hotspot',{}).get('active') else None
        reserved = {ap_mac, reserved_hotspot_mac(config)} - {None, ''}
        if any(p['enabled'] and p['mac'] in reserved for p in profiles(config)):
            raise ApiError(409,'MANAGEMENT_RADIO_RESERVED','Change the adapter role in Wi-Fi adapters before assigning it to an instrument.')

    def _save_locked(self, payload, cause, expected=None, revision=None):
        from .runtime import capabilities
        legacy_reboot = "telescopes" not in payload and capabilities(self.demo)['host_reboot']
        with self.lock:
            if self.operations.pending:
                raise ApiError(409, 'CONNECTION_OPERATION_PENDING', 'A connection operation is pending.')
            if self.action_at is not None:
                raise Invalid("A reboot is already scheduled.")
            payload = dict(payload)
            # Only used to derive a new hotspot key when a personal password renames the hotspot.
            rename_password = payload.pop("current_password", None)
            if not self.store.config["admin_hash"] and not payload.get("admin_password"):
                payload = dict(payload, admin_password=self.bootstrap["password"])
            previous = self.store.config
            value = validate_config(payload, previous, self.state["devices"])
            # Storing the effective bootstrap password as a salted hash is not a
            # password rotation. Compare credentials, not randomized hash bytes.
            admin = payload.get('admin_password', '')
            password_changed = bool(admin) and not (password_matches(admin, previous['admin_hash'])
                if previous['admin_hash'] else hmac.compare_digest(admin.encode(), self.bootstrap['password'].encode()))
            from .password import check, PasswordRuleError, valid_key, hotspot_key
            if password_changed:
                # A new administrator password is the single bridge password.
                try:
                    check(admin)
                except PasswordRuleError as error:
                    raise Invalid(str(error)) from None
            elif valid_key(previous.get('hotspot_key')) and value['hotspot'].get('ssid') \
                    and value['hotspot']['ssid'] != previous['hotspot_key']['ssid']:
                # The stored key belongs to one network name: a new name needs the password again.
                if not isinstance(rename_password, str) or not self._password_valid(rename_password):
                    raise ApiError(409, 'PASSWORD_REQUIRED', 'Enter the bridge password to rename the hotspot.')
                value['hotspot_key'] = hotspot_key(rename_password, value['hotspot']['ssid'])
            access_changed = any(value[key] != previous.get(key, default) for key, default in
                                 (('connection_mode', 'private'), ('hotspot_private_lan', False)))
            access_changed |= value['hotspot'].get('ssid', '') != previous.get('hotspot', {}).get('ssid', '')
            access_changed |= value['hotspot'].get('mac', '') != previous.get('hotspot', {}).get('mac', '')
            access_changed |= 'interface_roles' in payload and payload['interface_roles'] != interface_roles(previous)
            if access_changed:
                self.protection.precondition(expected, revision or (lambda: ''))
                self.protection.reject()
            if value['auto_policy'] != auto_policy(previous):
                if expected is None or expected in ('*','"*"') or revision is None:raise ApiError(428,'PRECONDITION_REQUIRED','Automatic policy changes require the actual current revision.')
                if expected!='"'+revision()+'"':raise ApiError(412,'PRECONDITION_FAILED','Configuration changed. Reload the policy.')
            if self.operations.pending or self.discovery_pending or self.certificate_job_pending or self.certificates.busy():
                raise Invalid("Certificate maintenance is in progress.")
            if value['hostname'] != previous['hostname'] and self.certificates.rename_blocked():
                raise Invalid("Disable HTTPS before renaming the bridge; reset/setup will require renewed client trust.")
            self.protection.guard(previous, value, reboot=legacy_reboot)
            self.guard_management(value)
            changed = {p['id'] for p in profiles(previous) if self.pauses.changed(p, next((n for n in profiles(value) if n['id'] == p['id']), None))}
            if any(self.pauses.held(ident) for ident in changed):
                if self.pauses.error: raise ApiError(409, 'PAUSE_REGISTRY_RECOVERY_REQUIRED', 'Recover the pause registry before editing held network intent.')
                self.protection.precondition(expected, revision or (lambda: ''))
            undo = self._unify(value, admin) if password_changed else None
            try:
                self.store.save(value)
            except BaseException:
                if undo: undo()
                raise
            self.serial_mounts.reconcile()
            self.refresh_auth_failures(profiles(value))
            if value['home']!=previous['home']:self.next_home_attempt=0
            self.pauses.clear(changed)
            operation_id = self.connection_events.request(previous, value, cause)
            self.blocked.clear()
            # Legacy clients retain the original reboot contract. The new UI
            # applies settings live; profile switches never restart the home radio.
            reboot = legacy_reboot
            if reboot:
                self.action_at = time.monotonic() + 4
        self.update(message="Configuration saved. Applying settings…")
        return {"ok": True, "url": "http://" + value["hostname"] + ".local", "reboot_in": 4 if reboot else 0,
                "operation_id": operation_id, "sessions_invalidated": password_changed}

    def _password_valid(self, password):
        """Check the current bridge password, at most five tries a minute. Caller holds the lock."""
        now = time.monotonic()
        while self.password_attempts and now - self.password_attempts[0] >= 60:
            self.password_attempts.popleft()
        if len(self.password_attempts) >= 5:
            raise Invalid("Too many attempts. Try again in a minute.")
        self.password_attempts.append(now)
        encoded = self.store.config["admin_hash"]
        valid = password_matches(password, encoded) if encoded else hmac.compare_digest(password.encode(), self.bootstrap["password"].encode())
        if valid:
            self.password_attempts.clear()
        return valid

    def _hotspot_key_ssid(self, config):
        """The network name the hotspot key is derived for: the name the hotspot uses now."""
        from .password import valid_key
        explicit = config.get('hotspot', {}).get('ssid', '')
        if explicit:
            return explicit
        if valid_key(config.get('hotspot_key')):
            return config['hotspot_key']['ssid']
        hotspot = self.state.get('hotspot', {})
        radio = (hotspot.get('mac') if hotspot.get('active') else '') or config.get('hotspot', {}).get('mac') \
            or reserved_hotspot_mac(config) or next((d['mac'] for d in self.state.get('devices', []) if d.get('ap', True)), '') \
            or getattr(self.store, 'mac', '')
        return hotspot_ssid({k: v for k, v in config.items() if k != 'hotspot_key'}, radio)

    def _unify(self, value, password):
        """Give the Linux account, the web interface and the hotspot this one password.

        The Linux account is changed first: if it fails, the exception leaves the
        configuration untouched. The caller saves value in one write and calls the
        returned undo (or None) if that write fails.
        """
        from .password import hotspot_key
        from .runtime import linux_account
        account = linux_account(self.demo)
        undo = self.net.linux_password(password) if account else None
        value["admin_hash"] = password_hash(password)
        value["hotspot_key"] = hotspot_key(password, self._hotspot_key_ssid(value))
        value.pop("hotspot_password", None)
        if account:
            value["linux_password_default"] = False
        return undo if callable(undo) else None

    def credentials(self, payload):
        """Change the single bridge password: web interface, hotspot and Linux account.

        Earlier clients name one target (web, hotspot or linux); every target now
        changes all three, so the rule "one password" always holds. All or nothing:
        a Linux account that cannot be changed leaves everything as it was.
        """
        target = payload.get("target", "all")
        if target not in ("all", "web", "hotspot", "linux"):
            raise Invalid("Unknown account.")
        from .runtime import linux_account
        from .password import check, PasswordRuleError
        if target == "linux" and not linux_account(self.demo):
            raise Invalid("Unknown account.")
        password = text(payload.get("password"), 128)
        try:
            check(password)
        except PasswordRuleError as error:
            raise Invalid(str(error)) from None
        current = text(payload.get("current_password"), 128)
        with self.lock:
            if self.operations.pending: raise ApiError(409, 'CONNECTION_OPERATION_PENDING', 'A connection operation is pending.')
        with self.net_lock, self.lock:
            self.protection.reject()
            if self.operations.pending: raise ApiError(409, 'CONNECTION_OPERATION_PENDING', 'A connection operation is pending.')
            if not self._password_valid(current):
                raise Invalid("Incorrect current administrator password.")
            value = copy.deepcopy(self.store.config)
            undo = self._unify(value, password)
            try:
                self.store.save(value)
            except BaseException:
                if undo: undo()
                raise
        changed = not hmac.compare_digest(password.encode(), current.encode())
        return {"ok": True, "sessions_invalidated": changed,
                "hotspot_reconnect": changed and bool(self.state["hotspot"].get("active"))}

    def telescope_action(self, payload, cause="MANUAL", expected=None, revision=None):
        ident, action = payload.get("id"), payload.get("action")
        if action not in ("connect", "disconnect", "delete"):
            raise Invalid("Unknown action.")
        with self.lock:
            if self.operations.pending: raise ApiError(409, 'CONNECTION_OPERATION_PENDING', 'A connection operation is pending.')
            if self.discovery_pending: raise ApiError(409, 'NETWORK_BUSY', 'Discovery is in progress.')
            value = copy.deepcopy(self.store.config)
            entries = copy.deepcopy(profiles(value))
            chosen = next((p for p in entries if p["id"] == ident), None)
            if not chosen:
                raise Invalid("Unknown profile.")
            self.standby.discard(ident)
            if self.pauses.held(ident):
                self.protection.reject(ident)
                self.protection.precondition(expected, revision or (lambda: ''))
            if action == "connect":
                if self.auth_failures.held(chosen):
                    raise ApiError(409, 'WIFI_AUTHENTICATION_FAILED', 'Change the saved Wi-Fi password or reboot the bridge before trying again.')
                target = payload.get("mac", chosen["mac"])
                if not isinstance(target, str) or target not in {d["mac"] for d in self.state["devices"]}:
                    raise Invalid("The selected adapter is unavailable.")
                if target == value.get("home", {}).get("mac"):
                    raise Invalid("This adapter is reserved for the private network.")
                if not role_allows(value, target, 'telescope'):
                    raise ApiError(409, 'MANAGEMENT_RADIO_RESERVED', 'Change the adapter role in Wi-Fi adapters before assigning it to an instrument.')
                if "expected_mac" in payload and payload["expected_mac"] != chosen["mac"]:
                    raise Invalid("Assignment changed. Refresh before moving this telescope.")
                chosen["mac"] = target
                for p in entries:
                    if p["mac"] == chosen["mac"]:
                        p["enabled"] = p["id"] == ident
            elif action == "disconnect":
                chosen["enabled"] = False
            else:
                entries.remove(chosen)
            # Blank passwords retain the stored secret for the same profile/SSID.
            value["telescopes"] = entries
            value["admin_password"] = ""
            previous = self.store.config
            normalized = validate_config(value, previous, self.state["devices"])
            self.protection.guard(previous, normalized)
            self.guard_management(normalized)
            if self.pauses.error: raise ApiError(409, 'PAUSE_REGISTRY_RECOVERY_REQUIRED', 'Recover the pause registry before retrying individual connections.')
            changed = {p['id'] for p in profiles(previous) if self.pauses.changed(p, next((n for n in profiles(normalized) if n['id'] == p['id']), None))} | {ident}
            if any(self.pauses.held(key) for key in changed): self.protection.precondition(expected, revision or (lambda: ''))
            self.store.save(normalized)
            self.refresh_auth_failures(entries)
            self.pauses.clear(changed)
            operation_id = self.connection_events.request(previous, self.store.config, cause, selected=ident)
            self.blocked.clear()
            if action == "delete":
                self.pending_deletions.add(role_for(chosen))
        return {"ok": True, "operation_id": operation_id}

    def retry(self):
        from .runtime import capabilities
        if not capabilities(self.demo)['host_reboot'] and not self.demo:
            raise Invalid("The OS updater prerequisites are not installed.")
        with self.lock:
            self.protection.reject()
            if self.operations.pending: raise ApiError(409, 'CONNECTION_OPERATION_PENDING', 'A connection operation is pending.')
            if self.discovery_pending or self.certificate_job_pending or self.certificates.busy():
                raise ApiError(409, 'HTTPS_BUSY', 'Certificate maintenance is in progress.')
            self.action_at = time.monotonic() + 4
        self.update(mode="rebooting", message="Rebooting and retrying the connection…")

    def refresh_auth_failures(self, entries):
        for ident in self.auth_failures.prune(entries):
            self.attempts.pop(role_for({'id': ident}), None)
            self.retries.pop(role_for({'id': ident}), None)
            self.operations.negative.pop(ident, None)

    def scan(self, name):
        if name not in {d["name"] for d in self.status()["devices"]}:
            raise Invalid("Unknown interface.")
        if not self.net_lock.acquire(blocking=False):
            raise Invalid("Connection in progress. Try scanning again shortly.")
        try:
            return self.net.scan(name)
        finally:
            self.net_lock.release()

    def active_ap(self, devices):
        known = self.state.get('hotspot', {}).get('mac')
        ordered = sorted(devices, key=lambda d:d['mac'] != known)
        return next((d for d in ordered if self.net.connected('setup', d)), None)

    def ap_can_stop(self, device):
        # Caller holds net_lock. Never use display clients/DHCP or cached vacancy.
        with self.lock:
            if (self.protection.protected() or self.operations.pending or self.discovery_pending
                    or self.certificate_job_pending or self.certificates.busy() or self.action_at is not None
                    or self.stop.is_set() or self.cleanup_required): return False
        if not self.net.ap_vacant(device): return False
        with self.lock:
            return not (self.protection.protected() or self.operations.pending or self.discovery_pending
                        or self.certificate_job_pending or self.certificates.busy() or self.action_at is not None
                        or self.stop.is_set() or self.cleanup_required)

    def _hotspot_event(self, reason=None, device=None):
        """Change-only management events; never include network names or errors."""
        previous = getattr(self, '_hotspot_failure', None)
        if reason == previous: return
        self._hotspot_failure = reason
        self.event_store.append('hotspot_unavailable' if reason else 'hotspot_restored',
            operation_id=uuid.uuid4().hex, profile_id='hotspot', profile_name='',
            radio_id=self.connection_events.radio(device['mac']) if device else None,
            cause='RECONCILE', reason=reason or 'HOTSPOT_RESTORED',
            outcome='failed' if reason else 'ready', associated=not reason, ready=not reason)

    def _ap_failed(self, device, message, diagnosis=None):
        """Count a hotspot failure on this radio and say it once in the service log."""
        mac = device.get('mac') if device else None
        if not mac:
            return
        count = self.ap_failures.get(mac, 0) + 1
        self.ap_failures[mac] = count
        if self.ap_confirmed and self.ap_confirmed[0] == mac:
            self.ap_confirmed = None
        name = device.get('name') or 'its radio'
        if count == 1:
            self.log(message.format(name=name))
        if count == HOTSPOT_FAILURE_LIMIT:
            self.log('the hotspot failed again on ' + name + '. Another free Wi-Fi adapter is tried first from now on.')
        text = (diagnosis() if callable(diagnosis) else diagnosis) if count in (1, HOTSPOT_FAILURE_LIMIT) and diagnosis else None
        if text:
            # Why it did not take: radio mode, who holds the radio, hostapd's own words.
            self.log('state when it failed: ' + text + '.')

    def _ap_confirmed(self, device, now):
        mac = device['mac']
        if not self.ap_confirmed or self.ap_confirmed[0] != mac:
            self.ap_confirmed = (mac, now)
        elif now - self.ap_confirmed[1] >= HOTSPOT_STABLE:
            self.ap_failures.pop(mac, None)

    def _ap_failing(self, device):
        return self.ap_failures.get(device['mac'], 0) >= HOTSPOT_FAILURE_LIMIT

    def _reclaim_unconfirmed_ap(self, devices, now):
        """Stop a recorded hotspot that does not work, so that it can start again.

        The AP owner record keeps its radio out of NetworkManager. When its
        access point or DHCP service stays down, nothing else can ever use that
        radio or start another hotspot, so after a grace period the bridge stops
        its own AP services and hands the radio back, as a normal AP stop does.
        """
        owner_of = getattr(self.net, 'hotspot_owner', None)
        owner = owner_of() if owner_of else ''
        if not owner:
            self.ap_unconfirmed = None
            return
        if not self.ap_unconfirmed or self.ap_unconfirmed[0] != owner:
            self.ap_unconfirmed = (owner, now, False)
            return
        _, since, reported = self.ap_unconfirmed
        if now - since < HOTSPOT_UNCONFIRMED_GRACE:
            return
        device = next((d for d in devices if d['name'] == owner), {'name': owner})
        observe = getattr(self.net, 'ap_diagnosis', None)
        said = getattr(self, 'ap_unconfirmed_said', {})
        self.ap_unconfirmed_said = said
        if observe and not reported and said.get(owner, 0) < HOTSPOT_FAILURE_LIMIT:
            said[owner] = said.get(owner, 0) + 1
            # Twice per radio and boot at most. Said before the stop changes the radio: why this hotspot was never confirmed.
            self.log('the hotspot on ' + owner + ' was not confirmed for ' + str(HOTSPOT_UNCONFIRMED_GRACE)
                     + ' seconds. State: ' + observe(owner) + '.')
        try:
            self.net.disconnect('setup')
        except NetworkError:
            if not reported:
                self.log('the hotspot on ' + owner + ' does not work and could not be stopped. '
                            'Check: rc-service wifi-bridge-ap status; tail /var/log/wifi-bridge-ap.log')
            self.ap_unconfirmed = (owner, since, True)
            return
        self.ap_unconfirmed = None
        self.update(setup_ssid='', hotspot={'active': False, 'reason': 'start_failed'})
        # The failure itself was counted when the hotspot was lost or did not start.
        self.log('the radio ' + device['name'] + ' was released from the stopped hotspot. The hotspot starts again.')

    def _hotspot_key(self, mac):
        """Network name and derived WPA key for a hotspot on this radio; never the password."""
        from .password import hotspot_psk, valid_key
        config = self.store.config
        ssid = hotspot_ssid(config, mac)
        key = config.get('hotspot_key')
        if valid_key(key) and key['ssid'] != ssid:
            ssid = key['ssid']  # The key names the network it belongs to.
        return ssid, hotspot_psk(config, ssid, self.bootstrap['password'])

    def _ap(self, devices, reserved, emergency=False, only=None, now=None):
        now = time.monotonic() if now is None else now
        current = self.active_ap(devices)
        if not current and self.state['hotspot'].get('active'):
            self._hotspot_event('HOTSPOT_RADIO_UNAVAILABLE')
            lost = next((d for d in devices if d['mac'] == self.state['hotspot'].get('mac')), None)
            observe = getattr(self.net, 'ap_diagnosis', None)
            self._ap_failed(lost, 'the hotspot on {name} stopped. It is restarted automatically. '
                                  'Details: tail /var/log/wifi-bridge-ap.log',
                            (lambda: observe(lost['name'])) if observe and lost else None)
        preferred = self.store.config.get('hotspot', {}).get('mac', '')
        if current and not role_allows(self.store.config, current['mac'], 'hotspot'):
            self.protection.reject()
            self.net.disconnect('setup')
            self.update(setup_ssid='', hotspot={'active': False, 'reason': 'adapter_role_changed'})
            current = None
        current_name = self.net.hotspot_name() if current else ''
        if current and (not preferred or current['mac'] == preferred) and self.ap_password in (None,self._hotspot_key(current['mac'])[1]) and current_name == self._hotspot_key(current['mac'])[0]:
            self.ap_password = self._hotspot_key(current['mac'])[1]
            self._ap_confirmed(current, now)
            self._hotspot_event(device=current)
            reason=self.state['hotspot'].get('reason')
            if reason not in ('requested','recovery','recovery_retained'):reason='recovery' if emergency else 'requested'
            self.update(setup_ssid=current_name,hotspot={'active':True,'reason':reason,'interface':current['name'],'mac':current['mac'],'ip':'192.168.77.1',**self.net.hotspot_clients(current)})
            return current
        owner_of = getattr(self.net, 'hotspot_owner', None)
        if not current and owner_of and owner_of():
            # The previous AP is still recorded but not working: every other
            # radio would be refused until it is stopped and released.
            self.update(setup_ssid='', hotspot={'active': False, 'reason': 'start_failed'})
            return None
        if (self.protection.protected() or self.operations.pending) and not emergency:
            if current:
                self.update(setup_ssid=current_name,hotspot={'active':True,'reason':'connection_protected','interface':current['name'],'mac':current['mac'],'ip':'192.168.77.1',**self.net.hotspot_clients(current)})
                return current
            self.update(setup_ssid='',hotspot={'active':False,'reason':'connection_operation_pending' if self.operations.pending else 'connection_protected'})
            return None
        protected = {p['mac'] for p in profiles(self.store.config) if self.protection.protected(p['id']) or self.pauses.held(p['id'])}
        home_mac = self.store.config.get('home',{}).get('mac')
        candidates = [d for d in devices if d['mac'] not in protected and d.get('ap',True) and role_allows(self.store.config, d['mac'], 'hotspot')
                      and (d['mac'] not in reserved or emergency and d['mac']==home_mac)
                      and (only is None or d['mac']==only['mac']) and self.net.radio_idle(d)]
        # A radio that keeps failing is tried after the other free radios, so a
        # hotspot that cannot run on the saved adapter moves to another one.
        candidates.sort(key=lambda d:(self._ap_failing(d),d['mac']==home_mac,d['usb'],d['name']))
        selected = next((d for d in candidates if d['mac'] == preferred
                         and not (self._ap_failing(d) and (len(candidates) > 1 or current))), None)
        if preferred:
            device = selected or (current if current and current['mac'] == preferred else None)
            if device is None:
                # Keep a working management path; recovery may use another free
                # radio, but never change the persisted owner-selected adapter.
                device = current or (candidates[0] if candidates else None)
            if device is current and current and current['mac'] != preferred:
                self._ap_confirmed(current, now)
                self.update(setup_ssid=current_name,hotspot={'active':True,'reason':'selected_adapter_unavailable',
                    'interface':current['name'],'mac':current['mac'],'ip':'192.168.77.1',**self.net.hotspot_clients(current)})
                return current
        else:
            device = current or (candidates[0] if candidates else None)
        if current:self.protection.reject()
        if not device:
            self.update(setup_ssid='',hotspot={'active':False,'reason':'protected_no_recovery_radio' if emergency and protected else 'selected_adapter_unavailable' if preferred else 'no_free_radio'})
            if getattr(self, '_hotspot_failure', None) != 'HOTSPOT_RADIO_UNAVAILABLE':
                self.log('no free Wi-Fi adapter can start the hotspot now. Plug in a USB Wi-Fi adapter or an Ethernet cable, '
                            'or check: rc-service wifi-bridge-ap status')
            self._hotspot_event('HOTSPOT_RADIO_UNAVAILABLE')
            return None
        # Try each eligible radio once per reconcile; a failed preferred adapter
        # never prevents an independent free radio from restoring management.
        attempts = [device] + [d for d in candidates if d['mac'] != device['mac']]
        for device in attempts:
            self.firewall(extra_isolated=[device])
            # Migrate an existing preference even if absent; only a first-ever
            # bootstrap reserves the selected radio before starting the AP.
            if not self.store.config.get('home') and 'interface_roles' not in self.store.config:
                with self.lock:
                    saved = copy.deepcopy(self.store.config)
                    reservation = preferred or device['mac']
                    saved['interface_roles'] = {reservation: 'hotspot'}
                    saved['hotspot'] = dict(saved.get('hotspot', {}), mac=reservation, enabled=True)
                    self.store.save(saved)
            ssid, psk = self._hotspot_key(device['mac'])
            try:
                self.net.hotspot(device,ssid,psk,self.hotspot_route_targets(devices))
            except NetworkError:
                surviving = self.active_ap(devices)
                if surviving:
                    # A failure before backend teardown can leave the old AP
                    # healthy. Preserve it instead of reporting a false outage.
                    self.update(setup_ssid=self.net.hotspot_name(), hotspot={'active':True,
                        'reason':'selected_adapter_unavailable', 'interface':surviving['name'],
                        'mac':surviving['mac'], 'ip':'192.168.77.1', **self.net.hotspot_clients(surviving)})
                    return surviving
                self._hotspot_event('HOTSPOT_START_FAILED', device)
                self._ap_failed(device, 'the hotspot could not start on {name}. '
                                        'Details: tail /var/log/wifi-bridge-ap.log',
                                getattr(self.net, 'last_ap_diagnosis', None))
                continue
            self.ap_password = psk
            self._ap_confirmed(device, now)
            self.log('the hotspot started on ' + device['name'] + '.')
            self._hotspot_event(device=device)
            self.update(setup_ssid=ssid,
                        hotspot={'active':True,'reason':'recovery' if emergency else 'requested',
                                 'interface':device['name'],'mac':device['mac'],'ip':'192.168.77.1',**self.net.hotspot_clients(device)})
            return device
        self.update(setup_ssid='', hotspot={'active':False,'reason':'start_failed'})
        return None

    def hotspot_route_targets(self, devices):
        # Include inactive saved instruments so normal switching can reuse a lease.
        # The home FIB still matters when its ingress was rejected for overlap.
        networks, addresses = [], set()
        home_mac = self.store.config.get('home', {}).get('mac')
        for device in devices:
            value = self.net.address(device, timeout=2)
            if not value:
                continue
            interface = ipaddress.IPv4Interface(value)
            addresses.update((interface.ip, interface.network.network_address, interface.network.broadcast_address))
            if device['mac'] == home_mac:
                networks.append(interface.network)
        return hotspot_route_targets((p.get('target_ip') for p in profiles(self.store.config) if not p.get('isolated')), networks, addresses)

    def management_paths(self, home, ap):
        # An interface that has just lost its address is no management path.
        return [path for path in self.net.management_paths(home,ap) if path.get('address')]

    def wired_home(self):
        # Demo and older synthetic backends need not expose physical Ethernet.
        getter = getattr(self.net, 'wired_home', None)
        return getter() if getter else None

    def current_management(self, devices=None):
        """Recheck only advertised paths under net_lock before a network mutation."""
        devices=self.net.devices() if devices is None else devices
        paths=[]
        for path in self.state.get('management',{}).get('paths',[])[:2]:
            mac=self.store.config.get('home',{}).get('mac') if path['role']=='home' else self.state['hotspot'].get('mac')
            device=next((d for d in devices if d['mac']==mac and d['name']==path['interface']),None)
            if path['role'] == 'home':
                wired = self.wired_home()
                if wired and wired['name'] == path['interface']: device = wired
            if not device:continue
            up=self.net.connected('home',device,self.store.config['home']) if path['role']=='home' else self.net.connected('setup',device)
            if not up:continue
            address=self.net.address(device);interface=ipaddress.ip_interface(address)
            if str(interface.ip)==path['ip'] and str(interface.network)==path['subnet']:
                paths.append({'role':path['role'],'device':device,'address':address})
        return paths

    def occupied_management_networks(self, devices):
        # Rejected home ingress can still own a preferred DHCP route in the FIB.
        networks=[ipaddress.ip_network('192.168.77.0/24')]
        networks += [ipaddress.ip_interface(path['address']).network for path in self.current_management(devices)]
        home=next((d for d in devices if d['mac']==self.store.config.get('home',{}).get('mac')),None)
        if home and self.net.connected('home',home,self.store.config['home']):networks.append(ipaddress.ip_interface(self.net.address(home)).network)
        return list(dict.fromkeys(networks))

    def _management(self, devices, config, now):
        tick=time.monotonic();by_mac={d['mac']:d for d in devices}
        home=by_mac.get(config.get('home',{}).get('mac'));ap=self.active_ap(devices)
        if not ap:
            self._reclaim_unconfirmed_ap(devices,now)
            self.net.release_stale_hotspot(devices)
        else:self.ap_unconfirmed=None
        reserved={p['mac'] for p in profiles(config) if p['enabled']}
        if config.get('home'): reserved.add(config['home']['mac'])
        if ap:ap=self._ap(devices,reserved,emergency=not config.get('hotspot',{}).get('enabled',False),now=now)
        retry={'reason':'NOT_CONFIGURED' if not config['home'] else 'HOME_READY','remaining_seconds':0}
        home_changed='home' in self.applied and self.applied['home']!=config['home']
        if home_changed:
            self.firewall();self.net.disconnect('home');self.applied.pop('home',None);self.next_home_attempt=0
        home_up=bool(home and config['home'] and self.net.connected('home',home,config['home']))
        wired=self.wired_home()
        # An owned Ethernet path can free the only onboard radio for the AP.
        # Keep other allocations and user-protected work unchanged.
        from .cameras.discovery import pi5_appliance
        single_radio_shared=(pi5_appliance() and config.get('connection_mode')=='hotspot_shared' and wired
            and wired['name']=='eth0' and len(devices)==1 and home and home['name']=='wlan0'
            and not home.get('usb',True) and home.get('ap',True)
            and role_allows(config,home['mac'],'hotspot')
            and not any(p['enabled'] and p['mac']==home['mac'] for p in profiles(config)))
        if single_radio_shared:
            wired_network=ipaddress.ip_interface(self.net.address(wired)).network
            if wired_network.overlaps(ipaddress.ip_network('192.168.77.0/24')):
                single_radio_shared=False
        if single_radio_shared and home_up and not ap:
            with self.lock:
                safe=not (self.protection.protected() or self.operations.pending or self.discovery_pending
                    or self.certificate_job_pending or self.certificates.busy() or self.action_at is not None
                    or self.stop.is_set() or self.cleanup_required or self.pauses.error or self.pauses.records)
            # Recheck ownership immediately before the disruptive command.
            if safe and self.wired_home()==wired and self.net.connected('home',home,config['home']):
                self.firewall(home=wired,setup=None,telescopes=[])
                self.net.disconnect('home');home_up=False
        stopped_ap=None
        if config['home'] and not home_up and not single_radio_shared:
            retry={'reason':'HOME_RADIO_MISSING' if not home else 'RETRY_WAIT','remaining_seconds':max(0,int(self.next_home_attempt-now))}
            # No new home attempt once a restart is requested or the service is stopping.
            if home and now>=self.next_home_attempt and not self.halting():
                if ap and ap['mac']==home['mac']:
                    if self.ap_can_stop(ap):
                        self.update(message='Empty recovery hotspot: a bounded private-network retry may briefly interrupt management.')
                        stopped_ap=ap;self.net.disconnect('setup');ap=None
                        self.update(setup_ssid='',hotspot={'active':False,'reason':'home_retry'})
                    else:retry={'reason':'HOME_RETRY_DEFERRED_AP_RESERVED','remaining_seconds':0}
                if not ap or ap['mac']!=home['mac']:
                    if self.net.radio_idle(home):
                        self._home_retry_note('HOME_RETRY_ATTEMPT', home)
                        # Keep a separate AP and all validated routes throughout the blocking command.
                        self.firewall()
                        try:
                            self.net.connect('home',home,config['home']);home_up=self.net.connected('home',home,config['home'])
                        except NetworkError:home_up=False
                        if home_up:self.applied['home']=copy.deepcopy(config['home'])
                        if stopped_ap and not home_up:
                            try:ap=self._ap(devices,set(),emergency=True,only=stopped_ap,now=now)
                            except NetworkError:
                                self.update(hotspot={'active':False,'reason':'restore_failed'})
                            retry={'reason':'HOME_RETRY_FAILED_AP_RESTORED' if ap else 'HOME_RETRY_FAILED_AP_RESTORE_FAILED','remaining_seconds':45}
                        else:retry={'reason':'HOME_READY' if home_up else 'HOME_RETRY_FAILED','remaining_seconds':0 if home_up else 45}
                        completed=now+max(0,time.monotonic()-tick)
                        self.last_home_attempt=completed;self.next_home_attempt=completed+45
                    elif not ap or ap['mac']!=home['mac']:retry={'reason':'HOME_RETRY_DEFERRED_RADIO_BUSY','remaining_seconds':0}
        self._home_retry_note(retry['reason'], home)
        if not home_up:home=None
        home = self.wired_home() or home
        instrument_networks=[];instrument_subnets={}
        for profile in profiles(config):
            device=by_mac.get(profile['mac'])
            if profile['enabled'] and not profile.get('isolated') and device and self.net.connected(role_for(profile),device,profile):
                address=self.net.address(device)
                if not address:continue  # Linked, but its address is gone: it occupies no network now.
                subnet=ipaddress.ip_interface(address).network
                instrument_networks.append(subnet);instrument_subnets[profile['id']]=subnet
        self.routing_conflicts={ident for ident,subnet in instrument_subnets.items() if home and ipaddress.ip_interface(self.net.address(home)).network.overlaps(subnet)}
        if self.routing_conflicts:
            home=None;retry={'reason':'MANAGEMENT_TELESCOPE_SUBNET_CONFLICT','remaining_seconds':0}
        desired_ap=config.get('hotspot',{}).get('enabled',False) or config.get('connection_mode','private')!='private'
        reserved=({home['mac']} if home else set())|{p['mac'] for p in profiles(config) if p['enabled']}
        if not ap and (desired_ap or not home) and retry['reason']!='HOME_RETRY_FAILED_AP_RESTORE_FAILED':
            ap_network=ipaddress.ip_network('192.168.77.0/24')
            overlap=any(ap_network.overlaps(n) for n in instrument_networks+([ipaddress.ip_interface(self.net.address(home)).network] if home else []))
            if overlap:self.update(setup_ssid='',hotspot={'active':False,'reason':'subnet_conflict'})
            else:ap=self._ap(devices,reserved,emergency=not home,now=now)
        elif ap and home and not desired_ap:
            explicit_stop=self.state['hotspot'].get('reason')=='requested'
            if explicit_stop or self.ap_can_stop(ap):
                self.net.disconnect('setup');ap=None;self.update(setup_ssid='',hotspot={'active':False,'reason':'disabled'})
            else:self.update(hotspot=dict(self.state['hotspot'],reason='recovery_retained'))
        if home and ap and ipaddress.ip_interface(self.net.address(home)).network.overlaps(ipaddress.ip_network('192.168.77.0/24')):
            home=None;retry={'reason':'MANAGEMENT_SUBNET_CONFLICT','remaining_seconds':0}
        # A home association established above may add a newly conflicting route.
        if ap:self.net.hotspot_dhcp(ap,self.hotspot_route_targets(devices))
        paths=self.management_paths(home,ap)
        settings, runtime = self.announcement()
        cameras = getattr(self, 'camera_service', None)
        if cameras:
            ingress = {(str(ipaddress.ip_interface(p['address']).ip), p['device']['name'],
                        str(ipaddress.ip_interface(p['address']).network)) for p in paths}
            if cameras.advertises_indi(ingress):
                runtime['indi'] = 7624
        if self.net.identity(config['hostname'], [p['device'] for p in paths], settings, runtime):
            self.event_store.append('announcement_updated', operation_id=uuid.uuid4().hex,
                profile_id='bridge', profile_name='', radio_id=None, cause='RECONCILE',
                reason='ANNOUNCEMENT_UPDATED', outcome='ready',
                interfaces=['home' if p['role'] == 'home' else 'hotspot' for p in paths])
        with self.lock:
            self.state['management']={'mode':'dual' if home and ap else 'home_only' if home else 'ap_only' if ap else 'none',
                'paths':[{'role':p['role'],'kind':p['device'].get('kind','wifi'),'interface':p['device']['name'],'ip':str(ipaddress.ip_interface(p['address']).ip),'subnet':str(ipaddress.ip_interface(p['address']).network)} for p in paths],
                'reason':'ROUTING_CONFLICT' if self.routing_conflicts else 'READY' if paths else 'MANAGEMENT_UNAVAILABLE','home_retry':retry}
        self.update(home_ip=self.net.address(home).split('/')[0] if home else '')
        return home,ap

    def halting(self):
        """True once a restart is requested or the service is stopping."""
        return self.action_at is not None or self.stop.is_set()

    def _home_retry_note(self, reason, device):
        """Write one service-log line when the home Wi-Fi retry reason changes.

        Waiting between attempts keeps the last reason. A new attempt after a failure
        writes nothing, so a long outage gives one failure line, then one line when
        the home Wi-Fi is back.
        """
        previous = self.home_retry_logged
        if reason == 'RETRY_WAIT' or reason == previous or reason not in HOME_RETRY_MESSAGES and reason != 'NOT_CONFIGURED':
            return
        if reason == 'HOME_RETRY_ATTEMPT' and previous and previous.startswith('HOME_RETRY_FAILED'):
            return
        self.home_retry_logged = reason
        # A home Wi-Fi already up at start, or no home Wi-Fi saved, is no change worth a line.
        if reason == 'NOT_CONFIGURED' or reason == 'HOME_READY' and previous in (None, 'NOT_CONFIGURED'):
            return
        name = device['name'] if device else ''
        radio = 'radio state not read'
        observed = getattr(self.net, 'radio_observations', {}).get(name) if device else None
        if observed:
            radio = 'NetworkManager state ' + observed['state'] + ', iw link ' + observed['link']
        self.home_log(HOME_RETRY_MESSAGES[reason].format(name=name, radio=radio))

    def retained_forwarding(self):
        """Keep unaffected, still-owned validated routes during free-radio preparation."""
        config = self.store.config
        devices = {d['mac']:d for d in self.state['devices']}
        home = devices.get(config.get('home', {}).get('mac'))
        if not home or not self.net.connected('home', home, config['home']): home = None
        home = self.wired_home() or home
        rows = {row['id']:row for row in self.state['telescopes']}
        pending = self.operations.records[self.operations.active]['public']['profile_id'] if self.operations.pending else None
        relays = []
        for profile in profiles(config):
            row = rows.get(profile['id'], {}); device = devices.get(profile['mac'])
            if (profile.get('isolated') or self.pauses.held(profile['id']) or profile['id'] == pending or profile['id'] == getattr(self.operations, 'victim_id', None) or not profile['enabled'] or not device
                    or row.get('state') not in ('online','waiting_service') or not row.get('connected')
                    or network_intent(self.applied.get(role_for(profile))) != network_intent(profile)):
                continue
            if not self.net.connected(role_for(profile), device, profile): continue
            network = ipaddress.ip_interface(self.net.address(device))
            if str(network.network) != row.get('subnet') or ipaddress.ip_address(profile['target_ip']) not in network.network: continue
            if home and network.network.overlaps(ipaddress.ip_interface(self.net.address(home)).network):continue
            relays.append({'profile_id':profile['id'],'device':device,'target':profile['target_ip'],
                           'kind':profile['kind'],**{proxy_flag(p['id']):bool(row.get(proxy_flag(p['id']))) for p in relay_plugins()}})
        ap = devices.get(self.state['hotspot'].get('mac')) if self.state['hotspot'].get('active') else None
        if ap and not self.net.connected('setup',ap):ap=None
        if home and (self.routing_conflicts or ap and ipaddress.ip_interface(self.net.address(home)).network.overlaps(ipaddress.ip_interface(self.net.address(ap)).network)):home=None
        return {'home':home,'telescopes':relays if home or ap else [],'setup':ap}

    def firewall(self, **kwargs):
        with self.lock:
            self.discovery_plan = None
        extra=kwargs.pop('extra_isolated',[])
        if not kwargs:
            kwargs = self.retained_forwarding()
        config = self.operations.intent(self.pauses.apply(copy.deepcopy(self.store.config)))
        macs = {p['mac'] for p in profiles(config)} - {config.get('home', {}).get('mac')}
        macs |= set(self.cleanup_required.values())
        if self.operations.pending and self.operations.allocated: macs.add(self.operations.allocated)
        macs|={d['mac'] for d in extra}
        if not kwargs.get('home'):macs.add(config.get('home',{}).get('mac'))
        isolated = [d for d in self.state['devices'] if d['mac'] in macs]
        access = self.net.firewall(isolated=isolated, **kwargs,
            sharing=config.get('connection_mode')=='hotspot_shared',
            private_lan=config.get('hotspot_private_lan',False),
            hotspot_targets=self.hotspot_route_targets(self.state['devices']) if kwargs.get('setup') else ())
        self.update(network_access=dict(access or {'available':False,'reason':'DEMO_NOT_VERIFIED'},
            mode=config.get('connection_mode','private'), configured=config.get('connection_mode')=='hotspot_shared',
            private_lan_allowed=config.get('hotspot_private_lan',False), internet_verified=False))
        paths=self.management_paths(kwargs.get('home'),kwargs.get('setup'))
        proxy=next((r for r in kwargs.get('telescopes',[]) if r.get('seestar_proxy')),None)
        with self.lock:
            self.management_plans=tuple((p['device']['name'],p['address']) for p in paths)
            self.management_plan=next(iter(self.management_plans),None)
            self.discovery_plans=tuple((p['device']['name'],p['address'],proxy['device']['name'],proxy['target']) for p in paths) if proxy else ()
            self.discovery_plan=next(iter(self.discovery_plans),None)

    def setup(self, devices, message):
        # Error recovery can establish management without claiming telescope success.
        self.update(devices=devices,home_ip='')
        reserved={p['mac'] for p in profiles(self.store.config) if p['enabled']}
        ap=self._ap(devices,reserved,emergency=True) if devices else None
        plan=self.retained_forwarding();plan.update(home=None,setup=ap)
        self.firewall(**plan)
        self.update(mode='setup',message=message)

    def _disconnect_profile(self, role):
        """Record intention, then verify disassociation before reporting it."""
        with self.lock:
            candidates = profiles(self._event_config or self.store.config)
            profile = self.applied.get(role) or next((p for p in candidates if role_for(p) == role), None)
            if profile is None:
                profile = next((s.get("profile") for s in self.connection_events.states.values()
                                if s.get("profile") and role_for(s["profile"]) == role), None)
            context = self._event_contexts.get(profile["id"]) if profile else None
            if profile:
                self.connection_events.phase(profile, "disconnecting", "DISCONNECTING", context)
        self.net.disconnect(role)
        if profile:
            device = next((d for d in self.state["devices"] if d["mac"] == profile.get("mac")), None)
            associated = bool(device and self.net.connected(role, device))
            if not associated:
                with self.lock:
                    collector = getattr(self, 'link_collector', None)
                    if collector: collector.connection(profile['id'], False)
                    self._event_contexts[profile["id"]] = self.connection_events.observe(
                        dict(profile, enabled=False), False, False, "saved", "PROFILE_DISABLED", context)
            else:
                with self.lock:
                    self.connection_events.phase(profile, "failed", "RECONCILE_FAILED", context)

    def _observe_cycle(self, failed=False):
        with self.lock:
            config = self._event_config or copy.deepcopy(self.store.config)
            current = {p["id"]: p for p in profiles(config)}
            previous = copy.deepcopy(self.connection_events.states)
            rows = {p["id"]: p for p in self.state["telescopes"]}
            devices = {d["mac"]: d for d in self.state["devices"]}
            mode = self.state["mode"]
        for ident in dict.fromkeys([*previous, *current]):
            profile = current.get(ident) or dict(previous[ident].get("profile", {}), enabled=False)
            if not profile.get("id"):
                continue
            row = rows.get(ident, {})
            context = self._event_contexts.get(ident)
            phase = row.get("state", "saved")
            associated = bool(row.get("connected")) if ident in current else False
            ready = bool(row.get("relay_ready")) and not failed and ident in current
            if failed or (not profile.get("enabled") and previous.get(ident, {}).get("associated")):
                # Recovery can suspend forwarding while preserving Wi-Fi on another
                # adapter. Do not turn its synthetic disconnected status into an event.
                # The desired profile may have moved while recovery kept its old
                # association. Inspect actual role ownership on both known adapters.
                macs = dict.fromkeys([previous.get(ident, {}).get("mac"), profile.get("mac")])
                try:
                    associated = False
                    for mac in macs:
                        device = devices.get(mac)
                        if device and self.net.connected(role_for(profile), device):
                            associated = True
                            profile = dict(profile, mac=mac)
                            break
                except NetworkError:
                    associated = previous.get(ident, {}).get("associated", False)
                    if associated:
                        profile = dict(profile, mac=previous[ident].get("mac"))
                ready = False
            if failed:
                phase, reason = "error", "RECONCILE_FAILED"
            elif profile.get("enabled") and profile.get("mac") not in devices:
                phase, reason, associated, ready = "missing", "RADIO_UNAVAILABLE", False, False
            elif phase == "conflict":
                reason = "CONFIGURATION_CONFLICT"
            elif phase == "invalid_ip":
                reason = "INVALID_TARGET"
            elif phase == 'authentication_failed':
                reason = 'WIFI_AUTHENTICATION_FAILED'
            elif not self.state.get("management",{}).get("paths"):
                reason = "HOME_UNAVAILABLE"
            elif not profile.get("enabled"):
                if associated:
                    phase, reason = "error", "RECONCILE_FAILED"
                else:
                    reason = "PROFILE_DISABLED" if ident in current else "PROFILE_REMOVED"
            elif associated and not ready:
                reason = "SERVICE_UNAVAILABLE"
            elif not associated:
                reason = "CONNECTION_LOST" if previous.get(ident, {}).get("associated") else "AWAITING_ASSOCIATION"
            else:
                reason = "READY"
            with self.lock:
                collector = getattr(self, 'link_collector', None)
                if collector: collector.connection(ident, associated)
                self._event_contexts[ident] = self.connection_events.observe(profile, associated, ready, phase, reason, context)
                self.connection_events.service(profile, row.get('service'), associated, self._event_contexts[ident])
        with self.lock:
            self._native_event_snapshot()
            self._switch_events()
            self.connection_events.prune(self.event_store.instrument_sequences, devices)
            self.event_store.flush()
            self.telemetry_sessions.reconcile(current,
                {ident for ident, state in self.connection_events.states.items() if state.get('associated')})
            self._event_contexts = {ident: context for ident, context in self._event_contexts.items()
                                    if ident in self.connection_events.contexts}

    def cycle(self, now=None):
        self._event_config = None
        self._event_contexts = {}
        self.last_cycle_ok = False
        try:
            # A network reconcile can take several seconds on the Zero W. Sample
            # both sides (with the collector's five-second throttle), rather than
            # stretching each interval by the reconcile plus the loop's sleep.
            if hasattr(self.net, "sample_activity"): self.net.sample_activity(policy=auto_policy(self.store.config))
            self._cycle(now)
            stations = getattr(self, 'stations', None)
            if stations: stations.tick()
            serial = getattr(self, 'serial_mounts', None)
            if serial: self.serial_events(serial.tick())
            with self.lock: self.operations.gate_cycle()
        except Exception:
            if hasattr(self.net, 'isolated_endpoints'):
                self.net.isolated_endpoints([], [], [])
            with self.lock:
                self.discovery_plan=None;self.discovery_plans=()
                self.state['network_access']={'available':False,'reason':'PLANNING_FAILED'}
                self.state['relay_ready']=False
                for row in self.state['telescopes']:
                    row['relay_ready']=False;row['legacy_ports']=False;row['seestar_proxy']=False;row['dwarf_proxy']=False
                    for p in relay_plugins():row[proxy_flag(p['id'])]=False
                    for path in row.get('management_paths',[]):path['forwarding_ready']=False
                    endpoint = row.get('client_endpoint')
                    if endpoint:
                        endpoint.update(ready=False, reason='PLANNING_FAILED')
                        for path in endpoint.get('alternatives', []):
                            path.update(ready=False, reason='PLANNING_FAILED')
            self._observe_cycle(failed=True)
            raise
        else:
            self._observe_cycle()
            self.last_cycle_ok = True
            if hasattr(self.net, "sample_activity"): self.net.sample_activity(policy=auto_policy(self.store.config))

    def _cycle(self, now=None):
        with self.lock: self.protection.prune()
        now = time.monotonic() if now is None else now
        self.cycle_now = now
        devices = self.net.devices()
        self.update(devices=devices)
        with self.lock:
            config = self.operations.intent(self.pauses.apply(copy.deepcopy(self.store.config)))
            self._event_config = config
            for profile in profiles(config):
                self.connection_events.context(profile)
            self._event_contexts = copy.deepcopy(self.connection_events.contexts)
            action = self.action_at
        if action is not None:
            if now >= action:
                self.net.reboot()
                if self.demo:
                    self.action_at = None
                    self.last_home_attempt = -1000
                    self.applied.clear()
                    self.net.disconnect("home")
                    for role in self.managed_roles:
                        self._disconnect_profile(role)
                else:
                    self.stop.wait(5)
            return
        if devices and self.country != config["country"]:
            self.net.country(config["country"])
            self.country = config["country"]
        for role, mac in list(self.cleanup_required.items()):
            self.net.disconnect(role)
            device = next((d for d in devices if d['mac'] == mac), None)
            if not device or not self.net.connected(role, device): self.cleanup_required.pop(role, None)
        if not self.configured_this_boot:
            self.firewall()
            owned = self.net.known_telescope_roles()
            wanted = {role_for(p) for p in profiles(config) if p['enabled']}
            for role in owned - wanted:
                if self.pauses.error and any(role_for(p) == role and self.protection.protected(p['id']) for p in profiles(self.store.config)): continue
                self._disconnect_profile(role)  # Remove orphan pending associations even if home is down.
            self.managed_roles |= owned
            self.configured_this_boot = True
        by_mac={d['mac']:d for d in devices}
        if hasattr(self.net, 'address_fallback'): self.net.address_fallback = config.get('auto_policy', {}).get('address_fallback', True) is not False
        home,ap=self._management(devices,config,now)
        entries = profiles(config)
        self.refresh_auth_failures(entries)
        desired = {role_for(p): p for p in entries if p["enabled"] and role_allows(config, p["mac"], "telescope")}
        stale = self.managed_roles - desired.keys()
        changed = {r for r, p in desired.items() if r in self.applied and network_intent(self.applied[r]) != network_intent(p)}
        victim = self.operations.victim_id
        yielding = role_for({'id': victim}) if victim else None
        if stale or changed:
            self.firewall()
            for role in stale | changed:
                if self.pauses.error and any(role_for(p) == role and self.protection.protected(p['id']) for p in profiles(self.store.config)): continue
                self._disconnect_profile(role)
                self.applied.pop(role, None)
                self.attempts.pop(role, None)
                # A profile set aside for the time of one operation keeps its retry pace.
                if role != yielding: self.retries.pop(role, None)
        obsolete = self.managed_roles - {role_for(p) for p in entries}
        with self.lock:
            pending = (self.pending_deletions | obsolete) - desired.keys()
            self.pending_deletions -= pending
        for role in pending:
            self.net.remove_profile(role)
        self.managed_roles = set(desired)
        with self.lock:
            # A release note explains a disabled profile only.
            self.release_notes.keep({p['id'] for p in entries if not p['enabled']})
            self.standby &= {p['id'] for p in profiles(self.store.config) if p['enabled']}
            kept = {role_for(p) for p in profiles(self.store.config) if p['enabled']}
            self.retries = {role: count for role, count in self.retries.items() if role in kept}
            self.link_stages = {role: stage for role, stage in self.link_stages.items() if role in kept}
        topology = (tuple(sorted(by_mac)), tuple((p["device"]["name"],p["address"]) for p in self.management_paths(home,ap)),
                    tuple((p["id"], p["mac"], p["target_ip"], p["ssid"]) for p in entries if p["enabled"]))
        if topology != self.topology:
            self.blocked.clear()
            self.topology = topology
        proxy_owners=dict(self.proxy_owners)
        proxy_kinds={proxy_flag(p['id']):p['id'] for p in relay_plugins()}
        for flag,ident in list(proxy_owners.items()):
            profile=next((p for p in entries if p['id']==ident and p['enabled']),None)
            device=by_mac.get(profile['mac']) if profile else None
            if (not profile or profile['kind']!=proxy_kinds[flag] or not device or self.pauses.held(ident)
                    or network_intent(self.applied.get(role_for(profile)))!=network_intent(profile)
                    or not self.net.connected(role_for(profile),device,profile)):
                proxy_owners[flag]=None

        self.service_readiness.prune(entries)
        rows, relays = [], []
        paths=self.management_paths(home,ap)
        occupied=[ipaddress.ip_interface(p["address"]).network for p in paths]
        if ipaddress.ip_network("192.168.77.0/24") not in occupied:occupied.append(ipaddress.ip_network("192.168.77.0/24"))
        # Reconcile validated/protected associations before new intent, without
        # changing saved order (the explicit force policy still uses that order).
        def priority(profile):
            device=by_mac.get(profile['mac']);role=role_for(profile)
            validated=(network_intent(self.applied.get(role))==network_intent(profile)
                       and device and self.net.connected(role,device,profile))
            return (not (validated and self.protection.protected(profile['id'])),not validated)
        for p in sorted(entries,key=priority):
            role, device = role_for(p), by_mac.get(p["mac"])
            row = {"id": p["id"], "target_ip": p["target_ip"], "client_ip": "", "connected": False,
                   "relay_ready": False, "interface": device["name"] if device else "", "state": "saved", "message": "Saved", "service": empty_service()}
            rows.append(row)
            if self.auth_failures.held(p):
                row.update(state='authentication_failed', reason='WIFI_AUTHENTICATION_FAILED', retry_blocked=True,
                           message='Wi-Fi authentication failed. Check the password. Retries resume after you change it or reboot the bridge.')
                continue
            if self.pauses.held(p['id']):
                row.update(state='paused', message='Connection paused: explicit owner action required.')
            if not p["enabled"]:
                continue
            if not role_allows(config, p["mac"], "telescope"):
                row.update(state="conflict", reason="ADAPTER_ROLE_RESERVED", message="This Wi-Fi adapter is reserved for another role. Change its role in Wi-Fi adapters first.")
                continue
            if not device:
                row.update(state="missing", message="Adapter missing")
                continue
            if ap and device['mac']==ap['mac']:
                row.update(state='conflict',message='Hotspot adapter is reserved for management.')
                continue
            blocked = self.blocked.get(role)
            if blocked:
                row.update(blocked)
                continue
            up = self.net.connected(role, device, p)
            if not up:
                self.service_readiness.disconnected(p['id'])
                with self.lock:
                    collector = getattr(self, 'link_collector', None)
                    if collector: collector.connection(p['id'], False)
                    if self.connection_events.states.get(p["id"], {}).get("associated"):
                        self._event_contexts[p["id"]] = self.connection_events.observe(
                            p, False, False, "waiting", "CONNECTION_LOST", self._event_contexts.get(p["id"]))
            if up:
                self.retries.pop(role, None)
            # No new telescope link attempt once a restart is requested or the service is stopping.
            if not up and paths and not self.halting() and now - self.attempts.get(role, -1000) >= retry_interval(self.retries.get(role, 0)):
                if not self.operations.before_association(p):
                    row.update(state='error'); continue
                self.attempts[role] = now
                self.firewall()
                with self.lock:
                    self.connection_events.phase(p, "connecting", "CONNECTING", self._event_contexts.get(p["id"]))
                try:
                    self.net.connect(role, device, p)
                    self.applied[role] = copy.deepcopy(p)
                    up = self.net.connected(role, device, p)
                    if up: self.retries.pop(role, None)
                    else: self.retries[role] = self.retries.get(role, 0) + 1
                except NetworkError as error:
                    up = False
                    self.retries[role] = self.retries.get(role, 0) + 1
                    rejected = isinstance(error, NetworkAuthenticationError)
                    if rejected: self.auth_failures.hold(p)
                    reason = error.reason if isinstance(error, NetworkIsolationError) else 'WIFI_AUTHENTICATION_FAILED' if rejected else 'ASSOCIATION_FAILED'
                    # The reason keeps its published value; the step that failed is said beside it.
                    stage = getattr(error, 'stage', None)
                    if stage: self.link_stages[role] = stage
                    else: self.link_stages.pop(role, None)
                    self.operations.fail_association(p, reason, stage)
                    with self.lock:
                        self.connection_events.phase(p, "failed", "ASSOCIATION_FAILED" if isinstance(error, NetworkIsolationError) else reason, self._event_contexts.get(p["id"]))
                    if isinstance(error, NetworkIsolationError):
                        row.update(state='error', reason=error.reason, message=str(error))
                        self.blocked[role] = copy.deepcopy(row)
                        continue
                    if rejected:
                        row.update(state='authentication_failed', reason=reason, retry_blocked=True,
                                   message='Wi-Fi authentication failed. Check the password. Retries resume after you change it or reboot the bridge.')
                        continue
            # One reading serves the whole cycle: an instrument that stops giving an
            # address between two readings is waited for, never a failed reconcile.
            address = self.net.address(device) if up else ''
            if up and not address:
                up = False
                self.link_stages[role] = 'ADDRESS_NOT_OBTAINED'
            if up: self.link_stages.pop(role, None)
            if not up:
                failures = self.retries.get(role, 0)
                wait = retry_interval(failures)
                # The radio stays assigned, but an explicit request for another
                # instrument may take it once an attempt has failed (see operations).
                row.update(state="waiting", message="Waiting for the telescope · automatic reconnection",
                           retry={'attempts': failures, 'interval_seconds': wait,
                                  'next_in_seconds': max(0, int(self.attempts.get(role, now - wait) + wait - now))})
                # Which step of joining its Wi-Fi failed last, when the system said so.
                if self.link_stages.get(role): row['failure_stage'] = self.link_stages[role]
                continue
            row.update(connected=True, client_ip=address, state="connected", message="Wi-Fi connected")
            # The instrument gave no address and the bridge took one of its fixed network itself.
            if role in getattr(self.net, 'fallback_roles', ()): row['address_fallback'] = True
            network = ipaddress.IPv4Interface(address)
            target = ipaddress.IPv4Address(p["target_ip"])
            if p['id'] in self.routing_conflicts:
                row.update(state='conflict',subnet=str(network.network),message='Home routing conflicts with this instrument; forwarding is suspended without disconnecting it.')
                continue
            if not p.get('isolated') and any(network.network.overlaps(n) for n in occupied):
                protected=self.protection.protected(p["id"])
                if not protected:self._disconnect_profile(role)
                row.update(connected=bool(protected), client_ip=address if protected else "", state="conflict", message="Subnet already in use. Connect this profile alone or change its network.")
                self.blocked[role] = copy.deepcopy(row)
                continue
            if target not in network.network or target in (network.ip, network.network.network_address, network.network.broadcast_address):
                protected=self.protection.protected(p["id"])
                if not protected:self._disconnect_profile(role)
                row.update(connected=bool(protected), client_ip=address if protected else "", state="invalid_ip", message="Telescope address is outside the subnet. Correct its IP.")
                self.blocked[role] = copy.deepcopy(row)
                continue
            if not self.operations.verify_association(p, device):
                row.update(relay_ready=False, state='error'); continue
            if not p.get('isolated'): occupied.append(network.network)
            route = {"profile_id":p['id'],"kind":p['kind'],"device":device,"target":p['target_ip']}
            for spec in relay_plugins():
                flag=proxy_flag(spec['id']);ports={(spec['relay']['transport'],port) for port in spec['relay']['ports']}
                conflicting=any(owner and owner!=p['id'] and ports & {(plugin(proxy_kinds[other])['relay']['transport'],port) for port in plugin(proxy_kinds[other])['relay']['ports']}
                                for other,owner in proxy_owners.items())
                route[flag]=not p.get('isolated') and p['kind']==spec['id'] and not conflicting and proxy_owners[flag] in (None,p['id'])
            for flag in proxy_owners:
                if route.get(flag):proxy_owners[flag]=p['id']
            if paths and not p.get('isolated'):relays.append(route)
            ready = self.net.probe(p["target_ip"], device) if plugin(p['kind'])['readiness']=='vespera_tcp' else True
            row['service'] = self.service_readiness.check(p, device, address, now,
                vespera=ready if plugin(p['kind'])['readiness']=='vespera_tcp' else None)
            # A mount that may stop its Wi-Fi without a client: remember when it last answered.
            if p['kind'] in WIFI_SLEEP and row['service'].get('state') != 'not_answering':
                self.mount_contact[p['id']] = now
            row.update(relay_ready=bool(ready and paths), state="online" if ready and paths else "waiting_service",
                       message="Forwarding available" if ready else "Wi-Fi connected · waiting for service",
                       subnet=str(network.network),
                       **{proxy_flag(spec['id']):bool(paths and route[proxy_flag(spec['id'])]) for spec in relay_plugins()},
                       native_proxy_reason='DEDICATED_ENDPOINT_ONLY' if p.get('isolated') else 'NATIVE_PROXY_CONFLICT' if plugin(p['kind'])['relay'] and not route[proxy_flag(p['kind'])] else None,
                       management_paths=[{'role':path['role'],'host':str(ipaddress.ip_interface(path['address']).ip),'subnet':str(ipaddress.ip_interface(path['address']).network),'forwarding_ready':bool(ready)} for path in paths])
        self.proxy_owners=proxy_owners
        self.firewall(home=home, telescopes=relays, setup=ap)
        if hasattr(self.net, 'isolated_endpoints'):
            self.net.isolated_endpoints(entries, rows, paths)
        # Adopt an already-owned association only after addressing/forwarding validated.
        # Later password/target/selector changes still compare against this snapshot.
        for profile in entries:
            if any(row['id'] == profile['id'] and row.get('connected') and row.get('subnet')
                   and row['state'] in ('online', 'waiting_service') for row in rows):
                self.applied.setdefault(role_for(profile), copy.deepcopy(profile))
        order={p["id"]:index for index,p in enumerate(entries)}
        rows.sort(key=lambda row:order[row["id"]])
        any_up = any(r["connected"] for r in rows)
        any_ready = any(r["relay_ready"] for r in rows)
        self.update(home_ip=self.net.address(home).split("/")[0] if home else "", telescopes=rows,
                    mode=("online" if any_ready or not desired else "waiting_service" if any_up else "waiting_telescope") if home else "field" if any_ready else "setup" if paths else "no_management",
                    telescope_connected=any_up, relay_ready=any_ready, last_error="",
                    message="Management forwarding is available." if paths else "No confirmed management forwarding path.")

    def loop(self):
        while not self.stop.is_set():
            try:
                with self.net_lock:
                    self.cycle()
            except NetworkError as error:
                self.update(last_error=str(error), relay_ready=False)
                # An instrument error must never commandeer a working home radio.
                if not self.state["home_ip"]:
                    try:
                        with self.net_lock:
                            self.setup(self.net.devices(), str(error))
                    except NetworkError:
                        self.update(mode="error", message="Could not start the hotspot. Check its driver and service logs.")
                else:
                    self.update(mode="error", message="Network error. Check the current management paths.")
            except Exception as error:
                # Where it happened, never the message: it may hold a network name.
                frame = traceback.extract_tb(error.__traceback__)[-1:]
                where = ' at ' + Path(frame[0].filename).name + ':' + str(frame[0].lineno) if frame else ''
                print("wifi-bridge: internal error " + type(error).__name__ + where, flush=True)
                self.update(mode="error", relay_ready=False, message="Internal error. Check the service log.")
            self.stop.wait(5)
