"""Attached UVC luminance camera, with bounded nonblocking stacked frames.

Only the owning camera worker invokes this driver. No threads, network access,
SDK, shell, device payload logging or image persistence. Construction is inert;
info probes explicitly on that worker and closes its temporary descriptor.
"""
from array import array
import errno
import math
import sys
import time

from .driver import CameraDriver, CameraError, finite, validate_exposure, validate_info
from .frames import Frame
from . import v4l2 as V

MAX_PIXELS = 1280 * 960
MAX_STACK = 256
MAX_PIXEL_WORK = 128 * 1024 * 1024
MAX_SECONDS = 3600
MAX_ENUM = 64


def _optional(io, fd, name, value):
    try:
        io.ioctl(fd, name, value)
        return value
    except OSError as error:
        if error.errno not in (errno.EINVAL, errno.ENOTTY):
            raise
        return None


def _control(io, fd, ident, kind=1):
    value = _optional(io, fd, 'QUERYCTRL', V.QueryControl(id=ident))
    if value is None or value.flags & (1 | 2 | 4 | 64) or value.type != kind:
        return None  # Disabled/grabbed/read-only/write-only; inactive may become manual.
    if not 0 <= value.minimum <= value.maximum <= 65535 or value.step < 1:
        return None
    return value


def _set_control(io, fd, ident, value):
    io.ioctl(fd, 'S_CTRL', V.Control(id=ident, value=value))
    actual = V.Control(id=ident)
    io.ioctl(fd, 'G_CTRL', actual)
    if actual.value != value:
        raise CameraError('camera_unsupported')


def luminance(raw, width, height, stride, pixel_format):
    """Strip row padding; preserve Y16 little-endian and unscaled YUYV luma."""
    size = 1 if pixel_format == V.GREY else 2
    if pixel_format not in (V.GREY, V.Y16, V.YUYV) or width * height > MAX_PIXELS or min(width, height) < 1:
        raise CameraError('invalid_frame')
    row_bytes = width * size
    if stride < row_bytes or len(raw) < stride * (height - 1) + row_bytes or (pixel_format == V.YUYV and width % 2):
        raise CameraError('invalid_frame')
    rows = [raw[y * stride:y * stride + row_bytes] for y in range(height)]
    packed = b''.join(row[::2] if pixel_format == V.YUYV else row for row in rows)
    pixels = array('H' if pixel_format == V.Y16 else 'B')
    pixels.frombytes(packed)
    if pixel_format == V.Y16 and sys.byteorder != 'little':
        pixels.byteswap()
    return pixels


