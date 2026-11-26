"""ASI 1.41 mapping and child protocol, with Python fakes and no USB access."""
from ctypes import c_int, c_long, sizeof
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from wifi_bridge.cameras.alpaca import AlpacaError, Camera
from wifi_bridge.cameras.driver import CameraError
from wifi_bridge.cameras.registry import load_registry
from wifi_bridge.cameras.service import CameraService
from wifi_bridge.cameras.zwo_asi import ASIDriver, CameraInfo, ControlCaps, SIGNATURES
from wifi_bridge.cameras.zwo_process import IsolatedASI, serve

DEVICE = dict(vendor_id=0x03c3, product_id=0x224a, device_class=0, interface_classes=[255],
              bus=1, port_path='1-2', speed_mbps=480)


class FakeSDK:
    def __init__(self):
        self.calls = []
        self.fail = {}
        self.status, self.count = 1, 1
        self.prop = CameraInfo(name=b'ZWO ASI224MC', ident=17, width=128, height=64,
                               pitch=3.75, color=0, depth=12, cooler=1)
        self.prop.formats[:3] = [2, 0, -1]
        self.prop.bins[:4] = [1, 2, 4, 0]
        self.ranges = [(0, 1, 50, 1), (1, 10, 60000000, 1)]
        self.readback = None
        self.controls = {}
        for name in SIGNATURES:
            setattr(self, name, Mock(side_effect=lambda *args, n=name: self.call(n, args)))

    def call(self, name, args):
        self.calls.append(name)
        if name in self.fail:
            return self.fail[name]
        if name == 'ASIGetNumOfConnectedCameras':
            return self.count
        if name == 'ASICameraCheck':
            return int(args[0] == 0x03c3 and args[1] != 0x1f10)
        if name == 'ASIGetCameraProperty':
            import ctypes
            ctypes.memmove(args[0], ctypes.byref(self.prop), sizeof(self.prop))
            assert args[1] == 0
        elif name in ('ASIOpenCamera', 'ASIInitCamera', 'ASICloseCamera'):
            assert args == (17,)
        elif name == 'ASIGetNumOfControls':
            args[1]._obj.value = len(self.ranges)
        elif name == 'ASIGetControlCaps':
            item = args[2]._obj
            item.kind, item.minimum, item.maximum, item.writable = self.ranges[args[1]]
        elif name == 'ASISetROIFormat':
            self.roi = args[1:]
        elif name == 'ASISetStartPos':
            self.origin = args[1:]
        elif name in ('ASIGetROIFormat', 'ASIGetStartPos'):
            wanted = self.readback or self.roi if name == 'ASIGetROIFormat' else self.origin
            for target, value in zip(args[1:], wanted):
                target._obj.value = value
        elif name == 'ASISetControlValue':
            self.controls[args[1]] = args[2:]
        elif name == 'ASIGetControlValue':
            args[2]._obj.value, args[3]._obj.value = self.controls[args[1]]
        elif name == 'ASIGetExpStatus':
            args[1]._obj.value = self.status
        elif name == 'ASIGetDataAfterExp':
            assert len(args[1]) == args[2]
            args[1][:2] = [0x34, 0x12]
        return 0


