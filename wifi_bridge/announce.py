"""Bounded DNS-SD identity, runtime capabilities and atomic service publication."""
import os
from pathlib import Path
import re
import tempfile
from xml.sax.saxutils import escape

from . import __version__

CAPABILITIES = ('pairing', 'instruments', 'connect', 'events-wait', 'telemetry',
                'alpaca-camera', 'maker-keys')


def display_name(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError('Invalid announcement name')
    return value.encode('utf-8')[:63].decode('utf-8', errors='ignore')


def enabled_capabilities(runtime):
    """Only explicit, currently available services may claim a native capability."""
    return [cap for cap in CAPABILITIES if runtime.get(cap) is True]


def txt_records(settings, runtime):
    name = display_name(settings['name'])
    if not re.fullmatch('[0-9a-f]{16}', settings['device_id']):
        raise ValueError('Invalid announcement identity')
    values = {'api': '/api/v1', 'api-version': '1', 'contract': '1',
              'product': 'astro-bridge', 'version': settings.get('version', __version__),
              'id': settings['device_id'], 'name': name,
              'caps': ','.join(enabled_capabilities(runtime))}
    port = runtime.get('https_port')
    if port is not None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Invalid announcement HTTPS port')
        values['https'] = str(port)
        fingerprint = runtime.get('tls_fingerprint')
        if fingerprint is not None:
            # SHA-256 of the bridge CA certificate: a TOFU pin, checked again over TLS.
            if not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
                raise ValueError('Invalid announcement fingerprint')
            values['fp'] = fingerprint
    # Contract 1.1 hints; absent on older bridges. Never instrument identities.
    values['contract-minor'] = '2'
    radios = runtime.get('free_radios')
    if radios is not None:
        if type(radios) is not int or not 0 <= radios <= 16: raise ValueError('Invalid announcement radio count')
        values['free_radios'] = str(radios)
    if runtime.get('hotspot') is True: values['hotspot'] = '1'
    # Changes at every start of the bridge: a client that kept an earlier answer knows it must ask again.
    boot = runtime.get('boot')
    if boot is not None:
        if not isinstance(boot, str) or not re.fullmatch('[0-9a-f]{8}', boot): raise ValueError('Invalid announcement boot mark')
        values['boot'] = boot
    records = [key + '=' + value for key, value in values.items()]
    validate_txt(records)
    return records


def validate_txt(records):
    if (len(records) > 32 or any(not isinstance(s, str) or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF or ord(c) in (0xFFFE, 0xFFFF) for c in s) or len(s.encode()) > 255 for s in records)
            or sum(len(s.encode()) + 1 for s in records) > 1300):
        raise ValueError('Announcement TXT exceeds DNS-SD limits')


def service_xml(settings, runtime, port=80):
    """Pure renderer: no state reads, filesystem changes or host commands."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Invalid announcement port')
    records = txt_records(settings, runtime)
    lines = ['<?xml version="1.0" standalone="no"?>',
             '<!DOCTYPE service-group SYSTEM "avahi-service.dtd">', '<service-group>',
             '  <name replace-wildcards="no">' + escape(display_name(settings['name'])) + '</name>']
    primary = records + (['indi=7624'] if runtime.get('indi') == 7624 else [])
    validate_txt(primary)
    for kind, txt in (('_http._tcp', ['path=/']), ('_astrobridge._tcp', primary), ('_smartbridge._tcp', records)):
        lines.extend(['  <service>', '    <type>' + escape(kind) + '</type>', '    <port>' + str(port) + '</port>'])
        lines.extend('    <txt-record>' + escape(value) + '</txt-record>' for value in txt)
        lines.append('  </service>')
    return ('\n'.join([*lines, '</service-group>', ''])).encode('utf-8')


def write_changed(path, content):
    """Replace only changed bytes; fsync the file and its containing directory."""
    path = Path(path)
    try:
        if path.read_bytes() == content: return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def update(directory, hostname, devices, settings, runtime, execute):
    """Only the Raspberry backend calls this; other hosts remain owner-managed."""
    directory = Path(directory)
    path = directory / 'avahi-daemon.conf'
    try: previous = path.read_text()
    except FileNotFoundError: previous = ''
    # An empty management set must never make Avahi publish on instrument radios.
    interfaces = ','.join(sorted({d['name'] for d in devices})) or 'lo'
    config = ('[server]\nhost-name=' + hostname + '\nallow-interfaces=' + interfaces +
              '\nuse-ipv6=no\n[wide-area]\nenable-wide-area=no\n[publish]\n' +
              'publish-workstation=no\n[reflector]\nenable-reflector=no\n')
    old_interfaces = re.search(r'^allow-interfaces\s*=\s*(.*)$', previous, re.M)
    interfaces_changed = old_interfaces is None or old_interfaces[1] != interfaces
    # Validate every public value before writing either configuration file.
    service = service_xml(settings, runtime)
    service_path = directory / 'services/astro-bridge.service'
    legacy = directory / 'services/wifi-bridge-http.service'
    pending_path = directory / '.astro-bridge-update'
    try: pending = pending_path.read_bytes()
    except FileNotFoundError: pending = None
    try: services_changed = service_path.read_bytes() != service
    except FileNotFoundError: services_changed = True
    changed = previous != config or services_changed or legacy.exists()
    if not changed and pending is None: return False
    restart = interfaces_changed or pending == b'restart'
    # Retain a bounded retry marker if writing or reloading fails. Otherwise the
    # next reconcile could mistake new bytes for a successfully applied update.
    write_changed(pending_path, b'restart' if restart else b'reload')
    write_changed(path, config.encode())
    write_changed(service_path, service)
    if legacy.exists(): legacy.unlink()
    # --reload refreshes services, not daemon host-name; a hostname-only edit
    # therefore takes effect for .local on the next interface/owner restart.
    try:
        execute(['systemctl', 'restart', 'avahi-daemon'] if restart else ['avahi-daemon', '--reload'])
    except Exception:
        # A reload fails when the daemon is not running: the next try starts it.
        if not restart: write_changed(pending_path, b'restart')
        raise
    pending_path.unlink()
    return True
