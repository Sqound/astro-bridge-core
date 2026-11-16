import tempfile
import os
import subprocess
import sys
import threading
from types import SimpleNamespace
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from wifi_bridge.cameras.registry import load_registry
from wifi_bridge.cameras.service import CameraProfileChanged, CameraService
from tests.test_gphoto_integration import PTP, PROFILE


class Profiles(unittest.TestCase):
    def test_detect_manual_activation_and_stale_generation(self):
        with tempfile.TemporaryDirectory() as root, \
             patch('wifi_bridge.cameras.gphoto_platform.attachment_token', return_value=7) as token, \
             patch('wifi_bridge.cameras.gphoto_platform.probe', return_value={'name': 'USB camera', 'width': 640, 'height': 480}), \
             patch('wifi_bridge.cameras.gphoto_platform.save_profile') as save:
            service = CameraService(path=Path(root) / 'cameras.json', gphoto_enabled=True)
            try:
                service.reconcile_usb([PTP], load_registry())
                ident = service.inventory()[0]['id']
                for _ in range(100):
                    view = service.camera_profile(ident)
                    if not view['probing']:
                        break
                    time.sleep(.01)
                self.assertEqual(view['detected']['name'], 'USB camera')
                self.assertIn('bits', view['missing'])
                self.assertFalse(view['ready'])
                generation = view['attachment_generation']
                values = {k: v for k, v in PROFILE.items() if k not in ('name', 'width', 'height')}
                ready = service.camera_profile(ident, generation, values)
                self.assertTrue(ready['ready'])
                self.assertNotIn('name', save.call_args.args[2])
                self.assertEqual(ready['values']['name'], 'USB camera')
                with self.assertRaises(CameraProfileChanged):
                    service.camera_profile(ident, generation, {'name': 'stale'})
                token.return_value = 8
                with self.assertRaises(CameraProfileChanged):
                    service.camera_profile(ident, ready['attachment_generation'], {'name': 'changed'})
            finally:
                service.close()

    def test_disable_cancels_probe_and_requires_fresh_probe_on_reenable(self):
        entered, released = threading.Event(), threading.Event()
        calls = []
        def probe(device, cancel):
            calls.append(cancel)
            entered.set()
            if len(calls) == 1:
                cancel.wait(2)
                released.wait(2)
                return PROFILE
            return PROFILE
        with tempfile.TemporaryDirectory() as root, \
             patch('wifi_bridge.cameras.gphoto_platform.attachment_token', return_value=7), \
             patch('wifi_bridge.cameras.gphoto_platform.probe', side_effect=probe):
            service = CameraService(path=Path(root) / 'cameras.json', gphoto_enabled=True)
            try:
                registry = load_registry()
                service.reconcile_usb([PTP], registry)
                self.assertTrue(entered.wait(1))
                ident = service.inventory()[0]['id']
                original = service.camera_profile(ident)['attachment_generation']
                service.update_settings({'serve': False})
                self.assertTrue(calls[0].is_set())
                view = service.camera_profile(ident)
                self.assertFalse(view['ready'])
                self.assertNotEqual(original, view['attachment_generation'])
                with self.assertRaises(CameraProfileChanged):
                    service.camera_profile(ident, view['attachment_generation'], {'name': 'ignored'})
                service.update_settings({'serve': True})
                service.reconcile_usb([PTP], registry)
                self.assertEqual(len(calls), 1)
                self.assertFalse(service.camera_profile(ident)['ready'])
                released.set()
                for _ in range(100):
                    service.reconcile_usb([PTP], registry)
                    if service.camera_profile(ident)['ready']:
                        break
                    time.sleep(.01)
                self.assertEqual(len(calls), 2)
                self.assertTrue(service.camera_profile(ident)['ready'])
            finally:
                released.set()
                service.close()

    def test_failed_replacement_updates_count(self):
        from wifi_bridge.cameras.driver import CameraError
        with tempfile.TemporaryDirectory() as root, \
             patch('wifi_bridge.cameras.gphoto_platform.attachment_token', return_value=7), \
             patch('wifi_bridge.cameras.gphoto_platform.probe', return_value=PROFILE), \
             patch('wifi_bridge.cameras.gphoto_platform.save_profile'):
            service = CameraService(path=Path(root) / 'cameras.json', gphoto_enabled=True)
            try:
                service.reconcile_usb([PTP], load_registry())
                ident = service.inventory()[0]['id']
                for _ in range(100):
                    view = service.camera_profile(ident)
                    if view['ready']:
                        break
                    time.sleep(.01)
                self.assertTrue(service.enabled())
                with patch('wifi_bridge.cameras.gphoto.GPhotoCamera', side_effect=CameraError('camera_unsupported')):
                    view = service.camera_profile(ident, view['attachment_generation'], {'name': 'changed'})
                self.assertFalse(view['ready'])
                self.assertFalse(service.enabled())
            finally:
                service.close()

    def test_usb_metadata_change_invalidates_generation(self):
        with tempfile.TemporaryDirectory() as root, \
             patch('wifi_bridge.cameras.gphoto_platform.attachment_token', return_value=7), \
             patch('wifi_bridge.cameras.gphoto_platform.probe', return_value={}):
            service = CameraService(path=Path(root) / 'cameras.json', gphoto_enabled=True)
            try:
                registry = load_registry()
                service.reconcile_usb([PTP], registry)
                ident = service.inventory()[0]['id']
                old = service.camera_profile(ident)['attachment_generation']
                service.reconcile_usb([dict(PTP, speed_mbps=12)], registry)
                self.assertNotEqual(old, service.camera_profile(ident)['attachment_generation'])
            finally:
                service.close()

    def test_probe_only_connects_and_reads_reported_properties(self):
        from wifi_bridge.cameras.gphoto_platform import probe
        code = r"""
import sys
for line in sys.stdin:
    if line.startswith('<getProperties'):
        print('<defSwitchVector device="Camera" name="CONNECTION"><defSwitch name="CONNECT">Off</defSwitch><defSwitch name="DISCONNECT">On</defSwitch></defSwitchVector>', flush=True)
    elif 'name="CONNECTION"' in line:
        print('<setSwitchVector device="Camera" name="CONNECTION" state="Ok"><oneSwitch name="CONNECT">On</oneSwitch></setSwitchVector>', flush=True)
        print('<defNumberVector device="Camera" name="CCD_INFO" state="Ok"><defNumber name="CCD_MAX_X">640</defNumber><defNumber name="CCD_MAX_Y">480</defNumber><defNumber name="CCD_PIXEL_SIZE_X">4.2</defNumber><defNumber name="CCD_PIXEL_SIZE_Y">4.2</defNumber><defNumber name="CCD_BITSPERPIXEL">0</defNumber></defNumberVector>', flush=True)
        print('<defNumberVector device="Camera" name="CCD_EXPOSURE"><defNumber name="CCD_EXPOSURE_VALUE" min="0.001" max="60">1</defNumber></defNumberVector>', flush=True)
        break
    else:
        sys.exit(2)
"""
        with patch('wifi_bridge.cameras.gphoto_platform.launch', side_effect=lambda device: subprocess.Popen(
                [sys.executable, '-u', '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE)):
            values = probe(PTP, threading.Event())
        self.assertEqual(values['width'], 640)
        self.assertEqual(values['pixel_um_x'], 4.2)
        self.assertEqual(values['exposure_min'], .001)
        self.assertNotIn('bits', values)
        self.assertNotIn('bayer_pattern', values)

    def test_atomic_persistence_preserves_profiles_and_partials(self):
        from wifi_bridge.cameras.gphoto_platform import profile, save_profile
        original_fstat = os.fstat
        def trusted(fd):
            info = original_fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_gid=0)
        with tempfile.TemporaryDirectory() as root, patch('wifi_bridge.cameras.gphoto_platform.os.fstat', side_effect=trusted):
            path = Path(root) / 'camera-gphoto.json'
            first, second = 'usb-' + '1' * 24, 'usb-' + '2' * 24
            save_profile(path, first, PROFILE)
            save_profile(path, second, {'name': 'partial', 'bits': 16})
            self.assertEqual(profile(path, first), PROFILE)
            self.assertEqual(profile(path, second), {'name': 'partial', 'bits': 16})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_same_port_new_device_number_replaces_generation(self):
        with tempfile.TemporaryDirectory() as root, \
             patch('wifi_bridge.cameras.gphoto_platform.attachment_token', return_value=7) as token, \
             patch('wifi_bridge.cameras.gphoto_platform.probe', return_value={}):
            service = CameraService(path=Path(root) / 'cameras.json', gphoto_enabled=True)
            try:
                registry = load_registry()
                service.reconcile_usb([PTP], registry)
                ident = service.inventory()[0]['id']
                old = service.camera_profile(ident)['attachment_generation']
                token.return_value = 8
                service.reconcile_usb([PTP], registry)
                new = service.camera_profile(ident)['attachment_generation']
                self.assertNotEqual(old, new)
                with self.assertRaises(CameraProfileChanged):
                    service.camera_profile(ident, old, {'name': 'stale'})
            finally:
                service.close()

    def test_ambiguity_removes_profile(self):
        with tempfile.TemporaryDirectory() as root, \
             patch('wifi_bridge.cameras.gphoto_platform.attachment_token', return_value=7), \
             patch('wifi_bridge.cameras.gphoto_platform.probe', return_value={}):
            service = CameraService(path=Path(root) / 'cameras.json', gphoto_enabled=True)
            try:
                registry = load_registry()
                service.reconcile_usb([PTP], registry)
                ident = service.inventory()[0]['id']
                service.reconcile_usb([PTP, dict(PTP, port_path='1-3')], registry)
                with self.assertRaises(KeyError):
                    service.camera_profile(ident)
            finally:
                service.close()
