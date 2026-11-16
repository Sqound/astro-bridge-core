"""Standard-library serial access (termios) and USB serial adapter inventory.

No pyserial. A saved configuration refers to an adapter by a stable udev link
(/dev/serial/by-id or by-path), never to /dev/ttyUSBn, whose number depends on
the plug order. Tests point the roots at temporary trees and pseudo-terminals.
"""
import errno
import fcntl
import os
from pathlib import Path
import re
import stat
import termios

# Test hooks: a sandbox tree and pseudo-terminals instead of the real /dev and /sys.
DEV_ROOT = os.environ.get('ASTRO_BRIDGE_SERIAL_DEV_ROOT', '/dev/serial')
SYS_ROOT = os.environ.get('ASTRO_BRIDGE_SERIAL_SYS_ROOT', '/sys')
NODE_PATTERN = os.environ.get('ASTRO_BRIDGE_SERIAL_NODE_PATTERN', r'tty(?:USB|ACM)[0-9]+')
STABLE_NAME = r'[A-Za-z0-9._:+@=,-]{1,200}'


def is_stable_path(path, root=None):
    root = root or DEV_ROOT
    return isinstance(path, str) and any(re.fullmatch(re.escape(root) + '/by-' + kind + '/' + STABLE_NAME, path) for kind in ('id', 'path'))


class PortBusy(OSError):
    pass


def open_serial(path, baud=9600, fmt='8N1'):
    """Open raw, no flow control, exclusive (flock). Returns a non-blocking fd."""
    node = os.path.realpath(path)
    fd = os.open(node, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        if not stat.S_ISCHR(os.fstat(fd).st_mode): raise OSError(errno.ENOTTY, 'Not a character device')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise PortBusy(errno.EBUSY, 'Serial port already in use') from None
        except OSError: pass
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[1] = attrs[3] = 0
        speed = getattr(termios, 'B%d' % baud)
        attrs[4] = attrs[5] = speed
        bits = {'7': termios.CS7, '8': termios.CS8}[fmt[0]]
        control = termios.CREAD | termios.CLOCAL | bits
        if fmt[1] != 'N': control |= termios.PARENB | (termios.PARODD if fmt[1] == 'O' else 0)
        if fmt[2] == '2': control |= termios.CSTOPB
        attrs[2] = control
        attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 0, 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        return fd
    except BaseException:
        os.close(fd); raise


def _read(path):
    try: return Path(path).read_text(errors='replace').strip()
    except OSError: return None


def usb_info(node_name, sys_root=None):
    """Manufacturer, product, ids and serial number of the USB device behind a tty."""
    sys_root = Path(sys_root or SYS_ROOT)
    try: here = (sys_root / 'class' / 'tty' / node_name / 'device').resolve()
    except OSError: return {}
    for directory in (here, *here.parents):
        if (directory / 'idVendor').is_file():
            return {'vendor_id': _read(directory / 'idVendor'), 'product_id': _read(directory / 'idProduct'),
                    'manufacturer': _read(directory / 'manufacturer'), 'product': _read(directory / 'product'),
                    'serial_number': _read(directory / 'serial')}
        if directory == sys_root or len(directory.parts) < 3: break
    return {}


def inventory(dev_root=None, sys_root=None, node_pattern=None):
    """Serial adapters currently present, one row per device node.

    A cheap listing of two directories: suitable for a Pi Zero W every few seconds.
    `stable_path` prefers by-id when the adapter has a serial number (unique);
    otherwise by-path, tied to the USB socket.
    """
    root = Path(dev_root or DEV_ROOT)
    pattern = re.compile(node_pattern or NODE_PATTERN)
    nodes = {}
    for kind in ('id', 'path'):
        directory = root / ('by-' + kind)
        try: names = sorted(os.listdir(directory))
        except OSError: continue
        for name in names:
            link = directory / name
            try: node = os.path.realpath(link)
            except OSError: continue
            if not pattern.fullmatch(os.path.basename(node)) or not os.path.exists(node): continue
            nodes.setdefault(node, {'by-id': [], 'by-path': []})['by-' + kind].append(str(link))
    rows = []
    for node, links in sorted(nodes.items()):
        info = usb_info(os.path.basename(node), sys_root)
        serial = info.get('serial_number')
        # by-id is only unique when the adapter reports a serial number.
        by_id = links['by-id'][0] if links['by-id'] and (serial or not info) else None
        by_path = links['by-path'][0] if links['by-path'] else None
        stable = by_id or by_path
        rows.append({'node': node, 'stable_path': stable,
                     'stable_kind': 'by-id' if stable and stable == by_id else 'by-path' if stable else None,
                     'by_id': links['by-id'][0] if links['by-id'] else None, 'by_path': by_path, **info})
    return rows
