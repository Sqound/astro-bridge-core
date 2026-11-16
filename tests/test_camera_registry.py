"""Camera tables are bounded data, never executable drivers or device discovery."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wifi_bridge.cameras.driver import CameraError
from wifi_bridge.cameras.registry import (CameraRegistry, MAX_MANIFEST_BYTES, load_registry,
                                          manifest_schema, validate_manifest, validate_registry)
from wifi_bridge.cameras.simulator import SimulatedCamera

ROOT = Path(__file__).resolve().parents[1]
BASE = {'schema_version': 1, 'id': 'uvc', 'driver': 'uvc', 'usb': [{'class': 14}], 'validation': 'unverified'}


class CameraRegistryTests(unittest.TestCase):
    def test_source_schema_is_reproducible_and_factories_are_explicit(self):
        self.assertEqual(json.loads((ROOT / 'cameras/schema/manifest.schema.json').read_text()), manifest_schema())
        registry = load_registry()
        self.assertEqual(registry.descriptors(), load_registry(ROOT / 'cameras').descriptors())
        self.assertEqual({d['id'] for d in registry.descriptors()}, {'simulator', 'uvc', 'zwo_asi'})
        self.assertTrue(registry.available('simulator'))
        self.assertTrue(registry.available('uvc'))
        camera = registry.create('simulator', width=20, height=10)
        self.assertIsInstance(camera, SimulatedCamera)
        with self.assertRaisesRegex(CameraError, 'not_open'):
            camera.start_exposure(1)
        from wifi_bridge.cameras.uvc import UVCCamera
        device = dict(vendor_id=1, product_id=2, device_class=14, interface_classes=[],
                      speed_mbps=480, bus=1, port_path='1-1')
        with patch('wifi_bridge.cameras.v4l2.LinuxVideoIO.nodes', side_effect=AssertionError('no host probe')):
            self.assertIsInstance(registry.create('uvc', device=device), UVCCamera)

    def test_exact_usb_rule_outranks_class_fallback_without_opening_device(self):
        exact = {**BASE, 'id': 'specific', 'usb': [{'vendor_id': 0x1234, 'product_id': 0x5678}]}
        factory = unittest.mock.Mock()
        for values in ([BASE, exact], [exact, BASE]):
            registry = CameraRegistry(values, {'uvc': factory})
            self.assertEqual(registry.match_usb(0x1234, 0x5678, 14)['id'], 'specific')
            self.assertEqual(registry.match_usb(0x1234, 0, 14)['id'], 'uvc')
            self.assertIsNone(registry.match_usb(0x1234, 0, 0))
        factory.assert_not_called()
        registry.create('specific', device='synthetic-device')
        factory.assert_called_once_with(device='synthetic-device')

    def test_descriptors_and_input_are_detached_from_internal_registry(self):
        rows = [copy.deepcopy(BASE)]
        registry = CameraRegistry(rows)
        rows[0]['usb'].clear()
        descriptors = registry.descriptors()
        descriptors[0]['usb'].clear()
        match = registry.match_usb(1, 1, 14)
        match['usb'].clear()
        self.assertEqual(registry.match_usb(1, 1, 14)['usb'], [{'class': 14}])

    def test_rejects_executable_unknown_unbounded_or_ambiguous_rules(self):
        changes = [{'exec': 'forbidden'}, {'schema_version': True}, {'schema_version': 2},
                   {'id': '../driver'}, {'driver': 'python:other'}, {'driver': []},
                   {'validation': 'partial_hardware'}, {'usb': [None]}, {'usb': [{'class': True}]},
                   {'usb': [{'class': 256}]}, {'usb': [{'class': 0}]}, {'usb': [{'class': 14}] * 33},
                   {'usb': [{'vendor_id': 1}]}, {'usb': [{'vendor_id': -1, 'product_id': 1}]},
                   {'usb': [{'vendor_id': 1, 'product_id': 65536}]},
                   {'usb': [{'vendor_id': 1, 'product_id': 2, 'class': 14}]},
                   {'driver': 'simulator'}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(CameraError):
                validate_manifest({**BASE, **change})
        for rows in ([], [BASE] * 33, [BASE, BASE],
                     [BASE, {**BASE, 'id': 'other'}], [{**BASE, 'usb': [{'class': 14}, {'class': 14}]}]):
            with self.subTest(count=len(rows)), self.assertRaises(CameraError):
                validate_registry(rows)
        for factories in ({'uvc': 'not callable'}, {'arbitrary': lambda: None}):
            with self.assertRaises(CameraError):
                CameraRegistry([BASE], factories)

    def test_usb_input_and_unknown_ids_are_validated(self):
        registry = CameraRegistry([BASE])
        for args in ((True, 1, 14), (-1, 1, 14), (1, 65536, 14), (1, 1, '14')):
            with self.assertRaises(CameraError):
                registry.match_usb(*args)
        for ident in ('missing', None, []):
            with self.assertRaises(CameraError):
                registry.create(ident)

    def test_loader_rejects_oversized_malformed_duplicate_and_recursive_json(self):
        invalid = [b'x' * (MAX_MANIFEST_BYTES + 1), b'{"schema_version":1,"schema_version":1}',
                   b'{', b'\xff', b'[' * 1100 + b']' * 1100]
        for data in invalid:
            with self.subTest(size=len(data)), tempfile.TemporaryDirectory() as folder:
                (Path(folder) / 'bad.json').write_bytes(data)
                with self.assertRaises(CameraError):
                    load_registry(folder)

    def test_loader_rejects_symlinks_and_too_many_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'source').write_text(json.dumps(BASE))
            (path / 'link.json').symlink_to(path / 'source')
            with self.assertRaisesRegex(CameraError, 'invalid_camera_manifest_file'):
                load_registry(path)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            for index in range(33):
                (path / (str(index) + '.json')).write_text(json.dumps(BASE))
            with self.assertRaisesRegex(CameraError, 'invalid_camera_registry'):
                load_registry(path)

    def test_camera_creation_never_invokes_usb_or_os_commands(self):
        with patch('subprocess.run', side_effect=AssertionError('no OS commands')), \
             patch('socket.socket', side_effect=AssertionError('no network')):
            registry = load_registry()
            registry.match_usb(1, 2, 14)
            camera = registry.create('simulator', width=2, height=2)
            camera.open()
            camera.close()
