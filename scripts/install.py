#!/usr/bin/env python3
"""Install the pinned Astro Bridge Core release in an isolated environment; no sudo."""
# SPDX-License-Identifier: MIT
import argparse
import hashlib
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.request
import venv

RELEASE_VERSION = '1.0.1'
RELEASE_SHA256 = None  # The release builder embeds the exact wheel checksum.


def wheel_data(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        if not response.url.startswith('https://'): raise ValueError('HTTPS download required')
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024: raise ValueError('Wheel exceeds the download limit')
    return data


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel',type=Path,help='Use a local wheel instead of downloading the pinned release')
    parser.add_argument('--sha256',help='Expected checksum for an explicit local wheel')
    parser.add_argument('--prefix',type=Path,default=Path.home()/('.local/share/astro-bridge-'+RELEASE_VERSION))
    args=parser.parse_args()
    prefix=args.prefix.expanduser().absolute()
    if prefix.is_symlink() or prefix.exists():parser.error('Use a new, empty installation prefix; updates belong to the deployment manager')
    if sys.platform=='win32':parser.error('Use a Unix environment; native Windows radio control is not supported')
    if sys.version_info < (3,11):parser.error('Python 3.11 or later is required')
    checksum=args.sha256 or RELEASE_SHA256
    if not checksum or not re.fullmatch('[0-9a-fA-F]{64}',checksum):
        parser.error('Use the installer from the release, or provide --wheel and --sha256')
    if args.sha256 and not args.wheel:parser.error('--sha256 requires --wheel')
    with tempfile.TemporaryDirectory(prefix='astro-core-download-') as folder:
        wheel=args.wheel.resolve() if args.wheel else Path(folder)/f'astro_bridge_core-{RELEASE_VERSION}-py3-none-any.whl'
        if not args.wheel:
            url=f'https://github.com/Sqound/astro-bridge-core/releases/download/v{RELEASE_VERSION}/'+wheel.name
            print('Downloading Astro Bridge Core '+RELEASE_VERSION+'...')
            wheel.write_bytes(wheel_data(url))
        if hashlib.sha256(wheel.read_bytes()).hexdigest()!=checksum.lower():parser.error('Wheel checksum mismatch; nothing was installed')
        venv.EnvBuilder(with_pip=True).create(prefix)
        subprocess.run([str(prefix/'bin/python'),'-m','pip','install','--no-index','--no-deps',str(wheel)],check=True)
    print('Installed in '+str(prefix)+'. No system Python packages were changed.')
    print('Start the simulation:')
    state=Path.home()/'.local/state/astro-bridge-demo'
    print(shlex.quote(str(prefix/'bin/astro-bridge'))+' --demo --state-dir '+shlex.quote(str(state)))
    print('Open http://127.0.0.1:8787; demo password: demo-telescope-2026')
    print('Real Linux networking needs a host integration from astro-bridge-implementations.')


if __name__=='__main__':main()
