"""Best-effort UTC estimates, never an authority for ordering or OS clock changes."""
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


def synchronized():
    """Read existing chrony evidence; missing/failed utilities prove nothing."""
    try:
        result = subprocess.run(['/usr/bin/chronyc', '-c', '-n', 'tracking'], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=1, check=False)
        if result.returncode or len(result.stdout) > 4096:
            return False
        fields = result.stdout.decode('ascii').strip().split(',')
        numbers = [float(value) for value in fields[3:13]]
        source = ipaddress.ip_address(fields[1])
        return (len(fields) == 14 and bool(re.fullmatch('[0-9A-Fa-f]{8}', fields[0]))
                and fields[0].upper() not in ('00000000', '7F7F0101')
                and not source.is_loopback and not source.is_unspecified
                and 1 <= int(fields[2]) <= 15 and fields[-1] == 'Normal'
                and all(math.isfinite(value) for value in numbers)
                and 0 <= time.time() - numbers[0] <= 3600
                and abs(numbers[1]) <= 1 and 0 <= numbers[8] <= 1 and 0 <= numbers[7] <= 2)
    except (OSError, ValueError, IndexError, UnicodeError, subprocess.TimeoutExpired):
        return False


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds')


class Clock:
    """Eight in-memory estimates on one monotonic timeline; only a floor persists."""
    def __init__(self, path, *, now=time.time, monotonic=time.monotonic, sync=synchronized):
        self.path = Path(path)
        self.now, self.monotonic, self.sync = now, monotonic, sync
        self.lock = threading.RLock()
        self.anchor, self.started = now(), monotonic()
        self.hints = []
        self.clients = {}
        self.lower_bound = None
        self.sync_checked = None
        self.is_synchronized = False
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
            self.is_synchronized = bool(self.sync())
            self.sync_checked = observed
        return self.is_synchronized

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
                'hint_age_seconds': max(0, int(observed - self.hints[-1][2])) if self.hints and not synced else None}

    def read(self):
        with self.lock:
            return self._read(self.monotonic())

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
            if abs(hinted - self.now()) > MAX_OFFSET:
                raise ApiError(400, 'TIME_HINT_OUT_OF_RANGE', 'Time hint must be within ten years of the system clock.')
            if observed - self.clients.get(client, -math.inf) < 60:
                raise ApiError(429, 'TIME_HINT_RATE_LIMITED', 'Wait 60 seconds before sending another time hint.')
            self.clients = {key: value for key, value in self.clients.items() if observed - value < 60}
            if len(self.clients) >= 256:
                raise ApiError(429, 'TIME_HINT_RATE_LIMITED', 'Wait 60 seconds before sending another time hint.')
            self.clients[client] = observed
            offset = hinted - (self.anchor + observed - self.started)
            self.hints = (self.hints + [(offset, data['uncertainty_ms'], observed)])[-8:]
            result = self._read(observed)
            floor = datetime.fromisoformat(result['corrected_utc']).timestamp() - max(h[1] for h in self.hints) / 1000
            try:
                atomic_json(self.path, {'lower_bound': max(self.lower_bound or 0, floor)})
            except OSError:
                pass  # Disk failure cannot interrupt network management.
            return result
