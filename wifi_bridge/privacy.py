"""Non-exportable runtime secret paths for every artifact boundary."""
import re
from pathlib import PurePosixPath

# Configuration exports/backups serialize configuration only, never this folder.
# Diagnostics must use normalized public records, never recursive state copying.
SECRET_FILES = frozenset(('maker-keys.kek','maker-keys.bin'))
PRIVATE_ARMOUR = re.compile(b'-----'+b'BEGIN '+b'(?:[A-Z0-9]+ )?'+b'PRIVATE'+b' KEY-----')


def private_path(name):
    return any(part in SECRET_FILES or any(part.startswith('.'+secret+'-') for secret in SECRET_FILES)
               for part in PurePosixPath(str(name)).parts)


def public_artifact(name, data):
    if private_path(name) or PRIVATE_ARMOUR.search(data):
        raise ValueError('Private key material is excluded from artifacts.')
