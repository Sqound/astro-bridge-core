#!/usr/bin/env python3
"""Install a checked wheel into an isolated Unix virtual environment; no sudo."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import venv


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel',required=True,type=Path)
    parser.add_argument('--sha256',required=True)
    parser.add_argument('--prefix',type=Path,default=Path.home()/'.local/share/astro-bridge')
    args=parser.parse_args()
    wheel=args.wheel.resolve()
    if hashlib.sha256(wheel.read_bytes()).hexdigest()!=args.sha256:parser.error('Wheel checksum mismatch')
    prefix=args.prefix.expanduser().absolute()
    if prefix.is_symlink() or prefix.exists():parser.error('Use a new, empty installation prefix; updates belong to the deployment manager')
    if sys.platform=='win32':parser.error('Use a Unix environment; native Windows radio control is not supported')
    venv.EnvBuilder(with_pip=True).create(prefix)
    subprocess.run([str(prefix/'bin/python'),'-m','pip','install','--no-index','--no-deps',str(wheel)],check=True)
    print('Installed. Start the simulation with:')
    print(str(prefix/'bin/astro-bridge')+' --demo --state-dir '+str(prefix/'state'))
    print('Real Linux networking needs a host integration from astro-bridge-implementations.')


if __name__=='__main__':main()
