"""Direct SDK loading for trusted camera driver code on Linux USB platforms."""
import ctypes
import os
from pathlib import Path
import sys

from .driver import CameraError


def load_platform_sdk(path):
    """Load the exact file supplied by a platform after its tier and ABI checks.

    Only a reviewed driver calls this in its owning process. The platform
    verifies the installation, controls its directory and declares the path.
    Discovery, descriptors and client requests must never supply it.
    """
    if sys.platform != 'linux' or not isinstance(path, (str, Path)):
        raise CameraError('camera_driver_unavailable')
    name = str(path)
    library = Path(name)
    if (len(name) > 4096 or '\0' in name or not library.is_absolute()
            or '..' in library.parts):
        raise CameraError('camera_driver_unavailable')

    try:
        if library.is_file():
            return ctypes.CDLL(name, mode=os.RTLD_NOW | os.RTLD_LOCAL)
    except OSError:
        pass
    # Avoid exposing host paths, native diagnostics or fallback search paths.
    raise CameraError('camera_driver_unavailable')
