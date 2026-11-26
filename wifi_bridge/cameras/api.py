"""Bridge camera inventory/settings boundary, independent from Alpaca routing."""
import re

from ..api import ApiError
from .alpaca import AlpacaError
from .driver import CameraError
from .service import CameraService, CameraOwnerChanged, CameraProfileChanged, DEFAULT_SETTINGS, load_settings, save_settings


class CameraAPI:
    def __init__(self, api):
        self.api = api
        self._demo_installed = {}
        self._demo_deactivated = {}
        self._demo_internet = True

    def drivers_status(self):
        """Manufacturer drivers for this hardware, from the catalog; the Cameras page list.

        Each row also says what the first boot did with a driver ticked in Setup
        (setup_result, from setup-results.json) and how an installation started
        from this page is going (install). setup_other lists outcomes for
        manufacturers without a row here ('*' is the whole drop).
        """
        from . import drivers
        from .catalog import catalog, host_arch
        controller = self.api.controller
        value = catalog()
        supported = bool(controller.camera_supported)
        # The demonstration shows a Raspberry Pi 5 with no manufacturer driver installed.
        arch = 'arm64' if controller.demo else host_arch()
        folder = controller.store.path.parent / 'demo-camera-drivers' if controller.demo else drivers.DRIVERS
        from .vendor_process import ADAPTERS
        rows = drivers.rows(value, arch, supported, folder, adapters=ADAPTERS)
        results = drivers.setup_results(folder, trusted_only=not controller.demo)
        jobs = self._jobs()
        for row in rows:
            if controller.demo and row['id'] in self._demo_installed:
                row.update(state='accepted', version=self._demo_installed[row['id']]['version'], reason=None,
                           accepted_utc=self._demo_installed[row['id']]['accepted_utc'], origin='web', license_text=True)
            if controller.demo and row['id'] in self._demo_deactivated and row['id'] not in self._demo_installed:
                row.update(state='deactivated', version=self._demo_deactivated[row['id']]['version'], reason=None)
            row['setup_result'] = results.get(row['id'])
            job = jobs.get(row['id'])
            row['install'] = dict(job) if job else None
        listed = {row['id'] for row in rows}
        other = {key: code for key, code in results.items() if key not in listed}
        return {'supported': supported, 'catalog_version': value['catalog_version'], 'drivers': rows,
                'setup_other': other, 'config_revision': self.api.revision()}

    def _jobs(self):
        controller = self.api.controller
        if not hasattr(controller, 'driver_jobs'):
            controller.driver_jobs = {}
        return controller.driver_jobs

    def _install_build(self, vendor, arch):
        from . import catalog as driver_catalog
        entry = driver_catalog.entries().get(vendor)
        builds = [build for build in driver_catalog.builds_for(entry, arch)
                  if driver_catalog.installable(build)] if entry and arch else []
        return builds[0] if builds else None

    def driver_change(self, vendor, action, data, expected):
        """Owner acts on one manufacturer driver, from the web interface only."""
        from . import drivers
        from .catalog import entries, host_arch
        if vendor not in entries():
            raise ApiError(404, 'NOT_FOUND', 'Unknown API resource.')
        controller = self.api.controller
        controller.protection.precondition(expected, self.api.revision)
        if action == 'withdraw':
            self.api.fields(data, set())
            if controller.demo:
                # Like the bridge: only the acceptance goes, the files stay and can be reactivated.
                gone = self._demo_installed.pop(vendor, None)
                if gone:
                    self._demo_deactivated[vendor] = gone
            else:
                drivers.withdraw(drivers.acceptance_path(vendor, drivers.DRIVERS))
                service = getattr(controller, 'camera_service', None)
                if service is not None:
                    service.driver_changed(vendor)
            return self.drivers_status()
        if action == 'reactivate':
            return self._reactivate(vendor, data)
        if action == 'accept':
            raise ApiError(409, 'DRIVER_NOT_SUPPLIED', 'This driver is not supplied with the bridge. Install it instead.')
        # Install: the bridge downloads the archive from the manufacturer, then installs it like Setup.
        self.api.fields(data, {'version', 'accept_terms'})
        if not controller.camera_supported:
            raise ApiError(409, 'CAMERA_UNSUPPORTED', 'Not supported by this hardware.')
        build = self._install_build(vendor, 'arm64' if controller.demo else host_arch())
        if build is None:
            raise ApiError(409, 'DRIVER_NOT_INSTALLABLE', "The manufacturer's file has not been checked for this bridge yet.")
        if data.get('accept_terms') is not True or data.get('version') != build['version']:
            raise ApiError(409, 'CAMERA_DRIVER_CHANGED', 'The camera driver changed. Review its licence again.')
        jobs = self._jobs()
        if any(job['state'] in ('downloading', 'installing') for job in jobs.values()):
            raise ApiError(409, 'DRIVER_INSTALL_RUNNING', 'Another driver is being installed. Try again when it ends.')
        jobs[vendor] = {'state': 'downloading', 'received': 0, 'total': build['size'], 'error': None,
                        'version': build['version']}
        import threading
        target = self._demo_install if controller.demo else self._install_job
        threading.Thread(target=target, args=(vendor, build), name='driver-install-' + vendor, daemon=True).start()
        return self.drivers_status()

    def _reactivate(self, vendor, data):
        """Reactivate: the owner accepts the manufacturer's terms again for files still on this bridge.

        Nothing is downloaded. Files that are no longer exactly a catalog build are
        refused, and the line then offers Install.
        """
        from . import drivers
        from .catalog import host_arch
        controller = self.api.controller
        self.api.fields(data, {'version', 'accept_terms'})
        arch = 'arm64' if controller.demo else host_arch()
        row = next((item for item in self.drivers_status()['drivers'] if item['id'] == vendor), None)
        if row is None or row['state'] != 'deactivated':
            raise ApiError(409, 'DRIVER_NOT_DEACTIVATED', 'The driver files on this bridge have changed. Install the driver again.')
        if data.get('accept_terms') is not True or data.get('version') != row['version']:
            raise ApiError(409, 'CAMERA_DRIVER_CHANGED', 'The camera driver changed. Review its licence again.')
        if controller.demo:
            self._demo_installed[vendor] = dict(self._demo_deactivated.pop(vendor),
                                                accepted_utc=controller.event_store.clock.read()['corrected_utc'])
            return self.drivers_status()
        try:
            drivers.reactivate(vendor, arch, controller.event_store.clock.read(), 'web', drivers.DRIVERS)
        except ValueError:
            raise ApiError(409, 'DRIVER_NOT_DEACTIVATED', 'The driver files on this bridge have changed. Install the driver again.') from None
        service = getattr(controller, 'camera_service', None)
        if service is not None:
            service.driver_changed(vendor)
        return self.drivers_status()

    def _install_job(self, vendor, build):
        """Download then install, outside the controller lock; the job record says how it went."""
        from . import drivers
        from .driver_download import DownloadError, fetch
        controller = self.api.controller
        job = self._jobs()[vendor]
        target = drivers.DRIVERS / ('.download-' + drivers.folder_name(vendor))
        try:
            drivers.DRIVERS.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            fetch(build, target, progress=lambda received, total: job.update(received=received))
            job['state'] = 'installing'
            drivers.install_archive(vendor, build, target, controller.event_store.clock.read(), 'web',
                                    drivers=drivers.DRIVERS)
            with controller.lock:
                service = getattr(controller, 'camera_service', None)
                if service is not None:
                    service.driver_changed(vendor)
            job.update(state='done', error=None)
        except DownloadError as error:
            job.update(state='failed', error=error.code)
        except ValueError as error:
            code = str(error)
            job.update(state='failed', error=code if re.fullmatch('[a-z_]{3,40}', code) else 'install_failed')
        except Exception as error:
            import logging
            logging.getLogger(__name__).warning('Camera driver installation failed: %s', type(error).__name__)
            job.update(state='failed', error='install_failed')
        finally:
            try:
                target.unlink()
            except OSError:
                pass

    def _demo_install(self, vendor, build):
        """Demonstration: no network; the toggle on the Cameras page says whether Internet is there."""
        import time
        job = self._jobs()[vendor]
        if not self._demo_internet:
            time.sleep(0.3)
            job.update(state='failed', error='offline')
            return
        for step in range(1, 5):
            time.sleep(0.25)
            job['received'] = build['size'] * step // 4
        job['state'] = 'installing'
        time.sleep(0.25)
        self._demo_deactivated.pop(vendor, None)
        self._demo_installed[vendor] = {'version': build['version'],
                                        'accepted_utc': self.api.controller.event_store.clock.read()['corrected_utc']}
        job.update(state='done', error=None)

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

    def handle(self, method, path, query, data, expected, local_address=None, browser=False):
        if query:
            raise ApiError(400, 'INVALID_FIELDS', 'Unknown or read-only fields in request.')
        with self.api.controller.lock:
            try:
                # Licence acceptance is an owner act in the web interface, not a native-client route.
                if browser and path == '/cameras/drivers' and method == 'GET':
                    return self.drivers_status()
                match = re.fullmatch(r'/cameras/drivers/([a-z][a-z0-9_]{0,31})/(accept|withdraw|install|reactivate)', path)
                if browser and match and method == 'POST':
                    return self.driver_change(match[1], match[2], data, expected)
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
                    self.api.fields(data, {'plugged', 'internet'})
                    if not data or any(type(value) is not bool for value in data.values()):
                        raise ValueError('invalid_camera_settings')
                    if 'internet' in data:
                        self._demo_internet = data['internet']
                    if 'plugged' in data:
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
