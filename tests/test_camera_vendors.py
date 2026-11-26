"""Player One, QHYCCD, Atik and Moravian adapters, against Python fakes of their documented C APIs.

Each fake answers the functions the adapter binds, with the argument and result
types of the manufacturer's official header. No manufacturer library is loaded,
no USB device is touched. None of this proves that a real camera answers: the
adapters are not verified on hardware.
"""
import ctypes as C
import json
import socket
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wifi_bridge.cameras import atik, drivers, moravian, player_one, qhyccd, vendor_platform, vendor_process, zwo_asi
from wifi_bridge.cameras import catalog as driver_catalog
from wifi_bridge.cameras.alpaca import Camera
from wifi_bridge.cameras.driver import CameraError
from wifi_bridge.cameras.registry import load_registry
from wifi_bridge.cameras.service import CameraService
from wifi_bridge.cameras.zwo_process import serve

from test_camera_sdk import root_owned


def usb(vendor, product, port='1-2'):
    return dict(vendor_id=vendor, product_id=product, device_class=0, interface_classes=[255],
                bus=1, port_path=port, speed_mbps=480)


def wait(predicate, seconds=2):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class Fake:
    """Every bound function is a Mock whose answer comes from handle(name, args)."""
    SIGNATURES = {}

    def __init__(self):
        self.fail = {}
        for name in self.SIGNATURES:
            setattr(self, name, Mock(side_effect=lambda *args, n=name: self.fail[n] if n in self.fail else self.handle(n, args)))

    def handle(self, name, args):
        return 0


def out(argument):
    """The object behind a byref() argument."""
    return argument._obj


# --------------------------------------------------------------------------- Player One

class FakePlayerOne(Fake):
    SIGNATURES = player_one.SIGNATURES

    def __init__(self, color=False):
        super().__init__()
        prop = player_one.Properties(cameraModelName=b'Mars-M', cameraID=3, maxWidth=128, maxHeight=64, bitDepth=12,
                                     isColorCamera=1 if color else 0, isHasCooler=1, bayerPattern=0 if color else -1,
                                     pixelSize=2.9, sensorModelName=b'IMX462', pID=0x1234)
        prop.bins[:3] = [1, 2, 0]
        prop.imgFormats[:3] = [0, 1, -1]
        self.cameras = [prop]
        self.configs = []
        for config, kind, low, high, writable in ((player_one.GAIN, 0, 0, 500, 1), (player_one.EXP, 1, 1e-5, 7200.0, 1),
                                                  (player_one.TEMPERATURE, 1, -50.0, 80.0, 0), (player_one.COOLER, 2, 0, 1, 1),
                                                  (player_one.TARGET_TEMP, 0, -40, 30, 1)):
            item = player_one.ConfigAttributes(isWritable=writable, isReadable=1, configID=config, valueType=kind)
            if kind == 1:
                item.minValue.floatValue, item.maxValue.floatValue = low, high
            else:
                item.minValue.intValue, item.maxValue.intValue = low, high
            self.configs.append(item)
        self.values = {player_one.TEMPERATURE: player_one.word(1, -9.5), player_one.COOLER: player_one.word(2, False),
                       player_one.TARGET_TEMP: player_one.word(0, 0)}
        self.auto, self.ready = 0, 0
        self.image, self.readback, self.order = {}, None, []

    def handle(self, name, args):
        if name == 'POAGetCameraCount':
            return len(self.cameras)
        if name == 'POAGetCameraProperties':
            C.memmove(args[1], C.byref(self.cameras[args[0]]), C.sizeof(player_one.Properties))
        elif name == 'POAGetConfigsCount':
            out(args[1]).value = len(self.configs)
        elif name == 'POAGetConfigAttributes':
            C.memmove(args[2], C.byref(self.configs[args[1]]), C.sizeof(player_one.ConfigAttributes))
        elif name == 'POASetConfig':
            assert isinstance(args[2], player_one.ConfigWord) and args[3] == 0
            self.values[args[1]] = args[2]
        elif name == 'POAGetConfig':
            C.memmove(args[2], C.byref(self.values[args[1]]), 8)
            out(args[3]).value = self.auto
        elif name.startswith('POASetImage'):
            self.image[name[11:]] = args[1:]
            self.order.append(name)
        elif name.startswith('POAGetImage') and name != 'POAGetImageData':
            key = name[11:]
            for target, value in zip(args[1:], (self.readback or {}).get(key, self.image[key])):
                out(target).value = value
        elif name == 'POAImageReady':
            out(args[1]).value = self.ready
        elif name == 'POAGetImageData':
            assert len(args[1]) == args[2] and args[3] == 1000
            args[1][:2] = [0x34, 0x12]
        return 0


