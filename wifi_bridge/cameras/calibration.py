"""Explicit owner-provided physical metadata; no discovery, guesses or live edits.

The service reads this state-local file only while preparing a newly attached
camera on its worker. Missing/invalid metadata never authorizes device access.
"""
import json
import os
from pathlib import Path
import re
import stat

from .driver import CameraError, finite

MAX_BYTES = 8192
MAX_CAMERAS = 32


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise CameraError('camera_unsupported')
        value[key] = item
    return value


def validate(value):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'cameras'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['cameras'], dict) or len(value['cameras']) > MAX_CAMERAS):
        raise CameraError('camera_unsupported')
    result = {}
    for ident, item in value['cameras'].items():
        if (not isinstance(ident, str) or not re.fullmatch(r'usb-[0-9a-f]{24}', ident)
                or not isinstance(item, dict) or set(item) != {'pixel_um'}):
            raise CameraError('camera_unsupported')
        pitch = item['pixel_um']
        if (not isinstance(pitch, list) or len(pitch) != 2
                or any(not finite(number, .01, 1000) for number in pitch)):
            raise CameraError('camera_unsupported')
        result[ident] = tuple(pitch)
    return result


class CameraCalibration:
    def __init__(self, path):
        self.path = Path(path)

    def pitch(self, ident):
        """Read once per new attachment; replug/restart applies owner changes."""
        fd = None
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise CameraError('camera_unsupported')
            with os.fdopen(fd, 'rb') as stream:
                fd = None
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise CameraError('camera_unsupported')
            return validate(json.loads(raw, object_pairs_hook=_unique)).get(ident)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError, RecursionError):
            raise CameraError('camera_unsupported') from None
        finally:
            if fd is not None:
                os.close(fd)
