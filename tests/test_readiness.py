"""Service evidence is bounded, read-only and separate from relay availability."""
import copy
import json
import socket
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_bridge import Fixture
from wifi_bridge.api import MobileAPI
from wifi_bridge.plugins import plugin, validate_manifest
from wifi_bridge.readiness import ServiceReadiness, empty_service, service_probe


class ServiceReadinessTests(Fixture):
    def profile(self, kind='zwo_mount'):
        return {'id': 'mount', 'mac': self.devices[0]['mac'], 'target_ip': '192.168.4.1', 'kind': kind}

    def test_tcp_handshake_success_and_timeout_use_bounded_bound_socket_without_payload(self):
        for kind, port in [('zwo_mount', 4030), ('seestar', 4700)]:
            for error in (None, TimeoutError(), ConnectionRefusedError()):
                stream = MagicMock(); stream.__enter__.return_value = stream
                stream.connect.side_effect = error
                with self.subTest(kind=kind, error=type(error).__name__), patch('wifi_bridge.readiness.socket.socket', return_value=stream), patch.object(socket, 'SO_BINDTODEVICE', 25, create=True):
                    if error:
                        with self.assertRaises(OSError): service_probe({'name': 'mount0'}, '192.168.4.2/24', '192.168.4.1', kind)
                    else:
                        self.assertTrue(service_probe({'name': 'mount0'}, '192.168.4.2/24', '192.168.4.1', kind))
                stream.settimeout.assert_called_once_with(2)
                stream.bind.assert_called_once_with(('192.168.4.2', 0))
                stream.connect.assert_called_once_with(('192.168.4.1', port))
                stream.send.assert_not_called(); stream.sendall.assert_not_called()
                stream.__exit__.assert_called_once()

    def test_udp_uses_existing_strict_synscan_identity_and_checks_reply_origin(self):
        for reply, origin, expected in [(b'=020600\r', ('192.168.4.1', 11880), True),
                (b'!1\r', ('192.168.4.1', 11880), False),
                (b'=020600\r', ('192.168.4.9', 11880), False)]:
            stream = MagicMock(); stream.__enter__.return_value = stream
            stream.recvfrom.return_value = reply, origin
            with patch('wifi_bridge.mounts.socket.socket', return_value=stream), patch.object(socket, 'SO_BINDTODEVICE', 25, create=True):
                self.assertEqual(service_probe({'name': 'mount0'}, '192.168.4.2/24', '192.168.4.1', 'synscan'), expected)
            stream.sendto.assert_called_once_with(b':e1\r', ('192.168.4.1', 11880))
            stream.recvfrom.assert_called_once_with(65)
        stream.recvfrom.side_effect = TimeoutError()
        with patch('wifi_bridge.mounts.socket.socket', return_value=stream), patch.object(socket, 'SO_BINDTODEVICE', 25, create=True):
            with self.assertRaises(TimeoutError): service_probe({'name': 'mount0'}, '192.168.4.2/24', '192.168.4.1', 'synscan')

    def test_no_offlink_probe_and_unprobed_families_never_open_sockets(self):
        with patch('wifi_bridge.readiness.socket.socket') as create:
            for target in ('192.168.5.1', '192.168.4.2', '192.168.4.0', '192.168.4.255', '127.0.0.1'):
                self.assertFalse(service_probe({'name': 'mount0'}, '192.168.4.2/24', target, 'zwo_mount'))
            for kind in ('dwarf', 'generic'):
                self.assertIsNone(service_probe({'name': 'mount0'}, '192.168.4.2/24', '192.168.4.1', kind))
            create.assert_not_called()

    def test_throttle_failures_active_suppression_and_recovery(self):
        network = MagicMock(); network.activity_status.return_value = {'profiles': {}}
        manager = ServiceReadiness(network)
        profile = self.profile()
        with patch('wifi_bridge.readiness.service_probe', side_effect=[False, False, True]) as probe:
            first = manager.check(profile, self.devices[0], '192.168.4.2/24', 0)
            self.assertEqual(first, dict(state='not_answering', checked_at_seq=1, method='tcp_connect', failures=1))
            self.assertEqual(manager.check(profile, self.devices[0], '192.168.4.2/24', 29), first)
            self.assertEqual(probe.call_count, 1)
            second = manager.check(profile, self.devices[0], '192.168.4.2/24', 30)
            self.assertEqual(second['failures'], 2)
            network.activity_status.return_value = {'profiles': {'mount': {'state': 'active'}}}
            self.assertEqual(manager.check(profile, self.devices[0], '192.168.4.2/24', 60), second)
            self.assertEqual(probe.call_count, 2)
            network.activity_status.return_value = {'profiles': {}}
            recovered = manager.check(profile, self.devices[0], '192.168.4.2/24', 61)
            self.assertEqual(recovered, dict(state='ready', checked_at_seq=3, method='tcp_connect', failures=0))

    def test_unknown_no_concurrent_probe_per_radio_and_demo_never_contacts_mount(self):
        network = MagicMock(); network.activity_status.return_value = {'profiles': {}}
        network.isolated = None
        manager = ServiceReadiness(network)
        profile = self.profile()
        with patch('wifi_bridge.readiness.service_probe', side_effect=AttributeError()) as probe:
            manager.inflight.add(profile['mac'])
            self.assertEqual(manager.check(profile, self.devices[0], '192.168.4.2/24', 0)['state'], 'not_probed')
            probe.assert_not_called()
            manager.inflight.clear()
            self.assertEqual(manager.check(profile, self.devices[0], '192.168.4.2/24', 1)['state'], 'unknown')
            self.assertFalse(manager.inflight)
        with patch('wifi_bridge.readiness.service_probe') as probe:
            self.assertEqual(ServiceReadiness(network, demo=True).check(profile, self.devices[0], '192.168.4.2/24', 0)['state'], 'not_probed')
            probe.assert_not_called()

    def test_isolated_probe_uses_owned_namespace_or_reports_unknown(self):
        network = MagicMock(); network.activity_status.return_value = {'profiles': {}}
        profile = dict(self.profile(), isolated=True)
        network.isolated.record_for.return_value = {'namespace': 'astro-test-radio'}
        manager = ServiceReadiness(network)
        with patch('wifi_bridge.isolated.in_namespace') as context, patch('wifi_bridge.readiness.service_probe', return_value=True) as probe:
            self.assertEqual(manager.check(profile, self.devices[0], '192.168.4.2/24', 0)['state'], 'ready')
            context.assert_called_once_with('/run/netns/astro-test-radio')
            context.return_value.__enter__.assert_called_once()
            probe.assert_called_once()
        network.isolated.record_for.return_value = None
        with patch('wifi_bridge.readiness.service_probe') as probe:
            self.assertEqual(manager.check(profile, self.devices[0], '192.168.4.2/24', 30)['state'], 'unknown')
            probe.assert_not_called()

    def test_concurrent_profile_on_same_radio_never_opens_second_probe(self):
        network = MagicMock(); network.activity_status.return_value = {'profiles': {}}
        manager = ServiceReadiness(network)
        started, release = threading.Event(), threading.Event()
        results = []
        def check_probe(*args):
            started.set()
            if not release.wait(5): raise AssertionError('Probe fixture deadline exceeded')
            return True
        def check():
            results.append(manager.check(self.profile(), self.devices[0], '192.168.4.2/24', 0))
        with patch('wifi_bridge.readiness.service_probe', side_effect=check_probe) as probe:
            worker = threading.Thread(target=check)
            worker.start()
            try:
                self.assertTrue(started.wait(5))
                second = manager.check(dict(self.profile(), id='other'), self.devices[0], '192.168.4.2/24', 0)
                self.assertEqual(second['state'], 'not_probed')
                self.assertEqual(probe.call_count, 1)
            finally:
                release.set(); worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results[0]['state'], 'ready')

    def test_reconnect_or_changed_target_does_not_bypass_profile_throttle(self):
        network = MagicMock(); network.activity_status.return_value = {'profiles': {}}
        manager = ServiceReadiness(network)
        profile = self.profile()
        with patch('wifi_bridge.readiness.service_probe', return_value=True) as probe:
            manager.check(profile, self.devices[0], '192.168.4.2/24', 0)
            manager.disconnected(profile['id'])
            self.assertEqual(manager.check(profile, self.devices[0], '192.168.4.2/24', 1)['state'], 'not_probed')
            changed = dict(profile, target_ip='192.168.4.3')
            self.assertEqual(manager.check(changed, self.devices[0], '192.168.4.2/24', 2)['state'], 'not_probed')
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(manager.check(changed, self.devices[0], '192.168.4.2/24', 30)['state'], 'ready')
            self.assertEqual(probe.call_count, 2)

    def test_controller_dead_service_keeps_association_relay_and_deduplicates_events(self):
        self.configured()
        self.store.config['telescopes'][0]['kind'] = 'seestar'
        # Exercise real probe scheduling against simulated network operations only.
        self.controller.service_readiness.demo = False
        with patch('wifi_bridge.readiness.service_probe', return_value=False) as probe:
            self.controller.cycle(0)
            row = self.controller.status()['telescopes'][0]
            self.assertTrue(row['connected']); self.assertTrue(row['relay_ready'])
            self.assertEqual(row['service']['state'], 'not_answering')
            cursor = self.controller.event_store.sequence
            self.controller.cycle(15); self.controller.cycle(30)
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(self.controller.event_store.sequence, cursor)
            self.assertEqual(len([e for e in self.controller.event_store.events if e['type'] == 'service_changed']), 1)
            self.assertEqual(self.controller.status()['telescopes'][0]['service']['failures'], 2)
        with patch('wifi_bridge.readiness.service_probe', return_value=True): self.controller.cycle(60)
        self.assertEqual(self.controller.event_store.sequence, cursor + 1)
        self.assertEqual(self.controller.event_store.events[-1]['reason'], 'SERVICE_READY')
        self.assertTrue(self.controller.status()['telescopes'][0]['connected'])

    def test_api_exposes_service_schema_and_pending_default(self):
        self.configured()
        api = MobileAPI(self.controller)
        self.assertEqual(api.telescope('primary')['connection']['service'], empty_service())
        self.controller.cycle(0)
        self.assertEqual(api.telescope('primary')['connection']['service']['state'], 'ready')
        spec = json.loads(Path('wifi_bridge/static/openapi.json').read_text())
        schema = spec['components']['schemas']['ServiceReadiness']
        self.assertEqual(set(schema['required']), {'state', 'checked_at_seq', 'method', 'failures'})
        self.assertEqual(spec['components']['schemas']['Connection']['properties']['service'], {'$ref': '#/components/schemas/ServiceReadiness'})

    def test_manifest_adapters_are_curated_and_transport_checked(self):
        self.assertEqual(plugin('zwo_mount')['readiness'], 'tcp_connect')
        self.assertEqual(plugin('seestar')['readiness'], 'tcp_connect')
        self.assertEqual(plugin('synscan')['readiness'], 'udp_identity')
        for method in ('udp_identity', 'arbitrary'):
            value = copy.deepcopy(plugin('zwo_mount')); value['readiness'] = method
            with self.assertRaises(ValueError): validate_manifest(value)
