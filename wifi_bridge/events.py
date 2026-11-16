"""Bounded local connection events; HTTP reads never generate transitions."""
import copy
import json
import re
from pathlib import Path
import threading
import time
import uuid

from .config import atomic_json, profiles, network_intent
from .clock import Clock

TYPES = {'camera_detected', 'camera_removed', 'camera_power_unstable', 'requested', 'disconnecting', 'disconnected', 'connecting', 'associated',
         'waiting', 'connected', 'failed', 'restored', 'automatic_switch_deferred', 'service_changed', 'hotspot_restored', 'hotspot_unavailable', 'announcement_updated'}
CAUSES = {'MANUAL', 'API', 'CONFIGURATION', 'RECONCILE', 'AUTOMATIC', 'SEESTAR_DISCOVERY', 'VESPERA_CONNECTION', 'DWARF_DISCOVERY', 'ZWO_MOUNT_CONNECTION', 'SYNSCAN_DISCOVERY', 'PLUGIN_CONNECTION'}
REASONS = {'CAMERA_DETECTED', 'CAMERA_REMOVED', 'CAMERA_POWER_UNSTABLE', 'REQUESTED', 'DISCONNECTING', 'CONNECTING', 'ASSOCIATED', 'READY', 'OPERATION_RUNNING',
           'CONNECTION_LOST', 'RADIO_UNAVAILABLE', 'HOME_UNAVAILABLE',
           'AWAITING_ASSOCIATION', 'SERVICE_UNAVAILABLE', 'CONFIGURATION_CONFLICT',
           'INVALID_TARGET', 'ASSOCIATION_FAILED', 'RECONCILE_FAILED',
           'ANNOUNCEMENT_UPDATED', 'HOTSPOT_RESTORED', 'HOTSPOT_RADIO_UNAVAILABLE', 'HOTSPOT_START_FAILED',
           'SERVICE_READY', 'SERVICE_UNKNOWN', 'PROFILE_DISABLED', 'PROFILE_REMOVED', 'AUTOMATIC_DEFERRED', 'WIFI_AUTHENTICATION_FAILED'}
FIELDS = {'sequence', 'boot_id', 'timestamp', 'clock_status', 'operation_id', 'profile_id',
          'profile_name', 'radio_id', 'type', 'cause', 'reason', 'outcome', 'associated', 'ready'}


def valid_interfaces(value):
    return (isinstance(value, list) and len(value) <= 2 and
            all(role in ('home', 'hotspot') for role in value) and len(set(value)) == len(value))


NATIVE_FIELDS = {
    'camera.detected': set(), 'camera.removed': set(), 'camera.power_unstable': set(),
    # Contract 1.3 proposal: a serial adapter appeared or disappeared (opaque id, no path).
    'serial.detected': set(), 'serial.removed': set(),
    'instrument.state': {'link', 'service'}, 'instrument.lost': {'reason'},
    'instrument.restored': set(), 'instrument.moved': {'from_radio_id', 'to_radio_id'},
    'instrument.endpoint_changed': set(),  # Addresses are hydrated only at HTTP read.
    'lease.expiring': {'lease_id', 'expires_in'}, 'lease.ended': {'lease_id', 'reason'},
    'telemetry.session_started': {'session_id'}, 'telemetry.session_ended': {'session_id'},
    'telemetry.available': {'session_id', 'latest_sample_seq'},
    'telemetry.adapter_yielded': {'adapter_id', 'retry_after'},
    'telemetry.adapter_refused': {'adapter_id'},
    # Contract 1.1 switch progress and completion notice for one connection operation.
    'instrument.switch_progress': {'operation', 'phase', 'eta_seconds'},
    'instrument.switch_completed': {'operation', 'outcome', 'reason'},
}
# Concrete camera.* event shapes and emissions.
RESERVED_NAMESPACES = {'camera'}
NATIVE_BASE = {'sequence', 'boot_id', 'timestamp', 'clock_status', 'instrument_id', 'type'}
JOURNAL_LIMIT = 1024
JOURNAL_BYTES = 1024 * 1024


