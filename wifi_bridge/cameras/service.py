"""Single-worker camera service; injected USB discovery owns reconciliation."""
from collections import deque
import hashlib
from concurrent.futures import CancelledError, Future, TimeoutError as FutureTimeout
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
import uuid

from .alpaca import AlpacaError, Camera
from .calibration import CameraCalibration
from .driver import CameraError
from .indi import IndiSupervisor, PORT as INDI_PORT
from .usb import MAX_ENTRIES, validate_device

DEFAULT_SETTINGS = {'serve': True, 'discover': True, 'indi': False, 'port': 11111, 'allowed_subnets': []}


class CameraProfileChanged(ValueError):
    """Profile state or attachment has changed."""


class CameraOwnerChanged(ValueError):
    """Captured consent no longer identifies the active Alpaca owner."""


def load_settings(path):
    path = Path(path)
    try:
        with path.open('rb') as stream:
            raw = stream.read(8193)
    except FileNotFoundError:
        return settings(DEFAULT_SETTINGS)
    if len(raw) > 8192:
        raise ValueError('invalid_camera_settings')
    value = json.loads(raw)
    if isinstance(value, dict):
        value.setdefault('discover', True)
        value.setdefault('indi', False)
    return settings(value)


def save_settings(path, value):
    value, path = settings(value), Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def settings(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_SETTINGS):
        raise ValueError('invalid_camera_settings')
    if type(value['indi']) is not bool or type(value['discover']) is not bool or type(value['serve']) is not bool or type(value['port']) is not int or not 1024 <= value['port'] <= 65535:
        raise ValueError('invalid_camera_settings')
    if value['indi'] and value['port'] == INDI_PORT:
        raise ValueError('invalid_camera_settings')
    nets = value['allowed_subnets']
    if not isinstance(nets, list) or len(nets) > 16:
        raise ValueError('invalid_camera_settings')
    normalized = []
    for net in nets:
        if not isinstance(net, str) or len(net) > 49:
            raise ValueError('invalid_camera_settings')
        parsed = ipaddress.ip_network(net, strict=True)
        if parsed.version != 4 or parsed.prefixlen == 0:
            raise ValueError('invalid_camera_settings')
        normalized.append(str(parsed))
    if len(set(normalized)) != len(normalized):
        raise ValueError('invalid_camera_settings')
    return dict(serve=value['serve'], indi=value['indi'], discover=value['discover'], port=value['port'], allowed_subnets=normalized)


