"""QHYCCD camera SDK (qhyccd.h of SDK 26.07.28 for Linux ARM64). Not verified on hardware.

Only the isolated child process opens a camera (vendor_process.py). The
bindings are written from the function signatures of the official header (C
mode, where control identifiers are int); the header is not copied here.

Established by the header and its SingleFrameMode sample, and used:
InitQHYCCDResource, ScanQHYCCD, GetQHYCCDId, GetQHYCCDModel, OpenQHYCCD,
SetQHYCCDStreamMode (0: single frame), InitQHYCCD, IsQHYCCDControlAvailable
(CAM_SINGLEFRAMEMODE, CAM_COLOR which answers the Bayer pattern,
CONTROL_TRANSFERBIT, CONTROL_COOLER), GetQHYCCDChipInfo, the limits and values
of CONTROL_GAIN and CONTROL_EXPOSURE (microseconds), SetQHYCCDResolution,
SetQHYCCDBinMode (1 x 1), SetQHYCCDBitsMode (16), ExpQHYCCDSingleFrame,
GetQHYCCDMemLength, GetQHYCCDSingleFrame, CloseQHYCCD, ReleaseQHYCCDResource,
and the chip temperature (CONTROL_CURTEMP) for a camera with a cooler.

The exposure and the readout may block in the library: they run on a thread of
the child process, so the bridge keeps asking the state without waiting.

Not done, with the reason:
- binning: the header does not say whether SetQHYCCDResolution counts binned
  or unbinned pixels, so only bin 1 is served;
- aborting an exposure: CancelQHYCCDExposingAndReadout would run while the
  readout call still waits on the other thread, which the header does not
  allow for; can_abort is False;
- setting the temperature: ControlQHYCCDTemp must be called every second;
- firmware: models that load firmware at plug-in (the fxload lines of the
  manufacturer's udev rules) are never sent firmware by the bridge, so they
  are not served;
- live mode, offset, USB traffic, read modes, colour debayering.
"""
import ctypes as C
import time

from .driver import CameraDriver, CameraError, validate_exposure, validate_info
from .frames import Frame
from .loader import load_platform_sdk
from .usb import SysfsUSB, validate_device
from .vendor_sdk import MAX_CAMERAS, OFFSETS, NativeJob, bind, single_usb, text

VENDOR_ID = 0x1618
VERIFIED_ON_HARDWARE = False

U, I, D, H, P = C.c_uint32, C.c_int, C.c_double, C.c_void_p, C.POINTER
CHAR = P(C.c_char)

SUCCESS, ERROR = 0, 0xFFFFFFFF
# CONTROL_ID
CONTROL_GAIN, CONTROL_EXPOSURE, CONTROL_TRANSFERBIT, CONTROL_CURTEMP, CONTROL_CURPWM, CONTROL_COOLER = 6, 8, 10, 14, 15, 18
CAM_COLOR, CAM_SINGLEFRAMEMODE = 20, 57
BAYER = {1: 'GB', 2: 'GR', 3: 'BG', 4: 'RG'}
ID_BYTES, MODEL_BYTES = 64, 128

SIGNATURES = {
    'InitQHYCCDResource': (U, ()), 'ReleaseQHYCCDResource': (U, ()), 'ScanQHYCCD': (U, ()),
    'GetQHYCCDId': (U, (U, CHAR)), 'GetQHYCCDModel': (U, (CHAR, CHAR)),
    'OpenQHYCCD': (H, (CHAR,)), 'CloseQHYCCD': (U, (H,)),
    'SetQHYCCDStreamMode': (U, (H, C.c_uint8)), 'InitQHYCCD': (U, (H,)),
    'IsQHYCCDControlAvailable': (U, (H, I)),
    'SetQHYCCDParam': (U, (H, I, D)), 'GetQHYCCDParam': (D, (H, I)),
    'GetQHYCCDParamMinMaxStep': (U, (H, I, P(D), P(D), P(D))),
    'GetQHYCCDChipInfo': (U, (H, P(D), P(D), P(U), P(U), P(D), P(D), P(U))),
    'SetQHYCCDResolution': (U, (H, U, U, U, U)), 'SetQHYCCDBinMode': (U, (H, U, U)),
    'SetQHYCCDBitsMode': (U, (H, U)), 'GetQHYCCDMemLength': (U, (H,)),
    'ExpQHYCCDSingleFrame': (U, (H,)),
    'GetQHYCCDSingleFrame': (U, (H, P(U), P(U), P(U), P(U), P(C.c_uint8))),
}


def checked(code):
    if code != SUCCESS:
        raise CameraError('capture_failed')


