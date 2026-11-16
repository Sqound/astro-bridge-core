"""Shared atomic records and immutable app identity; no OS updater."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import re
import stat
ACTIVE_PHASES = ("prepared", "applying", "verifying")

LIVE_PLATFORMS = ('linux-alpine-x86_64', 'linux-alpine-aarch64')
NESTED_MODULES = {
    'cameras': '__init__ alpaca api calibration discovery driver frames listener native registry runtime service simulator usb uvc v4l2',
    'maker_keys': '__init__ api fetch rsa service store verify',
    'telemetry': '__init__ alpaca analysis api cards demo engineio link metrics query sessions settings store',
    'telemetry/adapters': '__init__ alpaca_observer seestar_events vespera_status',
}
NESTED_FILES = frozenset(
    f'{package}/{name}.py' for package, modules in NESTED_MODULES.items() for name in modules.split()
) | {'cameras/tables/simulator.json', 'cameras/tables/uvc.json'}
STATIC_SUFFIXES = {'.html', '.js', '.css', '.json', '.svg', '.png', '.ico', '.webmanifest'}

def live_platform(root=Path('/'), machine=None):
    """An immutable OS declaration, never an app preference or environment flag."""
    import platform as host
    machine = machine or host.machine()
    path = root / 'etc/wifi-bridge/ota-platform'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 64:
            raise ValueError('Invalid OS update platform declaration.')
        value = stream.read(65).decode().strip()
    expected = {'x86_64': LIVE_PLATFORMS[0], 'aarch64': LIVE_PLATFORMS[1]}.get(machine)
    if value != expected:
        raise ValueError('OS update platform does not match this architecture.')
    return value

def allowed_file(relative, nested):
    path = PurePosixPath(relative)
    if len(path.parts) == 1:
        return re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*\.py', relative) is not None
    if len(path.parts) == 2 and path.parts[0] == 'static':
        return path.suffix in STATIC_SUFFIXES and re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*', path.name) is not None
    if nested and path.parent.as_posix() in NESTED_MODULES:
        return re.fullmatch(r'(?:__init__|[a-z][a-z0-9_]{0,62})\.py', path.name) is not None
    if nested and path.parent.as_posix() == 'cameras/tables':
        return re.fullmatch(r'[a-z][a-z0-9_]{0,31}\.json', path.name) is not None
    return False

def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)

def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def atomic(path, value):
    atomic_bytes(path, json.dumps(value).encode())

def source_identity(files):
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        if name != 'wifi_bridge/build.py':
            digest.update(name.encode() + b'\0' + content + b'\0')
    return digest.hexdigest()

def tree_identity(path):
    return source_identity({'wifi_bridge/' + str(p.relative_to(path)): p.read_bytes()
                            for p in path.rglob('*') if p.is_file() and '__pycache__' not in p.parts})

def read(path, default):
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict): raise ValueError('Invalid updater record.')
        return value
    except FileNotFoundError:
        return default
