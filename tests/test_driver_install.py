"""Camera drivers installed from the bridge, and what the first boot did with Setup's drivers.

Doubles only: a fake HTTPS connection (no socket is opened), synthetic archives
with a synthetic ELF header, temporary folders made to look root-owned.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from wifi_bridge.api import ApiError, MobileAPI
from wifi_bridge.cameras import catalog as driver_catalog
from wifi_bridge.cameras import drivers, vendor_process
from wifi_bridge.cameras.driver_download import DownloadError, fetch
from test_bridge import Fixture
from test_camera_drivers import CLOCK, InstallationTests, elf, sha
from test_camera_sdk import root_owned

BODY = b'synthetic archive bytes ' * 4096


def build(**changes):
    value = {'version': '1.0', 'arch': 'arm64', 'source': 'download', 'url': 'https://downloads.example.com/sdk?id=1',
             'redirect_hosts': ['files.example.com'], 'size': len(BODY), 'sha256': sha(BODY), 'nested': None,
             'files': [], 'license_member': None, 'license_sha256': None}
    value.update(changes)
    return value


class Response:
    def __init__(self, status=200, body=b'', headers=None, fail_after=None):
        self.status, self.stream, self.headers, self.fail_after, self.sent = status, io.BytesIO(body), headers or {}, fail_after, 0

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, size):
        if self.fail_after is not None and self.sent >= self.fail_after:
            raise ConnectionResetError()
        chunk = self.stream.read(size)
        self.sent += len(chunk)
        return chunk


class Server:
    """Fake HTTPSConnection factory: {(host, path): Response or exception}."""
    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def __call__(self, host, timeout=None, context=None):
        server = self

        class Connection:
            def request(self, method, path, headers=None):
                server.requests.append((host, method, path, dict(headers or {})))
                answer = server.routes.get((host, path))
                if isinstance(answer, BaseException):
                    raise answer
                self.answer = answer

            def getresponse(self):
                return self.answer

            def close(self):
                pass
        return Connection()


class FetchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.target = Path(folder.name) / '.download-testcam'

    def fetch(self, routes, value=None, **options):
        server = Server(routes)
        result = fetch(value or build(), self.target, factory=server, **options)
        return result, server

    def assertCode(self, code, routes, value=None):
        with self.assertRaises(DownloadError) as caught:
            self.fetch(routes, value)
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(self.target.exists())

    def test_verified_download_through_a_listed_redirect(self):
        seen = []
        size, server = self.fetch({('downloads.example.com', '/sdk?id=1'): Response(302, headers={'Location': 'https://files.example.com/sdk-1.0.tar.gz'}),
                                   ('files.example.com', '/sdk-1.0.tar.gz'): Response(200, BODY, {'Content-Length': str(len(BODY))})},
                                  progress=lambda received, total: seen.append((received, total)))
        self.assertEqual(size, len(BODY))
        self.assertEqual(self.target.read_bytes(), BODY)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(seen[-1], (len(BODY), len(BODY)))
        self.assertEqual([r[:3] for r in server.requests], [('downloads.example.com', 'GET', '/sdk?id=1'), ('files.example.com', 'GET', '/sdk-1.0.tar.gz')])
        # No credential, no cookie, nothing about the owner in the request.
        self.assertEqual(set(server.requests[0][3]), {'User-Agent', 'Accept-Encoding'})

    def test_redirects_outside_the_catalog_and_plain_http_are_refused(self):
        for location in ('https://evil.example.net/x', 'http://files.example.com/x', 'https://files.example.com:8443/x',
                         'https://user:pw@files.example.com/x'):
            with self.subTest(location=location):
                self.assertCode('redirect_refused', {('downloads.example.com', '/sdk?id=1'): Response(302, headers={'Location': location})})
        loop = {('downloads.example.com', '/sdk?id=1'): Response(302, headers={'Location': '/sdk?id=1'})}
        server = Server(loop)
        server.routes = type('Always', (dict,), {'get': lambda self, key: Response(302, headers={'Location': '/sdk?id=1'})})()
        with self.assertRaises(DownloadError) as caught:
            fetch(build(), self.target, factory=server)
        self.assertEqual(caught.exception.code, 'redirect_refused')

    def test_offline_certificate_and_server_errors(self):
        route = ('downloads.example.com', '/sdk?id=1')
        for error in (socket.gaierror(8, 'nodename nor servname provided'), ConnectionRefusedError(), TimeoutError(), OSError(51, 'Network is unreachable')):
            with self.subTest(error=type(error).__name__):
                self.assertCode('offline', {route: error})
        self.assertCode('certificate', {route: ssl.SSLCertVerificationError('certificate is not yet valid')})
        self.assertCode('download_failed', {route: Response(404)})
        self.assertCode('download_failed', {route: Response(302)})

    def test_changed_short_long_and_interrupted_files(self):
        route = ('downloads.example.com', '/sdk?id=1')
        self.assertCode('file_changed', {route: Response(200, BODY[:-1] + b'X')})
        self.assertCode('file_changed', {route: Response(200, BODY, {'Content-Length': str(len(BODY) + 1)})})
        self.assertCode('file_changed', {route: Response(200, BODY + b'more')})
        self.assertCode('download_interrupted', {route: Response(200, BODY[:1000])})
        self.assertCode('download_interrupted', {route: Response(200, BODY, fail_after=4096)})

    def test_cancelled_and_existing_target(self):
        route = ('downloads.example.com', '/sdk?id=1')
        with self.assertRaises(DownloadError) as caught:
            fetch(build(), self.target, factory=Server({route: Response(200, BODY)}), cancelled=lambda: True)
        self.assertEqual(caught.exception.code, 'cancelled')
        self.target.write_bytes(b'planted')
        with self.assertRaises(FileExistsError):
            fetch(build(), self.target, factory=Server({route: Response(200, BODY)}))

    def test_only_verified_tls_and_complete_builds(self):
        insecure = ssl.create_default_context(); insecure.check_hostname = False; insecure.verify_mode = ssl.CERT_NONE
        with self.assertRaises(ValueError):
            fetch(build(), self.target, factory=Server({}), context=insecure)
        for change in ({'sha256': None}, {'size': None}, {'source': 'bundled'}, {'url': None}):
            with self.subTest(change=change), self.assertRaises(DownloadError):
                fetch(build(**change), self.target, factory=Server({}))


class SetupResultsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.base = Path(folder.name); self.base.chmod(0o755)
        self.drivers = self.base / 'camera-drivers'; self.drivers.mkdir(mode=0o700)

    def write(self, value):
        path = self.drivers / drivers.SETUP_RESULTS
        path.write_text(value if isinstance(value, str) else json.dumps(value)); path.chmod(0o600)

    def test_codes_are_read_and_anything_odd_is_dropped(self):
        self.write({'player_one': 'installed', 'zwo_asi': 'accepted', 'atik': 'archive_changed', '*': 'invalid_drop',
                    'Bad Key': 'installed', 'qhyccd': 'x' * 80, 'moravian': 3})
        with root_owned(self.base):
            value = drivers.setup_results(self.drivers)
        self.assertEqual(value, {'player_one': 'installed', 'zwo_asi': 'accepted', 'atik': 'archive_changed', '*': 'invalid_drop'})

    def test_absent_untrusted_or_malformed_says_nothing(self):
        self.assertEqual(drivers.setup_results(self.drivers), {})
        self.write({'player_one': 'installed'})
        if os.geteuid() == 0:  # A test run as root (a container) must still see a file it does not own.
            os.chown(self.drivers / drivers.SETUP_RESULTS, 65534, 65534)
        self.assertEqual(drivers.setup_results(self.drivers), {})  # Not root-owned: never trusted.
        for text in ('[]', '{', '"x"', json.dumps({str(n): 'installed' for n in range(80)})):
            with self.subTest(text=text[:10]):
                self.write(text)
                with root_owned(self.base):
                    self.assertEqual(drivers.setup_results(self.drivers), {})


class InstallFromBridgeTests(Fixture):
    """POST /cameras/drivers/<id>/install, on a fixture bridge with an installable catalog."""
    def setUp(self):
        super().setUp()
        self.api = MobileAPI(self.controller)
        self.controller.demo = False
        helper = InstallationTests('test_listed_files_are_checked_installed_and_accepted'); helper.setUp(); self.addCleanup(helper.doCleanups)
        self.archive = helper.archive()
        self.catalog, self.build = helper.catalog(self.archive)
        self.drivers = helper.drivers
        patches = [patch.object(driver_catalog, '_cached', self.catalog), patch.object(drivers, 'DRIVERS', self.drivers),
                   patch('wifi_bridge.cameras.catalog.host_arch', return_value='arm64'),
                   # The synthetic manufacturer has an adapter: the Cameras page offers only those.
                   patch.dict(vendor_process.ADAPTERS, {'testcam': lambda device: None})]
        for item in patches:
            item.start(); self.addCleanup(item.stop)
        self.base = helper.base

    def handle(self, method, path, data=None, browser=True):
        revision = None
        if method != 'GET':
            revision = '"' + self.api.revision() + '"'
        return self.api.cameras.handle(method, path, {}, data or {}, revision, browser=browser)

    def row(self, value, ident='testcam'):
        return next(r for r in value['drivers'] if r['id'] == ident)

    def wait(self, ident='testcam'):
        for _ in range(200):
            job = self.controller.driver_jobs.get(ident)
            if job and job['state'] in ('done', 'failed'):
                return job
            time.sleep(0.02)
        self.fail('job did not end')

    def install(self, fake_fetch):
        with patch('wifi_bridge.cameras.driver_download.fetch', side_effect=fake_fetch):
            value = self.handle('POST', '/cameras/drivers/testcam/install', {'version': '1.0', 'accept_terms': True})
            self.assertIn(self.row(value)['install']['state'], ('downloading', 'installing', 'done', 'failed'))
            return self.wait()

    def test_install_downloads_checks_installs_and_records_the_owner_acceptance(self):
        def fake_fetch(value, target, progress=None, **_):
            self.assertEqual(value, self.build)
            Path(target).write_bytes(self.archive.read_bytes()); progress(len(BODY), len(BODY))
            return value['size']
        job = self.install(fake_fetch)
        self.assertEqual((job['state'], job['error']), ('done', None))
        record = json.loads(drivers.acceptance_path('testcam', self.drivers).read_text())
        self.assertEqual((record['origin'], record['terms_url'], record['version']), ('web', 'https://www.example.com/terms', '1.0'))
        self.assertFalse([p for p in self.drivers.iterdir() if p.name.startswith('.download')])
        with root_owned(self.base):
            row = self.row(self.handle('GET', '/cameras/drivers'))
        self.assertEqual((row['state'], row['origin'], row['install']['state']), ('accepted', 'web', 'done'))

    def test_failures_are_plain_codes_and_install_nothing(self):
        for code in ('offline', 'certificate', 'file_changed', 'download_interrupted'):
            with self.subTest(code=code):
                def fake_fetch(value, target, code=code, **_):
                    raise DownloadError(code)
                job = self.install(fake_fetch)
                self.assertEqual((job['state'], job['error']), ('failed', code))
                self.assertFalse((self.drivers / 'testcam').exists())
        def wrong_bytes(value, target, **_):
            Path(target).write_bytes(b'not the archive')
        self.assertEqual(self.install(wrong_bytes)['error'], 'archive_changed')
        self.assertFalse(drivers.acceptance_path('testcam', self.drivers).exists())

    def test_requests_that_are_refused(self):
        cases = [({}, 'CAMERA_DRIVER_CHANGED'), ({'version': '0.9', 'accept_terms': True}, 'CAMERA_DRIVER_CHANGED'),
                 ({'version': '1.0', 'accept_terms': False}, 'CAMERA_DRIVER_CHANGED')]
        for data, code in cases:
            with self.subTest(data=data), self.assertRaises(ApiError) as caught:
                self.handle('POST', '/cameras/drivers/testcam/install', data)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(ApiError) as extra:
            self.handle('POST', '/cameras/drivers/testcam/install', {'version': '1.0', 'accept_terms': True, 'url': 'https://x.example.com'})
        self.assertEqual(extra.exception.status, 400)
        with self.assertRaises(ApiError) as native:
            self.handle('POST', '/cameras/drivers/testcam/install', {'version': '1.0', 'accept_terms': True}, browser=False)
        self.assertEqual(native.exception.status, 404)
        self.controller.camera_supported = False
        with self.assertRaises(ApiError) as zero:
            self.handle('POST', '/cameras/drivers/testcam/install', {'version': '1.0', 'accept_terms': True})
        self.assertEqual(zero.exception.code, 'CAMERA_UNSUPPORTED')

    def test_one_installation_at_a_time(self):
        gate = threading.Event()
        def slow(value, target, **_):
            gate.wait(5); raise DownloadError('offline')
        with patch('wifi_bridge.cameras.driver_download.fetch', side_effect=slow):
            self.handle('POST', '/cameras/drivers/testcam/install', {'version': '1.0', 'accept_terms': True})
            with self.assertRaises(ApiError) as busy:
                self.handle('POST', '/cameras/drivers/testcam/install', {'version': '1.0', 'accept_terms': True})
            self.assertEqual(busy.exception.code, 'DRIVER_INSTALL_RUNNING')
            gate.set(); self.wait()

    def test_setup_outcomes_reach_each_row_and_the_others(self):
        path = self.drivers / drivers.SETUP_RESULTS
        self.drivers.mkdir(mode=0o700, exist_ok=True)
        path.write_text(json.dumps({'testcam': 'archive_changed', 'touptek': 'unknown_driver', '*': 'clear_failed'})); path.chmod(0o600)
        with root_owned(self.base):
            value = self.handle('GET', '/cameras/drivers')
        self.assertEqual(self.row(value)['setup_result'], 'archive_changed')
        self.assertIsNone(self.row(value, 'zwo_asi')['setup_result'])
        self.assertEqual(value['setup_other'], {'touptek': 'unknown_driver', '*': 'clear_failed'})


    def installed(self):
        def fake_fetch(value, target, progress=None, **_):
            Path(target).write_bytes(self.archive.read_bytes()); progress(len(BODY), len(BODY))
            return value['size']
        self.assertEqual(self.install(fake_fetch)['state'], 'done')

    def rooted(self, *args, **kwargs):
        with root_owned(self.base):
            return self.handle(*args, **kwargs)

    def state(self, ident='testcam'):
        return self.row(self.rooted('GET', '/cameras/drivers'), ident)

    def test_deactivate_keeps_the_files_and_reactivate_serves_them_again_without_downloading(self):
        from wifi_bridge.cameras import vendor_platform
        from wifi_bridge.cameras.driver import CameraError
        self.installed()
        service = self.api.cameras.service()
        changed = patch.object(service, 'driver_changed'); service_changed = changed.start(); self.addCleanup(changed.stop)
        folder = self.drivers / 'testcam'
        before = sorted(p.name for p in folder.iterdir())
        with root_owned(self.base):
            self.assertEqual(vendor_platform.resolve('testcam', self.drivers)['version'], '1.0')
        value = self.rooted('POST', '/cameras/drivers/testcam/withdraw')
        row = self.row(value)
        # Only the acceptance goes. The files, the licence and the udev rule stay on the card.
        self.assertEqual((row['state'], row['version'], row['origin'], row['accepted_utc']), ('deactivated', '1.0', None, None))
        self.assertFalse(drivers.acceptance_path('testcam', self.drivers).exists())
        self.assertEqual(sorted(p.name for p in folder.iterdir()), before)
        service_changed.assert_called_with('testcam')
        with root_owned(self.base), self.assertRaises(CameraError):
            vendor_platform.resolve('testcam', self.drivers)
        # The owner accepts again: nothing is fetched, the same files serve, the record is new and says web.
        service_changed.reset_mock()
        with patch('wifi_bridge.cameras.driver_download.fetch', side_effect=AssertionError('nothing is downloaded')):
            value = self.rooted('POST', '/cameras/drivers/testcam/reactivate', {'version': '1.0', 'accept_terms': True})
        row = self.row(value)
        self.assertEqual((row['state'], row['origin'], row['version']), ('accepted', 'web', '1.0'))
        record = json.loads(drivers.acceptance_path('testcam', self.drivers).read_text())
        self.assertEqual((record['origin'], record['terms_url'], record['library_sha256']), ('web', 'https://www.example.com/terms', sha(elf())))
        self.assertEqual(sorted(p.name for p in folder.iterdir()), before)
        service_changed.assert_called_once_with('testcam')
        with root_owned(self.base):
            self.assertEqual(vendor_platform.resolve('testcam', self.drivers)['version'], '1.0')

    def test_reactivation_needs_the_terms_again_and_only_for_a_deactivated_driver(self):
        with self.assertRaises(ApiError) as never:  # Never installed: Install is the gesture.
            self.rooted('POST', '/cameras/drivers/testcam/reactivate', {'version': '1.0', 'accept_terms': True})
        self.assertEqual(never.exception.code, 'DRIVER_NOT_DEACTIVATED')
        self.installed()
        with self.assertRaises(ApiError) as active:  # Still active.
            self.rooted('POST', '/cameras/drivers/testcam/reactivate', {'version': '1.0', 'accept_terms': True})
        self.assertEqual(active.exception.code, 'DRIVER_NOT_DEACTIVATED')
        self.rooted('POST', '/cameras/drivers/testcam/withdraw')
        for data in ({}, {'version': '1.0'}, {'version': '1.0', 'accept_terms': False}, {'version': '0.9', 'accept_terms': True}):
            with self.subTest(data=data), self.assertRaises(ApiError) as refused:
                self.rooted('POST', '/cameras/drivers/testcam/reactivate', data)
            self.assertEqual(refused.exception.code, 'CAMERA_DRIVER_CHANGED')
            self.assertFalse(drivers.acceptance_path('testcam', self.drivers).exists())
            self.assertEqual(self.state()['state'], 'deactivated')
        with self.assertRaises(ApiError) as extra:
            self.rooted('POST', '/cameras/drivers/testcam/reactivate', {'version': '1.0', 'accept_terms': True, 'url': 'https://x.example.com'})
        self.assertEqual(extra.exception.status, 400)
        with self.assertRaises(ApiError) as native:
            self.rooted('POST', '/cameras/drivers/testcam/reactivate', {'version': '1.0', 'accept_terms': True}, browser=False)
        self.assertEqual(native.exception.status, 404)

    def test_files_that_changed_are_not_reactivated_the_line_offers_install_and_install_works(self):
        library = self.drivers / 'testcam' / 'libtestcam.so.1.0'
        damages = {
            'library changed': lambda: library.write_bytes(elf() + b'x'),
            'library missing': lambda: library.unlink(),
            'licence changed': lambda: (self.drivers / 'testcam' / 'license.txt').write_bytes(b'other terms'),
            'licence missing': lambda: (self.drivers / 'testcam' / 'license.txt').unlink(),
            'installation record missing': lambda: (self.drivers / 'testcam' / 'driver.json').unlink(),
            'folder gone': lambda: __import__('shutil').rmtree(self.drivers / 'testcam'),
        }
        for name, damage in damages.items():
            with self.subTest(name):
                self.installed()
                self.rooted('POST', '/cameras/drivers/testcam/withdraw')
                self.assertEqual(self.state()['state'], 'deactivated')
                damage()
                row = self.state()
                self.assertEqual((row['state'], row['version']), ('available', '1.0'))  # Install, never a line without a button.
                with self.assertRaises(ApiError) as caught:
                    self.rooted('POST', '/cameras/drivers/testcam/reactivate', {'version': '1.0', 'accept_terms': True})
                self.assertEqual(caught.exception.code, 'DRIVER_NOT_DEACTIVATED')
                self.assertFalse(drivers.acceptance_path('testcam', self.drivers).exists())
                self.installed()
                self.assertEqual(self.state()['state'], 'accepted')
                self.rooted('POST', '/cameras/drivers/testcam/withdraw')
                self.assertEqual(self.state()['state'], 'deactivated')
                import shutil
                shutil.rmtree(self.drivers / 'testcam', ignore_errors=True)
                drivers.acceptance_path('testcam', self.drivers).unlink(missing_ok=True)

    def test_a_record_for_other_files_is_not_a_reactivation(self):
        self.installed()
        drivers.write_private_json(drivers.acceptance_path('testcam', self.drivers),
                                   drivers.acceptance_record('testcam', '0.9', sha(b'old library'), None, CLOCK, 'web', self.catalog))
        row = self.state()
        # The files are exactly the catalog build: one gesture brings them back.
        self.assertEqual((row['state'], row['version']), ('deactivated', '1.0'))
        self.rooted('POST', '/cameras/drivers/testcam/reactivate', {'version': '1.0', 'accept_terms': True})
        self.assertEqual(self.state()['state'], 'accepted')


class ReactivationFormsTests(unittest.TestCase):
    """Every manufacturer of the catalog, with and without a licence file: Deactivate then Reactivate."""
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.base = Path(folder.name); self.base.chmod(0o755)
        self.drivers = self.base / 'camera-drivers'
        self.library, self.licence = elf(), b'Synthetic manufacturer terms\n'

    def catalog_for(self, vendor, licence):
        import tarfile
        members = {'sdk/lib/libvendor.so.1.0': self.library}
        if licence:
            members['sdk/license.txt'] = self.licence
        archive = self.base / 'sdk.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name); info.size = len(data); tar.addfile(info, io.BytesIO(data))
        value = driver_catalog.catalog()
        build = {'version': '9.9', 'arch': 'arm64', 'source': 'download', 'url': 'https://downloads.example.com/sdk.tar.gz',
                 'redirect_hosts': [], 'size': archive.stat().st_size, 'sha256': sha(archive.read_bytes()), 'nested': None,
                 'files': [{'member': 'sdk/lib/libvendor.so.1.0', 'sha256': sha(self.library), 'install_as': 'libvendor.so.1.0'}],
                 'license_member': 'sdk/license.txt' if licence else None, 'license_sha256': sha(self.licence) if licence else None}
        next(entry for entry in value['drivers'] if entry['id'] == vendor)['builds'] = [build]
        return driver_catalog.validate_catalog(value), build, archive

    def row(self, value, vendor):
        with root_owned(self.base):
            return next(r for r in drivers.rows(value, 'arm64', True, self.drivers) if r['id'] == vendor)

    def test_each_manufacturer_goes_back_in_one_gesture_with_the_same_files(self):
        vendors = ['zwo_asi', 'qhyccd', 'player_one', 'atik', 'moravian']
        self.assertEqual([e['id'] for e in driver_catalog.catalog()['drivers']], vendors)
        for vendor in vendors:
            for licence in (True, False):
                with self.subTest(vendor=vendor, licence=licence):
                    value, build, archive = self.catalog_for(vendor, licence)
                    drivers.install_archive(vendor, build, archive, CLOCK, 'setup', self.drivers, value, workdir=self.base)
                    folder = self.drivers / drivers.folder_name(vendor)
                    files = {p.name: p.read_bytes() for p in folder.iterdir()}
                    self.assertEqual(self.row(value, vendor)['state'], 'accepted')
                    drivers.withdraw(drivers.acceptance_path(vendor, self.drivers))
                    row = self.row(value, vendor)
                    self.assertEqual((row['state'], row['version'], row['origin']), ('deactivated', '9.9', None))
                    with root_owned(self.base):
                        record = drivers.reactivate(vendor, 'arm64', CLOCK, 'web', self.drivers, value)
                    self.assertEqual((record['origin'], record['version'], record['license_sha256']),
                                     ('web', '9.9', sha(self.licence) if licence else None))
                    self.assertEqual(self.row(value, vendor)['state'], 'accepted')
                    self.assertEqual({p.name: p.read_bytes() for p in folder.iterdir()}, files)
                    # Files that are not the catalog build are never reactivated.
                    drivers.withdraw(drivers.acceptance_path(vendor, self.drivers))
                    (folder / 'libvendor.so.1.0').write_bytes(self.library + b'x')
                    with root_owned(self.base), self.assertRaisesRegex(ValueError, 'files_changed'):
                        drivers.reactivate(vendor, 'arm64', CLOCK, 'web', self.drivers, value)
                    self.assertEqual(self.row(value, vendor)['state'], 'available')
                    import shutil
                    shutil.rmtree(folder); drivers.acceptance_path(vendor, self.drivers).unlink(missing_ok=True)

    def test_a_zwo_library_of_the_owners_own_script_is_served_without_record_and_is_not_a_deactivation(self):
        value, build, archive = self.catalog_for('zwo_asi', True)
        drivers.install_archive('zwo_asi', build, archive, CLOCK, 'setup', self.drivers, value, workdir=self.base)
        folder = self.drivers / 'zwo'
        drivers.withdraw(drivers.acceptance_path('zwo_asi', self.drivers))
        (folder / 'driver.json').unlink()
        row = self.row(value, 'zwo_asi')
        self.assertEqual((row['state'], row['version']), ('installed', None))


class DemoInstallTests(Fixture):
    def test_demo_installs_without_network_and_its_internet_switch_says_offline(self):
        api = MobileAPI(self.controller)
        self.controller.demo = True
        value = InstallationTests('test_listed_files_are_checked_installed_and_accepted'); value.setUp(); self.addCleanup(value.doCleanups)
        catalog, build = value.catalog(value.archive())
        with patch.object(driver_catalog, '_cached', catalog), patch.dict(vendor_process.ADAPTERS, {'testcam': lambda device: None}):
            def call(method, path, data=None):
                return api.cameras.handle(method, path, {}, data or {}, '"' + api.revision() + '"' if method != 'GET' else None, browser=True)
            def wait():
                for _ in range(200):
                    job = self.controller.driver_jobs['testcam']
                    if job['state'] in ('done', 'failed'):
                        return job
                    time.sleep(0.02)
            call('POST', '/cameras/demo', {'internet': False})
            call('POST', '/cameras/drivers/testcam/install', {'version': '1.0', 'accept_terms': True})
            self.assertEqual(wait()['error'], 'offline')
            call('POST', '/cameras/demo', {'internet': True})
            call('POST', '/cameras/drivers/testcam/install', {'version': '1.0', 'accept_terms': True})
            self.assertEqual(wait()['state'], 'done')
            row = next(r for r in call('GET', '/cameras/drivers')['drivers'] if r['id'] == 'testcam')
            self.assertEqual((row['state'], row['origin']), ('accepted', 'web'))
            row = next(r for r in call('POST', '/cameras/drivers/testcam/withdraw')['drivers'] if r['id'] == 'testcam')
            self.assertEqual((row['state'], row['version']), ('deactivated', '1.0'))
            with self.assertRaises(ApiError) as refused:
                call('POST', '/cameras/drivers/testcam/reactivate', {'version': '1.0', 'accept_terms': False})
            self.assertEqual(refused.exception.code, 'CAMERA_DRIVER_CHANGED')
            row = next(r for r in call('POST', '/cameras/drivers/testcam/reactivate', {'version': '1.0', 'accept_terms': True})['drivers'] if r['id'] == 'testcam')
            self.assertEqual((row['state'], row['origin']), ('accepted', 'web'))
            with self.assertRaises(ApiError) as again:
                call('POST', '/cameras/drivers/testcam/reactivate', {'version': '1.0', 'accept_terms': True})
            self.assertEqual(again.exception.code, 'DRIVER_NOT_DEACTIVATED')


if __name__ == '__main__':
    unittest.main()