class CameraService:
    """Factories/instances are trusted code supplied by later platform discovery.

    All driver calls, including info/open/close/state, run on this one worker.
    Pending work is bounded. No service starts merely by importing this module.
    """
    def __init__(self, *, path=None, clock=time.monotonic, request_timeout=1.0, shutdown_timeout=0.5, default_bin=1, indi_platform=None, gphoto_enabled=False):
        self.gphoto_enabled = gphoto_enabled
        self.default_bin = default_bin
        self.indi = IndiSupervisor(indi_platform, clock)
        self._indi_target = None
        self._indi_public = ('waiting', frozenset(), 0)
        self._indi_paths = frozenset()
        self._retiring_asi = []
        self._retiring_probes = []
        self.clock, self.path = clock, Path(path) if path else None
        self._calibration = CameraCalibration(self.path.with_name('camera-calibration.json')) if self.path else None
        self.request_timeout, self.shutdown_timeout = request_timeout, shutdown_timeout
        self._count, self._published_port = 0, None
        self._published_addresses = frozenset()
        self._stop = threading.Event()
        self._stalled = threading.Event()
        self.identity = uuid.uuid4()
        if self.path:
            identity_path = self.path.with_name('camera-identity')
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                fd = os.open(identity_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                with identity_path.open('r') as stream:
                    self.identity = uuid.UUID(stream.read(37))
            else:
                with os.fdopen(fd, 'w') as stream:
                    stream.write(str(self.identity))
                    stream.flush()
                    os.fsync(stream.fileno())
        self._settings = load_settings(self.path) if self.path else settings(DEFAULT_SETTINGS)
        self._cameras = {}
        self._numbers, self._next_number = {}, 0
        self._usb, self._usb_losses = {}, {}
        self._queue = queue.Queue(maxsize=16)
        self._closing = False
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, name='camera-service', daemon=True)
        self._worker.start()

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    future, fn, args = self._queue.get(timeout=0.25)
                except queue.Empty:
                    for camera in self._cameras.values():
                        if self._stop.is_set():
                            break
                        try:
                            camera.state()
                        except Exception:
                            pass
                    continue
                if self._stop.is_set():
                    future.cancel()
                    break
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(fn(*args))
                except Exception as error:
                    future.set_exception(error)
                finally:
                    self._stalled.clear()
        finally:
            self.indi.stop()
            while True:
                try:
                    self._queue.get_nowait()[0].cancel()
                except queue.Empty:
                    break
            for camera in self._cameras.values():
                try:
                    camera.release()
                except Exception:
                    pass

    def _call(self, fn, *args):
        with self._lock:
            if self._closing or self._stalled.is_set():
                raise AlpacaError(0x501)
            future = Future()
            try:
                self._queue.put_nowait((future, fn, args))
            except queue.Full:
                raise AlpacaError(0x501) from None
        try:
            return future.result(timeout=self.request_timeout)
        except FutureTimeout:
            if not future.cancel():
                self._stalled.set()
                future.add_done_callback(lambda completed: self._stalled.clear())
            raise AlpacaError(0x501) from None
        except CancelledError:
            raise AlpacaError(0x501) from None

    def close(self):
        with self._lock:
            self._closing = True
            self._published_port = None
            self._stop.set()
        for item in list(self._usb.values()):
            if item.get('profile_job'):
                item['profile_job']['cancel'].set()
        # A stalled SDK cannot be interrupted safely. Its sole daemon worker
        # closes devices if it returns; never replace it or block shutdown.
        self._worker.join(timeout=self.shutdown_timeout)

    def set_listening(self, port, addresses=()):
        """Runtime publishes only a successfully bound listener, None on loss."""
        with self._lock:
            self._published_port = port
            self._published_addresses = frozenset(addresses) if port is not None else frozenset()

    def listening_port(self, address=None):
        with self._lock:
            if address is not None and address not in self._published_addresses:
                return None
            return self._published_port if self._settings['serve'] and not self._closing else None

    def add(self, ident, driver_name, driver):
        def register():
            if (not isinstance(ident, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', ident)
                    or ident in self._cameras or ident in self._usb or (ident not in self._numbers and self._next_number >= 8)
                    or len(set(self._cameras) | set(self._usb)) >= 8):
                raise ValueError('invalid_camera_id')
            if not isinstance(driver_name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', driver_name):
                raise ValueError('invalid_camera_driver')
            self._cameras[ident] = Camera(ident, driver_name, driver, self.clock, self.default_bin)
            if ident not in self._numbers:
                self._numbers[ident] = self._next_number
                self._next_number += 1
            with self._lock:
                self._count = len(self._cameras)
        self._call(register)

    def reconcile_usb(self, devices, registry):
        """Run factories/info/close on the existing worker; return payload-free events.

        None is uncertain inventory, so it cannot remove working devices. Attachment
        IDs describe a port+model, never a durable camera identity or serial number.
        Missing drivers still appear in inventory, but never become Alpaca devices.
        """
        if devices is None:
            def uncertain():
                self._indi_target = None
                if self.indi.child is not None:
                    self._stop_indi()
                return []
            return self._call(uncertain)
        if not isinstance(devices, list) or len(devices) > MAX_ENTRIES:
            raise ValueError('invalid_usb_metadata')
        devices = [validate_device(device) for device in devices]

        def reconcile():
            if not self._settings['serve']:
                return []
            now, events = self.clock(), []
            matched = {}
            from .gphoto_platform import attachment_token, ptp
            ptp_devices = [device for device in devices if ptp(device)] if self.gphoto_enabled else []
            for device in devices:
                descriptor = registry.match_device(device)
                if descriptor is None and len(ptp_devices) == 1 and device == ptp_devices[0]:
                    descriptor = {'id': 'gphoto', 'driver': 'gphoto'}
                if descriptor:
                    key = device['port_path'] + ':' + str(device['vendor_id']) + ':' + str(device['product_id'])
                    ident = 'usb-' + hashlib.sha256(key.encode('ascii')).hexdigest()[:24]
                    matched[ident] = (dict(device), descriptor)
            # The native ASI driver enumerates its whole family. An unknown
            # same-vendor peer also makes ownership ambiguous.
            candidates = [(ident, desc['indi_driver'], device) for ident, (device, desc) in matched.items()
                          if 'indi_driver' in desc]
            target = candidates[0] if len(candidates) == 1 and sum(d['vendor_id'] == 0x03c3 for d in devices) == 1 else None
            if self.indi.child is not None and target != self._indi_target:
                self._indi_target = None
                self._stop_indi()
            self._indi_target = target
            self._usb_losses = {key: deque((stamp for stamp in values if now - stamp <= 600), maxlen=1201)
                                for key, values in self._usb_losses.items() if values and now - values[-1] <= 600}
            for ident in list(self._usb):
                if ident in matched:
                    previous = self._usb[ident]
                    if (previous['driver'] != 'gphoto' or
                            (previous['usb'] == matched[ident][0] and
                             attachment_token(matched[ident][0]) == previous.get('profile_usb_token'))):
                        continue
                removed = self._usb.pop(ident)
                job = removed.get('profile_job')
                if job:
                    job['cancel'].set()
                    self._retiring_probes.append(job['thread'])
                camera = self._cameras.pop(ident, None)
                if camera:
                    try:
                        camera.release()
                    except Exception:
                        pass  # An unplugged driver may fail its final close.
                    self._retain_unreaped(camera.driver)
                losses = self._usb_losses.setdefault(ident, deque(maxlen=1201))
                losses.append(now)
                events.append({'type': 'camera_removed', 'camera_id': ident})
                if len(losses) == 3:
                    events.append({'type': 'camera_power_unstable', 'camera_id': ident})
            # Bounded history cannot grow with a stream of distinct USB models.
            while len(self._usb_losses) > 256:
                self._usb_losses.pop(next(iter(self._usb_losses)))
            for ident, (device, descriptor) in matched.items():
                if ident not in self._usb:
                    if len(set(self._usb) | set(self._cameras)) >= 8:
                        continue
                    self._usb[ident] = {'id': ident, 'name': descriptor['id'], 'driver': descriptor['driver'],
                                        'indi_driver': descriptor.get('indi_driver'), 'usb': device,
                                        'discovery': {'state': 'detected'}}
                    events.append({'type': 'camera_detected', 'camera_id': ident})
                    if descriptor['driver'] == 'gphoto':
                        self._start_profile_probe(self._usb[ident], device)
                item = self._usb[ident]
                if descriptor['driver'] == 'gphoto':
                    if not item.get('profile_job') and not item.get('profile_probe_started'):
                        self._start_profile_probe(item, device)
                    self._finish_profile_probe(item)
                    if not item.get('profile_job'):
                        self._activate_profile(item)
                    continue
                if item['indi_driver'] and self._settings['indi']:
                    item['discovery']['state'] = 'preparing'
                    continue
                if item['discovery']['state'] != 'detected':
                    continue
                item['discovery']['state'] = 'preparing'
                driver = None
                try:
                    if ident not in self._numbers and self._next_number >= 8:
                        raise CameraError('camera_unsupported')
                    if descriptor['driver'] in ('zwo_asi', 'gphoto') and self._asi_retiring():
                        item['discovery']['state'] = 'detected'
                        continue
                    options = {'device': device}
                    if descriptor['driver'] == 'uvc' and self._calibration is not None:
                        options['pixel_um'] = self._calibration.pitch(ident)
                    driver = registry.create(descriptor['id'], **options)
                    self._cameras[ident] = Camera(ident, descriptor['driver'], driver, self.clock, self.default_bin)
                except Exception as error:
                    if driver is not None:
                        try:
                            driver.close()
                        except Exception:
                            pass
                        self._retain_unreaped(driver)
                    item['discovery']['state'] = ('driver_missing' if isinstance(error, CameraError)
                                                   and error.code == 'camera_driver_unavailable' else 'unsupported')
                else:
                    if ident not in self._numbers:
                        self._numbers[ident] = self._next_number
                        self._next_number += 1
                    item['discovery']['state'] = 'ready'
            for ident, item in self._usb.items():
                losses = self._usb_losses.get(ident, ())
                item['discovery'].update(disconnects=len(losses), power_unstable=len(losses) >= 3)
            with self._lock:
                self._count = len(self._cameras)
            return events
        return self._call(reconcile)

    def driver_changed(self, driver):
        """A driver became available or was withdrawn by the owner.

        Attached cameras of that driver return to detection. Served ones are
        released exactly as on unplug; the next reconcile retries the driver.
        """
        def change():
            for ident, item in self._usb.items():
                if item['driver'] != driver:
                    continue
                camera = self._cameras.pop(ident, None)
                if camera:
                    try:
                        camera.release()
                    except Exception:
                        pass
                    self._retain_unreaped(camera.driver)
                item['discovery']['state'] = 'detected'
            with self._lock:
                self._count = len(self._cameras)
        return self._call(change)

    def _start_profile_probe(self, item, device):
        from .gphoto_platform import attachment_token, profile, probe
        item['profile_generation'] = uuid.uuid4().hex
        item['profile_usb_token'] = attachment_token(device)
        item['profile_detected'], item['profile_overrides'], item['profile_values'] = {}, {}, {}
        if self.path:
            try:
                item['profile_overrides'] = profile(self.path.with_name('camera-gphoto.json'), item['id'])
                item['profile_values'] = dict(item['profile_overrides'])
            except (CameraError, ValueError):
                pass
        # Never start a second isolated driver while the previous one is retiring.
        self._retiring_probes = [t for t in self._retiring_probes if t.is_alive()]
        if item['profile_usb_token'] is None or self._retiring_probes or self._asi_retiring():
            return
        job = {'cancel': threading.Event(), 'result': {}}
        def run():
            job['result'] = probe(device, job['cancel'])
        job['thread'] = threading.Thread(target=run, name='gphoto-probe', daemon=True)
        item['profile_job'] = job
        item['profile_probe_started'] = True
        job['thread'].start()

    def _finish_profile_probe(self, item):
        if not self._settings['serve']:
            return
        job = item.get('profile_job')
        if job and not job['thread'].is_alive():
            item.pop('profile_job')
            item['profile_detected'] = job['result']
            item['profile_values'] = dict(job['result'], **item['profile_overrides'])
            item['discovery']['state'] = 'detected'
            self._activate_profile(item)

    def _activate_profile(self, item):
        from .gphoto_platform import PROFILE_FIELDS, attachment_token, launch
        from .gphoto import GPhotoCamera
        values = item['profile_values']
        if not self._settings['serve']:
            return
        if not item.get('profile_probe_started'):
            item['discovery']['state'] = 'unsupported'
            return
        if item['profile_usb_token'] is None or attachment_token(item['usb']) != item['profile_usb_token']:
            item['discovery']['state'] = 'unsupported'
            return
        if set(values) != set(PROFILE_FIELDS) or item['id'] in self._cameras or item.get('profile_job'):
            if item['id'] not in self._cameras and not item.get('profile_job'):
                item['discovery']['state'] = 'unsupported'
            return
        if self._asi_retiring() or any(t.is_alive() for t in self._retiring_probes):
            return
        if item['id'] not in self._numbers and self._next_number >= 8:
            return
        try:
            driver = GPhotoCamera(profile=values, launch=lambda device=item['usb']: launch(device))
            camera = Camera(item['id'], 'gphoto', driver, self.clock, self.default_bin)
        except (CameraError, ValueError, KeyError, TypeError):
            item['discovery']['state'] = 'unsupported'
            return
        self._cameras[item['id']] = camera
        if item['id'] not in self._numbers:
            self._numbers[item['id']] = self._next_number
            self._next_number += 1
        item['discovery']['state'] = 'ready'
        with self._lock:
            self._count = len(self._cameras)

    def _profile_view(self, item):
        from .gphoto_platform import PROFILE_FIELDS
        values = dict(item['profile_values'])
        return {'attachment_generation': item['profile_generation'], 'detected': dict(item['profile_detected']),
                'values': values, 'missing': [k for k in PROFILE_FIELDS if k not in values],
                'ready': item['id'] in self._cameras, 'probing': bool(item.get('profile_job'))}

    def camera_profile(self, ident, generation=None, values=None):
        def access():
            from .gphoto_platform import attachment_token, save_profile, validate_partial
            item = self._usb.get(ident)
            if not item or item['driver'] != 'gphoto':
                raise KeyError(ident)
            self._finish_profile_probe(item)
            if values is not None:
                if (not self._settings['serve'] or not item.get('profile_probe_started') or
                        generation != item['profile_generation'] or item.get('profile_job') or
                        item['profile_usb_token'] is None or attachment_token(item['usb']) != item['profile_usb_token']):
                    raise CameraProfileChanged('profile_changed')
                camera = self._cameras.get(ident)
                if camera and (camera.connected or camera.owner or not camera.driver.handoff_ready()):
                    raise CameraProfileChanged('profile_busy')
                if not values:
                    raise ValueError('invalid_profile')
                overrides = validate_partial(dict(item['profile_overrides'], **validate_partial(values)))
                updated = validate_partial(dict(item['profile_detected'], **overrides))
                if not self.path:
                    raise ValueError('invalid_profile')
                save_profile(self.path.with_name('camera-gphoto.json'), ident, overrides)
                if camera:
                    self._cameras.pop(ident)
                    with self._lock:
                        self._count = len(self._cameras)
                    camera.release()
                item['profile_overrides'] = overrides
                item['profile_values'] = updated
                item['profile_generation'] = uuid.uuid4().hex
                self._activate_profile(item)
            return self._profile_view(item)
        return self._call(access)

    def _asi_retiring(self):
        self._retiring_asi = [driver for driver in self._retiring_asi if not driver.handoff_ready()]
        return bool(self._retiring_asi)

    def _retain_unreaped(self, driver):
        check = getattr(driver, 'handoff_ready', None)
        if check and not check():
            self._retiring_asi.append(driver)

    def _publish_indi(self):
        with self._lock:
            self._indi_paths = frozenset(self.indi.plan[1]) if self.indi.plan else frozenset()
            self._indi_public = (self.indi.state, self.indi.addresses, self.indi.last_reply + 3)

    def _stop_indi(self):
        stopped = self.indi.stop()
        self._publish_indi()
        if not stopped:
            raise OSError('indi_stopping')

    def indi_status(self, address=None):
        with self._lock:
            state, addresses, expires = self._indi_public
            if self._closing or not (self._settings['serve'] and self._settings['indi']):
                state = 'off'
            elif self.indi.platform is None:
                state = 'unavailable'
            elif state == 'off':
                state = 'waiting'
            elif state == 'ready' and (self.clock() >= expires or address is not None and address not in addresses):
                state = 'unavailable'
            return {'port': INDI_PORT, 'state': state}

    def advertises_indi(self, paths):
        # Avahi applies a single TXT record across all selected interfaces.
        with self._lock:
            state, _, expires = self._indi_public
            return (not self._closing and self._settings['serve'] and self._settings['indi'] and self._settings['discover'] and
                    state == 'ready' and self.clock() < expires and bool(paths) and self._indi_paths == paths)

    def sync_indi(self, paths):
        def sync():
            cfg = self._settings
            target = self._indi_target
            if target and (target[0] not in self._usb or target[0] in self._cameras or self._asi_retiring()):
                target = None
            self.indi.sync(cfg['serve'] and cfg['indi'], target, paths,
                           [ipaddress.IPv4Network(net) for net in cfg['allowed_subnets']])
            self._publish_indi()
        return self._call(sync)

    def get_settings(self):
        with self._lock:
            return settings(self._settings)

    def update_settings(self, patch):
        def update():
            if not isinstance(patch, dict) or not patch or set(patch) - set(DEFAULT_SETTINGS):
                raise ValueError('invalid_camera_settings')
            value = settings(dict(self._settings, **patch))
            switching = value['indi'] != self._settings['indi']
            selected = [ident for ident, row in self._usb.items() if row['indi_driver']]
            if switching and value['indi']:
                for ident in selected:
                    camera = self._cameras.get(ident)
                    if camera and (camera.connected or camera.owner or not getattr(camera.driver, 'handoff_ready', lambda: False)()):
                        raise CameraOwnerChanged('camera_owner_changed')
            if self.indi.child is not None and value != self._settings:
                self._stop_indi()
            if self.path:
                save_settings(self.path, value)
            if switching or value['serve'] != self._settings['serve']:
                self._indi_target = None
            if switching:
                for ident in selected:
                    self._cameras.pop(ident, None)
                    self._usb[ident]['discovery']['state'] = 'preparing' if value['indi'] else 'detected'
            with self._lock:
                self._settings = value
                self._count = len(self._cameras)
                if not value['serve']:
                    self._published_port = None
            if not value['serve']:
                for ident, item in self._usb.items():
                    if item['driver'] != 'gphoto':
                        continue
                    job = item.pop('profile_job', None)
                    if job:
                        job['cancel'].set()
                        self._retiring_probes.append(job['thread'])
                    item['profile_probe_started'] = False
                    item['profile_generation'] = uuid.uuid4().hex
                    item['profile_detected'] = {}
                    item['profile_values'] = dict(item['profile_overrides'])
                    item['discovery']['state'] = 'detected'
                    camera = self._cameras.pop(ident, None)
                    if camera:
                        try:
                            camera.release()
                        except Exception:
                            pass  # A disconnected device may fail its final close.
                        self._retain_unreaped(camera.driver)
                with self._lock:
                    self._count = len(self._cameras)
                for camera in self._cameras.values():
                    camera.release()
            return settings(value)
        return self._call(update)

    def enabled(self):
        with self._lock:
            return not self._closing and self._settings['serve'] and self._count > 0

    def release(self, ident, generation=None):
        def release():
            camera = self._cameras[ident]
            camera.state()
            if generation is not None and (not camera.owner or camera.generation != generation):
                raise CameraOwnerChanged('camera_owner_changed')
            camera.release()
        self._call(release)

    def demo_plug(self, plugged):
        """Explicit simulator only; never enumerate the host's devices."""
        from .simulator import SimulatedCamera
        def unplug():
            camera = self._cameras.pop('demo-camera', None)
            if camera:
                camera.release()
            with self._lock:
                self._count = len(self._cameras)
        if plugged:
            if not any(row['id'] == 'demo-camera' for row in self.inventory()):
                self.add('demo-camera', 'simulator', SimulatedCamera())
        else:
            self._call(unplug)


    def inventory(self, local_address=None):
        def read():
            port = self.listening_port(local_address)
            for row in self._usb.values():
                if row['driver'] == 'gphoto':
                    self._finish_profile_probe(row)
            result = []
            for camera in self._cameras.values():
                n = self._numbers[camera.ident]
                state = camera.state()
                item = dict(id=camera.ident, name=camera.info['name'], driver=camera.driver_name,
                            state=state, alpaca=({'port': port, 'device_number': n} if port is not None else None),
                            last_image={'bytes': len(camera.frame.data) if camera.frame else 0,
                                        'seconds': camera.last_seconds if camera.frame else 0.0})
                if camera.owner:
                    item['owner'] = dict(camera.owner, generation=camera.generation)
                if camera.owner and camera.refused and self.clock() - camera.refused['since'] < 120:
                    item['refused'] = dict(camera.refused)
                item['sensor'] = {key: camera.info[key] for key in ('width', 'height', 'bits', 'max_bin')}
                item['defaults'] = {'bin': camera.default_bin, 'gain': camera.info['gain_min']}
                item['current'] = {'bin': camera.values['binx'], 'exposure_seconds': camera.last_seconds}
                if camera.ident in self._usb:
                    discovered = self._usb[camera.ident]
                    item.update(usb=validate_device(discovered['usb']), discovery=dict(discovered['discovery']))
                    if discovered['driver'] == 'gphoto':
                        item['profile'] = self._profile_view(discovered)
                result.append(item)
            for ident, discovered in self._usb.items():
                if ident not in self._cameras:
                    indi = self.indi_status(local_address) if discovered['indi_driver'] and self._settings['indi'] else None
                    if indi and (not self._indi_target or self._indi_target[0] != ident) and indi['state'] == 'ready':
                        indi['state'] = 'waiting'
                    discovery = dict(discovered['discovery'])
                    if indi and indi['state'] == 'ready':
                        discovery['state'] = 'ready'
                    result.append(dict(id=ident, name=discovered['name'], driver=discovered['driver'],
                                       **({'profile': self._profile_view(discovered)} if discovered['driver'] == 'gphoto' else {}),
                                       state='idle' if indi else 'error', **({'indi': indi} if indi else {}),
                                       alpaca=None, last_image={'bytes': 0, 'seconds': 0.0},
                                       usb=validate_device(discovered['usb']), discovery=discovery))
            return result
        return self._call(read)

    def configured_devices(self):
        return self._call(lambda: [dict(DeviceName=camera.info['name'], DeviceType='Camera',
                                       DeviceNumber=self._numbers[ident],
                                       UniqueID=str(uuid.uuid5(self.identity, ident)))
                                   for ident, camera in self._cameras.items()])

    def execute(self, device, method, member, params, client, address):
        def call():
            if not self._settings['serve']:
                raise AlpacaError(0x501)
            ident = next((key for key, number in self._numbers.items() if number == device), None)
            if ident is None or ident not in self._cameras:
                raise AlpacaError(0x401)
            camera = self._cameras[ident]
            try:
                return camera.call(method, member, params, client, address)
            except CameraError as error:
                code = 0x407 if error.code == 'not_open' else 0x40B if error.code == 'busy' else 0x401
                raise AlpacaError(code) from None
            except AlpacaError:
                raise
            except Exception:
                raise AlpacaError(0x501) from None
        return self._call(call)
