"""Runtime discovery publication uses bounded public values and management roles."""
import http.client
import threading
import hashlib
import hmac
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from wifi_bridge.announce import (display_name, enabled_capabilities, service_xml,
                                  txt_records, update, validate_txt, write_changed)
from wifi_bridge.config import device_identity
from wifi_bridge.events import EventStore
from wifi_bridge.network import LinuxNetwork, NetworkError, run
from test_bridge import Fixture
from wifi_bridge.web import WebServer

SETTINGS = {'name': 'Bridge <test> & "stars"', 'device_id': '0123456789abcdef'}


class AnnouncementTests(unittest.TestCase):
    def test_xml_escape_utf8_name_and_runtime_caps(self):
        settings = dict(SETTINGS, name='星' * 22 + '<&')
        self.assertEqual(len(display_name(settings['name']).encode()), 63)
        xml = service_xml(SETTINGS, {'pairing': True, 'telemetry': False, 'connect': 1})
        parsed = ElementTree.fromstring(xml)
        self.assertEqual(parsed.findtext('name'), SETTINGS['name'])
        self.assertEqual(parsed.find('name').attrib['replace-wildcards'], 'no')
        self.assertIn(b'&lt;test&gt; &amp;', xml)
        self.assertEqual(parsed.findtext("service[type='_astrobridge._tcp']/port"), '80')
        self.assertEqual({s.findtext('type') for s in parsed.findall('service')}, {'_http._tcp', '_smartbridge._tcp', '_astrobridge._tcp'})
        for runtime, caps in (({}, []), ({'pairing': True}, ['pairing']),
                              ({'telemetry': True, 'maker-keys': True}, ['telemetry', 'maker-keys'])):
            records = dict(s.split('=', 1) for s in txt_records(settings, runtime))
            self.assertEqual(records['caps'], ','.join(caps))
            self.assertEqual(enabled_capabilities(runtime), caps)
            self.assertNotIn('https', records)
        self.assertIn('https=443', txt_records(settings, {'https_port': 443}))
        for port in (0, 65536, True, '443'):
            with self.assertRaises(ValueError): txt_records(settings, {'https_port': port})

    def test_txt_byte_limits_including_length_octets(self):
        validate_txt(['x' * 255])
        validate_txt(['x' * 255] * 5 + ['x' * 19])  # exactly 1300 bytes
        for records in (['x' * 256], ['星' * 86], ['x' * 255] * 5 + ['x' * 20], ['x\x00y']):
            with self.assertRaises(ValueError): validate_txt(records)
        with self.assertRaises(ValueError): txt_records(dict(SETTINGS, version='x' * 248), {})
        for name in ('', 'bad\nname', '\ud800'):
            with self.assertRaises(ValueError): service_xml(dict(SETTINGS, name=name), {})

    def test_atomic_bytes_change_only_and_reload_restart_table(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = [{'name': 'wlan1'}]
            calls = []
            execute = calls.append
            self.assertTrue(update(root, 'bridge', home, SETTINGS, {}, execute))
            self.assertEqual(calls, [['systemctl', 'restart', 'avahi-daemon']])
            path = root / 'services/astro-bridge.service'
            stamp = path.stat().st_mtime_ns
            calls.clear()
            self.assertFalse(update(root, 'bridge', home, SETTINGS, {}, execute))
            self.assertEqual(path.stat().st_mtime_ns, stamp)
            self.assertEqual(calls, [])
            self.assertTrue(update(root, 'bridge', home, SETTINGS, {'pairing': True}, execute))
            self.assertEqual(calls, [['avahi-daemon', '--reload']])
            calls.clear()
            renamed = dict(SETTINGS, name='Renamed bridge')
            self.assertTrue(update(root, 'renamed', home, renamed, {'pairing': True}, execute))
            self.assertEqual(calls, [['avahi-daemon', '--reload']])
            self.assertIn('host-name=renamed\n', (root / 'avahi-daemon.conf').read_text())
            self.assertEqual(ElementTree.fromstring(path.read_bytes()).findtext('name'), 'Renamed bridge')
            calls.clear()
            self.assertTrue(update(root, 'bridge', home + [{'name': 'wlan0'}], SETTINGS, {'pairing': True}, execute))
            self.assertEqual(calls, [['systemctl', 'restart', 'avahi-daemon']])
            calls.clear()
            self.assertTrue(update(root, 'bridge', [], SETTINGS, {'pairing': True}, execute))
            self.assertIn('allow-interfaces=lo\n', (root / 'avahi-daemon.conf').read_text())
            self.assertEqual(calls, [['systemctl', 'restart', 'avahi-daemon']])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            self.assertEqual(list(root.glob('services/.astro-bridge*')), [])

    def test_failed_replace_preserves_file_and_static_service_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'file'; path.write_bytes(b'old')
            with patch('wifi_bridge.announce.os.replace', side_effect=OSError):
                with self.assertRaises(OSError): write_changed(path, b'new')
            self.assertEqual(path.read_bytes(), b'old')
            update(root, 'bridge', [], SETTINGS, {}, lambda args: None)
            legacy = root / 'services/wifi-bridge-http.service'; legacy.write_text('legacy')
            calls = []
            self.assertTrue(update(root, 'bridge', [], SETTINGS, {}, calls.append))
            self.assertFalse(legacy.exists())
            self.assertEqual(calls, [['avahi-daemon', '--reload']])

    def test_failed_command_retries_without_another_file_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for runtime, expected in (({}, ['systemctl', 'restart', 'avahi-daemon']),
                                      ({'pairing': True}, ['avahi-daemon', '--reload'])):
                with patch('wifi_bridge.announce.write_changed', wraps=write_changed):
                    with self.assertRaises(OSError):
                        update(root, 'bridge', [], SETTINGS, runtime, lambda args: (_ for _ in ()).throw(OSError()))
                calls = []
                self.assertTrue(update(root, 'bridge', [], SETTINGS, runtime, calls.append))
                # A reload that failed means no daemon was running: the next try starts it.
                self.assertEqual(calls, [['systemctl', 'restart', 'avahi-daemon']])
                self.assertFalse(update(root, 'bridge', [], SETTINGS, runtime, calls.append))

    def test_stopped_daemon_is_started_again_without_any_file_change(self):
        with tempfile.TemporaryDirectory() as directory:
            net = LinuxNetwork(directory); calls = []; running = {'daemon': False}

            def command(args, **kwargs):
                calls.append(list(args))
                if args == ['avahi-daemon', '--check'] and not running['daemon']: raise NetworkError('System command failed: avahi-daemon')
                if args == ['systemctl', 'restart', 'avahi-daemon']: running['daemon'] = True
                return ''
            appliance = lambda marker: marker.name == 'pi5-appliance'
            with patch('wifi_bridge.runtime.platform', return_value='native'), \
                 patch('wifi_bridge.cameras.discovery._pi5_marker', side_effect=appliance), \
                 patch('wifi_bridge.announce.update', return_value=False), patch('wifi_bridge.network.run', side_effect=command):
                # After a restart of the bridge service alone the files are unchanged and nothing is running.
                self.assertTrue(net.identity('bridge', [{'name': 'wlan0'}], SETTINGS, {}))
                self.assertEqual(calls, [['avahi-daemon', '--check'], ['systemctl', 'restart', 'avahi-daemon']])
                calls.clear()
                self.assertFalse(net.identity('bridge', [{'name': 'wlan0'}], SETTINGS, {}))
                self.assertEqual(calls, [])  # At most twice a minute.
                self.assertFalse(net.keep_announcing(now=net.announce_check)); self.assertEqual(calls, [['avahi-daemon', '--check']])
                # A daemon that cannot be started never fails the reconcile.
                running['daemon'] = False; calls.clear()
                with patch('wifi_bridge.network.run', side_effect=NetworkError('unavailable')):
                    self.assertFalse(net.keep_announcing(now=net.announce_check))

    def test_each_start_of_the_bridge_changes_the_announcement(self):
        first = dict(s.split('=', 1) for s in txt_records(SETTINGS, {'boot': '0123abcd'}))
        self.assertEqual(first['boot'], '0123abcd')
        self.assertNotIn('boot', dict(s.split('=', 1) for s in txt_records(SETTINGS, {})))
        for value in ('0123ABCD', '0123abc', 12345678, '0123abcd-'):
            with self.assertRaises(ValueError): txt_records(SETTINGS, {'boot': value})
        fixture = Fixture('setUp'); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        _, runtime = fixture.controller.announcement()
        self.assertRegex(runtime['boot'], '^[0-9a-f]{8}$')
        self.assertTrue(fixture.controller.event_store.boot_id.replace('-', '').startswith(runtime['boot']))

    def test_stable_private_random_installation_identity(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first = device_identity(one)
            self.assertRegex(first, '^[0-9a-f]{16}$')
            self.assertEqual(first, device_identity(one))
            self.assertNotEqual(first, device_identity(two))
            secret = Path(one) / 'install-secret'
            self.assertEqual(len(secret.read_bytes()), 32)
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
            self.assertEqual(first, hmac.new(secret.read_bytes(), b'astro-bridge-id', hashlib.sha256).hexdigest()[:16])

    def test_non_raspberry_never_changes_host(self):
        with tempfile.TemporaryDirectory() as directory:
            for platform in ('docker', 'native'):
                with patch('wifi_bridge.runtime.platform', return_value=platform), patch('wifi_bridge.announce.update') as publish:
                    self.assertFalse(LinuxNetwork(directory).identity('bridge', [], SETTINGS, {}))
                    publish.assert_not_called()

    def test_private_pi5_announcement_requires_the_private_model_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            net = LinuxNetwork(directory)
            with patch('wifi_bridge.runtime.platform', return_value='native'), \
                 patch('wifi_bridge.cameras.discovery.pi5_gphoto_enabled', return_value=True), \
                 patch('wifi_bridge.announce.update', return_value=True) as publish:
                self.assertTrue(net.identity('bridge', [{'name':'wlan0'}], SETTINGS, {}))
                self.assertEqual(publish.call_args.args[2], [{'name':'wlan0'}])
            with patch('wifi_bridge.runtime.platform', return_value='native'), \
                 patch('wifi_bridge.cameras.discovery.pi5_gphoto_enabled', return_value=False), \
                 patch('wifi_bridge.announce.update') as publish:
                self.assertFalse(net.identity('bridge', [], SETTINGS, {}))
                publish.assert_not_called()
            with patch('wifi_bridge.runtime.platform', return_value='docker'), \
                 patch('wifi_bridge.cameras.discovery.pi5_gphoto_enabled', return_value=True), \
                 patch('wifi_bridge.announce.update') as publish:
                self.assertFalse(net.identity('bridge', [], SETTINGS, {}))
                publish.assert_not_called()

    def test_public_pi5_image_marker_enables_the_announcement(self):
        # The public Pi 5 image has no private camera marker; its own image
        # marker must still let the bridge start Avahi and announce itself.
        with tempfile.TemporaryDirectory() as directory:
            net = LinuxNetwork(directory)
            appliance = lambda marker: marker.name == 'pi5-appliance'
            with patch('wifi_bridge.runtime.platform', return_value='native'), \
                 patch('wifi_bridge.cameras.discovery._pi5_marker', side_effect=appliance), \
                 patch('wifi_bridge.announce.update', return_value=True) as publish:
                self.assertTrue(net.identity('bridge', [{'name':'eth0'}], SETTINGS, {}))
                self.assertEqual(publish.call_args.args[2], [{'name':'eth0'}])
            with patch('wifi_bridge.runtime.platform', return_value='native'), \
                 patch('wifi_bridge.cameras.discovery._pi5_marker', return_value=False), \
                 patch('wifi_bridge.announce.update') as publish:
                self.assertFalse(net.identity('bridge', [], SETTINGS, {}))
                publish.assert_not_called()

    def test_announcement_event_roles_survive_restart_and_reject_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.json'
            store = EventStore(path)
            kwargs = dict(operation_id='opaque', profile_id='bridge', profile_name='', radio_id=None,
                          cause='RECONCILE', reason='ANNOUNCEMENT_UPDATED', outcome='ready')
            for roles in ([], ['home'], ['hotspot'], ['home', 'hotspot']):
                event = store.append('announcement_updated', **kwargs, interfaces=roles)
                self.assertEqual(event['interfaces'], roles)
            self.assertTrue(EventStore(path).history_available)
            for roles in (['wlan0'], ['192.0.2.1'], ['home', 'home'], 'home', None):
                with self.assertRaises(ValueError): store.append('announcement_updated', **kwargs, interfaces=roles)


class AnnouncementFlowTests(Fixture):
    def test_changed_only_success_events_have_management_roles(self):
        self.configured()
        with patch.object(self.net, 'identity', side_effect=[NetworkError('unavailable'), True, False]):
            with self.assertRaises(NetworkError): self.controller.cycle(0)
            self.assertFalse(any(e['type'] == 'announcement_updated' for e in self.controller.event_store.events))
            self.controller.cycle(5)
            self.controller.cycle(10)
        events = [e for e in self.controller.event_store.events if e['type'] == 'announcement_updated']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['interfaces'], ['home'])
        self.assertNotIn('wlan', json.dumps(events))

    def test_openrc_restart_shim_and_daemon_reload(self):
        import subprocess
        with patch('wifi_bridge.network.using_openrc', return_value=True), patch('wifi_bridge.network.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')) as execute:
            run(['systemctl', 'restart', 'avahi-daemon'])
            self.assertEqual(execute.call_args.args[0], ['/sbin/rc-service', 'avahi-daemon', 'restart'])
            run(['avahi-daemon', '--reload'])
            self.assertEqual(execute.call_args.args[0], ['avahi-daemon', '--reload'])


