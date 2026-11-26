"""Find the ZWO ASI SDK library on this host and read its version.

The library is never part of an image. It is installed by the owner into a
root-owned folder under the bridge state, by one of:

- the common manufacturer installer (drivers.py): Astro Bridge Setup at first
  boot, or Install on the Cameras page. It also writes the owner's acceptance
  record, and the library is used only while that record names the same library;
- the owner-run installer script (install-zwo.py), which shows the complete
  licence and requires its acceptance before it installs anything.

The library is found by its base name; its version is read from the file name.
No version is required: an untested version still loads, with a warning.
"""
import hashlib
import json
from pathlib import Path
import re
import sys
import threading

from .driver import CameraError
from . import drivers as common
from .drivers import trusted, small_json, sha256_text

STATE = Path('/var/lib/wifi-bridge')
DRIVERS = STATE / 'camera-drivers'
# Owner-installed SDK. Earlier installers used a versioned directory name.
DIRECTORY = DRIVERS / 'zwo'
BASE_NAME = 'libASICamera2.so'
LIBRARY_NAME = re.compile(r'libASICamera2\.so\.([0-9]{1,3}(?:\.[0-9]{1,4}){1,2})')
# Versions that served a real camera through this driver. Others load with a warning.
TESTED_VERSIONS = ('1.41',)
MAX_LIBRARY = 32 * 1024 * 1024
_warned = set()
_warn_lock = threading.Lock()


def trusted_tree(directory):
    for ancestor in [*reversed(directory.parents), directory]:
        trusted(ancestor, directory=True)


def library_version(name):
    match = LIBRARY_NAME.fullmatch(name)
    return match[1] if match else None


def digest(path, limit):
    if not 0 < trusted(path).st_size <= limit:
        raise ValueError('invalid_size')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def installed(directory, drivers=DRIVERS):
    """An installed library declared by its root-only platform record.

    An installation by the common installer (it leaves driver.json) is used only
    while the owner's acceptance record names the same library, so Deactivate
    works as for every manufacturer. An installation by the owner-run script
    needs no record: that script already required the licence.
    """
    trusted_tree(directory)
    data = small_json(directory / 'platform.json')
    path = Path(data.get('path', '')) if isinstance(data.get('path'), str) else None
    if (set(data) != {'path', 'sha256'} or path is None or path.parent != directory
            or library_version(path.name) is None or not sha256_text(data['sha256'])
            or digest(path, MAX_LIBRARY) != data['sha256']):
        raise ValueError('invalid_installation')
    if (directory / 'driver.json').exists():
        record = common.read_acceptance(common.acceptance_path('zwo_asi', drivers), 'zwo_asi')
        if record is None or record['library_sha256'] != data['sha256']:
            raise ValueError('not_accepted')
    return {'source': 'installed', 'path': path, 'version': library_version(path.name), 'sha256': data['sha256']}


def installations(drivers=DRIVERS):
    found = []
    try:
        candidates = sorted(p for p in drivers.iterdir() if p.name == 'zwo' or p.name.startswith('zwo-'))
    except OSError:
        return found
    for candidate in candidates:
        try:
            found.append(installed(candidate, drivers))
        except (ValueError, OSError, RecursionError, json.JSONDecodeError, UnicodeError):
            continue
    return found


def resolve(drivers=DRIVERS):
    """Return the library to load, or raise camera_driver_unavailable."""
    found = installations(drivers)
    if not found:
        raise CameraError('camera_driver_unavailable')
    return max(found, key=lambda item: tuple(int(part) for part in item['version'].split('.')))


def declared_path(drivers=DRIVERS):
    # Trust only the platform layout above, never a discovery or client path.
    # A mismatched machine/class/ABI also fails eager loading in the child.
    found = resolve(drivers)
    warn_untested(found['version'])
    return found['path']


def warn_untested(version):
    if version in TESTED_VERSIONS:
        return
    with _warn_lock:
        if version in _warned:
            return
        _warned.add(version)
    print(f'Astro Bridge camera: ZWO ASI SDK {version} has not been tested with this release '
          f'(tested: {", ".join(TESTED_VERSIONS)}). It is used anyway.', file=sys.stderr, flush=True)
