"""Bounded passive Vespera connection demand; never reads application payloads."""
import ctypes
import ipaddress
import select
import socket
import time

from .config import auto_policy, profiles
from .plugins import plugin


def syn_filter(ports=(8082, 8083)):
    """Classic BPF: IPv4 TCP initial SYN, ports8082/8083, header-only snaplen."""
    program = []
    labels = {}
    def stmt(code, value): program.append((code, 0, 0, value))
    def jump(code, value, yes=None, no=None): program.append((code, yes, no, value))
    stmt(0x28, 12)                         # EtherType
    jump(0x15, 0x0800, no='reject')
    stmt(0x30, 14); stmt(0x54, 0xf0)       # IPv4 version
    jump(0x15, 0x40, no='reject')
    stmt(0x30, 23)
    jump(0x15, 6, no='reject')             # TCP only
    stmt(0x28, 20)
    jump(0x45, 0x3fff, yes='reject')       # No fragments, including first MF
    stmt(0x30, 14); stmt(0x54, 15)
    jump(0x35, 5, no='reject')             # Minimum IPv4 header
    stmt(0xb1, 14)                        # X = IPv4 IHL *4
    stmt(0x50, 27); stmt(0x54, 0x17)
    jump(0x15, 2, no='reject')             # SYN without ACK, FIN or RST
    stmt(0x48, 16)                        # TCP destination port
    if not ports or len(ports)>4 or any(type(p) is not int or not 1<=p<=65535 for p in ports):raise ValueError('Invalid passive TCP ports.')
    for index, port in enumerate(ports):
        jump(0x15, port, yes='accept', no='reject' if index == len(ports)-1 else None)
    labels['accept'] = len(program)
    stmt(0x87, 0); stmt(0x04, 34); stmt(0x16, 0)  # IHL+Ethernet+fixed TCP, no payload
    labels['reject'] = len(program)
    stmt(0x06, 0)
    return tuple((code, labels[yes]-i-1 if yes else 0,
                  labels[no]-i-1 if no else 0, value)
                 for i, (code, yes, no, value) in enumerate(program))


def filtered_socket(interface, ports=(8082, 8083)):
    """No receiving protocol until the mandatory kernel filter is installed."""
    if not hasattr(socket, 'AF_PACKET'): raise OSError('Passive demand unavailable.')
    class Instruction(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                    ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint32)]
    class Program(ctypes.Structure):
        _fields_ = [('len', ctypes.c_ushort), ('filter', ctypes.POINTER(Instruction))]
    instructions = syn_filter(ports)
    array = (Instruction * len(instructions))(*(Instruction(*item) for item in instructions))
    program = Program(len(instructions), array)
    stream = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
    try:
        stream.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
        stream.setsockopt(socket.SOL_SOCKET, 26, bytes(program))  # Linux SO_ATTACH_FILTER
        stream.bind((interface, 0x0800))
        stream.setblocking(False)
        return stream
    except (OSError, ValueError):
        stream.close()
        raise


def connection_request(frame, packet_type, management, ports=(8082, 8083)):
    """Validate only fixed headers; source stays transient and is never logged."""
    if packet_type == getattr(socket, 'PACKET_OUTGOING', 4) or len(frame) < 54: return None
    if frame[12:14] != b'\x08\x00': return None
    ip = frame[14:]
    ihl = (ip[0] & 15) * 4
    total = int.from_bytes(ip[2:4], 'big')
    if (ip[0] >> 4 != 4 or ihl < 20 or len(ip) < ihl+20 or total < ihl+20
            or ip[9] != 6 or int.from_bytes(ip[6:8], 'big') & 0x3fff): return None
    tcp = ip[ihl:ihl+20]
    offset = (tcp[12] >> 4) * 4
    if offset < 20 or total < ihl+offset or tcp[13] & 0x17 != 2: return None
    if int.from_bytes(tcp[2:4], 'big') not in ports: return None
    try:
        interface = ipaddress.IPv4Interface(management[1])
        source = ipaddress.IPv4Address(ip[12:16]); target = ipaddress.IPv4Address(ip[16:20])
        if (source not in interface.network or source in (interface.ip, interface.network.network_address,
                interface.network.broadcast_address) or source.is_multicast or source.is_unspecified): return None
        # Signature is RAM-only retransmission suppression; never public identity.
        return str(source), str(target), bytes(ip[12:20]+tcp[:8])
    except (ValueError, TypeError, IndexError): return None


