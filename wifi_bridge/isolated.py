"""Opt-in mount radio namespaces and explicit management-side transport endpoints.

No veth or IP route connects instrument namespaces to management or to each other.
Only bounded sockets created inside the selected namespace cross this boundary.
"""
import contextlib
import ctypes
import hashlib
import hmac
import secrets
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

from .instrument_relay import InstrumentRelay, RelayCounters, UdpOwner
from .isolated_observer import ObserverPermission, OBSERVER_MARK, declarations, input_rule, output_rules

KINDS = {'zwo_mount': ('tcp', 4030, 14030), 'synscan': ('udp', 11880, 21180), 'vespera': ('tcp', 8082, 18082)}
# Every relayed instrument port: (target port, endpoint service). The first is KINDS' port.
SERVICES = {'zwo_mount': ((4030, 'lx200'),), 'synscan': ((11880, 'synscan'),),
            # Vespera: REST over HTTP and Socket.IO (WebSocket upgrade), relayed byte-for-byte.
            'vespera': ((8082, 'vespera_http'), (8083, 'vespera_socketio'))}
# Multi-port kinds: the bridge assigns and persists a port block; owners do not choose it.
ASSIGNED = frozenset(kind for kind, services in SERVICES.items() if len(services) > 1)
RESERVED_PORTS = {18787, 18788}


def client_ports(profile):
    """Client port per relayed service, in SERVICES order."""
    return list(profile.get('client_ports') or [profile['client_port']])


def allocate_ports(entries, previous):
    from .config import Invalid
    used = set()
    for entry in entries:
        if not entry.get('isolated') or entry['kind'] in ASSIGNED: continue
        port = entry.get('client_port')
        if port is not None:
            if type(port) is not int or not 10000 <= port <= 49151 or port in RESERVED_PORTS:
                raise Invalid('Choose a dedicated client port from 10000 to 49151, excluding management ports.')
            key = (KINDS[entry['kind']][0], port)
            if key in used: raise Invalid('Dedicated client ports must be unique for each transport, including inactive mounts.')
            used.add(key)
    # Assigned blocks: keep a retained, valid, conflict-free block; else take the next free one.
    retained = []
    for entry in entries:
        if not entry.get('isolated') or entry['kind'] not in ASSIGNED: continue
        transport = KINDS[entry['kind']][0]
        block = entry.get('client_ports')
        size = len(SERVICES[entry['kind']])
        if (isinstance(block, list) and len(block) == size and all(type(p) is int and 10000 <= p <= 49151 and p not in RESERVED_PORTS for p in block)
                and not {(transport, p) for p in block} & used and len(set(block)) == size):
            used |= {(transport, p) for p in block}
        else:
            entry.pop('client_ports', None); retained.append(entry)
    for entry in retained:
        transport, _, port = KINDS[entry['kind']]
        size = len(SERVICES[entry['kind']])
        while any((transport, port + i) in used or port + i in RESERVED_PORTS for i in range(size)): port += 1
        if port + size - 1 > 49151: raise Invalid('No dedicated client port available.')
        entry['client_ports'] = [port + i for i in range(size)]
        used |= {(transport, p) for p in entry['client_ports']}
    for entry in entries:
        if entry.get('isolated') and entry['kind'] in ASSIGNED: entry['client_port'] = entry['client_ports'][0]
    # Explicit and retained values are reserved before assigning new profiles.
    for entry in entries:
        if not entry.get('isolated') or entry.get('client_port') is not None: continue
        transport, _, port = KINDS[entry['kind']]
        while (transport, port) in used or port in RESERVED_PORTS: port += 1
        if port > 49151: raise Invalid('No dedicated client port available.')
        entry['client_port'] = port
        used.add((transport, port))


def namespace_name(mac):
    if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac): raise ValueError('Invalid radio identity')
    return 'ab-radio-' + mac.replace(':', '')


