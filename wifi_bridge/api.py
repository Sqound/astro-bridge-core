"""Versioned native-client resources; reuse the appliance controller and validation."""
import copy
import hmac
import secrets
import json
from pathlib import Path
import uuid

from . import __version__
from .config import profiles, reserved_hotspot_mac


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


class MobileAPI:
    PREFIX = '/api/v1'
    NETWORK_FIELDS = {'ssid', 'mac', 'password', 'bssid', 'hidden', 'open'}
    TELESCOPE_FIELDS = NETWORK_FIELDS | {'name', 'kind', 'target_ip', 'enabled', 'auto_eligible', 'auto_replaceable'}

    def __init__(self, controller):
        self.controller = controller
        self.revision_key = secrets.token_bytes(32)

    def revision(self):
        # A per-process keyed revision detects secret changes without turning
        # revisions into offline password-guessing oracles. Restarts revoke tokens
        # and require clients to reload both configuration and revision.
        with self.controller.lock:
            raw = json.dumps(self.controller.store.config, sort_keys=True, separators=(',', ':')).encode()
            return hmac.new(self.revision_key, raw, 'sha256').hexdigest()

    def check_revision(self, expected):
        if expected is not None and expected not in ('*', '"' + self.revision() + '"'):
            raise ApiError(412, 'PRECONDITION_FAILED', 'Configuration changed. Reload it before editing.')

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
        return dict(saved, connection=live or {'id': ident, 'state': 'pending', 'connected': False,
                                             'relay_ready': False, 'client_ip': '', 'target_ip': saved['target_ip']})

    def read(self, resource, ident=None, client=None):
        with self.controller.lock:
            state = self.controller.status()
            revision = self.revision()
            if resource == 'status':
                data = dict(state, connection_protection=self.controller.protection.status(client))
            elif resource == 'config':
                data = {'config': state['config']}
            elif resource == 'plugins':
                from .plugins import public_registry
                data = public_registry()
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
                data = {'settings': {k: state['config'][k] for k in ('hostname', 'country', 'auto_connect', 'auto_policy', 'connection_mode', 'hotspot_private_lan')},
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
                    device['can_assign_telescope'] = device['mac'] != state['config']['home'].get('mac') and not any(a['role']=='hotspot' for a in assignments)
                    device['available_for_telescope'] = device['can_assign_telescope'] and not any(a['role'] == 'telescope' for a in assignments)
                data = {'interfaces': devices}
            else:
                raise ApiError(404, 'NOT_FOUND', 'Unknown API resource.')
            return dict(data, api_version='1', config_revision=revision)

    def update(self, resource, data, ident=None, create=False, expected=None):
        c = self.controller
        with c.lock:
            if resource == 'settings' and isinstance(data,dict) and {'connection_mode','hotspot_private_lan'} & data.keys():
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
                            other['enabled'] = False
            elif resource == 'home':
                self.fields(data, self.NETWORK_FIELDS)
                value['home'] = self.merge_network(value['home'], data)
            elif resource == 'hotspot':
                self.fields(data, {'enabled', 'ssid', 'mac'})
                value['hotspot'] = dict(value.get('hotspot', {}), **data)
            elif resource == 'settings':
                self.fields(data, {'hostname', 'country', 'auto_connect', 'auto_policy', 'acknowledge_interruption', 'connection_mode', 'hotspot_private_lan'})
                if 'acknowledge_interruption' in data and 'auto_policy' not in data:raise ApiError(400,'INVALID_FIELDS','Acknowledgement requires an automatic policy request.')
                value.update(data)
            else:
                raise ApiError(404, 'NOT_FOUND', 'Unknown API resource.')
            accepted = c.save(value, cause="API", expected=expected, revision=self.revision)
            # Reads are redacted; no request passwords are echoed to the client.
            return dict(self.read(resource, ident), operation_id=accepted.get("operation_id"))

    def action(self, ident, action, expected=None, data=None):
        with self.controller.lock:
            data = data or {}
            self.fields(data, {'mac', 'expected_mac'} if action == 'connect' else set())
            self.check_revision(expected)
            self.telescope(ident)
            accepted = self.controller.telescope_action(dict(data, id=ident, action=action), cause="API", expected=expected, revision=self.revision)
            return {'ok': True, 'api_version': '1', 'config_revision': self.revision(),
                    'status_url': self.PREFIX + '/telescopes/' + ident, 'operation_id': accepted['operation_id']}

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
    def info():
        return {'product': 'astro-bridge', 'version': __version__, 'api_version': '1',
                'authentication': 'bearer', 'token_expires_in': 43200,
                'capabilities': ['telescope_profiles', 'radio_switching', 'home_wifi', 'wifi_scan',
                                 'interface_inventory', 'hotspot', 'settings', 'passwords', 'reboot', 'connection_events', 'safe_discovery', 'physical_binding', 'profile_ensure', 'connection_operations', 'explicit_auto_policy', 'connection_activity', 'client_reservations', 'owner_locks', 'quiet_replacement', 'advanced_interruption_policy', 'seestar_demand', 'vespera_connection_demand', 'dwarf_discovery_demand', 'dwarf_native_proxy', 'dwarf_passive_demand', 'durable_connection_pauses', 'field_management_paths', 'hotspot_internet_sharing', 'consultation_metadata', 'wifi_mount_profiles', 'zwo_mount_native_proxy', 'synscan_udp_proxy', 'declarative_plugins', 'mount_read_only_identification', 'zwo_mount_connection_demand', 'synscan_discovery_demand'],
                'openapi_url': MobileAPI.PREFIX + '/openapi.json'}

    @staticmethod
    def specification():
        return json.loads((Path(__file__).parent / 'static' / 'openapi.json').read_text())
