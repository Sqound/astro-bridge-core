"""The release installer verifies downloads before touching an installation prefix."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


class InstallerTests(unittest.TestCase):
    def test_download_mismatch_never_creates_environment(self):
        installer = module('installer', ROOT/'scripts/install.py')
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)/'venv'
            installer.RELEASE_SHA256 = '0'*64
            with patch.object(installer, 'wheel_data', return_value=b'changed payload'), patch.object(sys, 'argv', ['install.py','--prefix',str(prefix)]):
                with self.assertRaises(SystemExit): installer.main()
            self.assertFalse(prefix.exists())

    def test_existing_installation_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); marker = root/'keep.txt'; marker.write_text('existing installation')
            result = subprocess.run([sys.executable, str(ROOT/'scripts/install.py'), '--prefix',str(root)],capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(marker.read_text(), 'existing installation')

    def test_rendered_installer_embeds_wheel_hash_and_installs_outside_checkout(self):
        builder = module('builder', ROOT/'tools/build-release.py')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); artifacts = builder.package(destination=root/'release')
            script = root/'release/install.py'
            self.assertIn('install.py', artifacts)
            self.assertIn((ROOT/'LICENSE').read_text().splitlines()[2], script.read_text())
            installer = module('release_installer', script)
            wheel = root/'release'/f'astro_bridge_core-{installer.RELEASE_VERSION}-py3-none-any.whl'
            self.assertEqual(installer.RELEASE_SHA256, hashlib.sha256(wheel.read_bytes()).hexdigest())
            prefix = root/'isolated'
            with patch.object(installer, 'wheel_data', return_value=wheel.read_bytes()), patch.object(sys, 'argv', ['install.py','--prefix',str(prefix)]):
                installer.main()
            installed = subprocess.check_output([str(prefix/'bin/python'),'-c','import wifi_bridge; print(wifi_bridge.__file__)'],cwd=root,text=True)
            self.assertIn(str(prefix), installed)
            self.assertNotIn(str(ROOT), installed)
