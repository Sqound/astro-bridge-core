"""Versioned catalog of camera manufacturer drivers; data only, never code.

One entry per manufacturer: display name, whether a camera was verified on
hardware, the official page of its terms, its USB vendor identifiers, its
optional INDI driver and the builds Astro Bridge knows. A build is always
downloaded from the manufacturer's official address (source "download"): no
manufacturer file is ever part of an image. Each build names the only files ever
taken from the archive, each with its own SHA-256.

A fingerprint the owner has not recorded yet stays null. Such a build is never
installable: nothing unknown is ever installed. The bridge and Astro Bridge
Setup read the same file, published with each release.
"""
import copy
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .driver import CameraError

SCHEMA_VERSION = 1
ARCHES = ('arm64', 'armv6')
SOURCES = ('download',)
MAX_BYTES = 65536
MAX_DRIVERS = 32
MAX_BUILDS = 16
MAX_FILES = 8
MAX_ARCHIVE = 512 * 1024 * 1024
BUILTIN_DRIVERS = ('simulator', 'uvc')
# Public data, served with the interface; also the only place an existing updater accepts it.
PATH = Path(__file__).resolve().parents[1] / 'static' / 'drivers-catalog.json'
SHA256 = re.compile('[0-9a-f]{64}')
IDENT = re.compile('[a-z][a-z0-9_]{0,31}')
HOST = re.compile(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')
MEMBER = re.compile(r'[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+){0,7}')
BASENAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,127}')
VERSION = re.compile(r'[0-9A-Za-z][0-9A-Za-z.+-]{0,31}')


def _fail(code='invalid_driver_catalog'):
    raise CameraError(code)


def _exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        _fail()


def https_url(value):
    """HTTPS on port 443, a public host name, no credentials in the address."""
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        _fail()
    parts = urlsplit(value)
    if (parts.scheme != 'https' or parts.username or parts.password or parts.port not in (None, 443)
            or not parts.hostname or not HOST.fullmatch(parts.hostname) or parts.fragment):
        _fail()
    return value


def _optional(value, check):
    if value is not None and not check(value):
        _fail()


def validate_member(value):
    return (isinstance(value, str) and MEMBER.fullmatch(value) is not None
            and not any(part in ('.', '..') for part in value.split('/')))


def validate_build(value):
    keys = ('version', 'arch', 'source', 'url', 'redirect_hosts', 'size', 'sha256', 'nested', 'files',
            'license_member', 'license_sha256')
    _exact(value, keys)
    _optional(value['version'], lambda v: isinstance(v, str) and VERSION.fullmatch(v))
    if value['arch'] not in ARCHES or value['source'] not in SOURCES:
        _fail()
    hosts = value['redirect_hosts']
    if (not isinstance(hosts, list) or len(hosts) > 4 or len(set(hosts)) != len(hosts)
            or any(not isinstance(h, str) or not HOST.fullmatch(h) for h in hosts)):
        _fail()
    _optional(value['size'], lambda v: type(v) is int and 0 < v <= MAX_ARCHIVE)
    for key in ('sha256', 'license_sha256'):
        _optional(value[key], lambda v: isinstance(v, str) and SHA256.fullmatch(v))
    for key in ('nested', 'license_member'):
        _optional(value[key], validate_member)
    files = value['files']
    if not isinstance(files, list) or len(files) > MAX_FILES:
        _fail()
    names = set()
    for item in files:
        _exact(item, ('member', 'sha256', 'install_as'))
        if not validate_member(item['member']) or not isinstance(item['install_as'], str) \
                or not BASENAME.fullmatch(item['install_as']) or item['install_as'] in names \
                or item['install_as'] in ('platform.json', 'license.txt'):
            _fail()
        names.add(item['install_as'])
        _optional(item['sha256'], lambda v: isinstance(v, str) and SHA256.fullmatch(v))
    if value['url'] is None:
        _fail()
    https_url(value['url'])
    if value['license_member'] is None and value['license_sha256'] is not None:
        _fail()
    return value


def validate_entry(value):
    keys = ('id', 'name', 'verified_on_hardware', 'terms_url', 'usb_vendors', 'indi_driver', 'needs_firmware', 'builds')
    _exact(value, keys)
    if not isinstance(value['id'], str) or not IDENT.fullmatch(value['id']) or value['id'] in BUILTIN_DRIVERS:
        _fail()
    if not isinstance(value['name'], str) or not 1 <= len(value['name']) <= 40 or not value['name'].strip() \
            or any(ord(c) < 32 for c in value['name']):
        _fail()
    if type(value['verified_on_hardware']) is not bool or type(value['needs_firmware']) is not bool:
        _fail()
    https_url(value['terms_url'])
    vendors = value['usb_vendors']
    if (not isinstance(vendors, list) or not 1 <= len(vendors) <= 8 or len(set(vendors)) != len(vendors)
            or any(type(v) is not int or not 1 <= v <= 0xffff for v in vendors)):
        _fail()
    _optional(value['indi_driver'], lambda v: isinstance(v, str) and re.fullmatch(r'indi_[a-z0-9_]{1,40}', v))
    builds = value['builds']
    if not isinstance(builds, list) or len(builds) > MAX_BUILDS:
        _fail()
    for build in builds:
        validate_build(build)
    return value


