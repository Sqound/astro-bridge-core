"""Multi-instrument isolation for Vespera: two telescopes on 10.0.0.1, one radio each.

Synthetic radios only. Relays, port assignment, firewall rules, namespaces journal
and native projections are the production code; no hardware is used.
"""
import copy
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.api import ApiError, MobileAPI
from wifi_bridge.config import Invalid, Store
from wifi_bridge.instrument_relay import InstrumentRelay
from wifi_bridge.isolated import IsolatedRadios, allocate_ports, namespace_firewall, namespace_name


class VesperaPairTests(Fixture):
    def setUp(self):
        super().setUp()
        self.net.radios = 4; self.devices = self.net.devices()
        self.controller.update(devices=self.devices)
        self.configured()
        self.api = MobileAPI(self.controller)

    def pair(self):
        first = dict(self.store.config['telescopes'][0], kind='vespera', target_ip='10.0.0.1', isolated=True)
        second = dict(first, id='vespera-two', name='Second Vespera', mac=self.devices[2]['mac'], ssid='Vespera-TWO')
        self.controller.save(dict(self.store.config, telescopes=[first, second]))
        return self.store.config['telescopes']

    def test_bridge_assigns_persistent_conflict_free_port_pairs(self):
        pair = self.pair()
        self.assertEqual([p['client_ports'] for p in pair], [[18082, 18083], [18084, 18085]])
        self.assertEqual([p['client_port'] for p in pair], [18082, 18084])
        # Survives reload, renaming and an omitted field.
        self.assertEqual(Store(self.tmp.name, self.devices[0]['mac']).config['telescopes'], pair)
        self.api.update('telescopes', {'name': 'Renamed'}, ident='primary')
        self.assertEqual([p['client_ports'] for p in self.store.config['telescopes']], [[18082, 18083], [18084, 18085]])
        # Owners never choose these ports.
        for change in ({'client_port': 18090}, {'client_ports': [18090, 18091]}):
            with self.subTest(change=change), self.assertRaises((Invalid, ApiError)):
                self.api.update('telescopes', change, ident='primary')

    def test_assigned_blocks_avoid_mount_ports_and_each_other(self):
        entries = [{'isolated': True, 'kind': 'zwo_mount', 'client_port': 18083},
                   {'isolated': True, 'kind': 'vespera'}, {'isolated': True, 'kind': 'vespera', 'client_ports': [18084, 18085]}]
        allocate_ports(entries, {})
        self.assertEqual(entries[2]['client_ports'], [18084, 18085])  # Retained block kept.
        self.assertEqual(entries[1]['client_ports'], [18086, 18087])  # Skips 18083 (mount) and the retained block.
        clash = [{'isolated': True, 'kind': 'vespera', 'client_ports': [18082, 18083]}, {'isolated': True, 'kind': 'vespera', 'client_ports': [18083, 18084]}]
        allocate_ports(clash, {})
        self.assertEqual([e['client_ports'] for e in clash], [[18082, 18083], [18084, 18085]])

    def test_both_connect_at_once_with_distinct_relays_and_native_roles(self):
        self.pair(); self.controller.cycle(0)
        rows = self.api.read('telescopes')['telescopes']
        self.assertTrue(all(p['connection']['connected'] and p['connection']['relay_ready'] for p in rows))
        self.assertTrue(all(p['connection']['state'] != 'conflict' for p in rows))
        ports = [sorted({a['port'] for a in p['client_endpoint']['services']}) for p in rows]
        # alternatives stays one entry per path (first port), as for mounts.
        self.assertEqual([{a['port'] for a in p['client_endpoint']['alternatives']} for p in rows], [{18082}, {18084}])
        self.assertEqual(ports, [[18082, 18083], [18084, 18085]])
        services = {(a['port'], a['service']) for a in rows[0]['client_endpoint']['services']}
        self.assertEqual(services, {(18082, 'vespera_http'), (18083, 'vespera_socketio')})
        catalog = self.controller.instrument_catalog(self.controller.status())
        self.assertEqual([(i['link'], i['simultaneous']) for i in catalog], [('connected', True)] * 2)
        self.assertEqual(self.controller.free_radios(), 1)  # Four radios: home, two Vespera, one free.
        from wifi_bridge.instruments import endpoint
        native = endpoint(self.controller.status(), 'vespera-two', 'home')
        self.assertEqual(native['mode'], 'relay')
        self.assertEqual({(p['port'], p['role'], p['protocol']) for p in native['primary']['ports']},
                         {(18084, 'rest', 'http'), (18085, 'stream', 'socketio')})
        self.assertFalse(native['requires_client_route'])

    def test_disabling_one_keeps_the_other_and_its_ports(self):
        self.pair(); self.controller.cycle(0)
        self.api.update('telescopes', {'enabled': False}, ident='primary'); self.controller.cycle(20)
        rows = {p['id']: p for p in self.api.read('telescopes')['telescopes']}
        self.assertFalse(rows['primary']['connection']['connected'])
        self.assertTrue(rows['vespera-two']['connection']['relay_ready'])
        self.assertEqual(rows['vespera-two']['client_ports'], [18084, 18085])