@contextlib.contextmanager
def in_namespace(path):
    """setns affects the calling OS thread only; always restore before returning."""
    with contextlib.ExitStack() as files:
        root = os.open('/proc/thread-self/ns/net', os.O_RDONLY)
        files.callback(os.close, root)
        target = os.open(path, os.O_RDONLY)
        files.callback(os.close, target)
        libc = ctypes.CDLL(None, use_errno=True)
        libc.setns.argtypes = [ctypes.c_int, ctypes.c_int]
        libc.setns.restype = ctypes.c_int
        def switch(fd):
            if libc.setns(fd, 0x40000000) != 0: raise OSError(ctypes.get_errno(), 'Network namespace unavailable')
        switch(target)
        try: yield
        finally: switch(root)


def namespace_socket(path, interface, kind, *, mark=None):
    with in_namespace(path):
        stream = socket.socket(socket.AF_INET, kind)
        try:
            stream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode()+b'\0')
            if mark is not None:
                if mark != OBSERVER_MARK: raise ValueError('Invalid observer socket mark')
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, mark)
        except BaseException:
            stream.close(); raise
        return stream


def namespace_firewall(target, transport, port):
    """port is one target port, or a tuple of them for a multi-port instrument."""
    remote = ipaddress.IPv4Address(target)
    ports = port if isinstance(port, tuple) else (port,)
    if transport not in ('tcp', 'udp') or not ports or any(type(p) is not int or not 1 <= p <= 65535 for p in ports):
        raise ValueError('Invalid isolated service')
    port = ports[0] if len(ports) == 1 else '{ ' + ', '.join(str(p) for p in ports) + ' }'
    incoming = ('chain input { type filter hook input priority 0; policy drop; '
                'iifname "lo" accept; ct state established,related accept; '
                'meta nfproto ipv4 udp sport 67 udp dport 68 accept; '+input_rule()+'}\n')
    outgoing = ('chain output { type filter hook output priority 0; policy drop; '
                'oifname "lo" accept; '
                f'meta mark != {OBSERVER_MARK} meta nfproto ipv4 udp sport 68 udp dport 67 accept; '
                +output_rules()+f'meta mark != {OBSERVER_MARK} ip daddr {remote} {transport} dport {port} accept; '
                f'meta mark != {OBSERVER_MARK} ip daddr {remote} icmp type echo-request accept; }}\n')
    return ('table inet ab_instrument {\n'+declarations()
            +'chain forward { type filter hook forward priority 0; policy drop; }\n'
            +incoming+outgoing+'}\n')



def linux_boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def phy_name(device):
    return (Path('/sys/class/net') / device['name'] / 'phy80211' / 'name').read_text().strip()


def process_identity(pid):
    try:
        raw = Path('/proc', str(pid), 'stat').read_text()
        return raw.rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError): return None


