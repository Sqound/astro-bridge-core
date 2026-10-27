"""Shared atomic records and immutable app identity; no OS updater."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
ACTIVE_PHASES = ("prepared", "applying", "verifying")

def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)

def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def atomic(path, value):
    atomic_bytes(path, json.dumps(value).encode())

def source_identity(files):
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        if name != 'wifi_bridge/build.py':
            digest.update(name.encode() + b'\0' + content + b'\0')
    return digest.hexdigest()

def tree_identity(path):
    return source_identity({'wifi_bridge/' + str(p.relative_to(path)): p.read_bytes()
                            for p in path.rglob('*') if p.is_file() and '__pycache__' not in p.parts})

def read(path, default):
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict): raise ValueError('Invalid updater record.')
        return value
    except FileNotFoundError:
        return default