class PlayerOneTests(unittest.TestCase):
    DEVICE = usb(0xa0a0, 0x1234)

    def make(self, sdk=None, rows=None):
        self.sdk = sdk or FakePlayerOne()
        self.source = Mock()
        self.source.scan.return_value = rows if rows is not None else [self.DEVICE]
        self.now = 10
        self.loader = Mock(return_value=self.sdk)
        return player_one.PlayerOneDriver(self.DEVICE, '/platform/player-one', loader=self.loader,
                                          inventory=self.source, clock=lambda: self.now)

    def test_header_layout_union_by_value_and_described_properties(self):
        self.assertEqual(C.sizeof(player_one.ConfigWord), C.sizeof(player_one.ConfigValue))
        self.assertEqual(C.alignment(player_one.ConfigWord), C.alignment(player_one.ConfigValue))
        self.assertEqual(bytes(player_one.word(1, 0.5)), bytes(player_one.ConfigValue(floatValue=0.5)))
        self.assertEqual(bytes(player_one.word(0, 300))[:C.sizeof(C.c_long)], (300).to_bytes(C.sizeof(C.c_long), 'little'))
        if C.sizeof(C.c_long) == 8:  # LP64: offsets of the header's structures.
            self.assertEqual((player_one.ConfigAttributes.maxValue.offset, C.sizeof(player_one.ConfigAttributes)), (24, 304))
            self.assertEqual(player_one.Properties.pixelSize.offset, 312)
        driver = self.make()
        self.loader.assert_not_called()
        info = driver.info()
        self.loader.assert_called_once_with('/platform/player-one')
        self.assertEqual(self.sdk.POASetConfig.argtypes[2], player_one.ConfigWord)
        self.assertEqual((info['name'], info['sensor_name'], info['width'], info['height'], info['pixel_um_x']),
                         ('Mars-M', 'IMX462', 128, 64, 2.9))
        self.assertEqual((info['bits'], info['max_adu'], info['sensor_type'], info['max_bin'], info['cooled']),
                         (16, 65535, 0, 2, True))
        self.assertEqual((info['gain_min'], info['gain_max'], info['exposure_min'], info['exposure_max']), (0, 500, 1e-5, 7200.0))
        self.assertNotIn('exposure_resolution', info)  # float-seconds control: step unknown (integer-us control reports 1e-6)
        self.sdk.POACloseCamera.assert_called_once_with(3)
        color = self.make(FakePlayerOne(color=True)).info()
        self.assertEqual((color['sensor_type'], color['bayer_offset'], color['max_bin']), (2, [0, 0], 1))

    def test_full_cycle_with_bin_roi_gain_exposure_and_frame(self):
        driver = self.make()
        driver.open()
        driver.start_exposure(0.5, 2, dict(x=8, y=4, width=32, height=16), 100, True)
        self.assertEqual(self.sdk.image, {'Format': (1,), 'Bin': (2,), 'Size': (16, 8), 'StartPos': (4, 2)})
        self.assertEqual(self.sdk.POASetImageBin.call_args_list[0].args, (3, 2))
        self.assertEqual(self.sdk.order, ['POASetImageFormat', 'POASetImageBin', 'POASetImageSize', 'POASetImageStartPos'])
        self.assertEqual(player_one.ConfigValue.from_buffer_copy(bytes(self.sdk.values[player_one.EXP])).floatValue, 0.5)
        self.assertEqual(player_one.ConfigValue.from_buffer_copy(bytes(self.sdk.values[player_one.GAIN])).intValue, 100)
        self.sdk.POAStartExposure.assert_called_once_with(3, 1)
        self.assertEqual(driver.state(), 'exposing')
        with self.assertRaisesRegex(CameraError, 'busy'):
            driver.start_exposure(1, 1, None, 1, True)
        self.sdk.ready = 1
        self.assertEqual(driver.state(), 'download')
        frame = driver.read_frame()
        self.assertEqual((frame.width, frame.height, frame.element_type, frame.data[:2]), (16, 8, 'UInt16', bytes([0x34, 0x12])))
        self.assertEqual((frame.metadata['bin'], frame.metadata['roi_x'], frame.metadata['exposure_seconds']), (2, 8, 0.5))
        with self.assertRaisesRegex(CameraError, 'not_ready'):
            driver.read_frame()
        driver.close()
        driver.close()
        self.sdk.POACloseCamera.assert_called_once_with(3)

    def test_identity_is_the_one_camera_of_this_usb_model(self):
        other = player_one.Properties.from_buffer_copy(self.sdk_props())
        other.cameraID, other.pID = 4, 0x5678
        sdk = FakePlayerOne()
        sdk.cameras.append(other)
        # Another Player One model beside it: still unique by product ID.
        self.make(sdk, rows=[self.DEVICE, usb(0xa0a0, 0x5678, '1-3')]).open()
        self.sdk.POAOpenCamera.assert_called_once_with(3)
        for rows, extra in (([self.DEVICE, usb(0xa0a0, 0x1234, '1-3')], None), ([], None), (None, None),
                            ([self.DEVICE], 0x1234)):
            sdk = FakePlayerOne()
            if extra:
                twin = player_one.Properties.from_buffer_copy(self.sdk_props())
                twin.cameraID = 9
                sdk.cameras.append(twin)
            driver = self.make(sdk, rows=rows)
            if rows is None:
                self.source.scan.return_value = None
            with self.subTest(rows=rows, extra=extra), self.assertRaisesRegex(CameraError, 'camera_unsupported'):
                driver.open()
            sdk.POAOpenCamera.assert_not_called()

    def sdk_props(self):
        return bytes(FakePlayerOne().cameras[0])

    def test_failures_never_publish_stale_data(self):
        driver = self.make()
        driver.open()
        for roi in (dict(x=0, y=0, width=30, height=16), dict(x=0, y=0, width=32, height=15)):
            with self.assertRaisesRegex(CameraError, 'invalid_roi'):
                driver.start_exposure(1, 1, roi, 1, True)
        self.sdk.fail['POASetImageSize'] = 7
        with self.assertRaisesRegex(CameraError, 'capture_failed'):
            driver.start_exposure(1, 1, None, 1, True)
        self.assertEqual(driver.state(), 'error')
        with self.assertRaisesRegex(CameraError, 'capture_failed'):
            driver.start_exposure(1, 1, None, 1, True)
        driver.abort()
        del self.sdk.fail['POASetImageSize']
        self.sdk.readback = {'Size': (124, 64)}
        with self.assertRaisesRegex(CameraError, 'invalid_roi'):
            driver.start_exposure(1, 1, None, 1, True)
        driver.abort()
        self.sdk.readback, self.sdk.auto = None, 1
        with self.assertRaisesRegex(CameraError, 'capture_failed'):
            driver.start_exposure(1, 1, None, 1, True)
        driver.abort()
        self.sdk.auto = 0
        driver.start_exposure(1, 1, None, 1, True)
        self.now += 40
        self.assertEqual(driver.state(), 'error')
        driver.abort()
        self.sdk.POAStopExposure.assert_called_with(3)
        color = self.make(FakePlayerOne(color=True))
        color.open()
        with self.assertRaisesRegex(CameraError, 'invalid_roi'):
            color.start_exposure(1, 1, dict(x=1, y=0, width=32, height=16), 1, True)

    def test_cooling_reads_and_sets_documented_configs(self):
        driver = self.make()
        driver.open()
        self.assertEqual(driver.cooling(), {'temperature': -9.5, 'target': 0, 'cooler_on': False})
        driver.set_cooling(True, -10)
        self.assertEqual(player_one.ConfigValue.from_buffer_copy(bytes(self.sdk.values[player_one.TARGET_TEMP])).intValue, -10)
        self.assertEqual(player_one.ConfigValue.from_buffer_copy(bytes(self.sdk.values[player_one.COOLER])).boolValue, 1)
        with self.assertRaisesRegex(CameraError, 'camera_unsupported'):
            driver.set_cooling(True, -60)

    def test_missing_symbol_or_library_is_driver_unavailable(self):
        sdk = FakePlayerOne()
        del sdk.POAImageReady
        with self.assertRaisesRegex(CameraError, 'camera_driver_unavailable'):
            self.make(sdk).open()
        driver = self.make()
        self.loader.side_effect = CameraError('camera_driver_unavailable')
        with self.assertRaisesRegex(CameraError, 'camera_driver_unavailable'):
            driver.open()


