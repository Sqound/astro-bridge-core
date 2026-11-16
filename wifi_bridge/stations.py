"""Station instruments: joined to the bridge network (hotspot or private LAN).

The bridge never joins their Wi-Fi and spends no radio on them. It only checks
that the declared address answers a TCP handshake on a read-only port (no
manufacturer command) and hands paired clients the instrument's own address.
Leases and night reservations apply; radio protection does not, because no
radio is involved.
"""
import ipaddress
import re
import socket
import threading
import time

from .plugins import plugin, port_role

# Kinds with a manufacturer station mode, and the port whose TCP handshake
# shows the instrument answers. DWARF uses its HTTP port so a check never
# occupies its single control session.
PROBE_PORTS = {'seestar': 4700, 'dwarf': 8082}
SERVICES = {'seestar': {}, 'dwarf': {9900: 'dwarf_ws', 8082: 'dwarf_http', 554: 'dwarf_rtsp'}}
MAX_STATIONS = 16
INTERVAL = 15
FIELDS = {'id', 'name', 'kind', 'target_ip', 'client_connect'}


def validate_stations(entries, used_ids):
    """Return normalized station profiles; raises Invalid with an owner-facing reason."""
    from .config import Invalid, text
    if not isinstance(entries, list) or len(entries) > MAX_STATIONS:
        raise Invalid('Save at most 16 station instruments.')
    result, ids = [], set(used_ids)
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - FIELDS - {'access'} or entry.get('access', 'station') != 'station':
            raise Invalid('Invalid station instrument.')
        ident = text(entry.get('id', ''), 32)
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}', ident) or ident in ids:
            raise Invalid('Invalid or duplicate profile ID.')
        ids.add(ident)
        name = text(entry.get('name', ''), 64).strip()
        kind = entry.get('kind')
        if not name or kind not in PROBE_PORTS:
            raise Invalid('Station mode is available for Seestar and DWARF instruments.')
        try:
            address = ipaddress.IPv4Address(entry.get('target_ip', ''))
        except (ipaddress.AddressValueError, TypeError, ValueError):
            raise Invalid('Enter the instrument IPv4 address on the bridge network.') from None
        if not address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
            raise Invalid('Enter the instrument IPv4 address on the bridge network.')
        connect = entry.get('client_connect', True)
        if type(connect) is not bool: raise Invalid('Invalid enabled value.')
        result.append({'id': ident, 'name': name, 'kind': kind, 'access': 'station',
                       'target_ip': str(address), 'client_connect': connect})
    return result


def tcp_probe(address, port, timeout=1.0):
    """Handshake only: returns round-trip milliseconds, or None when it does not answer."""
    started = time.monotonic()
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return round((time.monotonic() - started) * 1000, 1)
    except OSError:
        return None


