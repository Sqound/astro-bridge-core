"""UTC estimates use bounded hints, a monotonic anchor and only a persistent floor."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from .helpers.clock_fixture import chrony_runner, event_clock
else:
    from helpers.clock_fixture import chrony_runner, event_clock
from wifi_bridge.api import ApiError
from wifi_bridge.clock import Clock, MAX_OFFSET, synchronized, utc
from wifi_bridge.events import EventStore


class ClockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'clock.json'
        self.wall, self.mono, self.synced = 1793740800, 100, False
        self.clock = self.create()

    def create(self):
        return Clock(self.path, now=lambda: self.wall, monotonic=lambda: self.mono, sync=lambda: self.synced)


    def hint(self, offset, client='test-client', uncertainty=0):
        return self.clock.hint({'utc_iso': utc(self.wall + offset), 'uncertainty_ms': uncertainty}, client)

    def test_synchronized_hint_is_ignored_and_never_persisted(self):
        self.synced = True
        value = self.hint(100)
        self.assertEqual(value['source'], 'ntp')
        self.assertEqual(value['offset_ms'], 0)
        self.assertEqual(self.clock.hints, [])
        self.assertFalse(self.path.exists())

    def test_last_eight_hints_median_and_monotonic_anchor(self):
        for index, offset in enumerate([1000, 10, 20, 30, 40, 50, 60, 70, 80]):
            self.hint(offset, str(index))
        self.assertEqual(len(self.clock.hints), 8)
        self.assertEqual(self.clock.read()['offset_ms'], 45000)
        self.mono += 15
        self.wall -= 100
        value = self.clock.read()
        self.assertEqual(value['offset_ms'], 160000)
        self.assertEqual(value['hint_age_seconds'], 15)

    def test_clamps_to_persisted_floor_and_restart_keeps_only_floor(self):
        self.path.write_text(json.dumps({'lower_bound': self.wall + 100}))
        self.clock = self.create()
        self.assertEqual(self.hint(-10)['offset_ms'], 100000)
        self.assertEqual(self.clock.read()['source'], 'client_hint')
        restarted = self.create()
        self.assertEqual(restarted.hints, [])
        self.assertEqual(restarted.clients, {})
        self.assertEqual(restarted.read()['source'], 'lower_bound')
        self.assertEqual(restarted.read()['offset_ms'], 100000)
        self.assertEqual(set(json.loads(self.path.read_text())), {'lower_bound'})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_rate_limit_is_per_client_and_monotonic_at_exact_boundary(self):
        self.hint(1)
        with self.assertRaises(ApiError) as error:
            self.hint(2)
        self.assertEqual(error.exception.status, 429)
        self.hint(2, 'other')
        self.mono += 59.99
        self.wall += 100000
        with self.assertRaises(ApiError): self.hint(1)
        self.mono += .01
        self.hint(1)

    def test_restart_retains_only_uncertainty_adjusted_floor_from_hint(self):
        self.hint(20, uncertainty=2000)
        restarted = self.create()
        self.assertEqual(restarted.read()['offset_ms'], 18000)
        self.assertIsNone(restarted.read()['hint_age_seconds'])
        self.assertEqual(restarted.hints, [])
        self.assertEqual(restarted.clients, {})

    def test_bounds_uncertainty_validation_and_disk_failure(self):
        for value in [{'utc_iso': '2026-01-01', 'uncertainty_ms': 0},
                      {'utc_iso': '2026-01-01T00:00:00+09:00', 'uncertainty_ms': 0},
                      {'utc_iso': utc(self.wall), 'uncertainty_ms': True},
                      {'utc_iso': utc(self.wall), 'uncertainty_ms': -1},
                      {'utc_iso': utc(self.wall), 'uncertainty_ms': 86400001},
                      {'utc_iso': utc(self.wall), 'uncertainty_ms': 0, 'extra': 1}]:
            with self.subTest(value=value), self.assertRaises(ApiError): self.clock.hint(value, 'client')
        with self.assertRaises(ApiError): self.hint(MAX_OFFSET + 1)
        self.hint(MAX_OFFSET, uncertainty=1000)
        with patch('wifi_bridge.clock.atomic_json', side_effect=OSError):
            self.hint(-MAX_OFFSET, 'other')

    def test_events_add_corrected_utc_and_journal_restarts(self):
        store = EventStore(self.path.with_name('events.json'), clock=self.clock)
        def append():
            return store.append('connected', operation_id='synthetic-operation', profile_id='primary',
                profile_name='Synthetic instrument', radio_id=None, cause='RECONCILE', reason='READY', outcome='ready')
        self.assertNotIn('utc_corrected', append())
        self.hint(120)
        event = append()
        self.assertEqual(datetime.fromisoformat(event['utc_corrected']).timestamp() -
                         datetime.fromisoformat(event['timestamp']).timestamp(), 120)
        store.last_write = None; store.flush()
        restarted = EventStore(store.path, clock=self.create())
        self.assertTrue(restarted.history_available)
        self.assertEqual(restarted.events[-1], event)

    def test_sync_probe_is_cached_and_utility_failure_is_unverified(self):
        with patch.object(self.clock, 'sync', return_value=True) as probe:
            self.clock.read(); self.clock.read()
            self.assertEqual(probe.call_count, 1)
            self.mono += 5
            self.clock.read()
            self.assertEqual(probe.call_count, 2)
        with patch('wifi_bridge.clock.subprocess.run', side_effect=FileNotFoundError):
            self.assertFalse(synchronized())

    def test_sync_probe_requires_fresh_finite_external_reference(self):
        from types import SimpleNamespace
        fields = ['C0000201', '192.0.2.1', '2', str(self.wall), '0.01', '0', '0', '0', '0', '0', '.1', '.1', '64', 'Normal']
        def probe(value, code=0):
            result = SimpleNamespace(returncode=code, stdout=','.join(value).encode())
            with patch('wifi_bridge.clock.subprocess.run', return_value=result), patch('wifi_bridge.clock.time.time', return_value=self.wall):
                return synchronized()
        self.assertTrue(probe(fields))
        for index, value in [(0, '7F7F0101'), (0, '00000000'), (1, '127.0.0.1'), (1, '0.0.0.0'),
                             (2, '0'), (3, str(self.wall-3601)), (4, 'nan'), (13, 'Not synchronised')]:
            changed = fields[:]; changed[index] = value
            with self.subTest(field=index, value=value): self.assertFalse(probe(changed))
        self.assertFalse(probe(fields[:-1]))
        self.assertFalse(probe(fields, 1))
