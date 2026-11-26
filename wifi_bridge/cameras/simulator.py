"""Deterministic local star-field camera; no network, USB, SDK or OS access.

The injected monotonic clock makes timing testable without sleeping. Exposure and
0.3-second readout deadlines are independent of polling. Pixels are rendered on
read_frame in the owning worker, so rendering CPU time adds to download latency.
Bayer mode uses RGGB and bin 1; monochrome supports symmetric bins up to four.
"""
from array import array
import math
import random
import sys
import time

from .driver import CameraDriver, CameraError, integer, validate_exposure, validate_info
from .frames import Frame


class SimulatedCamera(CameraDriver):
    READOUT_SECONDS = 0.3

    def __init__(self, *, seed=1, width=1280, height=960, bayer=False, clock=time.monotonic):
        if not integer(seed, 0, (1 << 32) - 1) or not integer(width, 1, 4096) or not integer(height, 1, 4096) or width * height > 4 * 1024 * 1024 or type(bayer) is not bool or not callable(clock):
            raise CameraError('invalid_simulator_options')
        self._seed = seed
        self._clock = clock
        self._info = validate_info({
            'name': 'simulator', 'sensor_name': 'synthetic_star_field',
            'width': width, 'height': height, 'pixel_um_x': 3.75, 'pixel_um_y': 3.75,
            'bits': 16, 'max_adu': 65535, 'sensor_type': 2 if bayer else 0,
            'bayer_offset': [0, 0], 'max_bin': 1 if bayer else min(4, width, height),
            'gain_min': 0, 'gain_max': 100, 'exposure_min': 0.001, 'exposure_max': 3600.0, 'exposure_resolution': 0.0,
            'can_abort': True, 'cooled': False})
        self._opened = False
        self._state = 'idle'
        self._pending = None
        self._sequence = 0
        self._stars, self._hot = self._make_scene(width, height)

    def _make_scene(self, width, height):
        rng = random.Random(self._seed)
        stars = {}
        for _ in range(min(160, max(8, width * height // 12000))):
            cx, cy = rng.uniform(0, width - 1), rng.uniform(0, height - 1)
            sigma, flux = rng.uniform(0.8, 2.0), rng.uniform(1000, 16000)
            radius = math.ceil(4 * sigma)
            for y in range(max(0, int(cy) - radius), min(height, int(cy) + radius + 1)):
                for x in range(max(0, int(cx) - radius), min(width, int(cx) + radius + 1)):
                    value = flux * math.exp(-((x - cx) ** 2 + (y - cy) ** 2) / (2 * sigma ** 2))
                    index = y * width + x
                    stars[index] = stars.get(index, 0.0) + value
        hot = set(rng.sample(range(width * height), min(width * height, max(1, width * height // 100000))))
        return stars, hot

    def open(self):
        if not self._opened:
            self._opened = True
            self._state = 'idle'

    def close(self):
        self._opened = False
        self._pending = None
        self._state = 'idle'

    def info(self):
        return validate_info(self._info)

    def _require_open(self):
        if not self._opened:
            raise CameraError('not_open')

    def start_exposure(self, seconds, bin=1, roi=None, gain=0, light=True):
        self._require_open()
        if self.state() in ('waiting', 'exposing', 'reading'):
            raise CameraError('busy')
        request = validate_exposure(self._info, seconds, bin, roi, gain, light)
        started = self._clock()
        self._pending = request
        self._exposure_end = started + request['seconds']
        self._download_at = self._exposure_end + self.READOUT_SECONDS
        self._sequence += 1
        self._state = 'exposing'

    def state(self):
        if self._pending is not None:
            now = self._clock()
            self._state = ('exposing' if now < self._exposure_end else
                           'reading' if now < self._download_at else 'download')
        return self._state

    def abort(self):
        self._require_open()
        self._pending = None
        self._state = 'idle'

    def read_frame(self):
        self._require_open()
        if self.state() != 'download':
            raise CameraError('not_ready')
        try:
            frame = self._render(self._pending)
        except Exception:
            self._pending = None
            self._state = 'error'
            raise CameraError('frame_failed') from None
        self._pending = None
        self._state = 'idle'
        return frame

    def _render(self, request):
        roi, bin = request['roi'], request['bin']
        width, height = roi['width'] // bin, roi['height'] // bin
        sensor_width = self._info['width']
        seconds = request['seconds']
        scale = 1 + request['gain'] / 20
        # Repeated identical requests have identical pixels, independent of polling,
        # abort history, other camera instances or host wall-clock time.
        rng = random.Random(self._seed)
        pixels = array('H')
        for y in range(roi['y'], roi['y'] + roi['height'], bin):
            for x in range(roi['x'], roi['x'] + roi['width'], bin):
                signal = 0.0
                for dy in range(bin):
                    for dx in range(bin):
                        index = (y + dy) * sensor_width + x + dx
                        value = 100 + seconds * 3
                        if request['light']:
                            colour = (0.8 if y % 2 == 0 and x % 2 == 0 else
                                      0.6 if y % 2 == 1 and x % 2 == 1 else 1.0)
                            value += self._stars.get(index, 0.0) * seconds * (colour if self._info['sensor_type'] == 2 else 1.0)
                        if index in self._hot:
                            value += 30000 + seconds * 1000
                        signal += value
                pixels.append(max(0, min(65535, round(signal * scale + rng.gauss(0, 8)))))
        if sys.byteorder != 'little':
            pixels.byteswap()
        metadata = {'exposure_seconds': seconds, 'bin': bin, 'roi_x': roi['x'], 'roi_y': roi['y'],
                    'gain': request['gain'], 'light': request['light'], 'sequence': self._sequence,
                    'seed': self._seed, 'sensor_type': self._info['sensor_type'],
                    'bayer_offset_x': roi['x'] % 2 if self._info['sensor_type'] else 0,
                    'bayer_offset_y': roi['y'] % 2 if self._info['sensor_type'] else 0}
        return Frame(width, height, 'UInt16', pixels.tobytes(), metadata)
