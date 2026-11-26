"""Best-effort UTC estimates, never an authority for ordering.

The system clock is changed only on an appliance whose clock is manifestly
wrong (earlier than this release) and not synchronized, from an authenticated
browser's time. Network time (chrony) corrects it as soon as an uplink exists.
"""
from datetime import datetime, timezone
import ipaddress
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import threading
import time

from .config import atomic_json

MAX_OFFSET = 315576000  # Ten Julian years.
# No clock earlier than this release date can be right on a running bridge.
RELEASE_FLOOR = 1793491200  # 2026-11-01T00:00:00Z


def set_system_clock(value):
    """CLOCK_REALTIME only; monotonic ordering and timers are unaffected."""
    time.clock_settime(time.CLOCK_REALTIME, value)


def synchronized():
    """Read existing chrony evidence; missing/failed utilities prove nothing."""
    return chrony_reference() is not None


def chrony_reference():
    """UTC epoch of chrony's latest measurement when the evidence is current, else None."""
    try:
        result = subprocess.run(['/usr/bin/chronyc', '-c', '-n', 'tracking'], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=1, check=False)
        if result.returncode or len(result.stdout) > 4096:
            return None
        fields = result.stdout.decode('ascii').strip().split(',')
        numbers = [float(value) for value in fields[3:13]]
        source = ipaddress.ip_address(fields[1])
        valid = (len(fields) == 14 and bool(re.fullmatch('[0-9A-Fa-f]{8}', fields[0]))
                and fields[0].upper() not in ('00000000', '7F7F0101')
                and not source.is_loopback and not source.is_unspecified
                and 1 <= int(fields[2]) <= 15 and fields[-1] == 'Normal'
                and all(math.isfinite(value) for value in numbers)
                and 0 <= time.time() - numbers[0] <= 3600
                and abs(numbers[1]) <= 1 and 0 <= numbers[8] <= 1 and 0 <= numbers[7] <= 2)
        return numbers[0] if valid else None
    except (OSError, ValueError, IndexError, UnicodeError, subprocess.TimeoutExpired):
        return None


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds')


SYNC_RECORD_INTERVAL = 3600  # At most one write an hour while network time keeps arriving.


