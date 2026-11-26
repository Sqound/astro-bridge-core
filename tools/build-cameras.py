#!/usr/bin/env python3
"""Validate camera tables and regenerate their editor schema; no hardware access."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wifi_bridge.cameras.frames import benchmark_transpose
from wifi_bridge.cameras.registry import load_registry, manifest_schema
from wifi_bridge.cameras import catalog as driver_catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--benchmark', action='store_true')
    options = parser.parse_args()
    # The driver catalog first: the camera tables name its manufacturers.
    source = ROOT / 'cameras' / 'catalog' / 'drivers-catalog.json'
    value = driver_catalog.load_catalog(source)
    driver_catalog._cached = value
    registry = load_registry(ROOT / 'cameras')
    output = ROOT / 'cameras' / 'schema' / 'manifest.schema.json'
    content = json.dumps(manifest_schema(), indent=2) + '\n'
    outputs = {output: content,
               ROOT / 'cameras' / 'schema' / 'drivers-catalog.schema.json': json.dumps(driver_catalog.catalog_schema(), indent=2) + '\n',
               driver_catalog.PATH: source.read_text()}
    directory = ROOT / 'wifi_bridge' / 'cameras' / 'tables'
    for table in registry.descriptors():
        outputs[directory / (table['id'] + '.json')] = json.dumps(table, indent=2) + '\n'
    if directory.exists() and any(path not in outputs for path in directory.iterdir()):
        raise SystemExit('Unexpected bundled camera table file.')
    for output, content in outputs.items():
        if output.is_symlink() or output.parent.is_symlink():
            raise SystemExit('Camera outputs must not contain symlinks.')
        if options.check:
            if not output.exists() or output.is_symlink() or output.read_text() != content:
                raise SystemExit('Camera manifest schema or bundle is stale.')
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content)
    print(json.dumps({'camera_tables': len(registry.descriptors()), 'driver_catalog': value['catalog_version'],
                      'manufacturers': len(value['drivers']), 'schema': 'ok'}))
    if options.benchmark:
        print(json.dumps(benchmark_transpose(), sort_keys=True))


if __name__ == '__main__':
    main()