def valid_event(event):
    if not isinstance(event, dict): return False
    fields = set(event) - {'utc_corrected'}
    if 'utc_corrected' in event and not isinstance(event['utc_corrected'], str): return False
    kind = event.get('type')
    if kind in NATIVE_FIELDS:
        if fields != NATIVE_BASE | NATIVE_FIELDS[kind]: return False
        data = {key: event[key] for key in NATIVE_FIELDS[kind]}
        return valid_native(kind, event.get('instrument_id'), data)
    return (fields - {'interfaces'} == FIELDS and kind in TYPES
            and event.get('cause') in CAUSES and event.get('reason') in REASONS
            and (valid_interfaces(event.get('interfaces')) if kind == 'announcement_updated'
                 else 'interfaces' not in event))


def valid_native(kind, ident, data):
    if kind not in NATIVE_FIELDS or set(data) != NATIVE_FIELDS[kind]: return False
    if not isinstance(ident, str) or not re.fullmatch('[a-z0-9-]{1,32}', ident): return False
    if any(not isinstance(v, str) or not 1 <= len(v) <= 128 for k, v in data.items()
           if k not in ('expires_in', 'latest_sample_seq', 'retry_after', 'eta_seconds')): return False
    if kind.startswith('instrument.switch_') and not re.fullmatch('[0-9a-f]{32}\\.[0-9a-f]{32}', data['operation']): return False
    if kind == 'instrument.switch_progress':
        eta = data['eta_seconds']
        return (data['phase'] in ('leaving', 'associating', 'verifying', 'restoring')
                and type(eta) is int and 1 <= eta <= 120)
    if kind == 'instrument.switch_completed':
        return data['outcome'] in ('ready', 'failed') and re.fullmatch('[A-Z][A-Z0-9_]{0,63}', data['reason']) is not None
    if kind == 'telemetry.adapter_refused': return data['adapter_id'] == 'seestar_events_v1'
    if kind == 'telemetry.adapter_yielded':
        return data['adapter_id'] in ('vespera_status_v1','seestar_events_v1') and type(data['retry_after']) is int and data['retry_after'] == 600
    if kind == 'instrument.state':
        return data['link'] in ('connected', 'connecting', 'available', 'saved', 'disabled') and data['service'] in ('ready', 'not_answering', 'unknown', 'not_probed')
    if kind == 'instrument.lost': return data['reason'] in ('LINK_LOST', 'RADIO_UNAVAILABLE')
    if kind == 'lease.ended': return data['reason'] in ('EXPIRED', 'RELEASED')
    if kind == 'instrument.moved':
        return all(re.fullmatch('radio-[0-9a-f]{16}', v) for v in data.values())
    for key in ('lease_id', 'session_id'):
        if key in data and not re.fullmatch('[a-zA-Z0-9-]{1,128}', data[key]): return False
    for key in ('expires_in', 'latest_sample_seq'):
        if key in data and (type(data[key]) is not int or not 0 <= data[key] <= 2**63 - 1): return False
    return True


