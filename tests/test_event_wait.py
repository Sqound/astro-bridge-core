"""Native event transitions, bounded waits and journal wear using synthetic inputs."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.config import profiles, validate_config
from wifi_bridge.events import EventStore, JOURNAL_BYTES, NATIVE_FIELDS
from wifi_bridge.web import WebServer


class EventWaitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = [0.0]
        self.store = EventStore(Path(self.directory.name) / 'events.json', monotonic=lambda: self.now[0])
        self.addCleanup(self.store.close)

    def append(self):
        return self.store.native('instrument.restored', 'test-instrument')

    def parked(self, count):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self.store.lock:
                if self.store.waiters == count: return
            threading.Event().wait(.005)
        self.fail('Waiter count did not reach expected bound')

    def test_wait_wakes_on_append_and_fake_clock_timeout(self):
        result = []
        thread = threading.Thread(target=lambda: result.append(self.store.read(0, self.store.boot_id, wait=25)))
        thread.start(); self.parked(1)
        event = self.append(); thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertEqual(result[0]['events'], [event])
        with patch.object(self.store.condition, 'wait', side_effect=lambda timeout: self.now.__setitem__(0, self.now[0] + timeout)) as wait:
            page = self.store.read(1, self.store.boot_id, wait=25)
        self.assertEqual(page['events'], []); self.assertEqual(page['next_cursor'], 1)
        wait.assert_called_once_with(25)
        for arguments in ({'wait': -1}, {'wait': 26}, {'wait': True}, {'wait': .5}):
            with self.assertRaises(ValueError): self.store.read(**arguments)
        with patch.object(self.store.condition, 'wait') as wait:
            for args in ({}, {'after': 0}, {'after': 99}, {'after': 1, 'boot_id': 'f'*32}):
                self.store.read(**args, wait=25)
            wait.assert_not_called()

    def test_four_shared_waiters_cap_and_shutdown_release(self):
        results = []
        workers = [threading.Thread(target=lambda: results.append(self.store.read(0, wait=25))) for _ in range(4)]
        for worker in workers: worker.start()
        self.parked(4)
        with patch.object(self.store.condition, 'wait') as wait:
            self.assertEqual(self.store.read(0, wait=25)['events'], [])
            wait.assert_not_called()
        self.store.close()
        for worker in workers: worker.join(2); self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 4); self.assertEqual(self.store.waiters, 0)

    def test_1024_disk_256_memory_retention_and_one_write_per_ten_seconds(self):
        writes = []
        from wifi_bridge.events import atomic_json
        def record(path, payload):
            writes.append(self.now[0]); atomic_json(path, payload)
        with patch('wifi_bridge.events.atomic_json', side_effect=record):
            for batch in range(6):
                for _ in range(200): self.append()
                self.now[0] += 10; self.store.flush()
                self.assertLessEqual(len(self.store.events), 256)
                self.assertLessEqual(len(self.store.observed_events), 256)
            self.store.close()
        self.assertEqual(writes, [10,20,30,40,50,60])
        data = json.loads(self.store.path.read_text())
        self.assertEqual(len(data['events']), 1024)
        self.assertLessEqual(self.store.path.stat().st_size, JOURNAL_BYTES)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.read(0)['reset_reason'], 'RETENTION_GAP')
        self.assertEqual(self.store.read(176, limit=100)['events'][0]['sequence'], 177)
        restarted = EventStore(self.store.path)
        self.assertEqual(restarted.sequence, 1200)
        self.assertEqual(len(restarted.events), 256)
        self.assertEqual(restarted.read(176, limit=100)['events'][0]['sequence'], 177)
        self.assertEqual(restarted.read(1200, self.store.boot_id)['reset_reason'], 'BOOT_CHANGED')

    def test_overflow_between_batches_is_an_explicit_internal_gap(self):
        self.append(); self.now[0] = 10; self.store.flush()
        for _ in range(300): self.append()
        self.assertEqual(self.store.read(1)['reset_reason'], 'RETENTION_GAP')
        self.assertIsNone(self.store.read(45)['reset_reason'])
        self.now[0] = 20; self.store.flush()
        self.assertEqual(self.store.read(1)['reset_reason'], 'RETENTION_GAP')

    def test_native_allowlist_never_accepts_endpoint_addresses(self):
        with self.assertRaises(ValueError): self.store.native('instrument.endpoint_changed', 'test', endpoint={'host':'192.0.2.1'})
        with self.assertRaises(ValueError): self.store.native('instrument.lost', 'test', reason='arbitrary private response')
        with self.assertRaises(ValueError): self.store.native('instrument.moved', 'test', from_radio_id='02:00:00:00:00:01', to_radio_id='radio-'+16*'a')
        self.assertTrue({'telemetry.session_started','telemetry.session_ended','telemetry.available'} <= NATIVE_FIELDS.keys())


class NativeTransitionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.now = [100.0]
        self.controller.event_store.monotonic = lambda: self.now[0]
        self.controller.event_store.last_write = self.now[0]
        self.controller.protection.clock = lambda: self.now[0]
        self.configured(); self.controller.cycle(0)

    def events(self, after=0):
        return [e for e in self.controller.event_store.read(after, limit=100)['events'] if '.' in e['type']]

    def test_loss_restore_move_endpoint_and_privacy_from_controller(self):
        c = self.controller
        cursor = c.event_store.sequence
        self.net.disconnect('telescope'); c.cycle(20)
        kinds = [e['type'] for e in self.events(cursor)]
        self.assertIn('instrument.lost', kinds); self.assertIn('instrument.restored', kinds)
        # The owner intentionally moves a profile; no false loss/restoration.
        self.net.radios = 4; devices = self.net.devices(); c.update(devices=devices)
        cursor = c.event_store.sequence
        c.telescope_action({'id':'primary','action':'connect','mac':devices[2]['mac']}); c.cycle(40)
        events = self.events(cursor); kinds = [e['type'] for e in events]
        self.assertIn('instrument.moved', kinds)
        self.assertNotIn('instrument.lost', kinds); self.assertNotIn('instrument.restored', kinds)
        moved = next(e for e in events if e['type']=='instrument.moved')
        self.assertNotEqual(moved['from_radio_id'], moved['to_radio_id'])
        # Restore a valid routed target for the new radio before changing management.
        value = copy.deepcopy(self.store.config); value['telescopes'][0]['target_ip']='192.168.4.1'
        self.store.save(validate_config(value, self.store.config, devices)); c.cycle(60)
        cursor = c.event_store.sequence
        c.state['management']['paths'][0]['ip']='192.0.2.77'; c._native_event_snapshot()
        markers = [e for e in self.events(cursor) if e['type']=='instrument.endpoint_changed']
        # Routed primaries stay at their instrument address; their management change is not a primary change.
        self.assertEqual(markers, [])
        self.now[0] += 10; c.event_store.flush()
        persisted = c.event_store.path.read_text()
        for private in [d['mac'] for d in devices]+['10.0.0.1','Vespera-ABC','192.168.4.1']:
            self.assertNotIn(private, persisted)

    def test_state_debounce_latest_value_and_lease_lifecycle(self):
        c = self.controller; cursor = c.event_store.sequence
        row = c.state['telescopes'][0]
        row['service']['state']='not_answering'; c._native_event_snapshot()
        self.assertEqual([e for e in self.events(cursor) if e['type']=='instrument.state'], [])
        self.now[0] += 5; c._native_event_snapshot()
        states = [e for e in self.events(cursor) if e['type']=='instrument.state']
        self.assertEqual(len(states), 1); self.assertEqual(states[0]['service'], 'not_answering')
        c._native_event_snapshot(); self.assertEqual(len(self.events(cursor)), 1)
        value=c.protection.native_create('primary','synthetic-client',120)
        self.now[0] += 60; c.protection.prune(); c.protection.prune()
        self.assertEqual(sum(e['type']=='lease.expiring' for e in self.events(cursor)),1)
        self.now[0] += 60; c.protection.prune()
        ended = [e for e in self.events(cursor) if e['type']=='lease.ended']
        self.assertEqual(ended[-1]['reason'],'EXPIRED')
        value=c.protection.native_create('primary','synthetic-client',120)
        c.protection.native_change('primary',value['id'],'synthetic-client')
        self.assertEqual(self.events(cursor)[-1]['reason'],'RELEASED')

    def test_explicit_disconnect_and_legacy_release_end_leases_as_released(self):
        from wifi_bridge.api import MobileAPI
        c=self.controller;api=MobileAPI(c)
        value=c.protection.native_create('primary','synthetic-client',120)
        c.protection.change(value['id'],{'version':1},'synthetic-client',release=True)
        self.assertEqual(self.events()[-1]['reason'],'RELEASED')
        value=c.protection.native_create('primary','synthetic-client',120)
        api.instruments.action('primary','disconnect',{},'synthetic-client')
        self.assertEqual(self.events()[-1]['type'],'lease.ended')
        self.assertEqual(self.events()[-1]['reason'],'RELEASED')

    def test_available_state_matches_catalog_without_scanning_and_sequence_survives_eviction(self):
        c=self.controller; row=c.state['telescopes'][0]
        row.update(connected=False,state='waiting'); self.store.config['telescope']['enabled']=False
        c.candidates.nearby_at=c.candidates.clock()
        profile=profiles(self.store.config)[0]
        c.candidates.nearby_rows=({'ssid':profile['ssid'],'scan_source':'fresh'},)
        self.now[0]+=5
        with patch.object(self.net,'scan',side_effect=AssertionError('Event projection must not scan')):
            c._native_event_snapshot()
        self.assertEqual(self.events()[-1]['link'],'available')
        before=c.instrument_catalog(c.status())[0]['since_seq']
        for _ in range(300): c.event_store.native('instrument.restored','another-instrument')
        self.assertEqual(c.instrument_catalog(c.status())[0]['since_seq'],before)
        self.assertLessEqual(len(c.event_store.instrument_sequences),24)

    def test_saved_profiles_and_explicit_stops_are_not_unexpected_losses_but_leased_links_are(self):
        c=self.controller; cursor=c.event_store.sequence
        c.telescope_action({'id':'primary','action':'disconnect'}); c.cycle(20)
        self.assertNotIn('instrument.lost',[e['type'] for e in self.events(cursor)])
        c.cycle(40)
        self.assertNotIn('instrument.lost',[e['type'] for e in self.events(cursor)])
        # Leases can retain a temporarily disabled desired profile during restoration.
        profile=dict(profiles(self.store.config)[0],enabled=False)
        c.protection.native_create('primary','synthetic-client',120)
        state=c.connection_events.states['primary'];state.update(associated=True,intentional=False)
        c.connection_events.observe(profile,False,False,'waiting','CONNECTION_LOST')
        self.assertIn('instrument.lost',[e['type'] for e in self.events(cursor)])

    def test_tls_listener_rotation_preserves_store_global_owner_closes_it(self):
        owner=WebServer(('127.0.0.1',0),self.controller)
        sibling=WebServer(('127.0.0.1',0),self.controller,shared=owner)
        store=self.controller.event_store;results=[]
        waiter=threading.Thread(target=lambda:results.append(store.read(store.sequence,wait=25)))
        waiter.start();deadline=time.monotonic()+2
        while not store.waiters and time.monotonic()<deadline:threading.Event().wait(.005)
        self.assertEqual(store.waiters,1)
        sibling.server_close(); self.assertFalse(store.closed);self.assertTrue(waiter.is_alive())
        owner.server_close();waiter.join(2)
        self.assertTrue(store.closed);self.assertFalse(waiter.is_alive());self.assertEqual(results[0]['events'],[])
