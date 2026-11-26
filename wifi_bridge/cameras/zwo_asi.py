"""ASI SDK ABI (checked against 1.41 and 1.42). Only the isolated driver process opens an attached camera."""
import ctypes as C
import time

from .driver import CameraDriver, CameraError, validate_exposure, validate_info
from .frames import Frame
from .loader import load_platform_sdk
from .usb import SysfsUSB, validate_device

# One ASI220MM was served by a bridge on the bench, with SDK 1.41 supplied in an image. The bridge now delivers
# SDK 1.42, downloaded and installed on request (a new path), which no bridge has served a camera with yet:
# verified with 1.41, not verified with 1.42, so False. Documented or assumed is not verified on hardware.
VERIFIED_ON_HARDWARE = False
VERIFIED_SDK_VERSIONS = ('1.41',)


class CameraInfo(C.Structure):
    _fields_ = [('name', C.c_char * 64), ('ident', C.c_int), ('height', C.c_long),
                ('width', C.c_long), ('color', C.c_int), ('bayer', C.c_int),
                ('bins', C.c_int * 16), ('formats', C.c_int * 8), ('pitch', C.c_double),
                ('shutter', C.c_int), ('st4', C.c_int), ('cooler', C.c_int),
                ('usb3_host', C.c_int), ('usb3_camera', C.c_int), ('e_per_adu', C.c_float),
                ('depth', C.c_int), ('trigger', C.c_int), ('reserved', C.c_char * 16)]


class ControlCaps(C.Structure):
    _fields_ = [('name', C.c_char * 64), ('description', C.c_char * 128),
                ('maximum', C.c_long), ('minimum', C.c_long), ('default', C.c_long),
                ('auto', C.c_int), ('writable', C.c_int), ('kind', C.c_int),
                ('reserved', C.c_char * 32)]


I, L, P = C.c_int, C.c_long, C.POINTER
GAIN, EXPOSURE = 0, 1
RAW8, RAW16 = 0, 2
# The USB descriptor and SDK must identify the same reviewed model.
MODELS = {0x120a: 'ASI120MM', 0x120b: 'ASI120MC', 0x120c: 'ASI120MM Mini',
          0x120e: 'ASI120MC-S', 0x2209: 'ASI220MM Mini', 0x224a: 'ASI224MC', 0x294f: 'ASI294MC Pro',
          0x533f: 'ASI533MC Pro'}
SIGNATURES = {
    'ASIGetNumOfConnectedCameras': (), 'ASICameraCheck': (I, I),
    'ASIGetCameraProperty': (P(CameraInfo), I), 'ASIOpenCamera': (I,),
    'ASIInitCamera': (I,), 'ASICloseCamera': (I,), 'ASIGetNumOfControls': (I, P(I)),
    'ASIGetControlCaps': (I, I, P(ControlCaps)), 'ASISetROIFormat': (I, I, I, I, I),
    'ASIGetROIFormat': (I, P(I), P(I), P(I), P(I)), 'ASISetStartPos': (I, I, I),
    'ASIGetStartPos': (I, P(I), P(I)), 'ASIGetControlValue': (I, I, P(L), P(I)),
    'ASISetControlValue': (I, I, L, I),
    'ASIStartExposure': (I, I), 'ASIGetExpStatus': (I, P(I)), 'ASIStopExposure': (I,),
    'ASIGetDataAfterExp': (I, P(C.c_ubyte), L),
}


def checked(code):
    if code != 0:
        raise CameraError('capture_failed')


def terminated(values, end):
    values = list(values)
    if end not in values:
        raise CameraError('camera_unsupported')
    return values[:values.index(end)]


