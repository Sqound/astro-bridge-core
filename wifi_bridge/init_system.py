"""Translate the appliance's fixed service operations for systemd or OpenRC."""
from pathlib import Path
import subprocess


def using_openrc():
    marker = Path('/etc/wifi-bridge/init-system')
    return marker.exists() and marker.read_text().strip() == 'openrc'


def openrc_command(args, execute):
    """Return systemctl-compatible results; execute accepts an argument list."""
    action, *names = args[1:]
    if action == 'reboot' and not names:
        return execute(['/sbin/reboot'])
    if action not in ('start', 'stop', 'restart', 'is-active') or not names:
        raise ValueError('Unsupported OpenRC operation')
    results = []
    for name in names:
        operation = 'status' if action == 'is-active' else action
        # A one-shot password task must execute on every request.
        if name == 'wifi-bridge-password' and action == 'start':
            operation = 'restart'
        result = execute(['/sbin/rc-service', name, operation])
        results.append(result)
    if action == 'is-active':
        return subprocess.CompletedProcess(args, 0 if all(r.returncode == 0 for r in results) else 3,
            ''.join('active\n' if r.returncode == 0 else 'inactive\n' for r in results), '')
    return subprocess.CompletedProcess(args, next((r.returncode for r in results if r.returncode), 0),
                                       ''.join(r.stdout or '' for r in results), '')
