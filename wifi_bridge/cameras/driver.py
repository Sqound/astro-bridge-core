"""Camera driver contract; one owning worker calls each instance synchronously.

No method discovers USB devices or operates a remote instrument. Future platform
adapters provide reviewed factories; declarative tables never load executable code.
Errors carry internal machine codes, for translation by a future service boundary.
ROI coordinates and sizes use unbinned sensor pixels, with symmetric binning.
"""
from abc import ABC, abstractmethod
import math

from .frames import Frame, MAX_PIXELS

STATES = ('idle', 'waiting', 'exposing', 'reading', 'download', 'error')
INFO_FIELDS = ('name', 'sensor_name', 'width', 'height', 'pixel_um_x', 'pixel_um_y',
               'bits', 'max_adu', 'sensor_type', 'bayer_offset', 'max_bin',
               'gain_min', 'gain_max', 'exposure_min', 'exposure_max',
               'can_abort', 'cooled')


class CameraError(ValueError):
    """Internal, payload-free error; code is not presentation text."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def finite(value, minimum, maximum):
    return type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value)


def validate_info(value):
    """Validate a bounded, allowlisted driver description without retaining input."""
    # exposure_resolution (seconds) is optional: a driver that does not know its SDK unit leaves it out.
    optional = {'exposure_resolution', 'electrons_per_adu', 'full_well_capacity'}
    if not isinstance(value, dict) or set(value) - optional != set(INFO_FIELDS) or not set(INFO_FIELDS) <= set(value):
        raise CameraError('invalid_info')
    for key in ('name', 'sensor_name'):
        text = value[key]
        if not isinstance(text, str) or not 1 <= len(text) <= 64 or any(ord(c) < 32 for c in text):
            raise CameraError('invalid_info')
    for key in ('width', 'height'):
        if not integer(value[key], 1, 16384):
            raise CameraError('invalid_info')
    if value['width'] * value['height'] > MAX_PIXELS:
        raise CameraError('invalid_info')
    for key in ('pixel_um_x', 'pixel_um_y'):
        if not finite(value[key], 0.01, 1000):
            raise CameraError('invalid_info')
    if not integer(value['bits'], 1, 16) or not integer(value['max_adu'], 1, (1 << value['bits']) - 1):
        raise CameraError('invalid_info')
    if not integer(value['sensor_type'], 0, 5) or not integer(value['max_bin'], 1, 16):
        raise CameraError('invalid_info')
    offsets = value['bayer_offset']
    if not isinstance(offsets, (list, tuple)) or len(offsets) != 2 or any(not integer(v, 0, 1) for v in offsets):
        raise CameraError('invalid_info')
    if not integer(value['gain_min'], 0, 65535) or not integer(value['gain_max'], value['gain_min'], 65535):
        raise CameraError('invalid_info')
    if not finite(value['exposure_min'], 0.000001, 86400) or not finite(value['exposure_max'], value['exposure_min'], 86400):
        raise CameraError('invalid_info')
    if 'exposure_resolution' in value and not finite(value['exposure_resolution'], 0, value['exposure_max']):
        raise CameraError('invalid_info')
    # Only a value the SDK reports for the current mode; never derived or invented.
    for key in ('electrons_per_adu', 'full_well_capacity'):
        if key in value and not finite(value[key], 1e-6, 1e9):
            raise CameraError('invalid_info')
    if type(value['can_abort']) is not bool or type(value['cooled']) is not bool:
        raise CameraError('invalid_info')
    result = dict(value)
    result['bayer_offset'] = list(offsets)
    return result


def validate_exposure(info, seconds, bin, roi, gain, light):
    """Return a normalized exposure; invalid requests leave driver state intact."""
    if not finite(seconds, info['exposure_min'], info['exposure_max']):
        raise CameraError('invalid_exposure')
    if not integer(bin, 1, info['max_bin']) or not integer(gain, info['gain_min'], info['gain_max']) or type(light) is not bool:
        raise CameraError('invalid_exposure')
    if roi is None:
        roi = {'x': 0, 'y': 0, 'width': info['width'], 'height': info['height']}
    if not isinstance(roi, dict) or set(roi) != {'x', 'y', 'width', 'height'}:
        raise CameraError('invalid_roi')
    for key, maximum in (('x', info['width'] - 1), ('y', info['height'] - 1),
                         ('width', info['width']), ('height', info['height'])):
        if not integer(roi[key], 0 if key in ('x', 'y') else 1, maximum) or roi[key] % bin:
            raise CameraError('invalid_roi')
    if roi['x'] + roi['width'] > info['width'] or roi['y'] + roi['height'] > info['height']:
        raise CameraError('invalid_roi')
    return {'seconds': float(seconds), 'bin': bin, 'roi': dict(roi), 'gain': gain, 'light': light}


class CameraDriver(ABC):
    """No internal worker is started; calls belong to one external worker thread.

    open/close are idempotent. start_exposure may replace a completed download,
    never an active exposure. read_frame requires download and consumes the frame.
    abort discards an exposure/download; close discards all pending image data.
    """

    @abstractmethod
    def open(self): ...

    @abstractmethod
    def close(self): ...

    @abstractmethod
    def info(self) -> dict: ...

    @abstractmethod
    def start_exposure(self, seconds, bin, roi, gain, light): ...

    @abstractmethod
    def state(self) -> str: ...

    @abstractmethod
    def abort(self): ...

    @abstractmethod
    def read_frame(self) -> Frame: ...
