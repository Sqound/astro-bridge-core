"""Versioned native-client resources; reuse the appliance controller and validation."""
import copy
import hmac
import secrets
import json
import re
from pathlib import Path
import uuid

from . import __version__
from .config import profiles, reserved_hotspot_mac
from .readiness import empty_service
from .isolated import KINDS as ISOLATED_TRANSPORTS
from .radio_roles import interface_roles, role_allows


class ApiError(Exception):
    def __init__(self, status, code, message, retry_after=None):
        self.status, self.code, self.message = status, code, message
        # Seconds before a retry can succeed; 429 replies always carry Retry-After.
        self.retry_after = retry_after if retry_after is not None else 60 if status == 429 else None


class MobileAPI:
    PREFIX = '/api/v1'
    NETWORK_FIELDS = {'ssid', 'mac', 'password', 'bssid', 'hidden', 'open'}
    TELESCOPE_FIELDS = NETWORK_FIELDS | {'name', 'kind', 'target_ip', 'enabled', 'auto_eligible', 'auto_replaceable', 'client_connect', 'isolated', 'client_port'}

    def __init__(self, controller):
        self.controller = controller
        from .native_instruments import NativeInstruments
        self.instruments = NativeInstruments(self)
        from .telemetry.api import TelemetryAPI
        # Telemetry includes bounded, normalized card receipts and history.
        self.telemetry = TelemetryAPI(self)
        from .cameras.api import CameraAPI
        self.cameras = CameraAPI(self)
        from .nearby import NearbyDevices
        self.nearby = NearbyDevices(self)
        from .maker_keys.api import MakerKeyAPI
        self.maker_keys = MakerKeyAPI(self)
        self.revision_key = secrets.token_bytes(32)
        from .metrics import SystemMetrics
        self.metrics = SystemMetrics(self.relay_metrics)

    def relay_metrics(self):
        # Snapshot the registry under the controller lock; never expose target or owner.
        with self.controller.lock:
            manager = getattr(self.controller.net, 'isolated', None)
            relays = list(manager.relays.items()) if manager else []
            discovery = getattr(self.controller, 'discovery_metrics', None)
            scans = list(discovery.metrics.items()) if discovery else []
        import ipaddress
        return [dict(profile_id=key[0], host=key[1], port=key[2], transport=key[3], service='instrument',
                     **relay.metrics.snapshot()) for key, relay in relays[:48]] + [
            dict(profile_id=None, host=str(ipaddress.ip_interface(plan[1]).ip), port=4720,
                 transport='udp', service='seestar_discovery', **count.snapshot()) for plan, count in scans[:2]]

    def system_metrics(self):
        return self.metrics.read()

    def indi_status(self, local_address=None):
        from .cameras.indi import PORT
        service = getattr(self.controller, 'camera_service', None)
        return service.indi_status(local_address) if service else {'port': PORT, 'state': 'off'}

    def camera_inventory(self, local_address=None):
        """Expose driver/discovery state and the collision-safe native identity."""
        with self.controller.lock:
            return self.controller.camera_native.inventory(local_address)

    def revision(self):
        # A per-process keyed revision detects secret changes without turning
        # revisions into offline password-guessing oracles. Restarts revoke tokens
        # and require clients to reload both configuration and revision.
        with self.controller.lock:
            value = {'config': self.controller.store.config, 'clients_sequence': getattr(getattr(self.controller, 'paired_clients', None), 'state', {}).get('sequence', 0)}
            # Includes owner selections, conditional Seestar defaults and optional Alpaca cadence.
            value['telemetry_settings'] = self.telemetry.settings()
            value['camera_settings'] = self.cameras.settings()
            # Revisions depend only on a monotonic metadata sequence, never key bytes.
            value['maker_keys_sequence'] = self.controller.maker_keys.store.record['sequence']
            raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
            return hmac.new(self.revision_key, raw, 'sha256').hexdigest()

    def check_revision(self, expected):
        if expected is not None and expected not in ('*', '"' + self.revision() + '"'):
            raise ApiError(412, 'PRECONDITION_FAILED', 'Configuration changed. Reload it before editing.')

    def system_time(self, data=None, client=None):
        clock = self.controller.event_store.clock
        return clock.read() if data is None else clock.hint(data, client)

    @staticmethod
    def fields(data, allowed):
        if not isinstance(data, dict) or set(data) - allowed:
            raise ApiError(400, 'INVALID_FIELDS', 'Unknown or read-only fields in request.')

    @staticmethod
    def merge_network(previous, patch):
        value = dict(previous, **patch)
        # An omitted password may only retain credentials for an unchanged SSID.
        if patch.get('ssid', previous.get('ssid')) != previous.get('ssid') and 'password' not in patch:
            value['password'] = ''
        return value

    def telescope(self, ident, state=None):
        state = state or self.controller.status()
        saved = next((p for p in state['config']['telescopes'] if p['id'] == ident), None)
        if saved is None:
            raise ApiError(404, 'TELESCOPE_NOT_FOUND', 'Unknown telescope profile.')
        live = next((p for p in state['telescopes'] if p['id'] == ident), None)
        return dict(saved, client_endpoint=(live or {}).get('client_endpoint'), connection=live or {'id': ident, 'state': 'pending', 'connected': False,
                                             'relay_ready': False, 'client_ip': '', 'target_ip': saved['target_ip'], 'service': empty_service()})

    def read(self, resource, ident=None, client=None):
        with self.controller.lock:
            state = self.controller.status()
            revision = self.revision()
            if resource == 'status':
                data = dict(state, connection_protection=self.controller.protection.status(client))
            elif resource == 'config':
                data = {'config': state['config']}
            elif resource == 'plugins':
                # Observation/key descriptors are public capabilities, never credentials or activation.
                from .plugins import public_registry
                data = public_registry()
            elif resource == 'endpoints':
                from .endpoints import inventory
                # Inventory allowlists shared UDP ownership and refusal counts per path.
                entries = inventory(state)
                if ident is not None:
                    entries = [entry for entry in entries if entry['profile_id'] == ident]
                    if not entries:
                        raise ApiError(404, 'TELESCOPE_NOT_FOUND', 'Unknown telescope profile.')
                data = {'boot_id': self.controller.event_store.boot_id, 'endpoints': entries}
            elif resource == 'stations':
                rows = {row['id']: row for row in self.controller.stations.public()}
                if ident is not None and ident not in rows:
                    raise ApiError(404, 'TELESCOPE_NOT_FOUND', 'Unknown telescope profile.')
                data = {'station': rows[ident]} if ident else {'stations': list(rows.values())}
            elif resource == 'serial-mounts':
                rows = {row['id']: row for row in state['serial_mounts']}
                if ident is not None and ident not in rows:
                    raise ApiError(404, 'TELESCOPE_NOT_FOUND', 'Unknown telescope profile.')
                data = {'serial_mount': rows[ident]} if ident else {'serial_mounts': list(rows.values())}
            elif resource == 'serial-devices':
                data = {'serial_devices': state['serial_detected']}
            elif resource == 'serial-profiles':
                from .serial_profile import load_profiles, public_profile
                data = {'serial_profiles': [public_profile(p) for p in load_profiles().values()]}
            elif resource == 'telescopes':
                data = {'telescope': self.telescope(ident, state)} if ident else {
                    'telescopes': [self.telescope(p['id'], state) for p in state['config']['telescopes']]}
            elif resource == 'home':
                data = {'home': dict(state['config']['home'], connection={
                    'connected': bool(state['home_ip']), 'client_ip': state['home_ip']})}
            elif resource == 'hotspot':
                data = {'hotspot': dict(state['config']['hotspot'], connection=state['hotspot'],
                                        ssid=state['setup_ssid'], configured_ssid=state['config']['hotspot'].get('ssid', ''))}
            elif resource == 'settings':
                data = {'settings': {k: state['config'][k] for k in ('hostname', 'country', 'auto_connect', 'auto_policy', 'connection_mode', 'hotspot_private_lan', 'interface_roles')},
                        'default_credentials': state['default_credentials'], 'network_access':state['network_access']}
            elif resource == 'interfaces':
                devices = copy.deepcopy(state['devices'])
                for device in devices:
                    assignments = []
                    if device['mac'] == state['config']['home'].get('mac'):
                        assignments.append({'role': 'home'})
                    assignments += [{'role': 'telescope', 'id': p['id']} for p in state['config']['telescopes']
                                    if p['enabled'] and p['mac'] == device['mac']]
                    if (state['hotspot'].get('active') and state['hotspot'].get('mac') == device['mac']
                            or reserved_hotspot_mac(state['config']) == device['mac']):
                        assignments.append({'role': 'hotspot'})
                    device['assignments'] = assignments
                    device['configured_role'] = interface_roles(state['config']).get(device['mac'], 'auto')
                    device['can_assign_telescope'] = role_allows(state['config'], device['mac'], 'telescope') and device['mac'] != state['config']['home'].get('mac') and not any(a['role']=='hotspot' for a in assignments)
                    device['available_for_telescope'] = device['can_assign_telescope'] and not any(a['role'] == 'telescope' for a in assignments)
                data = {'interfaces': devices}
            else:
                raise ApiError(404, 'NOT_FOUND', 'Unknown API resource.')
            return dict(data, api_version='1', config_revision=revision)

    def update(self, resource, data, ident=None, create=False, expected=None):
        c = self.controller
        with c.lock:
            if resource == 'settings' and isinstance(data,dict) and {'connection_mode','hotspot_private_lan','interface_roles'} & data.keys():
                c.protection.precondition(expected,self.revision)
            self.check_revision(expected)
            value = copy.deepcopy(c.store.config)
            value['telescopes'] = copy.deepcopy(profiles(value))
            if resource == 'telescopes':
                self.fields(data, self.TELESCOPE_FIELDS | ({'id'} if create else set()))
                if create:
                    ident = data.get('id', 'scope-' + uuid.uuid4().hex[:12])
                    if any(p['id'] == ident for p in value['telescopes']):
                        raise ApiError(409, 'TELESCOPE_EXISTS', 'This telescope ID already exists.')
                    entry = dict(data, id=ident)
                    entry.setdefault('kind', 'generic')
                    entry.setdefault('enabled', False)
                    value['telescopes'].append(entry)
                else:
                    entry = next((p for p in value['telescopes'] if p['id'] == ident), None)
                    if entry is None:
                        raise ApiError(404, 'TELESCOPE_NOT_FOUND', 'Unknown telescope profile.')
                    merged = self.merge_network(entry, data)
                    entry.clear(); entry.update(merged)
                if entry.get('enabled') is True:
                    for other in value['telescopes']:
                        if other['id'] != ident and other.get('mac') == entry.get('mac'):
                            if entry.get('isolated') and other.get('enabled'):
                                raise ApiError(409, 'INSUFFICIENT_RADIOS', 'Simultaneous mounts require separate free Wi-Fi adapters. Choose another adapter or disconnect a mount explicitly.')
                            other['enabled'] = False
            elif resource == 'stations':
                # Station instruments join the bridge network: no radio, SSID or password.
                value['stations'] = copy.deepcopy(value.get('stations', []))
                if create:
                    self.fields(data, {'id', 'name', 'kind', 'target_ip', 'client_connect'})
                    ident = data.get('id', 'station-' + uuid.uuid4().hex[:12])
                    if any(p['id'] == ident for p in value['telescopes'] + value['stations']):
                        raise ApiError(409, 'TELESCOPE_EXISTS', 'This telescope ID already exists.')
                    value['stations'].append(dict(data, id=ident))
                else:
                    entry = next((p for p in value['stations'] if p['id'] == ident), None)
                    if entry is None: raise ApiError(404, 'TELESCOPE_NOT_FOUND', 'Unknown telescope profile.')
                    if data is None:
                        if any(v['profile_id'] == ident for v in c.protection.leases.values()):
                            raise ApiError(409, 'CONNECTION_PROTECTED', 'Release reservations and explicitly remove the owner lock before changing this connection.')
                        value['stations'].remove(entry)
                    else:
                        self.fields(data, {'name', 'kind', 'target_ip', 'client_connect'})
                        entry.update(data)
            elif resource == 'serial-mounts':
                # One code path for the bridge interface and for apps: same validation, same rights.
                value['serial_mounts'] = copy.deepcopy(value.get('serial_mounts', []))
                from .serial_mounts import FIELDS as SERIAL_FIELDS
                if create:
                    self.fields(data, SERIAL_FIELDS)
                    entry = c.serial_mounts.complete(data)
                    if any(p['id'] == entry['id'] for p in value['telescopes'] + value.get('stations', []) + value['serial_mounts']):
                        raise ApiError(409, 'TELESCOPE_EXISTS', 'This telescope ID already exists.')
                    value['serial_mounts'].append(entry)
                    ident = entry['id']
                else:
                    entry = next((p for p in value['serial_mounts'] if p['id'] == ident), None)
                    if entry is None: raise ApiError(404, 'TELESCOPE_NOT_FOUND', 'Unknown telescope profile.')
                    held = c.serial_mounts.link_held(ident)
                    if data is None:
                        # A mount whose client holds the link is never removed: disconnect first.
                        if held: raise ApiError(409, 'LINK_HELD', 'A client holds this mount. Disconnect it before removing the mount.')
                        value['serial_mounts'].remove(entry)
                    else:
                        self.fields(data, {'name', 'tcp_port', 'baud', 'format', 'idle_timeout_seconds'})
                        if held and set(data) - {'name'}:
                            raise ApiError(409, 'LINK_HELD', 'A client holds this mount. Only its name can change while it is connected.')
                        entry.update(data)
            elif resource == 'home':
                self.fields(data, self.NETWORK_FIELDS)
                value['home'] = self.merge_network(value['home'], data)
            elif resource == 'hotspot':
                self.fields(data, {'enabled', 'ssid', 'mac', 'current_password'})
                # A new name after a personal password needs it to derive the hotspot key.
                secret = data.pop('current_password', None) if isinstance(data, dict) else None
                value['hotspot'] = dict(value.get('hotspot', {}), **data)
                if secret is not None: value['current_password'] = secret
            elif resource == 'settings':
                # auto_policy includes opt-in protect_running_operations; save enforces
                # boolean validation, acknowledgement and the actual revision.
                self.fields(data, {'hostname', 'country', 'auto_connect', 'auto_policy', 'acknowledge_interruption', 'connection_mode', 'hotspot_private_lan', 'interface_roles'})
                if 'acknowledge_interruption' in data and 'auto_policy' not in data:raise ApiError(400,'INVALID_FIELDS','Acknowledgement requires an automatic policy request.')
                value.update(data)
            else:
                raise ApiError(404, 'NOT_FOUND', 'Unknown API resource.')
            accepted = c.save(value, cause="API", expected=expected, revision=self.revision)
            if resource in ('stations', 'serial-mounts') and data is None: return {'deleted': True, 'id': ident, 'api_version': '1'}
            # Reads are redacted; no request passwords are echoed to the client.
            return dict(self.read(resource, ident), operation_id=accepted.get("operation_id"))

    def devices(self):
        """Every declared device with its link nature, and the detected ones. Read-only."""
        from .devices import declared_devices
        state = self.controller.status()
        cameras = []
        if getattr(self.controller, 'camera_supported', False):
            try: cameras = self.controller.camera_native.catalog()
            except Exception: cameras = []
        return {'devices': declared_devices(state, cameras), 'detected': state['serial_detected'],
                'api_version': '1', 'config_revision': self.revision()}

    def serial_identify(self, data, ident=None):
        """Read-only identification, only while no client holds the link."""
        self.fields(data, set() if ident else {'device'})
        c = self.controller
        if ident:
            if c.serial_mounts.find(ident) is None: raise ApiError(404, 'TELESCOPE_NOT_FOUND', 'Unknown telescope profile.')
            try: identity = c.serial_mounts.identify(ident)
            except PermissionError: raise ApiError(409, 'LINK_HELD', 'A client holds this mount. Identification is only read while the link is free.') from None
            return {'identity': identity, 'api_version': '1'}
        found = next((d for d in c.serial_mounts.detected() if d['stable_path'] == data.get('device')), None)
        if found is None: raise ApiError(404, 'DEVICE_NOT_DETECTED', 'This serial adapter is not detected.')
        from .serial_profile import profile
        return {'identity': c.serial_mounts.identify_node(found['node'], profile(found['proposal']['profile'])), 'api_version': '1'}

    def action(self, ident, action, expected=None, data=None):
        with self.controller.lock:
            data = data or {}
            self.fields(data, {'mac', 'expected_mac'} if action == 'connect' else set())
            self.check_revision(expected)
            self.telescope(ident)
            accepted = self.controller.telescope_action(dict(data, id=ident, action=action), cause="API", expected=expected, revision=self.revision)
            return {'ok': True, 'api_version': '1', 'config_revision': self.revision(),
                    'status_url': self.PREFIX + '/telescopes/' + ident, 'operation_id': accepted['operation_id']}

    def release_endpoint_owner(self, ident, data, expected=None):
        self.fields(data, set())
        with self.controller.lock:
            if expected is None or expected in ('*', '"*"'):
                raise ApiError(428, 'PRECONDITION_REQUIRED', 'Provide the actual current configuration revision before releasing the UDP owner.')
            self.check_revision(expected)
            profile = next((p for p in profiles(self.controller.store.config) if p['id'] == ident), None)
            if not profile or not profile.get('isolated'):
                raise ApiError(404, 'ENDPOINT_NOT_FOUND', 'Unknown endpoint.')
            if ISOLATED_TRANSPORTS.get(profile['kind'], (None,))[0] != 'udp':
                raise ApiError(409, 'OWNER_NOT_SUPPORTED', 'This endpoint does not use UDP ownership.')
            manager = getattr(self.controller.net, 'isolated', None)
            ownership = manager.release_owner(ident) if manager else {
                'owner': None, 'refused_clients': 0, 'owner_idle_seconds': 20}
            return dict(ownership, ok=True, id=ident, api_version='1', config_revision=self.revision())

    def binding(self, ident, data=None, expected=None, unlink=False):
        from .candidates import validate_binding
        c = self.controller
        data = {} if data is None else data
        self.fields(data, set() if unlink else {'candidate_id', 'aliases', 'owner_confirmed', 'confirm_replace', 'conflicting_profile_ids', 'append_aliases', 'remove_bssid'})
        if ('candidate_id' in data and set(data) & {'aliases', 'owner_confirmed'}
                or 'confirm_replace' in data and type(data['confirm_replace']) is not bool
                or 'append_aliases' in data and type(data['append_aliases']) is not bool
                or 'remove_bssid' in data and set(data) != {'remove_bssid'}):
            raise ApiError(400, 'INVALID_FIELDS', 'Choose one physical binding source.')
        with c.certificates.config_guard(), c.lock:
            if expected is None or expected in ('*', '"*"'): raise ApiError(428, 'PRECONDITION_REQUIRED', 'Provide the actual current configuration revision before binding.')
            self.check_revision(expected)
            if c.operations.pending or c.discovery_pending or c.certificate_job_pending or c.action_at is not None or c.stop.is_set():
                raise ApiError(409, 'NETWORK_BUSY', 'Network or system maintenance is pending.')
            from .updates import Updates
            if Updates(c.store.path.parent, c.demo).busy(): raise ApiError(409, 'UPDATE_IN_PROGRESS', 'An application update is in progress.')
            value = copy.deepcopy(c.store.config); entries = profiles(value)
            entry = next((p for p in entries if p['id'] == ident), None)
            if entry is None: raise ApiError(404, 'TELESCOPE_NOT_FOUND', 'Unknown telescope profile.')
            c.protection.reject(ident)
            old_binding = validate_binding(entry.get('physical_binding'))
            removing = 'remove_bssid' in data
            if removing:
                if not old_binding or not isinstance(data['remove_bssid'], str) or data['remove_bssid'] not in {a['bssid'] for a in old_binding['aliases']}:
                    raise ApiError(400, 'INVALID_FIELDS', 'Choose an existing physical alias to remove.')
                aliases = [a for a in old_binding['aliases'] if a['bssid'] != data['remove_bssid']]
                binding = dict(old_binding, aliases=aliases) if aliases else None
            else:
                binding = None if unlink else c.candidates.binding(data)
                if binding and old_binding and data.get('append_aliases'):
                    aliases = {a['bssid']: a for a in old_binding['aliases']}
                    for observation in binding['aliases']:
                        if observation['bssid'] not in aliases or observation['source'] == 'wifi-observed':
                            aliases[observation['bssid']] = observation
                    binding = validate_binding(dict(binding, aliases=list(aliases.values())))
            conflicts = sorted(p['id'] for p in entries if p['id'] != ident and binding and
                               {a['bssid'] for a in (p.get('physical_binding') or {}).get('aliases', [])} &
                               {a['bssid'] for a in binding['aliases']})
            claimed = data.get('conflicting_profile_ids', [])
            if (not isinstance(claimed, list) or len(claimed) > 24
                    or any(not isinstance(i, str) for i in claimed)):
                raise ApiError(400, 'INVALID_FIELDS', 'Provide the exact conflicting profile IDs.')
            replacing = bool(old_binding and binding and not {(a['ssid'],a['bssid']) for a in old_binding['aliases']} <= {(a['ssid'],a['bssid']) for a in binding['aliases']})
            if not unlink and not removing and (conflicts or replacing) and (data.get('confirm_replace') is not True or sorted(claimed) != conflicts):
                error = ApiError(409, 'BINDING_CONFLICT', 'Explicitly confirm replacement of this physical binding and conflicting profiles.')
                error.details = {'conflicting_profile_ids': conflicts}; raise error
            for profile in entries:
                if profile['id'] in conflicts or profile['id'] == ident: profile.pop('physical_binding', None)
            if binding is not None: entry['physical_binding'] = validate_binding(binding)
            value['telescopes'] = entries
            c.protection.guard(c.store.config,value)
            c.store.save(value)  # Metadata only: never change mac/bssid/password or connection intent.
            return self.read('telescopes', ident)

    @staticmethod
    def required_scope(method, path):
        """Explicit native permissions; unknown routes always require admin."""
        if (method, path) in (('GET', '/info'), ('GET', '/openapi.json'), ('POST', '/auth/token')):
            return None
        if method == 'DELETE' and path == '/auth/token':
            return 'authenticated'
        if (method, path) in (('GET', '/system/time'), ('POST', '/system/time-hint')):
            return 'read'
        # Explicit camera-owner transfer and demo controls remain administrator-only.
        if method == 'POST' and (path == '/cameras/demo' or re.fullmatch(r'/cameras/[^/]+/release-owner', path)):
            return 'admin'
        if method == 'GET' and path == '/cameras':
            return 'camera'
        # Devices heard by the bridge: listing reads, linking needs the bounded link scope.
        if method == 'GET' and path == '/instruments/nearby':
            return 'read'
        if (method == 'POST' and re.fullmatch(r'/instruments/nearby/[^/]+/link', path)
                or method == 'DELETE' and re.fullmatch(r'/instruments/[^/]+/link', path)):
            return 'link'
        read_paths = ('/status', '/interfaces', '/telescopes', '/plugins', '/events', '/activity',
                      '/protection', '/endpoints', '/instruments', '/devices', '/serial-devices', '/serial-mounts', '/serial-profiles')
        if method == 'GET' and (path in read_paths or re.fullmatch(
                r'/(?:telescopes/[^/]+(?:/binding)?|endpoints/[^/]+|operations/[^/]+|instruments/[^/]+(?:/endpoint)?|serial-mounts/[^/]+)', path)):
            return 'read'
        if method == 'POST' and re.fullmatch(r'/instruments/[^/]+/(?:connect|lease|release|disconnect)', path):
            return 'connect'
        if method == 'GET' and (path in ('/telemetry', '/telemetry/samples', '/telemetry/sessions') or re.fullmatch(r'/telemetry/sessions/[^/]+(?:/(?:export|analysis))?', path)):
            return 'telemetry'
        return 'admin'

    @staticmethod
    def planned_native_route(method, path, data, query):
        """All native v1 routes are implemented."""
        return False

    def info(self, local_address=None, local_port=80):
        """Share live services; the home path includes owned Ethernet and private Wi-Fi."""
        from .announce import enabled_capabilities
        settings, runtime = self.controller.announcement()
        request_path = "other"
        endpoints = []
        with self.controller.lock:
            hostname = self.controller.store.config['hostname']
            for path in self.controller.state.get("management", {}).get("paths", []):
                role = "home" if path["role"] == "home" else "hotspot"
                if path["ip"] == local_address and request_path == "other":
                    request_path = role
                # Contract 1.2: the web interface on each confirmed management path (P6C source).
                port = '' if local_port in (None, 80) else ':' + str(local_port)
                endpoints.append({'url': 'http://' + path['ip'] + port + '/', 'path': role, 'tls': False})
                if runtime.get('https_port'):
                    endpoints.append({'url': 'https://' + hostname + '.local/', 'path': role, 'tls': True})
        return {'product': 'astro-bridge', 'version': __version__, 'api_version': '1',
                'authentication': 'bearer', 'token_expires_in': 43200,
                'contract_version': 1, 'device_id': settings['device_id'], 'name': settings['name'],
                'capabilities': enabled_capabilities(runtime), 'request_path': request_path,
                'pairing': {'available': runtime.get('pairing') is True, 'method': 'admin_password'},
                'legacy_capabilities': ['telescope_profiles', 'radio_switching', 'home_wifi', 'wifi_scan',
                                 'interface_inventory', 'hotspot', 'settings', 'passwords', 'reboot', 'connection_events', 'safe_discovery', 'physical_binding', 'profile_ensure', 'connection_operations', 'explicit_auto_policy', 'connection_activity', 'client_reservations', 'owner_locks', 'quiet_replacement', 'advanced_interruption_policy', 'seestar_demand', 'vespera_connection_demand', 'dwarf_discovery_demand', 'dwarf_native_proxy', 'dwarf_passive_demand', 'durable_connection_pauses', 'field_management_paths', 'hotspot_internet_sharing', 'consultation_metadata', 'wifi_mount_profiles', 'zwo_mount_native_proxy', 'synscan_udp_proxy', 'declarative_plugins', 'mount_read_only_identification', 'zwo_mount_connection_demand', 'synscan_discovery_demand', 'isolated_mount_endpoints', 'udp_endpoint_ownership', 'hotspot_recovery_events', 'pairing'],
                'openapi_url': MobileAPI.PREFIX + '/openapi.json',
                # Contract 1.1 (additive): minor version and pre-connect hints.
                'contract_minor': 2, 'free_radios': runtime.get('free_radios', 0),
                'hotspot': runtime.get('hotspot') is True,
                'tls_fingerprint': runtime.get('tls_fingerprint'),
                'management_endpoints': endpoints[:8]}

    @staticmethod
    def specification():
        return json.loads((Path(__file__).parent / 'static' / 'openapi.json').read_text())