class EventStore:
    """256 live records, 1,024 disk records, one atomic write per ten seconds."""
    def __init__(self, path, capacity=256, clock=None, *, monotonic=None):
        self.path = Path(path)
        self.clock = clock or Clock(self.path.with_name('clock.json'))
        self.monotonic = monotonic or (lambda: time.monotonic())
        self.capacity = min(256, max(1, capacity))
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.waiters = 0
        self.saturated_reads = 0
        self.closed = False
        self.observed_events = {}
        self.instrument_sequences = {}
        self.boot_id = uuid.uuid4().hex
        self.sequence = 0
        self.events = []
        self.persistent = True
        self.history_available = True
        self.dirty = False
        self.last_write = None
        self.write_count = 0
        try:
            data = self._load()
            self.sequence = data['sequence']
            self.events = data['events'][-self.capacity:]
        except FileNotFoundError:
            self._persist([])
        except (OSError, ValueError, KeyError, TypeError):
            self.history_available = False
            self._persist([])

    def _load(self):
        if self.path.stat().st_size > JOURNAL_BYTES: raise ValueError('Oversized journal')
        data = json.loads(self.path.read_text())
        if (not isinstance(data, dict) or data.get('format') != 1 or
                type(data.get('sequence')) is not int or not 0 <= data['sequence'] <= 2**63 - 1 or
                not isinstance(data.get('events'), list) or len(data['events']) > JOURNAL_LIMIT):
            raise ValueError('Invalid journal')
        previous = 0
        for event in data['events']:
            if (not valid_event(event) or type(event['sequence']) is not int or
                    not previous < event['sequence'] <= data['sequence']):
                raise ValueError('Invalid event')
            previous = event['sequence']
        return data

    def _history(self):
        try: old = self._load()['events']
        except (OSError, ValueError, KeyError, TypeError): old = []
        merged = {event['sequence']: event for event in old}
        merged.update((event['sequence'], event) for event in self.events)
        return sorted(merged.values(), key=lambda event: event['sequence'])[-JOURNAL_LIMIT:]

    def track_instruments(self, identifiers):
        identifiers = set(identifiers)
        if len(identifiers) > 32: raise ValueError('Too many instruments')
        with self.lock:
            if identifiers == self.instrument_sequences.keys(): return
            history = self._history()
            self.instrument_sequences = {ident: self.instrument_sequences.get(ident, max(
                (event['sequence'] for event in history if event.get('instrument_id', event.get('profile_id')) == ident), default=0))
                for ident in identifiers}

    def _persist(self, events):
        self.last_write = self.monotonic()  # Failed attempts are throttled too.
        try:
            payload = {'format': 1, 'sequence': self.sequence, 'events': events}
            if len(json.dumps(payload, ensure_ascii=False, indent=2).encode()) > JOURNAL_BYTES:
                raise OSError('Journal capacity exceeded')
            atomic_json(self.path, payload)
            self.write_count += 1
            self.persistent = True
            self.dirty = False
        except OSError:
            self.persistent = False

    def flush(self):
        with self.lock:
            if self.dirty and (self.last_write is None or self.monotonic() - self.last_write >= 10):
                self._persist(self._history())

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
            self.flush()  # Never bypass the storage write-rate bound on shutdown.

    def _append(self, fields):
        with self.condition:
            self.sequence += 1
            clock = self.clock.read()
            event = dict(sequence=self.sequence, boot_id=self.boot_id,
                         timestamp=clock['system_utc'], clock_status=clock['clock_status'], **fields)
            if clock['source'] == 'client_hint' or clock['offset_ms']:
                event['utc_corrected'] = clock['corrected_utc']
            ident = fields.get('instrument_id', fields.get('profile_id'))
            if ident in self.instrument_sequences: self.instrument_sequences[ident] = self.sequence
            self.events.append(event)
            self.events = self.events[-self.capacity:]
            self.observed_events[event['sequence']] = self.monotonic()
            self.observed_events = dict(list(self.observed_events.items())[-self.capacity:])
            self.dirty = True
            self.flush()
            self.condition.notify_all()
            return copy.deepcopy(event)

    def append(self, event_type, *, operation_id, profile_id, profile_name, radio_id,
               cause, reason, outcome, associated=False, ready=False, interfaces=None):
        if event_type not in TYPES or cause not in CAUSES or reason not in REASONS:
            raise ValueError('Unknown event code')
        if (event_type == 'announcement_updated' and not valid_interfaces(interfaces)) or (event_type != 'announcement_updated' and interfaces is not None):
            raise ValueError('Invalid announcement interfaces')
        fields = dict(operation_id=operation_id, profile_id=profile_id, profile_name=profile_name,
                      radio_id=radio_id, type=event_type, cause=cause, reason=reason, outcome=outcome,
                      associated=bool(associated), ready=bool(ready))
        if interfaces is not None: fields['interfaces'] = interfaces[:]
        return self._append(fields)

    def native(self, kind, instrument_id, **fields):
        if not valid_native(kind, instrument_id, fields): raise ValueError('Invalid native event')
        return self._append(dict(type=kind, instrument_id=instrument_id, **fields))

    def age(self, sequence):
        """Process-local event age: persisted replay is never a fresh push notice."""
        with self.lock:
            observed = self.observed_events.get(sequence)
            return None if observed is None else max(0, self.monotonic() - observed)

    def read(self, after=None, boot_id=None, limit=50, wait=0):
        if type(limit) is not int or not 1 <= limit <= 100 or (after is not None and (type(after) is not int or not 0 <= after <= 2**63 - 1)):
            raise ValueError('Invalid event cursor or limit')
        if type(wait) is not int or not 0 <= wait <= 25: raise ValueError('Invalid event wait')
        if boot_id is not None and (not isinstance(boot_id, str) or not re.fullmatch(r'[0-9a-f]{32}', boot_id)):
            raise ValueError('Invalid boot ID')
        with self.condition:
            page = self._page(after, boot_id, limit)
            if wait and after is not None and not page['events'] and not page['gap'] and not self.closed and self.waiters < 4:
                self.waiters += 1
                deadline = self.monotonic() + wait
                try:
                    while not self.closed and self.sequence == after:
                        remaining = deadline - self.monotonic()
                        if remaining <= 0: break
                        self.condition.wait(remaining)
                    page = self._page(after, boot_id, limit)
                finally: self.waiters -= 1
            elif wait and after is not None and not page['events'] and not page['gap'] and not self.closed:
                # All four waits are taken: answer now and tell the caller when to retry.
                self.saturated_reads += 1
                return dict(page, _retry_after=2)
            return page

    def _page(self, after, boot_id, limit):
        events = self._history()
        oldest = events[0]['sequence'] if events else self.sequence + 1
        reason = 'BOOT_CHANGED' if boot_id is not None and boot_id != self.boot_id else None
        if reason is None and after is not None:
            reason = 'CURSOR_AHEAD' if after > self.sequence else 'RETENTION_GAP' if after < oldest - 1 else None
            # More than 256 events between disk batches may lose unflushed entries.
            sequences = [e['sequence'] for e in events if e['sequence'] > after]
            if reason is None and sequences and (len(sequences) != self.sequence - after or sequences[0] != after + 1):
                reason = 'RETENTION_GAP'
        if not self.history_available and after is None: reason = reason or 'HISTORY_UNAVAILABLE'
        if after is None or reason:
            selected, cursor, more = events[-limit:], self.sequence, False
        else:
            available = [e for e in events if e['sequence'] > after]
            selected = available[:limit]
            cursor = selected[-1]['sequence'] if selected else after
            more = len(available) > len(selected)
        return dict(boot_id=self.boot_id, oldest_sequence=oldest, latest_sequence=self.sequence,
                    next_cursor=cursor, has_more=more, gap=reason is not None,
                    reset_reason=reason, persistent=self.persistent, history_available=self.history_available,
                    clock_status=self.clock.read()['clock_status'], events=copy.deepcopy(selected))


