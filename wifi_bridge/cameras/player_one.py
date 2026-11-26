"""Player One Astronomy camera SDK (PlayerOneCamera.h of SDK 3.10.1). Not verified on hardware.

Only the isolated child process opens a camera (vendor_process.py). The
bindings are written from the function signatures of the official header; the
manufacturer's header is not copied here.

Established by the header and used: camera count and properties (sensor size,
pixel size, ADC depth, colour and Bayer pattern, supported bins and formats,
cooler, USB product ID), open and init, the list of configs with their limits,
gain (POA_GAIN), exposure in seconds (POA_EXP, since SDK 3.8.0, or POA_EXPOSURE
in microseconds), ROI, bin and RAW8/RAW16 format with read-back, single-frame
exposure, POAImageReady, POAGetImageData, stop, close, and the cooler
(temperature, cooler on/off, target temperature).

The header passes POAConfigValue (a union of long, double and POABool) by value
to POASetConfig. ctypes refuses unions by value, so the same eight bytes are
passed as a one-field structure of the same size and alignment, which the ARM
and x86-64 calling conventions pass exactly like that union.

Not done: colour binning (a colour camera is served at bin 1, as for ZWO),
sensor modes, offset, white balance, auto exposure, ST4 guiding, video mode.
"""
import ctypes as C
import time

from .driver import CameraDriver, CameraError, validate_exposure, validate_info
from .frames import Frame
from .loader import load_platform_sdk
from .usb import SysfsUSB, validate_device
from .vendor_sdk import MAX_CAMERAS, OFFSETS, bind, single_usb, text

VENDOR_ID = 0xa0a0
VERIFIED_ON_HARDWARE = False

I, L, P = C.c_int, C.c_long, C.POINTER


class Properties(C.Structure):
    _fields_ = [('cameraModelName', C.c_char * 256), ('userCustomID', C.c_char * 16), ('cameraID', I),
                ('maxWidth', I), ('maxHeight', I), ('bitDepth', I), ('isColorCamera', I), ('isHasST4Port', I),
                ('isHasCooler', I), ('isUSB3Speed', I), ('bayerPattern', I), ('pixelSize', C.c_double),
                ('SN', C.c_char * 64), ('sensorModelName', C.c_char * 32), ('localPath', C.c_char * 256),
                ('bins', I * 8), ('imgFormats', I * 8), ('isSupportHardBin', I), ('pID', I),
                ('reserved', C.c_char * 248)]


class ConfigValue(C.Union):
    _fields_ = [('intValue', L), ('floatValue', C.c_double), ('boolValue', I)]


class ConfigWord(C.Structure):
    """POAConfigValue passed by value: the same eight bytes as the union."""
    _fields_ = [('raw', C.c_uint64)]


class ConfigAttributes(C.Structure):
    _fields_ = [('isSupportAuto', I), ('isWritable', I), ('isReadable', I), ('configID', I), ('valueType', I),
                ('maxValue', ConfigValue), ('minValue', ConfigValue), ('defaultValue', ConfigValue),
                ('szConfName', C.c_char * 64), ('szDescription', C.c_char * 128), ('reserved', C.c_char * 64)]


OK = 0
# POAConfig
EXPOSURE_US, GAIN, TEMPERATURE, COOLER_POWER, TARGET_TEMP, COOLER, EXP = 0, 1, 3, 16, 17, 18, 31
VAL_INT, VAL_FLOAT, VAL_BOOL = 0, 1, 2
RAW8, RAW16, END = 0, 1, -1
BAYER = {0: 'RG', 1: 'BG', 2: 'GR', 3: 'GB'}
MONO = -1

