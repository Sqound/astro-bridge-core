"""Bounded card projections. Receipt clocks are independent of journal batching."""
from datetime import datetime, timedelta

from .metrics import validate
from .query import Query
from .settings import selected_adapters

# A card never exposes raw protocol dictionaries or an unbounded metric map.
METRICS = frozenset('''operation.kind operation.state operation.step target.name
stack.frames_accepted stack.frames_rejected exposure.seconds battery.percent
battery.charging device.temperature_c env.temperature_c env.humidity_pct
env.dew_point_c filter.name heater.state target.ra_hours target.dec_deg
mount.tracking mount.slewing mount.at_park camera.state link.rssi_dbm
link.bitrate_mbps link.ping_ms link.loss_pct'''.split())


class Readings:
    def __init__(self, store):
        self.store = store
        self.rows = {}

    def receive(self, ident, adapter, values, cadence, received=None):
        now = self.store.monotonic() if received is None else received
        if ident not in self.rows and len(self.rows) >= 24: return
        if not 0 < cadence <= 120: raise ValueError('Invalid receipt cadence')
        clock = self.store.clock.read()
        utc = clock.get('corrected_utc') or clock['system_utc']
        delay = max(0, self.store.monotonic()-now)
        utc = (datetime.fromisoformat(utc)-timedelta(seconds=delay)).isoformat()
        target = self.rows.setdefault(ident, {}).setdefault(adapter, {})
        for metric, value in values.items():
            if metric not in METRICS: continue
            validate(metric, value)
            prior = target.get(metric)
            target[metric] = {'value':value, 'received':now, 'utc':utc, 'cadence_seconds':cadence,
                              'since':prior['since'] if prior and prior['value'] == value else utc}

    def clear(self, ident, adapter):
        self.rows.get(ident, {}).pop(adapter, None)

    def snapshot(self, ident, adapters):
        now = self.store.monotonic()
        result = {}
        for adapter, values in self.rows.get(ident, {}).items():
            if adapter not in adapters: continue
            for metric, row in values.items():
                age = max(0, now-row['received'])
                candidate = {k:row[k] for k in ('value','utc','since','cadence_seconds')}
                candidate.update(adapter_id=adapter, age_seconds=round(age, 3), stale=age>3*row['cadence_seconds'])
                if metric not in result or age < result[metric]['age_seconds']: result[metric] = candidate
        return result


def recent(query, ident):
    # One latest session per observing night, with a fourth night only to mark
    # clipping. SQL aggregates session headers, never full sample histories.
    selected = query.store.db.execute("""SELECT id, MAX(started_seq) FROM sessions
        WHERE profile_id=? GROUP BY date(started_utc, '-12 hours')
        ORDER BY MAX(started_seq) DESC LIMIT 4""", (ident,)).fetchall()
    rows = [dict(query.store.db.execute('SELECT * FROM sessions WHERE id=?', (seq[0],)).fetchone()) for seq in selected]
    seen = {row['id'] for row in rows}
    for sql, values in query.store.queue:
        if sql.startswith('INSERT INTO sessions') and values[1] == ident and values[0] not in seen:
            rows.append(query.session(values[0])); seen.add(values[0])
    rows.sort(key=lambda row:row['started_seq'], reverse=True)
    nights = {}
    for row in rows:
        night = (datetime.fromisoformat(row['started_utc'])-timedelta(hours=12)).date()
        nights.setdefault(night, query.session(row['id']))
    return list(nights.values())[:4]


def history(query, ident, now):
    rows = recent(query, ident)
    # Tonight is the bridge clock's noon-to-noon observing date (UTC offset as
    # reported by the clock). Unknown clocks remain explicitly unverified.
    night = (datetime.fromisoformat(now)-timedelta(hours=12)).date()
    targets = {}
    partial = len(rows)>3
    count = query.store.db.execute('SELECT COUNT(*) FROM sessions WHERE profile_id=?', (ident,)).fetchone()[0]
    count += sum(sql.startswith('INSERT INTO sessions') and values[1]==ident for sql,values in query.store.queue)
    partial = partial or count>len(rows)
    summaries = []
    for row in rows[:3]:
        records = query.records(row['id'], limit=501, reverse=True)
        counts = {metric:next((item['value'] for _, table, item in records if table=='samples' and item['metric']==metric), None)
                  for metric in ('stack.frames_accepted','stack.frames_rejected')}
        summaries.append({'id':row['id'],'started':row['started_utc'],'ended':row['ended_utc'],
                          'clock_status':row['clock_status'],'frames_accepted':counts['stack.frames_accepted'],
                          'frames_rejected':counts['stack.frames_rejected']})
        if (datetime.fromisoformat(row['ended_utc'] or now)-timedelta(hours=12)).date()!=night: continue
        partial = partial or len(records)>500
        target = (next((item['value'] for _,table,item in reversed(records[:500]) if table=='samples' and item['metric']=='target.name'), None)
                  if len(records)<=500 else None)
        counters = {}
        for _, table, sample in reversed(records[:500]):
            if table!='samples': continue
            metric, value = sample['metric'], sample['value']
            if metric=='target.name': target=value
            if metric not in ('stack.frames_accepted','stack.frames_rejected'): continue
            # Counters are cumulative within a generation; a reset starts a new
            # contribution. A clipped window cannot claim a complete total.
            previous = counters.get(metric, 0)
            delta = value-previous if value>=previous else value
            counters[metric]=value
            if target is None: continue
            if target not in targets and len(targets)>=8: partial=True; continue
            totals=targets.setdefault(target, {'target':target,'frames_accepted':None,'frames_rejected':None})
            field = metric.removeprefix('stack.')
            totals[field]=(totals[field] or 0)+delta
    return {'recent':summaries, 'tonight':list(targets.values()), 'history_partial':partial}


def project(controller):
    store = controller.telemetry_store
    query = Query(store)
    clock = store.clock.read()
    now = clock.get('corrected_utc') or clock['system_utc']
    from ..config import profiles
    result = {}
    cache = getattr(controller, '_card_history', {})
    controller._card_history = cache
    configured = profiles(controller.store.config)
    for ident in list(cache):
        if ident not in {p['id'] for p in configured}: del cache[ident]
    for profile in configured:
        ident=profile['id']
        rows = recent(query, ident)
        signature = ((datetime.fromisoformat(now)-timedelta(hours=12)).date().isoformat(), tuple((row['id'], row['ended_seq'], next(iter(query.records(row['id'], limit=1, reverse=True)), (0,))[0]) for row in rows))
        if ident not in cache or cache[ident][0] != signature:
            cache[ident] = (signature, history(query, ident, now))
        result[ident] = {'readings':controller.telemetry_sessions.readings.snapshot(ident, selected_adapters(store.settings, profile)),
                         **cache[ident][1]}
    return result
