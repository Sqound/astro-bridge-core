"""Small management facade; downloads and installation run in separate units."""
import json
from pathlib import Path
import subprocess
from . import __version__
from .build import BUILD_ID
from .ota import ACTIVE_PHASES

ALPINE = Path('/etc/alpine-release').exists()
UPDATER = Path('/usr/libexec/astro-bridge-ota')
from .config import Invalid


class Updates:
    def __init__(self, state, demo=False):
        self.root = Path(state) / 'updates'
        self.demo = demo

    def read(self, name, default):
        try:
            value = json.loads((self.root / name).read_text())
            return value if isinstance(value, dict) else default
        except (OSError, ValueError):
            return default

    def recovery_required(self):
        path = self.root / 'transaction.json'
        try:
            value = json.loads(path.read_text())
            return not isinstance(value, dict) or value.get('phase') not in (*ACTIVE_PHASES, 'complete', 'rolled_back')
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            return True

    def busy(self):
        return self.recovery_required() or self.read('transaction.json', {}).get('phase') in ACTIVE_PHASES

    def supported(self):
        try:
            from .runtime import platform
            return platform() == "raspberry" and (not ALPINE or UPDATER.is_file() and Path('/etc/wifi-bridge/updater-version').read_text().strip() == '2')
        except OSError:
            return False

    def status(self):
        value = self.read('status.json', {'state': 'unchecked'})
        if self.recovery_required(): value = dict(value, state='recovery_required')
        return dict(value, current_version=__version__, current_build_id=BUILD_ID, supported=self.supported(),
                    maintenance=self.busy(),
                    automatic=self.read('preferences.json', {}).get('automatic', False), demo=self.demo)

    def write(self, name, data):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / name
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(data))
        temp.chmod(0o600)
        temp.replace(path)

    def action(self, data):
        if self.demo:
            raise Invalid('Updates are unavailable in demonstration mode.')
        if not self.supported():
            raise Invalid('The OS updater prerequisites are not installed.')
        if self.busy():
            raise Invalid('An application update is in progress. Try again shortly.')
        if data == {'action': 'check'}:
            unit = 'astro-bridge-update-check'
        elif data.get('action') == 'preferences' and set(data) == {'action', 'automatic'} and type(data['automatic']) is bool:
            self.write('preferences.json', {'automatic': data['automatic']})
            return self.status()
        elif set(data) == {'action', 'release_id'} and data['action'] == 'install':
            status = self.status()
            if not status.get('available') or data['release_id'] != status.get('release_id'):
                raise Invalid('Check the available version again.')
            self.write('request.json', {'release_id': data['release_id']})
            unit = 'astro-bridge-update-install'
        else:
            raise Invalid('Invalid update action.')
        if ALPINE:
            command = ['/usr/bin/python3', str(UPDATER), 'check' if unit.endswith('check') else 'install']
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        else:
            result = subprocess.run(['systemctl', 'start', '--no-block', unit], capture_output=True, timeout=10)
            if result.returncode:
                raise Invalid('The update service is not installed or available.')
        return {'ok': True}
