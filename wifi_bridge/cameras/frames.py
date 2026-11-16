"""Bounded immutable images and Alpaca ordering, without transport dependencies.

Frame data is packed row-major bytes, UInt16 little-endian or Byte. The converted
array has columns contiguous: index = x * height + y (height varies fastest).
Only normalized scalar metadata is accepted; raw device replies are excluded.
"""
from array import array
from dataclasses import dataclass
import math
from types import MappingProxyType
import statistics
import sys
import time

MAX_PIXELS = 16 * 1024 * 1024
_METADATA = {'exposure_seconds', 'bin', 'roi_x', 'roi_y', 'gain', 'light',
             'sequence', 'seed', 'sensor_type', 'bayer_offset_x', 'bayer_offset_y',
             'stacked_frames', 'frame_seconds'}


@dataclass(frozen=True)
class Frame:
    width: int
    height: int
    element_type: str
    data: bytes
    metadata: dict

    def __post_init__(self):
        if type(self.width) is not int or type(self.height) is not int or not 1 <= self.width <= 16384 or not 1 <= self.height <= 16384 or self.width * self.height > MAX_PIXELS:
            raise ValueError('invalid_frame_dimensions')
        if self.element_type not in ('UInt16', 'Byte'):
            raise ValueError('invalid_frame_type')
        size = 2 if self.element_type == 'UInt16' else 1
        if not isinstance(self.data, bytes) or len(self.data) != self.width * self.height * size:
            raise ValueError('invalid_frame_data')
        if not isinstance(self.metadata, dict) or set(self.metadata) - _METADATA:
            raise ValueError('invalid_frame_metadata')
        for value in self.metadata.values():
            if type(value) not in (int, float, bool) or (type(value) is float and not math.isfinite(value)) or (type(value) is int and not -(1 << 63) <= value < (1 << 63)):
                raise ValueError('invalid_frame_metadata')
        object.__setattr__(self, 'metadata', MappingProxyType(dict(self.metadata)))


def column_major(frame):
    """Return native-endian values, never mutate the original packed frame."""
    code = 'H' if frame.element_type == 'UInt16' else 'B'
    source = array(code)
    source.frombytes(frame.data)
    if code == 'H' and sys.byteorder != 'little':
        source.byteswap()
    result = array(code, [0]) * (frame.width * frame.height)
    for x in range(frame.width):
        result[x * frame.height:(x + 1) * frame.height] = source[x::frame.width]
    return result


def benchmark_transpose(iterations=5):
    """Measure only packed-frame to column-major conversion at 1280 by 960."""
    if type(iterations) is not int or not 1 <= iterations <= 20:
        raise ValueError('invalid_benchmark_iterations')
    frame = Frame(1280, 960, 'UInt16', bytes(1280 * 960 * 2), {})
    column_major(frame)  # Warm allocator/cache; excluded from measurements.
    samples = []
    for _ in range(iterations):
        started = time.perf_counter()
        result = column_major(frame)
        samples.append((time.perf_counter() - started) * 1000)
        del result
    return {'width': frame.width, 'height': frame.height, 'element_type': frame.element_type,
            'iterations': iterations, 'median_ms': statistics.median(samples),
            'min_ms': min(samples), 'max_ms': max(samples)}
