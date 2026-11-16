import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wifi_bridge.cameras.alpaca import AlpacaError
from wifi_bridge.cameras.registry import load_registry
from wifi_bridge.cameras.service import CameraService
from wifi_bridge.cameras.simulator import SimulatedCamera

PTP = dict(vendor_id=0x1234, product_id=0x5678, device_class=0, interface_classes=[6], speed_mbps=480, bus=1, port_path='1-2')
PROFILE = dict(name='Owner camera', width=640, height=480, pixel_um_x=4.2, pixel_um_y=4.2, bits=16, exposure_min=0.001, exposure_max=60, bayer_pattern='RGGB')

class FakeGPhoto(SimulatedCamera):
    def is_ready(self):
        return True

class GphotoIntegration(unittest.TestCase):
    def test_discovery_profile_alpaca_ambiguity_and_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key = '1-2:4660:22136'
            ident = 'usb-' + hashlib.sha256(key.encode()).hexdigest()[:24]
            (root / 'camera-gphoto.json').write_text(json.dumps({'schema_version': 1, 'cameras': {ident: PROFILE}}))
            registry = load_registry()
            with patch('wifi_bridge.cameras.gphoto.GPhotoCamera', side_effect=lambda **kw: FakeGPhoto()), \
                 patch('wifi_bridge.cameras.gphoto_platform.profile', return_value=PROFILE), \
                 patch('wifi_bridge.cameras.gphoto_platform.attachment_token', return_value=1), \
                 patch('wifi_bridge.cameras.gphoto_platform.probe', return_value={}):
                service = CameraService(path=root / 'cameras.json', gphoto_enabled=True)
                try:
                    service.reconcile_usb([PTP], registry)
                    for _ in range(100):
                        if service.inventory()[0]['discovery']['state'] == 'ready':
                            break
                        __import__('time').sleep(.01)
                    self.assertEqual(service.inventory()[0]['discovery']['state'], 'ready')
                    self.assertEqual(service.configured_devices()[0]['DeviceNumber'], 0)
                    service.execute(0, 'PUT', 'connected', {'connected': 'true'}, 1, '127.0.0.1')
                    self.assertTrue(service.execute(0, 'GET', 'connected', {}, 1, '127.0.0.1'))
                    self.assertFalse(service.execute(0, 'GET', 'canabortexposure', {}, 1, '127.0.0.1'))
                    with self.assertRaises(AlpacaError) as error:
                        service.execute(0, 'GET', 'gain', {}, 1, '127.0.0.1')
                    self.assertEqual(error.exception.number, 0x400)
                    service.reconcile_usb([PTP, dict(PTP, port_path='1-3')], registry)
                    self.assertEqual(service.configured_devices(), [])
                finally:
                    service.close()
            service = CameraService(path=root / 'other.json')
            try:
                service.reconcile_usb([PTP], registry)
                self.assertEqual(service.inventory(), [])
            finally:
                service.close()

    def test_missing_profile_does_not_create_driver(self):
        with tempfile.TemporaryDirectory() as temp:
            service = CameraService(path=Path(temp) / 'cameras.json', gphoto_enabled=True)
            try:
                service.reconcile_usb([PTP], load_registry())
                self.assertEqual(service.inventory()[0]['discovery']['state'], 'unsupported')
                self.assertEqual(service.configured_devices(), [])
            finally:
                service.close()
