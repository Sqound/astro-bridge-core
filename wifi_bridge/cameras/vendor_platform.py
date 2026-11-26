"""The library of a manufacturer other than ZWO, when it is installed and accepted.

A manufacturer library is never part of the image: it is installed by the
owner (Astro Bridge Setup at first boot, or Install on the Cameras page) into
/var/lib/wifi-bridge/camera-drivers/<manufacturer>/ by drivers.py. It is used
only when all of these hold, otherwise its cameras report
camera_driver_unavailable and the bridge runs on:

- the installation folder and its parents are root-owned and not writable by others;
- platform.json names a library in that folder, and the file has that SHA-256;
- the owner's acceptance record names the same library SHA-256.

Nothing here loads the library: only the child process of an opened camera does.
"""
import hashlib
import json
from pathlib import Path

from . import drivers as common
from .driver import CameraError

MAX_LIBRARY = common.MAX_LIBRARY


def _trusted_tree(directory):
    for ancestor in [*reversed(directory.parents), directory]:
        common.trusted(ancestor, directory=True)


def _digest(path):
    if not 0 < common.trusted(path).st_size <= MAX_LIBRARY:
        raise ValueError('invalid_size')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def resolve(vendor, drivers=None):
    """{'path', 'sha256', 'version'} of the accepted installation, or camera_driver_unavailable."""
    drivers = Path(drivers) if drivers is not None else common.DRIVERS
    try:
        found = common.installed_record(vendor, drivers)
        if found is None:
            raise ValueError('not_installed')
        _trusted_tree(found['path'].parent)
        if _digest(found['path']) != found['sha256']:
            raise ValueError('library_changed')
        record = common.read_acceptance(common.acceptance_path(vendor, drivers), vendor)
        if record is None or record['library_sha256'] != found['sha256']:
            raise ValueError('not_accepted')
    except (ValueError, OSError, RecursionError, json.JSONDecodeError, UnicodeError):
        raise CameraError('camera_driver_unavailable') from None
    return found


def declared_path(vendor, drivers=None):
    # Trust only the installation records above, never a discovery or client path.
    return resolve(vendor, drivers)['path']
