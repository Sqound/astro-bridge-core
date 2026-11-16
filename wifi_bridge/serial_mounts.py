"""Declared serial mounts (not Wi-Fi), their relays and the detected adapters.

Declaring a mount is the owner's explicit confirmation: nothing is relayed for
an adapter that is only detected. Each declared mount runs its own relay (own
adapter, TCP port, single client, stop sequence); adding or removing one never
touches the others.
"""
import copy
import os
import re
import threading
import time

from . import serial_port
from .serial_profile import profile as find_profile, public_profile, load_profiles, BAUDS, FORMAT
from .serial_relay import SerialRelay

MAX_MOUNTS = 8
PORT_BASE = 2000          # the app's serial-bridge preset default; 2000..2099 never collides with other relays
PORT_LIMIT = 2099
FIELDS = {'id', 'name', 'profile', 'device', 'tcp_port', 'baud', 'format', 'idle_timeout_seconds', 'declared_identity'}
SCAN_INTERVAL = 3.0


def validate_serial_mounts(entries, used_ids, previous=()):
    """Normalized list; assigns the lowest free TCP port in 2000-2099 when none is given."""
    from .config import Invalid, text
    if not isinstance(entries, list) or len(entries) > MAX_MOUNTS:
        raise Invalid('Save at most 8 serial mounts.')
    before = {m['id']: m for m in previous}
    result, ids, devices, ports = [], set(used_ids), set(), set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - FIELDS:
            raise Invalid('Invalid serial mount.')
        ident = text(entry.get('id', ''), 32)
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}', ident) or ident in ids:
            raise Invalid('Invalid or duplicate profile ID.')
        ids.add(ident)
        name = text(entry.get('name', ''), 64).strip()
        if not name: raise Invalid('Invalid serial mount.')
        prof = find_profile(entry.get('profile', 'nexstar'))
        if prof is None: raise Invalid('Unknown serial device profile.')
        device = entry.get('device')
        if not serial_port.is_stable_path(device):
            raise Invalid('Choose the adapter by its stable /dev/serial/by-id or by-path link, not by /dev/ttyUSB number.')
        if device in devices: raise Invalid('Each serial mount needs its own adapter.')
        devices.add(device)
        baud = entry.get('baud', prof['serial']['baud'])
        fmt = entry.get('format', prof['serial']['format'])
        if type(baud) is not int or baud not in BAUDS or not isinstance(fmt, str) or not FORMAT.fullmatch(fmt):
            raise Invalid('Invalid serial settings.')
        idle = entry.get('idle_timeout_seconds', 120)
        if type(idle) is not int or idle != 0 and not 5 <= idle <= 3600:
            raise Invalid('The idle timeout must be 0 (off) or 5 to 3600 seconds.')
        port = entry.get('tcp_port', (before.get(ident) or {}).get('tcp_port'))
        if port is not None and (type(port) is not int or not PORT_BASE <= port <= PORT_LIMIT):
            raise Invalid('Choose a TCP port from 2000 to 2099.')
        identity = entry.get('declared_identity', (before.get(ident) or {}).get('declared_identity'))
        if identity is not None and (not isinstance(identity, dict) or len(str(identity)) > 400):
            raise Invalid('Invalid serial mount.')
        result.append({'id': ident, 'name': name, 'profile': prof['id'], 'device': device, 'tcp_port': port,
                       'baud': baud, 'format': fmt, 'idle_timeout_seconds': idle,
                       **({'declared_identity': identity} if identity else {})})
    taken = {m['tcp_port'] for m in result if m['tcp_port'] is not None}
    if len(taken) != len([m for m in result if m['tcp_port'] is not None]):
        raise Invalid('Each serial mount needs its own TCP port.')
    for mount in result:
        if mount['tcp_port'] is None:
            mount['tcp_port'] = next(p for p in range(PORT_BASE, PORT_LIMIT + 1) if p not in taken)
            taken.add(mount['tcp_port'])
    return result


