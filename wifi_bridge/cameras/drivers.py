"""Manufacturer camera drivers on the bridge: state, traced acceptance and installation.

Common to every manufacturer of the driver catalog (catalog.py):

- a driver stays off until its owner accepts the manufacturer's terms; the
  acceptance is a small root-only record bound to the exact library (and licence
  text, when the archive has one), with the terms page, the catalog version, the
  date with the state of the clock, and where it was given (origin: setup, web
  or installer). Withdrawing it (Deactivate) removes the record and nothing
  else: the files stay, and Reactivate on the Cameras page records the owner's
  acceptance again for files that are still exactly a catalog build;
- installation takes only the files the catalog lists from an archive whose
  size and SHA-256 match the catalog, checks each file's SHA-256 and ELF class,
  machine and ABI, and publishes the folder in one rename. Nothing is executed
  and no other member is read. Downloading is not part of this module;
- each opened camera runs its manufacturer library in its own child process
  (vendor_process.py).

Records written before this module (ZWO only) keep being read: their origin is web.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile

from . import catalog as driver_catalog

STATE = Path('/var/lib/wifi-bridge')
DRIVERS = STATE / 'camera-drivers'
ORIGINS = ('setup', 'web', 'installer')
LEGACY_KEYS = frozenset(('vendor', 'version', 'library_sha256', 'license_sha256', 'accepted_utc', 'clock_status'))
KEYS = LEGACY_KEYS | {'origin', 'terms_url', 'catalog_version'}
MAX_LIBRARY = 32 * 1024 * 1024
MAX_LICENSE = 2 * 1024 * 1024  # Some manufacturers ship their terms as a PDF (Atik).
MAX_NESTED = 256 * 1024 * 1024
MAX_MEMBERS = 20000
# (ELF class, machine) of each catalog architecture; ARMv6 also needs the hard-float ABI flag.
ELF = {'arm64': (2, 183), 'armv6': (1, 40)}


def folder_name(vendor):
    """Installation folder of a manufacturer; ZWO keeps the folder its installer always used."""
    return 'zwo' if vendor == 'zwo_asi' else vendor.replace('_', '-')


def acceptance_path(vendor, drivers=DRIVERS):
    return drivers / (vendor.replace('_', '-') + '-license.json')


def trusted(path, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if info.st_uid != 0 or info.st_mode & 0o022 or not kind(info.st_mode):
        raise ValueError('untrusted_installation')
    return info


def small_json(path, limit=1024):
    if trusted(path).st_size > limit:
        raise ValueError('oversized_record')
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict):
        raise ValueError('invalid_record')
    return data


def sha256_text(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def read_acceptance(path, vendor):
    """The owner's acceptance for this manufacturer, None when absent; raises when malformed."""
    try:
        data = small_json(path)
    except FileNotFoundError:
        return None
    if set(data) not in (LEGACY_KEYS, KEYS) or data['vendor'] != vendor:
        raise ValueError('invalid_acceptance')
    data = dict({'origin': 'web', 'terms_url': None, 'catalog_version': None}, **data)
    legacy_licence = sha256_text(data['license_sha256'])
    if (not isinstance(data['version'], str) or not sha256_text(data['library_sha256'])
            or not (legacy_licence or (data['license_sha256'] is None and set(data) == KEYS))
            or not isinstance(data['accepted_utc'], str) or len(data['accepted_utc']) > 40
            or data['clock_status'] not in ('synchronized', 'unverified') or data['origin'] not in ORIGINS
            or not (data['terms_url'] is None or isinstance(data['terms_url'], str) and len(data['terms_url']) <= 512)
            or not (data['catalog_version'] is None or isinstance(data['catalog_version'], str) and len(data['catalog_version']) <= 32)):
        raise ValueError('invalid_acceptance')
    return data


def acceptance_record(vendor, version, library_sha256, license_sha256, clock, origin, value=None):
    """What the owner accepted, where and when: never a password, never a client address."""
    if origin not in ORIGINS:
        raise ValueError('invalid_origin')
    value = value or driver_catalog.catalog()
    entry = driver_catalog.entries(value).get(vendor)
    return {'vendor': vendor, 'version': version, 'library_sha256': library_sha256, 'license_sha256': license_sha256,
            'clock_status': clock['clock_status'], 'accepted_utc': clock['corrected_utc'], 'origin': origin,
            'terms_url': entry['terms_url'] if entry else None, 'catalog_version': value['catalog_version']}


