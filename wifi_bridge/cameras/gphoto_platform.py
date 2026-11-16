"""Pi 5 gPhoto launcher and owner-supplied sensor profile."""
import json
import os
from pathlib import Path
import stat
import subprocess

from .calibration import MAX_BYTES, _unique
from .driver import CameraError
from .usb import SysfsUSB, _read


def ptp(device):
    classes = [device['device_class'], *device['interface_classes']]
    return 6 in classes and not any(c in (2, 10, 224) for c in classes)


def profile(path, ident):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022:
            raise CameraError('camera_unsupported')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise CameraError('camera_unsupported')
        value = json.loads(raw, object_pairs_hook=_unique)
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'cameras'} or
                type(value['schema_version']) is not int or value['schema_version'] != 1 or
                not isinstance(value['cameras'], dict) or len(value['cameras']) > 32):
            raise CameraError('camera_unsupported')
        result = value['cameras'].get(ident)
        if not isinstance(result, dict):
            raise CameraError('camera_unsupported')
        return validate_partial(result)
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise CameraError('camera_unsupported') from None
    finally:
        if fd is not None:
            os.close(fd)


def launch(device):
    # The scan is repeated immediately before opening. Never trust a stale
    # discovery snapshot or hand an unrelated PTP camera to libgphoto2.
    devices = SysfsUSB().scan()
    if devices is None or [d for d in devices if ptp(d)] != [device]:
        raise CameraError('camera_changed')
    node_path = Path('/sys/bus/usb/devices') / device['port_path']
    attrs = node_path.resolve(strict=True)
    if not attrs.is_relative_to('/sys/devices') or attrs.name != device['port_path']:
        raise CameraError('camera_changed')
    bus = int(_read(node_path / 'busnum'))
    number = int(_read(node_path / 'devnum'))
    if (bus != device['bus'] or not 1 <= number <= 127 or
            int(_read(node_path / 'idVendor'), 16) != device['vendor_id'] or
            int(_read(node_path / 'idProduct'), 16) != device['product_id']):
        raise CameraError('camera_changed')
    node = Path('/dev/bus/usb') / f'{bus:03}' / f'{number:03}'
    info = node.lstat()
    if (not stat.S_ISCHR(info.st_mode) or info.st_uid != 0 or
            (os.major(info.st_rdev), os.minor(info.st_rdev)) != (189, (bus - 1) * 128 + number - 1)):
        raise CameraError('camera_changed')
    for binary in ('/usr/bin/bwrap', '/usr/bin/indi_gphoto_ccd'):
        if not os.access(binary, os.X_OK):
            raise CameraError('camera_driver_unavailable')
    link = '/sys/bus/usb/devices/' + device['port_path']
    command = ['/usr/bin/bwrap', '--unshare-user', '--disable-userns', '--unshare-net',
               '--unshare-pid', '--as-pid-1', '--unshare-ipc', '--unshare-uts',
               '--die-with-parent', '--new-session', '--cap-drop', 'ALL', '--clearenv',
               '--setenv', 'PATH', '/usr/bin', '--setenv', 'HOME', '/tmp',
               '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib',
               '--proc', '/proc', '--dev', '/dev', '--size', '67108864', '--tmpfs', '/tmp',
               '--ro-bind', str(attrs), str(attrs), '--symlink', str(attrs), link,
               '--dev-bind', str(node), str(node), '--remount-ro', '/dev', '--remount-ro', '/',
               '/usr/bin/indi_gphoto_ccd']
    return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, cwd='/', env={'PATH': '/usr/bin'})

PROFILE_FIELDS = ('name', 'width', 'height', 'pixel_um_x', 'pixel_um_y', 'bits',
                  'exposure_min', 'exposure_max', 'bayer_pattern')


def validate_partial(values):
    from .driver import finite, integer
    if not isinstance(values, dict) or set(values) - set(PROFILE_FIELDS):
        raise ValueError('invalid_profile')
    for key, value in values.items():
        valid = False
        if key == 'name':
            valid = isinstance(value, str) and 1 <= len(value) <= 64 and all(32 <= ord(c) != 127 for c in value)
        elif key in ('width', 'height'):
            valid = integer(value, 1, 16384)
        elif key in ('pixel_um_x', 'pixel_um_y'):
            valid = finite(value, .01, 1000)
        elif key == 'bits':
            valid = type(value) is int and value in (8, 16)
        elif key in ('exposure_min', 'exposure_max'):
            valid = finite(value, .000001, 4294)
        elif key == 'bayer_pattern':
            valid = value in ('', 'RGGB', 'BGGR', 'GRBG', 'GBRG')
        if not valid:
            raise ValueError('invalid_profile')
    if 'width' in values and 'height' in values and values['width'] * values['height'] > 16 * 1024 * 1024:
        raise ValueError('invalid_profile')
    if 'exposure_min' in values and 'exposure_max' in values and values['exposure_min'] > values['exposure_max']:
        raise ValueError('invalid_profile')
    return dict(values)


