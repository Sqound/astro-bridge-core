"""Explicit activity-driven sessions; association alone never starts observation."""
import threading

from .metrics import validate
from .store import session_context

IDLE_SECONDS = 30 * 60


class Sessions:
    def __init__(self, store, *, events=None):
        self.store = store
        self.events = events
        self.lock = threading.RLock()
        self.active = {}
        from .cards import Readings
        self.readings = Readings(store)
        self.closed = False

    def activity(self, profile_id, kind, adapter_set, metric, value, *, location=None):
        """Only an owner-enabled adapter may call this with allowlisted fields.

        location is a process-local ownership token, never persisted. Polling an
        unchanged status must not be described by an adapter as activity.
        """
        validate(metric, value)
        with self.lock:
            ident = self.touch(profile_id, kind, adapter_set, location=location)
            return ident, self._sample(profile_id, ident, metric, value)

    def observe(self, profile_id, metric, value):
        """Sample an existing session without opening/extending its idle deadline."""
        validate(metric, value)
        with self.lock:
            if self.closed: return
            self.tick()
            current = self.active.get(profile_id)
            if current: return self._sample(profile_id, current['id'], metric, value)

    def touch(self, profile_id, kind, adapter_set, *, location=None):
        """Confirmed traffic opens/extends a session without a synthetic metric."""
        kind, adapters = session_context(profile_id, kind, adapter_set)
        if location is not None and (not isinstance(location, str) or len(location) > 64):
            raise ValueError('Invalid telemetry location')
        with self.lock:
            if self.closed: raise ValueError('Telemetry sessions closed')
            self.tick()
            current = self.active.get(profile_id)
            context = (kind, adapters, location)
            if current and current['context'] != context:
                self.end(profile_id, 'profile_move')
                current = None
            if current is None:
                if len(self.active) >= 24: raise ValueError('Too many telemetry sessions')
                current = {'id': self.store.start_session(profile_id, kind, adapter_set),
                           'context': context, 'last_activity': self.store.monotonic(),
                           'pending_seq': None, 'available_due': None, 'available_at': None}
                self.active[profile_id] = current
                self._event('telemetry.session_started', profile_id, current['id'])
            current['last_activity'] = self.store.monotonic()
            return current['id']

    def end(self, profile_id, reason='disconnect'):
        with self.lock:
            if self.closed: return
            current = self.active.get(profile_id)
            if current:
                self._available(profile_id, current, force=True)
                self.store.end_session(current['id'], reason)
                self._event('telemetry.session_ended', profile_id, current['id'])
                del self.active[profile_id]

    def reconcile(self, profiles, associated):
        """Close existing sessions on confirmed loss/move; never open one."""
        with self.lock:
            if self.closed: return
            self.readings.rows = {k:v for k,v in self.readings.rows.items() if k in profiles}
            for ident, current in list(self.active.items()):
                profile = profiles.get(ident)
                if profile is None or not profile.get('enabled') or ident not in associated:
                    self.end(ident, 'disconnect')
                elif current['context'][2] is not None and current['context'][2] != profile.get('mac'):
                    self.end(ident, 'profile_move')
            self.tick()

    def tick(self):
        with self.lock:
            if self.closed: return
            now = self.store.monotonic()
            for ident, current in list(self.active.items()):
                if now - current['last_activity'] >= IDLE_SECONDS: self.end(ident, 'idle')
                else: self._available(ident, current)
            self.store.maintain(idle=not self.active)

    def _event(self, kind, profile_id, session_id, **fields):
        # EventStore takes only its own lock and never calls the controller or
        # sessions. Keep this direction; callbacks must not acquire controller.lock.
        if self.events is not None and not self.events.closed:
            self.events.native(kind, profile_id, session_id=session_id, **fields)

    def _sample(self, profile_id, ident, metric, value):
        seq = self.store.sample(ident, metric, value)
        current = self.active[profile_id]
        current['pending_seq'] = seq
        if current['available_due'] is None:
            now = self.store.monotonic()
            current['available_due'] = max(now+1, (current['available_at'] if current['available_at'] is not None else -30)+30)
        return seq

    def _available(self, profile_id, current, *, force=False):
        now = self.store.monotonic()
        if current['pending_seq'] is not None and (force or now >= current['available_due']):
            self._event('telemetry.available', profile_id, current['id'], latest_sample_seq=current['pending_seq'])
            current.update(pending_seq=None, available_due=None, available_at=now)

    def close(self):
        with self.lock:
            if self.closed: return
            for ident in list(self.active): self.end(ident, 'shutdown')
            self.store.close()
            self.closed = True