# --------------------------------------------------------------------------- QHYCCD

class FakeQHY(Fake):
    SIGNATURES = qhyccd.SIGNATURES
    HANDLE = 0x5000

    def __init__(self, bayer=None):
        super().__init__()
        self.count, self.bayer = 1, bayer
        self.controls = {qhyccd.CAM_SINGLEFRAMEMODE: 0, qhyccd.CONTROL_TRANSFERBIT: 0, qhyccd.CONTROL_GAIN: 0,
                         qhyccd.CONTROL_EXPOSURE: 0, qhyccd.CONTROL_COOLER: 0}
        self.limits = {qhyccd.CONTROL_GAIN: (0.0, 100.0), qhyccd.CONTROL_EXPOSURE: (1.0, 3600e6)}
        self.params = {qhyccd.CONTROL_CURTEMP: -5.0, qhyccd.CONTROL_CURPWM: 40.0}
        self.release = threading.Event()
        self.release.set()
        self.frame = None

    def handle(self, name, args):
        if name == 'ScanQHYCCD':
            return self.count
        if name == 'GetQHYCCDId':
            args[1].value = b'QHY183M-1a2b3c'
        elif name == 'GetQHYCCDModel':
            args[1].value = b'QHY183M'
        elif name == 'OpenQHYCCD':
            return self.HANDLE
        elif name == 'IsQHYCCDControlAvailable':
            if args[1] == qhyccd.CAM_COLOR:
                return self.bayer if self.bayer else qhyccd.ERROR
            return self.controls.get(args[1], qhyccd.ERROR)
        elif name == 'GetQHYCCDParamMinMaxStep':
            out(args[2]).value, out(args[3]).value = self.limits[args[1]]
            out(args[4]).value = 1
        elif name == 'GetQHYCCDChipInfo':
            for target, value in zip(args[1:], (13.2, 8.8, 64, 32, 2.4, 2.4, 12)):
                out(target).value = value
        elif name == 'SetQHYCCDParam':
            self.params[args[1]] = args[2]
        elif name == 'GetQHYCCDParam':
            return self.params[args[1]]
        elif name == 'GetQHYCCDMemLength':
            return 64 * 32 * 2 + 1024
        elif name == 'ExpQHYCCDSingleFrame':
            self.release.wait(2)
            return 0x2001  # QHYCCD_READ_DIRECTLY: success.
        elif name == 'GetQHYCCDSingleFrame':
            w, h, bpp, channels = self.frame or (self.resolution[2], self.resolution[3], 16, 1)
            for target, value in zip(args[1:5], (w, h, bpp, channels)):
                out(target).value = value
            args[5][:2] = [0x78, 0x56]
        elif name == 'SetQHYCCDResolution':
            self.resolution = args[1:]
        elif name == 'GetQHYCCDParam':
            return 0.0
        return 0


