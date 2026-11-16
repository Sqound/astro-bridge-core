"""Owner-controlled local telemetry resources; no observation commands."""
import json
import re

from ..api import ApiError
from ..config import profiles
from ..plugins import plugin
from .query import Query
from .settings import selected_settings


def invalid():
    return ApiError(400, 'INVALID_FIELDS', 'Unknown or read-only fields in request.')


def integer(value, default, maximum):
    if value is None: return default
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,19}', value) or int(value) > maximum: raise invalid()
    return int(value)


class TelemetryAPI:
    def __init__(self, api):
        self.api = api
        self.store = api.controller.telemetry_store
        self.query = Query(self.store)

    def settings(self): return self.store.settings

    def status(self):
        controller = self.api.controller
        diagnostics = controller.seestar_collector.status()
        for ident, row in controller.vespera_collector.status().items():
            diagnostics.setdefault(ident, []).append(dict(row, adapter_id='vespera_status_v1', unavailable_reason=None))
        if controller.demo and hasattr(controller, 'demo_telemetry'):
            diagnostics.update(controller.demo_telemetry.status())
        from .cards import project
        with self.store.lock:
            pending = sum(sql.startswith('INSERT INTO sessions') for sql, _ in self.store.queue)
            effective = {p['id']:selected_settings(self.settings(), p) for p in profiles(self.api.controller.store.config)}
            return {'enabled': any(p['adapters'] for p in effective.values()), 'profiles': effective, 'adapter_status':diagnostics,
                    'persistent': self.store.persistent, 'retention': {'nights': self.store.nights, 'max_bytes': self.store.max_bytes},
                    'used_bytes': self.store._used_bytes(),
                    'sessions_count': self.store.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] + pending,
                    'clock_status': self.store.clock.read()['clock_status'], 'boot_id': self.store.boot_id,
                    'latest_seq': self.query.latest(), 'config_revision': self.api.revision(), 'card_data':project(controller)}

    def handle(self, method, path, query, data, expected, client=None):
        parts = path.strip('/').split('/')
        allowed = {'profile_id','after','limit'} if path == '/telemetry/sessions' and method == 'GET' else {'session_id','after','boot_id','limit'} if path == '/telemetry/samples' else {'format'} if len(parts) == 4 and parts[-1] == 'export' else set()
        if set(query) - allowed or any(len(v) != 1 or not v[0] for v in query.values()): raise invalid()
        q = {k:v[0] for k,v in query.items()}
        with self.api.controller.lock, self.api.controller.telemetry_sessions.lock, self.store.lock:
            if method == 'GET':
                if path == '/telemetry': return self.status()
                if path == '/telemetry/sessions':
                    limit = integer(q.get('limit'), 100, 100)
                    if not limit: raise invalid()
                    profile = q.get('profile_id')
                    if profile is not None and not re.fullmatch('[a-z0-9-]{1,32}', profile): raise invalid()
                    return self.query.sessions(profile, integer(q.get('after'), 0, 2**63-1), limit)
                if path == '/telemetry/samples':
                    ident = q.get('session_id')
                    row = self.require(ident)
                    limit = integer(q.get('limit'), 100, 500)
                    boot = q.get('boot_id')
                    if not limit or boot is not None and not re.fullmatch('[0-9a-f]{32}', boot): raise invalid()
                    page = self.query.page(row['id'], integer(q.get('after'), None, 2**63-1), boot, limit)
                    if client and page['samples']: self.store.reader(row['id'], client, page['samples'][0]['seq'], page['samples'][-1]['seq'])
                    return page
                if len(parts) in (3,4) and parts[:2] == ['telemetry','sessions']:
                    row = self.require(parts[2])
                    if len(parts) == 3: return self.query.summary(row)
                    if parts[3] == 'analysis':
                        from .analysis import project
                        return project(self.query, row)
                    if parts[3] == 'export':
                        format = q.get('format', 'json')
                        if format not in ('csv','json'): raise invalid()
                        return (row['kind'] + '-' + row['started_utc'][:10] + '.' + format, format, self.query.export(row, format, client))
            if method == 'PATCH' and path == '/telemetry/settings':
                self.precondition(expected)
                self.api.fields(data, {'retention_nights','max_bytes','profiles'})
                current = self.settings(); value = dict(current, **data)
                configured = {p['id']:p for p in profiles(self.api.controller.store.config)}
                retained = {ident:selected_settings(current, configured[ident])
                            for ident in current['profiles'] if ident in configured}
                if 'profiles' in data and not isinstance(data['profiles'], dict): raise invalid()
                value['profiles'] = dict(retained)
                for ident, selection in data.get('profiles', {}).items():
                    if not isinstance(selection, dict) or 'adapters' not in selection: raise invalid()
                    value['profiles'][ident] = dict(retained.get(ident, {}), **selection)
                if type(value['retention_nights']) is not int or not 1 <= value['retention_nights'] <= 365: raise invalid()
                if type(value['max_bytes']) is not int or not 4*1024**2 <= value['max_bytes'] <= 512*1024**2: raise invalid()
                if not isinstance(value['profiles'], dict) or len(value['profiles']) > 24: raise invalid()
                for ident, selection in value['profiles'].items():
                    if ident not in configured: raise invalid()
                    self.api.fields(selection, {'adapters','alpaca_interval_seconds'})
                    cadence = selection.get('alpaca_interval_seconds', 15)
                    if type(cadence) is not int or not 10 <= cadence <= 120: raise invalid()
                    adapters = selection.get('adapters')
                    allowed = [a['id'] if isinstance(a, dict) else a for a in plugin(configured[ident]['kind'])['telemetry']['adapters']]
                    if not isinstance(adapters, list) or len(adapters) > 16 or any(not isinstance(a,str) or a not in allowed for a in adapters) or len(set(adapters)) != len(adapters): raise invalid()
                with self.store.db:
                    self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES ('settings', ?)", (json.dumps(value, sort_keys=True),))
                self.store.settings = value
                self.store.nights, self.store.max_bytes = value['retention_nights'], value['max_bytes']
                return self.status()
            if method == 'DELETE' and (path == '/telemetry/sessions' or len(parts) == 3 and parts[:2] == ['telemetry','sessions']):
                self.precondition(expected); self.api.fields(data, set())
                ident = parts[2] if len(parts) == 3 else None
                if ident: self.require(ident)
                # End active sessions first, so adapters can start new sessions on their next activity.
                sessions = self.api.controller.telemetry_sessions
                with sessions.lock:
                    for profile, current in list(sessions.active.items()):
                        if ident is None or current['id'] == ident: sessions.end(profile)
                self.store.flush()
                with self.store.db:
                    if ident: self.store.db.execute('DELETE FROM sessions WHERE id=?', (ident,))
                    else: self.store.db.execute('DELETE FROM sessions')
                self.store.session_cache = {k:v for k,v in self.store.session_cache.items() if ident is not None and k != ident}
                self.store.coverage_queue = {k:v for k,v in self.store.coverage_queue.items() if ident is not None and k != ident}
                self.store.reader_queue = {k:v for k,v in self.store.reader_queue.items() if ident is not None and k[0] != ident}
                return {'ok': True, 'config_revision': self.api.revision()}
            raise ApiError(405, 'METHOD_NOT_ALLOWED', 'Unsupported API route or method.')

    def precondition(self, expected):
        self.api.controller.protection.precondition(expected, self.api.revision)

    def require(self, ident):
        if not isinstance(ident, str) or not re.fullmatch('[a-z0-9-]{1,64}', ident): raise invalid()
        row = self.query.session(ident)
        if row is None: raise ApiError(404, 'NOT_FOUND', 'Unknown API resource.')
        return row