class ASITests(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeSDK()
        self.source = Mock()
        self.source.scan.return_value = [DEVICE]
        self.loader = Mock(return_value=self.sdk)
        self.now = 40
        self.driver = ASIDriver(DEVICE, '/platform/selected', loader=self.loader, inventory=self.source,
                                clock=lambda: self.now)

    def test_native_layout_signatures_and_lazy_id_lifecycle(self):
        self.loader.assert_not_called()
        info = self.driver.info()
        self.loader.assert_called_once_with('/platform/selected')
        self.assertEqual(self.sdk.ASISetControlValue.argtypes, [c_int, c_int, c_long, c_int])
        self.assertTrue(all(getattr(self.sdk, name).restype == c_int for name in SIGNATURES))
        if sizeof(c_long) == 8:  # LP64 size/offsets from ASI_CAMERA_INFO, ASI_CONTROL_CAPS.
            self.assertEqual((sizeof(CameraInfo), CameraInfo.pitch.offset, sizeof(ControlCaps)), (248, 192, 264))
        self.assertEqual((info['width'], info['height'], info['pixel_um_x'], info['max_bin']),
                         (128, 64, 3.75, 2))
        self.assertEqual((info['gain_min'], info['gain_max'], info['exposure_min'], info['exposure_max']),
                         (1, 50, 0.00001, 60))
        self.assertEqual(info['exposure_resolution'], 1e-6)
        self.assertEqual((info['bits'], info['cooled'], info['can_abort']), (16, True, True))
        self.sdk.ASICloseCamera.assert_called_once_with(17)
        self.driver.open()
        self.driver.open()
        self.assertEqual(self.sdk.ASIOpenCamera.call_count, 2)
        self.driver.close()
        self.driver.close()
        self.assertEqual(self.sdk.ASICloseCamera.call_count, 2)

    def test_asi220_exact_usb_and_sdk_identity(self):
        device = dict(DEVICE, product_id=0x2209)
        self.source.scan.return_value = [device]
        driver = ASIDriver(device, '/platform/selected', loader=self.loader, inventory=self.source)
        self.sdk.prop.name = b'ZWO ASI220MM Mini'
        driver.open(); driver.close()
        self.sdk.prop.name = b'ZWO ASI220MM'
        with self.assertRaisesRegex(CameraError, 'camera_unsupported'):
            driver.open()

    def test_roi_controls_status_frame_and_consume(self):
        self.driver.open()
        roi = dict(x=8, y=4, width=32, height=16)
        self.driver.start_exposure(0.012345, 2, roi, 7, False)
        self.sdk.ASISetROIFormat.assert_called_once_with(17, 16, 8, 2, 2)
        self.sdk.ASISetStartPos.assert_called_once_with(17, 4, 2)
        self.assertEqual([a.args for a in self.sdk.ASISetControlValue.call_args_list],
                         [(17, 1, 12345, 0), (17, 0, 7, 0)])
        self.sdk.ASIStartExposure.assert_called_once_with(17, 0)
        self.assertEqual(self.driver.state(), 'exposing')
        with self.assertRaisesRegex(CameraError, 'busy'):
            self.driver.start_exposure(1, 1, None, 7, True)
        self.sdk.status = 2
        self.assertEqual(self.driver.state(), 'download')
        frame = self.driver.read_frame()
        self.assertEqual((frame.width, frame.height, frame.element_type, len(frame.data)), (16, 8, 'UInt16', 256))
        self.assertEqual(frame.data[:2], bytes([0x34, 0x12]))
        self.assertEqual((frame.metadata['sequence'], frame.metadata['roi_x'], frame.metadata['exposure_seconds']),
                         (1, 8, 0.012345))
        with self.assertRaisesRegex(CameraError, 'not_ready'):
            self.driver.read_frame()
        self.driver.close()

    def test_ambiguous_disappeared_inaccessible_and_unknown_are_never_opened(self):
        for count, rows in [(0, [DEVICE]), (2, [DEVICE]), (1, None), (1, []),
                            (1, [dict(DEVICE, port_path='1-3')]),
                            (1, [DEVICE, dict(DEVICE, product_id=0x9876, port_path='1-3')])]:
            self.source.scan.return_value = rows
            self.sdk.count = count
            with self.subTest(count=count, rows=rows), self.assertRaisesRegex(CameraError, 'camera_unsupported'):
                self.driver.open()
        self.sdk.ASIOpenCamera.assert_not_called()
        self.source.scan.return_value = [DEVICE]
        self.sdk.prop.name = b'ZWO ASI120MM'
        with self.assertRaisesRegex(CameraError, 'camera_unsupported'):
            self.driver.open()
        self.sdk.ASIOpenCamera.assert_not_called()
        self.sdk.prop.name = b'ZWO ASI224MC'
        # An unrelated filter wheel is not an ASI camera according to ASICameraCheck.
        self.source.scan.return_value = [DEVICE, dict(DEVICE, product_id=0x1f10)]
        self.driver.open()
        self.driver.close()
        self.source.scan.side_effect = [[DEVICE], []]
        with self.assertRaisesRegex(CameraError, 'camera_unsupported'):
            self.driver.open()
        self.assertIsNone(self.driver.ident)

    def test_unsupported_properties_and_init_failures_close_partial_handle(self):
        for field, value in [('pitch', 0), ('width', 16385), ('depth', 0), ('shutter', 1), ('color', 5)]:
            with self.subTest(field=field):
                with patch.object(self.sdk, 'prop', CameraInfo.from_buffer_copy(self.sdk.prop)):
                    setattr(self.sdk.prop, field, value)
                    with self.assertRaises(CameraError):
                        self.driver.open()
                    self.assertIsNone(self.driver.ident)
        self.sdk.fail['ASIInitCamera'] = 5
        with self.assertRaisesRegex(CameraError, 'capture_failed'):
            self.driver.open()
        self.assertEqual(self.sdk.ASICloseCamera.call_count, 6)
        del self.sdk.fail['ASIInitCamera']
        self.sdk.ranges = [(0, 1, 12, 0), (1, 1, 10, 1)]
        with self.assertRaisesRegex(CameraError, 'camera_unsupported'):
            self.driver.info()

    def test_raw8_bayer_origin_and_roi_readback_errors(self):
        self.sdk.prop.formats[:2] = [0, -1]
        self.sdk.prop.color, self.sdk.prop.bayer = 1, 3
        self.driver.open()
        info = self.driver.info()
        self.assertEqual((info['bits'], info['sensor_type'], info['bayer_offset'], info['max_bin']),
                         (8, 2, [0, 1], 1))
        for roi in [dict(x=1,y=0,width=16,height=8), dict(x=0,y=0,width=10,height=8)]:
            with self.assertRaisesRegex(CameraError, 'invalid_roi'):
                self.driver.start_exposure(1, 1, roi, 1, True)
        self.sdk.ASISetROIFormat.assert_not_called()
        self.sdk.readback = [8, 2, 1, 0]
        with self.assertRaisesRegex(CameraError, 'invalid_roi'):
            self.driver.start_exposure(1, 1, None, 1, True)
        self.sdk.ASIStartExposure.assert_not_called()
        self.driver.close()

    def test_failed_poll_abort_timeout_and_transfer_do_not_publish_stale_data(self):
        self.driver.open()
        self.driver.start_exposure(1, 1, None, 1, True)
        self.now += 12
        self.assertEqual(self.driver.state(), 'error')
        self.driver.abort()
        self.sdk.ASIStopExposure.assert_called_once_with(17)
        self.driver.start_exposure(1, 1, None, 1, True)
        self.sdk.fail['ASIGetExpStatus'] = 5
        self.assertEqual(self.driver.state(), 'error')
        self.driver.close()
        self.driver.open()
        self.driver.start_exposure(1, 1, None, 1, True)
        del self.sdk.fail['ASIGetExpStatus']
        self.sdk.status = 2
        self.assertEqual(self.driver.state(), 'download')
        self.sdk.fail['ASIGetDataAfterExp'] = 13
        with self.assertRaisesRegex(CameraError, 'capture_failed'):
            self.driver.read_frame()
        self.assertEqual(self.driver.state(), 'error')
        self.driver.close()

    def test_failed_replacement_invalidates_previous_alpaca_image(self):
        camera = Camera('fixture','zwo_asi',self.driver,time.monotonic)
        def call(method, name, params=None):
            return camera.call(method, name, params or {}, 1, '127.0.0.1')
        call('PUT','connected',{'connected':'true'})
        exposure = {'duration':'1','light':'true'}
        call('PUT','startexposure',exposure)
        self.sdk.status = 2
        self.assertEqual(call('GET','imagearray').width, 128)
        self.sdk.fail['ASISetControlValue'] = 5
        with self.assertRaises(CameraError):
            call('PUT','startexposure',exposure)
        self.assertFalse(call('GET','imageready'))
        with self.assertRaises(AlpacaError):
            call('GET','imagearray')
        camera.release()

    def test_process_protocol_does_not_pickle_frames_or_expose_native_errors(self):
        parent, child = socket.socketpair()
        proxy = IsolatedASI(device=DEVICE)
        proxy.sock = parent
        def run():
            with child:
                serve(child, factory=lambda device: self.driver)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertEqual(proxy._call('open', DEVICE), self.driver.info())
            proxy.start_exposure(1, 1, None, 1, True)
            self.sdk.status = 2
            self.assertEqual(proxy.state(), 'download')
            self.assertEqual(proxy.read_frame().data[:2], bytes([0x34, 0x12]))
            proxy._call('close')
        finally:
            parent.close()
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive())

    def test_real_child_fds_and_crash_recovery_with_python_camera(self):
        proxy = IsolatedASI(device=DEVICE)
        run = subprocess.Popen
        child = ('import socket,sys; from wifi_bridge.cameras.zwo_process import serve; '
                 'from wifi_bridge.cameras.simulator import SimulatedCamera; '
                 'serve(socket.socket(fileno=int(sys.argv[1])),lambda device:SimulatedCamera(width=8,height=4))')
        def spawn(command, **options):
            self.assertEqual(command[:3], [sys.executable,'-m','wifi_bridge.cameras.zwo_process'])
            self.assertEqual(options['stderr'], subprocess.DEVNULL)
            return run([sys.executable,'-c',child,command[-1]], **options)
        with patch('wifi_bridge.cameras.zwo_process.subprocess.Popen', side_effect=spawn):
            try:
                self.assertEqual(proxy.info()['width'], 8)
                self.assertIsNone(proxy.process)
                proxy.open()
                proxy.process.kill()
                proxy.process.wait(timeout=2)
                self.assertEqual(proxy.state(), 'error')
                self.assertIsNone(proxy.process)
                proxy.close()
                proxy.open()
                self.assertEqual(proxy.state(), 'idle')
            finally:
                proxy.close()

    def test_unreaped_child_cannot_be_replaced(self):
        proxy = IsolatedASI(device=DEVICE)
        proc = proxy.process = Mock()
        proc.wait.side_effect = subprocess.TimeoutExpired('isolated-helper', 0.1)
        proc.poll.return_value = None
        proxy._stop()
        self.assertIs(proxy.process, proc)
        with patch('subprocess.Popen') as spawn, self.assertRaisesRegex(CameraError,'capture_failed'):
            proxy.open()
        spawn.assert_not_called()

    def test_timeout_and_child_exit_leave_release_possible(self):
        for crash in (False, True):
            left, right = socket.socketpair()
            proxy = IsolatedASI(device=DEVICE)
            proxy.sock = left
            with right, patch('wifi_bridge.cameras.zwo_process.CALL_SECONDS', 0.02):
                if crash:
                    right.close()
                before = time.monotonic()
                self.assertEqual(proxy.state(), 'error')
                self.assertLess(time.monotonic() - before, 1)
                proxy.close()
                self.assertIsNone(proxy.sock)

    def test_missing_declaration_is_reported_through_normal_inventory(self):
        registry = load_registry()
        self.assertIsNone(registry.match_device(dict(DEVICE, product_id=0x1f10)))
        registry._factories['zwo_asi'] = lambda device: ASIDriver(
            device, '/absent', loader=Mock(side_effect=CameraError('camera_driver_unavailable')),
            inventory=self.source)
        service = CameraService()
        try:
            service.reconcile_usb([DEVICE], registry)
            record, = service.inventory()
            self.assertEqual(record['discovery']['state'], 'driver_missing')
            self.assertIsNone(record['alpaca'])
            self.assertEqual(record['driver'], 'zwo_asi')
        finally:
            service.close()