class FakeRelay:
    started = []
    def __init__(self, host, port, transport, target, target_port, factory, counters, **kwargs):
        self.values = (host, port, transport, target, target_port, factory)
        self.closed = False
    def start(self): FakeRelay.started.append(self); return self
    def healthy(self): return not self.closed
    def close(self): self.closed = True


class VesperaNamespaceTests(unittest.TestCase):
    PROFILES = [{'id': 'vespera-one', 'kind': 'vespera', 'isolated': True, 'enabled': True, 'target_ip': '10.0.0.1',
                 'mac': '02:00:00:00:00:02', 'client_port': 18082, 'client_ports': [18082, 18083]},
                {'id': 'vespera-two', 'kind': 'vespera', 'isolated': True, 'enabled': True, 'target_ip': '10.0.0.1',
                 'mac': '02:00:00:00:00:03', 'client_port': 18084, 'client_ports': [18084, 18085]}]
    PATHS = [{'role': 'home', 'address': '192.0.2.10/24', 'device': {'name': 'wlan1'}}]

    def manager(self):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        with patch('wifi_bridge.isolated.linux_boot_id', return_value='test-boot'):
            manager = IsolatedRadios(root.name, lambda args, **kwargs: '')
        for profile in self.PROFILES:
            manager.records[profile['mac']] = {'namespace': namespace_name(profile['mac']), 'profile_id': profile['id'],
                                               'device': {'name': 'wlan-' + profile['id'], 'mac': profile['mac']}}
        # Each upstream socket factory names the namespace it would open in.
        manager.socket_factory = lambda record: ('factory', record['namespace'])
        return manager

    def rows(self, connected=True):
        return [{'id': p['id'], 'connected': connected, 'state': 'online'} for p in self.PROFILES]

    def test_each_relay_reaches_only_its_own_radio_namespace(self):
        manager = self.manager(); FakeRelay.started = []
        with patch('wifi_bridge.isolated.InstrumentRelay', FakeRelay):
            rows = self.rows(); manager.sync(copy.deepcopy(self.PROFILES), rows, self.PATHS)
        relays = {(r.values[1], r.values[4]): r.values[5][1] for r in FakeRelay.started}
        self.assertEqual(relays, {(18082, 8082): namespace_name('02:00:00:00:00:02'), (18083, 8083): namespace_name('02:00:00:00:00:02'),
                                  (18084, 8082): namespace_name('02:00:00:00:00:03'), (18085, 8083): namespace_name('02:00:00:00:00:03')})
        self.assertTrue(all(r.values[3] == '10.0.0.1' for r in FakeRelay.started))
        self.assertTrue(all(row['relay_ready'] for row in rows))

    def test_disconnect_and_restart_close_then_restore_the_same_ports(self):
        manager = self.manager(); FakeRelay.started = []
        with patch('wifi_bridge.isolated.InstrumentRelay', FakeRelay):
            manager.sync(copy.deepcopy(self.PROFILES), self.rows(), self.PATHS)
            first = list(FakeRelay.started)
            rows = self.rows(); rows[1]['connected'] = False
            manager.sync(copy.deepcopy(self.PROFILES), rows, self.PATHS)
            self.assertEqual([r.closed for r in first], [False, False, True, True])
            self.assertFalse(rows[1]['relay_ready'])
            # A restarted service rebuilds relays on the persisted ports.
            restarted = self.manager(); FakeRelay.started = []
            restarted.sync(copy.deepcopy(self.PROFILES), self.rows(), self.PATHS)
        self.assertEqual(sorted(r.values[1] for r in FakeRelay.started), [18082, 18083, 18084, 18085])

    def test_missing_namespace_record_never_falls_back_to_another_radio(self):
        manager = self.manager(); manager.records.pop('02:00:00:00:00:03'); FakeRelay.started = []
        with patch('wifi_bridge.isolated.InstrumentRelay', FakeRelay):
            rows = self.rows(); manager.sync(copy.deepcopy(self.PROFILES), rows, self.PATHS)
        self.assertEqual({r.values[1] for r in FakeRelay.started}, {18082, 18083})
        self.assertFalse(rows[1]['relay_ready'])
        self.assertEqual({a['reason'] for a in rows[1]['client_endpoint']['services']}, {'ENDPOINT_BIND_FAILED'})

    def test_namespace_firewall_admits_both_vespera_ports_only(self):
        script = namespace_firewall('10.0.0.1', 'tcp', (8082, 8083))
        self.assertIn('ip daddr 10.0.0.1 tcp dport { 8082, 8083 } accept', script)
        self.assertNotIn('4030', script)
        self.assertIn('tcp dport 4030 accept', namespace_firewall('192.168.4.1', 'tcp', 4030))

    def test_connect_writes_one_namespace_with_both_ports(self):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup); calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return 'udhcpc' if args == ['busybox', '--list'] else 'wpa_state=COMPLETED' if args[0] == 'wpa_cli' else ''
        with patch('wifi_bridge.isolated.linux_boot_id', return_value='test-boot'):
            manager = IsolatedRadios(root.name, runner)
        device = {'name': 'wlan-test', 'mac': '02:00:00:00:00:02'}
        profile = dict(self.PROFILES[0], ssid='Vespera-TEST', password='')
        with patch('wifi_bridge.isolated.shutil.which', return_value='/test/tool'), \
             patch('wifi_bridge.isolated.phy_name', return_value='phy7'), \
             patch.object(manager, '_spawn'), patch.object(manager, 'address', return_value='10.0.0.2/24'):
            manager.connect('telescope-vespera-one', device, profile)
        rules = [kw['input'] for args, kw in calls if 'input' in kw]
        self.assertEqual(len(rules), 1)
        self.assertIn('tcp dport { 8082, 8083 }', rules[0])
        self.assertEqual(manager.records[device['mac']]['namespace'], namespace_name(device['mac']))


