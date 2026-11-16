"""Private udhcpc lease hook: on-link IPv4 only, no default route or DNS changes."""
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def lease_address(address, mask, target):
    interface = ipaddress.IPv4Interface(address + '/' + mask)
    remote = ipaddress.IPv4Address(target)
    private = [ipaddress.ip_network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    if (not any(interface.network.subnet_of(n) for n in private) or interface.network.prefixlen < 16
            or remote not in interface.network or remote in (interface.ip, interface.network.network_address, interface.network.broadcast_address)
            or interface.ip in (interface.network.network_address, interface.network.broadcast_address)):
        raise ValueError('Invalid instrument lease')
    return str(interface)


def main():
    from wifi_bridge.config import atomic_json
    import re
    name = os.environ['AB_INTERFACE']
    if not re.fullmatch('[A-Za-z0-9_.-]{1,15}', name) or os.environ.get('interface') != name:
        raise ValueError('Unexpected DHCP interface')
    path = Path(os.environ['AB_LEASE_FILE'])
    event = sys.argv[1] if len(sys.argv) == 2 else ''
    def command(*args):
        subprocess.run(['ip', *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    if event in ('deconfig', 'nak', 'leasefail'):
        path.unlink(missing_ok=True)
        command('-4', 'address', 'flush', 'dev', name, 'scope', 'global')
    elif event in ('bound', 'renew'):
        address = lease_address(os.environ['ip'], os.environ.get('subnet', '255.255.255.0'), os.environ['AB_TARGET'])
        seconds = int(os.environ.get('lease', '0'))
        if not 10 <= seconds <= 2**32-1: raise ValueError('Invalid lease lifetime')
        # Retain only this interface's connected prefix. Ignore routers/routes/DNS.
        old = json.loads(path.read_text()).get('address') if path.exists() else None
        if old and old != address: command('address', 'del', old, 'dev', name)
        command('address', 'replace', address, 'dev', name, 'valid_lft', str(seconds), 'preferred_lft', str(seconds))
        atomic_json(path, {'address': address, 'expires': time.monotonic()+seconds})


if __name__ == '__main__':
    try: main()
    except Exception: sys.exit(1)  # Never echo untrusted DHCP data or environment.
