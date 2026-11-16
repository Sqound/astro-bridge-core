"""Read the local implementation's root-provisioned declaration for ASI 1.41."""
import hashlib
import json
from pathlib import Path
import re
import stat

from .driver import CameraError

DIRECTORY = Path('/var/lib/wifi-bridge/camera-drivers/zwo-1.41')
LIBRARY = 'libASICamera2.so.1.41'
MAX_LIBRARY = 32 * 1024 * 1024


def trusted(path, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if info.st_uid != 0 or info.st_mode & 0o022 or not kind(info.st_mode):
        raise ValueError('untrusted_installation')
    return info


def declared_path(directory=DIRECTORY):
    # Trust the platform installer (root only), never a discovery or client path.
    try:
        for ancestor in [*reversed(directory.parents), directory]:
            trusted(ancestor, directory=True)
        record = directory / 'platform.json'
        if trusted(record).st_size > 1024:
            raise ValueError
        data = json.loads(record.read_bytes())
        library = directory / LIBRARY
        if (not isinstance(data, dict) or set(data) != {'path', 'sha256'}
                or data['path'] != str(library) or not isinstance(data['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', data['sha256'])
                or not 0 < trusted(library).st_size <= MAX_LIBRARY):
            raise ValueError
        with library.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != data['sha256']:
            raise ValueError
        # A mismatched machine/class/ABI also fails eager loading in the child.
        return library
    except (ValueError, OSError, RecursionError):
        raise CameraError('camera_driver_unavailable') from None
