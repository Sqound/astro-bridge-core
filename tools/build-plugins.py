"""Validate community source manifests and reproducibly bundle the OTA catalog."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from wifi_bridge.plugins import CORE_IDS, SCHEMA_VERSION, validate_registry, manifest_schema
parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
paths=sorted((ROOT/'plugins').glob('*.json'))
values=[]
for path in paths:
    if path.is_symlink() or path.stat().st_size>32768:raise ValueError('Unsafe source manifest.')
    value=json.loads(path.read_text())
    if path.stem!=value.get('id'):raise ValueError('Plugin filename must match its ID.')
    values.append(value)
registry=validate_registry(values)
ordered=[registry.pop(key) for key in CORE_IDS]+[registry[key] for key in sorted(registry)]
output=json.dumps({'schema_version':SCHEMA_VERSION,'plugins':ordered},ensure_ascii=False,indent=2)+'\n'
destination=ROOT/'wifi_bridge/static/plugins.json'
if args.check:
    if not destination.exists() or destination.read_text()!=output:raise SystemExit('Plugin catalog is stale; run python3 tools/build-plugins.py')
else:destination.write_text(output)
print('PASS: validated '+str(len(ordered))+' declarative plugins')

schema=json.dumps(dict(manifest_schema(),**{'$schema':'https://json-schema.org/draft/2020-12/schema'}),ensure_ascii=False,indent=2)+'\n'
schema_path=ROOT/'plugins/schema/manifest.schema.json'
if args.check:
    if not schema_path.exists() or schema_path.read_text()!=schema:raise SystemExit('Plugin editor schema is stale')
else:
    schema_path.parent.mkdir(exist_ok=True);schema_path.write_text(schema)
