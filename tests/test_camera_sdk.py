"""ZWO ASI SDK on the bridge: never in the image, loaded once installed and accepted, like every manufacturer."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from wifi_bridge.api import ApiError, MobileAPI
from wifi_bridge.cameras import drivers, zwo_platform
from wifi_bridge.cameras.driver import CameraError
from test_bridge import Fixture

LICENCE = b'Synthetic vendor licence\nPermission is granted to redistribute.\n'
LIBRARY = b'\x7fELF synthetic library'
CLOCK = {'clock_status': 'unverified', 'corrected_utc': '2026-11-18T10:00:00.000+00:00'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


@contextlib.contextmanager
def root_owned(base):
    """Model the root-owned appliance layout with the test user's files."""
    original = Path.lstat

    def fixture(path):
        fields = list(original(path))
        fields[4] = 0
        # Host folders above the fixture stand for the image's root-owned tree.
        if path in base.parents:
            fields[0] = stat.S_IFDIR | 0o755
        return os.stat_result(fields)
    with patch.object(Path, 'lstat', fixture):
        yield


class Layout:
    """An installed ZWO driver as the common installer leaves it (drivers.install_archive)."""
    def __init__(self, base, version='1.42', record=True):
        self.drivers = base / 'drivers'
        self.folder = self.drivers / 'zwo'
        self.acceptance = self.drivers / 'zwo-asi-license.json'
        self.folder.mkdir(parents=True)
        for folder in (base, self.drivers, self.folder):
            folder.chmod(0o755)
        self.version = version
        self.library = self.folder / f'libASICamera2.so.{version}'
        self.library.write_bytes(LIBRARY)
        (self.folder / 'platform.json').write_text(json.dumps({'path': str(self.library), 'sha256': sha(LIBRARY)}))
        (self.folder / 'driver.json').write_text(json.dumps({'vendor': 'zwo_asi', 'version': version, 'arch': 'arm64', 'catalog_version': '2026.11.20.2'}))
        if record:
            self.accept()

    def accept(self):
        drivers.write_private_json(self.acceptance, drivers.acceptance_record('zwo_asi', self.version, sha(LIBRARY), sha(LICENCE), CLOCK, 'setup'))


class InstalledSDKTests(unittest.TestCase):
    """No library comes with an image: ZWO is loaded only once installed, and only while accepted."""
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.addCleanup(zwo_platform._warned.clear)

    def test_nothing_is_loaded_before_installation(self):
        self.assertFalse(hasattr(zwo_platform, 'BUNDLED') or hasattr(zwo_platform, 'bundled') or hasattr(zwo_platform, 'accept'))
        (self.base / 'drivers').mkdir()
        with root_owned(self.base):
            with self.assertRaisesRegex(CameraError, 'camera_driver_unavailable'):
                zwo_platform.declared_path(self.base / 'drivers')

    def test_installed_library_is_used_only_while_its_acceptance_names_it(self):
        layout = Layout(self.base)
        with root_owned(self.base), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(zwo_platform.declared_path(layout.drivers), layout.library)
            # Deactivate removes the record: the library stays installed but is no longer served.
            drivers.withdraw(layout.acceptance)
            with self.assertRaises(CameraError):
                zwo_platform.declared_path(layout.drivers)
            layout.accept()
            self.assertEqual(zwo_platform.declared_path(layout.drivers), layout.library)
            # A record for another library does not count.
            drivers.write_private_json(layout.acceptance, drivers.acceptance_record('zwo_asi', '1.42', sha(b'other'), sha(LICENCE), CLOCK, 'web'))
            with self.assertRaises(CameraError):
                zwo_platform.declared_path(layout.drivers)

    def test_writable_or_tampered_installation_is_refused(self):
        layout = Layout(self.base)
        with root_owned(self.base), contextlib.redirect_stderr(io.StringIO()):
            layout.library.chmod(0o666)
            with self.assertRaises(CameraError):
                zwo_platform.declared_path(layout.drivers)
            layout.library.chmod(0o644)
            layout.library.write_bytes(LIBRARY + b'x')
            with self.assertRaises(CameraError):
                zwo_platform.declared_path(layout.drivers)
        layout.library.write_bytes(LIBRARY)
        # Real ownership: files of an ordinary user are never trusted.
        if os.geteuid() != 0:
            with self.assertRaises(CameraError):
                zwo_platform.declared_path(layout.drivers)

    def test_version_comes_from_the_file_name_and_untested_versions_warn_once(self):
        self.assertEqual(zwo_platform.library_version('libASICamera2.so.1.42'), '1.42')
        self.assertEqual(zwo_platform.library_version('libASICamera2.so.2.0.1'), '2.0.1')
        self.assertIsNone(zwo_platform.library_version('libASICamera2.so'))
        source = Path(zwo_platform.__file__).read_text()
        self.assertNotIn("'1.41'", source.replace("TESTED_VERSIONS = ('1.41',)", ''))
        layout = Layout(self.base, '9.9')
        stderr = io.StringIO()
        with root_owned(self.base), contextlib.redirect_stderr(stderr):
            zwo_platform.declared_path(layout.drivers)
            zwo_platform.declared_path(layout.drivers)
        self.assertEqual(stderr.getvalue().count('ZWO ASI SDK 9.9 has not been tested'), 1)

    def test_owner_script_installation_needs_no_record_and_the_newest_wins(self):
        layout = Layout(self.base, record=False)
        # The script leaves no driver.json: it required the licence itself.
        (layout.folder / 'driver.json').unlink()
        newer = layout.drivers / 'zwo-1.43'
        newer.mkdir(mode=0o700)
        library = newer / 'libASICamera2.so.1.43'
        library.write_bytes(b'owner installed')
        (newer / 'platform.json').write_text(json.dumps({'path': str(library), 'sha256': sha(b'owner installed')}))
        with root_owned(self.base), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(zwo_platform.declared_path(layout.drivers), library)
            self.assertEqual([item['version'] for item in zwo_platform.installations(layout.drivers)], ['1.42', '1.43'])


