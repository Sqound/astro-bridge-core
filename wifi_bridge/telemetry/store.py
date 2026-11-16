"""Bounded SQLite journal with 30-second/500-row transactions and private files."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
import weakref

from ..clock import Clock

BATCH_ROWS = 500
FLUSH_SECONDS = 30
END_REASONS = frozenset(('idle', 'disconnect', 'profile_move', 'shutdown', 'restart'))
SCHEMA = (
    'CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    '''CREATE TABLE sessions (id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
       kind TEXT NOT NULL, adapter_set TEXT NOT NULL, started_seq INTEGER NOT NULL,
       ended_seq INTEGER, started_utc TEXT NOT NULL, ended_utc TEXT,
       ended_reason TEXT, clock_status TEXT NOT NULL, summary_json TEXT NOT NULL)''',
    '''CREATE TABLE samples (seq INTEGER PRIMARY KEY, session_id TEXT NOT NULL
       REFERENCES sessions(id) ON DELETE CASCADE, boot_id TEXT NOT NULL,
       mono_ms INTEGER NOT NULL, utc TEXT NOT NULL, utc_corrected TEXT,
       metric TEXT NOT NULL, num REAL, text TEXT)''',
    '''CREATE TABLE events (seq INTEGER PRIMARY KEY, session_id TEXT NOT NULL
       REFERENCES sessions(id) ON DELETE CASCADE, boot_id TEXT NOT NULL,
       type TEXT NOT NULL, fields_json TEXT NOT NULL)''',
    'CREATE INDEX samples_session_seq ON samples(session_id, seq)',
    'CREATE INDEX events_session_seq ON events(session_id, seq)',
    'CREATE INDEX sessions_time ON sessions(started_utc, started_seq)',
    "INSERT INTO metadata VALUES ('sequence', '0')",
    "INSERT INTO metadata VALUES ('reserved_sequence', '0')",
)


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[a-z0-9-]{1,32}', value) is not None


def session_context(profile_id, kind, adapter_set):
    if not identifier(profile_id) or not isinstance(kind, str) or not re.fullmatch(r'[a-z0-9_-]{1,32}', kind):
        raise ValueError('Invalid telemetry profile')
    if (not isinstance(adapter_set, (list, tuple)) or len(adapter_set) > 16 or
            any(not isinstance(v, str) or re.fullmatch(r'[a-z0-9_]{1,64}', v) is None for v in adapter_set) or
            len(set(adapter_set)) != len(adapter_set)):
        raise ValueError('Invalid telemetry adapters')
    return kind, tuple(sorted(adapter_set))


class TelemetryStore:
    """Injected state path follows Storage; failures degrade to a bounded RAM DB.

    Ordering is independent of UTC. Durable bounded reservations prevent reuse
    of even unflushed sequence numbers. An abrupt exit leaves explicit gaps.
    boot_id identifies the monotonic time domain, matching the event journal.
    """
    def __init__(self, path, *, clock=None, monotonic=None, boot_id=None,
                 nights=30, max_bytes=64 * 1024 * 1024):
        if type(nights) is not int or not 1 <= nights <= 3650:
            raise ValueError('Invalid telemetry retention')
        if type(max_bytes) is not int or not 65536 <= max_bytes <= 1024**3:
            raise ValueError('Invalid telemetry quota')
        self.path = Path(path) if path is not None else None
        self.monotonic = monotonic or time.monotonic
        self.clock = clock or Clock((self.path or Path('telemetry.sqlite3')).with_name('clock.json'))
        self.boot_id = boot_id or uuid.uuid4().hex
        if not re.fullmatch(r'[0-9a-f]{32}', self.boot_id): raise ValueError('Invalid boot ID')
        self.nights, self.max_bytes = nights, max_bytes
        self.lock = threading.RLock()
        self.queue = []
        self.reader_queue = {}
        self.coverage_queue = {}
        self.coverage_degraded = False
        self.closed = False
        self.persistent = False
        self.history_available = True
        self.session_cache = {}
        self.last_flush = self.monotonic()
        self.last_purge = self.last_flush
        self.write_count = 0
        self.db = None
        try:
            if self.path is None: raise OSError('No persistent storage')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Create privately before SQLite opens WAL/SHM beside it.
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
            self.db = self._connect(str(self.path))
            self._migrate()
            self.persistent = True
        except (OSError, sqlite3.Error):
            if self.db is not None: self.db.close()
            self.history_available = False
            self.db = self._connect(':memory:')
            self._migrate()
        self.committed_sequence = int(self.db.execute("SELECT value FROM metadata WHERE key='sequence'").fetchone()[0])
        self.reserved_sequence = int(self.db.execute("SELECT value FROM metadata WHERE key='reserved_sequence'").fetchone()[0])
        self.sequence = self.reserved_sequence
        row = self.db.execute("SELECT value FROM metadata WHERE key='settings'").fetchone()
        self.settings = json.loads(row[0]) if row else {'retention_nights': nights, 'max_bytes': max_bytes, 'profiles': {}}
        self.nights, self.max_bytes = self.settings['retention_nights'], self.settings['max_bytes']
        self._recover()
        self.maintain(force=True, idle=True)
        self._finalizer = weakref.finalize(self, self.db.close)

    def _connect(self, location):
        db = sqlite3.connect(location, timeout=.5, check_same_thread=False)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA auto_vacuum=INCREMENTAL')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=NORMAL')
            db.execute('PRAGMA cache_size=-512')
            db.execute('PRAGMA wal_autocheckpoint=64')
            db.execute('PRAGMA journal_size_limit=262144')
            return db
        except sqlite3.Error:
            db.close()
            raise

    def _migrate(self):
        # DDL otherwise autocommits before the first INSERT on sqlite3 defaults.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('CREATE TABLE IF NOT EXISTS migrations (version INTEGER PRIMARY KEY)')
            version = self.db.execute('SELECT COALESCE(MAX(version),0) FROM migrations').fetchone()[0]
            if version > 1: raise sqlite3.DatabaseError('Unsupported telemetry schema')
            if version == 0:
                for statement in SCHEMA: self.db.execute(statement)
                self.db.execute('INSERT INTO migrations VALUES (1)')
            sequence = self.db.execute("SELECT value FROM metadata WHERE key='sequence'").fetchone()
            if sequence is None or not sequence[0].isdigit() or not 0 <= int(sequence[0]) < 2**63:
                raise sqlite3.DatabaseError('Invalid telemetry sequence')
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('reserved_sequence', ?)", (sequence[0],))
            reserved = self.db.execute("SELECT value FROM metadata WHERE key='reserved_sequence'").fetchone()[0]
            if not reserved.isdigit() or not int(sequence[0]) <= int(reserved) < 2**63:
                raise sqlite3.DatabaseError('Invalid telemetry reservation')
            self.db.execute('CREATE INDEX IF NOT EXISTS sessions_profile_started ON sessions(profile_id, started_seq)')
            self.db.execute("CREATE TABLE IF NOT EXISTS telemetry_readers (session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE, client_name TEXT NOT NULL, from_seq INTEGER NOT NULL, to_seq INTEGER NOT NULL, PRIMARY KEY(session_id, client_name))")
            self.db.execute("CREATE TABLE IF NOT EXISTS telemetry_coverage (session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE, unknown_before_seq INTEGER, ranges_json TEXT NOT NULL) WITHOUT ROWID")
            self.coverage_degraded = bool(self.db.execute("SELECT 1 FROM metadata WHERE key='coverage_degraded'").fetchone())
            self.db.commit()
        except sqlite3.Error:
            self._rollback()
            raise

    def _recover(self):
        # A session cannot span an unknown service downtime/monotonic timeline.
        for row in self.db.execute('SELECT * FROM sessions WHERE ended_seq IS NULL').fetchall():
            self.session_cache[row['id']] = tuple(row)
            self.end_session(row['id'], 'restart')
        self.flush()

    def _next(self):
        if self.closed: raise ValueError('Telemetry store closed')
        if self.sequence >= 2**63 - 1: raise ValueError('Telemetry sequence exhausted')
        if self.sequence >= self.reserved_sequence:
            reserved = min(2**63 - 1, self.sequence + BATCH_ROWS)
            try:
                with self.db:
                    self.db.execute("UPDATE metadata SET value=? WHERE key='reserved_sequence'", (str(reserved),))
            except sqlite3.Error:
                self._rollback()
                self._memory()
            self.reserved_sequence = reserved
        self.sequence += 1
        return self.sequence

    def _room(self, rows):
        if len(self.queue) + rows > BATCH_ROWS: self.flush()

    def _enqueue(self, *statements):
        self._room(len(statements))
        self.queue.extend(statements)
        if len(self.queue) >= BATCH_ROWS: self.flush()

    def timestamps(self):
        clock = self.clock.read()
        corrected = clock['corrected_utc'] if clock['source'] == 'client_hint' or clock['offset_ms'] else None
        return int(self.monotonic() * 1000), clock['system_utc'], corrected, clock['clock_status']

    def start_session(self, profile_id, kind, adapter_set):
        session_context(profile_id, kind, adapter_set)
        with self.lock:
            ended = {args[-1] for sql, args in self.queue if sql.startswith('UPDATE sessions SET ended_seq')}
            if len(self.session_cache.keys() - ended) >= 24: raise ValueError('Too many telemetry sessions')
            _, utc, corrected, status = self.timestamps()
            self._room(2)
            ident, seq = uuid.uuid4().hex, self._next()
            row = (ident, profile_id, kind, json.dumps(sorted(adapter_set)), seq, None, corrected or utc, None, None, status, '{}')
            self.session_cache[ident] = row
            self._enqueue(('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?)',
                          (ident, profile_id, kind, json.dumps(sorted(adapter_set)), seq, corrected or utc, status, '{}')),
                          ('INSERT INTO events VALUES (?, ?, ?, ?, ?)',
                          (seq, ident, self.boot_id, 'session.started', '{}')))
            return ident

    def sample(self, session_id, metric, value):
        from .metrics import validate
        num, text = validate(metric, value)
        with self.lock:
            self._require_session(session_id)
            if metric.startswith('stack.reject_reason.'):
                reasons = {r[0] for r in self.db.execute("SELECT DISTINCT metric FROM samples WHERE session_id=? AND metric LIKE 'stack.reject_reason.%' LIMIT 129", (session_id,))}
                reasons.update(args[6] for sql, args in self.queue if sql.startswith('INSERT INTO samples') and args[1] == session_id and args[6].startswith('stack.reject_reason.'))
                if metric not in reasons and len(reasons) >= 128: raise ValueError('Invalid telemetry metric')
            mono, utc, corrected, _ = self.timestamps()
            self._room(1)
            seq = self._next()
            self._enqueue(('INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                          (seq, session_id, self.boot_id, mono, utc, corrected, metric, num, text)))
            if self.monotonic() - self.last_flush >= FLUSH_SECONDS: self.flush()
            return seq

    def _require_session(self, ident):
        if self.closed: raise ValueError('Telemetry store closed')
        if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f]{32}', ident):
            raise ValueError('Invalid telemetry session')
        if any(sql.startswith('UPDATE sessions SET ended_seq') and args[-1] == ident for sql, args in self.queue):
            raise ValueError('Ended telemetry session')
        # At most 500 queue entries; avoid flushing just to validate a new session.
        if any(sql.startswith('INSERT INTO sessions') and args[0] == ident for sql, args in self.queue):
            return
        try:
            exists = self.db.execute('SELECT 1 FROM sessions WHERE id=? AND ended_seq IS NULL', (ident,)).fetchone()
        except sqlite3.Error:
            self._memory()
            exists = ident in self.session_cache
        if not exists:
            raise ValueError('Unknown or ended telemetry session')

    def end_session(self, ident, reason):
        if reason not in END_REASONS: raise ValueError('Invalid telemetry end reason')
        with self.lock:
            self._require_session(ident)
            _, utc, corrected, _ = self.timestamps()
            self._room(2)
            seq = self._next()
            count = self.db.execute('SELECT COUNT(*) FROM samples WHERE session_id=?', (ident,)).fetchone()[0]
            count += sum(sql.startswith('INSERT INTO samples') and args[1] == ident for sql, args in self.queue)
            self._enqueue(('UPDATE sessions SET ended_seq=?, ended_utc=?, ended_reason=?, summary_json=? WHERE id=?',
                          (seq, corrected or utc, reason, json.dumps({'samples': count}), ident)),
                          ('INSERT INTO events VALUES (?, ?, ?, ?, ?)',
                          (seq, ident, self.boot_id, 'session.ended', json.dumps({'reason': reason}))))
            return seq

    def _rollback(self):
        try: self.db.rollback()
        except sqlite3.Error: pass

    def _memory(self):
        replacement = self._connect(':memory:')
        previous = self.db
        deadline = time.monotonic() + .5
        def progress(status, remaining, total):
            if time.monotonic() > deadline: raise sqlite3.OperationalError('Telemetry backup unavailable')
        try:
            previous.backup(replacement, pages=64, sleep=.01, progress=progress)
        except sqlite3.Error:
            # Unreadable storage cannot take network management down. Rebuild only
            # bounded session metadata needed to accept the pending batch; older
            # history is explicitly unavailable, never silently claimed intact.
            replacement.close()
            replacement = self._connect(':memory:')
            self.db = replacement
            self._migrate()
            inserted = {args[0] for sql, args in self.queue if sql.startswith('INSERT INTO sessions')}
            with replacement:
                for ident, row in self.session_cache.items():
                    if ident not in inserted:
                        replacement.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', row)
            self.history_available = False
            self.coverage_degraded = True
        finally:
            try: previous.close()
            except sqlite3.Error: pass
        self.db = replacement
        if hasattr(self, '_finalizer'):
            self._finalizer.detach()
            self._finalizer = weakref.finalize(self, self.db.close)
        self.persistent = False

    def flush(self):
        with self.lock:
            if self.closed or not (self.queue or self.reader_queue or self.coverage_queue): return
            try:
                self._commit()
            except sqlite3.Error:
                self._rollback()
                # Preserve committed history and the bounded pending batch in RAM.
                self._memory()
                self._commit()
            self.last_flush = self.monotonic()
            self.write_count += 1
            ended = {args[-1] for sql, args in self.queue if sql.startswith('UPDATE sessions SET ended_seq')}
            self.session_cache = {ident: row for ident, row in self.session_cache.items() if ident not in ended}
            self.queue.clear()
            self.reader_queue.clear()
            self.coverage_queue.clear()
            try:
                if self._used_bytes() > self.max_bytes: self.maintain(force=True)
            except sqlite3.Error:
                self._memory()

    def _commit(self):
        latest = max([self.committed_sequence] + [args[0] for sql, args in self.queue if sql.startswith(('INSERT INTO samples', 'INSERT INTO events'))])
        with self.db:
            for sql, args in self.queue:
                self.db.execute(sql, args)
                if sql.startswith('INSERT INTO sessions'):
                    self.db.execute('INSERT INTO telemetry_coverage VALUES (?, ?, ?)', (args[0],args[4]-1,'[]'))
            for (ident, name), value in self.reader_queue.items():
                self.db.execute("INSERT INTO telemetry_readers VALUES (?, ?, ?, ?) ON CONFLICT(session_id, client_name) DO UPDATE SET from_seq=MIN(from_seq, excluded.from_seq), to_seq=MAX(to_seq, excluded.to_seq)", (ident, name, value['from_seq'], value['to_seq']))
            from .analysis import coverage
            for ident in self.coverage_queue:
                current = coverage(self, ident)
                self.db.execute('INSERT INTO telemetry_coverage VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET unknown_before_seq=excluded.unknown_before_seq, ranges_json=excluded.ranges_json',
                                (ident, current['unknown_before_seq'], json.dumps([[r['from_seq'],r['to_seq']] for r in current['fetched']])))
            if self.coverage_degraded:
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('coverage_degraded','1')")
            self.db.execute("UPDATE metadata SET value=? WHERE key='sequence'", (str(latest),))
            reserved = min(2**63 - 1, max(self.reserved_sequence, self.sequence + BATCH_ROWS))
            self.db.execute("UPDATE metadata SET value=? WHERE key='reserved_sequence'", (str(reserved),))
        self.committed_sequence = latest
        self.reserved_sequence = reserved

    def samples(self, session_id, *, after=0, limit=100):
        if not isinstance(session_id, str) or not re.fullmatch(r'[0-9a-f]{32}', session_id):
            raise ValueError('Invalid telemetry session')
        if type(after) is not int or not 0 <= after <= 2**63 - 1 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('Invalid telemetry cursor')
        with self.lock:
            # Reads do not turn the batching policy into per-poll SD writes.
            if self.closed: raise ValueError('Telemetry store closed')
            try:
                rows = self._sample_rows(session_id, after, limit)
            except sqlite3.Error:
                self._memory()
                rows = self._sample_rows(session_id, after, limit)
            pending = [dict(zip(('seq', 'session_id', 'boot_id', 'mono_ms', 'utc', 'utc_corrected', 'metric', 'num', 'text'), args))
                       for sql, args in self.queue if sql.startswith('INSERT INTO samples') and args[1] == session_id and args[0] > after]
            return (rows + pending)[:limit]

    def _sample_rows(self, session_id, after, limit):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM samples WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?', (session_id, after, limit))]

    def reader(self, ident, name, first, last):
        # At most 16 paired names per session and 128 pending sessions per batch.
        if self.closed: return
        exists = self.db.execute('SELECT 1 FROM sessions WHERE id=?', (ident,)).fetchone()
        if not exists and not any(sql.startswith('INSERT INTO sessions') and args[0] == ident for sql,args in self.queue): return
        # Exact fetched intervals are independent of the bounded display-name audit.
        from .analysis import merged, INTERVALS, coverage
        prior = coverage(self, ident)
        prior_name = self.reader_queue.get((ident,name)) or self.db.execute('SELECT * FROM telemetry_readers WHERE session_id=? AND client_name=?', (ident,name)).fetchone()
        if (prior_name and prior_name['from_seq']<=first and prior_name['to_seq']>=last and
                any(r['from_seq']<=first and r['to_seq']>=last for r in prior['fetched'])): return
        if ident not in self.coverage_queue and len(self.coverage_queue) >= 128:
            self.coverage_degraded = True
        else:
            audit = self.coverage_queue.setdefault(ident, {'ranges':[], 'unknown_before_seq':0})
            ranges = merged(audit['ranges'] + [[first,last]])
            if len(ranges)>INTERVALS: audit['unknown_before_seq']=max(audit['unknown_before_seq'],ranges[-INTERVALS-1][1])
            audit['ranges']=ranges[-INTERVALS:]
        key = (ident, name)
        if key not in self.reader_queue:
            count = self.db.execute('SELECT COUNT(*) FROM telemetry_readers WHERE session_id=?', (ident,)).fetchone()[0]
            pending = sum(k[0] == ident for k in self.reader_queue)
            exists = self.db.execute('SELECT 1 FROM telemetry_readers WHERE session_id=? AND client_name=?', key).fetchone()
            if not exists and count + pending >= 16: return
            if len(self.reader_queue) >= 128: return
        old = self.reader_queue.get(key)
        self.reader_queue[key] = {'client_name': name, 'from_seq': min(first, old['from_seq']) if old else first,
                                  'to_seq': max(last, old['to_seq']) if old else last}

    def _used_bytes(self):
        pages = self.db.execute('PRAGMA page_count').fetchone()[0]
        free = self.db.execute('PRAGMA freelist_count').fetchone()[0]
        return (pages - free) * self.db.execute('PRAGMA page_size').fetchone()[0]

    def maintain(self, *, force=False, idle=False):
        with self.lock:
            if self.closed: return
            now = self.monotonic()
            if (self.queue or self.reader_queue or self.coverage_queue) and now - self.last_flush >= FLUSH_SECONDS: self.flush()
            if not force and now - self.last_purge < 3600: return
            self.flush()
            self.last_purge = now
            try:
                with self.db:
                    # A night is noon-to-noon UTC, based on the best UTC at start.
                    nights = self.db.execute("SELECT DISTINCT date(started_utc, '-12 hours') AS night FROM sessions ORDER BY night DESC LIMIT ?", (self.nights,)).fetchall()
                    if nights:
                        self.db.execute("DELETE FROM sessions WHERE ended_seq IS NOT NULL AND date(started_utc, '-12 hours') < ?", (nights[-1][0],))
                    while self._used_bytes() > self.max_bytes:
                        oldest = self.db.execute('SELECT id FROM sessions WHERE ended_seq IS NOT NULL ORDER BY started_seq LIMIT 1').fetchone()
                        if oldest:
                            self.db.execute('DELETE FROM sessions WHERE id=?', (oldest[0],))
                        else:
                            # A single long active session cannot bypass the quota.
                            count = self.db.execute('DELETE FROM samples WHERE seq IN (SELECT seq FROM samples ORDER BY seq LIMIT 500)').rowcount
                            if not count: break  # Irreducible schema/session metadata.
                self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                # Incremental reclamation bounds the allocated file between full
                # compactions. It does not rewrite every surviving sample.
                free = self.db.execute('PRAGMA freelist_count').fetchone()[0]
                for _ in range(free): self.db.execute('PRAGMA incremental_vacuum(1)')
                last = self.db.execute("SELECT value FROM metadata WHERE key='vacuum_utc'").fetchone()
                day = datetime.now(timezone.utc).date().isoformat()
                if idle and (last is None or day > last[0]):
                    self.db.execute('VACUUM')
                    with self.db: self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('vacuum_utc', ?)", (day,))
                self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            except sqlite3.Error:
                self._rollback()
                self._memory()

    def close(self):
        with self.lock:
            if self.closed: return
            self.flush()
            # A clean shutdown can release unused reservations; no returned seq
            # exceeds this value. Crashes retain the reserved upper bound.
            try:
                with self.db:
                    self.db.execute("UPDATE metadata SET value=? WHERE key='reserved_sequence'", (str(self.sequence),))
            except sqlite3.Error:
                pass
            self.closed = True
            self.db.close()
            if hasattr(self, '_finalizer'): self._finalizer.detach()