class Clock:
    """Eight in-memory estimates on one monotonic timeline; a floor and the last network sync persist."""
    def __init__(self, path, *, now=time.time, monotonic=time.monotonic, sync=chrony_reference, set_system=None):
        self.path = Path(path)
        self.now, self.monotonic, self.sync = now, monotonic, sync
        # Supplied only by a platform that owns the host clock.
        self.set_system = set_system
        self.system_set = None
        self.lock = threading.RLock()
        self.anchor, self.started = now(), monotonic()
        self.hints = []
        self.clients = {}
        self.lower_bound = None
        self.sync_checked = None
        self.is_synchronized = False
        # Monotonic instant of chrony's latest measurement seen during this run (never a wall-clock difference).
        self.sync_mono = None
        # The last network synchronization recorded before or during this run: chrony's own UTC reference time.
        self.sync_path = self.path.with_name('clock-sync.json')
        self.sync_recorded = None
        try:
            if self.sync_path.stat().st_size > 256:
                raise ValueError('Oversized sync record')
            data = json.loads(self.sync_path.read_text())
            value = data['last_network_sync']
            if (set(data) != {'last_network_sync'} or type(value) not in (int, float)
                    or not math.isfinite(value) or not RELEASE_FLOOR <= value <= 253402300799):
                raise ValueError('Invalid sync record')
            self.sync_recorded = value
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            if self.path.stat().st_size > 256:
                raise ValueError('Oversized clock floor')
            data = json.loads(self.path.read_text())
            value = data['lower_bound']
            if (set(data) != {'lower_bound'} or type(value) not in (int, float)
                    or not math.isfinite(value) or not 0 <= value <= 253402300799):
                raise ValueError('Invalid clock floor')
            self.lower_bound = value
        except (OSError, ValueError, KeyError, TypeError):
            pass
        # The image's checkpoint is independent and must never be rewritten here.
        try:
            checkpoint = self.path.with_name('clock-lower-bound')
            if checkpoint.stat().st_size <= 32:
                value = int(checkpoint.read_text())
                if 1577836800 <= value <= 4102444800:
                    self.lower_bound = max(self.lower_bound or 0, value)
        except (OSError, ValueError):
            pass

    def _synchronized(self, observed):
        if self.sync_checked is None or observed - self.sync_checked >= 5:
            result = self.sync()
            self.is_synchronized = bool(result)
            self.sync_checked = observed
            if self.is_synchronized:
                self._note_sync(observed, result)
        return self.is_synchronized

    def _note_sync(self, observed, reference):
        """Remember when chrony last measured network time; age uses the monotonic clock only."""
        system = self.now()
        if reference is True or not isinstance(reference, (int, float)) or not 0 <= system - reference <= 3600:
            reference = system
        self.sync_mono = observed - (system - reference)
        # Network time is the truth here, so it replaces an earlier record even if that one is later.
        if self.sync_recorded is None or reference - self.sync_recorded >= SYNC_RECORD_INTERVAL or reference < self.sync_recorded:
            try:
                atomic_json(self.sync_path, {'last_network_sync': reference})
            except OSError:
                pass  # Disk failure cannot interrupt network management.
            self.sync_recorded = reference

    def _last_sync(self, observed, synced):
        """What the bridge can honestly say about its last setting from network time."""
        known = utc(self.sync_recorded) if self.sync_recorded is not None else None
        if self.sync_mono is not None:
            kind, age = 'network', max(0, int(observed - self.sync_mono))
        elif self.hints and not synced:
            kind, age = 'client_hint', max(0, int(observed - self.hints[-1][2]))
        elif self.sync_recorded is not None:
            kind, age = 'network', None
        else:
            kind, age = 'never', None
        return {'kind': kind, 'age_seconds': age, 'this_run': age is not None, 'last_network_sync_utc': known}

    def _read(self, observed):
        system = self.now()
        synced = self._synchronized(observed)
        corrected, source = system, 'lower_bound'
        if synced:
            source = 'ntp'
        elif self.hints:
            corrected = self.anchor + observed - self.started + statistics.median(h[0] for h in self.hints)
            source = 'client_hint'
        if not synced and self.lower_bound is not None:
            corrected = max(corrected, self.lower_bound)
        offset = corrected - system
        return {'clock_status': 'synchronized' if synced else 'unverified',
                'system_utc': utc(system), 'corrected_utc': utc(corrected),
                'offset_ms': round(offset * 1000), 'source': source,
                'hint_age_seconds': max(0, int(observed - self.hints[-1][2])) if self.hints and not synced else None,
                'last_sync': self._last_sync(observed, synced)}

    def read(self):
        with self.lock:
            return self._read(self.monotonic())

    def browser_view(self):
        """What the web interface shows: the bridge's own time when it is manifestly wrong."""
        with self.lock:
            value = self._read(self.monotonic())
            system = self.now()
            return {'clock_status': value['clock_status'], 'system_utc': value['system_utc'],
                    'corrected_utc': value['corrected_utc'], 'source': value['source'], 'last_sync': value['last_sync'],
                    'system_plausible': system >= RELEASE_FLOOR, 'set_from_browser': self.system_set is not None}

    def hint(self, data, client):
        # Import lazily because MobileAPI owns the shared public error shape.
        from .api import ApiError
        if (not isinstance(data, dict) or set(data) != {'utc_iso', 'uncertainty_ms'}
                or not isinstance(data.get('utc_iso'), str) or len(data['utc_iso']) > 40
                or type(data.get('uncertainty_ms')) is not int or not 0 <= data['uncertainty_ms'] <= 86400000):
            raise ApiError(400, 'INVALID_TIME_HINT', 'Provide UTC time and uncertainty from 0 to 86400000 milliseconds.')
        try:
            stamp = datetime.fromisoformat(data['utc_iso'].replace('Z', '+00:00'))
            if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
                raise ValueError('UTC required')
            hinted = stamp.timestamp()
        except (ValueError, OverflowError):
            raise ApiError(400, 'INVALID_TIME_HINT', 'Provide UTC time and uncertainty from 0 to 86400000 milliseconds.') from None
        with self.lock:
            observed = self.monotonic()
            if self._synchronized(observed):
                return self._read(observed)
            # A clock before this release is no reference for the ten-year bound.
            if abs(hinted - max(self.now(), RELEASE_FLOOR)) > MAX_OFFSET:
                raise ApiError(400, 'TIME_HINT_OUT_OF_RANGE', 'Time hint must be within ten years of the system clock.')
            if observed - self.clients.get(client, -math.inf) < 60:
                raise ApiError(429, 'TIME_HINT_RATE_LIMITED', 'Wait 60 seconds before sending another time hint.')
            self.clients = {key: value for key, value in self.clients.items() if observed - value < 60}
            if len(self.clients) >= 256:
                raise ApiError(429, 'TIME_HINT_RATE_LIMITED', 'Wait 60 seconds before sending another time hint.')
            self.clients[client] = observed
            offset = hinted - (self.anchor + observed - self.started)
            self.hints = (self.hints + [(offset, data['uncertainty_ms'], observed)])[-8:]
            if (self.set_system is not None and self.now() < RELEASE_FLOOR <= hinted
                    and data['uncertainty_ms'] <= 600000):
                try:
                    self.set_system(hinted)
                    self.system_set = {'utc': utc(hinted), 'client': 'browser'}
                    print('Astro Bridge clock: the system clock was earlier than this release; '
                          'it was set from a signed-in browser until network time is available.', flush=True)
                except (OSError, ValueError, OverflowError):
                    pass  # No privilege: estimates still use the hint.
            result = self._read(observed)
            floor = datetime.fromisoformat(result['corrected_utc']).timestamp() - max(h[1] for h in self.hints) / 1000
            try:
                atomic_json(self.path, {'lower_bound': max(self.lower_bound or 0, floor)})
            except OSError:
                pass  # Disk failure cannot interrupt network management.
            return result
