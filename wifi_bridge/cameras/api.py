"""Bridge camera inventory/settings boundary, independent from Alpaca routing."""
import re

from ..api import ApiError
from .alpaca import AlpacaError
from .driver import CameraError
from .service import CameraService, CameraOwnerChanged, CameraProfileChanged, DEFAULT_SETTINGS, load_settings, save_settings


class CameraAPI:
    def __init__(self, api):
        self.api = api

    def service(self):
        controller = self.api.controller
        with controller.lock:
            if not controller.camera_supported:
                return None
            service = getattr(controller, 'camera_service', None)
            if service is None:
                devices = controller.status().get('devices', [])
                present = [device for device in devices if not device.get('missing')]
                only_24 = bool(present) and all(device.get('bands') == ['2.4'] for device in present)
                from .discovery import pi5_gphoto_enabled
                service = CameraService(gphoto_enabled=pi5_gphoto_enabled(), indi_platform=getattr(controller, 'indi_platform', None),
                                        path=controller.store.path.parent / 'cameras.json', default_bin=2 if only_24 else 1)
                controller.camera_service = service
            return service

    def settings(self):
        service = getattr(self.api.controller, 'camera_service', None)
        return service.get_settings() if service else load_settings(self.api.controller.store.path.parent / 'cameras.json')

    def status(self, local_address=None):
        self.service()
        return {'settings': self.settings(), 'indi': self.api.indi_status(local_address),
                'config_revision': self.api.revision()}

    def handle(self, method, path, query, data, expected, local_address=None):
        if query:
            raise ApiError(400, 'INVALID_FIELDS', 'Unknown or read-only fields in request.')
        with self.api.controller.lock:
            try:
                match = re.fullmatch(r'/cameras/([a-z][a-z0-9_-]{0,63})/profile', path)
                if match and method in ('GET', 'PATCH'):
                    service = self.service()
                    if not service:
                        raise KeyError(match[1])
                    if method == 'GET':
                        return service.camera_profile(match[1])
                    self.api.controller.protection.precondition(expected, self.api.revision)
                    self.api.fields(data, {'attachment_generation', 'values'})
                    if not isinstance(data.get('attachment_generation'), str) or not re.fullmatch('[0-9a-f]{32}', data['attachment_generation']) or not isinstance(data.get('values'), dict):
                        raise ValueError('invalid_profile')
                    return service.camera_profile(match[1], data['attachment_generation'], data['values'])
                match = re.fullmatch(r'/cameras/([a-z][a-z0-9_-]{0,63})/release-owner', path)
                if match and method == 'POST':
                    return self.release_owner(match[1], data, expected)
                if path == '/cameras/demo' and method == 'POST':
                    if not self.api.controller.demo:
                        raise ApiError(404, 'NOT_FOUND', 'Unknown API resource.')
                    self.api.controller.protection.precondition(expected, self.api.revision)
                    self.api.fields(data, {'plugged'})
                    if type(data.get('plugged')) is not bool:
                        raise ValueError('invalid_camera_settings')
                    self.service().demo_plug(data['plugged'])
                    return {'ok': True}
                if path == '/cameras' and method == 'GET':
                    service = self.service()
                    return self.api.camera_inventory(local_address) if service else []
                if path == '/cameras/settings' and method == 'GET':
                    return self.status(local_address)
                if path == '/cameras/settings' and method == 'PATCH':
                    service = self.service()
                    self.api.controller.protection.precondition(expected, self.api.revision)
                    self.api.fields(data, set(DEFAULT_SETTINGS))
                    if service:
                        service.update_settings(data)
                    else:
                        if not data:
                            raise ValueError('invalid_camera_settings')
                        save_settings(self.api.controller.store.path.parent / 'cameras.json', dict(self.settings(), **data))
                    return self.status(local_address)
            except KeyError:
                raise ApiError(404, 'NOT_FOUND', 'Unknown API resource.') from None
            except CameraProfileChanged:
                raise ApiError(409, 'CAMERA_PROFILE_CHANGED', 'Camera or profile changed or is in use. Refresh before trying again.') from None
            except CameraOwnerChanged:
                raise ApiError(409, 'CAMERA_BUSY', 'Disconnect the Alpaca camera before enabling INDI.') from None
            except ValueError:
                raise ApiError(400, 'INVALID_CAMERA_SETTINGS', 'Invalid camera settings.') from None
            except (AlpacaError, OSError):
                raise ApiError(503, 'CAMERA_UNAVAILABLE', 'Camera service is unavailable.') from None
        raise ApiError(404, 'NOT_FOUND', 'Unknown API resource.')

    def release_owner(self, ident, data, expected):
        """Consent applies to exactly one owner generation, checked on its worker."""
        self.api.fields(data, {'confirm_release', 'owner_generation'})
        if data.get('confirm_release') is not True or not isinstance(data.get('owner_generation'), str) or not re.fullmatch('[0-9a-f]{32}', data['owner_generation']):
            raise ApiError(400, 'INVALID_FIELDS', 'Unknown or read-only fields in request.')
        with self.api.controller.lock:
            self.api.controller.protection.precondition(expected, self.api.revision)
            try:
                service = self.service()
                if not service:
                    raise KeyError(ident)
                service.release(ident, data['owner_generation'])
            except CameraOwnerChanged:
                raise ApiError(409, 'CAMERA_OWNER_CHANGED', 'Camera owner changed. Review the current owner before trying again.') from None
            except (AlpacaError, CameraError, OSError):
                raise ApiError(503, 'CAMERA_UNAVAILABLE', 'Camera service is unavailable.') from None
            except KeyError:
                raise ApiError(404, 'NOT_FOUND', 'Unknown API resource.') from None
            return {'ok': True}
