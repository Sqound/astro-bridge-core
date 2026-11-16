"""Bounded read projections over committed rows and the live transaction batch."""
import csv
import io
import json

from .metrics import BOOLEAN, NUMERIC, TEXT

SAMPLE_KEYS = ('seq', 'session_id', 'boot_id', 'mono_ms', 'utc', 'utc_corrected', 'metric', 'num', 'text')
EVENT_KEYS = ('seq', 'session_id', 'boot_id', 'type', 'fields_json')
SESSION_KEYS = ('id', 'profile_id', 'kind', 'adapter_set', 'started_seq', 'ended_seq', 'started_utc', 'ended_utc', 'ended_reason', 'clock_status', 'summary_json')


def sample(row):
    value = row['text'] if row['text'] is not None else row['num']
    if isinstance(value, float) and value.is_integer(): value = int(value)
    if row['metric'] in BOOLEAN: value = bool(value)
    return {key: row[key] for key in SAMPLE_KEYS[:7]} | {'value': value}


def event(row):
    fields = json.loads(row['fields_json'])
    return {key: row[key] for key in EVENT_KEYS[:4]} | {'reason': fields.get('reason')}


class Query:
    def __init__(self, store): self.store = store

    def pending(self, table):
        keys = SAMPLE_KEYS if table == 'samples' else EVENT_KEYS
        return [dict(zip(keys, args)) for sql, args in self.store.queue if sql.startswith('INSERT INTO ' + table)]

    def latest(self):
        # sequence may equal a crash reservation without any corresponding record.
        return max([self.store.committed_sequence] + [r['seq'] for t in ('samples', 'events') for r in self.pending(t)])

    def session(self, ident):
        row = self.store.db.execute('SELECT * FROM sessions WHERE id=?', (ident,)).fetchone()
        row = dict(row) if row else None
        for sql, args in self.store.queue:
            if sql.startswith('INSERT INTO sessions') and args[0] == ident:
                row = dict(zip(SESSION_KEYS, (args[0], args[1], args[2], args[3], args[4], None, args[5], None, None, args[6], args[7])))
            elif sql.startswith('UPDATE sessions SET ended_seq') and args[-1] == ident and row:
                row.update(ended_seq=args[0], ended_utc=args[1], ended_reason=args[2])
        return row

    def sessions(self, profile=None, after=0, limit=100):
        sql = 'SELECT * FROM sessions WHERE started_seq>?'
        args = [after]
        if profile is not None: sql += ' AND profile_id=?'; args.append(profile)
        rows = [dict(r) for r in self.store.db.execute(sql + ' ORDER BY started_seq LIMIT ?', (*args, limit + 1))]
        ids = {r['id'] for r in rows}
        for statement, values in self.store.queue:
            if statement.startswith('INSERT INTO sessions') and values[0] not in ids:
                row = self.session(values[0])
                if row['started_seq'] > after and (profile is None or profile == row['profile_id']): rows.append(row)
        rows.sort(key=lambda r: r['started_seq'])
        return {'sessions': [self.header(self.session(r['id'])) for r in rows[:limit]],
                'has_more': len(rows) > limit, 'next_cursor': rows[min(limit, len(rows))-1]['started_seq'] if rows else after}

    @staticmethod
    def header(row):
        return {key: row[key] for key in ('id', 'profile_id', 'kind', 'started_seq', 'ended_seq', 'clock_status')} | {
            'adapters': json.loads(row['adapter_set']), 'started': row['started_utc'], 'ended': row['ended_utc']}

    def records(self, ident, after=0, limit=501, upper=2**63-1, reverse=False):
        result = []
        for table in ('samples', 'events'):
            order = 'DESC' if reverse else 'ASC'
            rows = [dict(r) for r in self.store.db.execute(
                'SELECT * FROM ' + table + ' WHERE session_id=? AND seq>? AND seq<=? ORDER BY seq ' + order + ' LIMIT ?',
                (ident, after, upper, limit))]
            rows += [r for r in self.pending(table) if r['session_id'] == ident and after < r['seq'] <= upper]
            result += [(r['seq'], table, sample(r) if table == 'samples' else event(r)) for r in rows]
        result.sort(key=lambda r: r[0], reverse=reverse)
        return result[:limit]

    def missing(self, after, latest):
        count = self.store.db.execute('SELECT COUNT(*) FROM (SELECT seq FROM samples WHERE seq>? AND seq<=? UNION ALL SELECT seq FROM events WHERE seq>? AND seq<=?)',
                                      (after, latest, after, latest)).fetchone()[0]
        count += sum(after < r['seq'] <= latest for t in ('samples', 'events') for r in self.pending(t))
        return count != latest - after

    def page(self, ident, after=None, boot_id=None, limit=100):
        latest = self.latest()
        reason = 'BOOT_CHANGED' if boot_id is not None and boot_id != self.store.boot_id else None
        if reason is None and after is not None:
            reason = 'CURSOR_AHEAD' if after > latest else 'RETENTION_GAP' if self.missing(after, latest) else None
        if not self.store.history_available and after is None: reason = reason or 'HISTORY_UNAVAILABLE'
        reset = reason is not None or after is None
        rows = self.records(ident, 0 if reset else after, limit if reset else limit+1, reverse=reset)
        if reset: rows.reverse()
        more = not reset and len(rows) > limit
        rows = rows[:limit]
        cursor = latest if reset or not more else rows[-1][0]
        return {'samples': [r[2] for r in rows if r[1] == 'samples'], 'events': [r[2] for r in rows if r[1] == 'events'],
                'next_cursor': cursor, 'has_more': more, 'gap': reason is not None, 'reset_reason': reason,
                # Contract 1.2: the cursor's monotonic domain, to send back as boot_id.
                'boot_id': self.store.boot_id}

    def summary(self, row):
        ident = row['id']
        # Bounded vocabulary and SQL aggregation keep memory independent of history.
        values = {r[0]: r[1] for r in self.store.db.execute('SELECT metric, MAX(num) FROM samples WHERE session_id=? GROUP BY metric LIMIT 200', (ident,))}
        for r in self.pending('samples'):
            if r['session_id'] == ident and r['num'] is not None: values[r['metric']] = max(values.get(r['metric']) or 0, r['num'])
        readers = self.store.db.execute('SELECT client_name, from_seq, to_seq FROM telemetry_readers WHERE session_id=? ORDER BY client_name', (ident,))
        audit = {r[0]: dict(r) for r in readers}
        for (session, client), value in self.store.reader_queue.items():
            if session == ident:
                old = audit.get(client, value)
                audit[client] = dict(value, from_seq=min(old['from_seq'], value['from_seq']), to_seq=max(old['to_seq'], value['to_seq']))
        gaps = []
        end = row['ended_seq'] or self.latest()
        # Gaps are global journal losses, not normal interleaving of instruments.
        previous = row['started_seq'] - 1
        cursor = self.store.db.execute('SELECT seq FROM (SELECT seq FROM samples UNION ALL SELECT seq FROM events) WHERE seq>=? AND seq<=? ORDER BY seq', (previous, end))
        pending = sorted(r['seq'] for t in ('samples', 'events') for r in self.pending(t) if previous <= r['seq'] <= end)
        import heapq
        for seq in heapq.merge((r[0] for r in cursor), pending):
            if seq > previous + 1: gaps.append({'from_seq': previous+1, 'to_seq': seq-1, 'reason': 'RETENTION_GAP'})
            previous = seq
            if len(gaps) >= 100: break
        if previous < end and len(gaps) < 100: gaps.append({'from_seq':previous+1,'to_seq':end,'reason':'RETENTION_GAP'})
        return self.header(row) | {'frames_accepted': values.get('stack.frames_accepted', 0),
            'frames_rejected': values.get('stack.frames_rejected', 0),
            'reject_reasons': dict([(k.removeprefix('stack.reject_reason.'), v) for k,v in sorted(values.items()) if k.startswith('stack.reject_reason.')][:128]),
            'pointing_failures': self.store.db.execute("SELECT COUNT(*) FROM samples WHERE session_id=? AND metric='error.code' AND text='pointing_failed'", (ident,)).fetchone()[0] + sum(r['session_id'] == ident and r['metric'] == 'error.code' and r['text'] == 'pointing_failed' for r in self.pending('samples')), 'gaps': gaps, 'readers': list(audit.values())[:16]}

    def export(self, row, format, client=None):
        """A fixed sequence ceiling, pages <=128; lock is released between chunks."""
        with self.store.lock:
            upper = self.latest()
            header = self.summary(row)
        encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        if format == 'csv': yield b'seq,utc,utc_corrected,metric,value\r\n'
        else:
            dictionary = {m: ('boolean' if m in BOOLEAN else 'number' if m in NUMERIC else 'string') for m in sorted(BOOLEAN | NUMERIC | TEXT)}
            dictionary['stack.reject_reason.*'] = 'number'
            yield b'{"session":' + encode(header) + b',"metrics_dictionary":' + encode(dictionary) + b',"samples":['
        for table in (('samples',) if format == 'csv' else ('samples','events')):
            after, first = 0, True
            if table == 'events': yield b'],"events":['
            while True:
                with self.store.lock: rows = self.records(row['id'], after, 128, upper)
                if not rows: break
                fetched = [r[0] for r in rows if r[1] == 'samples']
                if table == 'samples' and client and fetched:
                    with self.store.lock: self.store.reader(row['id'], client, fetched[0], fetched[-1])
                for seq, kind, value in rows:
                    if kind != table: continue
                    if format == 'csv':
                        line = io.StringIO(newline=''); writer = csv.writer(line)
                        writer.writerow([value[k] if value[k] is not None else '' for k in ('seq','utc','utc_corrected','metric','value')])
                        yield line.getvalue().encode()
                    else:
                        yield (b'' if first else b',') + encode(value)
                        first = False
                after = rows[-1][0]
        if format == 'json': yield b']}'
