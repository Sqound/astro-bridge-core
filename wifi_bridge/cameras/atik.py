"""Atik camera SDK (AtikCameras.h and AtikDefs.h of SDK 2026.10.07). Not verified on hardware.

Only the isolated child process opens a camera (vendor_process.py). The
bindings are written from the function signatures of the official header and
the SDK Developer's Guide; the manufacturer's files are not copied here.

Established by the official documentation and used: device count, whether a
device is a camera, connect and disconnect, ArtemisProperties (sensor size,
pixel size, description), ArtemisColourProperties (mono or RGGB with its
offsets), preview off, bin 1, subframe with read-back, ArtemisStartExposure in
seconds, ArtemisImageReady, ArtemisImageFailed, ArtemisCameraState,
ArtemisGetImageData, ArtemisImageBuffer (16-bit pixels), ArtemisStopExposure,
cooling (ArtemisCoolingInfo, ArtemisTemperatureSensorInfo and
ArtemisSetCooling in hundredths of a degree, ArtemisCoolerWarmUp), and the gain
of the Horizon and ACIS CMOS cameras through their camera specific options
(ID_GOPresetMode set to Custom, then ID_GOCustomGain within the range the
camera reports). Atik documents that this gain is saved in the camera.

Not done, with the reason:
- the gain of CCD cameras: ArtemisSetGain gives a "scale of 1-24" without
  saying which models; their gain stays as the camera has it (range 0 to 0);
- binning: the documentation does not say whether the subframe counts binned
  or unbinned pixels, so only bin 1 is served;
- the exposure range: the SDK does not report one; the bridge bounds it to
  1 ms and 3600 s, and the camera's own refusal is reported as a failure;
- the eight-bit mode, fast mode, overlapped exposures, filter wheels, guiding;
- cameras on the shared Cypress vendor identifier 04b4 (older Atik models):
  the same identifier is used by other manufacturers before their firmware loads.
"""
import ctypes as C
import time

from .driver import CameraDriver, CameraError, validate_exposure, validate_info
from .frames import Frame
from .loader import load_platform_sdk
from .usb import SysfsUSB, validate_device
from .vendor_sdk import MAX_CAMERAS, bind, native_u16, single_usb, text, u16_bytes

VENDOR_ID = 0x20e7
VERIFIED_ON_HARDWARE = False

I, H, P = C.c_int, C.c_void_p, C.POINTER
CHAR = P(C.c_char)


class Properties(C.Structure):
    _fields_ = [('Protocol', I), ('nPixelsX', I), ('nPixelsY', I), ('PixelMicronsX', C.c_float),
                ('PixelMicronsY', C.c_float), ('ccdflags', I), ('cameraflags', I),
                ('Description', C.c_char * 40), ('Manufacturer', C.c_char * 40)]


ARTEMIS_OK = 0
COLOUR_UNKNOWN, COLOUR_NONE, COLOUR_RGGB = 0, 1, 2
CAMERA_ERROR = -1
ID_GO_PRESET_MODE, ID_GO_CUSTOM_GAIN = 1, 5
PRESET_CUSTOM = 0
COOLING_HAS, COOLING_SETPOINT = 1, 16
NAME_BYTES = 100  # "a user-allocated char array of length 100"
EXPOSURE_MIN, EXPOSURE_MAX = 0.001, 3600.0

SIGNATURES = {
    'ArtemisDeviceCount': (I, ()), 'ArtemisDeviceIsCamera': (I, (I,)), 'ArtemisDeviceName': (I, (I, CHAR)),
    'ArtemisConnect': (H, (I,)), 'ArtemisDisconnect': (I, (H,)), 'ArtemisIsConnected': (I, (H,)),
    'ArtemisProperties': (I, (H, P(Properties))),
    'ArtemisColourProperties': (I, (H, P(I), P(I), P(I), P(I), P(I))),
    'ArtemisSetPreview': (I, (H, C.c_bool)), 'ArtemisBin': (I, (H, I, I)), 'ArtemisGetBin': (I, (H, P(I), P(I))),
    'ArtemisSubframe': (I, (H, I, I, I, I)), 'ArtemisGetSubframe': (I, (H, P(I), P(I), P(I), P(I))),
    'ArtemisStartExposure': (I, (H, C.c_float)), 'ArtemisStopExposure': (I, (H,)),
    'ArtemisImageReady': (I, (H,)), 'ArtemisImageFailed': (I, (H,)), 'ArtemisCameraState': (I, (H,)),
    'ArtemisGetImageData': (I, (H, P(I), P(I), P(I), P(I), P(I), P(I))),
    'ArtemisImageBuffer': (H, (H,)),
    'ArtemisHasCameraSpecificOption': (C.c_bool, (H, C.c_ushort)),
    'ArtemisCameraSpecificOptionGetData': (I, (H, C.c_ushort, P(C.c_ubyte), I, P(I))),
    'ArtemisCameraSpecificOptionSetData': (I, (H, C.c_ushort, P(C.c_ubyte), I)),
    'ArtemisCoolingInfo': (I, (H, P(I), P(I), P(I), P(I), P(I))),
    'ArtemisTemperatureSensorInfo': (I, (H, I, P(I))),
    'ArtemisSetCooling': (I, (H, I)), 'ArtemisCoolerWarmUp': (I, (H,)),
}


