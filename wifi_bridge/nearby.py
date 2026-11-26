"""Instruments heard by the bridge's Wi-Fi, offered to paired apps before any setup.

Paired apps list recognized devices in range and link one in a single request.
Linking creates a disabled profile from the bridge's own observation only: the
recognized plugin type, the observed network, the plugin's factory address and
any free adapter allowed for instruments. It never changes bridge settings, its
Wi-Fi, an existing profile created another way, or an address chosen by the app.
A protected network takes the password given in the request, or the plugin's
documented factory password. The password is stored like any profile password
and never logged or returned.
"""
import copy
import json
import re
import uuid

from .api import ApiError
from .config import Invalid, atomic_json, profiles, reserved_hotspot_mac
from .plugins import onboarding_defaults, plugin, ssid_plugin
from .radio_roles import role_allows

IN_RANGE_SECONDS = 120   # Heard by the last scans.
RECENT_SECONDS = 600     # Last heard within ten minutes, e.g. while the free radio is busy.
OBSERVATION = re.compile('[a-f0-9]{32}')


def protected(security):
    return bool(security) and security.strip() not in ('', '--')


class NearbyDevices:
    def __init__(self, api):
        self.api, self.c = api, api.controller
        self.path = self.c.store.path.parent / 'nearby-links.json'

    def _links(self):
        try:
            data = json.loads(self.path.read_text())
            if set(data) != {'format', 'profiles'} or data['format'] != 1 or not isinstance(data['profiles'], list):
                return []
            return [p for p in data['profiles'] if isinstance(p, str)][:64]
        except (OSError, ValueError):
            return []

    def _save_links(self, links):
        atomic_json(self.path, {'format': 1, 'profiles': links[-64:]})

    def free_radios(self, config, state):
        """Adapters allowed for instruments, other than the private network and hotspot ones."""
        busy = {config.get('home', {}).get('mac'), state.get('hotspot', {}).get('mac'), reserved_hotspot_mac(config)}
        return [d for d in state.get('devices', []) if not d.get('missing') and d['mac'] not in busy
                and role_allows(config, d['mac'], 'telescope')]

    def _rows(self):
        discovery = self.c.candidates
        now = int(discovery.wall())
        rows = {}
        for ident, entry in copy.deepcopy(discovery.observations).items():
            spec = ssid_plugin(entry['ssid'])
            if spec['id'] == 'generic' or not spec['relay']:
                continue
            # A clock stepped backwards must not make an observation look older than it is.
            age = max(0, now - entry['observed_at'])
            if age > RECENT_SECONDS:
                continue
            previous = rows.get(entry['ssid'])
            if previous is None or (age, -entry['signal']) < (previous[1], -previous[0]['signal']):
                rows[entry['ssid']] = (dict(entry, observation_id=ident), age, spec)
        return rows.values()

    def list(self):
        c = self.c
        with c.lock:
            config, state = copy.deepcopy(c.store.config), c.status()
            saved = profiles(config)
            links, devices = [], []
            for entry, age, spec in sorted(self._rows(), key=lambda row: (row[1], -row[0]['signal'])):
                defaults = onboarding_defaults(entry['ssid'])
                linked = next((p for p in saved if p['ssid'] == entry['ssid'] and (not p.get('bssid') or p['bssid'] == entry['bssid'])), None)
                password = 'none' if not protected(entry['security']) else 'factory_default' if defaults.get('password') else 'required'
                reason = None if linked or defaults.get('target_ip') else 'ADDRESS_UNKNOWN'
                devices.append({
                    'nearby_id': entry['observation_id'], 'kind': spec['id'], 'category': spec['category'],
                    'name': spec['name'], 'label': spec['name'] + ' · ' + entry['ssid'], 'network_name': entry['ssid'],
                    'signal': entry['signal'], 'band': '5' if entry['frequency'] >= 4900 else '2.4',
                    'last_seen_seconds': age, 'state': 'linked' if linked else 'in_range' if age <= IN_RANGE_SECONDS else 'recently_seen',
                    'instrument_id': linked['id'] if linked else None, 'password': password,
                    'linkable': linked is None and reason is None, 'reason': reason})
            radios = self.free_radios(config, state)
            busy = {p['mac'] for p in saved if p.get('enabled')}
            discovery = c.candidates
            scan_age = max(0, int(discovery.clock() - discovery.nearby_at)) if discovery.nearby_at is not None else None
            return {'devices': devices, 'free_radios': len(radios), 'free_radios_idle': sum(d['mac'] not in busy for d in radios),
                    'rule': 'ONE_DEVICE_PER_FREE_RADIO', 'scan_age_seconds': scan_age, 'scan_interval_seconds': 45,
                    'api_version': '1'}

    def link(self, ident, data, client):
        """Create (or refresh) the profile of a recognized device heard by the bridge."""
        if not isinstance(ident, str) or not OBSERVATION.fullmatch(ident):
            raise ApiError(404, 'NEARBY_DEVICE_NOT_FOUND', 'This device is no longer in range. List nearby devices again.')
        if not isinstance(data, dict) or set(data) - {'password', 'name'}:
            raise ApiError(400, 'INVALID_FIELDS', 'Only password and name may be given.')
        password, name = data.get('password'), data.get('name')
        if password is not None and (not isinstance(password, str) or not 8 <= len(password.encode()) <= 63):
            raise ApiError(400, 'INVALID_PASSWORD', 'The Wi-Fi password must contain 8 to 63 bytes.')
        if name is not None and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 64):
            raise ApiError(400, 'INVALID_NAME', 'The name must contain 1 to 64 characters.')
        c = self.c
        with c.lock:
            row = next((item for item in self.list()['devices'] if item['nearby_id'] == ident), None)
            if row is None:
                raise ApiError(404, 'NEARBY_DEVICE_NOT_FOUND', 'This device is no longer in range. List nearby devices again.')
            entry = c.candidates.observations[ident]
            links = self._links()
            existing = next((p for p in profiles(c.store.config) if p['id'] == row['instrument_id']), None)
            if existing is not None and existing['id'] not in links:
                # Created by the owner: never changed through this route.
                raise ApiError(409, 'ALREADY_CONFIGURED', 'This device already has a profile on the bridge.')
            if row['reason']:
                raise ApiError(409, 'ADDRESS_UNKNOWN', 'The bridge does not know the address of this device type.')
            defaults = onboarding_defaults(entry['ssid'])
            network = {'open': True} if not protected(entry['security']) else None
            if network is None:
                secret = password or defaults.get('password')
                if not secret:
                    raise ApiError(400, 'PASSWORD_REQUIRED', "Give the device's Wi-Fi password.")
                network = {'password': secret}
            elif password is not None:
                raise ApiError(400, 'INVALID_FIELDS', 'This network is open: give no password.')
            config, state = c.store.config, c.status()
            radios = self.free_radios(config, state)
            if not radios:
                raise ApiError(409, 'NO_FREE_RADIO', 'No Wi-Fi adapter is free for an instrument. One free adapter links one device at a time.')
            busy = {p['mac'] for p in profiles(config) if p.get('enabled')}
            radio = (existing or {}).get('mac') if existing and any(d['mac'] == existing.get('mac') for d in radios) else \
                next((d['mac'] for d in radios if d['mac'] not in busy), radios[0]['mac'])
            fields = dict(network, name=(name or row['name']).strip(), kind=row['kind'], ssid=entry['ssid'], bssid=entry['bssid'],
                          mac=radio, target_ip=defaults['target_ip'])
            ident_profile = existing['id'] if existing else 'scope-' + uuid.uuid4().hex[:12]
            try:
                if existing is not None:
                    self.api.update('telescopes', fields, ident_profile)
                else:
                    self.api.update('telescopes', dict(fields, id=ident_profile, enabled=False), create=True)
            except Invalid as error:
                raise ApiError(400, 'VALIDATION_ERROR', str(error)) from None
            if ident_profile not in links:
                self._save_links(links + [ident_profile])
            return {'instrument_id': ident_profile, 'nearby_id': ident, 'created': existing is None, 'api_version': '1'}

    def unlink(self, ident):
        c = self.c
        with c.lock:
            links = self._links()
            profile = next((p for p in profiles(c.store.config) if p['id'] == ident), None)
            if profile is None or ident not in links:
                raise ApiError(404, 'LINK_NOT_FOUND', 'Only a device linked from the nearby list can be unlinked here.')
            live = next((row for row in c.state['telescopes'] if row['id'] == ident), {})
            if profile.get('enabled') or live.get('connected') or any(v['profile_id'] == ident for v in c.protection.leases.values()):
                raise ApiError(409, 'INSTRUMENT_IN_USE', 'Disconnect this instrument before unlinking it.')
            value = copy.deepcopy(c.store.config)
            value['telescopes'] = [p for p in profiles(value) if p['id'] != ident]
            c.save(value, cause='API', expected=None, revision=self.api.revision)
            self._save_links([p for p in links if p != ident])
            return {'unlinked': True, 'instrument_id': ident, 'api_version': '1'}
