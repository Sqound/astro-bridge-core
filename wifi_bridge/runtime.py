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


def capabilities(demo=False):
    appliance = demo or platform() in ('raspberry', 'live-usb')
    return {'platform': 'demo' if demo else platform(),
            'host_reboot': appliance, 'linux_credentials': appliance}
