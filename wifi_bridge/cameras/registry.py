"""Bounded declarative USB hints and curated driver factories; no USB discovery.

Exact vendor/product rules outrank class rules. Duplicate rules at the same
specificity fail closed. A match is a hint, never consent to open a device.
UVC has a reviewed lazy platform factory; construction never probes a device.
The source table is loaded explicitly; importing this module reads no files.
"""
import copy
import json
from pathlib import Path
import re

from .driver import CameraError, integer

SCHEMA_VERSION = 1
DRIVER_IDS = ('simulator', 'uvc', 'zwo_asi')
MAX_MANIFESTS = 32
MAX_MANIFEST_BYTES = 16384


def _fields(value, required):
    if not isinstance(value, dict) or set(value) != set(required):
        raise CameraError('invalid_camera_manifest')


def validate_manifest(value):
    fields = ('schema_version', 'id', 'driver', 'usb', 'validation')
    if isinstance(value, dict) and 'indi_driver' in value:
        if value.get('driver') != 'zwo_asi' or value['indi_driver'] != 'indi_asi_ccd':
            raise CameraError('invalid_camera_manifest')
        fields += ('indi_driver',)
    _fields(value, fields)
    if type(value['schema_version']) is not int or value['schema_version'] != SCHEMA_VERSION:
        raise CameraError('unsupported_camera_schema')
    if not isinstance(value['id'], str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', value['id']):
        raise CameraError('invalid_camera_id')
    if not isinstance(value['driver'], str) or value['driver'] not in DRIVER_IDS:
        raise CameraError('unknown_camera_driver')
    if value['validation'] not in ('unverified', 'simulation'):
        raise CameraError('invalid_camera_validation')
    if not isinstance(value['usb'], list) or len(value['usb']) > 32:
        raise CameraError('invalid_camera_usb')
    for rule in value['usb']:
        if not isinstance(rule, dict):
            raise CameraError('invalid_camera_usb')
        if set(rule) == {'vendor_id', 'product_id'}:
            if any(not integer(v, 0, 65535) for v in rule.values()):
                raise CameraError('invalid_camera_usb')
        elif set(rule) == {'class'}:
            if not integer(rule['class'], 1, 255):
                raise CameraError('invalid_camera_usb')
        else:
            raise CameraError('invalid_camera_usb')
    if 'indi_driver' in value and (not value['usb'] or any(rule.get('vendor_id') != 0x03c3 for rule in value['usb'])):
        raise CameraError('invalid_camera_manifest')
    if value['driver'] == 'simulator' and value['usb']:
        raise CameraError('simulator_cannot_match_usb')
    return copy.deepcopy(value)


def validate_registry(values):
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_MANIFESTS:
        raise CameraError('invalid_camera_registry')
    result, rules = {}, set()
    for value in values:
        item = validate_manifest(value)
        if item['id'] in result:
            raise CameraError('duplicate_camera_id')
        for rule in item['usb']:
            signature = tuple(sorted(rule.items()))
            if signature in rules:
                raise CameraError('ambiguous_camera_usb')
            rules.add(signature)
        result[item['id']] = item
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CameraError('duplicate_camera_field')
        result[key] = value
    return result


def load_registry(directory=None):
    """Read at most 32 small local tables; no dynamic imports or executable data."""
    path = Path(directory) if directory is not None else Path(__file__).parent / 'tables'
    if path.is_symlink():
        raise CameraError('invalid_camera_manifest_file')
    paths = []
    for entry in path.iterdir():
        if entry.suffix != '.json':
            continue
        if entry.is_symlink() or not entry.is_file():
            raise CameraError('invalid_camera_manifest_file')
        paths.append(entry)
        if len(paths) > MAX_MANIFESTS:
            raise CameraError('invalid_camera_registry')
    values = []
    for entry in sorted(paths):
        with entry.open('rb') as stream:
            data = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(data) > MAX_MANIFEST_BYTES:
            raise CameraError('oversized_camera_manifest')
        try:
            values.append(json.loads(data, object_pairs_hook=_unique_object))
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise CameraError('invalid_camera_manifest_json') from None
    return CameraRegistry(values)


class CameraRegistry:
    def __init__(self, values, factories=None):
        self._manifests = validate_registry(values)
        # These callables come only from reviewed platform code, never a manifest.
        self._factories = dict(factories or {})
        if any(key not in DRIVER_IDS or not callable(value) for key, value in self._factories.items()):
            raise CameraError('unknown_camera_driver')
        self._factories.setdefault('simulator', _simulator)
        self._factories.setdefault('uvc', _uvc)
        self._factories.setdefault('zwo_asi', _zwo_asi)

    def descriptors(self):
        return tuple(copy.deepcopy(list(self._manifests.values())))

    def available(self, ident):
        return self._descriptor(ident)['driver'] in self._factories

    def _descriptor(self, ident):
        if not isinstance(ident, str) or ident not in self._manifests:
            raise CameraError('unknown_camera_id')
        return self._manifests[ident]

    def create(self, ident, **options):
        driver = self._descriptor(ident)['driver']
        if driver not in self._factories:
            raise CameraError('camera_driver_unavailable')
        # Instantiation does not open the camera; the owning worker decides when.
        return self._factories[driver](**options)

    def match_usb(self, vendor_id, product_id, usb_class):
        if not integer(vendor_id, 0, 65535) or not integer(product_id, 0, 65535) or not integer(usb_class, 0, 255):
            raise CameraError('invalid_camera_usb')
        fallback = None
        for descriptor in self._manifests.values():
            for rule in descriptor['usb']:
                if set(rule) == {'vendor_id', 'product_id'}:
                    if rule['vendor_id'] == vendor_id and rule['product_id'] == product_id:
                        return copy.deepcopy(descriptor)
                elif rule['class'] == usb_class:
                    fallback = descriptor
        return copy.deepcopy(fallback)

    def match_device(self, device):
        """Exact IDs beat all class hints, including composite USB interfaces.

        A network interface on a composite device rules out a camera class hint;
        only an explicit reviewed vendor/product entry may override that rule.
        """
        vendor, product = device['vendor_id'], device['product_id']
        classes = [device['device_class'], *device['interface_classes']]
        exact = self.match_usb(vendor, product, 0)
        if exact:
            return exact
        if any(value in (2, 10, 224) for value in classes):
            return None
        for value in classes:
            match = self.match_usb(vendor, product, value)
            if match:
                return match
        return None


def _simulator(**options):
    from .simulator import SimulatedCamera
    return SimulatedCamera(**options)


def _uvc(**options):
    from .uvc import UVCCamera
    return UVCCamera(**options)


def _zwo_asi(**options):
    from .zwo_process import IsolatedASI
    return IsolatedASI(**options)


def manifest_schema():
    def obj(properties):
        return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}
    usb_id = {'type': 'integer', 'minimum': 0, 'maximum': 65535}
    schema = {'$schema': 'https://json-schema.org/draft/2020-12/schema', **obj({
        'schema_version': {'type': 'integer', 'const': SCHEMA_VERSION},
        'id': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,31}$'},
        'driver': {'enum': list(DRIVER_IDS)},
        'usb': {'type': 'array', 'maxItems': 32, 'uniqueItems': True, 'items': {'oneOf': [
            obj({'vendor_id': usb_id, 'product_id': usb_id}),
            obj({'class': {'type': 'integer', 'minimum': 1, 'maximum': 255}})]}},
        'validation': {'enum': ['unverified', 'simulation']}}),
        'allOf': [{'if': {'properties': {'driver': {'const': 'simulator'}}},
                   'then': {'properties': {'usb': {'maxItems': 0}}}}]}
    schema['properties']['indi_driver'] = {'const': 'indi_asi_ccd'}
    schema['allOf'].append({'if': {'required': ['indi_driver']}, 'then': {'properties': {
        'driver': {'const': 'zwo_asi'}, 'usb': {'minItems': 1, 'items': {
            'required': ['vendor_id'], 'properties': {'vendor_id': {'const': 0x03c3}}}}}}})
    return schema
