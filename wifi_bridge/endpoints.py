"""Allowlisted endpoint inventory derived from a controller snapshot; no I/O."""
from .plugins import plugin, port_role, proxy_flag
from .isolated import ASSIGNED as ISOLATED_ASSIGNED


SERVICE_STATES = ('ready', 'not_answering', 'not_probed', 'unknown')


def inventory(snapshot):
    """Return saved profiles and their currently confirmed client paths."""
    live = {row['id']: row for row in snapshot.get('telescopes', [])}
    result = []
    for profile in snapshot['config']['telescopes']:
        kind = profile['kind']
        relay = plugin(kind)['relay']
        ports = list(relay['ports']) if relay else []
        row = live.get(profile['id'], {})
        entry = {'profile_id': profile['id'], 'kind': kind,
                 'target': {'ip': profile['target_ip'], 'ports': ports}, 'paths': []}
        result.append(entry)
        if (not profile.get('enabled') or not row.get('connected')
                or row.get('target_ip', profile['target_ip']) != profile['target_ip']):
            continue
        state = (row.get('service') or {}).get('state', 'unknown')
        if state not in SERVICE_STATES: state = 'unknown'

        def add(role, host, port, transport, service, ready, route=False, owner=None, udp=False, refused_clients=0, purpose=None):
            purpose, protocol = purpose or port_role(kind, port)
            path = {'path': 'hotspot' if role == 'ap' else 'home', 'host': host,
                    'port': port, 'transport': transport, 'service': service,
                    'role': purpose, 'protocol': protocol,
                    'ready': bool(ready), 'service_state': state, 'requires_client_route': route}
            if udp:
                path['owner'] = ({'address': owner['address'], 'since_seq': owner['since_seq']}
                                 if owner else None)
                path['refused_clients'] = (min(2**53-1, max(0, refused_clients))
                                          if type(refused_clients) is int else 0)
            entry['paths'].append(path)

        if profile.get('isolated'):
            endpoint = row.get('client_endpoint') or {}
            for path in endpoint.get('services') or endpoint.get('alternatives', []):
                if path.get('role') not in ('home', 'ap') or not path.get('host'): continue
                transport = path['transport']
                service = path.get('service') or ('lx200' if transport == 'tcp' else 'synscan')
                # Multi-port relays (Vespera) keep the target port's declared role.
                purpose = port_role(kind, path['target_port']) if path.get('target_port') and kind in ISOLATED_ASSIGNED else ('control', service)
                add(path['role'], path['host'], path['port'], transport,
                    service, path.get('ready', False),
                    owner=path.get('owner'), udp=transport == 'udp',
                    refused_clients=path.get('refused_clients', 0), purpose=purpose)
            continue
        # A routed target is distinct from a bridge port redirect. Never imply
        # that the bridge has verified the client's/router's route to that IP.
        for path in row.get('management_paths', []):
            if path.get('role') not in ('home', 'ap') or not path.get('host'): continue
            ready = path.get('forwarding_ready', False)
            transport = relay['transport'] if relay else 'tcp'
            for port in ports:
                if row.get(proxy_flag(kind)):
                    service = {'zwo_mount': 'lx200', 'synscan': 'synscan',
                               'seestar': 'seestar_native'}.get(kind)
                    if kind == 'vespera':
                        service = 'vespera_http' if port == 8082 else 'vespera_socketio'
                    if kind == 'dwarf':
                        # The DWARF proxy owns all three fixed ports; advertise each.
                        service = {9900: 'dwarf_ws', 8082: 'dwarf_http', 554: 'dwarf_rtsp'}.get(port)
                    if service:
                        add(path['role'], path['host'], port, transport, service, ready)
                add(path['role'], profile['target_ip'], port, transport, 'direct_route', ready, route=True)
    return result