class ASIDriver(CameraDriver):
    def __init__(self, device, path, *, loader=load_platform_sdk, inventory=None, clock=time.monotonic):
        self.device, self.path = validate_device(device), path
        self.inventory = inventory if inventory is not None else SysfsUSB()
        self.loader, self.clock = loader, clock
        self.sdk = self.ident = self.description = self.pending = None
        self.phase, self.sequence = 'idle', 0

    def _unique(self):
        # No USB port in ASI_CAMERA_INFO. Count even SDK-recognized ASIs absent
        # from our descriptor table (or inaccessible inside a Linux container).
        rows = self.inventory.scan()
        if rows is None or self.sdk.ASIGetNumOfConnectedCameras() != 1:
            raise CameraError('camera_unsupported')
        matches = [d for d in rows if d['vendor_id'] == 0x03c3 and
                   self.sdk.ASICameraCheck(d['vendor_id'], d['product_id']) == 1]
        if len(matches) != 1 or matches[0] != self.device:
            raise CameraError('camera_unsupported')

    def open(self):
        if self.ident is not None:
            return
        if self.sdk is None:
            self.sdk = self.loader(self.path)
            try:
                for name, arguments in SIGNATURES.items():
                    fn = getattr(self.sdk, name)
                    fn.argtypes, fn.restype = list(arguments), I
            except AttributeError:
                self.sdk = None
                raise CameraError('camera_driver_unavailable') from None
        self._unique()
        info = CameraInfo()
        checked(self.sdk.ASIGetCameraProperty(C.byref(info), 0))
        model = info.name.decode('ascii', errors='replace').removeprefix('ZWO ')
        if info.ident < 0 or model != MODELS.get(self.device['product_id']):
            raise CameraError('camera_unsupported')
        checked(self.sdk.ASIOpenCamera(info.ident))
        self.ident = info.ident
        try:
            self._unique()
            checked(self.sdk.ASIInitCamera(self.ident))
            description = self._describe(info)
            if self.description is not None and self.description != description:
                raise CameraError('camera_unsupported')
            self.description, self.phase = description, 'idle'
        except BaseException:
            self.close()
            raise

    def _describe(self, info):
        count = I()
        checked(self.sdk.ASIGetNumOfControls(self.ident, C.byref(count)))
        if not 2 <= count.value <= 128:
            raise CameraError('camera_unsupported')
        limits = {}
        self.has_flip = False
        for index in range(count.value):
            cap = ControlCaps()
            checked(self.sdk.ASIGetControlCaps(self.ident, index, C.byref(cap)))
            if cap.kind == 9:  # ASI_FLIP
                self.has_flip = True
            if cap.kind in (GAIN, EXPOSURE) and cap.writable == 1:
                if cap.kind in limits:
                    raise CameraError('camera_unsupported')
                limits[cap.kind] = (cap.minimum, cap.maximum)
        formats, bins = terminated(info.formats, -1), terminated(info.bins, 0)
        if (set(limits) != {GAIN, EXPOSURE} or 1 not in bins or not {RAW8, RAW16}.intersection(formats)
                or info.color not in (0, 1) or info.cooler not in (0, 1)
                or info.shutter != 0 or not 1 <= info.depth <= 16 or info.color and not 0 <= info.bayer <= 3):
            raise CameraError('camera_unsupported')
        # Contiguous symmetric bins only; color binning may change the mosaic.
        if info.color and self.has_flip and self._control(9)[0] != 0:
            raise CameraError('camera_unsupported')
        self.format = RAW16 if RAW16 in formats else RAW8
        max_bin = 1
        while not info.color and max_bin < 16 and max_bin + 1 in bins:
            b = max_bin + 1
            if info.width % (8*b) or info.height % (2*b):
                break
            max_bin = b
        bits = 16 if self.format == RAW16 else 8
        name = info.name.decode('ascii')
        offsets = ((0, 0), (1, 1), (1, 0), (0, 1))
        # ASI RAW16 is an unsigned 16-bit output container; leave its packing intact.
        return validate_info(dict(name=name, sensor_name=name, width=info.width, height=info.height,
            pixel_um_x=info.pitch, pixel_um_y=info.pitch, bits=bits, max_adu=(1 << bits)-1,
            sensor_type=2 if info.color else 0, bayer_offset=offsets[info.bayer] if info.color else (0, 0),
            max_bin=max_bin, gain_min=limits[GAIN][0], gain_max=limits[GAIN][1],
            exposure_min=max(1, limits[EXPOSURE][0])/1e6, exposure_max=min(86400, limits[EXPOSURE][1]/1e6),
            exposure_resolution=1e-6,  # ASI_EXPOSURE is written in whole microseconds
            can_abort=True, cooled=bool(info.cooler)))

    def info(self):
        if self.description is None:
            self.open()
            self.close()
        return validate_info(self.description)

    def close(self):
        ident, self.ident = self.ident, None
        self.pending, self.phase = None, 'idle'
        if ident is not None:
            checked(self.sdk.ASICloseCamera(ident))

    def _require_open(self):
        if self.ident is None:
            raise CameraError('not_open')

    def _control(self, kind):
        value, auto = L(), I()
        checked(self.sdk.ASIGetControlValue(self.ident, kind, C.byref(value), C.byref(auto)))
        return value.value, auto.value

    def start_exposure(self, seconds, bin=1, roi=None, gain=0, light=True):
        self._require_open()
        if self.phase == 'exposing':
            raise CameraError('busy')
        if self.phase == 'error':
            raise CameraError('capture_failed')
        req = validate_exposure(self.description, seconds, bin, roi, gain, light)
        roi = req['roi']
        x, y = roi['x']//bin, roi['y']//bin
        width, height = roi['width']//bin, roi['height']//bin
        # Even color origins preserve the published Bayer offsets.
        if (width % 8 or height % 2 or self.description['sensor_type'] and (x % 2 or y % 2)
                or self.device['product_id'] in (0x120a, 0x120b) and width*height % 1024):
            raise CameraError('invalid_roi')
        ident, sdk = self.ident, self.sdk
        usec = round(seconds * 1e6)
        self.pending = None
        self.phase = 'error'  # Partial configuration cannot publish an older frame.
        checked(sdk.ASISetROIFormat(ident, width, height, bin, self.format))
        checked(sdk.ASISetStartPos(ident, x, y))
        actual = [I() for _ in range(6)]
        checked(sdk.ASIGetROIFormat(ident, *(C.byref(v) for v in actual[:4])))
        checked(sdk.ASIGetStartPos(ident, *(C.byref(v) for v in actual[4:])))
        if [v.value for v in actual] != [width, height, bin, self.format, x, y]:
            raise CameraError('invalid_roi')
        checked(sdk.ASISetControlValue(ident, EXPOSURE, usec, 0))
        checked(sdk.ASISetControlValue(ident, GAIN, gain, 0))
        if self._control(EXPOSURE) != (usec, 0) or self._control(GAIN) != (gain, 0):
            raise CameraError('capture_failed')
        checked(sdk.ASIStartExposure(ident, 0))  # Shutterless: always acquire actual light.
        req.update(seconds=usec/1e6, light=True)
        self.pending, self.deadline = req, self.clock() + req['seconds'] + 10
        self.sequence += 1
        self.phase = 'exposing'

    def state(self):
        if self.phase == 'exposing':
            value = I()
            if self.clock() >= self.deadline or self.sdk.ASIGetExpStatus(self.ident, C.byref(value)):
                self.phase = 'error'
            else:
                self.phase = {1: 'exposing', 2: 'download'}.get(value.value, 'error')
        return self.phase

    def abort(self):
        self._require_open()
        if self.phase in ('exposing', 'error'):
            self.phase = 'error'
            checked(self.sdk.ASIStopExposure(self.ident))
        self.phase, self.pending = 'idle', None

    def read_frame(self):
        self._require_open()
        if self.phase != 'download':
            raise CameraError('not_ready')
        req, self.pending = self.pending, None
        self.phase = 'error'
        roi, b = req['roi'], req['bin']
        width, height = roi['width']//b, roi['height']//b
        raw = (C.c_ubyte * (width*height*(2 if self.format == RAW16 else 1)))()
        checked(self.sdk.ASIGetDataAfterExp(self.ident, raw, len(raw)))
        metadata = dict(exposure_seconds=req['seconds'], bin=b, roi_x=roi['x'], roi_y=roi['y'],
                        gain=req['gain'], light=True, sequence=self.sequence,
                        sensor_type=self.description['sensor_type'])
        frame = Frame(width, height, 'UInt16' if self.format == RAW16 else 'Byte', bytes(raw), metadata)
        self.phase = 'idle'
        return frame