class IdentityHTTPTests(Fixture):
    def setUp(self):
        super().setUp()
        self.server = WebServer(('127.0.0.1', 0), self.controller)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read()), response.getheaders()
        connection.close()
        return result

    def test_public_info_socket_path_identity_and_runtime_schema(self):
        from helpers.contract_schema import validate
        spec = self.server.api.specification()
        for role, expected in (('home', 'home'), ('ap', 'hotspot'), ('none', 'other')):
            self.controller.state['management']['paths'] = [] if role == 'none' else [{'role': role, 'ip': '127.0.0.1'}]
            code, body, _ = self.request('GET', '/api/v1/info?request_path=hotspot', headers={'X-Request-Path': 'hotspot'})
            self.assertEqual(code, 200)
            self.assertEqual(body['request_path'], expected)
            self.assertEqual(body['device_id'], self.server.paired_clients.device_id)
            self.assertEqual(body['contract_version'], 1)
            self.assertEqual(body['capabilities'], ['pairing', 'instruments', 'connect', 'events-wait', 'telemetry', 'maker-keys'])
            self.assertEqual(body['pairing'], {'available': True, 'method': 'admin_password'})
            validate(body, spec['components']['schemas']['NativeInfo'], spec)
            settings, runtime = self.controller.announcement()
            txt = dict(s.split('=', 1) for s in txt_records(settings, runtime))
            self.assertEqual(txt['id'], body['device_id'])
            self.assertEqual(txt['name'], body['name'])
            self.assertEqual(txt['caps'].split(','), body['capabilities'])