class QHYTests(unittest.TestCase):
    DEVICE = usb(0x1618, 0xc184)

    def make(self, sdk=None, rows=None):
        self.sdk = sdk or FakeQHY()
        self.source = Mock()
        self.source.scan.return_value = rows if rows is not None else [self.DEVICE]
        self.now = 10
        return qhyccd.QHYDriver(self.DEVICE, '/platform/qhy', loader=Mock(return_value=self.sdk),
                                inventory=self.source, clock=lambda: self.now)

    def test_properties_from_chip_info_limits_and_bayer_answer(self):
        info = self.make().info()
        self.assertEqual((info['name'], info['width'], info['height'], info['pixel_um_x'], info['bits']), ('QHY183M', 64, 32, 2.4, 16))
        self.assertEqual((info['gain_min'], info['gain_max'], info['exposure_min'], info['exposure_max']), (0, 100, 1e-6, 3600.0))
        self.assertEqual(info['exposure_resolution'], 1e-6)
        self.assertEqual((info['max_bin'], info['can_abort'], info['cooled'], info['sensor_type']), (1, False, True, 0))
        self.sdk.SetQHYCCDBitsMode.assert_called_once_with(FakeQHY.HANDLE, 16)
        self.sdk.SetQHYCCDStreamMode.assert_called_once_with(FakeQHY.HANDLE, 0)
        self.sdk.CloseQHYCCD.assert_called_once_with(FakeQHY.HANDLE)
        self.sdk.ReleaseQHYCCDResource.assert_called_once_with()
        for code, offset in ((4, [0, 0]), (3, [1, 1]), (2, [1, 0]), (1, [0, 1])):
            self.assertEqual(self.make(FakeQHY(bayer=code)).info()['bayer_offset'], offset)
        self.assertEqual(self.sdk.GetQHYCCDParam.restype, C.c_double)
        self.assertEqual(self.sdk.OpenQHYCCD.restype, C.c_void_p)

    def test_blocking_exposure_runs_on_its_own_thread(self):
        driver = self.make()
        driver.open()
        self.sdk.release.clear()
        driver.start_exposure(0.25, 1, dict(x=2, y=2, width=16, height=8), 30, True)
        self.assertEqual(self.sdk.params[qhyccd.CONTROL_EXPOSURE], 250000.0)
        self.assertEqual(self.sdk.params[qhyccd.CONTROL_GAIN], 30.0)
        self.assertEqual(self.sdk.resolution, (2, 2, 16, 8))
        self.assertEqual(driver.state(), 'exposing')
        with self.assertRaisesRegex(CameraError, 'busy'):
            driver.abort()
        self.sdk.release.set()
        self.assertTrue(wait(lambda: driver.state() == 'download'))
        frame = driver.read_frame()
        self.assertEqual((frame.width, frame.height, frame.data[:2]), (16, 8, bytes([0x78, 0x56])))
        driver.close()
        self.sdk.CloseQHYCCD.assert_called_once()

    def test_errors_ambiguity_and_unreadable_frames(self):
        for count, rows in ((2, None), (0, None), (1, [self.DEVICE, usb(0x1618, 0x0001, '1-3')]), (qhyccd.ERROR, None)):
            sdk = FakeQHY()
            sdk.count = count
            with self.subTest(count=count, rows=rows), self.assertRaisesRegex(CameraError, 'camera_unsupported'):
                self.make(sdk, rows).open()
            sdk.OpenQHYCCD.assert_not_called()
            sdk.ReleaseQHYCCDResource.assert_called_once_with()
        sdk = FakeQHY()
        del sdk.controls[qhyccd.CAM_SINGLEFRAMEMODE]
        with self.assertRaisesRegex(CameraError, 'camera_unsupported'):
            self.make(sdk).open()
        sdk.CloseQHYCCD.assert_called_once()
        driver = self.make()
        driver.open()
        self.sdk.frame = (16, 8, 8, 1)
        driver.start_exposure(0.1, 1, dict(x=0, y=0, width=16, height=8), 1, True)
        self.assertTrue(wait(lambda: driver.state() == 'download'))
        with self.assertRaisesRegex(CameraError, 'capture_failed'):
            driver.read_frame()
        self.sdk.frame = None
        driver.abort()
        self.sdk.fail['ExpQHYCCDSingleFrame'] = qhyccd.ERROR
        driver.start_exposure(0.1, 1, None, 1, True)
        self.assertTrue(wait(lambda: driver.state() == 'error'))
        driver.abort()
        del self.sdk.fail['ExpQHYCCDSingleFrame']
        self.sdk.release.clear()
        driver.start_exposure(0.1, 1, None, 1, True)
        self.now += 100
        self.assertEqual(driver.state(), 'error')
        driver.close()  # The library still waits: the child process ends instead of a concurrent close.
        self.sdk.CloseQHYCCD.assert_not_called()
        self.sdk.release.set()

    def test_cooling_is_read_only(self):
        driver = self.make()
        driver.open()
        self.assertEqual(driver.cooling(), {'temperature': -5.0, 'power': 40.0})
        with self.assertRaisesRegex(CameraError, 'camera_unsupported'):
            driver.set_cooling(True, -10)


# --------------------------------------------------------------------------- Atik

class FakeAtik(Fake):
    SIGNATURES = atik.SIGNATURES
    HANDLE = 0x7000

    def __init__(self, colour=atik.COLOUR_NONE, custom_gain=None):
        super().__init__()
        self.devices = [True]
        self.colour, self.offsets = colour, (1, 0)
        self.custom_gain = custom_gain  # (min, max, current) for Horizon and ACIS cameras
        self.ready, self.failed, self.state = 0, 0, 2
        self.subframe, self.binning, self.set_options = (0, 0, 64, 32), (1, 1), []
        self.cooling_flags, self.temperature = 1 | 2 | 16, -1050
        self.pixels = (C.c_ubyte * (64 * 32 * 2))()
        self.pixels[:2] = [0x9a, 0x78]

    def handle(self, name, args):
        if name == 'ArtemisDeviceCount':
            return len(self.devices)
        if name == 'ArtemisDeviceIsCamera':
            return int(self.devices[args[0]])
        if name == 'ArtemisConnect':
            return self.HANDLE
        if name == 'ArtemisProperties':
            out(args[1]).nPixelsX, out(args[1]).nPixelsY = 64, 32
            out(args[1]).PixelMicronsX = out(args[1]).PixelMicronsY = 3.76
            out(args[1]).Description = b'Atik Horizon II'
        elif name == 'ArtemisColourProperties':
            out(args[1]).value = self.colour
            out(args[2]).value, out(args[3]).value = self.offsets
        elif name == 'ArtemisHasCameraSpecificOption':
            return args[1] == atik.ID_GO_CUSTOM_GAIN and self.custom_gain is not None
        elif name == 'ArtemisCameraSpecificOptionGetData':
            data = b''.join(v.to_bytes(2, 'little') for v in self.custom_gain)
            args[2][:6] = list(data)
            out(args[4]).value = 6
        elif name == 'ArtemisCameraSpecificOptionSetData':
            value = int.from_bytes(bytes(args[2][:2]), 'little')
            self.set_options.append((args[1], value, args[3]))
            if args[1] == atik.ID_GO_CUSTOM_GAIN:
                self.custom_gain = (*self.custom_gain[:2], value)
        elif name == 'ArtemisCoolingInfo':
            out(args[1]).value = self.cooling_flags
            out(args[2]).value = 120
        elif name == 'ArtemisTemperatureSensorInfo':
            out(args[2]).value = 1 if args[1] == 0 else self.temperature
        elif name == 'ArtemisSubframe':
            self.subframe = args[1:]
        elif name == 'ArtemisBin':
            self.binning = args[1:]
        elif name == 'ArtemisGetSubframe':
            for target, value in zip(args[1:], self.subframe):
                out(target).value = value
        elif name == 'ArtemisGetBin':
            out(args[1]).value, out(args[2]).value = self.binning
        elif name == 'ArtemisImageReady':
            return self.ready
        elif name == 'ArtemisImageFailed':
            return self.failed
        elif name == 'ArtemisCameraState':
            return self.state
        elif name == 'ArtemisGetImageData':
            for target, value in zip(args[1:], (self.subframe[0], self.subframe[1], self.subframe[2], self.subframe[3], 1, 1)):
                out(target).value = value
        elif name == 'ArtemisImageBuffer':
            return C.addressof(self.pixels)
        return 0


