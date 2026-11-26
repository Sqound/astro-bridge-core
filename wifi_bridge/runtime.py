"""Deployment capabilities; application state never grants host privileges."""
import os
from pathlib import Path


def platform():
    value = os.environ.get('ASTRO_BRIDGE_PLATFORM', '')
    if value not in ('', 'native', 'docker', 'raspberry'):
        raise ValueError('Unknown Astro Bridge deployment platform')
    if value:
        return value
    from .ota import live_platform
    if Path('/etc/wifi-bridge/deployment').is_file():
        try:
            return 'live-usb' if live_platform() else 'native'
        except (OSError, ValueError, UnicodeError):
            return 'native'
    appliance = Path('/etc/wifi-bridge/diskless').is_file() or Path('/etc/wifi-bridge/updater-version').is_file()
    return 'raspberry' if appliance else 'native'


# Root-owned marker written only by the Raspberry Pi 5 image recipe (image/pi5/prepare.py).
PI5_APPLIANCE = Path('/etc/wifi-bridge/pi5-appliance')


def pi5_appliance():
    try:
        info = PI5_APPLIANCE.lstat()
    except OSError:
        return False
    return info.st_uid == 0 and not info.st_mode & 0o022 and PI5_APPLIANCE.is_file()


def linux_account(demo=False):
    """The Linux account the bridge password also protects, or None.

    The Raspberry Pi 5 image uses root (its console and SSH account); the Pi Zero W
    and live images use the fixed account bridge. Other installations leave the
    host accounts alone.
    """
    if demo:
        return 'root'
    if pi5_appliance():
        return 'root'
    return 'bridge' if platform() in ('raspberry', 'live-usb') else None


def capabilities(demo=False):
    current = 'demo' if demo else platform()
    appliance = demo or current in ('raspberry', 'live-usb')
    account = linux_account(demo)
    return {'platform': current,
            'host_reboot': appliance, 'linux_credentials': account is not None,
            'linux_account': account}