class ZwoThroughTheCommonInstallerTests(unittest.TestCase):
    """The real catalog build of ZWO (zip, nested tar.bz2, library and licence members) installs like any manufacturer."""
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.base.chmod(0o755)
        self.addCleanup(zwo_platform._warned.clear)

    def test_install_leaves_library_rule_and_record_and_the_loader_then_finds_it(self):
        import copy
        import tarfile
        import zipfile
        from wifi_bridge.cameras import catalog as driver_catalog
        value = copy.deepcopy(driver_catalog.catalog())
        entry = next(item for item in value['drivers'] if item['id'] == 'zwo_asi')
        build = next(item for item in entry['builds'] if item['arch'] == 'arm64')
        # A synthetic archive of the same shape as the vendor's: ZIP > nested tar.bz2 > library and licence.
        library = b'\x7fELF\x02\x01\x01' + bytes(11) + (183).to_bytes(2, 'little') + bytes(44) + b'synthetic ZWO library'
        member = build['files'][0]['member']
        nested = self.base / 'nested.tar.bz2'
        with tarfile.open(nested, 'w:bz2') as tar:
            for name, data in ((member, library), (build['license_member'], LICENCE)):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        archive = self.base / 'sdk.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.write(nested, build['nested'])
        build.update(size=archive.stat().st_size, sha256=sha(archive.read_bytes()))
        build['files'][0]['sha256'] = sha(library)
        build['license_sha256'] = sha(LICENCE)
        drivers_folder = self.base / 'camera-drivers'
        record = drivers.install_archive('zwo_asi', build, archive, CLOCK, 'setup', drivers_folder, value, workdir=self.base)
        self.assertEqual((record['origin'], record['version']), ('setup', '1.42'))
        folder = drivers_folder / 'zwo'
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         ['70-astro-bridge-zwo.rules', 'driver.json', 'libASICamera2.so.1.42', 'license.txt', 'platform.json'])
        rules = (folder / '70-astro-bridge-zwo.rules').read_text()
        self.assertIn('ATTR{idVendor}=="03c3"', rules)
        self.assertEqual(rules.count('MODE="0600"'), len(rules.splitlines()))
        self.assertNotIn('MODE="0666"', rules)
        with root_owned(self.base), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(zwo_platform.declared_path(drivers_folder), folder / 'libASICamera2.so.1.42')
            drivers.withdraw(drivers.acceptance_path('zwo_asi', drivers_folder))
            with self.assertRaises(CameraError):
                zwo_platform.declared_path(drivers_folder)


class NoSDKRouteTests(Fixture):
    """The licence of a library that came with the image is gone: ZWO has the routes of every manufacturer."""
    def setUp(self):
        super().setUp()
        self.api = MobileAPI(self.controller)
        self.facade = self.api.cameras

    def handle(self, method, path, data=None, revision=None, browser=True):
        return self.facade.handle(method, path, {}, data or {}, revision, browser=browser)

    def test_the_sdk_routes_are_gone_and_zwo_is_a_line_like_the_others(self):
        for method, path in (('GET', '/cameras/sdk'), ('POST', '/cameras/sdk/zwo_asi/accept'), ('POST', '/cameras/sdk/zwo_asi/withdraw')):
            with self.assertRaises(ApiError) as gone:
                self.handle(method, path, {})
            self.assertEqual(gone.exception.status, 404, path)
        rows = {row['id']: row for row in self.handle('GET', '/cameras/drivers')['drivers']}
        self.assertEqual(rows['zwo_asi']['state'], 'available')
        self.assertEqual(rows['zwo_asi']['version'], '1.42')
        with self.assertRaises(ApiError) as supplied:
            self.handle('POST', '/cameras/drivers/zwo_asi/accept', {'version': '1.42'}, '"' + self.api.revision() + '"')
        self.assertEqual(supplied.exception.status, 409)


class DriverChangeTests(unittest.TestCase):
    def test_waiting_cameras_are_retried_and_served_ones_released(self):
        from wifi_bridge.cameras.service import CameraService
        service = CameraService()
        self.addCleanup(service.close)
        released = Mock()
        service._usb = {'usb-a': {'driver': 'zwo_asi', 'discovery': {'state': 'driver_missing'}},
                        'usb-b': {'driver': 'zwo_asi', 'discovery': {'state': 'ready'}},
                        'usb-c': {'driver': 'uvc', 'discovery': {'state': 'driver_missing'}}}
        service._cameras = {'usb-b': Mock(release=released, driver=Mock())}
        with patch.object(service, '_retain_unreaped'):
            service.driver_changed('zwo_asi')
        self.assertEqual({k: v['discovery']['state'] for k, v in service._usb.items()},
                         {'usb-a': 'detected', 'usb-b': 'detected', 'usb-c': 'driver_missing'})
        released.assert_called_once()
        self.assertEqual(service._cameras, {})


if __name__ == '__main__':
    unittest.main()
