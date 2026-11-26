"""Explicitly gated USB discovery coordinator, independent of HTTP and controller."""
import threading
import time
import platform
import stat
from pathlib import Path
from collections import deque

from .alpaca import AlpacaError
from .usb import SysfsUSB, UEventMonitor


def camera_tier_allowed(system, model='', *, indi=False, pi5_test=False):
    """Pure platform decision; never inspect a device while deciding eligibility.

    Pi Zero remains a connections/telemetry appliance. Pi camera service is
    limited to the approved Pi 3/4 tier; other Linux implementations opt in.
    """
    if system != 'Linux':
        return False
    if model.startswith('Raspberry Pi 5 '):
        return pi5_test and not indi
    if model.startswith('Raspberry Pi'):
        return model.startswith('Raspberry Pi 4 ') if indi else model.startswith(('Raspberry Pi 3 ', 'Raspberry Pi 4 '))
    return True


def pi5_test_enabled(marker=Path('/etc/wifi-bridge/pi5-asi220-test')):
    # This opt-in belongs to the private image, never to a client or state setting.
    try:
        for path in (*reversed(marker.parents), marker):
            info = path.lstat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                return False
            if path == marker:
                if not stat.S_ISREG(info.st_mode):
                    return False
            elif not stat.S_ISDIR(info.st_mode):
                return False
        return True
    except OSError:
        return False


def platform_supported(demo=False, *, indi=False):
    """Decide the tier before any service worker, listener or USB monitor exists."""
    if demo:
        return not indi  # Demo uses only explicit in-process simulator drivers.
    system = platform.system()
    model = ''
    if system == 'Linux':
        try:
            with Path('/proc/device-tree/model').open('rb') as stream:
                raw = stream.read(257)
            if len(raw) > 256:
                return False
            model = raw.rstrip(b'\0\n').decode('ascii')
        except FileNotFoundError:
            pass  # Other Linux implementations have no Raspberry device tree.
        except (OSError, UnicodeError):
            return False
        if not model and platform.machine().lower().startswith(('arm', 'aarch64')):
            return False  # A hidden device tree cannot authorize a Pi Zero container.
    return camera_tier_allowed(system, model, indi=indi,
                               pi5_test=(pi5_test_enabled() or pi5_gphoto_enabled()) if model.startswith('Raspberry Pi 5 ') else False)


def _pi5_marker(marker):
    if platform.system() != 'Linux':
        return False
    try:
        with Path('/proc/device-tree/model').open('rb') as stream:
            model = stream.read(257)
        return (len(model) <= 256 and model.rstrip(b'\0\n').startswith(b'Raspberry Pi 5 ') and
                pi5_test_enabled(marker))
    except OSError:
        return False


def pi5_gphoto_enabled():
    return _pi5_marker(Path('/etc/wifi-bridge/pi5-indi-gphoto-test'))


def pi5_appliance():
    """The Pi 5 appliance image, public or private, on a real Pi 5.

    Its root-owned image marker enables the appliance network behavior (DNS-SD
    announcement, wired uplink for the onboard radio) without the private
    gPhoto camera opt-in.
    """
    return pi5_gphoto_enabled() or _pi5_marker(Path('/etc/wifi-bridge/pi5-appliance'))


class CameraDiscovery:
    def __init__(self, service, registry, *, supported=False, source=None, monitor=None,
                 clock=time.monotonic, on_event=None):
        self.service, self.registry, self.supported = service, registry, supported
        self.source = source if source is not None else SysfsUSB()
        self.monitor = monitor if monitor is not None else UEventMonitor()
        self.clock, self.on_event = clock, on_event
        self.deadline, self.last_scan = 0, float('-inf')
        self.pending = False
        self.pending_events = deque(maxlen=64)
        self.callback_errors = 0
        self._stop = threading.Event()
        self._thread = None

    def sync(self):
        if not self.supported or self._stop.is_set() or not self.service.get_settings()['serve']:
            return []
        self._deliver()
        self.monitor.start()
        self.pending = self.monitor.changed() or self.pending
        now = self.clock()
        if now < self.deadline and (not self.pending or now - self.last_scan < 0.5):
            return []
        self.pending, self.last_scan, self.deadline = False, now, now + 5
        try:
            events = self.service.reconcile_usb(self.source.scan(), self.registry)
        except (AlpacaError, OSError):
            return []
        # Callbacks run outside the sole driver worker, avoiding lock inversion
        # with an HTTP/controller thread waiting for its inventory response.
        if self.on_event:
            self.pending_events.extend(dict(event) for event in events)
            self._deliver()
        return events

    def _deliver(self):
        while self.on_event and self.pending_events:
            try:
                self.on_event(dict(self.pending_events[0]))
            except Exception:
                self.callback_errors = min(self.callback_errors + 1, 2**63 - 1)
                return  # Retain a bounded queue and retry on the next timer tick.
            self.pending_events.popleft()

    def start(self):
        if self.supported and self._thread is None and not self._stop.is_set():
            self._thread = threading.Thread(target=self._run, name='camera-usb', daemon=True)
            self._thread.start()

    def _run(self):
        try:
            while not self._stop.is_set():
                self.sync()
                self._stop.wait(0.25)
        finally:
            self.monitor.close()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        else:
            self.monitor.close()