class ConnectionEvents:
    """Translate confirmed reconcile observations into one shared event path."""
    def __init__(self, store):
        self.store = store
        self.contexts = {}
        self.states = {}
        self.radios = {}
        self.leased = lambda ident: False

    def radio(self, mac):
        if not mac:
            return None
        return self.radios.setdefault(mac, 'radio-' + uuid.uuid4().hex[:16])

    def context(self, profile):
        ident = profile['id']
        if ident not in self.contexts:
            self.contexts[ident] = {'operation_id': uuid.uuid4().hex, 'cause': 'RECONCILE'}
        return dict(self.contexts[ident])

    def request(self, previous, current, cause, selected=None, operation_id=None):
        before = {p['id']: p for p in profiles(previous)}
        after = {p['id']: p for p in profiles(current)}
        affected = [ident for ident in dict.fromkeys([*before, *after])
                    if (network_intent(before.get(ident)) != network_intent(after.get(ident)) and
                        (before.get(ident, {}).get('enabled') or after.get(ident, {}).get('enabled'))) or ident == selected]
        if not affected:
            return None
        context = {'operation_id': operation_id or uuid.uuid4().hex, 'cause': cause}
        for ident in affected:
            profile = after.get(ident) or before[ident]
            self.contexts[ident] = dict(context)
            state = self.states.setdefault(ident, {})
            state['recovering'] = False
            state['intentional'] = network_intent(before.get(ident)) != network_intent(after.get(ident))
            if state['intentional']: state.pop('native_lost', None)
            self.emit(profile, 'requested', 'REQUESTED', 'accepted', context=context)
        return context['operation_id']

    def emit(self, profile, kind, reason, outcome, *, context=None, associated=None, ready=None):
        state = self.states.setdefault(profile['id'], {})
        context = context or self.context(profile)
        return self.store.append(kind, **context, profile_id=profile['id'], profile_name=profile['name'],
                                 radio_id=self.radio(profile.get('mac')), reason=reason, outcome=outcome,
                                 associated=state.get('associated', False) if associated is None else associated,
                                 ready=state.get('ready', False) if ready is None else ready)

    def phase(self, profile, kind, reason, context=None):
        state = self.states.setdefault(profile['id'], {})
        context = context or self.context(profile)
        signature = (context['operation_id'], kind, reason)
        if state.get('signal') != signature:
            self.emit(profile, kind, reason, 'failed' if kind == 'failed' else 'in_progress', context=context)
            state['signal'] = signature

    def prune(self, current_ids, device_macs):
        """Deleted inactive profiles and historical radios must not grow forever."""
        keep = set(current_ids) | {ident for ident, state in self.states.items() if state.get('associated')}
        self.states = {ident: state for ident, state in self.states.items() if ident in keep}
        self.contexts = {ident: context for ident, context in self.contexts.items() if ident in keep}
        macs = set(device_macs) | {state.get('mac') for state in self.states.values()}
        self.radios = {mac: ident for mac, ident in self.radios.items() if mac in macs}

    def service(self, profile, service, associated, context=None):
        """Keep service checks separate from legacy association/forwarding events."""
        state = self.states.setdefault(profile['id'], {})
        if not associated:
            state.pop('service', None)
            return
        if not service or service.get('method') not in ('tcp_connect', 'udp_identity'):
            return
        value = service.get('state')
        if value == 'not_probed' or value == state.get('service'):
            return
        reason = {'ready': 'SERVICE_READY', 'not_answering': 'SERVICE_UNAVAILABLE', 'unknown': 'SERVICE_UNKNOWN'}[value]
        self.emit(profile, 'service_changed', reason, value, context=context)
        state['service'] = value

    def native_state(self, ident, link, service):
        state = self.states.setdefault(ident, {})
        value = (link, service)
        now = self.store.monotonic()
        if value != state.get('native_state') and now - state.get('state_emitted', -1e30) >= 5:
            self.store.native('instrument.state', ident, link=link, service=service)
            state.update(native_state=value, state_emitted=now)

    def observe(self, profile, associated, ready, phase, reason, context=None):
        associated = bool(associated)
        ready = bool(ready and associated)
        state = self.states.setdefault(profile['id'], {})
        context = context or self.context(profile)
        if (not ready and (state.get('ready') or state.get('associated')) and
                not state.get('intentional') and context['operation_id'] == state.get('last_ready_operation')):
            context = {'operation_id': uuid.uuid4().hex, 'cause': 'RECONCILE'}
            self.contexts[profile['id']] = dict(context)
        radio_id = self.radio(profile.get('mac'))
        old_radio = state.get('last_connected_radio')
        if associated and old_radio and radio_id and old_radio != radio_id:
            self.store.native('instrument.moved', profile['id'], from_radio_id=old_radio, to_radio_id=radio_id)
        if state.get('associated') and not associated and not state.get('intentional') and (profile.get('enabled') or self.leased(profile['id'])):
            self.store.native('instrument.lost', profile['id'], reason='RADIO_UNAVAILABLE' if reason == 'RADIO_UNAVAILABLE' else 'LINK_LOST')
            state['native_lost'] = True
        if associated:
            if state.pop('native_lost', False): self.store.native('instrument.restored', profile['id'])
            state['last_connected_radio'] = radio_id
        if state.get('associated') and (not associated or state.get('radio_id') != radio_id):
            old = dict(profile, mac=state.get('mac', profile.get('mac')))
            self.emit(old, 'disconnected', reason if not associated else 'CONNECTION_LOST', 'disconnected',
                      context=context, associated=False, ready=False)
            state['recovering'] = state.get('ever_ready', False) and not state.get('intentional', False)
            state['associated'] = False
            state['ready'] = False
        if associated and not state.get('associated'):
            self.emit(profile, 'associated', 'ASSOCIATED', 'associated', context=context, associated=True, ready=False)
        if ready and not state.get('ready'):
            kind = 'restored' if state.get('recovering') else 'connected'
            self.emit(profile, kind, 'READY', 'ready', context=context, associated=True, ready=True)
            state['ever_ready'] = True
            state['recovering'] = False
            state['intentional'] = False
            state['signal'] = (context['operation_id'], kind, 'READY')
        elif not ready and (profile.get('enabled') or associated):
            kind = 'failed' if phase in ('missing', 'conflict', 'invalid_ip', 'error', 'authentication_failed') else 'waiting'
            signature = (context['operation_id'], kind, reason)
            if state.get('signal') != signature:
                self.emit(profile, kind, reason, 'failed' if kind == 'failed' else 'waiting',
                          context=context, associated=associated, ready=False)
                state['signal'] = signature
            if state.get('ready'):
                state['recovering'] = True
        if ready:
            state['last_ready_operation'] = context['operation_id']
        state.update(associated=associated, ready=ready, radio_id=radio_id, mac=profile.get('mac'),
                     profile={k: profile.get(k) for k in ('id', 'name', 'mac', 'enabled')})

        return context