def write_private_json(path, record):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return record


def withdraw(path):
    path.unlink(missing_ok=True)


def elf_arch(head):
    """Catalog architecture of an ELF header, or None. Never loads the file."""
    if len(head) < 64 or head[:4] != b'\x7fELF' or head[5] != 1 or head[6] != 1:
        return None
    machine = int.from_bytes(head[18:20], 'little')
    for arch, (klass, expected) in ELF.items():
        if head[4] == klass and machine == expected:
            if arch == 'armv6' and int.from_bytes(head[36:40], 'little') & 0x600 != 0x400:
                return None  # Soft-float or unknown ARM ABI.
            return arch
    return None


def _copy_bounded(source, destination, limit):
    digest, size = hashlib.sha256(), 0
    while chunk := source.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise ValueError('member_too_large')
        digest.update(chunk)
        destination.write(chunk)
    return digest.hexdigest()


def _file_digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _members(archive):
    """Regular members of a zip or tar archive, as (name, size, opener). Links and paths are refused."""
    if zipfile.is_zipfile(archive):
        handle = zipfile.ZipFile(archive)
        items = handle.infolist()
        if len(items) > MAX_MEMBERS:
            handle.close()
            raise ValueError('archive_limit')
        result = {}
        for item in items:
            if item.is_dir() or (item.external_attr >> 28) not in (0, 8):
                continue
            if item.filename in result:
                handle.close()
                raise ValueError('duplicate_member')
            result[item.filename] = (item.file_size, (lambda item=item: handle.open(item)))
        return handle, result
    handle = tarfile.open(archive, mode='r:*')
    result, count = {}, 0
    for member in handle:
        count += 1
        if count > MAX_MEMBERS:
            handle.close()
            raise ValueError('archive_limit')
        if not member.isfile():
            continue
        if member.name in result:
            handle.close()
            raise ValueError('duplicate_member')
        result[member.name] = (member.size, (lambda member=member: handle.extractfile(member)))
    return handle, result