class IsolatedRadios:
    def __init__(self, directory, runner):
        self.directory = Path(directory) / 'isolated'
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.run = runner
        self.children = {}
        self.records = {}
        self.observer_permissions = {}
        self.observer_accesses = set()
        self.relays = {}
        self.counters = {}
        self.udp_owners = {}
        self.ping_at = {}
        self.pinging = set()
        self.lock = threading.RLock()
        self.policy = {'quiet_seconds': 120, 'cooldown_seconds': 60}
        self.boot_id = linux_boot_id()
        from .config import atomic_json
        key_file = self.directory/'intent-key.json'
        if not key_file.exists(): atomic_json(key_file, {'key':secrets.token_hex(32)})
        try:
            key_info = key_file.stat()
            if key_file.is_symlink() or key_info.st_uid != os.geteuid() or key_info.st_mode & 0o077 or key_info.st_size > 256:
                raise ValueError()
            self.intent_key = bytes.fromhex(json.loads(key_file.read_text())['key'])
            if len(self.intent_key) != 32: raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise RuntimeError('Private isolated intent key requires administrator recovery') from None
        for file in self.directory.glob('radio-*.json'):
            try:
                info = file.stat()
                if file.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 16384:
                    raise ValueError('Private bounded ownership required')
                value = json.loads(file.read_text())
                if value['boot_id'] != self.boot_id:
                    file.unlink(); continue
                from .config import role_for, private_ipv4
                ident = value['profile_id']
                if (not isinstance(ident, str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,31}', ident)
                        or value['role'] != role_for({'id':ident}) or type(value.get('created')) is not bool
                        or value.get('kind') not in KINDS or not re.fullmatch('[0-9a-f]{64}', str(value.get('intent')))
                        or not isinstance(value['processes'], list) or len(value['processes']) > 2):
                    raise ValueError('Invalid ownership intent')
                private_ipv4(value['target'])
                for process in value['processes']:
                    if (set(process) != {'pid','start'} or type(process['pid']) is not int or process['pid'] < 2
                            or process['start'] is not None and not re.fullmatch('[0-9]+', str(process['start']))):
                        raise ValueError('Invalid helper identity')
                mac = value['device']['mac']
                if (file.name != 'radio-'+mac.replace(':', '')+'.json' or value['namespace'] != namespace_name(mac)
                        or not re.fullmatch('[A-Za-z0-9_.-]{1,15}', value['device']['name'])
                        or not re.fullmatch('phy[0-9]+', value['phy'])): raise ValueError()
                self.records[mac] = value
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                # Invalid ownership must never authorize moving or killing anything.
                raise RuntimeError('Isolated radio ownership requires administrator recovery') from None

    def _file(self, mac): return self.directory / ('radio-'+mac.replace(':', '')+'.json')

    def persist(self, record):
        from .config import atomic_json
        atomic_json(self._file(record['device']['mac']), record)

    def record_for(self, device): return self.records.get(device['mac'])

    def intent(self, profile):
        from .config import network_intent
        return hmac.new(self.intent_key, json.dumps(network_intent(profile),sort_keys=True).encode(), 'sha256').hexdigest()

    def command(self, record, args, **kwargs):
        return self.run(['ip', 'netns', 'exec', record['namespace'], *args], **kwargs)

    def present_devices(self):
        found = []
        for record in list(self.records.values()):
            try:
                raw = self.command(record, ['ip', '-j', 'link', 'show', 'dev', record['device']['name']], timeout=2)
                links = json.loads(raw)
                if links and links[0].get('address') == record['device']['mac']:
                    found.append(dict(record['device'], isolated=True))
            except Exception: continue
        return found

    def _spawn(self, record, args, **environment):
        env = dict(PATH='/usr/sbin:/usr/bin:/sbin:/bin', LC_ALL='C',
                   PYTHONPATH=str(Path(__file__).resolve().parent.parent), **environment)
        child = subprocess.Popen(['ip', 'netns', 'exec', record['namespace'], *args],
                                 env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children[child.pid] = child
        record.setdefault('processes', []).append({'pid': child.pid, 'start': process_identity(child.pid)})
        self.persist(record)
        return child

    def _stop_processes(self, record):
        for process in record.get('processes', []):
            pid = process['pid']
            try:
                # PIDs may be reused. Only signal a recorded start-time in our namespace.
                if (process['start'] and process_identity(pid) == process['start']
                        and os.stat('/proc/'+str(pid)+'/ns/net').st_ino == os.stat('/run/netns/'+record['namespace']).st_ino):
                    os.kill(pid, 15)
            except (OSError, ProcessLookupError): pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not any(process_identity(p['pid']) == p['start'] for p in record.get('processes', [])): break
            time.sleep(.05)
        for process in record.get('processes', []):
            try:
                if process['start'] and process_identity(process['pid']) == process['start']:
                    if os.stat('/proc/'+str(process['pid'])+'/ns/net').st_ino == os.stat('/run/netns/'+record['namespace']).st_ino:
                        os.kill(process['pid'], 9)
            except OSError: pass
        for process in record.get('processes', []):
            child = self.children.pop(process['pid'], None)
            if child:
                try: child.wait(timeout=2)
                except subprocess.TimeoutExpired: pass
        record['processes'] = []
        self.persist(record)

    def connect(self, role, device, profile):
        from .network import NetworkError, NetworkAuthenticationError, NetworkIsolationError
        if profile['kind'] not in KINDS: raise NetworkError('Isolation is supported for ZWO and SynScan mounts and Vespera telescopes only.')
        for command in ('ip', 'iw', 'wpa_supplicant', 'wpa_cli', 'busybox', 'nft'):
            if not shutil.which(command): raise NetworkIsolationError('ISOLATION_TOOLS_MISSING')
        if 'udhcpc' not in self.run(['busybox', '--list'], timeout=3).splitlines():
            raise NetworkIsolationError('ISOLATION_DHCP_TOOL_MISSING')
        old = self.record_for(device)
        if old: self.release(old['role'])
        self.close_profile(profile['id'])
        namespace = namespace_name(device['mac'])
        if Path('/run/netns', namespace).exists():
            raise NetworkIsolationError('ISOLATION_OWNERSHIP_CONFLICT')
        try: phy = phy_name(device)
        except OSError: raise NetworkIsolationError('ISOLATION_RADIO_UNAVAILABLE') from None
        if not re.fullmatch('phy[0-9]+', phy): raise NetworkError('Invalid Wi-Fi radio identity.')
        record = {'namespace': namespace, 'role': role, 'device': dict(device), 'phy': phy,
                  'boot_id': self.boot_id, 'profile_id': profile['id'], 'target': profile['target_ip'],
                  'kind': profile['kind'], 'intent': self.intent(profile), 'processes': [], 'created': False}
        self.records[device['mac']] = record
        # Ownership journal precedes mutations, including a partially moved radio.
        self.persist(record)
        folder = self.directory / namespace
        folder.mkdir(mode=0o700, exist_ok=True)
        try:
            self.run(['nmcli', 'device', 'set', device['name'], 'managed', 'no'])
            self.run(['ip', 'link', 'set', device['name'], 'down'])
            self.run(['ip', 'link', 'set', 'dev', device['name'], 'address', device['mac']])
            self.run(['ip', 'netns', 'add', namespace])
            record['created'] = True; self.persist(record)
            self.run(['iw', 'phy', phy, 'set', 'netns', 'name', namespace])
            self.command(record, ['ip', 'link', 'set', 'lo', 'up'])
            # Netlink operations also work in Docker where /proc/sys is read-only.
            self.command(record, ['ip', 'link', 'set', 'dev', device['name'], 'addrgenmode', 'none'])
            self.command(record, ['ip', '-6', 'address', 'flush', 'dev', device['name']])
            self.command(record, ['ip', 'link', 'set', device['name'], 'up'])
            transport = KINDS[profile['kind']][0]
            port = tuple(p for p, _ in SERVICES[profile['kind']])
            # No veth, no default route, no forwarding. Even raw upstream sockets
            # may reach only the selected service, DHCP and target ICMP echo;
            # the empty observer sets admit nothing until explicit owner opt-in.
            script = namespace_firewall(profile['target_ip'], transport, port)
            self.command(record, ['nft', '-f', '-'], input=script)
            record['observer_policy_v1'] = True
            password = profile.get('password', '')
            if password:
                psk = password if re.fullmatch('[0-9a-fA-F]{64}', password) else hashlib.pbkdf2_hmac('sha1', password.encode(), profile['ssid'].encode(), 4096, 32).hex()
                security = 'key_mgmt=WPA-PSK\npsk='+psk+'\n'
            else: security = 'key_mgmt=NONE\n'
            conf = 'ctrl_interface='+str(folder)+'\nupdate_config=0\nnetwork={\nssid='+profile['ssid'].encode().hex()+'\n'+security
            if profile.get('bssid'): conf += 'bssid='+profile['bssid']+'\n'
            conf += 'scan_ssid='+str(int(profile.get('hidden', False)))+'\n}\n'
            private_text(folder/'wpa.conf', conf)
            self._spawn(record, ['wpa_supplicant', '-i', device['name'], '-c', str(folder/'wpa.conf')])
            deadline = time.monotonic()+30
            monitor = None; monitor_path = folder/'monitor'
            monitor_path.unlink(missing_ok=True)
            try:
                while time.monotonic() < deadline:
                    if monitor is None and (folder/device['name']).exists():
                        monitor = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                        monitor.bind(str(monitor_path)); monitor.connect(str(folder/device['name']))
                        monitor.send(b'ATTACH'); monitor.setblocking(False)
                    if monitor:
                        while True:
                            try: event = monitor.recv(2048)
                            except BlockingIOError: break
                            if b'reason=WRONG_KEY' in event or b'pre-shared key may be incorrect' in event:
                                raise NetworkAuthenticationError()
                    status = self.run(['wpa_cli', '-p', str(folder), '-i', device['name'], 'status'], timeout=2, check=False)
                    if 'wpa_state=COMPLETED' in status: break
                    time.sleep(.25)
                else: raise NetworkError('Isolated Wi-Fi association timed out. Check power, signal and Wi-Fi credentials.')
            finally:
                if monitor: monitor.close()
                monitor_path.unlink(missing_ok=True)
            # ESP-based mount APs may lose DHCP replies with station power saving.
            # Drivers without a power-save control can continue without this hint.
            self.command(record, ['iw', 'dev', device['name'], 'set', 'power_save', 'off'], check=False)
            hook = folder/'dhcp-hook'
            private_text(hook, '#!'+sys.executable+'\nimport sys\nsys.path.insert(0,'+repr(str(Path(__file__).resolve().parent.parent))+')\nfrom wifi_bridge.isolated_dhcp import main\ntry: main()\nexcept Exception: sys.exit(1)\n', mode=0o700)
            lease = folder/'lease.json'; lease.unlink(missing_ok=True)
            self._spawn(record, ['busybox', 'udhcpc', '-f', '-n', '-t', '5', '-T', '2', '-i', device['name'], '-s', str(hook)],
                        AB_INTERFACE=device['name'], AB_TARGET=profile['target_ip'], AB_LEASE_FILE=str(lease))
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                if self.address(device): return
                time.sleep(.25)
            raise NetworkError('The mount did not provide a usable on-link IPv4 lease.')
        except Exception as error:
            try: self.release(role)
            except Exception:
                raise NetworkIsolationError('ISOLATION_RECOVERY_REQUIRED') from None
            if isinstance(error, NetworkAuthenticationError): raise
            raise NetworkIsolationError('ISOLATION_SETUP_FAILED') from None

    def connected(self, role, device, profile=None):
        record = self.record_for(device)
        if not record or record['role'] != role: return False
        if profile and (not profile.get('isolated') or record['profile_id'] != profile['id']
                        or not hmac.compare_digest(record['intent'], self.intent(profile))): return False
        try:
            link = self.command(record, ['iw', 'dev', device['name'], 'link'], timeout=2)
            from .link_observations import remember_link
            metadata = {}
            remember_link(metadata, device['name'], link)
            record['link_observation'] = metadata[device['name']]
            return bool(re.search('^Connected to ', link, re.M) and self.address(device)
                        and (profile is None or any(line.strip() == 'SSID: '+profile['ssid'] for line in link.splitlines())))
        except Exception: return False

    def address(self, device):
        record = self.record_for(device)
        if not record: return ''
        try:
            lease = json.loads((self.directory/record['namespace']/'lease.json').read_text())
            if lease['expires'] <= time.monotonic(): return ''
            raw = self.command(record, ['ip', '-j', '-4', 'address', 'show', 'dev', device['name']], timeout=2)
            addresses = {a['local']+'/'+str(a['prefixlen']) for row in json.loads(raw) for a in row.get('addr_info', [])}
            return lease['address'] if lease['address'] in addresses else ''
        except (OSError, ValueError, KeyError): return ''

    def link_observation(self, device, profile):
        if not self.connected(self.record_for(device)['role'], device, profile): return None
        link = self.command(self.record_for(device), ['iw', 'dev', device['name'], 'link'], timeout=2)
        match = re.search('^Connected to ([a-fA-F0-9:]{17})', link, re.M)
        return {'bssid': match[1].lower()} if match else None

    def release(self, role):
        record = next((r for r in self.records.values() if r['role'] == role), None)
        if not record: return
        with self.lock:
            # Serialize invalidation with policy writes and marked socket setup.
            record['observer_closing'] = True
        self.close_profile(record['profile_id'])
        self._stop_processes(record)
        ns_exists = Path('/run/netns', record['namespace']).exists()
        if ns_exists and not record.get('created'):
            from .network import NetworkError
            raise NetworkError('Isolated namespace creation was interrupted; administrator recovery is required.')
        if ns_exists and record.get('created'):
            present = self.command(record, ['ip', '-j', 'link', 'show'], timeout=2)
            owned = any(row.get('address') == record['device']['mac'] for row in json.loads(present))
            if owned:
                self.command(record, ['ip', 'link', 'set', record['device']['name'], 'down'])
                self.command(record, ['ip', '-4', 'address', 'flush', 'dev', record['device']['name']])
                self.command(record, ['iw', 'phy', record['phy'], 'set', 'netns', str(os.getpid())])
            self.run(['ip', 'netns', 'delete', record['namespace']])
        root_mac = Path('/sys/class/net', record['device']['name'], 'address')
        if root_mac.exists() and root_mac.read_text().strip() == record['device']['mac']:
            self.run(['nmcli', 'device', 'set', record['device']['name'], 'managed', 'yes'])
        self._file(record['device']['mac']).unlink(missing_ok=True)
        (self.directory/record['namespace']/'lease.json').unlink(missing_ok=True)
        (self.directory/record['namespace']/'wpa.conf').unlink(missing_ok=True)
        self.records.pop(record['device']['mac'], None)

    def socket_factory(self, record):
        return lambda kind: namespace_socket('/run/netns/'+record['namespace'], record['device']['name'], kind)

    def observer_access(self, record, profile, address):
        return ObserverPermission(self, record, profile, address)

    def close_profile(self, ident):
        for access in list(self.observer_accesses):
            if access.ident == ident: access.close()
        for key, value in list(self.relays.items()):
            if key[0] == ident:
                value.close(); self.relays.pop(key)
        self.counters.pop(ident, None)
        owner = self.udp_owners.pop(ident, None)
        if owner: owner.release()

    def sync(self, profiles, rows, paths, demo=False):
        wanted = set()
        for profile in profiles:
            if not profile.get('isolated'): continue
            row = next((r for r in rows if r['id'] == profile['id']), None)
            if not row: continue
            transport = KINDS[profile['kind']][0]
            port = profile['client_port']
            endpoints = []
            count = self.counters.setdefault(profile['id'], RelayCounters())
            owner = self.udp_owners.setdefault(profile['id'], UdpOwner()) if transport == 'udp' else None
            for path, (target_port, service), port in ((path, service, port) for path in paths
                                                       for service, port in zip(SERVICES[profile['kind']], client_ports(profile))):
                host = str(ipaddress.ip_interface(path['address']).ip)
                endpoint = {'role': path['role'], 'host': host, 'port': port,
                            'protocol': {'tcp': 'lx200', 'udp': 'synscan'}[transport] if profile['kind'] not in ASSIGNED else service,
                            'service': service, 'target_port': target_port,
                            'transport': transport, 'ready': False, 'reason': 'NOT_CONNECTED'}
                key = (profile['id'], host, port, transport, path['device']['name'], profile['target_ip'], profile['mac'])
                if profile['enabled'] and row.get('connected') and row.get('state') in ('online', 'waiting_service'):
                    wanted.add(key)
                    try:
                        if not demo:
                            record = self.records[profile['mac']]
                            if key in self.relays and not self.relays[key].healthy():
                                self.relays.pop(key).close()
                            if key not in self.relays:
                                self.relays[key] = InstrumentRelay(host, port, transport, profile['target_ip'], target_port,
                                    self.socket_factory(record), count, interface=path['device']['name'],
                                    subnet=str(ipaddress.ip_interface(path['address']).network), owner=owner).start()
                        endpoint.update(ready=True, reason='SIMULATED' if demo else 'LISTENING')
                    except (OSError, ValueError, KeyError): endpoint['reason'] = 'ENDPOINT_BIND_FAILED'
                endpoints.append(endpoint)
            preferred = next((e for e in endpoints if e['role'] == 'home'), next(iter(endpoints), None))
            port = profile['client_port']
            row['client_endpoint'] = dict(preferred or {'host': None, 'port': port, 'transport': transport,
                'protocol': 'lx200' if transport == 'tcp' else 'synscan', 'ready': False, 'reason': 'NO_MANAGEMENT_PATH'},
                # alternatives keeps one entry per path (first service), as before multi-port
                # relays; services lists every relayed port on every path.
                alternatives=[e for e in endpoints if e['target_port'] == SERVICES[profile['kind']][0][0]],
                **({'services': endpoints} if profile['kind'] in ASSIGNED else {}), simulated=demo)
            if owner:
                if not any(e['ready'] for e in endpoints): owner.release()
                ownership = owner.snapshot()
                row['client_endpoint'].update(ownership)
                for endpoint in endpoints: endpoint.update(ownership)
            row['transport_metrics'] = count.snapshot(**self._policy_args())
            row['relay_ready'] = any(e['ready'] for e in endpoints)
            if row.get('connected') and not row['relay_ready']:
                row.update(state='waiting_service', message='Dedicated mount endpoint unavailable; check its port and management connection.')
            record = self.records.get(profile['mac'])
            if not demo and record and row.get('connected'): self._ping(profile['id'], record)
        for key in set(self.relays)-wanted:
            self.relays.pop(key).close()
        saved = {p['id'] for p in profiles if p.get('isolated')}
        for ident in set(self.counters)-saved:
            self.counters.pop(ident, None); self.ping_at.pop(ident, None)
        active_udp = {key[0] for key in wanted if key[3] == 'udp'}
        for ident in set(self.udp_owners)-active_udp:
            self.udp_owners.pop(ident).release()

    def owner_status(self, ident):
        owner = self.udp_owners.get(ident)
        return owner.snapshot() if owner else {'owner': None, 'refused_clients': 0, 'owner_idle_seconds': 20}

    def release_owner(self, ident):
        owner = self.udp_owners.get(ident)
        return owner.release() if owner else self.owner_status(ident)

    def _policy_args(self):
        return {'quiet': self.policy['quiet_seconds'], 'cooldown': self.policy['cooldown_seconds']}

    def activity(self):
        return {ident: count.snapshot(**self._policy_args()) for ident, count in list(self.counters.items())
                if any(key[0] == ident and relay.healthy() for key, relay in list(self.relays.items()))}

    def _ping(self, ident, record):
        now = time.monotonic()
        with self.lock:
            if ident in self.pinging or now < self.ping_at.get(ident, 0): return
            self.pinging.add(ident); self.ping_at[ident] = now+5
        counter = self.counters.get(ident)
        def collect():
            value = None
            measured = False
            try:
                if not shutil.which('ping'):
                    if counter: counter.probe_error = 'PING_TOOL_UNAVAILABLE'
                    return
                measured = True
                reply = self.command(record, ['ping', '-n', '-c', '1', '-W', '1', '-I', record['device']['name'], record['target']], timeout=2, check=False)
                match = re.search(r'time[=<]([0-9.]+)\s*ms', reply)
                if match: value = float(match[1])
                measured = bool(match or re.search(r'\b[1-9][0-9]* packets? transmitted', reply))
                if not measured and counter: counter.probe_error = 'PROBE_UNAVAILABLE'
            except Exception:
                measured = False
                if counter: counter.probe_error = 'PROBE_UNAVAILABLE'
            finally:
                with self.lock:
                    if measured and self.counters.get(ident) is counter and self.records.get(record['device']['mac']) is record:
                        counter.ping(value)
                    self.pinging.discard(ident)
        threading.Thread(target=collect, name='mount-ping', daemon=True).start()

    def close(self):
        for relay in list(self.relays.values()): relay.close()
        self.relays.clear()
        for owner in self.udp_owners.values(): owner.release()
        self.udp_owners.clear()
        errors = []
        for record in list(self.records.values()):
            try: self.release(record['role'])
            except Exception: errors.append(record['profile_id'])
        if errors: raise RuntimeError('Some isolated radios require administrator recovery')


def private_text(path, value, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), mode); stream.write(value)
