"""Documented Wi-Fi mount transports and bounded read-only identification.

No alignment, tracking, guiding, slew, park, initialization or setting commands
are sent here. Wi-Fi names are hints; protocol replies are not authentication.
"""
import ipaddress
import re
import socket
import time
from .plugins import all_plugins, plugin, profile_kinds, ssid_plugin

MOUNTS = {p['id']:{'name':p['name'],'target_ip':p['default_address'],'port':p['relay']['ports'][0],
                  'transport':p['relay']['transport']} for p in all_plugins() if p['category']=='mount' and p['relay']}
PROFILE_KINDS = profile_kinds()


def ssid_kind(ssid):
    selected=ssid_plugin(ssid)
    return selected['id'] if selected['category']=='mount' else None


def zwo_identity(reply):
    if not isinstance(reply, bytes) or len(reply) > 40: return None
    try: name = reply.decode('ascii').strip()
    except UnicodeError: return None
    if not re.fullmatch(r'(?:ZWO[ _-]+)?AM[0-9]{1,3}N?#', name, re.I): return None
    return {'family': 'zwo_mount', 'model': name[:-1], 'authentication': 'unverified'}


def synscan_identity(reply):
    if not isinstance(reply, bytes) or not re.fullmatch(rb'=[0-9A-Fa-f]{6}\r', reply): return None
    # The final wire byte is a board/mount code, not another firmware component.
    # Keep the documented wire value; do not guess a model or serial from it.
    return {'family': 'synscan', 'board_version_hex': reply[1:7].decode().upper(),
            'model': None, 'authentication': 'unverified'}


def mount_probe(device, address, target, kind, *, clock=time.monotonic):
    if kind not in MOUNTS or plugin(kind)['identification'] not in ('zwo_model','synscan_version'): return None
    interface = ipaddress.IPv4Interface(address)
    remote = ipaddress.IPv4Address(target)
    if (remote not in interface.network or remote in (interface.ip, interface.network.network_address,
            interface.network.broadcast_address) or not remote.is_private or remote.is_loopback or remote.is_link_local): return None
    spec = MOUNTS[kind]
    deadline = clock() + 2.5
    def remaining():
        value = deadline - clock()
        if value <= 0: raise TimeoutError('Mount identification deadline exceeded.')
        return value
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if spec['transport'] == 'tcp' else socket.SOCK_DGRAM) as stream:
        stream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device['name'].encode() + b'\0')
        stream.bind((str(interface.ip), 0))
        stream.settimeout(remaining())
        if plugin(kind)['identification'] == 'zwo_model':
            stream.connect((str(remote), spec['port']))
            stream.settimeout(remaining())
            stream.sendall(b':GVP#')
            data = b''
            while len(data) < 40 and clock() < deadline:
                stream.settimeout(remaining())
                part = stream.recv(41 - len(data))
                if not part: break
                data += part
                if b'#' in data: break
            identity = zwo_identity(data)
        else:
            stream.sendto(b':e1\r', (str(remote), spec['port']))
            data, origin = stream.recvfrom(65)
            if origin != (str(remote), spec['port']): return None
            identity = synscan_identity(data)
    if identity is None or clock() >= deadline: return None
    return {'address': str(remote), 'source': kind + '-read-only-query',
            'reported_identity': None, 'protocol_identity': identity,
            'identity_reason': 'PROTOCOL_REPLY_NOT_AUTHENTICATED'}