class SocketIoRelayTests(unittest.TestCase):
    def test_http_upgrade_and_websocket_frames_pass_unchanged(self):
        server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(1); server.settimeout(3)
        self.addCleanup(server.close)
        request = (b'GET /socket.io/?EIO=4&transport=websocket HTTP/1.1\r\nHost: 10.0.0.1:8083\r\n'
                   b'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGVzdC1rZXktMDAwMDAwMA==\r\n'
                   b'Sec-WebSocket-Version: 13\r\n\r\n')
        frame = b'\x81\x05\x34\x30\x7b\x7d\x0a'  # Opaque text frame from the instrument.
        seen = []
        def serve():
            client, _ = server.accept()
            with client:
                data = b''
                while b'\r\n\r\n' not in data: data += client.recv(1024)
                seen.append(data)
                client.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n' + frame)
                seen.append(client.recv(64))
        thread = threading.Thread(target=serve, daemon=True); thread.start()
        relay = InstrumentRelay('127.0.0.1', 0, 'tcp', '127.0.0.1', server.getsockname()[1],
                                lambda kind: socket.socket(socket.AF_INET, kind)).start()
        self.addCleanup(relay.close)
        with socket.create_connection(('127.0.0.1', relay.port), timeout=4) as client:
            client.sendall(request)
            received = b''
            while not received.endswith(frame): received += client.recv(1024)
            masked = b'\x81\x82\x01\x02\x03\x04\x33\x31'
            client.sendall(masked)
            thread.join(3)
        self.assertEqual(seen, [request, masked])
        self.assertTrue(received.startswith(b'HTTP/1.1 101'))


if __name__ == '__main__':
    unittest.main()
