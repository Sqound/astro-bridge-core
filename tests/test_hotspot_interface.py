"""Persistent AP adapter selection and conservative stale ownership recovery."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.api import ApiError, MobileAPI
from wifi_bridge.config import Invalid, Store, validate_config
from wifi_bridge.network import LinuxNetwork, NetworkError


class HotspotInterfaceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.net.radios = 4
        self.devices = self.net.devices()
        self.controller.update(devices=self.devices)
        self.configured()
        self.api = MobileAPI(self.controller)

    def select(self, mac, **kwargs):
        return self.api.update('hotspot', {'mac': mac, **kwargs}, expected='"' + self.api.revision() + '"')

    def test_selection_uses_permanent_mac_survives_reload_and_legacy_patch(self):
        mac = self.devices[3]['mac']
        self.select(mac.upper(), enabled=True)
        self.api.update('hotspot', {'ssid': 'Observatory'}, expected='"' + self.api.revision() + '"')
        self.controller.cycle(0)
        self.assertEqual(self.controller.state['hotspot']['mac'], mac)
        self.assertEqual(Store(self.tmp.name, self.devices[0]['mac']).config['hotspot']['mac'], mac)
        self.assertEqual(self.api.read('hotspot')['hotspot']['mac'], mac)
        before = copy.deepcopy(self.store.config['telescopes'])
        self.select('')
        self.controller.cycle(10)
        self.assertEqual(self.store.config['telescopes'], before)
        self.assertEqual(self.store.config['hotspot']['mac'], '')
        self.assertEqual(self.controller.state['hotspot']['mac'], mac)

    def test_selection_requires_current_revision_and_respects_protection(self):
        mac = self.devices[2]['mac']
        with self.assertRaises(ApiError) as error:
            self.api.update('hotspot', {'mac': mac})
        self.assertEqual(error.exception.code, 'PRECONDITION_REQUIRED')
        self.store.config['telescopes'][0]['owner_locked'] = True
        before = copy.deepcopy(self.store.config)
        with self.assertRaises(ApiError) as error:
            self.select(mac)
        self.assertEqual(error.exception.code, 'CONNECTION_PROTECTED')
        self.assertEqual(self.store.config, before)

    def test_selected_mac_still_used_after_interface_renumbering(self):
        self.select(self.devices[3]['mac'], enabled=True)
        renamed = copy.deepcopy(self.devices)
        renamed[3]['name'] = 'wlan7'
        with patch.object(self.net, 'devices', return_value=renamed):
            self.controller.cycle(0)
        self.assertEqual(self.controller.state['hotspot']['mac'], self.devices[3]['mac'])
        self.assertEqual(self.controller.state['hotspot']['interface'], 'wlan7')

    def test_invalid_incompatible_home_and_active_telescope_assignments_rejected(self):
        values = [None, 5, 'broken', 'aa:bb:cc:dd:ee:ff', self.devices[1]['mac'], self.devices[0]['mac']]
        for mac in values:
            data = copy.deepcopy(self.store.config)
            data['hotspot'] = {'enabled': True, 'mac': mac}
            with self.subTest(mac=mac), self.assertRaises(Invalid):
                validate_config(data, self.store.config, self.devices)
        devices = copy.deepcopy(self.devices)
        devices[2]['ap'] = False
        data = copy.deepcopy(self.store.config)
        data['hotspot'] = {'enabled': True, 'mac': devices[2]['mac']}
        with self.assertRaises(Invalid):
            validate_config(data, self.store.config, devices)

    def test_missing_selected_adapter_retained_and_no_silent_normal_fallback(self):
        mac = self.devices[3]['mac']
        self.select(mac, enabled=True)
        remaining = self.devices[:3]
        data = copy.deepcopy(self.store.config)
        value = validate_config(data, self.store.config, remaining)
        self.assertEqual(value['hotspot']['mac'], mac)
        self.controller.update(devices=remaining)
        with patch.object(self.net, 'devices', return_value=remaining):
            self.controller.cycle(0)
        self.assertFalse(self.controller.state['hotspot']['active'])
        self.assertEqual(self.controller.state['hotspot']['reason'], 'selected_adapter_unavailable')
        self.assertTrue(self.controller.state['telescopes'][0]['relay_ready'])

    def test_migration_keeps_home_and_instrument_and_is_idempotent(self):
        self.select(self.devices[2]['mac'], enabled=True)
        self.controller.cycle(0)
        self.select(self.devices[3]['mac'])
        with patch.object(self.net, 'disconnect', wraps=self.net.disconnect) as disconnect, patch.object(self.net, 'hotspot', wraps=self.net.hotspot) as start:
            self.controller.cycle(10)
            self.controller.cycle(20)
        self.assertEqual(start.call_count, 1)
        self.assertTrue(all(call.args[0] == 'setup' for call in disconnect.call_args_list))
        self.assertEqual(self.controller.state['hotspot']['mac'], self.devices[3]['mac'])
        self.assertTrue(self.controller.state['telescopes'][0]['relay_ready'])
        self.assertTrue(self.net.connected('home', self.devices[1]))

    def test_busy_selected_adapter_preserves_existing_hotspot(self):
        self.select(self.devices[2]['mac'], enabled=True)
        self.controller.cycle(0)
        self.select(self.devices[3]['mac'])
        idle = self.net.radio_idle
        with patch.object(self.net, 'radio_idle', side_effect=lambda d: d['mac'] != self.devices[3]['mac'] and idle(d)), patch.object(self.net, 'hotspot') as start:
            self.controller.cycle(10)
        start.assert_not_called()
        self.assertEqual(self.controller.state['hotspot']['mac'], self.devices[2]['mac'])
        self.assertEqual(self.controller.state['hotspot']['reason'], 'selected_adapter_unavailable')

    def test_recovery_fallback_never_rewrites_missing_preference(self):
        mac = self.devices[3]['mac']
        self.select(mac, enabled=True)
        remaining = self.devices[:3]
        self.controller.update(devices=remaining)
        ap = self.controller._ap(remaining, {self.devices[0]['mac']}, emergency=True)
        self.assertEqual(ap['mac'], self.devices[2]['mac'])
        self.assertEqual(self.store.config['hotspot']['mac'], mac)

    def test_pinned_hotspot_radio_reserved_before_start(self):
        self.select(self.devices[2]['mac'], enabled=True)
        interface = next(d for d in self.api.read('interfaces')['interfaces'] if d['mac'] == self.devices[2]['mac'])
        self.assertFalse(interface['can_assign_telescope'])
        self.assertIn({'role': 'hotspot'}, interface['assignments'])


class StaleHotspotTests(Fixture):
    def setUp(self):
        super().setUp()
        self.linux = LinuxNetwork(self.tmp.name)
        self.marker = Path(self.tmp.name, 'hotspot.json')
        self.marker.write_text(json.dumps({'interface': self.devices[0]['name']}))
        self.calls = []

    def observe(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == 'systemctl': return 'inactive\ninactive\n'
        if args[0] == 'nmcli': return '30 (disconnected)'
        if args[-1] == 'link': return 'Not connected.\n'
        return 'Interface wlan0\n\ttype managed\n'

    def test_stopped_idle_owned_radio_released_without_network_mutation(self):
        with patch('wifi_bridge.network.run', side_effect=self.observe):
            self.assertTrue(self.linux.release_stale_hotspot(self.devices))
            self.assertTrue(self.linux.radio_idle(self.devices[0]))
            self.assertFalse(self.linux.release_stale_hotspot(self.devices))
        self.assertFalse(self.marker.exists())
        self.assertTrue(all('stop' not in c and 'restart' not in c and 'set' not in c for c in self.calls))

    def test_active_partial_unknown_associated_ap_or_unmanaged_retains_marker(self):
        for services, state, link, info in [
            ('active\nactive\n', '30', 'Not connected.', 'type managed'),
            ('active\ninactive\n', '30', 'Not connected.', 'type managed'),
            ('unknown\nunknown\n', '30', 'Not connected.', 'type managed'),
            ('inactive\ninactive\n', '100', 'Connected to a device', 'type managed'),
            ('inactive\ninactive\n', '10', 'Not connected.', 'type managed'),
            ('inactive\ninactive\n', '30', 'Not connected.', 'type AP'),
        ]:
            def run(args, **kwargs):
                if args[0] == 'systemctl': return services
                if args[0] == 'nmcli': return state
                return link if args[-1] == 'link' else info
            with self.subTest(services=services, state=state, info=info), patch('wifi_bridge.network.run', side_effect=run):
                self.assertFalse(self.linux.release_stale_hotspot(self.devices))
                self.assertTrue(self.marker.exists())
        with patch('wifi_bridge.network.run', side_effect=NetworkError('observation unavailable')):
            self.assertFalse(self.linux.release_stale_hotspot(self.devices))
        with patch('wifi_bridge.network.run') as run:
            self.assertFalse(self.linux.release_stale_hotspot([]))
            run.assert_not_called()

    def test_changed_marker_during_observation_is_preserved(self):
        def run(args, **kwargs):
            if args[-1] == 'info': self.marker.write_text('{"interface":"new-owner"}')
            return self.observe(args, **kwargs)
        with patch('wifi_bridge.network.run', side_effect=run):
            self.assertFalse(self.linux.release_stale_hotspot(self.devices))
        self.assertEqual(json.loads(self.marker.read_text())['interface'], 'new-owner')
