"""Optional Linux Services entry. An unconfined indiserver is never executed."""
import os
from pathlib import Path
import socket
import stat
import subprocess

from .usb import validate_device, _read

DRIVER = 'indi_asi_ccd'
PORT = 7624


def usb_node(device, gid, sysfs=Path('/sys/bus/usb/devices'), usbfs=Path('/dev/bus/usb')):
    device = validate_device(device)
    path = sysfs / device['port_path']
    bus, number = int(_read(path / 'busnum')), int(_read(path / 'devnum'))
    if (bus != device['bus'] or not 1 <= number <= 127 or
            int(_read(path / 'idVendor'), 16) != device['vendor_id'] or
            int(_read(path / 'idProduct'), 16) != device['product_id']):
        raise ValueError('camera_changed')
    node = usbfs / f'{bus:03}' / f'{number:03}'
    info = node.lstat()
    if not stat.S_ISCHR(info.st_mode) or (os.major(info.st_rdev), os.minor(info.st_rdev)) != (189, (bus-1)*128+number-1):
        raise ValueError('invalid_usb_node')
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, gid, 0o660):
        raise ValueError('invalid_indi_usb_permissions')
    return node


def sandbox_command(device_node, device, attributes):
    # Debian merged-/usr layout. The private root contains no host state or
    # host sockets. Upstream's wildcard TCP and Linux abstract Unix socket
    # remain entirely inside this new network namespace (loopback only).
    link = '/sys/bus/usb/devices/' + device['port_path']
    return ['/usr/bin/bwrap', '--unshare-user', '--disable-userns', '--unshare-net', '--unshare-pid', '--as-pid-1', '--unshare-ipc',
            '--unshare-uts', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
            '--clearenv', '--setenv', 'PATH', '/usr/bin',
            '--setenv', 'HOME', '/tmp', '--ro-bind', '/usr', '/usr',
            '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
            '--proc', '/proc', '--dev', '/dev', '--dir', '/tmp',
            '--ro-bind', str(attributes), str(attributes), '--symlink', str(attributes), link,
            '--dev-bind', str(device_node), str(device_node),
            '--ro-bind', str(Path(__file__).with_name('indi_worker.py').resolve()), '/worker.py',
            '--remount-ro', '/dev', '--remount-ro', '/',
            '/usr/bin/python3', '-I', '/worker.py', str(device_node), device['port_path'],
            str(device['vendor_id']), str(device['product_id'])]


class LinuxIndi:
    name = 'indi'

    def listen(self, access):
        if not access.interface or access.subnet.prefixlen == 0:
            raise ValueError('unconfirmed_management_interface')
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, access.interface.encode('ascii') + b'\0')
            listener.bind((access.address, PORT))
            listener.listen(8)
            return listener
        except BaseException:
            listener.close()
            raise

    def launch(self, driver, device, child):
        import grp
        import pwd
        if driver != DRIVER or device['vendor_id'] != 0x03c3:
            raise ValueError('invalid_indi_driver')
        for path in ('/usr/bin/indiserver', '/usr/bin/indi_asi_ccd', '/usr/bin/bwrap', '/usr/bin/python3'):
            if not os.access(path, os.X_OK):
                raise OSError('missing_indi_package')
        account = pwd.getpwnam('astro-indi')
        if account.pw_uid == 0 or account.pw_gid == 0 or grp.getgrgid(account.pw_gid).gr_name != 'astro-indi':
            raise ValueError('invalid_indi_account')
        node = usb_node(device, account.pw_gid)
        attributes = (Path('/sys/bus/usb/devices') / device['port_path']).resolve(strict=True)
        if not attributes.is_relative_to('/sys/devices') or attributes.name != device['port_path']:
            raise ValueError('invalid_indi_sysfs')
        return subprocess.Popen(sandbox_command(node, device, attributes),
                                stdin=child, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                user=account.pw_uid, group=account.pw_gid, extra_groups=[], cwd='/', env={'PATH': '/usr/bin'})