class QHYDriver(CameraDriver):
    READOUT_SECONDS = 60  # Bound on the readout after the exposure, before the capture counts as failed.

    def __init__(self, device, path, *, loader=load_platform_sdk, inventory=None, clock=time.monotonic):
        self.device, self.path = validate_device(device), path
        self.inventory = inventory if inventory is not None else SysfsUSB()
        self.loader, self.clock = loader, clock
        self.sdk = self.handle = self.description = self.pending = self.job = None
        self.resource = False
        self.phase, self.sequence = 'idle', 0

    def open(self):
        if self.handle is not None:
            return
        if self.sdk is None:
            self.sdk = bind(self.loader(self.path), SIGNATURES)
        if not self.resource:
            checked(self.sdk.InitQHYCCDResource())
            self.resource = True
        try:
            count = self.sdk.ScanQHYCCD()
            if count != 1 or count > MAX_CAMERAS:
                raise CameraError('camera_unsupported')
            single_usb(self.inventory, (VENDOR_ID,), self.device)
            ident = C.create_string_buffer(ID_BYTES)
            checked(self.sdk.GetQHYCCDId(0, ident))
            model = C.create_string_buffer(MODEL_BYTES)
            name = text(model) if self.sdk.GetQHYCCDModel(ident, model) == SUCCESS else ''
            handle = self.sdk.OpenQHYCCD(ident)
            if not handle:
                raise CameraError('capture_failed')
            self.handle = handle
            if self.sdk.IsQHYCCDControlAvailable(handle, CAM_SINGLEFRAMEMODE) == ERROR:
                raise CameraError('camera_unsupported')
            checked(self.sdk.SetQHYCCDStreamMode(handle, 0))
            checked(self.sdk.InitQHYCCD(handle))
            description = self._describe(name or text(ident) or 'QHYCCD camera')
            if self.description is not None and self.description != description:
                raise CameraError('camera_unsupported')
            self.description, self.phase = description, 'idle'
        except BaseException:
            self.close()
            raise

    def _limits(self, control):
        low, high, step = D(), D(), D()
        if (self.sdk.IsQHYCCDControlAvailable(self.handle, control) != SUCCESS
                or self.sdk.GetQHYCCDParamMinMaxStep(self.handle, control, C.byref(low), C.byref(high), C.byref(step)) != SUCCESS
                or not low.value <= high.value):
            raise CameraError('camera_unsupported')
        return low.value, high.value

    def _describe(self, name):
        chip_w, chip_h, pixel_w, pixel_h = D(), D(), D(), D()
        width, height, bpp = U(), U(), U()
        checked(self.sdk.GetQHYCCDChipInfo(self.handle, C.byref(chip_w), C.byref(chip_h), C.byref(width),
                                           C.byref(height), C.byref(pixel_w), C.byref(pixel_h), C.byref(bpp)))
        if self.sdk.IsQHYCCDControlAvailable(self.handle, CONTROL_TRANSFERBIT) == SUCCESS:
            checked(self.sdk.SetQHYCCDBitsMode(self.handle, 16))
            bits = 16
        else:
            bits = bpp.value
        if bits not in (8, 16):
            raise CameraError('camera_unsupported')
        self.bits = bits
        pattern = self.sdk.IsQHYCCDControlAvailable(self.handle, CAM_COLOR)
        color = pattern in BAYER
        gain_low, gain_high = self._limits(CONTROL_GAIN)
        exposure_low, exposure_high = self._limits(CONTROL_EXPOSURE)
        return validate_info(dict(
            name=name, sensor_name=name, width=width.value, height=height.value,
            pixel_um_x=pixel_w.value, pixel_um_y=pixel_h.value, bits=bits, max_adu=(1 << bits) - 1,
            sensor_type=2 if color else 0, bayer_offset=list(OFFSETS[BAYER[pattern]]) if color else [0, 0],
            max_bin=1, gain_min=max(0, int(-(-gain_low // 1))), gain_max=min(65535, int(gain_high // 1)),
            exposure_min=max(1e-6, exposure_low / 1e6), exposure_max=min(86400.0, exposure_high / 1e6),
            exposure_resolution=1e-6,  # CONTROL_EXPOSURE is written in whole microseconds
            can_abort=False, cooled=self.sdk.IsQHYCCDControlAvailable(self.handle, CONTROL_COOLER) == SUCCESS))

    def info(self):
        if self.description is None:
            self.open()
            self.close()
        return validate_info(self.description)

    def close(self):
        handle, self.handle = self.handle, None
        self.pending, self.phase = None, 'idle'
        if self.job is not None and self.job.alive():
            # The library still owns a blocking call: the child process ends instead.
            self.job = None
            return
        self.job = None
        try:
            if handle:
                checked(self.sdk.CloseQHYCCD(handle))
        finally:
            if self.resource:
                self.resource = False
                self.sdk.ReleaseQHYCCDResource()

    def _require_open(self):
        if self.handle is None:
            raise CameraError('not_open')

    def start_exposure(self, seconds, bin=1, roi=None, gain=0, light=True):
        self._require_open()
        if self.phase in ('exposing', 'reading'):
            raise CameraError('busy')
        if self.phase == 'error':
            raise CameraError('capture_failed')
        req = validate_exposure(self.description, seconds, bin, roi, gain, light)
        roi = req['roi']
        if self.description['sensor_type'] and (roi['x'] % 2 or roi['y'] % 2 or roi['width'] % 2 or roi['height'] % 2):
            raise CameraError('invalid_roi')
        handle, sdk = self.handle, self.sdk
        usec = round(req['seconds'] * 1e6)
        self.pending = None
        self.phase = 'error'
        checked(sdk.SetQHYCCDBinMode(handle, 1, 1))
        checked(sdk.SetQHYCCDResolution(handle, roi['x'], roi['y'], roi['width'], roi['height']))
        checked(sdk.SetQHYCCDParam(handle, CONTROL_EXPOSURE, float(usec)))
        checked(sdk.SetQHYCCDParam(handle, CONTROL_GAIN, float(gain)))
        if abs(sdk.GetQHYCCDParam(handle, CONTROL_EXPOSURE) - usec) > 1 or abs(sdk.GetQHYCCDParam(handle, CONTROL_GAIN) - gain) > 0.5:
            raise CameraError('capture_failed')
        length = sdk.GetQHYCCDMemLength(handle)
        needed = roi['width'] * roi['height'] * self.bits // 8
        if length == ERROR or not needed <= length <= 4 * needed + (1 << 24):
            raise CameraError('capture_failed')
        buffer = (C.c_uint8 * length)()

        def capture():
            if sdk.ExpQHYCCDSingleFrame(handle) == ERROR:
                raise CameraError('capture_failed')
            w, h, depth, channels = U(), U(), U(), U()
            checked(sdk.GetQHYCCDSingleFrame(handle, C.byref(w), C.byref(h), C.byref(depth), C.byref(channels), buffer))
            return w.value, h.value, depth.value, channels.value

        req.update(seconds=usec / 1e6, light=True)
        self.pending, self.buffer = req, buffer
        self.deadline = self.clock() + req['seconds'] + self.READOUT_SECONDS
        self.sequence += 1
        self.phase = 'exposing'
        self.job = NativeJob(capture).start()

    def state(self):
        if self.phase == 'exposing':
            if self.job.done():
                self.phase = 'download' if self.job.error is None else 'error'
            elif self.clock() >= self.deadline:
                self.phase = 'error'
        return self.phase

    def abort(self):
        self._require_open()
        if self.phase == 'exposing':
            raise CameraError('busy')  # Not offered: can_abort is False.
        self.phase, self.pending = 'idle', None

    def read_frame(self):
        self._require_open()
        if self.phase != 'download':
            raise CameraError('not_ready')
        req, self.pending = self.pending, None
        self.phase = 'error'
        roi = req['roi']
        width, height, depth, channels = self.job.result
        self.job = None
        if (width, height, depth, channels) != (roi['width'], roi['height'], self.bits, 1):
            raise CameraError('capture_failed')
        size = self.bits // 8
        data = bytes(self.buffer)[:width * height * size]
        self.buffer = None
        metadata = dict(exposure_seconds=req['seconds'], bin=1, roi_x=roi['x'], roi_y=roi['y'],
                        gain=req['gain'], light=True, sequence=self.sequence,
                        sensor_type=self.description['sensor_type'])
        frame = Frame(width, height, 'UInt16' if size == 2 else 'Byte', data, metadata)
        self.phase = 'idle'
        return frame

    # Cooling: reading only (setting a temperature needs a call every second). Not yet offered to apps.
    def cooling(self):
        self._require_open()
        if not self.description['cooled'] or self.phase == 'exposing':
            raise CameraError('camera_unsupported')
        return {'temperature': self.sdk.GetQHYCCDParam(self.handle, CONTROL_CURTEMP),
                'power': self.sdk.GetQHYCCDParam(self.handle, CONTROL_CURPWM)}

    def set_cooling(self, on, target_celsius=None):
        raise CameraError('camera_unsupported')