SIGNATURES = {
    'POAGetCameraCount': (I, ()),
    'POAGetCameraProperties': (I, (I, P(Properties))),
    'POAOpenCamera': (I, (I,)), 'POAInitCamera': (I, (I,)), 'POACloseCamera': (I, (I,)),
    'POAGetConfigsCount': (I, (I, P(I))),
    'POAGetConfigAttributes': (I, (I, I, P(ConfigAttributes))),
    'POASetConfig': (I, (I, I, ConfigWord, I)),
    'POAGetConfig': (I, (I, I, P(ConfigValue), P(I))),
    'POASetImageStartPos': (I, (I, I, I)), 'POAGetImageStartPos': (I, (I, P(I), P(I))),
    'POASetImageSize': (I, (I, I, I)), 'POAGetImageSize': (I, (I, P(I), P(I))),
    'POASetImageBin': (I, (I, I)), 'POAGetImageBin': (I, (I, P(I))),
    'POASetImageFormat': (I, (I, I)), 'POAGetImageFormat': (I, (I, P(I))),
    'POAStartExposure': (I, (I, I)), 'POAStopExposure': (I, (I,)),
    'POAImageReady': (I, (I, P(I))),
    'POAGetImageData': (I, (I, P(C.c_ubyte), L, I)),
}


def checked(code):
    if code != OK:
        raise CameraError('capture_failed')


def word(kind, value):
    union = ConfigValue()
    if kind == VAL_FLOAT:
        union.floatValue = float(value)
    elif kind == VAL_BOOL:
        union.boolValue = 1 if value else 0
    else:
        union.intValue = int(value)
    return ConfigWord.from_buffer_copy(bytes(union))


def terminated(values, end):
    values = list(values)
    if end not in values:
        raise CameraError('camera_unsupported')
    return values[:values.index(end)]