class AtikTests(unittest.TestCase):
    DEVICE = usb(0x20e7, 0xdfc1)

    def make(self, sdk=None, rows=None):
        self.sdk = sdk or FakeAtik()
        self.source = Mock()
        self.source.scan.return_value = rows if rows is not None else [self.DEVICE]
        self.now = 10
        return atik.AtikDriver(self.DEVICE, '/platform/atik', loader=Mock(return_value=self.sdk),
                               inventory=self.source, clock=lambda: self.now)

    def test_properties_colour_offsets_and_gain_ranges(self):
        info = self.make().info()
        self.assertEqual((info['name'], info['width'], info['height'], info['pixel_um_x']), ('Atik Horizon II', 64, 32, 3.76))
        self.assertEqual((info['bits'], info['sensor_type'], info['gain_min'], info['gain_max'], info['cooled']), (16, 0, 0, 0, True))
        self.assertEqual((info['exposure_min'], info['exposure_max'], info['max_bin']), (0.001, 3600.0, 1))
        self.assertNotIn('exposure_resolution', info)  # SDK step unknown
        self.sdk.ArtemisDisconnect.assert_called_once_with(FakeAtik.HANDLE)
        colour = self.make(FakeAtik(colour=atik.COLOUR_RGGB, custom_gain=(0, 60, 10))).info()
        self.assertEqual((colour['sensor_type'], colour['bayer_offset'], colour['gain_max']), (2, [1, 0], 60))
        with self.assertRaisesRegex(CameraError, 'camera_unsupported'):
            self.make(FakeAtik(colour=atik.COLOUR_UNKNOWN)).info()

    def test_full_cycle_subframe_exposure_ready_and_buffer(self):
        driver = self.make()
        driver.open()
        driver.start_exposure(2.5, 1, dict(x=4, y=2, width=32, height=16), 0, True)
        self.sdk.ArtemisSetPreview.assert_called_once_with(FakeAtik.HANDLE, False)
        self.sdk.ArtemisSubframe.assert_called_once_with(FakeAtik.HANDLE, 4, 2, 32, 16)
        self.sdk.ArtemisStartExposure.assert_called_once_with(FakeAtik.HANDLE, 2.5)
        self.sdk.ArtemisCameraSpecificOptionSetData.assert_not_called()  # A CCD keeps its own gain.
        self.assertEqual(driver.state(), 'exposing')
        self.sdk.ready = 1
        self.assertEqual(driver.state(), 'download')
        frame = driver.read_frame()
        self.assertEqual((frame.width, frame.height, frame.element_type, frame.data[:2]), (32, 16, 'UInt16', bytes([0x9a, 0x78])))
        driver.start_exposure(1, 1, None, 0, True)
        self.sdk.ready, self.sdk.failed = 1, 1
        self.assertEqual(driver.state(), 'error')
        driver.abort()
        self.sdk.ArtemisStopExposure.assert_called_once_with(FakeAtik.HANDLE)
        self.sdk.ready = self.sdk.failed = 0
        self.sdk.state = atik.CAMERA_ERROR
        driver.start_exposure(1, 1, None, 0, True)
        self.assertEqual(driver.state(), 'error')

    def test_cmos_custom_gain_is_written_only_when_it_changes(self):
        driver = self.make(FakeAtik(custom_gain=(0, 60, 10)))
        driver.open()
        driver.start_exposure(1, 1, None, 10, True)
        self.assertEqual(self.sdk.set_options, [])
        driver.abort()
        driver.start_exposure(1, 1, None, 20, True)
        self.assertEqual(self.sdk.set_options, [(atik.ID_GO_PRESET_MODE, 0, 2), (atik.ID_GO_CUSTOM_GAIN, 20, 2)])
        with self.assertRaisesRegex(CameraError, 'invalid_exposure'):
            driver.abort() or driver.start_exposure(1, 1, None, 61, True)

    def test_one_camera_among_devices_and_subframe_readback(self):
        sdk = FakeAtik()
        sdk.devices = [False, True]  # A filter wheel device and the camera.
        driver = self.make(sdk)
        driver.open()
        sdk.ArtemisConnect.assert_called_once_with(1)
        sdk.subframe = (0, 0, 10, 10)
        sdk.ArtemisSubframe.side_effect = lambda *a: 0
        with self.assertRaisesRegex(CameraError, 'invalid_roi'):
            driver.start_exposure(1, 1, dict(x=0, y=0, width=32, height=16), 0, True)
        for devices, rows in (([True, True], None), ([False], None), ([True], [self.DEVICE, usb(0x20e7, 1, '1-3')])):
            sdk = FakeAtik()
            sdk.devices = devices
            with self.subTest(devices=devices), self.assertRaisesRegex(CameraError, 'camera_unsupported'):
                self.make(sdk, rows).open()
            sdk.ArtemisConnect.assert_not_called()

    def test_cooling_in_hundredths_and_warm_up_at_the_end(self):
        driver = self.make()
        driver.open()
        self.assertEqual(driver.cooling(), {'power': 120, 'cooler_on': False, 'temperature': -10.5})
        driver.set_cooling(True, -15)
        self.sdk.ArtemisSetCooling.assert_called_once_with(FakeAtik.HANDLE, -1500)
        driver.close()
        self.sdk.ArtemisCoolerWarmUp.assert_called_once_with(FakeAtik.HANDLE)
        self.sdk.ArtemisDisconnect.assert_called_with(FakeAtik.HANDLE)


# --------------------------------------------------------------------------- Moravian

