"""Moravian Instruments camera library (gxccd.h of libgxccd 0.12.3). Not verified on hardware.

Only the isolated child process opens a camera (vendor_process.py). The
bindings are written from the function signatures and comments of the official
header; the header is not copied here.

Established by the header and used: gxccd_enumerate_usb with its callback,
gxccd_initialize_usb, gxccd_release, the boolean, integer and string
parameters (chip size in pixels, pixel size in nanometres, shortest exposure in
microseconds, longest in milliseconds, maximum gain, maximum pixel value,
default read mode, sub-frame, cooler, colour filters), gxccd_set_read_mode,
gxccd_set_binning (1 x 1), gxccd_set_gain, gxccd_start_exposure (seconds,
sub-frame in pixels with y growing down), gxccd_image_ready,
gxccd_abort_exposure, gxccd_read_image (16-bit little-endian pixels, first row
at the bottom of the image: the bridge turns it top-down), the chip
temperature, the cooler utilisation and gxccd_set_temperature.

The readout may block in the library: it runs on a thread of the child process.

Not done, with the reason:
- colour cameras (RGB, CMY, CMYG): the header does not say in which row order
  the "odd pixel" flags of the Bayer mask are given, and the image is turned
  top-down here, so the pattern cannot be published without guessing;
- binning: the header does not say whether the sub-frame counts binned or
  unbinned pixels, so only bin 1 is served;
- switching the cooler off: the library has no such call (only a target);
- Ethernet adapters, filter wheels, guiding, preflash, read modes other than
  the camera's default.
"""
import ctypes as C
import time

from .driver import CameraDriver, CameraError, validate_exposure, validate_info
from .frames import Frame
from .loader import load_platform_sdk
from .usb import SysfsUSB, validate_device
from .vendor_sdk import MAX_CAMERAS, NativeJob, bind, flip_rows, single_usb, text

VENDOR_ID = 0x1347
VERIFIED_ON_HARDWARE = False

I, H, P = C.c_int, C.c_void_p, C.POINTER
CALLBACK = C.CFUNCTYPE(None, C.c_int)

# gxccd_get_boolean_parameter
GBP_SUB_FRAME, GBP_COOLER, GBP_RGB, GBP_CMY, GBP_CMYG = 1, 4, 128, 129, 130
# gxccd_get_integer_parameter
GIP_CHIP_W, GIP_CHIP_D, GIP_PIXEL_W, GIP_PIXEL_D = 1, 2, 3, 4
GIP_MINIMAL_EXPOSURE, GIP_MAXIMAL_EXPOSURE, GIP_DEFAULT_READ_MODE, GIP_MAX_GAIN, GIP_MAX_PIXEL_VALUE = 9, 10, 12, 16, 17
GSP_CAMERA_DESCRIPTION, GSP_CHIP_DESCRIPTION = 0, 3
GV_CHIP_TEMPERATURE, GV_POWER_UTILIZATION = 0, 11
TEXT_BYTES = 128

SIGNATURES = {
    'gxccd_enumerate_usb': (None, (CALLBACK,)),
    'gxccd_initialize_usb': (H, (I,)), 'gxccd_release': (None, (H,)),
    'gxccd_get_boolean_parameter': (I, (H, I, P(C.c_bool))),
    'gxccd_get_integer_parameter': (I, (H, I, P(I))),
    'gxccd_get_string_parameter': (I, (H, I, P(C.c_char), C.c_size_t)),
    'gxccd_get_value': (I, (H, I, P(C.c_float))),
    'gxccd_set_temperature': (I, (H, C.c_float)),
    'gxccd_set_read_mode': (I, (H, I)), 'gxccd_set_binning': (I, (H, I, I)),
    'gxccd_set_gain': (I, (H, C.c_uint16)),
    'gxccd_start_exposure': (I, (H, C.c_double, C.c_bool, I, I, I, I)),
    'gxccd_abort_exposure': (I, (H, C.c_bool)),
    'gxccd_image_ready': (I, (H, P(C.c_bool))),
    'gxccd_read_image': (I, (H, C.c_void_p, C.c_size_t)),
}


