#!/usr/bin/env python3
"""Build a deterministic, dependency-free wheel and source archive; no upload."""
import argparse
import base64
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def package(root=ROOT, destination=None):
    root = Path(root)
    destination = Path(destination or root / 'dist')
    from wifi_bridge.plugins import SCHEMA_VERSION, validate_registry
    source = validate_registry([json.loads(p.read_text()) for p in sorted((root/'plugins').glob('*.json'))])
    bundled = json.loads((root/'wifi_bridge/static/plugins.json').read_text())
    if bundled.get('schema_version') != SCHEMA_VERSION or validate_registry(bundled['plugins']) != source:
        raise ValueError('Plugin catalog is stale; run tools/build-plugins.py')
    version = re.search(r'__version__ = "([^"]+)"', (root/'wifi_bridge/__init__.py').read_text())[1]
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version): raise ValueError('Invalid version')
    files = {}
    for path in sorted((root/'wifi_bridge').rglob('*')):
        relative = path.relative_to(root/'wifi_bridge')
        if path.is_symlink(): raise ValueError('Source symlinks are forbidden')
        if path.is_file() and '__pycache__' not in relative.parts and (len(relative.parts)==1 and path.suffix=='.py' or len(relative.parts)==2 and relative.parts[0]=='static'):
            files['wifi_bridge/'+relative.as_posix()] = path.read_bytes()
    from wifi_bridge.ota import source_identity
    identity = source_identity(files)
    files['wifi_bridge/build.py'] = f'BUILD_ID = "{identity}"\n'.encode()
    info = f'astro_bridge_core-{version}.dist-info'
    files[info+'/METADATA'] = (f'Metadata-Version: 2.1\nName: astro-bridge-core\nVersion: {version}\nSummary: Smart telescope and mount connection manager\nRequires-Python: >=3.11\nLicense: MIT\nProject-URL: Homepage, https://github.com/Sqound/astro-bridge-core\nProject-URL: Documentation, https://github.com/Sqound/astro-bridge-core/tree/main/doc\nProject-URL: Issues, https://github.com/Sqound/astro-bridge-core/issues\nProvides-Extra: push\nRequires-Dist: py-vapid==1.9.6; extra == "push"\nRequires-Dist: http-ece==1.2.1; extra == "push"\nRequires-Dist: cryptography>=46,<51; extra == "push"\n\n').encode()
    files[info+'/WHEEL'] = b'Wheel-Version: 1.0\nGenerator: astro-bridge-release\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
    files[info+'/entry_points.txt'] = b'[console_scripts]\nastro-bridge = wifi_bridge.__main__:main\nwifi-bridge = wifi_bridge.__main__:main\n'
    files[info+'/licenses/LICENSE'] = (root/'LICENSE').read_bytes()
    output = io.StringIO(newline=''); rows = csv.writer(output, lineterminator='\n')
    for name, data in sorted(files.items()):
        rows.writerow([name, 'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('='), len(data)])
    rows.writerow([info+'/RECORD', '', '']); files[info+'/RECORD'] = output.getvalue().encode()
    destination.mkdir(parents=True, exist_ok=True)
    wheel = destination/f'astro_bridge_core-{version}-py3-none-any.whl'
    with zipfile.ZipFile(wheel, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            item=zipfile.ZipInfo(name, (2020,1,1,0,0,0)); item.external_attr=0o100644 << 16; item.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(item,data)
    # Source includes contributor tools/tests, but never generated or private files.
    archive_path=destination/f'astro_bridge_core-{version}.tar.gz'
    with archive_path.open('wb') as stream, gzip.GzipFile(fileobj=stream,mode='wb',mtime=0,filename='') as compressed, tarfile.open(fileobj=compressed,mode='w') as archive:
        for path in sorted(root.rglob('*')):
            relative=path.relative_to(root)
            if any(p in ('.git','dist','build','__pycache__','.venv','.runtime','artifacts') for p in relative.parts): continue
            if path.is_symlink(): raise ValueError('Source symlinks are forbidden')
            if not path.is_file(): continue
            data=path.read_bytes(); item=tarfile.TarInfo(f'astro_bridge_core-{version}/'+relative.as_posix());item.size=len(data);item.mode=0o755 if path.stat().st_mode & 0o111 else 0o644
            archive.addfile(item,io.BytesIO(data))
    installer=destination/'install.py'
    installer.write_text((root/'scripts/install.py').read_text().replace(
        'RELEASE_SHA256 = None', 'RELEASE_SHA256 = '+repr(hashlib.sha256(wheel.read_bytes()).hexdigest()))
        +'\n'+''.join('# '+line+'\n' for line in (root/'LICENSE').read_text().splitlines()))
    artifacts={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (wheel,archive_path,installer)}
    (destination/'SHA256SUMS').write_text(''.join(f'{digest}  {name}\n' for name,digest in sorted(artifacts.items())))
    (destination/'release.json').write_text(json.dumps({'version':version,'build_id':identity,'artifacts':artifacts},indent=2)+'\n')
    return artifacts


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);args=parser.parse_args()
    print(json.dumps(package(destination=args.output),indent=2))