class FakeMoravian(Fake):
    SIGNATURES = moravian.SIGNATURES
    CAMERA = 0x9000

    def __init__(self):
        super().__init__()
        self.ids = [11]
        self.booleans = {moravian.GBP_SUB_FRAME: True, moravian.GBP_COOLER: True, moravian.GBP_RGB: False,
                         moravian.GBP_CMY: False, moravian.GBP_CMYG: False}
        self.integers = {moravian.GIP_CHIP_W: 4, moravian.GIP_CHIP_D: 2, moravian.GIP_PIXEL_W: 9000, moravian.GIP_PIXEL_D: 9000,
                         moravian.GIP_MINIMAL_EXPOSURE: 125, moravian.GIP_MAXIMAL_EXPOSURE: 3600000,
                         moravian.GIP_DEFAULT_READ_MODE: 1, moravian.GIP_MAX_GAIN: 4030, moravian.GIP_MAX_PIXEL_VALUE: 65535}
        self.ready = False
        self.release = threading.Event()
        self.release.set()

    def handle(self, name, args):
        if name == 'gxccd_enumerate_usb':
            for ident in self.ids:
                args[0](ident)
            return None
        if name == 'gxccd_initialize_usb':
            return self.CAMERA
        if name == 'gxccd_get_boolean_parameter':
            out(args[2]).value = self.booleans[args[1]]
        elif name == 'gxccd_get_integer_parameter':
            if args[1] not in self.integers:
                return -1
            out(args[2]).value = self.integers[args[1]]
        elif name == 'gxccd_get_string_parameter':
            args[2].value = {0: b'C3-61000 Pro', 3: b'IMX455'}[args[1]]
        elif name == 'gxccd_get_value':
            out(args[2]).value = {0: -20.25, 11: 0.5}[args[1]]
        elif name == 'gxccd_image_ready':
            out(args[1]).value = self.ready
        elif name == 'gxccd_read_image':
            self.release.wait(2)
            width = args[2] // 2 // 2
            # Two rows, bottom row first: 1, 2, ... then 101, 102, ...
            rows = [list(range(1, width + 1)), list(range(101, 101 + width))]
            values = b''.join(v.to_bytes(2, 'little') for row in rows for v in row)
            C.memmove(args[1], values, len(values))
        return 0


class MoravianTests(unittest.TestCase):
    DEVICE = usb(0x1347, 0x0c02)

    def make(self, sdk=None, rows=None):
        self.sdk = sdk or FakeMoravian()
        self.source = Mock()
        self.source.scan.return_value = rows if rows is not None else [self.DEVICE]
        self.now = 10
        return moravian.MoravianDriver(self.DEVICE, '/platform/moravian', loader=Mock(return_value=self.sdk),
                                       inventory=self.source, clock=lambda: self.now)

    def test_parameters_in_documented_units(self):
        info = self.make().info()
        self.assertEqual((info['name'], info['sensor_name'], info['width'], info['height'], info['pixel_um_x']),
                         ('C3-61000 Pro', 'IMX455', 4, 2, 9.0))
        self.assertEqual((info['exposure_min'], info['exposure_max'], info['gain_max'], info['max_adu']), (0.000125, 3600.0, 4030, 65535))
        self.assertNotIn('exposure_resolution', info)  # SDK step unknown
        self.sdk.gxccd_initialize_usb.assert_called_once_with(11)
        self.sdk.gxccd_set_read_mode.assert_called_once_with(FakeMoravian.CAMERA, 1)
        self.sdk.gxccd_set_binning.assert_called_once_with(FakeMoravian.CAMERA, 1, 1)
        self.sdk.gxccd_release.assert_called_once_with(FakeMoravian.CAMERA)
        self.assertIsNone(self.sdk.gxccd_release.restype)
        for flag in (moravian.GBP_RGB, moravian.GBP_CMY, moravian.GBP_CMYG):
            sdk = FakeMoravian()
            sdk.booleans[flag] = True
            with self.subTest(flag=flag), self.assertRaisesRegex(CameraError, 'camera_unsupported'):
                self.make(sdk).info()

    def test_readout_thread_and_bottom_up_rows_turned_top_down(self):
        driver = self.make()
        driver.open()
        driver.start_exposure(1.5, 1, None, 200, True)
        self.sdk.gxccd_set_gain.assert_called_once_with(FakeMoravian.CAMERA, 200)
        self.sdk.gxccd_start_exposure.assert_called_once_with(FakeMoravian.CAMERA, 1.5, True, 0, 0, 4, 2)
        self.assertEqual(driver.state(), 'exposing')
        self.sdk.release.clear()
        self.sdk.ready = True
        self.assertEqual(driver.state(), 'reading')
        with self.assertRaisesRegex(CameraError, 'busy'):
            driver.start_exposure(1, 1, None, 0, True)
        self.sdk.release.set()
        self.assertTrue(wait(lambda: driver.state() == 'download'))
        frame = driver.read_frame()
        values = [int.from_bytes(frame.data[i:i + 2], 'little') for i in range(0, len(frame.data), 2)]
        self.assertEqual(values, [101, 102, 103, 104, 1, 2, 3, 4])

    def test_abort_while_exposing_or_reading_and_no_gain_or_subframe(self):
        driver = self.make()
        driver.open()
        driver.start_exposure(1, 1, None, 0, True)
        driver.abort()
        self.sdk.gxccd_abort_exposure.assert_called_once_with(FakeMoravian.CAMERA, False)
        self.assertEqual(driver.state(), 'idle')
        self.sdk.release.clear()
        driver.start_exposure(1, 1, None, 0, True)
        self.sdk.ready = True
        self.assertEqual(driver.state(), 'reading')
        driver.abort()
        self.sdk.release.set()
        self.assertTrue(wait(lambda: driver.state() == 'error'))
        driver.close()
        sdk = FakeMoravian()
        del sdk.integers[moravian.GIP_MAX_GAIN]
        sdk.booleans[moravian.GBP_SUB_FRAME] = False
        driver = self.make(sdk)
        driver.open()
        self.assertEqual(driver.info()['gain_max'], 0)
        with self.assertRaisesRegex(CameraError, 'invalid_roi'):
            driver.start_exposure(1, 1, dict(x=0, y=0, width=2, height=2), 0, True)
        driver.start_exposure(1, 1, None, 0, True)
        sdk.gxccd_set_gain.assert_not_called()

    def test_identity_errors_and_cooling(self):
        for ids, rows in (([], None), ([1, 2], None), ([1], [self.DEVICE, usb(0x1347, 0x0001, '1-3')])):
            sdk = FakeMoravian()
            sdk.ids = ids
            with self.subTest(ids=ids), self.assertRaisesRegex(CameraError, 'camera_unsupported'):
                self.make(sdk, rows).open()
            sdk.gxccd_initialize_usb.assert_not_called()
        sdk = FakeMoravian()
        sdk.fail['gxccd_initialize_usb'] = None
        with self.assertRaisesRegex(CameraError, 'capture_failed'):
            self.make(sdk).open()
        driver = self.make()
        driver.open()
        self.assertEqual(driver.cooling(), {'temperature': -20.25, 'power': 0.5})
        driver.set_cooling(True, -10)
        self.sdk.gxccd_set_temperature.assert_called_once_with(FakeMoravian.CAMERA, -10.0)
        with self.assertRaisesRegex(CameraError, 'camera_unsupported'):
            driver.set_cooling(False)
        self.sdk.fail['gxccd_start_exposure'] = -1
        with self.assertRaisesRegex(CameraError, 'capture_failed'):
            driver.start_exposure(1, 1, None, 0, True)
        self.assertEqual(driver.state(), 'error')


