"""Synthetic storage/lifecycle checks; no manufacturer or hardware connection."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from wifi_bridge.telemetry import Sessions, TelemetryStore
from wifi_bridge.telemetry.metrics import BOOLEAN, NUMERIC, TEXT, validate


class Time:
    def __init__(self):
        self.mono = 0
        self.date = '2026-10-01T20:00:00+00:00'
        self.corrected = None

    def read(self):
        return {'system_utc': self.date, 'corrected_utc': self.corrected or self.date,
                'clock_status': 'unverified', 'source': 'client_hint' if self.corrected else 'lower_bound',
                'offset_ms': 1000 if self.corrected else 0}


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'telemetry.sqlite3'
        self.time = Time()

    def store(self, **kwargs):
        store = TelemetryStore(self.path, clock=self.time, monotonic=lambda: self.time.mono, **kwargs)
        self.addCleanup(store.close)
        return store

    def test_empty_migration_permissions_and_wal(self):
        store = self.store()
        self.assertTrue(store.persistent)
        self.assertEqual(store.db.execute('SELECT version FROM migrations').fetchone()[0], 1)
        self.assertEqual(store.db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(store.db.execute('PRAGMA synchronous').fetchone()[0], 1)
        for path in self.path.parent.glob('telemetry.sqlite3*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        store.close()
        again = self.store()
        self.assertEqual(again.sequence, 0)
        self.assertEqual(again.db.execute('SELECT COUNT(*) FROM migrations').fetchone()[0], 1)

    def test_sequence_shared_across_reopen_and_clock_reversal(self):
        store = self.store()
        ident = store.start_session('demo', 'generic', ['link_v1'])
        self.assertEqual(store.sample(ident, 'link.rssi_dbm', -55), 2)
        self.time.date = '2020-01-01T00:00:00+00:00'
        self.time.corrected = '2026-10-01T20:00:01+00:00'
        self.assertEqual(store.sample(ident, 'target.name', 'Demo target'), 3)
        before = store.write_count
        rows = store.samples(ident)
        self.assertEqual(store.write_count, before)
        self.assertEqual(rows[-1]['utc_corrected'], self.time.corrected)
        self.assertEqual(rows[-1]['boot_id'], store.boot_id)
        store.close()
        again = self.store()
        self.assertNotEqual(again.boot_id, store.boot_id)
        session = again.db.execute('SELECT * FROM sessions').fetchone()
        self.assertEqual((session['ended_seq'], session['ended_reason']), (4, 'restart'))
        self.assertEqual(json.loads(session['summary_json']), {'samples': 2})
        new = again.start_session('demo', 'generic', [])
        self.assertEqual(again.sample(new, 'link.ping_ms', 20), 6)
        again.flush()
        sequences = [r[0] for r in again.db.execute('SELECT seq FROM samples UNION ALL SELECT seq FROM events ORDER BY seq')]
        self.assertEqual(sequences, list(range(1, 7)))

    def test_batch_bounds_time_shutdown_and_read_without_write(self):
        store = self.store()
        ident = store.start_session('demo', 'generic', [])
        for i in range(497): store.sample(ident, 'link.ping_ms', i)
        self.assertEqual(store.write_count, 0)
        self.assertEqual(len(store.queue), 499)
        store.sample(ident, 'link.ping_ms', 1)
        self.assertEqual((store.write_count, len(store.queue)), (1, 0))
        self.time.mono = 29
        store.sample(ident, 'link.ping_ms', 2)
        store.maintain()
        self.assertEqual(store.write_count, 1)
        self.time.mono = 30
        store.maintain()
        self.assertEqual(store.write_count, 2)
        store.sample(ident, 'link.ping_ms', 3)
        store.close()
        store.close()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM samples').fetchone()[0], 500)

    def test_unknown_metrics_values_and_session_validation(self):
        for metric in NUMERIC: self.assertEqual(validate(metric, 1), (1, None))
        for metric in BOOLEAN: self.assertEqual(validate(metric, True), (1, None))
        for metric in TEXT: self.assertEqual(validate(metric, 'demo'), (None, 'demo'))
        self.assertEqual(validate('stack.reject_reason.cloud', 2), (2, None))
        for metric, value in [('raw.payload', 'demo'), ('link.ping_ms', float('nan')),
                              ('link.ping_ms', float('inf')), ('link.ping_ms', True),
                              ('mount.slewing', 1), ('target.name', 'x' * 65),
                              ('target.name', 'line\nbreak'), ('stack.reject_reason.BAD', 1)]:
            with self.subTest(metric=metric, value=value), self.assertRaises(ValueError): validate(metric, value)
        store = self.store()
        with self.assertRaises(ValueError): store.start_session('bad id', 'generic', [])
        for adapters in ([{}], ['x', 'x'], ['raw.payload']):
            with self.assertRaises(ValueError): store.start_session('demo', 'generic', adapters)
        with self.assertRaises(ValueError): store.sample('missing', 'link.ping_ms', 1)
        ident = store.start_session('demo', 'generic', [])
        store.end_session(ident, 'disconnect')
        with self.assertRaises(ValueError): store.sample(ident, 'link.ping_ms', 1)
        self.assertEqual(store.sequence, 2)

    def test_activity_sessions_idle_disconnect_move_and_shutdown(self):
        store = self.store()
        sessions = Sessions(store)
        sessions.tick()
        self.assertFalse(sessions.active)
        ident, _ = sessions.activity('demo', 'generic', ['link_v1'], 'link.ping_ms', 10, location='radio-a')
        self.time.mono = 1799
        sessions.tick()
        self.assertEqual(sessions.active['demo']['id'], ident)
        self.time.mono = 1800
        sessions.tick()
        self.assertFalse(sessions.active)
        sessions.activity('demo', 'generic', [], 'link.ping_ms', 10, location='radio-a')
        sessions.reconcile({'demo': {'enabled': True, 'mac': 'radio-b'}}, {'demo'})
        sessions.activity('demo', 'generic', [], 'link.ping_ms', 10)
        sessions.reconcile({'demo': {'enabled': True}}, set())
        sessions.activity('demo', 'generic', [], 'link.ping_ms', 10)
        sessions.close()
        sessions.close()
        sessions.tick()
        sessions.reconcile({}, set())
        with self.assertRaises(ValueError): sessions.activity('demo', 'generic', [], 'link.ping_ms', 10)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual([r[0] for r in db.execute('SELECT ended_reason FROM sessions ORDER BY started_seq')],
                             ['idle', 'profile_move', 'disconnect', 'shutdown'])

    def test_close_waits_for_active_producer_and_maintenance_stops(self):
        store = self.store()
        sessions = Sessions(store)
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        sample = store.sample
        errors = []
        def blocked(*args):
            entered.set()
            if not release.wait(5): raise RuntimeError('Producer timeout')
            return sample(*args)
        def producer():
            try: sessions.activity('demo', 'generic', [], 'link.ping_ms', 10)
            except Exception as error: errors.append(error)
        with patch.object(store, 'sample', blocked):
            worker = threading.Thread(target=producer)
            worker.start()
            self.assertTrue(entered.wait(5))
            closer = threading.Thread(target=lambda: (sessions.close(), closed.set()))
            closer.start()
            self.assertFalse(closed.wait(.05))
            release.set()
            worker.join(5)
            closer.join(5)
        self.assertFalse(errors)
        self.assertTrue(closed.is_set())
        # A still-running controller maintenance cycle becomes an idempotent no-op.
        sessions.reconcile({}, set())
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM samples').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT ended_reason FROM sessions').fetchone()[0], 'shutdown')

    def test_memory_only_missing_path_and_disk_failure(self):
        self.path.parent.joinpath('blocked').write_text('not a directory')
        for path in (None, self.path.parent / 'blocked' / 'telemetry.sqlite3'):
            store = TelemetryStore(path, clock=self.time)
            self.addCleanup(store.close)
            self.assertFalse(store.persistent)
            ident = store.start_session('demo', 'generic', [])
            store.sample(ident, 'link.ping_ms', 1)
            store.flush()
            self.assertEqual(len(store.samples(ident)), 1)
        store = self.store()
        ident = store.start_session('demo', 'generic', [])
        store.sample(ident, 'link.ping_ms', 1)
        store.flush()
        store.sample(ident, 'link.ping_ms', 2)
        commit = store._commit
        calls = []
        def fail_once():
            calls.append(1)
            if len(calls) == 1: raise sqlite3.OperationalError('Synthetic disk failure')
            commit()
        with patch.object(store, '_commit', fail_once): store.flush()
        self.assertFalse(store.persistent)
        self.assertEqual([r['num'] for r in store.samples(ident)], [1, 2])

    def test_unreadable_backup_falls_back_with_explicit_history_loss(self):
        store = self.store()
        ident = store.start_session('demo', 'generic', [])
        store.sample(ident, 'link.ping_ms', 1)
        store.flush()
        store.sample(ident, 'link.ping_ms', 2)
        committed = store.committed_sequence
        self.assertGreater(store.sequence, committed)
        self.assertGreaterEqual(store.reserved_sequence, store.sequence)
        db = store.db
        class BrokenBackup:
            def __getattr__(self, key): return getattr(db, key)
            def backup(self, target, **kwargs): raise sqlite3.DatabaseError('Synthetic unreadable storage')
        store.db = BrokenBackup()
        commit = store._commit
        calls = []
        def fail_once():
            calls.append(1)
            if len(calls) == 1: raise sqlite3.DatabaseError('Synthetic unreadable storage')
            commit()
        with patch.object(store, '_commit', fail_once): store.flush()
        self.assertFalse(store.persistent)
        self.assertFalse(store.history_available)
        self.assertEqual([r['num'] for r in store.samples(ident)], [2])
        store.sample(ident, 'link.ping_ms', 3)
        store.end_session(ident, 'disconnect')
        store.flush()
        self.assertEqual(store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual([r['num'] for r in store.samples(ident)], [2, 3])

    def test_retention_nights_counts_nights_not_sessions(self):
        store = self.store(nights=2)
        for date in ('2026-09-28T20:00:00+00:00', '2026-09-29T02:00:00+00:00',
                     '2026-09-29T20:00:00+00:00', '2026-09-30T20:00:00+00:00'):
            self.time.date = date
            ident = store.start_session('demo', 'generic', [])
            store.sample(ident, 'link.ping_ms', 1)
            store.end_session(ident, 'disconnect')
        store.maintain(force=True)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 2)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM samples').fetchone()[0], 2)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 4)
        store.close()
        again = self.store(nights=1)
        self.assertEqual(again.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 1)
        self.assertGreater(again.sequence, 1)

    def test_retention_size_active_session_and_hourly_schedule(self):
        store = self.store(max_bytes=65536)
        for _ in range(3):
            ident = store.start_session('demo', 'generic', [])
            for _ in range(500): store.sample(ident, 'target.name', 'x' * 64)
            store.end_session(ident, 'disconnect')
        active = store.start_session('active', 'generic', [])
        for _ in range(1200): store.sample(active, 'target.name', 'x' * 64)
        store.maintain(force=True, idle=False)
        self.assertLessEqual(store._used_bytes(), 65536)
        self.assertIsNotNone(store.db.execute('SELECT id FROM sessions WHERE id=?', (active,)).fetchone())
        before = store.last_purge
        self.time.mono += 3599
        store.maintain()
        self.assertEqual(store.last_purge, before)
        self.time.mono += 1
        store.maintain()
        self.assertEqual(store.last_purge, 3600)
        self.assertEqual(store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertLessEqual(self.path.stat().st_size, 65536)

    def test_vacuum_at_most_daily_and_only_idle(self):
        store = self.store()
        statements = []
        store.db.set_trace_callback(statements.append)
        store.db.execute("DELETE FROM metadata WHERE key='vacuum_utc'")
        store.db.commit()
        store.maintain(force=True, idle=False)
        self.assertNotIn('VACUUM', statements)
        store.maintain(force=True, idle=True)
        store.maintain(force=True, idle=True)
        self.assertEqual(statements.count('VACUUM'), 1)

    def test_controller_timer_and_primary_listener_own_store_lifecycle(self):
        from wifi_bridge.config import Store
        from wifi_bridge.controller import Controller
        from wifi_bridge.network import DemoNetwork
        from wifi_bridge.web import WebServer
        network = DemoNetwork()
        config = Store(self.tmp.name, network.devices()[0]['mac'])
        controller = Controller(config, network, {'password': 'synthetic-bootstrap'}, demo=True)
        self.addCleanup(controller.telemetry_sessions.close)
        self.assertEqual(controller.telemetry_store.boot_id, controller.event_store.boot_id)
        self.assertFalse(controller.telemetry_sessions.active)
        primary = WebServer(('127.0.0.1', 0), controller)
        self.addCleanup(primary.server_close)
        secondary = WebServer(('127.0.0.1', 0), controller, shared=primary)
        secondary.server_close()
        self.assertFalse(controller.telemetry_store.closed)
        store = controller.telemetry_store
        store.clock, store.monotonic = self.time, lambda: self.time.mono
        store.last_flush, store.last_purge = 0, 0
        ident, _ = controller.telemetry_sessions.activity('demo', 'generic', [], 'link.ping_ms', 1)
        self.time.mono = 30
        primary.metrics_deadline, primary.metrics_poll_interval = 0, .5
        primary.service_actions()
        self.assertFalse(store.queue)
        self.assertEqual(len(store.samples(ident)), 1)
        self.assertEqual([e['type'] for e in controller.event_store.events if e['type'].startswith('telemetry.')],
                         ['telemetry.session_started', 'telemetry.available'])
        primary.server_close()
        self.assertTrue(store.closed)
        controller.telemetry_sessions.reconcile({}, set())

    def test_100000_row_insert_rss_and_queue_are_bounded(self):
        script = '''import json, resource, sys
from wifi_bridge.telemetry import TelemetryStore
class Clock:
 def read(self): return {'system_utc':'2026-10-01T20:00:00+00:00', 'corrected_utc':'2026-10-01T20:00:00+00:00', 'clock_status':'unverified', 'source':'lower_bound', 'offset_ms':0}
s = TelemetryStore(sys.argv[1], clock=Clock(), monotonic=lambda:0)
scale = 1 if sys.platform == 'darwin' else 1024
baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
i = s.start_session('demo', 'generic', [])
maximum = 0
for n in range(100000):
 s.sample(i, 'link.ping_ms', n)
 maximum = max(maximum, len(s.queue))
s.flush()
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
print(json.dumps({'rows':s.db.execute('SELECT COUNT(*) FROM samples').fetchone()[0], 'queue_max':maximum, 'rss_baseline_bytes':baseline, 'rss_peak_bytes':peak, 'rss_growth_bytes':peak-baseline}))
s.close()
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.path)],
                                capture_output=True, text=True, check=True, timeout=60)
        report = json.loads(result.stdout)
        print('Telemetry 100000-row RSS: ' + json.dumps(report, sort_keys=True))
        self.assertEqual(report['rows'], 100000)
        self.assertLessEqual(report['queue_max'], 500)
        self.assertLess(report['rss_growth_bytes'], 16 * 1024 * 1024)

    def test_migration_ddl_rolls_back_as_one_transaction(self):
        from wifi_bridge.telemetry import store as module
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        instance = TelemetryStore.__new__(TelemetryStore)
        instance.db = db
        with patch.object(module, 'SCHEMA', module.SCHEMA[:2] + ('INVALID SQL',)):
            with self.assertRaises(sqlite3.Error): instance._migrate()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)
        instance._migrate()
        self.assertEqual(db.execute('SELECT version FROM migrations').fetchone()[0], 1)

    def test_abrupt_kill_loses_only_pending_batch_and_never_corrupts(self):
        script = '''import json, signal, sys
from wifi_bridge.telemetry import TelemetryStore
class Clock:
 def read(self): return {'system_utc':'2026-10-01T20:00:00+00:00', 'corrected_utc':'2026-10-01T20:00:00+00:00', 'clock_status':'unverified', 'source':'lower_bound', 'offset_ms':0}
s = TelemetryStore(sys.argv[1], clock=Clock(), monotonic=lambda:0)
i = s.start_session('demo', 'generic', [])
for n in range(1123): s.sample(i, 'link.ping_ms', n)
print(json.dumps({'latest_read':max(r['seq'] for r in s.samples(i, after=1000, limit=500)), 'committed':s.committed_sequence, 'reserved':s.reserved_sequence}), flush=True)
signal.pause()
'''
        child = subprocess.Popen([sys.executable, '-c', script, str(self.path)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        try:
            observed = json.loads(child.stdout.readline())
            self.assertGreater(observed['latest_read'], observed['committed'])
            self.assertGreaterEqual(observed['reserved'], observed['latest_read'])
            child.kill()
            child.wait(timeout=5)
        finally:
            child.stdout.close()
        store = self.store()
        self.assertEqual(store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertGreater(store.sequence, observed['latest_read'])
        self.assertGreaterEqual(store.sequence, observed['reserved'])
        count = store.db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
        self.assertLess(1123 - count, 500)
        self.assertGreater(count, 0)
        self.assertEqual(store.db.execute('SELECT ended_reason FROM sessions').fetchone()[0], 'restart')


if __name__ == '__main__': unittest.main()
