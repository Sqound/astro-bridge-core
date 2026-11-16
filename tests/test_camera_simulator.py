"""Synchronous simulated camera behavior with injected time; never sleeps."""
import struct
import unittest
from unittest.mock import patch

from wifi_bridge.cameras.driver import CameraDriver, CameraError, INFO_FIELDS, validate_info
from wifi_bridge.cameras.simulator import SimulatedCamera


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class SimulatorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.camera = SimulatedCamera(seed=7, width=40, height=30, clock=self.clock)
        self.camera.open()

    def expose(self, camera=None, **kwargs):
        camera = camera or self.camera
        camera.start_exposure(1, **kwargs)
        self.clock.now += 1.3
        return camera.read_frame()

    def test_contract_defaults_and_info_are_complete_and_detached(self):
        camera = SimulatedCamera()
        self.assertIsInstance(camera, CameraDriver)
        info = camera.info()
        self.assertEqual(set(info), set(INFO_FIELDS))
        self.assertEqual((info['width'], info['height'], info['bits'], info['max_adu']), (1280, 960, 16, 65535))
        self.assertEqual(info['sensor_type'], 0)
        self.assertEqual(info['max_bin'], 4)
        info['bayer_offset'][0] = 1
        self.assertEqual(camera.info()['bayer_offset'], [0, 0])

    def test_deadline_state_machine_and_consumed_download(self):
        self.assertEqual(self.camera.state(), 'idle')
        self.camera.start_exposure(2)
        self.assertEqual(self.camera.state(), 'exposing')
        with self.assertRaisesRegex(CameraError, 'not_ready'):
            self.camera.read_frame()
        self.clock.now = 102
        self.assertEqual(self.camera.state(), 'reading')
        self.clock.now = 102.299
        self.assertEqual(self.camera.state(), 'reading')
        self.clock.now = 102.3
        self.assertEqual(self.camera.state(), 'download')
        frame = self.camera.read_frame()
        self.assertEqual((frame.width, frame.height, len(frame.data)), (40, 30, 2400))
        self.assertEqual(self.camera.state(), 'idle')
        with self.assertRaisesRegex(CameraError, 'not_ready'):
            self.camera.read_frame()

    def test_polling_can_skip_directly_to_download_and_open_is_idempotent(self):
        self.camera.start_exposure(1)
        self.camera.open()
        self.clock.now += 50
        self.assertEqual(self.camera.state(), 'download')
        self.camera.read_frame()

    def test_active_exposure_cannot_be_replaced_but_download_can(self):
        self.camera.start_exposure(1)
        for now in (100, 101.1):
            self.clock.now = now
            with self.assertRaisesRegex(CameraError, 'busy'):
                self.camera.start_exposure(2)
        self.clock.now = 102
        self.camera.start_exposure(2)
        self.clock.now = 104.3
        self.assertEqual(self.camera.read_frame().metadata['sequence'], 2)

    def test_abort_discards_exposure_readout_and_download_and_allows_restart(self):
        for phase in (0.2, 1.1, 1.3):
            with self.subTest(phase=phase):
                self.camera.start_exposure(1)
                self.clock.now += phase
                self.camera.abort()
                self.assertEqual(self.camera.state(), 'idle')
                with self.assertRaisesRegex(CameraError, 'not_ready'):
                    self.camera.read_frame()
        self.camera.abort()
        self.assertEqual(len(self.expose().data), 2400)

    def test_close_is_idempotent_discards_and_reopen_is_empty(self):
        self.camera.start_exposure(1)
        self.camera.close()
        self.camera.close()
        self.clock.now += 5
        for call in (lambda: self.camera.start_exposure(1), self.camera.read_frame, self.camera.abort):
            with self.assertRaisesRegex(CameraError, 'not_open'):
                call()
        self.camera.open()
        self.assertEqual(self.camera.state(), 'idle')
        with self.assertRaisesRegex(CameraError, 'not_ready'):
            self.camera.read_frame()

    def test_same_seed_parameters_and_repeated_exposures_are_deterministic(self):
        other = SimulatedCamera(seed=7, width=40, height=30, clock=self.clock)
        other.open()
        first = self.expose()
        second = self.expose(other)
        third = self.expose()
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.data, third.data)
        self.assertEqual(first.metadata['sequence'], 1)
        self.assertEqual(third.metadata['sequence'], 2)
        different = SimulatedCamera(seed=8, width=40, height=30, clock=self.clock)
        different.open()
        self.assertNotEqual(first.data, self.expose(different).data)

    def test_binning_roi_dark_gain_and_noise_are_bounded(self):
        frame = self.expose(bin=2, roi={'x': 4, 'y': 2, 'width': 20, 'height': 16}, light=False)
        self.assertEqual((frame.width, frame.height), (10, 8))
        self.assertEqual((frame.metadata['roi_x'], frame.metadata['roi_y'], frame.metadata['bin']), (4, 2, 2))
        dark = self.expose(light=False)
        light = self.expose()
        dark_values = struct.unpack('<1200H', dark.data)
        self.assertGreater(len(set(dark_values)), 10)  # Gaussian read noise.
        self.assertGreater(max(dark_values), 30000)  # Stable hot pixels.
        self.assertGreater(sum(struct.unpack('<1200H', light.data)), sum(dark_values))
        high_gain = self.expose(gain=100)
        self.assertGreater(sum(struct.unpack('<1200H', high_gain.data)), sum(struct.unpack('<1200H', light.data)))

    def test_optional_bayer_descriptor_offsets_and_binning_limit(self):
        camera = SimulatedCamera(seed=7, width=40, height=30, bayer=True, clock=self.clock)
        camera.open()
        self.assertEqual((camera.info()['sensor_type'], camera.info()['max_bin']), (2, 1))
        frame = self.expose(camera, roi={'x': 1, 'y': 1, 'width': 20, 'height': 16})
        self.assertEqual((frame.metadata['bayer_offset_x'], frame.metadata['bayer_offset_y']), (1, 1))
        with self.assertRaisesRegex(CameraError, 'invalid_exposure'):
            camera.start_exposure(1, bin=2)

    def test_invalid_requests_preserve_completed_frame(self):
        self.camera.start_exposure(1)
        self.clock.now += 2
        for kwargs in ({'seconds': True}, {'seconds': float('inf')}, {'seconds': 10 ** 1000},
                       {'seconds': 0}, {'seconds': 3601}, {'bin': True}, {'bin': 5},
                       {'gain': -1}, {'gain': 101}, {'light': 1},
                       {'roi': {'x': 0, 'y': 0, 'width': 41, 'height': 30}},
                       {'bin': 2, 'roi': {'x': 1, 'y': 0, 'width': 20, 'height': 10}},
                       {'roi': {'x': 0, 'y': 0, 'width': 20, 'height': 30, 'raw': 1}}):
            values = {'seconds': 1, **kwargs}
            with self.subTest(keys=list(kwargs)), self.assertRaises(CameraError):
                self.camera.start_exposure(**values)
            self.assertEqual(self.camera.state(), 'download')
        self.camera.read_frame()

    def test_render_failure_has_payload_free_error_and_can_restart(self):
        self.camera.start_exposure(1)
        self.clock.now += 2
        with patch.object(self.camera, '_render', side_effect=RuntimeError('private upstream payload')):
            with self.assertRaises(CameraError) as raised:
                self.camera.read_frame()
        self.assertEqual(str(raised.exception), 'frame_failed')
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(self.camera.state(), 'error')
        self.expose()
        self.assertEqual(self.camera.state(), 'idle')

    def test_constructor_and_info_validate_bounds_without_device_access(self):
        for kwargs in ({'width': 0}, {'height': True}, {'width': 4096, 'height': 4096},
                       {'seed': -1}, {'seed': 1 << 32}, {'bayer': 1}, {'clock': None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CameraError):
                SimulatedCamera(**kwargs)
        info = self.camera.info()
        for patch_values in ({'raw': 1}, {'bits': 17}, {'max_adu': 65536}, {'pixel_um_x': float('nan')},
                             {'can_abort': 1}, {'bayer_offset': [2, 0]}, {'gain_max': -1}):
            with self.subTest(patch_values=patch_values), self.assertRaises(CameraError):
                validate_info({**info, **patch_values})
