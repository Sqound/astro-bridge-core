"""Pure allowlisted native catalog and endpoint projections; never initiate I/O."""
import math
import re
from .endpoints import inventory, SERVICE_STATES
from .plugins import plugin


def lease_summary(held, mine, now):
    """expires_in keeps its 1.0 bound; a night reservation adds its full horizon."""
    if not held: return {'held': False, 'mine': False, 'expires_in': None, 'reservation_expires_in': None}
    chosen = max(mine or held, key=lambda v: v['expires'])
    remaining = max(0, math.ceil(chosen['expires'] - now))
    return {'held': True, 'mine': bool(mine), 'expires_in': min(remaining, 3600),
            'reservation_expires_in': remaining if chosen.get('reservation') else None}


def catalog(snapshot, leases=(), client=None, *, now=0, observations=(), observation_age=None, events=(), sequences=None, telemetry=None, maker_keys=None, identities=None, free_radios=0, operations=None):
    live = {row['id']: row for row in snapshot.get('telescopes', [])}
    result = []
    for profile in snapshot['config']['telescopes']:
        ident = profile['id']; row = live.get(ident, {})
        permission = profile.get('client_connect', True)
        seen = observation_age is not None and 0 <= observation_age < 120 and any(
            item.get('scan_source') == 'fresh' and item.get('ssid') == profile.get('ssid')
            and (not profile.get('bssid') or item.get('bssid') == profile['bssid']) for item in observations)
        operation = snapshot.get('connection_operation') or {}
        connecting = row.get('state') in ('connecting', 'associating') or operation.get('profile_id') == ident and operation.get('state') == 'running'
        link = ('connected' if row.get('connected') else 'connecting' if connecting
                else 'disabled' if not permission else 'available' if seen else 'saved')
        service = (row.get('service') or {}).get('state', 'unknown')
        if service not in SERVICE_STATES: service = 'unknown'
        held = [value for value in leases if value['profile_id'] == ident and value['expires'] > now]
        mine = [value for value in held if value['client'] == client]
        blockers = []
        if not permission: blockers.append('CLIENT_CONNECT_DISABLED')
        if profile.get('owner_locked'): blockers.append('OWNER_LOCKED')
        if row.get('state') == 'paused': blockers.append('CONNECTION_PAUSED')
        if any(value['client'] != client for value in held): blockers.append('LEASED_BY_OTHER_CLIENT')
        reason = row.get('reason')
        if not isinstance(reason, str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,127}', reason): reason = None
        verified, source = identities.project(profile) if identities else (None, 'unverified')
        sequence = (sequences or {}).get(ident, max((event['sequence'] for event in events if event.get('profile_id', event.get('instrument_id')) == ident), default=0))
        result.append({'id': ident, 'kind': {'mount': 'mount', 'telescope': 'smart_telescope'}.get(plugin(profile['kind'])['category'], 'other'),
            'family': profile['kind'], 'name': profile['name'], 'link': link, 'service': service,
            'protocol': plugin(profile['kind']).get('protocol'), 'model': (verified or {}).get('model'),
            'verified_identity': verified, 'identity_source': source,
            # Contract 1.1: how the bridge reaches it, and whether connecting it keeps the others.
            'access': 'direct', 'simultaneous': bool(row.get('connected')) or free_radios > 0,
            # Contract 1.2: read-only instrument operation evidence (protect_running_operations).
            'current_operation': (operations or {}).get(ident),
            'since_seq': sequence, 'reason': reason, 'client_connect': permission, 'blockers': blockers,
            'lease': lease_summary(held, mine, now),
            'capabilities': {'telemetry': list((telemetry or {}).get(ident, ())), 'maker_key': (maker_keys or {}).get(profile['kind'], 'absent') if plugin(profile['kind']).get('maker_key') else None}})
    return result


def endpoint(snapshot, ident, request_path='other'):
    entry = next((entry for entry in inventory(snapshot) if entry['profile_id'] == ident), None)
    if not entry: return None
    paths = [path for path in entry['paths'] if path['ready']]
    if not paths: return None
    # Prefer the ingress path, then its proxy if available; retain other path alternatives.
    paths.sort(key=lambda p: (p['path'] != request_path, p['path'] != 'home', p['requires_client_route']))
    first = paths[0]
    selected = [p for p in paths if p['requires_client_route'] == first['requires_client_route']]
    addresses = []
    for role in dict.fromkeys(p['path'] for p in selected):
        same = [p for p in selected if p['path'] == role]
        address = {'host': same[0]['host'], 'ports': [{'port': p['port'], 'transport': p['transport'], 'service': p['service'],
                                                    'role': p['role'], 'protocol': p['protocol']} for p in same]}
        if address not in addresses: addresses.append(address)
    profile = next(p for p in snapshot['config']['telescopes'] if p['id'] == ident)
    return {'instrument_id': ident, 'request_path': request_path,
            'mode': 'routed' if first['requires_client_route'] else 'relay' if profile.get('isolated') else 'proxy',
            'primary': addresses[0], 'alternatives': addresses[1:],
            'requires_client_route': first['requires_client_route'], 'target_ip': entry['target']['ip']}
