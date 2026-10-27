"""Bounded native mount demand and read-only SynScan discovery relay."""
import ipaddress
import socket
import time

from .config import auto_policy, profiles
from .mounts import synscan_identity
from .plugins import plugin, proxy_flag, demand_flag
from .vespera_demand import VesperaDemand, connection_request, filtered_socket


class TcpPluginDemand(VesperaDemand):
    def open_socket(self, plan):
        return filtered_socket(plan[0], tuple(plugin(self.kind)['relay']['ports']))

    def receive(self, frame, packet_type, plan):
        parsed = connection_request(frame, packet_type, plan, tuple(plugin(self.kind)['relay']['ports']))
        if not parsed: return
        source, target, signature = parsed
        with self.c.lock:
            if plan not in self.plans(): return
            management_ip = str(ipaddress.IPv4Interface(plan[1]).ip)
            candidates = [p for p in profiles(self.c.store.config) if p['kind'] == self.kind]
            if target==management_ip and plugin(self.kind)['discovery']['target_mode']=='original':return
            if target != management_ip and not any(p['target_ip'] == target for p in candidates): return
            if not candidates: return
        self.offer(plan, source, signature, target=None if target == management_ip else target, seen_seconds=5)


class ZwoMountDemand(TcpPluginDemand):
    kind = 'zwo_mount'


def synscan_query(data, client, plan):
    """Only the documented non-mutating motor-board version question."""
    if data not in (b':e1\r', b':e2\r'): return None
    try:
        interface = ipaddress.IPv4Interface(plan[1])
        source = ipaddress.IPv4Address(client[0])
        if source not in interface.network or source in (interface.ip, interface.network.network_address, interface.network.broadcast_address): return None
        return str(source)
    except (ValueError, TypeError, IndexError): return None


class SynscanDiscovery(VesperaDemand):
    kind = 'synscan'

    def __init__(self, controller, clock=time.monotonic, *, kind=None):
        super().__init__(controller, clock, kind=kind)
        self.next_request = 0

    def plans(self):
        with self.c.lock:
            if self.c.demo: return ()
            enabled = (self.c.store.config.get('auto_connect') and auto_policy(self.c.store.config)[demand_flag(self.kind)]
                       and any(p['kind'] == self.kind for p in profiles(self.c.store.config)))
            selected = any(row.get(proxy_flag(self.kind)) for row in self.c.state['telescopes'])
            return tuple(self.c.management_plans[:2]) if enabled or selected else ()

    def sync(self):
        plans = self.plans()
        self.pending = {key:value for key,value in self.pending.items() if key[0] in plans and value[0]>self.clock()}
        for plan in list(self.sockets):
            if plan not in plans: self.sockets.pop(plan).close()
        if not plans:
            self.reason = 'DEMO_NOT_OBSERVED' if self.c.demo else 'DISABLED'
            return
        if self.clock() < self.retry_at: return
        for plan in plans:
            if plan in self.sockets: continue
            stream = None
            try:
                stream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, plan[0].encode()+b'\0')
                stream.bind(('', 11880)); stream.settimeout(.1)
                self.sockets[plan] = stream
                self.supported = True; self.reason = 'LISTENING'
            except (OSError, ValueError, AttributeError):
                if stream is not None: stream.close()
                self.close(); self.supported = False; self.reason = 'OBSERVER_UNAVAILABLE'
                self.retry_at = self.clock()+30
                return

    def receive(self, data, client, plan, listener):
        source = synscan_query(data, client, plan)
        if source is None or self.clock() < self.next_request: return
        with self.c.lock:
            if plan not in self.plans(): return
            if not any(p['kind'] == self.kind for p in profiles(self.c.store.config)): return
            row = next((r for r in self.c.state['telescopes'] if r.get(proxy_flag(self.kind)) and r.get('relay_ready')), None)
            profile = next((p for p in profiles(self.c.store.config) if row and p['id'] == row['id'] and p['kind'] == self.kind), None)
            device = next((d for d in self.c.state['devices'] if profile and d['mac'] == profile['mac']), None)
            binding = (profile['id'], profile['mac'], profile['target_ip']) if device else None
        self.next_request = self.clock()+.25
        if binding is None:
            signature = (source, client[1], data)
            self.offer(plan, source, signature, seen_seconds=5)
            return
        # The selected direct Wi-Fi path is already verified by reconciliation.
        # Forward only version reads; never synthesize identity or station data.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as upstream:
            upstream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device['name'].encode()+b'\0')
            upstream.settimeout(.75)
            upstream.sendto(data, (binding[2], 11880)); reply, origin = upstream.recvfrom(65)
        if origin != (binding[2], 11880) or synscan_identity(reply) is None: return
        with self.c.lock:
            current = next((p for p in profiles(self.c.store.config) if p['id'] == binding[0]), None)
            valid = (current and current['enabled'] and current['kind']==self.kind and (current['id'],current['mac'],current['target_ip']) == binding
                     and plan in self.plans() and any(r['id'] == binding[0] and r.get(proxy_flag(self.kind)) and r.get('relay_ready') for r in self.c.state['telescopes']))
        if valid: listener.sendto(reply, client)

    def loop(self):
        try:
            while not self.c.stop.is_set():
                self.sync()
                self.retry_pending()
                for plan, stream in list(self.sockets.items()):
                    try:
                        data, client = stream.recvfrom(9)
                        self.receive(data, client, plan, stream)
                    except (OSError, ValueError): continue
                self.c.stop.wait(.05 if self.sockets else .5)
        finally: self.close()
