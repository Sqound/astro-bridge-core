"""Camera drivers of several manufacturers: catalog, open registry, traced acceptance, installation, isolation."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from wifi_bridge.api import ApiError, MobileAPI
from wifi_bridge.cameras import catalog as driver_catalog
from wifi_bridge.cameras import drivers
from wifi_bridge.cameras.driver import CameraError
from wifi_bridge.cameras.registry import CameraRegistry, load_registry, manifest_schema, validate_manifest
from test_bridge import Fixture
from test_camera_sdk import root_owned

ROOT = Path(__file__).resolve().parents[1]
CLOCK = {'clock_status': 'unverified', 'corrected_utc': '2026-11-19T10:00:00.000+00:00'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def elf(klass=2, machine=183, flags=0):
    head = bytearray(64)
    head[:4] = b'\x7fELF'; head[4] = klass; head[5] = 1; head[6] = 1
    head[18:20] = machine.to_bytes(2, 'little'); head[36:40] = flags.to_bytes(4, 'little')
    return bytes(head) + b'synthetic library body'


def unpinned(ident):
    """The shipped catalog with one manufacturer back to waiting for its fingerprints."""
    value = driver_catalog.catalog()
    for build in driver_catalog.entries(value)[ident]['builds']:
        build.update(size=None, sha256=None, license_sha256=None)
        for item in build['files']:
            item['sha256'] = None
    return value


class CatalogTests(unittest.TestCase):
    def test_shipped_catalog_schema_and_copy_are_reproducible(self):
        source = ROOT / 'cameras/catalog/drivers-catalog.json'
        value = driver_catalog.load_catalog(source)
        self.assertEqual(driver_catalog.PATH.read_text(), source.read_text())
        self.assertEqual(json.loads((ROOT / 'cameras/schema/drivers-catalog.schema.json').read_text()), driver_catalog.catalog_schema())
        self.assertEqual(driver_catalog.catalog(), value)
        self.assertEqual([e['id'] for e in value['drivers']], ['zwo_asi', 'qhyccd', 'player_one', 'atik', 'moravian'])

    def test_every_manufacturer_is_pinned_and_marked_not_verified(self):
        entries = driver_catalog.entries()
        zwo = entries['zwo_asi']
        # Verified with SDK 1.41 supplied in an image, not with the 1.42 the bridge downloads: not verified.
        self.assertFalse(zwo['verified_on_hardware'])
        self.assertEqual(zwo['usb_vendors'], [0x03c3])
        self.assertEqual(zwo['indi_driver'], 'indi_asi_ccd')
        # Like every manufacturer, ZWO is only ever downloaded from its site: no build comes with an image.
        self.assertEqual({b['source'] for e in entries.values() for b in e['builds']}, {'download'})
        self.assertTrue(zwo['builds'][0]['url'].startswith('https://dl.zwoastro.com/'))
        self.assertEqual({b['license_sha256'] for b in zwo['builds']}, {'98ad1c18048bfdabc8463740ac36a8d8cd710bdc3102ac6c22978ec50056e5a2'})
        self.assertTrue(all(driver_catalog.installable(b) for b in zwo['builds']))
        for ident in ('qhyccd', 'player_one', 'atik', 'moravian'):
            with self.subTest(ident=ident):
                entry = entries[ident]
                self.assertFalse(entry['verified_on_hardware'])
                self.assertTrue(entry['terms_url'].startswith('https://'))
                # Fingerprints recorded: offered for download, and still marked as not verified on hardware.
                self.assertTrue(entry['builds'])
                self.assertTrue(all(driver_catalog.installable(b) for b in entry['builds']))
                self.assertFalse(driver_catalog.installable(driver_catalog.entries(unpinned(ident))[ident]['builds'][0]))
        # No line without a direct official address: ToupTek and SVBONY are absent.
        self.assertFalse({'touptek', 'svbony'} & set(entries))

    def test_invalid_catalogs_are_refused(self):
        base = driver_catalog.catalog()
        def changed(edit):
            value = copy.deepcopy(base); edit(value); return value
        build = lambda v: v['drivers'][1]['builds'][0]
        cases = {
            'http address': lambda v: build(v).update(url='http://example.com/sdk.tar.gz'),
            'credentials in address': lambda v: build(v).update(url='https://user:secret@example.com/sdk.tar.gz'),
            'other port': lambda v: build(v).update(url='https://example.com:8443/sdk.tar.gz'),
            'terms not https': lambda v: v['drivers'][1].update(terms_url='ftp://example.com/'),
            'unknown arch': lambda v: build(v).update(arch='x86'),
            'path escape': lambda v: build(v).update(files=[{'member': '../etc/passwd', 'sha256': None, 'install_as': 'lib.so'}]),
            'reserved file name': lambda v: build(v).update(files=[{'member': 'lib/a.so', 'sha256': None, 'install_as': 'platform.json'}]),
            'a build that comes with an image': lambda v: v['drivers'][0]['builds'][0].update(source='bundled', url=None),
            'download without address': lambda v: v['drivers'][0]['builds'][0].update(url=None),
            'duplicate id': lambda v: v['drivers'].append(copy.deepcopy(v['drivers'][1])),
            'shared vendor': lambda v: v['drivers'][2].update(usb_vendors=[0x03c3]),
            'built-in id': lambda v: v['drivers'][1].update(id='uvc'),
            'extra field': lambda v: v['drivers'][1].update(script='run.sh'),
            'bad fingerprint': lambda v: build(v).update(sha256='XYZ'),
            'licence fingerprint without text': lambda v: build(v).update(license_sha256='0' * 64),
            'schema version': lambda v: v.update(schema_version=2),
        }
        for name, edit in cases.items():
            with self.subTest(name=name), self.assertRaises(CameraError):
                driver_catalog.validate_catalog(changed(edit))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'catalog.json'
            path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaises(CameraError): driver_catalog.load_catalog(path)
            path.write_bytes(b' ' * (driver_catalog.MAX_BYTES + 1))
            with self.assertRaises(CameraError): driver_catalog.load_catalog(path)

    def test_architecture_of_the_host(self):
        self.assertEqual(driver_catalog.host_arch('aarch64'), 'arm64')
        self.assertEqual(driver_catalog.host_arch('armv6l'), 'armv6')
        self.assertIsNone(driver_catalog.host_arch('x86_64'))


class OpenRegistryTests(unittest.TestCase):
    TABLE = {'schema_version': 1, 'id': 'player_one_test', 'driver': 'player_one',
             'usb': [{'vendor_id': 0xa0a0, 'product_id': 0x1234}], 'validation': 'unverified'}

    def test_any_catalog_manufacturer_is_a_known_driver(self):
        import wifi_bridge.cameras.registry as registry
        self.assertFalse(hasattr(registry, 'DRIVER_IDS'))
        self.assertEqual(validate_manifest(self.TABLE)['driver'], 'player_one')
        self.assertEqual(set(manifest_schema()['properties']['driver']['enum']), {'simulator', 'uvc', *driver_catalog.entries()})

    def test_usb_vendor_and_indi_driver_belong_to_their_manufacturer(self):
        for change in ({'usb': [{'vendor_id': 0x03c3, 'product_id': 1}]}, {'usb': [{'class': 14}]},
                       {'indi_driver': 'indi_asi_ccd'}, {'indi_driver': 'indi_playerone_ccd'}, {'driver': 'touptek'}):
            with self.subTest(change=change), self.assertRaises(CameraError):
                validate_manifest({**self.TABLE, **change})
        zwo = json.loads((ROOT / 'cameras/zwo_asi.json').read_text())
        self.assertEqual(validate_manifest(zwo), zwo)
        with self.assertRaises(CameraError):
            validate_manifest({**zwo, 'usb': [{'vendor_id': 0xa0a0, 'product_id': 1}]})

    def test_a_manufacturer_without_adapter_is_matched_but_never_served(self):
        from unittest.mock import patch
        from wifi_bridge.cameras import vendor_process
        adapters = {k: v for k, v in vendor_process.ADAPTERS.items() if k != 'player_one'}
        with patch.dict(vendor_process.ADAPTERS, adapters, clear=True):
            registry = CameraRegistry([self.TABLE])
        self.assertEqual(registry.match_usb(0xa0a0, 0x1234, 255)['driver'], 'player_one')
        self.assertFalse(registry.available('player_one_test'))
        with self.assertRaisesRegex(CameraError, 'camera_driver_unavailable'):
            registry.create('player_one_test', device={})
        self.assertTrue(load_registry().available('zwo_asi'))
        # With its adapter, the same manufacturer is served once its driver is installed and accepted.
        registry = CameraRegistry([self.TABLE])
        self.assertTrue(registry.available('player_one_test'))
        device = dict(vendor_id=0xa0a0, product_id=0x1234, device_class=0, interface_classes=[255], speed_mbps=480, bus=1, port_path='1-1')
        with self.assertRaisesRegex(CameraError, 'camera_driver_unavailable'):
            registry.create('player_one_test', device=device)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.base = Path(folder.name); self.base.chmod(0o755)

    def test_earlier_zwo_records_stay_valid_with_origin_web(self):
        path = self.base / 'zwo-asi-license.json'
        path.write_text(json.dumps({'vendor': 'zwo_asi', 'version': '1.42', 'library_sha256': '1' * 64, 'license_sha256': '2' * 64,
                                    'accepted_utc': CLOCK['corrected_utc'], 'clock_status': 'unverified'}))
        with root_owned(self.base):
            record = drivers.read_acceptance(path, 'zwo_asi')
        self.assertEqual((record['origin'], record['terms_url'], record['catalog_version']), ('web', None, None))

    def test_new_records_carry_origin_terms_and_catalog_version(self):
        record = drivers.acceptance_record('zwo_asi', '1.42', '1' * 64, '2' * 64, CLOCK, 'setup')
        self.assertEqual(record['origin'], 'setup')
        self.assertEqual(record['terms_url'], 'https://www.zwoastro.com/software/product-sdk/')
        self.assertEqual(record['catalog_version'], driver_catalog.catalog()['catalog_version'])
        path = self.base / 'zwo-asi-license.json'
        drivers.write_private_json(path, record)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with root_owned(self.base):
            self.assertEqual(drivers.read_acceptance(path, 'zwo_asi'), record)
            path.write_text(json.dumps(dict(record, origin='client')))
            with self.assertRaises(ValueError): drivers.read_acceptance(path, 'zwo_asi')
            path.write_text(json.dumps(dict(record, vendor='qhyccd')))
            with self.assertRaises(ValueError): drivers.read_acceptance(path, 'zwo_asi')
        with self.assertRaises(ValueError):
            drivers.acceptance_record('zwo_asi', '1.42', '1' * 64, None, CLOCK, 'somewhere')

    def test_zwo_acceptance_records_its_origin_like_the_others(self):
        from test_camera_sdk import Layout
        layout = Layout(self.base)
        with root_owned(self.base):
            row = {r['id']: r for r in drivers.rows(driver_catalog.catalog(), 'arm64', True, layout.drivers)}['zwo_asi']
        self.assertEqual((row['state'], row['origin'], row['reason']), ('accepted', 'setup', None))


class InstallationTests(unittest.TestCase):
    """One module installs any manufacturer from a local archive; downloads are not part of it."""
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.base = Path(folder.name); self.base.chmod(0o755)
        self.drivers = self.base / 'camera-drivers'
        self.library, self.licence = elf(), b'Synthetic manufacturer terms\n'

    def archive(self, kind='tar', members=None, nested=False):
        members = members if members is not None else {'sdk/lib/arm64/libtestcam.so.1.0': self.library, 'sdk/license.txt': self.licence}
        path = self.base / ('sdk.' + ('zip' if kind == 'zip' else 'tar.gz'))
        def write_tar(target, items):
            with tarfile.open(target, 'w:gz') as tar:
                for name, data in items.items():
                    info = tarfile.TarInfo(name); info.size = len(data); tar.addfile(info, io.BytesIO(data))
        if nested:
            inner = self.base / 'inner.tar.gz'; write_tar(inner, members)
            with zipfile.ZipFile(path, 'w') as outer:
                outer.writestr('SDK/inner.tar.gz', inner.read_bytes())
        elif kind == 'zip':
            with zipfile.ZipFile(path, 'w') as outer:
                for name, data in members.items(): outer.writestr(name, data)
        else:
            write_tar(path, members)
        return path

    def catalog(self, archive, **changes):
        value = driver_catalog.catalog()
        build = {'version': '1.0', 'arch': 'arm64', 'source': 'download', 'url': 'https://downloads.example.com/sdk.tar.gz',
                 'redirect_hosts': [], 'size': archive.stat().st_size, 'sha256': sha(archive.read_bytes()), 'nested': None,
                 'files': [{'member': 'sdk/lib/arm64/libtestcam.so.1.0', 'sha256': sha(self.library), 'install_as': 'libtestcam.so.1.0'}],
                 'license_member': 'sdk/license.txt', 'license_sha256': sha(self.licence)}
        build.update(changes)
        value['drivers'].append({'id': 'testcam', 'name': 'Test camera', 'verified_on_hardware': False,
                                 'terms_url': 'https://www.example.com/terms', 'usb_vendors': [0x1209], 'indi_driver': None,
                                 'needs_firmware': False, 'builds': [build]})
        return driver_catalog.validate_catalog(value), build

    def install(self, value, build, archive):
        return drivers.install_archive('testcam', build, archive, CLOCK, 'setup', self.drivers, value, workdir=self.base)

    def test_listed_files_are_checked_installed_and_accepted(self):
        for kind, nested in (('tar', False), ('zip', False), ('zip', True)):
            with self.subTest(kind=kind, nested=nested):
                archive = self.archive(kind, nested=nested)
                value, build = self.catalog(archive, nested='SDK/inner.tar.gz' if nested else None)
                record = self.install(value, build, archive)
                folder = self.drivers / 'testcam'
                self.assertEqual((folder / 'libtestcam.so.1.0').read_bytes(), self.library)
                self.assertEqual((folder / 'license.txt').read_bytes(), self.licence)
                self.assertEqual(json.loads((folder / 'platform.json').read_text()),
                                 {'path': str(folder / 'libtestcam.so.1.0'), 'sha256': sha(self.library)})
                self.assertEqual(record['origin'], 'setup'); self.assertEqual(record['terms_url'], 'https://www.example.com/terms')
                self.assertEqual(stat.S_IMODE(drivers.acceptance_path('testcam', self.drivers).stat().st_mode), 0o600)
                self.assertFalse([p for p in self.drivers.iterdir() if p.name.startswith('.')])
                with root_owned(self.base):
                    row = next(r for r in drivers.rows(value, 'arm64', True, self.drivers) if r['id'] == 'testcam')
                self.assertEqual((row['state'], row['origin'], row['version']), ('accepted', 'setup', '1.0'))

    def test_nothing_unknown_is_installed_and_an_earlier_driver_stays(self):
        archive = self.archive()
        value, build = self.catalog(archive)
        self.install(value, build, archive)
        before = (self.drivers / 'testcam' / 'libtestcam.so.1.0').read_bytes()
        cases = {
            'fingerprint_missing': (self.catalog(archive, sha256=None), archive),
            'archive_changed': (self.catalog(archive, sha256='0' * 64), archive),
            'file_changed': (self.catalog(archive, files=[dict(build['files'][0], sha256='0' * 64)]), archive),
            'licence_changed': (self.catalog(archive, license_sha256='0' * 64), archive),
            'member_missing': (self.catalog(archive, files=[dict(build['files'][0], member='sdk/lib/absent.so')]), archive),
        }
        for code, ((value_x, build_x), path) in cases.items():
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                self.install(value_x, build_x, path)
            self.assertEqual((self.drivers / 'testcam' / 'libtestcam.so.1.0').read_bytes(), before)
        for library in (elf(1, 40, 0x200), elf(2, 62)):  # soft-float ARMv6, x86-64
            self.library = library
            other = self.archive()
            value_x, build_x = self.catalog(other)
            with self.subTest(library=library[:20]), self.assertRaisesRegex(ValueError, 'wrong_architecture'):
                self.install(value_x, build_x, other)
        with self.assertRaisesRegex(ValueError, 'unknown_build'):
            drivers.install_archive('player_one', build, archive, CLOCK, 'setup', self.drivers, value)

    def test_elf_architectures(self):
        self.assertEqual(drivers.elf_arch(elf(2, 183)), 'arm64')
        self.assertEqual(drivers.elf_arch(elf(1, 40, 0x400)), 'armv6')
        self.assertIsNone(drivers.elf_arch(elf(1, 40, 0x200)))
        self.assertIsNone(drivers.elf_arch(b'#!/bin/sh\n' + b' ' * 64))

    def test_rows_follow_the_hardware_and_the_catalog(self):
        value = driver_catalog.catalog()
        self.assertEqual(drivers.rows(value, 'arm64', False, self.drivers), [])
        with root_owned(self.base):
            arm64 = {r['id']: r for r in drivers.rows(value, 'arm64', True, self.drivers)}
            armv6 = {r['id'] for r in drivers.rows(value, 'armv6', True, self.drivers)}
        # Every manufacturer's fingerprints are pinned: each can be installed.
        self.assertEqual((arm64['zwo_asi']['state'], arm64['zwo_asi']['version']), ('available', '1.42'))
        self.assertEqual({r['state'] for r in arm64.values()}, {'available'})
        # A build without its fingerprints is never offered.
        with root_owned(self.base):
            waiting = {r['id']: r for r in drivers.rows(unpinned('atik'), 'arm64', True, self.drivers)}
        self.assertEqual((waiting['atik']['state'], waiting['atik']['reason']), ('impossible', 'fingerprint_missing'))
        # Moravian's ARMv6 archive holds only a static library: no ARMv6 build. Player One ships an ARMv6 hard-float one.
        self.assertEqual(armv6, {'zwo_asi', 'player_one'})
        self.assertEqual(drivers.rows(value, None, True, self.drivers), [])


class IsolationTests(unittest.TestCase):
    def test_only_reviewed_adapters_run_in_a_child_and_zwo_keeps_its_entry_point(self):
        import sys
        from wifi_bridge.cameras import vendor_process
        from wifi_bridge.cameras.zwo_process import IsolatedASI
        device = dict(vendor_id=0x03c3, product_id=0x120a, device_class=0, interface_classes=[255], speed_mbps=480, bus=1, port_path='1-1')
        self.assertIsNone(vendor_process.factory('touptek'))
        with self.assertRaisesRegex(CameraError, 'camera_driver_unavailable'):
            vendor_process.IsolatedDriver('touptek', device=device)
        self.assertEqual(set(vendor_process.ADAPTERS), {'zwo_asi', 'player_one', 'qhyccd', 'atik', 'moravian'})
        driver = vendor_process.factory('zwo_asi')(device=device)
        self.assertIsInstance(driver, IsolatedASI)
        self.assertEqual(driver.command(7), [sys.executable, '-m', 'wifi_bridge.cameras.zwo_process', '7'])
        driver.vendor = 'other'
        self.assertEqual(driver.command(7)[:4], [sys.executable, '-m', 'wifi_bridge.cameras.vendor_process', 'other'])


class DriversInterfaceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.api = MobileAPI(self.controller)

    def handle(self, method, path, data=None, revision=None, browser=True):
        return self.api.cameras.handle(method, path, {}, data or {}, revision, browser=browser)

    def test_page_lists_the_catalog_for_this_hardware(self):
        value = self.handle('GET', '/cameras/drivers')
        self.assertTrue(value['supported'])
        self.assertEqual([r['id'] for r in value['drivers']], ['zwo_asi', 'qhyccd', 'player_one', 'atik', 'moravian'])
        self.assertEqual(value['drivers'][0]['state'], 'available')
        revision = '"' + value['config_revision'] + '"'
        # Nothing is supplied with the bridge: there is no licence to accept before installing.
        with self.assertRaises(ApiError) as supplied:
            self.handle('POST', '/cameras/drivers/zwo_asi/accept', {'version': '1.42'}, revision)
        self.assertEqual(supplied.exception.code, 'DRIVER_NOT_SUPPLIED')
        # A build without its fingerprints: nothing to install.
        with patch.object(driver_catalog, '_cached', unpinned('player_one')), self.assertRaises(ApiError) as later:
            self.handle('POST', '/cameras/drivers/player_one/install', {'version': '3.10.1', 'accept_terms': True}, revision)
        self.assertEqual(later.exception.code, 'DRIVER_NOT_INSTALLABLE')
        with self.assertRaises(ApiError) as unknown:
            self.handle('POST', '/cameras/drivers/touptek/install', {}, revision)
        self.assertEqual(unknown.exception.status, 404)
        # The owner acts in the web interface; native clients do not see this list.
        with self.assertRaises(ApiError) as native:
            self.handle('GET', '/cameras/drivers', browser=False)
        self.assertEqual(native.exception.status, 404)

    def test_hardware_without_camera_service_says_so(self):
        self.controller.camera_supported = False
        value = self.handle('GET', '/cameras/drivers')
        self.assertEqual((value['supported'], value['drivers']), (False, []))


if __name__ == '__main__':
    unittest.main()
