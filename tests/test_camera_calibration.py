"""Owner metadata in temporary files, and fake camera I/O only."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_camera_uvc import DEVICE, FakeVideo
from wifi_bridge.cameras.calibration import CameraCalibration, MAX_BYTES, validate
from wifi_bridge.cameras.driver import CameraError
from wifi_bridge.cameras.registry import CameraRegistry, load_registry
from wifi_bridge.cameras.service import CameraService
from wifi_bridge.cameras.uvc import UVCCamera

IDENT = 'usb-' + '1' * 24


class CalibrationTests(unittest.TestCase):
    def test_no_construction_read_and_missing_file_returns_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'calibration.json'
            with patch('wifi_bridge.cameras.calibration.os.open', side_effect=AssertionError('inert')):
                calibration = CameraCalibration(path)
            self.assertIsNone(calibration.pitch(IDENT))

    def test_strict_schema_bounds_duplicates_nonfinite_and_unknown_values(self):
        good = {'schema_version': 1, 'cameras': {IDENT: {'pixel_um': [3.75, 3.5]}}}
        self.assertEqual(validate(good), {IDENT: (3.75, 3.5)})
        invalid = [dict(good, schema_version=True), dict(good, extra=True),
                   dict(good, cameras={str(i): {'pixel_um': [1, 1]} for i in range(33)}),
                   dict(good, cameras={'../node': {'pixel_um': [1, 1]}}),
                   dict(good, cameras={IDENT: {'pixel_um': [1, 1], 'raw': 'forbidden'}})]
        for pitch in ([0, 1], [True, 1], [1], [1, float('inf')], [1, float('nan')], [1, 1001], '1'):
            invalid.append(dict(good, cameras={IDENT: {'pixel_um': pitch}}))
        for value in invalid:
            with self.assertRaises(CameraError):
                validate(value)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'calibration.json'
            calibration = CameraCalibration(path)
            for raw in (b'x' * (MAX_BYTES + 1), b'{"schema_version":1,"schema_version":1,"cameras":{}}',
                        b'\xff', b'[' * 1100 + b']' * 1100, b'{}'):
                path.write_bytes(raw)
                with self.assertRaisesRegex(CameraError, 'camera_unsupported'):
                    calibration.pitch(IDENT)
            path.write_text(json.dumps(good))
            self.assertEqual(calibration.pitch(IDENT), (3.75, 3.5))
            self.assertIsNone(calibration.pitch('usb-' + '2' * 24))

    def test_symlink_fifo_and_directory_are_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            regular = root / 'file'
            regular.write_text('{}')
            link = root / 'link'
            link.symlink_to(regular)
            fifo = root / 'fifo'
            os.mkfifo(fifo)
            for path in (link, fifo, root):
                with self.assertRaises(CameraError):
                    CameraCalibration(path).pitch(IDENT)

    def test_owner_metadata_reaches_factory_after_replug_and_alpaca_has_real_pitch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            io = FakeVideo()
            options = []
            def factory(**kwargs):
                options.append(kwargs)
                return UVCCamera(**kwargs, io=io, clock=lambda: io.now)
            registry = CameraRegistry(list(load_registry().descriptors()), {'uvc': factory})
            service = CameraService(path=root / 'cameras.json')
            try:
                service.reconcile_usb([DEVICE], registry)
                before = service.inventory()[0]
                self.assertEqual(before['discovery']['state'], 'unsupported')
                self.assertIsNone(before['alpaca'])
                self.assertFalse(io.calls)
                document = {'schema_version': 1, 'cameras': {before['id']: {'pixel_um': [3.75, 3.5]}}}
                (root / 'camera-calibration.json').write_text(json.dumps(document))
                service.reconcile_usb([DEVICE], registry)
                self.assertEqual(service.inventory()[0]['discovery']['state'], 'unsupported')
                service.reconcile_usb([], registry)
                service.reconcile_usb([DEVICE], registry)
                self.assertEqual(options[-1]['pixel_um'], (3.75, 3.5))
                self.assertEqual(service.inventory()[0]['discovery']['state'], 'ready')
                self.assertIsNone(service.inventory()[0]['alpaca'])  # No listening endpoint invented.
                service.execute(0, 'PUT', 'connected', {'connected': 'true'}, 1, '127.0.0.1')
                for prop, expected in (('pixelsizex', 3.75), ('pixelsizey', 3.5)):
                    self.assertEqual(service.execute(0, 'GET', prop, {}, 1, '127.0.0.1'), expected)
                service.reconcile_usb([], registry)
                self.assertFalse(io.fd_open)
                (root / 'camera-calibration.json').write_text('{}')
                service.reconcile_usb([DEVICE], registry)
                self.assertEqual(service.inventory()[0]['discovery']['state'], 'unsupported')
            finally:
                service.close()