def validate_catalog(value):
    _exact(value, ('schema_version', 'catalog_version', 'drivers'))
    if type(value['schema_version']) is not int or value['schema_version'] != SCHEMA_VERSION:
        _fail('unsupported_driver_catalog')
    if not isinstance(value['catalog_version'], str) or not re.fullmatch(r'[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]{1,3}', value['catalog_version']):
        _fail()
    drivers = value['drivers']
    if not isinstance(drivers, list) or not 1 <= len(drivers) <= MAX_DRIVERS:
        _fail()
    seen, vendors = set(), set()
    for entry in drivers:
        validate_entry(entry)
        if entry['id'] in seen:
            _fail('duplicate_driver_id')
        seen.add(entry['id'])
        # One USB vendor belongs to one manufacturer: no ambiguous match.
        if vendors & set(entry['usb_vendors']):
            _fail('ambiguous_driver_vendor')
        vendors |= set(entry['usb_vendors'])
    return copy.deepcopy(value)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('duplicate_driver_catalog_field')
        result[key] = value
    return result


def load_catalog(path=None):
    """Read and validate a catalog file; bounded, no executable content."""
    path = Path(path) if path is not None else PATH
    if path.is_symlink():
        _fail('invalid_driver_catalog_file')
    with path.open('rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        _fail('oversized_driver_catalog')
    try:
        value = json.loads(data, object_pairs_hook=_unique)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        _fail('invalid_driver_catalog_json')
    return validate_catalog(value)


_cached = None


def catalog():
    """The catalog shipped with this bridge, read once."""
    global _cached
    if _cached is None:
        _cached = load_catalog()
    return copy.deepcopy(_cached)


def entries(value=None):
    return {entry['id']: entry for entry in (value or catalog())['drivers']}


def driver_ids(value=None):
    return BUILTIN_DRIVERS + tuple(entries(value))


def installable(build):
    """Every fingerprint is known: the archive (downloads), each file, and the licence text if any."""
    files_known = bool(build['files']) and all(item['sha256'] for item in build['files'])
    licence_known = build['license_member'] is None or build['license_sha256'] is not None
    return (bool(build['version']) and build['size'] is not None and build['sha256'] is not None
            and files_known and licence_known)


def host_arch(machine=None):
    """Catalog architecture of this host, or None (x86 hosts and ARMv7 have no catalog builds yet)."""
    import platform
    name = (machine if machine is not None else platform.machine()).lower()
    return {'aarch64': 'arm64', 'arm64': 'arm64', 'armv6l': 'armv6'}.get(name)


def builds_for(entry, arch):
    return [build for build in entry['builds'] if build['arch'] == arch]


def catalog_schema():
    """JSON Schema of the catalog, for editors and for Setup; the validator above is the reference."""
    def obj(properties):
        return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}
    sha = {'type': ['string', 'null'], 'pattern': '^[0-9a-f]{64}$'}
    member = {'type': ['string', 'null'], 'pattern': '^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+){0,7}$'}
    url = {'type': 'string', 'format': 'uri', 'pattern': '^https://'}
    build = obj({
        'version': {'type': ['string', 'null'], 'pattern': '^[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$'},
        'arch': {'enum': list(ARCHES)}, 'source': {'enum': list(SOURCES)},
        'url': {'type': ['string', 'null'], 'pattern': '^https://'},
        'redirect_hosts': {'type': 'array', 'maxItems': 4, 'uniqueItems': True, 'items': {'type': 'string'}},
        'size': {'type': ['integer', 'null'], 'minimum': 1, 'maximum': MAX_ARCHIVE},
        'sha256': sha, 'nested': member,
        'files': {'type': 'array', 'maxItems': MAX_FILES, 'items': obj({
            'member': {'type': 'string', 'pattern': member['pattern']}, 'sha256': sha,
            'install_as': {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$'}})},
        'license_member': member, 'license_sha256': sha})
    entry = obj({
        'id': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,31}$', 'not': {'enum': list(BUILTIN_DRIVERS)}},
        'name': {'type': 'string', 'minLength': 1, 'maxLength': 40},
        'verified_on_hardware': {'type': 'boolean'}, 'terms_url': url,
        'usb_vendors': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'uniqueItems': True,
                        'items': {'type': 'integer', 'minimum': 1, 'maximum': 65535}},
        'indi_driver': {'type': ['string', 'null'], 'pattern': '^indi_[a-z0-9_]{1,40}$'},
        'needs_firmware': {'type': 'boolean'},
        'builds': {'type': 'array', 'maxItems': MAX_BUILDS, 'items': build}})
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema', **obj({
        'schema_version': {'type': 'integer', 'const': SCHEMA_VERSION},
        'catalog_version': {'type': 'string', 'pattern': r'^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]{1,3}$'},
        'drivers': {'type': 'array', 'minItems': 1, 'maxItems': MAX_DRIVERS, 'items': entry}})}
