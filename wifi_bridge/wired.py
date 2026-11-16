"""Read-only admission of one bridge-owned wired management connection."""
import ipaddress
from pathlib import Path
import re


def home(run, split, root=Path('/sys/class/net')):
    raw = run(['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device', 'status'], timeout=3)
    if len(raw) > 16384: return None
    candidates = []
    for line in raw.splitlines():
        fields = split(line)
        if len(fields) == 4 and fields[1:] == ['ethernet', 'connected', 'wifi-bridge-wired']:
            candidates.append(fields[0])
    if len(candidates) != 1 or not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', candidates[0]): return None
    name = candidates[0]; path = root / name
    if not (path / 'device').exists() or (path / 'wireless').exists() or (path / 'phy80211').exists(): return None
    if (path / 'type').read_text().strip() != '1' or (path / 'carrier').read_text().strip() != '1': return None
    raw = run(['nmcli', '-t', '-f', 'GENERAL.CONNECTION,GENERAL.PERM-HWADDR,IP4.ADDRESS', 'device', 'show', name], timeout=3)
    if len(raw) > 4096: return None
    values = [split(line) for line in raw.splitlines()]
    connection = [v[1] for v in values if len(v) == 2 and v[0] == 'GENERAL.CONNECTION']
    macs = [v[1] for v in values if len(v) == 2 and v[0] == 'GENERAL.PERM-HWADDR']
    addresses = [v[1] for v in values if len(v) == 2 and v[0].startswith('IP4.ADDRESS[')]
    if connection != ['wifi-bridge-wired'] or len(macs) != 1 or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', macs[0]): return None
    if len(addresses) != 1: return None
    address = ipaddress.IPv4Interface(addresses[0])
    if address.ip.is_loopback or address.ip.is_link_local or address.ip.is_unspecified or address.ip.is_multicast: return None
    return {'name': name, 'mac': macs[0].lower(), 'kind': 'ethernet',
            'label': name, 'usb': '/usb' in str((path / 'device').resolve()),
            'address': str(address)}
