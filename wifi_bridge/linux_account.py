"""The Linux side of the single bridge password, and the SSH state the image reports.

Raspberry Pi 5 image: the account is root. Its hash lives in the RAM system and
is kept on the data partition by astro-console-account; saving it also restarts
astro-ssh, whose rule (astro_ssh_rule.py) opens password login only once root no
longer has the initial password. The password reaches chpasswd through stdin
only; command errors are never repeated (they may contain it).
"""
import json
from pathlib import Path

SHADOW = Path('/etc/shadow')
SSH_STATE = Path('/run/astro-ssh/state.json')


def _root_hash(shadow=SHADOW):
    entries = [line.split(':') for line in shadow.read_text().splitlines() if line.startswith('root:')]
    if len(entries) != 1 or len(entries[0]) < 2 or not entries[0][1]:
        raise ValueError('missing root entry')
    return entries[0][1]


def change_root_password(password, run, shadow=SHADOW):
    """Set root's password and keep it across reboots; return how to undo it.

    Raises when the account could not be changed; then nothing changed.
    """
    previous = _root_hash(shadow)
    run(['chpasswd', '-c', 'sha512'], input='root:' + password + '\n')

    def undo():
        run(['chpasswd', '-e'], input='root:' + previous + '\n')
        run(['rc-service', 'astro-console-account', 'save'])
    try:
        # Keeps the new hash on the data partition and restarts astro-ssh.
        run(['rc-service', 'astro-console-account', 'save'])
    except Exception:
        try:
            run(['chpasswd', '-e'], input='root:' + previous + '\n')
        except Exception:
            pass
        raise
    return undo


def ssh_status(account, path=SSH_STATE):
    """What SSH really accepts, as written by the image rule at its last start.

    password_login is None when the rule never ran here (no image rule, or a
    host that is not an Astro Bridge image).
    """
    value = None
    if account:
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= 256:
                data = json.loads(path.read_text())
                if isinstance(data, dict) and type(data.get('password_login')) is bool:
                    value = data['password_login']
        except (OSError, ValueError, UnicodeError):
            value = None
    return {'account': account, 'password_login': value}