def link_label(kind):
    return {'wifi': 'Wi-Fi', 'serial_usb': 'Serial (USB)', 'usb': 'USB'}[kind]


class SerialMounts:
    def __init__(self, controller, *, clock=time.monotonic, factory=SerialRelay, inventory=None, log=print):
        self.c, self.clock, self.factory, self.log = controller, clock, factory, log
        self.inventory = inventory or serial_port.inventory
        self.lock = threading.RLock()
        self.relays = {}
        self.errors = {}
        self.scanned = None
        self.found = []
        self.probes = {}
        # Demonstration never opens a device, unless a test sandbox is configured.
        self.active = not controller.demo or serial_port.DEV_ROOT != '/dev/serial'

    def mounts(self):
        return list(self.c.store.config.get('serial_mounts', []))

    def find(self, ident):
        return next((m for m in self.mounts() if m['id'] == ident), None)

    # -- relays ---------------------------------------------------------------
    def reconcile(self):
        """Start, stop or rename relays to match the saved configuration, one mount at a time."""
        if not self.active: return
        with self.lock:
            wanted = {m['id']: m for m in self.mounts()}
            for ident in set(self.relays) - set(wanted):
                self.relays.pop(ident).close()
            for ident, mount in wanted.items():
                relay = self.relays.get(ident)
                if relay is not None and not relay.healthy(): self.relays.pop(ident); relay = None
                same = relay and (relay.device, relay.tcp_port, relay.baud, relay.fmt, relay.idle_seconds, relay.profile['id']) == (
                    mount['device'], mount['tcp_port'], mount['baud'], mount['format'], mount['idle_timeout_seconds'], mount['profile'])
                if relay is not None and not same:
                    relay.close(); self.relays.pop(ident); relay = None
                if relay is None:
                    relay = self.factory(ident, mount['name'], mount['device'], find_profile(mount['profile']), mount['tcp_port'],
                                         bind='127.0.0.1' if self.c.demo else '0.0.0.0', baud=mount['baud'], fmt=mount['format'],
                                         idle_seconds=mount['idle_timeout_seconds'], log=self.log)
                    try:
                        relay.start(); self.relays[ident] = relay; self.errors.pop(ident, None)
                    except OSError as error:
                        self.errors[ident] = 'PORT_IN_USE' if error.errno == 98 or error.errno == 48 else 'LISTEN_FAILED'
                else:
                    relay.name = mount['name']
            for ident in set(self.errors) - set(wanted): self.errors.pop(ident)

    def close(self):
        with self.lock:
            relays, self.relays = list(self.relays.values()), {}
        for relay in relays: relay.close()

    def tick(self):
        self.reconcile()
        return self.scan()

    # -- detection ----------------------------------------------------------------
    def scan(self, force=False):
        if not self.active: return self.found
        now = self.clock()
        with self.lock:
            if not force and self.scanned is not None and now - self.scanned < SCAN_INTERVAL: return self.found
            self.scanned = now
        rows = self.inventory()
        with self.lock:
            self.found = rows
            nodes = {r['node'] for r in rows}
            for node in set(self.probes) - nodes: self.probes.pop(node)
        for row in rows: self._auto_identify(row)
        return rows

    def _declared_node(self, row):
        """The mount that names this adapter by any of its links (stable even when it is unplugged)."""
        links = {row.get('by_id'), row.get('by_path'), row.get('stable_path')} - {None}
        for mount in self.mounts():
            if mount['device'] in links or os.path.realpath(mount['device']) == row['node']: return mount
        return None

    def _hint(self, row):
        for prof in load_profiles().values():
            for hint in prof.get('usb_hints', []):
                if (row.get('vendor_id'), row.get('product_id')) == (hint['vendor'], hint['product']): return prof
        return None

    def _auto_identify(self, row):
        """Read-only identification of a new adapter, only for the cable chips the profile names."""
        if self._declared_node(row) or row['node'] in self.probes: return
        prof = self._hint(row)
        self.probes[row['node']] = None
        if prof is None: return
        self.identify_node(row['node'], prof)

    def identify_node(self, node, prof):
        """Explicit or automatic read of an undeclared adapter; never while the port is busy."""
        probe = SerialRelay('probe', 'probe', node, prof, 0, auto_identify=False)
        probe.listener = None
        try:
            probe._identify()
        except Exception:
            probe.identity = None
        with self.lock: self.probes[node] = probe.identity
        return probe.identity

    # -- public state ----------------------------------------------------------------
    def public(self):
        rows = []
        with self.lock:
            relays = dict(self.relays)
            errors = dict(self.errors)
        for mount in self.mounts():
            relay = relays.get(mount['id'])
            live = relay.snapshot() if relay else {'state': 'demo' if not self.active else 'error', 'reason': errors.get(mount['id']),
                                                    'adapter_present': None, 'client': None, 'refused_clients': 0, 'last_refused': None,
                                                    'stops_sent': 0, 'last_stop': None, 'stop_pending': False, 'identity': None, 'journal': []}
            identity = live.get('identity')
            declared = mount.get('declared_identity') or {}
            mismatch = bool(identity and identity.get('responding') and declared.get('model_code') is not None
                            and identity.get('model_code') != declared.get('model_code'))
            rows.append(dict(mount, **live,
                             kind='mount', link_type='serial_usb',
                             identification=('identified' if identity and identity.get('responding') else 'not_identified' if identity else 'unknown'),
                             identity_mismatch=mismatch,
                             client_setup={'port': mount['tcp_port'], 'transport': 'tcp', 'host': None, 'preset': find_profile(mount['profile'])['app_preset']}))
        return rows

    def detected(self):
        """Adapters seen but not declared: offered for adding, never relayed."""
        rows = []
        with self.lock:
            found = list(self.found)
            probes = dict(self.probes)
        used = {m['tcp_port'] for m in self.mounts()}
        for row in found:
            if self._declared_node(row): continue
            identity = probes.get(row['node'])
            prof = self._hint(row) or find_profile('nexstar')
            port = next((p for p in range(PORT_BASE, PORT_LIMIT + 1) if p not in used), None)
            responding = bool(identity and identity.get('responding'))
            name = ('NexStar ' + identity['model_name']) if responding and identity.get('model_name') else prof['name']
            rows.append({'node': row['node'], 'stable_path': row['stable_path'], 'stable_kind': row['stable_kind'],
                         'vendor_id': row.get('vendor_id'), 'product_id': row.get('product_id'),
                         'manufacturer': row.get('manufacturer'), 'product': row.get('product'), 'serial_number': row.get('serial_number'),
                         'by_path': row.get('by_path'), 'link_type': 'serial_usb',
                         'identification': 'identified' if responding else 'not_identified' if identity else 'unknown',
                         'identity': identity, 'can_add': bool(row['stable_path']),
                         'unique_by_id': bool(row.get('serial_number')),
                         'proposal': {'name': name, 'profile': prof['id'], 'device': row['stable_path'], 'tcp_port': port,
                                      'baud': prof['serial']['baud'], 'format': prof['serial']['format'], 'idle_timeout_seconds': 120,
                                      **({'declared_identity': {k: identity.get(k) for k in ('model_code', 'model_name', 'hand_control_version')}} if responding else {})}})
        return rows

    def complete(self, data):
        """A declaration may name just a detected adapter: proposal defaults fill the rest."""
        import uuid
        from .api import ApiError
        found = next((d['proposal'] for d in self.detected() if d['stable_path'] == data.get('device')), None)
        entry = dict(found or {})
        entry.update(data)
        if not entry.get('name'): raise ApiError(400, 'INVALID_FIELDS', 'Give this serial mount a name.')
        entry.setdefault('id', 'serial-' + uuid.uuid4().hex[:8])
        return entry

    def link_held(self, ident):
        relay = self.relays.get(ident)
        return bool(relay and relay.snapshot()['client'])

    def identify(self, ident):
        relay = self.relays.get(ident)
        if relay is None: return None
        return relay.request_identify()
