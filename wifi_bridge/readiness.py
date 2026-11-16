"""Bounded service checks, independent of Wi-Fi association and relay readiness."""
import copy
import ipaddress
import socket
import threading
from contextlib import nullcontext

from .mounts import mount_probe
from .plugins import plugin


def empty_service(method=None):
    return {'state': 'not_probed', 'checked_at_seq': None, 'method': method, 'failures': 0}


def service_probe(device, address, target, kind):
    """Only TCP handshakes or the existing read-only SynScan identity exchange."""
    method = plugin(kind)['readiness']
    if method == 'udp_identity':
        return mount_probe(device, address, target, kind) is not None
    if method != 'tcp_connect':
        return None
    interface = ipaddress.IPv4Interface(address)
    remote = ipaddress.IPv4Address(target)
    if (remote not in interface.network or remote in (interface.ip, interface.network.network_address,
            interface.network.broadcast_address) or not remote.is_private or remote.is_loopback or remote.is_link_local):
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device['name'].encode() + b'\0')
        stream.bind((str(interface.ip), 0))
        stream.settimeout(2)
        stream.connect((str(remote), plugin(kind)['relay']['ports'][0]))
    return True


class ServiceReadiness:
    """One synchronous probe per radio; repeated status reads never trigger checks."""
    def __init__(self, network, demo=False):
        self.net, self.demo = network, demo
        self.values = {}
        self.lock = threading.Lock()
        self.inflight = set()
        self.sequence = 0

    def prune(self, profiles):
        keep = {p['id'] for p in profiles}
        with self.lock:
            self.values = {key: value for key, value in self.values.items() if key in keep}

    def disconnected(self, ident):
        with self.lock:
            record = self.values.get(ident)
            if record:
                record['service'] = empty_service(record['service']['method'])

    def check(self, profile, device, address, now, *, vespera=None):
        method = plugin(profile['kind'])['readiness']
        method = None if method == 'association' else method
        if method is None:
            return empty_service()
        signature = (profile['mac'], profile['target_ip'], profile['kind'], address, bool(profile.get('isolated')))
        with self.lock:
            record = self.values.get(profile['id'])
            if record is None or record['signature'] != signature:
                record = {'signature': signature, 'next_check': record['next_check'] if record else -1, 'service': empty_service(method)}
                self.values[profile['id']] = record
            # Vespera retains its existing controller probe and readiness semantics.
            if vespera is not None:
                return self._result(record, bool(vespera))
            activity = self.net.activity_status().get('profiles', {}).get(profile['id'], {}) if hasattr(self.net, 'activity_status') else {}
            if (self.demo or now < record['next_check'] or profile['mac'] in self.inflight
                    or activity.get('state') == 'active' or activity.get('raw_active') is True):
                return copy.deepcopy(record['service'])
            self.inflight.add(profile['mac'])
            record['next_check'] = now + 30
        try:
            context = nullcontext()
            manager = getattr(self.net, 'isolated', None)
            if profile.get('isolated'):
                owned = manager.record_for(device) if manager else None
                if not owned:
                    result = None
                    with self.lock:
                        return self._result(record, result)
                from .isolated import in_namespace
                context = in_namespace('/run/netns/' + owned['namespace'])
            with context:
                result = service_probe(device, address, profile['target_ip'], profile['kind'])
        except OSError:
            result = False
        except (ValueError, AttributeError):
            result = None
        finally:
            with self.lock:
                self.inflight.discard(profile['mac'])
        with self.lock:
            return self._result(record, result)

    def _result(self, record, result):
        self.sequence += 1
        value = record['service']
        value.update(state='unknown' if result is None else 'ready' if result else 'not_answering',
                     checked_at_seq=self.sequence,
                     failures=0 if result else min(value['failures'] + 1, 2**31 - 1) if result is False else value['failures'])
        return copy.deepcopy(value)
