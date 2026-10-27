"""Connection outcomes, redacted replay and bounded journal recovery."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.config import profiles, validate_config
from wifi_bridge.events import EventStore
from wifi_bridge.network import NetworkError


class EventStoreTests(unittest.TestCase):
    def append(self, store, name='Synthetic instrument'):
        return store.append('connected', operation_id='op-test', profile_id='primary',
                            profile_name=name, radio_id='radio-opaque', cause='RECONCILE',
                            reason='READY', outcome='ready', associated=True, ready=True)

    def test_retention_replay_gap_restart_and_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.json'
            store = EventStore(path, capacity=4)
            for _ in range(6):
                self.append(store)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual([e['sequence'] for e in store.read()['events']], [3, 4, 5, 6])
            page = store.read(3, store.boot_id, 2)
            self.assertEqual([e['sequence'] for e in page['events']], [4, 5])
            self.assertTrue(page['has_more']); self.assertEqual(page['next_cursor'], 5)
            self.assertEqual(store.read(0, store.boot_id)['reset_reason'], 'RETENTION_GAP')
            self.assertEqual(store.read(99, store.boot_id)['reset_reason'], 'CURSOR_AHEAD')
            restarted = EventStore(path, capacity=4)
            self.assertNotEqual(restarted.boot_id, store.boot_id)
            self.assertEqual(restarted.read(6, store.boot_id)['reset_reason'], 'BOOT_CHANGED')
            self.assertEqual(self.append(restarted)['sequence'], 7)
            self.assertEqual(restarted.read(6, restarted.boot_id)['events'][0]['sequence'], 7)

    def test_corrupt_shape_does_not_stop_startup_or_force_endless_cursor_reset(self):
        for bad in ('broken', '[]', 'null', '{"format":1,"sequence":2,"events":{}}',
                    '{"format":1,"sequence":2,"events":[null]}'):
            with self.subTest(journal=bad), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'events.json'; path.write_text(bad)
                store = EventStore(path)
                self.assertFalse(store.history_available)
                self.assertEqual(store.read()['reset_reason'], 'HISTORY_UNAVAILABLE')
                cursor = self.append(store)['sequence']
                self.assertIsNone(store.read(cursor, store.boot_id)['reset_reason'])

    def test_invalid_cursor_and_persistence_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / 'events.json')
            for arguments in ({'after': -1}, {'after': True}, {'after': 2**64}, {'limit': 101}, {'boot_id': '../path'}):
                with self.assertRaises(ValueError): store.read(**arguments)
            with patch('wifi_bridge.events.atomic_json', side_effect=OSError('disk full')):
                self.append(store)
            self.assertFalse(store.read()['persistent'])
            self.assertEqual(store.read()['events'][0]['clock_status'], 'unverified')


class ConnectionEventTests(Fixture):
    def events(self, after=0):
        return [e for e in self.controller.event_store.read(after, self.controller.event_store.boot_id, 100)['events']]

    def test_acceptance_and_polling_never_claim_success_before_reconcile(self):
        self.configured(); self.controller.cycle(0)
        cursor = self.controller.event_store.sequence
        result = self.controller.telescope_action({'id': 'primary', 'action': 'disconnect'})
        self.assertTrue(result['operation_id'])
        self.assertEqual([e['type'] for e in self.events(cursor)], ['requested'])
        for _ in range(3): self.controller.status(); self.controller.event_store.read()
        self.assertEqual([e['type'] for e in self.events(cursor)], ['requested'])
        self.controller.cycle(5)
        changes = self.events(cursor)
        self.assertIn('disconnected', [e['type'] for e in changes])
        self.assertEqual({e['operation_id'] for e in changes}, {result['operation_id']})
        cursor = self.controller.event_store.sequence; self.controller.cycle(10)
        self.assertEqual(self.events(cursor), [])

    def test_association_service_readiness_and_restoration_are_distinct(self):
        self.configured()
        with patch.object(self.net, 'probe', return_value=False): self.controller.cycle(0)
        initial = self.events()
        self.assertIn('associated', [e['type'] for e in initial])
        self.assertNotIn('connected', [e['type'] for e in initial])
        self.assertEqual(initial[-1]['reason'], 'SERVICE_UNAVAILABLE')
        self.controller.cycle(5)
        self.assertEqual(self.events()[-1]['type'], 'connected')
        with patch.object(self.net, 'probe', return_value=False): self.controller.cycle(10)
        self.controller.cycle(15)
        self.assertEqual(self.events()[-1]['type'], 'restored')

    def test_failed_association_and_firewall_never_emit_connected(self):
        self.configured(lambda c: c['telescope'].update(ssid='ECHEC'))
        self.controller.cycle(0)
        self.assertIn('ASSOCIATION_FAILED', [e['reason'] for e in self.events()])
        self.assertNotIn('connected', [e['type'] for e in self.events()])
        self.configured()
        def firewall(**options):
            if options.get('telescopes'): raise NetworkError('private-network-secret')
        with patch.object(self.net, 'firewall', side_effect=firewall), self.assertRaises(NetworkError):
            self.controller.cycle(20)
        self.assertIn('RECONCILE_FAILED', [e['reason'] for e in self.events()])
        self.assertNotIn('connected', [e['type'] for e in self.events()])
        self.assertNotIn('private-network-secret', json.dumps(self.events()))

    def test_explicit_move_and_replacement_share_operation_without_false_restore(self):
        self.configured(); self.net.radios = 4; devices = self.net.devices()
        self.controller.update(devices=devices)
        value = copy.deepcopy(self.store.config)
        value['telescopes'] = copy.deepcopy(profiles(value))
        value['telescopes'].append(dict(value['telescopes'][0], id='second', name='Other instrument',
                                       mac=devices[2]['mac'], target_ip='192.168.4.1', enabled=True))
        self.store.save(validate_config(value, self.store.config, devices)); self.controller.cycle(0)
        cursor = self.controller.event_store.sequence
        result = self.controller.telescope_action({'id': 'primary', 'action': 'connect', 'mac': devices[2]['mac']})
        self.controller.cycle(5)
        changes = self.events(cursor)
        self.assertEqual({e['operation_id'] for e in changes}, {result['operation_id']})
        self.assertEqual({e['profile_id'] for e in changes if e['type'] == 'disconnected'}, {'primary', 'second'})
        self.assertEqual(changes[-1]['type'], 'connected')
        self.assertNotIn('restored', [e['type'] for e in changes])
        encoded = json.dumps(changes)
        for secret in [d['mac'] for d in devices] + ['10.0.0.1', '192.168.4.1', 'vespera-test-pass', 'maison-test-pass', 'Vespera-ABC']:
            self.assertNotIn(secret, encoded)

    def test_unplug_and_real_association_loss_are_observed_even_with_quick_recovery(self):
        self.configured(); self.controller.cycle(0)
        cursor = self.controller.event_store.sequence
        with patch.object(self.net, 'devices', return_value=self.devices[1:]): self.controller.cycle(5)
        changes = self.events(cursor)
        self.assertIn('disconnected', [e['type'] for e in changes])
        self.assertEqual(changes[-1]['reason'], 'RADIO_UNAVAILABLE')
        self.controller.cycle(20)
        self.assertEqual(self.events()[-1]['type'], 'restored')
        cursor = self.controller.event_store.sequence; self.net.disconnect('telescope')
        self.controller.cycle(40)
        self.assertIn('disconnected', [e['type'] for e in self.events(cursor)])
        self.assertEqual(self.events()[-1]['type'], 'restored')

    def test_deleted_state_is_pruned_only_after_disassociation_is_confirmed(self):
        self.configured(); self.controller.cycle(0)
        cursor = self.controller.event_store.sequence
        self.controller.telescope_action({'id': 'primary', 'action': 'delete'})
        original = self.net.disconnect
        with patch.object(self.net, 'disconnect', side_effect=lambda role: None if role == 'telescope' else original(role)):
            self.controller.cycle(5)
        self.assertNotIn('disconnected', [e['type'] for e in self.events(cursor)])
        self.assertIn('primary', self.controller.connection_events.states)
        original('telescope'); self.controller.cycle(10)
        self.assertNotIn('primary', self.controller.connection_events.states)
        self.assertNotIn('primary', self.controller.connection_events.contexts)
        self.controller.connection_events.radios['historical-radio-address'] = 'old-radio'
        self.controller.cycle(15)
        self.assertNotIn('historical-radio-address', self.controller.connection_events.radios)

    def test_recovery_preserves_association_without_claiming_disconnection(self):
        self.configured(); self.net.radios = 3; self.controller.cycle(0)
        cursor = self.controller.event_store.sequence
        self.net.disconnect('home')
        with patch.object(self.net, 'radio_idle', return_value=False):
            self.controller.cycle(5)
        changes = self.events(cursor)
        self.assertNotIn('disconnected', [e['type'] for e in changes])
        self.assertEqual(changes[-1]['type'], 'waiting')
        self.assertTrue(changes[-1]['associated']); self.assertFalse(changes[-1]['ready'])
        self.assertEqual(changes[-1]['reason'], 'HOME_UNAVAILABLE')