def checked(code):
    if code != ARTEMIS_OK:
        raise CameraError('capture_failed')


class AtikDriver(CameraDriver):
    def __init__(self, device, path, *, loader=load_platform_sdk, inventory=None, clock=time.monotonic):
        self.device, self.path = validate_device(device), path
        self.inventory = inventory if inventory is not None else SysfsUSB()
        self.loader, self.clock = loader, clock
        self.sdk = self.handle = self.description = self.pending = None
        self.cooling_started = False
        self.phase, self.sequence = 'idle', 0

    def _index(self):
        count = self.sdk.ArtemisDeviceCount()
        if not 1 <= count <= MAX_CAMERAS:
            raise CameraError('camera_unsupported')
        cameras = [index for index in range(count) if self.sdk.ArtemisDeviceIsCamera(index)]
        single_usb(self.inventory, (VENDOR_ID,), self.device)
        if len(cameras) != 1:
            raise CameraError('camera_unsupported')
        return cameras[0]

    def open(self):
        if self.handle is not None:
            return
        if self.sdk is None:
            self.sdk = bind(self.loader(self.path), SIGNATURES)
        index = self._index()
        handle = self.sdk.ArtemisConnect(index)
        if not handle:
            raise CameraError('capture_failed')
        self.handle = handle
        try:
            description = self._describe()
            if self.description is not None and self.description != description:
                raise CameraError('camera_unsupported')
            self.description, self.phase = description, 'idle'
        except BaseException:
            self.close()
            raise

    def _option(self, ident, length):
        data, actual = (C.c_ubyte * length)(), I()
        checked(self.sdk.ArtemisCameraSpecificOptionGetData(self.handle, ident, data, length, C.byref(actual)))
        if actual.value != length:
            raise CameraError('capture_failed')
        return data

    def _describe(self):
        prop = Properties()
        checked(self.sdk.ArtemisProperties(self.handle, C.byref(prop)))
        colour, nx, ny, px, py = I(), I(), I(), I(), I()
        checked(self.sdk.ArtemisColourProperties(self.handle, *(C.byref(v) for v in (colour, nx, ny, px, py))))
        if colour.value not in (COLOUR_NONE, COLOUR_RGGB):
            raise CameraError('camera_unsupported')
        color = colour.value == COLOUR_RGGB
        # The pattern is RGGB shifted by the reported offsets: the red pixel of the cell moves with them.
        offset = [nx.value % 2, ny.value % 2] if color else [0, 0]
        self.custom_gain = bool(self.sdk.ArtemisHasCameraSpecificOption(self.handle, ID_GO_CUSTOM_GAIN))
        gain_min = gain_max = 0
        if self.custom_gain:
            data = self._option(ID_GO_CUSTOM_GAIN, 6)
            gain_min, gain_max = native_u16(data, 0), native_u16(data, 2)
            if gain_min > gain_max:
                raise CameraError('camera_unsupported')
        flags, level, low, high, setpoint = I(), I(), I(), I(), I()
        cooled = (self.sdk.ArtemisCoolingInfo(self.handle, *(C.byref(v) for v in (flags, level, low, high, setpoint))) == ARTEMIS_OK
                  and bool(flags.value & COOLING_HAS))
        self.cooling_flags = flags.value if cooled else 0
        name = text(prop.Description) or 'Atik camera'
        return validate_info(dict(
            name=name, sensor_name=name, width=prop.nPixelsX, height=prop.nPixelsY,
            pixel_um_x=round(prop.PixelMicronsX, 4), pixel_um_y=round(prop.PixelMicronsY, 4), bits=16, max_adu=65535,
            sensor_type=2 if color else 0, bayer_offset=offset, max_bin=1, gain_min=gain_min, gain_max=gain_max,
            exposure_min=EXPOSURE_MIN, exposure_max=EXPOSURE_MAX, can_abort=True, cooled=cooled))

    def info(self):
        if self.description is None:
            self.open()
            self.close()
        return validate_info(self.description)

    def close(self):
        handle, self.handle = self.handle, None
        self.pending, self.phase = None, 'idle'
        if handle:
            if self.cooling_started:
                # Atik: warming up must be requested at the end of operation.
                self.cooling_started = False
                self.sdk.ArtemisCoolerWarmUp(handle)
            self.sdk.ArtemisDisconnect(handle)

    def _require_open(self):
        if self.handle is None:
            raise CameraError('not_open')

    def _set_gain(self, gain):
        if not self.custom_gain:
            return  # Range 0 to 0: the camera keeps its own gain.
        current = native_u16(self._option(ID_GO_CUSTOM_GAIN, 6), 4)
        if current == gain:
            return
        for ident, value in ((ID_GO_PRESET_MODE, PRESET_CUSTOM), (ID_GO_CUSTOM_GAIN, gain)):
            data = (C.c_ubyte * 2)(*u16_bytes(value))
            checked(self.sdk.ArtemisCameraSpecificOptionSetData(self.handle, ident, data, 2))
        if native_u16(self._option(ID_GO_CUSTOM_GAIN, 6), 4) != gain:
            raise CameraError('capture_failed')

    def start_exposure(self, seconds, bin=1, roi=None, gain=0, light=True):
        self._require_open()
        if self.phase == 'exposing':
            raise CameraError('busy')
        if self.phase == 'error':
            raise CameraError('capture_failed')
        req = validate_exposure(self.description, seconds, bin, roi, gain, light)
        roi = req['roi']
        if self.description['sensor_type'] and (roi['x'] % 2 or roi['y'] % 2):
            raise CameraError('invalid_roi')
        handle, sdk = self.handle, self.sdk
        self.pending = None
        self.phase = 'error'
        checked(sdk.ArtemisSetPreview(handle, False))
        checked(sdk.ArtemisBin(handle, 1, 1))
        checked(sdk.ArtemisSubframe(handle, roi['x'], roi['y'], roi['width'], roi['height']))
        actual = [I() for _ in range(6)]
        checked(sdk.ArtemisGetBin(handle, C.byref(actual[0]), C.byref(actual[1])))
        checked(sdk.ArtemisGetSubframe(handle, *(C.byref(v) for v in actual[2:])))
        if [v.value for v in actual] != [1, 1, roi['x'], roi['y'], roi['width'], roi['height']]:
            raise CameraError('invalid_roi')
        self._set_gain(gain)
        checked(sdk.ArtemisStartExposure(handle, req['seconds']))
        req.update(seconds=float(C.c_float(req['seconds']).value), light=True)
        self.pending, self.deadline = req, self.clock() + req['seconds'] + 60
        self.sequence += 1
        self.phase = 'exposing'

    def state(self):
        if self.phase == 'exposing':
            if self.clock() >= self.deadline:
                self.phase = 'error'
            elif self.sdk.ArtemisImageReady(self.handle):
                self.phase = 'error' if self.sdk.ArtemisImageFailed(self.handle) else 'download'
            elif self.sdk.ArtemisCameraState(self.handle) == CAMERA_ERROR:
                self.phase = 'error'
        return self.phase

    def abort(self):
        self._require_open()
        if self.phase in ('exposing', 'error'):
            self.phase = 'error'
            checked(self.sdk.ArtemisStopExposure(self.handle))
        self.phase, self.pending = 'idle', None

    def read_frame(self):
        self._require_open()
        if self.phase != 'download':
            raise CameraError('not_ready')
        req, self.pending = self.pending, None
        self.phase = 'error'
        roi = req['roi']
        values = [I() for _ in range(6)]
        checked(self.sdk.ArtemisGetImageData(self.handle, *(C.byref(v) for v in values)))
        x, y, width, height, bx, by = (v.value for v in values)
        if (width, height, bx, by) != (roi['width'], roi['height'], 1, 1):
            raise CameraError('capture_failed')
        pointer = self.sdk.ArtemisImageBuffer(self.handle)
        if not pointer:
            raise CameraError('capture_failed')
        data = C.string_at(pointer, width * height * 2)
        metadata = dict(exposure_seconds=req['seconds'], bin=1, roi_x=roi['x'], roi_y=roi['y'],
                        gain=req['gain'], light=True, sequence=self.sequence,
                        sensor_type=self.description['sensor_type'])
        frame = Frame(width, height, 'UInt16', data, metadata)
        self.phase = 'idle'
        return frame

    # Cooling, for cameras whose cooling information says they have one. Not yet offered to apps.
    def cooling(self):
        self._require_open()
        if not self.description['cooled']:
            raise CameraError('camera_unsupported')
        flags, level, low, high, setpoint = I(), I(), I(), I(), I()
        checked(self.sdk.ArtemisCoolingInfo(self.handle, *(C.byref(v) for v in (flags, level, low, high, setpoint))))
        count, temperature = I(), I()
        result = {'power': level.value, 'cooler_on': bool(flags.value & 64)}
        if self.sdk.ArtemisTemperatureSensorInfo(self.handle, 0, C.byref(count)) == ARTEMIS_OK and count.value >= 1:
            checked(self.sdk.ArtemisTemperatureSensorInfo(self.handle, 1, C.byref(temperature)))
            result['temperature'] = temperature.value / 100
        return result

    def set_cooling(self, on, target_celsius=None):
        self._require_open()
        if not self.description['cooled']:
            raise CameraError('camera_unsupported')
        if not on:
            checked(self.sdk.ArtemisCoolerWarmUp(self.handle))
            self.cooling_started = False
            return
        if target_celsius is None or not self.cooling_flags & COOLING_SETPOINT or not -100 <= target_celsius <= 50:
            raise CameraError('camera_unsupported')
        checked(self.sdk.ArtemisSetCooling(self.handle, round(target_celsius * 100)))
        self.cooling_started = True
