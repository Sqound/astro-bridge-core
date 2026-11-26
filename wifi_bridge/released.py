"""Why a saved instrument stopped using its radio on request.

A profile that is disabled on purpose is not a lost link. The bridge keeps one
short note per profile so that apps and the web interface can say which one:

- SWITCHED_OFF: an app said the instrument was switched off on request.
- RADIO_YIELDED: the instrument was out of reach and only being retried; its
  radio went to another instrument that an app asked for.
- REPLACED_ON_REQUEST: the user confirmed replacing this connected instrument.

A note never changes any connection by itself. It is cleared as soon as the
profile is enabled again or removed. Only profile identifiers are stored.
"""
import json
import re
import time

from .config import atomic_json

REASONS = ('SWITCHED_OFF', 'RADIO_YIELDED', 'REPLACED_ON_REQUEST')
IDENT = re.compile('[a-z0-9][a-z0-9-]{0,31}')


class ReleaseNotes:
    def __init__(self, directory, clock=time.monotonic):
        self.path, self.clock = directory / 'radio-releases.json', clock
        self.notes = {}
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict) and data.get('format') == 1 and isinstance(data.get('notes'), dict):
                for ident, value in list(data['notes'].items())[:64]:
                    if (isinstance(ident, str) and IDENT.fullmatch(ident) and isinstance(value, dict)
                            and value.get('reason') in REASONS):
                        to = value.get('to')
                        # The moment is unknown after a restart: only the reason survives.
                        self.notes[ident] = {'reason': value['reason'], 'at': None,
                                             'to': to if isinstance(to, str) and IDENT.fullmatch(to) else None}
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            atomic_json(self.path, {'format': 1, 'notes': {ident: {'reason': note['reason'], 'to': note['to']}
                                                           for ident, note in self.notes.items()}})
        except OSError:
            pass  # The note stays in memory; storage must never block a connection.

    def note(self, ident, reason, to=None):
        if reason not in REASONS:
            raise ValueError('Unknown release reason')
        self.notes[ident] = {'reason': reason, 'to': to, 'at': self.clock()}
        while len(self.notes) > 64:
            self.notes.pop(next(iter(self.notes)))
        self._save()

    def clear(self, ident):
        if self.notes.pop(ident, None) is not None:
            self._save()

    def keep(self, idents):
        """Forget profiles that are enabled again or no longer exist."""
        stale = [ident for ident in self.notes if ident not in idents]
        for ident in stale:
            self.notes.pop(ident)
        if stale:
            self._save()

    def public(self, ident):
        note = self.notes.get(ident)
        if note is None:
            return None
        return {'reason': note['reason'], 'to': note['to'],
                'age_seconds': None if note['at'] is None else max(0, int(self.clock() - note['at']))}

    def status(self):
        return {ident: self.public(ident) for ident in self.notes}
