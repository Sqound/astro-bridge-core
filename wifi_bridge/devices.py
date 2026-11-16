"""One list of every declared device with the nature of its link, plus the
devices that are only detected. Read-only projection for the home page and the
API; it never opens a port or sends a byte."""
from .config import profiles
from .plugins import plugin

LINK_TYPES = ('wifi', 'serial_usb', 'usb')


def _wifi_row(profile, live):
    category = plugin(profile['kind'])['category']
    endpoint = live.get('client_endpoint') or None
    online = bool(live.get('connected')) and (live.get('service') or {}).get('state') != 'not_answering'
    sessions = (live.get('transport_metrics') or {}).get('tcp_established')
    return {'id': profile['id'], 'name': profile['name'], 'family': profile['kind'],
            'kind': {'mount': 'mount', 'telescope': 'smart_telescope'}.get(category, 'other'),
            'link_type': 'wifi', 'access': 'direct', 'state': 'online' if online else 'offline',
            'client_connected': None if sessions is None else sessions > 0,
            'endpoint': {'host': endpoint.get('host'), 'port': endpoint.get('port'), 'transport': endpoint.get('transport')} if endpoint and endpoint.get('port') else None}


def declared_devices(state, cameras=()):
    live = {row['id']: row for row in state.get('telescopes', [])}
    rows = [_wifi_row(p, live.get(p['id'], {})) for p in profiles(state['config'])]
    for station in state.get('stations', []):
        rows.append({'id': station['id'], 'name': station['name'], 'family': station['kind'], 'kind': 'smart_telescope',
                     'link_type': 'wifi', 'access': 'station', 'state': {True: 'online', False: 'offline'}.get(station.get('reachable'), 'unknown'),
                     'client_connected': None, 'endpoint': None})
    for mount in state.get('serial_mounts', []):
        rows.append({'id': mount['id'], 'name': mount['name'], 'family': mount['profile'] + '_serial', 'kind': 'mount',
                     'link_type': 'serial_usb', 'access': 'serial_relay',
                     'state': 'online' if mount.get('adapter_present') else 'offline',
                     'client_connected': bool(mount.get('client')),
                     'endpoint': {'host': None, 'port': mount['tcp_port'], 'transport': 'tcp'},
                     'detail': {'adapter_present': mount.get('adapter_present'), 'identification': mount.get('identification'),
                                'identity_mismatch': mount.get('identity_mismatch')}})
    for camera in cameras:
        rows.append({'id': camera['id'], 'name': camera['name'], 'family': camera['family'], 'kind': 'camera',
                     'link_type': 'usb', 'access': camera.get('access', 'attached'),
                     'state': 'online' if camera.get('link') == 'connected' else 'offline',
                     'client_connected': None, 'endpoint': None})
    return rows