class PlayerOneDriver(CameraDriver):
    def __init__(self, device, path, *, loader=load_platform_sdk, inventory=None, clock=time.monotonic):
        self.device, self.path = validate_device(device), path
        self.inventory = inventory if inventory is not None else SysfsUSB()
        self.loader, self.clock = loader, clock
        self.sdk = self.ident = self.description = self.pending = None
        self.configs = {}
        self.phase, self.sequence = 'idle', 0

    def _camera(self):
        """The one SDK camera whose USB product ID is this device's."""
        count = self.sdk.POAGetCameraCount()
        if not 1 <= count <= MAX_CAMERAS:
            raise CameraError('camera_unsupported')
        found = []
        for index in range(count):
            prop = Properties()
            checked(self.sdk.POAGetCameraProperties(index, C.byref(prop)))
            if prop.pID == self.device['product_id']:
                found.append(prop)
        single_usb(self.inventory, (VENDOR_ID,), self.device, self.device['product_id'])
        if len(found) != 1 or found[0].cameraID < 0:
            raise CameraError('camera_unsupported')
        return found[0]

    def open(self):
        if self.ident is not None:
            return
        if self.sdk is None:
            self.sdk = bind(self.loader(self.path), SIGNATURES)
        prop = self._camera()
        checked(self.sdk.POAOpenCamera(prop.cameraID))
        self.ident = prop.cameraID
        try:
            checked(self.sdk.POAInitCamera(self.ident))
            description = self._describe(prop)
            if self.description is not None and self.description != description:
                raise CameraError('camera_unsupported')
            self.description, self.phase = description, 'idle'
        except BaseException:
            self.close()
            raise

    def _describe(self, prop):
        count = I()
        checked(self.sdk.POAGetConfigsCount(self.ident, C.byref(count)))
        if not 2 <= count.value <= 128:
            raise CameraError('camera_unsupported')
        configs = {}
        for index in range(count.value):
            attributes = ConfigAttributes()
            checked(self.sdk.POAGetConfigAttributes(self.ident, index, C.byref(attributes)))
            configs[attributes.configID] = attributes
        self.configs = configs
        gain = configs.get(GAIN)
        exposure = configs.get(EXP) or configs.get(EXPOSURE_US)
        if (gain is None or gain.isWritable != 1 or gain.valueType != VAL_INT or exposure is None
                or exposure.isWritable != 1 or exposure.valueType != (VAL_FLOAT if exposure.configID == EXP else VAL_INT)):
            raise CameraError('camera_unsupported')
        self.exposure_config = exposure.configID
        formats, bins = terminated(prop.imgFormats, END), terminated(prop.bins, 0)
        color = prop.isColorCamera == 1
        if (1 not in bins or not {RAW8, RAW16}.intersection(formats) or prop.isColorCamera not in (0, 1)
                or color != (prop.bayerPattern != MONO) or color and prop.bayerPattern not in BAYER):
            raise CameraError('camera_unsupported')
        self.format = RAW16 if RAW16 in formats else RAW8
        max_bin = 1
        while not color and max_bin < 16 and max_bin + 1 in bins:
            b = max_bin + 1
            if prop.maxWidth % (4 * b) or prop.maxHeight % (2 * b):
                break
            max_bin = b
        if exposure.configID == EXP:
            low, high = exposure.minValue.floatValue, exposure.maxValue.floatValue
        else:
            low, high = exposure.minValue.intValue / 1e6, exposure.maxValue.intValue / 1e6
        # Integer-microsecond control: 1 us. The float-seconds control has no known step: left out.
        extra = {} if exposure.configID == EXP else {'exposure_resolution': 1e-6}
        bits = 16 if self.format == RAW16 else 8
        name = text(prop.cameraModelName) or 'Player One camera'
        sensor = text(prop.sensorModelName) or name
        return validate_info(dict(
            name=name, sensor_name=sensor, width=prop.maxWidth, height=prop.maxHeight,
            pixel_um_x=prop.pixelSize, pixel_um_y=prop.pixelSize, bits=bits, max_adu=(1 << bits) - 1,
            sensor_type=2 if color else 0, bayer_offset=list(OFFSETS[BAYER[prop.bayerPattern]]) if color else [0, 0],
            max_bin=max_bin, gain_min=max(0, gain.minValue.intValue), gain_max=min(65535, gain.maxValue.intValue),
            exposure_min=max(1e-6, low), exposure_max=min(86400.0, high), **extra,
            can_abort=True, cooled=prop.isHasCooler == 1))

    def info(self):
        if self.description is None:
            self.open()
            self.close()
        return validate_info(self.description)

    def close(self):
        ident, self.ident = self.ident, None
        self.pending, self.phase = None, 'idle'
        if ident is not None:
            checked(self.sdk.POACloseCamera(ident))

    def _require_open(self):
        if self.ident is None:
            raise CameraError('not_open')

    def _get(self, config):
        value, auto = ConfigValue(), I()
        checked(self.sdk.POAGetConfig(self.ident, config, C.byref(value), C.byref(auto)))
        return value, auto.value

    def _set(self, config, kind, value):
        checked(self.sdk.POASetConfig(self.ident, config, word(kind, value), 0))

    def start_exposure(self, seconds, bin=1, roi=None, gain=0, light=True):
        self._require_open()
        if self.phase == 'exposing':
            raise CameraError('busy')
        if self.phase == 'error':
            raise CameraError('capture_failed')
        req = validate_exposure(self.description, seconds, bin, roi, gain, light)
        roi = req['roi']
        x, y = roi['x'] // bin, roi['y'] // bin
        width, height = roi['width'] // bin, roi['height'] // bin
        # The header: width % 4 == 0 and height % 2 == 0; even colour origins keep the Bayer offsets.
        if width % 4 or height % 2 or self.description['sensor_type'] and (x % 2 or y % 2):
            raise CameraError('invalid_roi')
        ident, sdk = self.ident, self.sdk
        self.pending = None
        self.phase = 'error'  # A partial configuration never publishes an older frame.
        checked(sdk.POASetImageFormat(ident, self.format))
        checked(sdk.POASetImageBin(ident, bin))  # Bin first: it changes the size and start position.
        checked(sdk.POASetImageSize(ident, width, height))
        checked(sdk.POASetImageStartPos(ident, x, y))
        actual = [I() for _ in range(6)]
        checked(sdk.POAGetImageFormat(ident, C.byref(actual[0])))
        checked(sdk.POAGetImageBin(ident, C.byref(actual[1])))
        checked(sdk.POAGetImageSize(ident, C.byref(actual[2]), C.byref(actual[3])))
        checked(sdk.POAGetImageStartPos(ident, C.byref(actual[4]), C.byref(actual[5])))
        if [v.value for v in actual] != [self.format, bin, width, height, x, y]:
            raise CameraError('invalid_roi')
        if self.exposure_config == EXP:
            self._set(EXP, VAL_FLOAT, req['seconds'])
        else:
            self._set(EXPOSURE_US, VAL_INT, round(req['seconds'] * 1e6))
        self._set(GAIN, VAL_INT, gain)
        exposure, exposure_auto = self._get(self.exposure_config)
        value_gain, gain_auto = self._get(GAIN)
        applied = exposure.floatValue if self.exposure_config == EXP else exposure.intValue / 1e6
        if exposure_auto or gain_auto or value_gain.intValue != gain or abs(applied - req['seconds']) > max(1e-5, req['seconds'] * 1e-3):
            raise CameraError('capture_failed')
        checked(sdk.POAStartExposure(ident, 1))  # POA_TRUE: single frame (snap mode).
        req.update(seconds=applied, light=True)
        self.pending, self.deadline = req, self.clock() + applied + 30
        self.sequence += 1
        self.phase = 'exposing'

    def state(self):
        if self.phase == 'exposing':
            ready = I()
            if self.clock() >= self.deadline or self.sdk.POAImageReady(self.ident, C.byref(ready)) != OK:
                self.phase = 'error'
            elif ready.value == 1:
                self.phase = 'download'
        return self.phase

    def abort(self):
        self._require_open()
        if self.phase in ('exposing', 'error'):
            self.phase = 'error'
            checked(self.sdk.POAStopExposure(self.ident))
        self.phase, self.pending = 'idle', None

    def read_frame(self):
        self._require_open()
        if self.phase != 'download':
            raise CameraError('not_ready')
        req, self.pending = self.pending, None
        self.phase = 'error'
        roi, b = req['roi'], req['bin']
        width, height = roi['width'] // b, roi['height'] // b
        size = 2 if self.format == RAW16 else 1
        raw = (C.c_ubyte * (width * height * size))()
        checked(self.sdk.POAGetImageData(self.ident, raw, len(raw), 1000))
        metadata = dict(exposure_seconds=req['seconds'], bin=b, roi_x=roi['x'], roi_y=roi['y'],
                        gain=req['gain'], light=True, sequence=self.sequence,
                        sensor_type=self.description['sensor_type'])
        frame = Frame(width, height, 'UInt16' if size == 2 else 'Byte', bytes(raw), metadata)
        self.phase = 'idle'
        return frame

    # Cooling, for cameras whose properties say they have a cooler. Not yet offered to apps by the bridge.
    def cooling(self):
        self._require_open()
        if not self.description['cooled']:
            raise CameraError('camera_unsupported')
        result = {}
        for key, config in (('temperature', TEMPERATURE), ('power', COOLER_POWER), ('target', TARGET_TEMP), ('cooler_on', COOLER)):
            if config in self.configs and self.configs[config].isReadable == 1:
                value, _ = self._get(config)
                kind = self.configs[config].valueType
                result[key] = (value.floatValue if kind == VAL_FLOAT else bool(value.boolValue) if kind == VAL_BOOL
                               else value.intValue)
        return result

    def set_cooling(self, on, target_celsius=None):
        self._require_open()
        cooler, target = self.configs.get(COOLER), self.configs.get(TARGET_TEMP)
        if not self.description['cooled'] or cooler is None or cooler.isWritable != 1:
            raise CameraError('camera_unsupported')
        if target_celsius is not None:
            if target is None or target.isWritable != 1 or not target.minValue.intValue <= target_celsius <= target.maxValue.intValue:
                raise CameraError('camera_unsupported')
            self._set(TARGET_TEMP, VAL_INT, int(target_celsius))
        self._set(COOLER, VAL_BOOL, bool(on))