class VesperaDemand:
    kind = 'vespera'
    read_size = 94

    def open_socket(self, plan):
        return filtered_socket(plan[0],tuple(plugin(self.kind)['relay']['ports']))

    def __init__(self, controller, clock=time.monotonic, *, kind=None):
        if kind is not None:self.kind=kind
        self.c, self.clock = controller, clock
        self.sockets = {}
        self.seen = {}
        self.pending = {}
        self.retry_at = 0
        self.supported = False
        self.reason = 'DEMO_NOT_OBSERVED' if controller.demo else 'DISABLED'

    def status(self):
        with self.c.lock:
            return {'supported': self.supported, 'observer': self.reason,
                    **self.c.operations.demand_status(self.kind)}

    def close(self):
        for stream in self.sockets.values(): stream.close()
        self.sockets.clear()
        self.pending.clear()

    def plans(self):
        with self.c.lock:
            if self.c.demo or not self.c.store.config.get('auto_connect') or not auto_policy(self.c.store.config)[self.kind+'_demand']:
                return ()
            return tuple(self.c.management_plans[:2])

    def sync(self):
        plans = self.plans()
        self.pending = {key:value for key,value in self.pending.items() if key[0] in plans and value[0]>self.clock()}
        for plan in list(self.sockets):
            if plan not in plans: self.sockets.pop(plan).close()
        with self.c.lock:
            if not plans:
                self.reason = 'DEMO_NOT_OBSERVED' if self.c.demo else 'DISABLED' if not self.c.store.config.get('auto_connect') or not auto_policy(self.c.store.config)[self.kind+'_demand'] else 'MANAGEMENT_UNAVAILABLE'
                return
        if self.clock() < self.retry_at: return
        for plan in plans:
            if plan in self.sockets: continue
            try:
                self.sockets[plan] = self.open_socket(plan)
                with self.c.lock: self.supported = True; self.reason = 'LISTENING'
            except (OSError, ValueError, AttributeError):
                self.close()
                with self.c.lock: self.supported = False; self.reason = 'OBSERVER_UNAVAILABLE'
                self.retry_at = self.clock()+30
                return

    def receive(self, frame, packet_type, plan):
        parsed = connection_request(frame, packet_type, plan,tuple(plugin(self.kind)['relay']['ports']))
        if not parsed: return
        source, target, signature = parsed
        with self.c.lock:
            if plan not in self.plans():return
            endpoint=str(ipaddress.IPv4Interface(plan[1]).ip)
            proxy=plugin(self.kind)['discovery'].get('target_mode')=='proxy_or_original' and target==endpoint
            if not any(p['kind']==self.kind and (proxy or p['target_ip']==target) for p in profiles(self.c.store.config)):return
        self.offer(plan, source, signature, target=None if proxy else target)

    def offer(self, plan, source, signature, *, target=None, seen_seconds=600):
        now = self.clock()
        self.seen = {key: expiry for key, expiry in self.seen.items() if expiry > now}
        key = (plan, signature)
        if key in self.seen or key in self.pending or len(self.seen) >= 128: return
        result = self.c.operations.demand(plan, source, kind=self.kind, target=target)
        if result == 'RECONCILE_PENDING':
            # Fixed headers only, RAM only, at most eight requests for fifteen
            # seconds. Every eventual admission rechecks source, policy and locks.
            if len(self.pending)<8:self.pending[key]=(now+15,source,target,seen_seconds)
        elif result != 'UNTRUSTED_MANAGEMENT_SOURCE':self.seen[key]=now+seen_seconds

    def retry_pending(self):
        plans=self.plans();now=self.clock()
        self.seen={key:expiry for key,expiry in self.seen.items() if expiry>now}
        self.pending={key:value for key,value in self.pending.items() if key[0] in plans and value[0]>now}
        with self.c.lock:
            if self.c.stop.is_set():self.pending.clear();return
            if not self.c.last_cycle_ok or self.c.net_lock.locked():return
        for key,value in list(self.pending.items()):
            self.pending.pop(key,None)
            if key in self.seen or len(self.seen)>=128:continue
            expires,source,target,seen_seconds=value
            result=self.c.operations.demand(key[0],source,kind=self.kind,target=target)
            if result=='RECONCILE_PENDING':self.pending[key]=value
            elif result!='UNTRUSTED_MANAGEMENT_SOURCE':self.seen[key]=now+seen_seconds

    def loop(self):
        try:
            while not self.c.stop.is_set():
                self.sync()
                self.retry_pending()
                streams = list(self.sockets.values())
                if not streams: self.c.stop.wait(.5); continue
                try:
                    readable, _, _ = select.select(streams, [], [], .25)
                    for plan, stream in list(self.sockets.items()):
                        if stream not in readable: continue
                        for _ in range(4):
                            try: frame, address = stream.recvfrom(self.read_size)
                            except BlockingIOError: break
                            self.receive(frame, address[2], plan)
                except (OSError, ValueError):
                    self.close()
                    with self.c.lock: self.supported = False; self.reason = 'OBSERVER_UNAVAILABLE'
                    self.retry_at = self.clock()+30
                self.c.stop.wait(.05)
        finally: self.close()
