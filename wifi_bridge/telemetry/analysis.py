"""Bounded retained-session chart and conservative observed exposure projection."""
import json


BUCKETS = 240
EXPOSURE_ROWS = 50000
INTERVALS = 128
SQL_BUDGET = 20000  # Progress callbacks, each after 1,000 SQLite VM instructions.


def merged(ranges):
    result = []
    for first, last in sorted(ranges):
        if result and first <= result[-1][1] + 1:
            result[-1][1] = max(last, result[-1][1])
        else:
            result.append([first, last])
    return result


def coverage(store, ident):
    row = store.db.execute('SELECT * FROM telemetry_coverage WHERE session_id=?', (ident,)).fetchone()
    pending = store.coverage_queue.get(ident)
    if not row:
        new = next((args for sql,args in store.queue if sql.startswith('INSERT INTO sessions') and args[0]==ident), None)
        if new: row = {'unknown_before_seq':new[4]-1, 'ranges_json':'[]'}
    if row:
        unknown, ranges = row['unknown_before_seq'], json.loads(row['ranges_json'])
    else:
        unknown, ranges = None, []  # Legacy sessions have no negative evidence.
    if pending:
        ranges = merged(ranges + pending['ranges'])
        if unknown is not None: unknown = max(unknown, pending['unknown_before_seq'])
    if len(ranges) > INTERVALS:
        if unknown is not None: unknown = max(unknown, ranges[-INTERVALS-1][1])
        ranges = ranges[-INTERVALS:]
    if store.coverage_degraded: unknown = None
    return {'unknown_before_seq': unknown, 'fetched': [{'from_seq':a,'to_seq':b} for a,b in ranges]}


def _project(query, row):
    store, ident = query.store, row['id']
    pending = [r for r in query.pending('samples') if r['session_id'] == ident]
    bounds = store.db.execute('SELECT MIN(mono_ms), MAX(mono_ms), COUNT(*) FROM samples WHERE session_id=?', (ident,)).fetchone()
    times = [r['mono_ms'] for r in pending] + [v for v in bounds[:2] if v is not None]
    first, last = (min(times), max(times)) if times else (0, 0)
    width = max(1, (last-first+1+BUCKETS-1)//BUCKETS)
    metrics = ('stack.frames_accepted', 'stack.frames_rejected')
    # SQLite aggregates the retained journal, returning <=480 rows regardless of
    # session length. Pending writes merge without forcing the journal to disk.
    buckets = {}
    sql = '''SELECT CAST((mono_ms-?)/? AS INTEGER) AS bucket, metric,
        MIN(seq) AS from_seq, MAX(seq) AS to_seq, MIN(num) AS low, MAX(num) AS high,
        MIN(mono_ms) AS mono_ms, MIN(COALESCE(utc_corrected,utc)) AS utc
        FROM samples WHERE session_id=? AND metric IN (?,?) GROUP BY bucket, metric'''
    for item in store.db.execute(sql, (first, width, ident, *metrics)):
        buckets[(item['bucket'],item['metric'])] = dict(item)
    for item in pending:
        if item['metric'] not in metrics: continue
        key = ((item['mono_ms']-first)//width, item['metric'])
        b = buckets.setdefault(key, {'bucket':key[0], 'metric':item['metric'], 'from_seq':item['seq'], 'to_seq':item['seq'],
                                    'low':item['num'], 'high':item['num'], 'mono_ms':item['mono_ms'], 'utc':item['utc_corrected'] or item['utc']})
        b.update(from_seq=min(b['from_seq'],item['seq']), to_seq=max(b['to_seq'],item['seq']),
                 low=min(b['low'],item['num']), high=max(b['high'],item['num']))
    points = [dict(value) for _,value in sorted(buckets.items())]
    for value in points: del value['bucket']
    # A stable exposure setting on both sides of a counter increment is observed
    # evidence only. Counter resets, missing baselines and changed settings are
    # not repaired by multiplying the last exposure by a cumulative counter.
    records = store.db.execute("SELECT * FROM samples WHERE session_id=? AND metric IN ('stack.frames_accepted','exposure.seconds') ORDER BY seq LIMIT ?", (ident,EXPOSURE_ROWS+1))
    import heapq
    pending_exposure = sorted((r for r in pending if r['metric'] in ('stack.frames_accepted','exposure.seconds')), key=lambda r:r['seq'])
    stream = heapq.merge((dict(r) for r in records), pending_exposure, key=lambda r:r['seq'])
    seconds, count, previous, exposure, changed, seen = 0, 0, None, None, False, False
    complete = store.history_available
    for index, item in enumerate(stream):
        if index >= EXPOSURE_ROWS: complete=False; break
        value = item['num']
        if item['metric'] == 'exposure.seconds':
            if exposure != value: changed=True
            exposure = value if value >= 0 else None
            continue
        if previous is None:
            complete = complete and value == 0
        else:
            delta = value-previous
            if delta < 0 or changed or exposure is None: complete=False
            elif delta > 0: seconds += delta*exposure; count += delta; seen=True
        previous=value;changed=False
    last_exposure = store.db.execute("SELECT num, seq FROM samples WHERE session_id=? AND metric='exposure.seconds' ORDER BY seq DESC LIMIT 1", (ident,)).fetchone()
    latest_seconds = last_exposure[0] if last_exposure else None
    for item in pending_exposure:
        if item['metric']=='exposure.seconds': latest_seconds=item['num']
    summary=query.summary(row)
    complete = complete and not summary['gaps'] and previous is not None
    # Any journal gap may hide a setting change; the observed total is unknown.
    if summary['gaps']: seconds=None;count=0;seen=False
    return {'session_id':ident, 'sample_count':bounds[2]+len(pending), 'bucket_count':BUCKETS,
            'from_mono_ms':first, 'to_mono_ms':last, 'points':points,
            'history_available':store.history_available, 'gaps':summary['gaps'],
            'reader_coverage':coverage(store,ident),
            'exposure':{'observed_seconds':seconds if seen else None,'observed_frames':count,
                        'complete':complete, 'latest_seconds':latest_seconds if latest_seconds is not None and latest_seconds>=0 else None}}


def project(query, row):
    # A SQLite VM budget prevents a large retained session monopolizing the
    # appliance. No partial projection is ever presented as complete.
    import sqlite3
    work = [0]
    def budget():
        work[0] += 1
        return work[0] > SQL_BUDGET
    query.store.db.set_progress_handler(budget, 1000)
    try:
        result = _project(query, row)
        result['limited'] = False
        return result
    except sqlite3.OperationalError as failure:
        if 'interrupted' not in str(failure): raise
        return {'session_id':row['id'], 'sample_count':None, 'bucket_count':BUCKETS,
                'from_mono_ms':0, 'to_mono_ms':0, 'points':[], 'limited':True,
                'history_available':query.store.history_available, 'gaps':[],
                'reader_coverage':{'unknown_before_seq':None,'fetched':[]},
                'exposure':{'observed_seconds':None,'observed_frames':0,'complete':False,'latest_seconds':None}}
    finally:
        query.store.db.set_progress_handler(None, 0)
