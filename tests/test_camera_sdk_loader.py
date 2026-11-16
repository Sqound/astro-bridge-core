"""Synthetic platform declarations only; never execute an SDK or open USB."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from wifi_bridge.cameras import loader
from wifi_bridge.cameras.driver import CameraError


class PlatformSDKTests(unittest.TestCase):
    def test_exact_platform_file_uses_direct_eager_local_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / 'synthetic.so'
            library.write_text('fixture')
            with mock.patch.object(loader.sys, 'platform', 'linux'), \
                    mock.patch.object(loader.ctypes, 'CDLL') as cdll:
                self.assertIs(loader.load_platform_sdk(library), cdll.return_value)
                cdll.assert_called_once_with(str(library), mode=os.RTLD_NOW | os.RTLD_LOCAL)
                cdll.return_value.assert_not_called()

    def test_undeclared_missing_or_ambiguous_path_never_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(loader.sys, 'platform', 'linux'), \
                    mock.patch.object(loader.ctypes, 'CDLL') as cdll:
                for path in (None, '', 'relative.so', directory, Path(directory) / 'absent.so',
                             directory + '/../bad.so', '/lib/bad\x00name', '/' + 'x' * 4097):
                    with self.subTest(path=str(path)[:64]), self.assertRaises(CameraError) as err:
                        loader.load_platform_sdk(path)
                    self.assertEqual(err.exception.code, 'camera_driver_unavailable')
                cdll.assert_not_called()

    def test_no_search_retry_or_raw_diagnostics_on_load_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / 'synthetic.so'
            library.touch()
            with mock.patch.object(loader.sys, 'platform', 'linux'), \
                    mock.patch.object(loader.ctypes, 'CDLL', side_effect=OSError('private path')) as cdll:
                with self.assertRaises(CameraError) as err:
                    loader.load_platform_sdk(library)
                self.assertEqual(err.exception.code, 'camera_driver_unavailable')
                self.assertNotIn('private path', str(err.exception))
                cdll.assert_called_once()
            with mock.patch.object(loader.sys, 'platform', 'other'), \
                    mock.patch.object(loader.ctypes, 'CDLL') as cdll:
                with self.assertRaises(CameraError):
                    loader.load_platform_sdk(library)
                cdll.assert_not_called()
