"""One child process per opened camera, for every manufacturer library.

A manufacturer library is closed native code that may crash or hang. It is
loaded only in a disposable child process: the bridge talks to it over a
bounded socket (the transport of zwo_process.py), native output is discarded,
no core file is written, every call has a deadline and the child dies with its
parent. A driver is off until its owner accepted the manufacturer's terms
(drivers.py): before that, opening a camera fails with
camera_driver_unavailable, which the Cameras page explains in plain words.
A library that is missing or does not load (for example a glibc library that
gcompat cannot satisfy on Alpine) gives the same answer: the bridge runs on.

Only reviewed adapters below may run in a child. A manufacturer of the catalog
without an adapter is known, never served. Every adapter other than ZWO is
written from its manufacturer's documentation and not verified on hardware
(VERIFIED_ON_HARDWARE in each module, verified_on_hardware in the catalog).
"""
import socket
import sys

from .driver import CameraError
from .zwo_process import IsolatedASI, serve


def _zwo(device):
    from .zwo_asi import ASIDriver
    from .zwo_platform import declared_path
    return ASIDriver(device, declared_path())


def _player_one(device):
    from .player_one import PlayerOneDriver
    from .vendor_platform import declared_path
    return PlayerOneDriver(device, declared_path('player_one'))


def _qhyccd(device):
    from .qhyccd import QHYDriver
    from .vendor_platform import declared_path
    return QHYDriver(device, declared_path('qhyccd'))


def _atik(device):
    from .atik import AtikDriver
    from .vendor_platform import declared_path
    return AtikDriver(device, declared_path('atik'))


def _moravian(device):
    from .moravian import MoravianDriver
    from .vendor_platform import declared_path
    return MoravianDriver(device, declared_path('moravian'))


# Reviewed adapters: manufacturer -> factory(device) run inside the child only.
ADAPTERS = {'zwo_asi': _zwo, 'player_one': _player_one, 'qhyccd': _qhyccd, 'atik': _atik, 'moravian': _moravian}
# The module of each adapter, which declares VERIFIED_ON_HARDWARE.
MODULES = {'zwo_asi': 'zwo_asi', 'player_one': 'player_one', 'qhyccd': 'qhyccd', 'atik': 'atik', 'moravian': 'moravian'}


def verified_on_hardware(vendor):
    """What the adapter itself declares. No manufacturer driver is verified on hardware in the form delivered today."""
    if vendor not in ADAPTERS:
        return False
    import importlib
    return bool(importlib.import_module('.' + MODULES[vendor], __package__).VERIFIED_ON_HARDWARE)


class IsolatedDriver(IsolatedASI):
    """The shared isolation for a manufacturer driver. ZWO keeps its historical child entry point."""
    def __init__(self, vendor, *, device):
        if vendor not in ADAPTERS:
            raise CameraError('camera_driver_unavailable')
        super().__init__(device=device)
        self.vendor = vendor

    def command(self, descriptor):
        if self.vendor == 'zwo_asi':
            return super().command(descriptor)
        return [sys.executable, '-m', 'wifi_bridge.cameras.vendor_process', self.vendor, str(descriptor)]


def factory(vendor):
    """A registry factory for this manufacturer, or None when no adapter exists yet.

    The library is looked up before any child starts: a missing or not yet
    accepted driver answers camera_driver_unavailable at once.
    """
    if vendor not in ADAPTERS:
        return None

    def create(**options):
        if vendor != 'zwo_asi':  # ZWO: the registry checks its installed SDK (registry._zwo_asi).
            from .vendor_platform import resolve
            resolve(vendor)
        return IsolatedDriver(vendor, **options)
    return create


if __name__ == '__main__':
    import resource
    import signal
    vendor = sys.argv[1] if len(sys.argv) == 3 else ''
    if vendor not in ADAPTERS:
        raise SystemExit(2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    with socket.socket(fileno=int(sys.argv[2])) as channel:
        serve(channel, factory=ADAPTERS[vendor], arm_watchdog=signal.alarm)