def checked(code):
    if code != 0:  # The header: 0 on success, -1 on error.
        raise CameraError('capture_failed')


class MoravianDriver(CameraDriver):
    READOUT_SECONDS = 120  # Bound on a CCD readout before the capture counts as failed.

    def __init__(self, device, path, *, loader=load_platform_sdk, inventory=None, clock=time.monotonic):
        self.device, self.path = validate_device(device), path
        self.inventory = inventory if inventory is not None else SysfsUSB()
        self.loader, self.clock = loader, clock
        self.sdk = self.camera = self.description = self.pending = self.job = None
        self.discard = False
        self.phase, self.sequence = 'idle', 0

    def _camera_id(self):
        found = []
        callback = CALLBACK(lambda ident: found.append(ident) if len(found) <= MAX_CAMERAS else None)
        self.sdk.gxccd_enumerate_usb(callback)
        single_usb(self.inventory, (VENDOR_ID,), self.device)
        if len(found) != 1:
            raise CameraError('camera_unsupported')
        return found[0]

    def open(self):
        if self.camera is not None:
            return
        if self.sdk is None:
            self.sdk = bind(self.loader(self.path), SIGNATURES)
        camera = self.sdk.gxccd_initialize_usb(self._camera_id())
        if not camera:
            raise CameraError('capture_failed')
        self.camera = camera
        try:
            description = self._describe()
            if self.description is not None and self.description != description:
                raise CameraError('camera_unsupported')
            self.description, self.phase = description, 'idle'
        except BaseException:
            self.close()
            raise

    def _bool(self, index):
        value = C.c_bool()
        checked(self.sdk.gxccd_get_boolean_parameter(self.camera, index, C.byref(value)))
        return value.value

    def _int(self, index, default=None):
        value = I()
        if self.sdk.gxccd_get_integer_parameter(self.camera, index, C.byref(value)) != 0:
            if default is None:
                raise CameraError('capture_failed')
            return default
        return value.value

    def _text(self, index):
        buffer = C.create_string_buffer(TEXT_BYTES)
        if self.sdk.gxccd_get_string_parameter(self.camera, index, buffer, TEXT_BYTES) != 0:
            return ''
        return text(buffer)

    def _describe(self):
        if any(self._bool(index) for index in (GBP_RGB, GBP_CMY, GBP_CMYG)):
            raise CameraError('camera_unsupported')
        width, height = self._int(GIP_CHIP_W), self._int(GIP_CHIP_D)
        checked(self.sdk.gxccd_set_read_mode(self.camera, self._int(GIP_DEFAULT_READ_MODE)))
        checked(self.sdk.gxccd_set_binning(self.camera, 1, 1))
        self.sub_frame = self._bool(GBP_SUB_FRAME)
        # A maximum gain that cannot be read: no gain setting (range 0 to 0).
        self.gain_max = self._int(GIP_MAX_GAIN, -1)
        max_adu = self._int(GIP_MAX_PIXEL_VALUE, 65535)
        name = self._text(GSP_CAMERA_DESCRIPTION) or 'Moravian camera'
        return validate_info(dict(
            name=name, sensor_name=self._text(GSP_CHIP_DESCRIPTION) or name, width=width, height=height,
            pixel_um_x=self._int(GIP_PIXEL_W) / 1000, pixel_um_y=self._int(GIP_PIXEL_D) / 1000,
            bits=16, max_adu=max_adu if 1 <= max_adu <= 65535 else 65535, sensor_type=0, bayer_offset=[0, 0],
            max_bin=1, gain_min=0, gain_max=max(0, min(65535, self.gain_max)),
            exposure_min=max(1e-6, self._int(GIP_MINIMAL_EXPOSURE) / 1e6),
            exposure_max=min(86400.0, self._int(GIP_MAXIMAL_EXPOSURE) / 1e3),
            can_abort=True, cooled=self._bool(GBP_COOLER)))

    def info(self):
        if self.description is None:
            self.open()
            self.close()
        return validate_info(self.description)

    def close(self):
        camera, self.camera = self.camera, None
        self.pending, self.phase = None, 'idle'
        if self.job is not None and self.job.alive():
            self.job = None
            return  # The library still reads out: the child process ends instead.
        self.job = None
        if camera:
            self.sdk.gxccd_release(camera)

    def _require_open(self):
        if self.camera is None:
            raise CameraError('not_open')

    def start_exposure(self, seconds, bin=1, roi=None, gain=0, light=True):
        self._require_open()
        if self.phase in ('exposing', 'reading'):
            raise CameraError('busy')
        if self.phase == 'error':
            raise CameraError('capture_failed')
        req = validate_exposure(self.description, seconds, bin, roi, gain, light)
        roi = req['roi']
        full = roi == {'x': 0, 'y': 0, 'width': self.description['width'], 'height': self.description['height']}
        if not self.sub_frame and not full:
            raise CameraError('invalid_roi')
        self.pending = None
        self.phase = 'error'
        if self.gain_max > 0:
            checked(self.sdk.gxccd_set_gain(self.camera, gain))
        checked(self.sdk.gxccd_start_exposure(self.camera, req['seconds'], True,
                                              roi['x'], roi['y'], roi['width'], roi['height']))
        req.update(light=True)
        self.pending, self.discard = req, False
        self.deadline = self.clock() + req['seconds'] + self.READOUT_SECONDS
        self.sequence += 1
        self.phase = 'exposing'

    def state(self):
        if self.phase == 'exposing':
            ready = C.c_bool()
            if self.clock() >= self.deadline or self.sdk.gxccd_image_ready(self.camera, C.byref(ready)) != 0:
                self.phase = 'error'
            elif ready.value:
                roi = self.pending['roi']
                buffer = (C.c_ubyte * (roi['width'] * roi['height'] * 2))()
                camera, sdk = self.camera, self.sdk
                self.buffer = buffer
                self.job = NativeJob(lambda: checked(sdk.gxccd_read_image(camera, buffer, len(buffer)))).start()
                self.phase = 'reading'
        if self.phase == 'reading':
            if self.job.done():
                self.phase = 'error' if self.job.error is not None or self.discard else 'download'
            elif self.clock() >= self.deadline:
                self.phase = 'error'
        return self.phase

    def abort(self):
        self._require_open()
        if self.phase == 'exposing':
            self.phase = 'error'
            checked(self.sdk.gxccd_abort_exposure(self.camera, False))
        elif self.phase == 'reading':
            self.discard = True  # The readout ends on its own; its image is dropped.
            return
        self.phase, self.pending = 'idle', None

    def read_frame(self):
        self._require_open()
        if self.phase != 'download':
            raise CameraError('not_ready')
        req, self.pending = self.pending, None
        self.phase = 'error'
        roi = req['roi']
        width, height = roi['width'], roi['height']
        data = flip_rows(bytes(self.buffer), width, height, 2)
        self.buffer = self.job = None
        metadata = dict(exposure_seconds=req['seconds'], bin=1, roi_x=roi['x'], roi_y=roi['y'],
                        gain=req['gain'], light=True, sequence=self.sequence,
                        sensor_type=self.description['sensor_type'])
        frame = Frame(width, height, 'UInt16', data, metadata)
        self.phase = 'idle'
        return frame

    # Cooling, for cameras with a cooler. Not yet offered to apps.
    def cooling(self):
        self._require_open()
        if not self.description['cooled'] or self.phase == 'reading':
            raise CameraError('camera_unsupported')
        result = {}
        for key, index in (('temperature', GV_CHIP_TEMPERATURE), ('power', GV_POWER_UTILIZATION)):
            value = C.c_float()
            if self.sdk.gxccd_get_value(self.camera, index, C.byref(value)) == 0:
                result[key] = round(value.value, 3)
        return result

    def set_cooling(self, on, target_celsius=None):
        self._require_open()
        if not self.description['cooled'] or not on or target_celsius is None or not -100 <= target_celsius <= 50:
            raise CameraError('camera_unsupported')
        checked(self.sdk.gxccd_set_temperature(self.camera, float(target_celsius)))