class Stations:
    def __init__(self, controller, *, clock=time.monotonic, probe=tcp_probe):
        self.c, self.clock, self.probe = controller, clock, probe
        self.lock = threading.Lock()
        self.states = {}
        self.worker = None
        # Demonstration never opens sockets: every saved station answers.
        self.demo_reachable = {}

    def profiles(self):
        return list(self.c.store.config.get('stations', []))

    def find(self, ident):
        return next((p for p in self.profiles() if p['id'] == ident), None)

    # Reachability -----------------------------------------------------------------
    def record(self, ident, rtt):
        """Apply one observation; also feeds session telemetry while it answers."""
        profile = self.find(ident)
        if profile is None: return
        with self.lock:
            previous = self.states.get(ident, {})
            reachable = rtt is not None
            self.states[ident] = {'reachable': reachable, 'rtt_ms': rtt, 'checked': self.clock(),
                                  'changes': previous.get('changes', 0) + (previous.get('reachable') is True and not reachable)}
        self.telemetry(profile, rtt, previous.get('reachable') is True and not reachable)

    def telemetry(self, profile, rtt, lost):
        sessions = getattr(self.c, 'telemetry_sessions', None)
        if sessions is None or sessions.closed: return
        reserved = any(v['profile_id'] == profile['id'] and v['expires'] > self.c.protection.clock()
                       for v in self.c.protection.leases.values())
        try:
            if lost: sessions.observe(profile['id'], 'link.disconnects', 1)
            elif rtt is not None and reserved:
                # A client reservation is the explicit intent to observe this night.
                sessions.activity(profile['id'], profile['kind'], ['station_link_v1'], 'link.ping_ms', rtt)
            elif rtt is not None:
                sessions.observe(profile['id'], 'link.ping_ms', rtt)
        except ValueError:
            pass  # Telemetry limits never affect reachability.

    def tick(self):
        """Called by the controller cycle; checks run off the cycle thread."""
        due = [p for p in self.profiles() if self.clock() - self.states.get(p['id'], {}).get('checked', -INTERVAL) >= INTERVAL]
        known = {p['id'] for p in self.profiles()}
        with self.lock:
            for ident in set(self.states) - known: self.states.pop(ident)
        if not due: return
        if self.c.demo:
            for p in due: self.record(p['id'], 1.0 if self.demo_reachable.get(p['id'], True) else None)
            return
        if self.worker and self.worker.is_alive(): return
        def run():
            for p in due: self.record(p['id'], self.probe(p['target_ip'], PROBE_PORTS[p['kind']]))
        self.worker = threading.Thread(target=run, name='station-probe', daemon=True)
        self.worker.start()

    def reachable(self, ident):
        with self.lock: return self.states.get(ident, {}).get('reachable')

    # Native projection ------------------------------------------------------------
    def catalog(self, client=None, events=(), sequences=None):
        from .instruments import lease_summary
        p = self.c.protection
        result = []
        for profile in self.profiles():
            ident = profile['id']
            reachable = self.reachable(ident)
            held = [v for v in p.leases.values() if v['profile_id'] == ident and v['expires'] > p.clock()]
            mine = [v for v in held if v['client'] == client]
            permission = profile['client_connect']
            link = 'disabled' if not permission else 'saved' if reachable is None else 'connected' if reachable else 'unreachable'
            blockers = (['CLIENT_CONNECT_DISABLED'] if not permission else []) + (
                ['LEASED_BY_OTHER_CLIENT'] if any(v['client'] != client for v in held) else [])
            sequence = (sequences or {}).get(ident, max((e['sequence'] for e in events if e.get('profile_id', e.get('instrument_id')) == ident), default=0))
            entry = plugin(profile['kind'])
            result.append({'id': ident, 'kind': 'smart_telescope', 'family': profile['kind'], 'name': profile['name'],
                'link': link, 'service': 'not_probed' if reachable is None else 'ready' if reachable else 'not_answering',
                'protocol': entry.get('protocol'), 'model': None, 'verified_identity': None, 'identity_source': 'unverified',
                'access': 'station', 'simultaneous': True,
                'current_operation': self.c.operation_detail(dict(profile, enabled=True)),
                'since_seq': sequence, 'reason': None if reachable is not False else 'NOT_ANSWERING',
                'client_connect': permission, 'blockers': blockers, 'lease': lease_summary(held, mine, p.clock()),
                'capabilities': {'telemetry': ['station_link_v1'], 'maker_key': None}})
        return result

    def endpoint(self, ident, request_path='other'):
        profile = self.find(ident)
        if profile is None or not self.reachable(ident): return None
        services = SERVICES[profile['kind']]
        ports = []
        for port in plugin(profile['kind'])['relay']['ports']:
            role, protocol = port_role(profile['kind'], port)
            ports.append({'port': port, 'transport': 'tcp', 'service': services.get(port, profile['kind'] + '_native'),
                          'role': role, 'protocol': protocol})
        return {'instrument_id': ident, 'request_path': request_path, 'mode': 'direct',
                'primary': {'host': profile['target_ip'], 'ports': ports}, 'alternatives': [],
                'requires_client_route': False, 'target_ip': profile['target_ip']}

    def connect(self, ident, client, ttl, reserve, path):
        from .api import ApiError
        profile = self.find(ident)
        if not profile['client_connect']:
            raise ApiError(403, 'CLIENT_CONNECT_DISABLED', 'Paired apps cannot connect this telescope.')
        self.c.protection.native_guard(ident, client, owner=False)
        if not self.reachable(ident):
            raise ApiError(409, 'INSTRUMENT_UNREACHABLE', 'The instrument does not answer on the bridge network. Check that it is on and joined to it.')
        lease = self.c.protection.native_create(ident, client, ttl, reservation=reserve)
        lease['station'] = True
        return 200, {'state': 'ready', 'instrument_id': ident, 'lease': self.c.protection.native_public(lease),
                     'endpoint': self.endpoint(ident, path)}

    def disconnect(self, ident, data, client):
        """Nothing to disconnect: release only this holder's leases."""
        from .api import ApiError
        own = {k: v for k, v in self.c.protection.leases.items() if v['profile_id'] == ident and v['client'] == client}
        if 'lease_id' in data and data['lease_id'] not in own:
            raise ApiError(409, 'INSTRUMENT_PROTECTED', 'Release reservations and explicitly remove the owner lock before changing this connection.')
        for key in own:
            self.c.protection.leases.pop(key)
            if self.c.protection.lease_hook: self.c.protection.lease_hook('lease.ended', ident, key, 0, 'RELEASED')
        return {'ok': True}

    def public(self):
        """Owner status rows, without probing."""
        with self.lock:
            return [dict(p, reachable=self.states.get(p['id'], {}).get('reachable'),
                         rtt_ms=self.states.get(p['id'], {}).get('rtt_ms')) for p in self.profiles()]