def install_archive(vendor, build, archive, clock, origin, drivers=DRIVERS, value=None, workdir=None):
    """Install one catalog build from a local archive, then record the owner's acceptance.

    Raises ValueError with a plain code (fingerprint_missing, archive_changed,
    member_missing, file_changed, wrong_architecture, licence_changed) and leaves
    any earlier installation of this manufacturer untouched.
    """
    value = value or driver_catalog.catalog()
    entry = driver_catalog.entries(value).get(vendor)
    if entry is None or build not in entry['builds'] or build['source'] != 'download':
        raise ValueError('unknown_build')
    if not driver_catalog.installable(build):
        raise ValueError('fingerprint_missing')
    archive = Path(archive)
    if archive.stat().st_size != build['size'] or _file_digest(archive) != build['sha256']:
        raise ValueError('archive_changed')
    drivers.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.install-' + folder_name(vendor) + '-', dir=drivers))
    scratch = Path(tempfile.mkdtemp(prefix='.unpack-', dir=workdir or drivers))
    handles = []
    try:
        source = archive
        if build['nested']:
            handle, members = _members(source)
            handles.append(handle)
            if build['nested'] not in members or members[build['nested']][0] > MAX_NESTED:
                raise ValueError('member_missing')
            source = scratch / PurePosixPath(build['nested']).name
            with members[build['nested']][1]() as stream, source.open('xb') as out:
                _copy_bounded(stream, out, MAX_NESTED)
        handle, members = _members(source)
        handles.append(handle)
        library = None
        for item in build['files']:
            if item['member'] not in members or members[item['member']][0] > MAX_LIBRARY:
                raise ValueError('member_missing')
            target = staging / item['install_as']
            with members[item['member']][1]() as stream, target.open('xb') as out:
                if _copy_bounded(stream, out, MAX_LIBRARY) != item['sha256']:
                    raise ValueError('file_changed')
            with target.open('rb') as stream:
                if elf_arch(stream.read(64)) != build['arch']:
                    raise ValueError('wrong_architecture')
            target.chmod(0o644)
            library = library or item
        licence_sha = None
        if build['license_member']:
            if build['license_member'] not in members or members[build['license_member']][0] > MAX_LICENSE:
                raise ValueError('member_missing')
            with members[build['license_member']][1]() as stream, (staging / 'license.txt').open('xb') as out:
                licence_sha = _copy_bounded(stream, out, MAX_LICENSE)
            if licence_sha != build['license_sha256']:
                raise ValueError('licence_changed')
            (staging / 'license.txt').chmod(0o644)
        final = drivers / folder_name(vendor)
        # The platform record format the ZWO loader has always read.
        write_private_json(staging / 'platform.json', {'path': str(final / library['install_as']), 'sha256': library['sha256']})
        write_private_json(staging / 'driver.json', {'vendor': vendor, 'version': build['version'], 'arch': build['arch'],
                                                    'catalog_version': value['catalog_version']})
        rules = udev_rules(vendor)
        if rules:
            (staging / ('70-astro-bridge-' + folder_name(vendor) + '.rules')).write_text(rules)
        staging.chmod(0o755)
        previous = None
        if final.exists() or final.is_symlink():
            previous = Path(tempfile.mkdtemp(prefix='.previous-', dir=drivers))
            os.rename(final, previous / 'driver')
        os.rename(staging, final)
        if previous is not None:
            shutil.rmtree(previous, ignore_errors=True)
        record = acceptance_record(vendor, build['version'], library['sha256'], licence_sha, clock, origin, value)
        write_private_json(acceptance_path(vendor, drivers), record)
        return record
    finally:
        for handle in handles:
            handle.close()
        shutil.rmtree(scratch, ignore_errors=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def udev_rules(vendor):
    """Root-only access to the exact models of this manufacturer's camera table, never a whole vendor."""
    from .registry import load_registry
    lines = []
    for descriptor in load_registry().descriptors():
        if descriptor['driver'] != vendor:
            continue
        for rule in descriptor['usb']:
            if set(rule) == {'vendor_id', 'product_id'}:
                lines.append('SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="%04x", '
                             'ATTR{idProduct}=="%04x", OWNER="root", GROUP="root", MODE="0600"\n'
                             % (rule['vendor_id'], rule['product_id']))
            elif 'except_product_ids' in rule:
                # Root only for this manufacturer's products: never the manufacturer's world-writable rule.
                excluded = ''.join('ATTR{idProduct}!="%04x", ' % product for product in rule['except_product_ids'])
                lines.append('SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="%04x", %s'
                             'OWNER="root", GROUP="root", MODE="0600"\n' % (rule['vendor_id'], excluded))
    return ''.join(lines)


def installed_record(vendor, drivers=DRIVERS):
    """A completed installation of this manufacturer, from its root-only records, or None."""
    folder = drivers / folder_name(vendor)
    try:
        trusted(folder, directory=True)
        platform = small_json(folder / 'platform.json')
        path = Path(platform.get('path', '')) if isinstance(platform.get('path'), str) else None
        if set(platform) != {'path', 'sha256'} or path is None or path.parent != folder or not sha256_text(platform['sha256']):
            return None
        meta = small_json(folder / 'driver.json') if (folder / 'driver.json').exists() else {}
        return {'path': path, 'sha256': platform['sha256'], 'version': meta.get('version') if isinstance(meta.get('version'), str) else None}
    except (OSError, ValueError, json.JSONDecodeError, UnicodeError, RecursionError):
        return None


def reactivation(vendor, arch, drivers=DRIVERS, value=None):
    """The catalog build that the installed files of a deactivated driver still are, or None.

    The files must be the ones this installer wrote: an installation record
    (driver.json) of this manufacturer and, for a download build of this catalog
    for this hardware, every listed file and the licence with exactly the
    catalog's SHA-256, owned by root and not writable by others. Anything else
    (changed, partial or foreign files, a library placed by the owner's own
    script) is not reactivated: it is installed again.
    """
    value = value or driver_catalog.catalog()
    entry = driver_catalog.entries(value).get(vendor)
    found = installed_record(vendor, drivers)
    if entry is None or found is None or not arch:
        return None
    folder = drivers / folder_name(vendor)
    try:
        meta = small_json(folder / 'driver.json')
        if meta.get('vendor') != vendor:
            return None
        for build in driver_catalog.builds_for(entry, arch):
            if build['source'] != 'download' or not driver_catalog.installable(build) or build['version'] != meta.get('version'):
                continue
            first = build['files'][0]
            if first['sha256'] != found['sha256'] or folder / first['install_as'] != found['path']:
                continue
            if any(trusted(folder / item['install_as']).st_size > MAX_LIBRARY or _file_digest(folder / item['install_as']) != item['sha256']
                   for item in build['files']):
                continue
            if build['license_member'] and (trusted(folder / 'license.txt').st_size > MAX_LICENSE
                                            or _file_digest(folder / 'license.txt') != build['license_sha256']):
                continue
            return build
    except (OSError, ValueError, json.JSONDecodeError, UnicodeError, RecursionError, KeyError):
        return None
    return None


def reactivate(vendor, arch, clock, origin, drivers=DRIVERS, value=None):
    """Record the owner's acceptance again for the files of a deactivated driver; nothing is downloaded.

    Raises ValueError('files_changed') when the files are no longer exactly a
    catalog build: the owner then installs the driver again.
    """
    value = value or driver_catalog.catalog()
    build = reactivation(vendor, arch, drivers, value)
    if build is None:
        raise ValueError('files_changed')
    record = acceptance_record(vendor, build['version'], build['files'][0]['sha256'],
                               build['license_sha256'] if build['license_member'] else None, clock, origin, value)
    write_private_json(acceptance_path(vendor, drivers), record)
    return record


SETUP_RESULTS = 'setup-results.json'
SETUP_CODE = re.compile('[a-z_]{3,40}')


def setup_results(drivers=DRIVERS, trusted_only=True):
    """What the first boot did with the drivers Setup placed on the card: {vendor or '*': code}.

    Written once by the first boot (image/pi5/firstboot.py, image/alpine/persistence.py).
    Absent, unreadable or malformed: {} (the page then says nothing about Setup).
    """
    path = drivers / SETUP_RESULTS
    try:
        if trusted_only:
            data = small_json(path, limit=4096)
        else:  # The demonstration's own folder, never the bridge's.
            data = json.loads(path.read_text()) if path.stat().st_size <= 4096 else {}
    except (OSError, ValueError, json.JSONDecodeError, UnicodeError, RecursionError):
        return {}
    if not isinstance(data, dict) or len(data) > 64:
        return {}
    return {key: code for key, code in data.items()
            if isinstance(key, str) and (key == '*' or re.fullmatch('[a-z][a-z0-9_]{0,31}', key))
            and isinstance(code, str) and SETUP_CODE.fullmatch(code)}


def rows(value=None, arch=None, supported=True, drivers=DRIVERS, adapters=None):
    """One row per manufacturer that has a build for this hardware; data for the Cameras page.

    state: accepted (installed, terms accepted), deactivated (its files are still
    exactly a catalog build and the acceptance was withdrawn: Reactivate),
    installed (a ZWO library placed by the owner's own script, served without a
    record), available (can be installed: every fingerprint is known), impossible
    (reason given). Files that are neither deactivated nor the owner's script count
    as absent: Install replaces them. ZWO is a line like the others.
    adapters, when given, keeps only the manufacturers that have a camera
    adapter on this bridge: a driver nobody can serve is not offered.
    """
    if not supported:
        return []
    value = value or driver_catalog.catalog()
    result = []
    for entry in value['drivers']:
        builds = driver_catalog.builds_for(entry, arch) if arch else []
        if not builds:
            continue  # No official build for this hardware: no line.
        if adapters is not None and entry['id'] not in adapters:
            continue  # No adapter: its cameras could never be served.
        row = {'id': entry['id'], 'name': entry['name'], 'verified_on_hardware': entry['verified_on_hardware'],
               'terms_url': entry['terms_url'], 'state': 'impossible', 'version': None, 'accepted_utc': None,
               'origin': None, 'reason': 'fingerprint_missing', 'license_text': False}
        found = installed_record(entry['id'], drivers)
        if found:
            try:
                record = read_acceptance(acceptance_path(entry['id'], drivers), entry['id'])
            except (OSError, ValueError, json.JSONDecodeError, UnicodeError):
                record = None
            if record is not None and record['library_sha256'] == found['sha256']:
                row.update(state='accepted', version=found['version'] or record['version'], reason=None,
                           accepted_utc=record['accepted_utc'], origin=record['origin'])
            elif reactivation(entry['id'], arch, drivers, value) is not None:
                row.update(state='deactivated', version=found['version'], reason=None)
            elif entry['id'] == 'zwo_asi' and not (drivers / folder_name('zwo_asi') / 'driver.json').exists():
                row.update(state='installed', version=found['version'], reason=None)
            else:
                found = None  # Foreign or changed files: as if absent, Install replaces them.
        if not found:
            ready = [build for build in builds if driver_catalog.installable(build)]
            if ready:
                row.update(state='available', version=ready[0]['version'], reason=None)
        result.append(row)
    return result
