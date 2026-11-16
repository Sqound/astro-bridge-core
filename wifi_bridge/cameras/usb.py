"""Bounded Linux USB metadata; no device opens, serials, commands or payload logs.

All reads are explicit. Import and construction never inspect the host. Kernel
uevents are wake-up hints only: sysfs is always reconciled after loss/overflow.
"""
from itertools import islice
import math
import os
from pathlib import Path
import re
import socket

MAX_ENTRIES = 512
DEVICE = re.compile(r'[1-9][0-9]{0,2}-[1-9][0-9]{0,2}(?:\.[1-9][0-9]{0,2}){0,7}')
INTERFACE = re.compile(r'([1-9][0-9]{0,2}-[1-9][0-9]{0,2}(?:\.[1-9][0-9]{0,2}){0,7}):[0-9]{1,3}\.[0-9]{1,3}')


def validate_device(value):
    fields = {'vendor_id', 'product_id', 'device_class', 'interface_classes', 'speed_mbps', 'bus', 'port_path'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('invalid_usb_metadata')
    for key, maximum in (('vendor_id', 65535), ('product_id', 65535), ('device_class', 255), ('bus', 999)):
        if type(value[key]) is not int or not 0 <= value[key] <= maximum:
            raise ValueError('invalid_usb_metadata')
    path, classes, speed = value['port_path'], value['interface_classes'], value['speed_mbps']
    if not isinstance(path, str) or not DEVICE.fullmatch(path) or int(path.split('-')[0]) != value['bus']:
        raise ValueError('invalid_usb_metadata')
    if not isinstance(classes, list) or len(classes) > 256 or any(type(c) is not int or not 0 <= c <= 255 for c in classes):
        raise ValueError('invalid_usb_metadata')
    if type(speed) not in (int, float) or not math.isfinite(speed) or not 0 < speed <= 100000:
        raise ValueError('invalid_usb_metadata')
    return dict(value, interface_classes=sorted(set(classes)))


def _read(path):
    # Attribute symlinks are not USB metadata. Device directories themselves are
    # symlinks on Linux and are intentionally followed within the injected root.
    if path.is_symlink():
        raise ValueError('invalid_usb_attribute')
    with path.open('rb') as stream:
        raw = stream.read(65)
    if len(raw) > 64:
        raise ValueError('invalid_usb_attribute')
    return raw.decode('ascii').strip()


def _hex(path, digits):
    value = _read(path)
    if not re.fullmatch('[0-9a-fA-F]{' + str(digits) + '}', value):
        raise ValueError('invalid_usb_attribute')
    return int(value, 16)


class SysfsUSB:
    def __init__(self, root='/sys/bus/usb/devices'):
        self.root = Path(root)

    def scan(self):
        """None means unavailable/incomplete evidence, never mass removal."""
        try:
            with os.scandir(self.root) as entries:
                paths = [self.root / entry.name for entry in islice(entries, MAX_ENTRIES + 1)]
            if len(paths) > MAX_ENTRIES:
                return None
            classes = {}
            for path in paths:
                match = INTERFACE.fullmatch(path.name)
                if match:
                    try:
                        classes.setdefault(match[1], set()).add(_hex(path / 'bInterfaceClass', 2))
                    except FileNotFoundError:
                        continue
                    except (OSError, ValueError, UnicodeError):
                        return None
            result = []
            for path in sorted(paths):
                if not DEVICE.fullmatch(path.name):
                    continue
                try:
                    speed = float(_read(path / 'speed'))
                    if not math.isfinite(speed) or not 0 < speed <= 100000:
                        raise ValueError('invalid_usb_speed')
                    result.append({'vendor_id': _hex(path / 'idVendor', 4),
                                   'product_id': _hex(path / 'idProduct', 4),
                                   'device_class': _hex(path / 'bDeviceClass', 2),
                                   'interface_classes': sorted(classes.get(path.name, ())),
                                   'speed_mbps': speed, 'bus': int(path.name.split('-')[0]),
                                   'port_path': path.name})
                except FileNotFoundError:
                    # A device can vanish between enumeration and its reads.
                    continue
                except (OSError, ValueError, UnicodeError):
                    return None
            return result
        except OSError:
            return None


class UEventMonitor:
    """Optional nonblocking, kernel-origin USB hints; five-second poll survives."""
    def __init__(self, socket_factory=socket.socket):
        self.factory, self.socket = socket_factory, None
        self._attempted = False

    def start(self):
        if self._attempted:
            return
        self._attempted = True
        sock = None
        try:
            family = getattr(socket, 'AF_NETLINK')
            sock = self.factory(family, socket.SOCK_DGRAM, 15)  # NETLINK_KOBJECT_UEVENT
            sock.setblocking(False)
            sock.bind((0, 1))
            self.socket = sock
        except (AttributeError, OSError):
            if sock is not None:
                sock.close()

    def changed(self):
        if self.socket is None:
            return False
        changed = False
        for _ in range(32):
            try:
                raw, sender = self.socket.recvfrom(4097)
            except BlockingIOError:
                return changed
            except OSError:
                self.close()
                return True  # Includes ENOBUFS: reconcile instead of trusting loss.
            if not sender or sender[0] != 0:
                continue
            if len(raw) > 4096:
                changed = True
                continue
            fields = raw.split(b'\0')
            if b'SUBSYSTEM=usb' in fields and any(b'ACTION=' + action in fields for action in (b'add', b'remove', b'change')):
                changed = True
        return True  # Bounded drain exhausted; next reconciliation handles backlog.

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None