def save_profile(path, ident, values):
    """Atomically preserve existing profiles; never follow a profile symlink."""
    import tempfile
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        value = {'schema_version': 1, 'cameras': {}}
    else:
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022:
                raise ValueError('invalid_profile_file')
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('invalid_profile_file')
        value = json.loads(raw, object_pairs_hook=_unique)
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'cameras'} or
                value['schema_version'] != 1 or not isinstance(value['cameras'], dict) or len(value['cameras']) > 32):
            raise ValueError('invalid_profile_file')
    if ident not in value['cameras'] and len(value['cameras']) >= 32:
        raise ValueError('invalid_profile_file')
    value['cameras'][ident] = validate_partial(values)
    raw = json.dumps(value, separators=(',', ':')).encode()
    if len(raw) > MAX_BYTES:
        raise ValueError('invalid_profile_file')
    fd, temporary = tempfile.mkstemp(prefix='.camera-gphoto-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def probe(device, cancel):
    """Bounded isolated INDI read-only discovery, no exposure or configuration."""
    import select
    import time
    from .gphoto import _Xml, _write, _write_raw
    found, proc = {}, None
    try:
        # sysfs product is optional, sanitized and never includes a serial.
        product = _read(Path('/sys/bus/usb/devices') / device['port_path'] / 'product')
        if product and len(product) <= 64 and all(32 <= ord(c) < 127 for c in product):
            found['name'] = product
    except (OSError, ValueError, UnicodeError):
        pass
    try:
        proc = launch(device)
        os.set_blocking(proc.stdout.fileno(), False)
        os.set_blocking(proc.stdin.fileno(), False)
        _write_raw(proc, b'<getProperties version="1.7"/>\n')
        xml, indi_device, connected = _Xml(), None, False
        deadline = time.monotonic() + 5
        while not cancel.is_set() and time.monotonic() < deadline and proc.poll() is None:
            if not select.select([proc.stdout], [], [], .1)[0]:
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            for tag, attrs, elements in xml.feed(chunk):
                name = attrs.get('name')
                if name == 'CONNECTION' and tag == 'defSwitchVector' and indi_device is None:
                    indi_device = attrs.get('device')
                    if not indi_device or len(indi_device) > 128 or 'CONNECT' not in elements or 'DISCONNECT' not in elements:
                        raise ValueError('invalid_connection')
                    _write(proc, 'newSwitchVector', indi_device, 'CONNECTION', {'CONNECT': 'On', 'DISCONNECT': 'Off'})
                elif attrs.get('device') == indi_device and name == 'CONNECTION' and tag == 'setSwitchVector':
                    connected = attrs.get('state') == 'Ok' and elements.get('CONNECT', ({}, ''))[1].strip() == 'On'
                elif connected and attrs.get('device') == indi_device and name == 'CCD_INFO' and tag in ('defNumberVector', 'setNumberVector') and attrs.get('state') == 'Ok':
                    for source, target in (('CCD_MAX_X', 'width'), ('CCD_MAX_Y', 'height'), ('CCD_PIXEL_SIZE_X', 'pixel_um_x'), ('CCD_PIXEL_SIZE_Y', 'pixel_um_y')):
                        try:
                            number = float(elements[source][1])
                            candidate = int(number) if target in ('width', 'height') and number.is_integer() else number
                            found.update(validate_partial({target: candidate}))
                        except (KeyError, ValueError, OverflowError):
                            pass
                elif connected and attrs.get('device') == indi_device and name == 'CCD_EXPOSURE' and tag == 'defNumberVector':
                    try:
                        meta = elements['CCD_EXPOSURE_VALUE'][0]
                        found.update(validate_partial({'exposure_min': float(meta['min']), 'exposure_max': float(meta['max'])}))
                    except (KeyError, ValueError, OverflowError):
                        pass
    except Exception:
        pass
    finally:
        if proc is not None:
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            while proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=.2)
                except Exception:
                    # Keep the job retiring until the child really exits; no
                    # other camera process may take over this USB attachment.
                    time.sleep(.1)
    return found


def attachment_token(device):
    """Recheck sole PTP attachment and capture its kernel device number."""
    try:
        devices = SysfsUSB().scan()
        if devices is None or [entry for entry in devices if ptp(entry)] != [device]:
            return None
        path = Path('/sys/bus/usb/devices') / device['port_path']
        number = int(_read(path / 'devnum'))
        if (not 1 <= number <= 127 or int(_read(path / 'busnum')) != device['bus'] or
                int(_read(path / 'idVendor'), 16) != device['vendor_id'] or
                int(_read(path / 'idProduct'), 16) != device['product_id']):
            return None
        return number
    except (OSError, ValueError, UnicodeError):
        return None
