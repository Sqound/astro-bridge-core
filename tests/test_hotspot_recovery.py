"""Synthetic AP outages recover within a bounded reconciliation cycle."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

from test_bridge import Fixture
from helpers.contract_schema import validate
from wifi_bridge.api import MobileAPI
from wifi_bridge.network import LinuxNetwork, NetworkError


class HotspotRecoveryTests(Fixture):
    def setUp(self):
        super().setUp()
        self.net.radios = 5
        self.devices = self.net.devices()
        self.controller.update(devices=self.devices)
        self.configured()
        self.preferred = self.devices[3]
        self.fallback = self.devices[2]
        self.store.config['hotspot'] = {'enabled': True, 'mac': self.preferred['mac']}

    def events(self):
        return [e for e in self.controller.event_store.events if e['type'].startswith('hotspot_')]

    def test_unplug_replug_restores_preference_with_or_without_role_reservation(self):
        for reserved in (False, True):
            with self.subTest(reserved=reserved):
                self.store.config['interface_roles'][self.preferred['mac']] = 'hotspot' if reserved else 'auto'
                self.controller.cycle(0)
                remaining = [d for d in self.devices if d != self.preferred]
                with patch.object(self.net, 'devices', return_value=remaining):
                    self.controller.cycle(10)
                self.assertTrue(self.controller.state['hotspot']['active'])
                self.assertEqual(self.controller.state['hotspot']['mac'], self.fallback['mac'])
                self.assertTrue(self.net.connected('home', self.devices[1]))
                self.assertEqual(self.store.config['hotspot']['mac'], self.preferred['mac'])
                self.controller.cycle(20)
                self.assertEqual(self.controller.state['hotspot']['mac'], self.preferred['mac'])
                self.assertEqual(self.store.config['interface_roles'][self.preferred['mac']], 'hotspot' if reserved else 'auto')

    def test_legacy_no_home_bootstrap_preserves_absent_preference_and_returns(self):
        self.store.config.update(home={}, telescopes=[], telescope={})
        self.store.config.pop('interface_roles', None)
        self.store.save(self.store.config)
        remaining = [d for d in self.devices if d != self.preferred]
        with patch.object(self.net, 'devices', return_value=remaining):
            self.controller.cycle(0)
        self.assertTrue(self.controller.state['hotspot']['active'])
        self.assertEqual(self.controller.state['hotspot']['mac'], self.devices[0]['mac'])
        self.assertEqual(self.store.config['hotspot']['mac'], self.preferred['mac'])
        self.assertEqual(self.store.config['interface_roles'], {self.preferred['mac']: 'hotspot'})
        reopened = type(self.store)(self.tmp.name, self.devices[0]['mac'])
        self.assertEqual(reopened.config['hotspot']['mac'], self.preferred['mac'])
        self.assertEqual(reopened.config['interface_roles'], {self.preferred['mac']: 'hotspot'})
        self.controller.cycle(10)
        self.assertTrue(self.controller.state['hotspot']['active'])
        self.assertEqual(self.controller.state['hotspot']['mac'], self.preferred['mac'])
        self.assertEqual(self.store.config['interface_roles'], {self.preferred['mac']: 'hotspot'})

    def test_start_failure_uses_free_radio_then_returns_to_preference(self):
        original = self.net.hotspot
        attempts = []
        def start(device, *args):
            attempts.append(device['mac'])
            if len(attempts) == 1:
                raise NetworkError('synthetic start failure')
            return original(device, *args)
        with patch.object(self.net, 'hotspot', side_effect=start):
            self.controller.cycle(0)
        self.assertEqual(attempts, [self.preferred['mac'], self.fallback['mac']])
        self.assertEqual(self.controller.state['hotspot']['mac'], self.fallback['mac'])
        self.assertEqual([e['type'] for e in self.events()], ['hotspot_unavailable', 'hotspot_restored'])
        spec = MobileAPI.specification()
        for event in self.events():
            validate(event, spec['components']['schemas']['ConnectionEvent'], spec)
        self.controller.cycle(10)
        self.assertEqual(self.controller.state['hotspot']['mac'], self.preferred['mac'])
        count = len(self.events())
        self.controller.cycle(20)
        self.controller.status()
        self.assertEqual(len(self.events()), count)

    def test_failed_migration_keeps_a_confirmed_surviving_fallback(self):
        remaining = [d for d in self.devices if d != self.preferred]
        with patch.object(self.net, 'devices', return_value=remaining):
            self.controller.cycle(0)
        with patch.object(self.net, 'hotspot', side_effect=NetworkError('synthetic early failure')):
            self.controller.cycle(10)
        self.assertEqual(self.controller.state['hotspot']['mac'], self.fallback['mac'])
        self.assertTrue(self.controller.state['hotspot']['active'])
        self.assertEqual(self.events(), [])

    def test_automatic_selection_retries_another_candidate(self):
        self.store.config['hotspot']['mac'] = ''
        original = self.net.hotspot
        attempts = []
        def start(device, *args):
            attempts.append(device['mac'])
            if len(attempts) == 1: raise NetworkError('synthetic start failure')
            return original(device, *args)
        with patch.object(self.net, 'hotspot', side_effect=start):
            self.controller.cycle(0)
        self.assertEqual(attempts, [self.fallback['mac'], self.preferred['mac']])
        self.assertTrue(self.controller.state['hotspot']['active'])

    def test_outage_respects_busy_incompatible_and_other_role_reservations(self):
        self.controller.cycle(0)
        self.store.config['interface_roles'][self.fallback['mac']] = 'telescope'
        remaining = copy.deepcopy([d for d in self.devices if d != self.preferred])
        remaining[-1]['ap'] = False
        with patch.object(self.net, 'devices', return_value=remaining), patch.object(self.net, 'hotspot') as start:
            self.controller.cycle(10)
            self.controller.cycle(20)
        start.assert_not_called()
        self.assertFalse(self.controller.state['hotspot']['active'])
        self.assertEqual([e['type'] for e in self.events()], ['hotspot_unavailable'])
        self.controller.cycle(30)
        self.assertEqual([e['type'] for e in self.events()], ['hotspot_unavailable', 'hotspot_restored'])
        encoded = str(self.events())
        self.assertNotIn(self.preferred['mac'], encoded)
        self.assertNotIn('synthetic start failure', encoded)

    def test_all_start_failures_retry_next_cycle_without_losing_home(self):
        with patch.object(self.net, 'hotspot', side_effect=NetworkError('synthetic start failure')):
            self.controller.cycle(0)
        self.assertFalse(self.controller.state['hotspot']['active'])
        self.assertTrue(self.controller.state['home_ip'])
        self.assertEqual(self.events()[-1]['reason'], 'HOTSPOT_START_FAILED')
        self.controller.cycle(10)
        self.assertEqual(self.controller.state['hotspot']['mac'], self.preferred['mac'])
        self.assertEqual(self.events()[-1]['type'], 'hotspot_restored')


class LinuxHotspotStartRecoveryTests(Fixture):
    def test_failed_hostapd_start_releases_owned_radio_for_retry(self):
        net = LinuxNetwork(self.tmp.name)
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if args[:2] == ['systemctl', 'restart']:
                raise NetworkError('synthetic service failure')
            return ''
        with patch('wifi_bridge.network.run', side_effect=run), patch.object(net, '_wait_nm_unmanaged'):
            with self.assertRaises(NetworkError):
                net.hotspot(self.devices[0], 'Recovery-DEMO', 'synthetic-ap-pass')
        self.assertFalse(Path(self.tmp.name, 'hotspot.json').exists())
        self.assertIn(['systemctl', 'stop', 'wifi-bridge-ap', 'wifi-bridge-dhcp'], calls)
        self.assertIn(['nmcli', 'device', 'set', 'wlan0', 'managed', 'yes'], calls)
        with patch('wifi_bridge.network.run', return_value=''), patch.object(net, 'connected', return_value=True), patch.object(net, '_wait_nm_unmanaged'):
            net.hotspot(self.devices[0], 'Recovery-DEMO', 'synthetic-ap-pass')
        self.assertEqual(json.loads(Path(self.tmp.name, 'hotspot.json').read_text()),
                         {'interface': self.devices[0]['name'], 'mac': self.devices[0]['mac']})