# --------------------------------------------------------------------------- Common: child process, library, registry

ADAPTERS = {
    'player_one': (lambda: FakePlayerOne(), PlayerOneTests.DEVICE, player_one.PlayerOneDriver, 'ready', 1),
    'qhyccd': (lambda: FakeQHY(), QHYTests.DEVICE, qhyccd.QHYDriver, None, None),
    'atik': (lambda: FakeAtik(), AtikTests.DEVICE, atik.AtikDriver, 'ready', 1),
    'moravian': (lambda: FakeMoravian(), MoravianTests.DEVICE, moravian.MoravianDriver, 'ready', True),
}


class CommonTests(unittest.TestCase):
    def test_each_adapter_declares_itself_unverified_like_the_catalog(self):
        entries = driver_catalog.entries()
        for vendor, module in (('player_one', player_one), ('qhyccd', qhyccd), ('atik', atik), ('moravian', moravian)):
            with self.subTest(vendor=vendor):
                self.assertIs(module.VERIFIED_ON_HARDWARE, False)
                self.assertFalse(entries[vendor]['verified_on_hardware'])
                self.assertFalse(vendor_process.verified_on_hardware(vendor))
                self.assertIn(module.VENDOR_ID, entries[vendor]['usb_vendors'])
        # ZWO: one ASI220MM with SDK 1.41 from an image. The delivered SDK 1.42 and its install path are not verified.
        self.assertIs(zwo_asi.VERIFIED_ON_HARDWARE, False)
        self.assertFalse(vendor_process.verified_on_hardware('zwo_asi'))
        self.assertFalse(entries['zwo_asi']['verified_on_hardware'])
        self.assertEqual(zwo_asi.VERIFIED_SDK_VERSIONS, ('1.41',))
        self.assertNotIn('1.42', zwo_asi.VERIFIED_SDK_VERSIONS)
        self.assertFalse(vendor_process.verified_on_hardware('touptek'))

    def test_full_cycle_through_the_child_protocol(self):
        for vendor, (make, device, kind, flag, value) in ADAPTERS.items():
            with self.subTest(vendor=vendor):
                sdk, source = make(), Mock()
                source.scan.return_value = [device]
                driver = kind(device, '/platform/' + vendor, loader=Mock(return_value=sdk), inventory=source)
                parent, child = socket.socketpair()
                proxy = vendor_process.IsolatedDriver(vendor, device=device)
                proxy.sock = parent
                thread = threading.Thread(target=lambda: serve(child, factory=lambda d: driver))
                thread.start()
                try:
                    info = proxy._call('open', device)
                    self.assertEqual(info, driver.info())
                    proxy.start_exposure(0.01, 1, None, info['gain_min'], True)
                    if flag:
                        setattr(sdk, flag, value)
                    self.assertTrue(wait(lambda: proxy.state() == 'download'))
                    frame = proxy.read_frame()
                    self.assertEqual((frame.width, frame.height), (info['width'], info['height']))
                    proxy._call('close')
                finally:
                    parent.close()
                    thread.join(timeout=2)
                    child.close()
                self.assertFalse(thread.is_alive())

    def test_library_that_does_not_load_answers_driver_unavailable_through_the_child(self):
        device = PlayerOneTests.DEVICE
        driver = player_one.PlayerOneDriver(device, '/absent', loader=Mock(side_effect=CameraError('camera_driver_unavailable')),
                                            inventory=Mock())
        parent, child = socket.socketpair()
        proxy = vendor_process.IsolatedDriver('player_one', device=device)
        proxy.sock = parent
        thread = threading.Thread(target=lambda: serve(child, factory=lambda d: driver))
        thread.start()
        try:
            with self.assertRaisesRegex(CameraError, 'camera_driver_unavailable'):
                proxy._call('open', device)
        finally:
            parent.close()
            thread.join(timeout=2)
            child.close()
        self.assertEqual(proxy.command(5)[2:], ['wifi_bridge.cameras.vendor_process', 'player_one', '5'])

    def test_registry_matches_whole_vendor_and_a_missing_library_keeps_the_bridge_running(self):
        registry = load_registry()
        for vendor, (_, device, _, _, _) in ADAPTERS.items():
            self.assertEqual(registry.match_device(device)['driver'], vendor)
            self.assertTrue(registry.available(vendor))
        self.assertIsNone(registry.match_device(usb(0xa0a0, 0xf001)))  # Player One filter wheel.
        self.assertEqual(registry.match_device(usb(0x03c3, 0x224a))['driver'], 'zwo_asi')
        self.assertIsNone(registry.match_device(dict(usb(0x1618, 0x1), interface_classes=[2])))
        with TemporaryDirectory() as folder, patch.object(drivers, 'DRIVERS', Path(folder) / 'none'):
            service = CameraService()
            try:
                devices = [dict(ADAPTERS[v][1], port_path='1-%d' % (i + 2)) for i, v in enumerate(ADAPTERS)]
                service.reconcile_usb(devices, registry)
                states = {row['driver']: row['discovery']['state'] for row in service.inventory()}
                self.assertEqual(states, {vendor: 'driver_missing' for vendor in ADAPTERS})
            finally:
                service.close()

    def test_whole_vendor_rules_validate_and_give_root_only_udev_lines(self):
        from wifi_bridge.cameras.registry import validate_manifest, CameraRegistry
        table = json.loads((Path(__file__).resolve().parents[1] / 'cameras/player_one.json').read_text())
        self.assertEqual(validate_manifest(table), table)
        for rule in ({'vendor_id': 0x03c3, 'except_product_ids': []}, {'vendor_id': 0xa0a0, 'except_product_ids': [1, 1]},
                     {'vendor_id': 0xa0a0, 'except_product_ids': 'x'}, {'vendor_id': 0xa0a0, 'except_product_ids': [70000]}):
            with self.subTest(rule=rule), self.assertRaises(CameraError):
                validate_manifest({**table, 'usb': [rule]})
        with self.assertRaises(CameraError):
            validate_manifest({'schema_version': 1, 'id': 'x', 'driver': 'uvc', 'validation': 'unverified',
                               'usb': [{'vendor_id': 1, 'except_product_ids': []}]})
        with self.assertRaisesRegex(CameraError, 'ambiguous_camera_usb'):
            CameraRegistry([table, {**table, 'id': 'player_one_2', 'usb': [{'vendor_id': 0xa0a0, 'except_product_ids': [2]}]}])
        exact = {**table, 'id': 'mars', 'usb': [{'vendor_id': 0xa0a0, 'product_id': 0x1234}]}
        self.assertEqual(CameraRegistry([table, exact]).match_device(PlayerOneTests.DEVICE)['id'], 'mars')
        rules = drivers.udev_rules('player_one')
        self.assertEqual(rules, 'SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="a0a0", '
                                'ATTR{idProduct}!="f001", OWNER="root", GROUP="root", MODE="0600"\n')
        self.assertIn('ATTR{idVendor}=="1618", OWNER="root"', drivers.udev_rules('qhyccd'))

    def test_installed_and_accepted_library_only(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            base.chmod(0o755)
            library = b'\x7fELF synthetic Player One library'
            install = base / 'player-one'
            install.mkdir(mode=0o755)
            path = install / 'libPlayerOneCamera.so.3.10.1'
            path.write_bytes(library)
            import hashlib
            digest = hashlib.sha256(library).hexdigest()
            drivers.write_private_json(install / 'platform.json', {'path': str(path), 'sha256': digest})
            with root_owned(base):
                with self.assertRaisesRegex(CameraError, 'camera_driver_unavailable'):
                    vendor_platform.resolve('player_one', base)  # Installed, not accepted.
                clock = {'clock_status': 'unverified', 'corrected_utc': '2026-11-20T10:00:00.000+00:00'}
                drivers.write_private_json(drivers.acceptance_path('player_one', base),
                                           drivers.acceptance_record('player_one', '3.10.1', digest, '1' * 64, clock, 'web'))
                self.assertEqual(vendor_platform.declared_path('player_one', base), path)
                path.write_bytes(library + b'changed')
                with self.assertRaisesRegex(CameraError, 'camera_driver_unavailable'):
                    vendor_platform.resolve('player_one', base)
                with self.assertRaisesRegex(CameraError, 'camera_driver_unavailable'):
                    vendor_platform.resolve('atik', base)
            with self.assertRaisesRegex(CameraError, 'camera_driver_unavailable'):
                vendor_platform.resolve('player_one', base)  # Not root-owned.

    def test_driver_rows_offer_only_manufacturers_with_an_adapter(self):
        value = driver_catalog.catalog()
        with TemporaryDirectory() as folder:
            every = {r['id'] for r in drivers.rows(value, 'arm64', True, Path(folder))}
            offered = {r['id'] for r in drivers.rows(value, 'arm64', True, Path(folder), adapters={'zwo_asi', 'atik'})}
        self.assertEqual(every, {'zwo_asi', 'qhyccd', 'player_one', 'atik', 'moravian'})
        self.assertEqual(offered, {'zwo_asi', 'atik'})

    def test_alpaca_camera_over_an_adapter_reports_no_abort_for_qhy(self):
        sdk, source = FakeQHY(), Mock()
        source.scan.return_value = [QHYTests.DEVICE]
        driver = qhyccd.QHYDriver(QHYTests.DEVICE, '/p', loader=Mock(return_value=sdk), inventory=source)
        camera = Camera('fixture', 'qhyccd', driver, time.monotonic)
        call = lambda method, name, params=None: camera.call(method, name, params or {}, 1, '127.0.0.1')
        call('PUT', 'connected', {'connected': 'true'})
        self.assertFalse(call('GET', 'canabortexposure'))
        call('PUT', 'startexposure', {'duration': '0.01', 'light': 'true'})
        self.assertTrue(wait(lambda: call('GET', 'imageready')))
        self.assertEqual(call('GET', 'imagearray').width, 64)
        camera.release()

    def test_native_inventory_says_whether_the_driver_was_verified_on_hardware(self):
        from wifi_bridge.cameras.native import NativeCameras
        rows = [{'id': 'p1', 'name': 'Mars-M', 'driver': 'player_one'}, {'id': 'z1', 'name': 'ASI224MC', 'driver': 'zwo_asi'},
                {'id': 's1', 'name': 'simulator', 'driver': 'simulator'}]
        service = SimpleNamespace(inventory=lambda address: [dict(r) for r in rows])
        controller = SimpleNamespace(camera_service=service, camera_supported=True, lock=threading.RLock(),
                                     store=SimpleNamespace(config={'telescopes': []}), state={}, protection=SimpleNamespace(leases={}))
        result = {row['id']: row for row in NativeCameras(controller).inventory()}
        self.assertIs(result['p1']['verified_on_hardware'], False)
        self.assertIs(result['z1']['verified_on_hardware'], False)
        self.assertNotIn('verified_on_hardware', result['s1'])


if __name__ == '__main__':
    unittest.main()
