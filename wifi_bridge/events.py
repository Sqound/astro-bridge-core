"""Bounded local connection events; HTTP reads never generate transitions."""
import copy
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import threading
import time
import uuid

from .config import atomic_json, profiles, network_intent

TYPES = {'requested', 'disconnecting', 'disconnected', 'connecting', 'associated',
         'waiting', 'connected', 'failed', 'restored', 'automatic_switch_deferred'}
CAUSES = {'MANUAL', 'API', 'CONFIGURATION', 'RECONCILE', 'AUTOMATIC', 'SEESTAR_DISCOVERY', 'VESPERA_CONNECTION', 'DWARF_DISCOVERY', 'ZWO_MOUNT_CONNECTION', 'SYNSCAN_DISCOVERY', 'PLUGIN_CONNECTION'}
REASONS = {'REQUESTED', 'DISCONNECTING', 'CONNECTING', 'ASSOCIATED', 'READY',
           'CONNECTION_LOST', 'RADIO_UNAVAILABLE', 'HOME_UNAVAILABLE',
           'AWAITING_ASSOCIATION', 'SERVICE_UNAVAILABLE', 'CONFIGURATION_CONFLICT',
           'INVALID_TARGET', 'ASSOCIATION_FAILED', 'RECONCILE_FAILED',
           'PROFILE_DISABLED', 'PROFILE_REMOVED', 'AUTOMATIC_DEFERRED'}
FIELDS = {'sequence', 'boot_id', 'timestamp', 'clock_status', 'operation_id', 'profile_id',
          'profile_name', 'radio_id', 'type', 'cause', 'reason', 'outcome', 'associated', 'ready'}


class EventStore:
    """A bounded, root-only journal; sequence persists, boot ID scopes a service run."""
    def __init__(self, path, capacity=256):
        self.path = Path(path)
        self.capacity = capacity
        self.lock = threading.RLock()
        self.observed_events = {}
        self.boot_id = uuid.uuid4().hex
        self.sequence = 0
        self.events = []
        self.persistent = True
        self.history_available = True
        try:
            if self.path.stat().st_size > 512 * 1024:
                raise ValueError('Oversized journal')
            data = json.loads(self.path.read_text())
            if (not isinstance(data, dict) or data.get('format') != 1 or
                    type(data.get('sequence')) is not int or data['sequence'] < 0 or
                    not isinstance(data.get('events'), list)):
                raise ValueError('Invalid journal')
            previous = 0
            for event in data['events']:
                if (not isinstance(event, dict) or set(event) != FIELDS or type(event['sequence']) is not int or
                        not previous < event['sequence'] <= data['sequence'] or
                        event['type'] not in TYPES or event['cause'] not in CAUSES or event['reason'] not in REASONS):
                    raise ValueError('Invalid event')
                previous = event['sequence']
            self.sequence = data['sequence']
            self.events = data['events'][-capacity:]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            self.history_available = False
        # Validate writability before an actual connection transition.
        self._persist()

    def _persist(self):
        try:
            atomic_json(self.path, {'format': 1, 'sequence': self.sequence, 'events': self.events})
            self.persistent = True
        except OSError:
            # Event storage must not take down the management network on disk-full.
            self.persistent = False

    def append(self, event_type, *, operation_id, profile_id, profile_name, radio_id,
               cause, reason, outcome, associated=False, ready=False):
        if event_type not in TYPES or cause not in CAUSES or reason not in REASONS:
            raise ValueError('Unknown event code')
        with self.lock:
            self.sequence += 1
            event = dict(sequence=self.sequence, boot_id=self.boot_id,
                         timestamp=datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                         clock_status='unverified', operation_id=operation_id,
                         profile_id=profile_id, profile_name=profile_name, radio_id=radio_id,
                         type=event_type, cause=cause, reason=reason, outcome=outcome,
                         associated=bool(associated), ready=bool(ready))
            self.events.append(event)
            self.observed_events[event["sequence"]] = time.monotonic()
            self.observed_events = dict(list(self.observed_events.items())[-self.capacity:])
            self.events = self.events[-self.capacity:]
            self._persist()
            return copy.deepcopy(event)

    def age(self, sequence):
        """Process-local event age: persisted replay is never a fresh push notice."""
        with self.lock:
            observed=self.observed_events.get(sequence)
            return None if observed is None else max(0,time.monotonic()-observed)

    def read(self, after=None, boot_id=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100 or (after is not None and (type(after) is not int or not 0 <= after <= 2**63 - 1)):
            raise ValueError('Invalid event cursor or limit')
        if boot_id is not None and (not isinstance(boot_id, str) or not re.fullmatch(r'[0-9a-f]{32}', boot_id)):
            raise ValueError('Invalid boot ID')
        with self.lock:
            oldest = self.events[0]['sequence'] if self.events else self.sequence + 1
            reason = 'BOOT_CHANGED' if boot_id is not None and boot_id != self.boot_id else None
            if reason is None and after is not None:
                reason = 'CURSOR_AHEAD' if after > self.sequence else 'RETENTION_GAP' if after < oldest - 1 else None
            if not self.history_available and after is None:
                reason = reason or 'HISTORY_UNAVAILABLE'
            if after is None or reason:
                selected = self.events[-limit:]
                cursor = self.sequence
                more = False
            else:
                available = [e for e in self.events if e['sequence'] > after]
                selected = available[:limit]
                cursor = selected[-1]['sequence'] if selected else after
                more = len(available) > len(selected)
            return dict(boot_id=self.boot_id, oldest_sequence=oldest, latest_sequence=self.sequence,
                        next_cursor=cursor, has_more=more, gap=reason is not None,
                        reset_reason=reason, persistent=self.persistent, history_available=self.history_available, clock_status='unverified',
                        events=copy.deepcopy(selected))


class ConnectionEvents:
    """Translate confirmed reconcile observations into one shared event path."""
    def __init__(self, store):
        self.store = store
        self.contexts = {}
        self.states = {}
        self.radios = {}

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
            kind = 'failed' if phase in ('missing', 'conflict', 'invalid_ip', 'error') else 'waiting'
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