class UVCCamera(CameraDriver):
    def __init__(self, *, device, pixel_um=None, io=None, clock=time.monotonic):
        from .usb import validate_device
        self.device = validate_device(device)
        if pixel_um is not None and (not isinstance(pixel_um, (tuple, list)) or len(pixel_um) != 2
                or any(not finite(value, 0.01, 1000) for value in pixel_um)):
            raise CameraError('invalid_info')
        self.pixel_um = tuple(pixel_um) if pixel_um is not None else None
        self.io = io if io is not None else V.LinuxVideoIO()
        self.clock = clock
        self.fd = None
        self._info = None
        self._format = None
        self._maps = []
        self._allocated = False
        self._streaming = False
        self._state = 'idle'
        self._sum = self._pending = None
        self._sequence = 0

    def _formats(self, fd):
        formats = []
        for index in range(MAX_ENUM):
            item = _optional(self.io, fd, 'ENUM_FMT', V.FormatDescription(index=index, type=V.CAPTURE))
            if item is None:
                break
            formats.append(item.pixelformat)
        else:
            raise CameraError('camera_unsupported')
        return [value for value in (V.GREY, V.Y16, V.YUYV) if value in formats]

    def _sizes(self, fd, pixel_format):
        sizes = []
        for index in range(MAX_ENUM):
            item = _optional(self.io, fd, 'ENUM_FRAMESIZES', V.FrameSize(index=index, pixel_format=pixel_format))
            if item is None:
                break
            if item.type == 1:
                width, height = item.sizes[:2]
                if 0 < width <= 1280 and 0 < height <= 960 and width * height <= MAX_PIXELS:
                    sizes.append((width, height))
            elif item.type in (2, 3):
                min_w, max_w, step_w, min_h, max_h, step_h = item.sizes
                if not (0 < min_w <= max_w and 0 < min_h <= max_h):
                    raise CameraError('camera_unsupported')
                step_w, step_h = (1, 1) if item.type == 2 else (step_w, step_h)
                if not step_w or not step_h:
                    raise CameraError('camera_unsupported')
                width = min_w + max(0, (min(640, max_w) - min_w) // step_w) * step_w
                height = min_h + max(0, (min(480, max_h) - min_h) // step_h) * step_h
                if width <= 1280 and height <= 960:
                    sizes.append((width, height))
                break
        else:
            raise CameraError('camera_unsupported')
        # Legacy ENUM_FRAMESIZES absence permits one bounded S_FMT request.
        if not sizes and index == 0 and item is None:
            sizes = [(640, 480)]
        return sorted(set(sizes), key=lambda size: (abs(size[0] * size[1] - 640 * 480), size))

    def _negotiate(self, fd):
        cap = V.Capability()
        self.io.ioctl(fd, 'QUERYCAP', cap)
        flags = cap.device_caps if cap.capabilities & 0x80000000 else cap.capabilities
        if bytes(cap.driver).split(b'\0', 1)[0] != b'uvcvideo' or flags & 0x04000001 != 0x04000001:
            raise CameraError('camera_unsupported')
        for pixel_format in self._formats(fd):
            for width, height in self._sizes(fd, pixel_format)[:8]:
                fmt = self.io.abi.Format(type=V.CAPTURE)
                fmt.fmt.pix = V.PixelFormat(width=width, height=height, pixelformat=pixel_format, field=1)
                if _optional(self.io, fd, 'S_FMT', fmt) is None:
                    continue
                pix = fmt.fmt.pix
                size = 1 if pix.pixelformat == V.GREY else 2
                if (pix.pixelformat != pixel_format or pix.field != 1 or not 0 < pix.width <= 1280
                        or not 0 < pix.height <= 960 or pix.width * pix.height > MAX_PIXELS
                        or (pixel_format == V.YUYV and pix.width % 2)
                        or not pix.width * size <= pix.bytesperline <= V.MAX_BUFFER_BYTES // pix.height
                        or not pix.bytesperline * pix.height <= pix.sizeimage <= V.MAX_BUFFER_BYTES):
                    continue
                return dict(width=pix.width, height=pix.height, stride=pix.bytesperline,
                            bytes=pix.sizeimage, pixel_format=pixel_format)
        raise CameraError('camera_unsupported')

    def _describe(self, fd, fmt):
        parameters = V.StreamParameters(type=V.CAPTURE)
        self.io.ioctl(fd, 'G_PARM', parameters)
        numerator, denominator = parameters.data[2:4]
        if not numerator or not denominator or not 0.0001 <= numerator / denominator <= 60:
            raise CameraError('camera_unsupported')
        self._period = numerator / denominator
        self._exposure = _control(self.io, fd, V.EXPOSURE_ABSOLUTE)
        self._auto = _control(self.io, fd, V.EXPOSURE_AUTO, 3)
        self._gain = _control(self.io, fd, V.GAIN)
        self._autogain = _control(self.io, fd, V.AUTOGAIN, 2)
        # Without manual exposure, duration is a nominal video-frame interval.
        maximum = min(self._period, self._exposure.maximum / 10000) if self._exposure else self._period
        minimum = self._exposure.minimum / 10000 if self._exposure else self._period
        if self._exposure and maximum >= minimum:
            ctrl = self._exposure
            maximum = (ctrl.minimum + int((maximum * 10000 - ctrl.minimum) // ctrl.step) * ctrl.step) / 10000
        if not 0 < minimum <= maximum:
            raise CameraError('camera_unsupported')
        self._frame_max = maximum
        self._stack_limit = min(MAX_STACK, MAX_PIXEL_WORK // (fmt['width'] * fmt['height']))
        bits = 16 if fmt['pixel_format'] == V.Y16 else 8
        return validate_info(dict(name='uvc', sensor_name='uvc_luminance',
            width=fmt['width'], height=fmt['height'], pixel_um_x=self.pixel_um[0], pixel_um_y=self.pixel_um[1],
            bits=bits, max_adu=(1 << bits) - 1, sensor_type=0, bayer_offset=[0, 0], max_bin=1,
            gain_min=self._gain.minimum if self._gain else 0,
            gain_max=self._gain.maximum if self._gain else 0,
            exposure_min=minimum, exposure_max=min(MAX_SECONDS, maximum * self._stack_limit),
            can_abort=True, cooled=False))

    def _acquire(self):
        if self.pixel_um is None:
            # Alpaca mandates positive physical pixel pitch; UVC cannot supply it.
            # Only reviewed platform metadata may provide it, never a guessed value.
            raise CameraError('camera_unsupported')
        for node in self.io.nodes(self.device):
            fd = None
            try:
                fd = self.io.open(node)
                fmt = self._negotiate(fd)
                info = self._describe(fd, fmt)
                if self._info is not None and (self._info != info or self._format != fmt):
                    raise CameraError('camera_unsupported')  # Never change published geometry silently.
            except (OSError, CameraError):
                if fd is not None:
                    self.io.close(fd)
                continue
            self._info, self._format = info, fmt
            return fd
        raise CameraError('camera_unsupported')

    def info(self):
        if self._info is None:
            try:
                fd = self._acquire()
                self.io.close(fd)
            except (OSError, ValueError):
                raise CameraError('camera_unsupported') from None
        return validate_info(self._info)

    def open(self):
        if self.fd is None:
            try:
                self.fd = self._acquire()
            except (OSError, ValueError):
                raise CameraError('camera_unsupported') from None
            self._state = 'idle'

    def _require_open(self):
        if self.fd is None:
            raise CameraError('not_open')

    def _buffer(self, index=0):
        return self.io.abi.Buffer(index=index, type=V.CAPTURE, memory=V.MMAP)

    def _stop(self):
        failed = False
        if self._streaming:
            try:
                self.io.ioctl(self.fd, 'STREAMOFF', V.U32(V.CAPTURE))
            except (OSError, CameraError):
                failed = True
            self._streaming = False
        # Closing the fd on STREAMOFF failure releases queue ownership first.
        if failed and self.fd is not None:
            self.io.close(self.fd)
            self.fd = None
        for mapping in self._maps:
            try:
                mapping.close()
            except OSError:
                failed = True
        self._maps = []
        if self._allocated and self.fd is not None:
            try:
                self.io.ioctl(self.fd, 'REQBUFS', V.RequestBuffers(type=V.CAPTURE, memory=V.MMAP))
            except (OSError, CameraError):
                self.io.close(self.fd)
                self.fd = None
        self._allocated = False
        if failed and self.fd is not None:
            self.io.close(self.fd)
            self.fd = None

    def close(self):
        try:
            self._stop()
        finally:
            if self.fd is not None:
                self.io.close(self.fd)
                self.fd = None
            self._sum = self._pending = None
            self._state = 'idle'

    def abort(self):
        self._require_open()
        self._stop()
        self._sum = self._pending = None
        self._state = 'idle' if self.fd is not None else 'error'

    def start_exposure(self, seconds, bin=1, roi=None, gain=0, light=True):
        self._require_open()
        if self._state in ('waiting', 'exposing', 'reading'):
            raise CameraError('busy')
        request = validate_exposure(self._info, seconds, bin, roi, gain, light)
        # HasShutter=False: ASCOM requires ignoring Light after type validation.
        # Capture real luminance unchanged; never synthesize a dark frame.
        request['light'] = True
        if self._gain and (gain - self._gain.minimum) % self._gain.step:
            raise CameraError('invalid_exposure')
        count = max(1, math.ceil(seconds / self._frame_max - 1e-9))
        unit = seconds / count
        if self._exposure:
            ctrl = self._exposure
            units = ctrl.minimum + max(0, round((unit * 10000 - ctrl.minimum) / ctrl.step)) * ctrl.step
            units = min(units, ctrl.minimum + int((self._frame_max * 10000 - ctrl.minimum) // ctrl.step) * ctrl.step)
            unit = units / 10000
        if count > self._stack_limit:
            raise CameraError('invalid_exposure')
        self._sum = self._pending = None
        try:
            if self._exposure:
                if self._auto:
                    _set_control(self.io, self.fd, V.EXPOSURE_AUTO, 1)
                _set_control(self.io, self.fd, V.EXPOSURE_ABSOLUTE, units)
            if self._gain:
                if self._autogain:
                    _set_control(self.io, self.fd, V.AUTOGAIN, 0)
                _set_control(self.io, self.fd, V.GAIN, gain)
            buffers = V.RequestBuffers(count=2, type=V.CAPTURE, memory=V.MMAP)
            self.io.ioctl(self.fd, 'REQBUFS', buffers)
            self._allocated = True
            if not 1 <= buffers.count <= V.MAX_BUFFERS:
                raise CameraError('camera_unsupported')
            for index in range(buffers.count):
                buf = self._buffer(index)
                self.io.ioctl(self.fd, 'QUERYBUF', buf)
                if (buf.index != index or buf.type != V.CAPTURE or buf.memory != V.MMAP
                        or buf.offset > 0xffffffff
                        or not self._format['bytes'] <= buf.length <= V.MAX_BUFFER_BYTES):
                    raise CameraError('camera_unsupported')
                self._maps.append(self.io.map(self.fd, buf.length, buf.offset))
                self.io.ioctl(self.fd, 'QBUF', self._buffer(index))
            self._streaming = True  # STREAMOFF also covers a partial STREAMON failure.
            self.io.ioctl(self.fd, 'STREAMON', V.U32(V.CAPTURE))
            self._sum = array('I', [0]) * (request['roi']['width'] * request['roi']['height'])
            self._pending = request
            self._count, self._target, self._unit = 0, count, unit
            self._sequence += 1
            now = self.clock()
            self._deadline = now + max(2, self._period * 3)
            # Service polling is cooperative; allow time for bounded worker turns.
            self._total_deadline = now + min(7200, count * (self._period + 1) + 5)
            self._state = 'exposing'
        except (OSError, ValueError, MemoryError):
            self._stop()
            self._sum = self._pending = None
            self._state = 'error'
            raise CameraError('capture_failed') from None

    def state(self):
        if self._state != 'exposing':
            return self._state
        try:
            now = self.clock()
            if now >= min(self._deadline, self._total_deadline):
                raise CameraError('capture_timeout')
            buf = self._buffer()
            try:
                self.io.ioctl(self.fd, 'DQBUF', buf)
            except OSError as error:
                if error.errno in (errno.EAGAIN, errno.EINTR):
                    return self._state
                raise
            fmt = self._format
            if (buf.index >= len(self._maps) or buf.flags & 0x40 or buf.type != V.CAPTURE
                    or buf.memory != V.MMAP or buf.bytesused > len(self._maps[buf.index])
                    or buf.bytesused > V.MAX_BUFFER_BYTES):
                raise CameraError('invalid_frame')
            raw = self._maps[buf.index][:buf.bytesused]
            pixels = luminance(raw, fmt['width'], fmt['height'], fmt['stride'], fmt['pixel_format'])
            roi = self._pending['roi']
            offset = 0
            for y in range(roi['y'], roi['y'] + roi['height']):
                start = y * fmt['width'] + roi['x']
                for value in pixels[start:start + roi['width']]:
                    self._sum[offset] += value
                    offset += 1
            self.io.ioctl(self.fd, 'QBUF', self._buffer(buf.index))
            if self.clock() >= min(self._deadline, self._total_deadline):
                raise CameraError('capture_timeout')
            self._count += 1
            self._deadline = now + max(2, self._period * 3)
            if self._count == self._target:
                self._stop()
                if self.fd is None:
                    raise CameraError('capture_failed')
                self._state = 'download'
        except (OSError, ValueError, MemoryError):
            self._stop()
            self._sum = self._pending = None
            self._state = 'error'
        return self._state

    def read_frame(self):
        self._require_open()
        if self._state != 'download':
            raise CameraError('not_ready')
        try:
            frame = self._frame()
        except (ValueError, MemoryError):
            self._sum = self._pending = None
            self._state = 'error'
            raise CameraError('frame_failed') from None
        self._sum = self._pending = None
        self._state = 'idle'
        return frame

    def _frame(self):
        request, roi = self._pending, self._pending['roi']
        code = 'H' if self._info['bits'] == 16 else 'B'
        pixels = array(code, (value // self._count for value in self._sum))
        if code == 'H' and sys.byteorder != 'little':
            pixels.byteswap()
        metadata = dict(exposure_seconds=self._unit * self._count, bin=1, roi_x=roi['x'], roi_y=roi['y'],
                        gain=request['gain'], light=True, sequence=self._sequence, sensor_type=0,
                        stacked_frames=self._count, frame_seconds=self._unit)
        return Frame(roi['width'], roi['height'], 'UInt16' if code == 'H' else 'Byte', pixels.tobytes(), metadata)
