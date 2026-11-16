"""Bounded local resource and relay measurements; never read packet payloads."""
from collections import deque
import copy
import math
import os
from pathlib import Path
import threading
import time


LIMIT = 2**53 - 1


def percentiles(values):
    values = sorted(values)
    return {name: round(values[max(0, math.ceil(len(values)*fraction)-1)], 3) if values else None
            for name, fraction in (('p50_ms', .5), ('p95_ms', .95))}


def proc_snapshot(root='/proc', ticks=None, page_size=None):
    """Read only bounded numeric proc files. Missing evidence remains null."""
    root = Path(root)
    def read(name):
        with (root/name).open() as stream:
            value = stream.read(65537)
        if len(value) > 65536: raise ValueError('Oversized proc file')
        return value
    result = dict(rss_bytes=None, threads=None, cpu_seconds=None, load_average=None,
                  memory_free_bytes=None, memory_available_bytes=None)
    try:
        # comm may include spaces and ')'; fields after its final ')' start at 3.
        fields = read('self/stat').rsplit(')', 1)[1].split()
        utime, stime, threads, rss = [int(fields[i]) for i in (11, 12, 17, 21)]
        if min(utime, stime, threads, rss) < 0 or max(utime, stime, threads, rss) > 2**63-1: raise ValueError
        frequency = ticks or os.sysconf('SC_CLK_TCK')
        pages = page_size or os.sysconf('SC_PAGE_SIZE')
        result.update(rss_bytes=rss*pages, threads=threads, cpu_seconds=(utime+stime)/frequency)
    except (OSError, ValueError, IndexError, KeyError): pass
    try:
        loads = [float(value) for value in read('loadavg').split()[:3]]
        if len(loads) != 3 or any(not math.isfinite(v) or v < 0 for v in loads): raise ValueError
        result['load_average'] = loads
    except (OSError, ValueError): pass
    try:
        fields = {}
        for line in read('meminfo').splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0] in ('MemFree:', 'MemAvailable:') and parts[2] == 'kB':
                value = int(parts[1])
                if 0 <= value <= 2**63-1: fields[parts[0]] = value*1024
        result.update(memory_free_bytes=fields.get('MemFree:'), memory_available_bytes=fields.get('MemAvailable:'))
    except (OSError, ValueError): pass
    return result


class ForwardingMetrics:
    """One live ingress endpoint, 256 recent local forwarding samples."""
    def __init__(self):
        self.lock = threading.Lock()
        self.sent = self.received = self.datagrams = self.drops = 0
        self.latency = deque(maxlen=256)

    def forward(self, size, upstream, started, datagram=False):
        with self.lock:
            if upstream: self.sent = min(LIMIT, self.sent+size)
            else: self.received = min(LIMIT, self.received+size)
            self.datagrams = min(LIMIT, self.datagrams+int(datagram))
            self.latency.append(max(0, (time.monotonic()-started)*1000))

    def drop(self):
        with self.lock: self.drops = min(LIMIT, self.drops+1)

    def snapshot(self):
        with self.lock:
            return dict(bytes_to_instrument=self.sent, bytes_from_instrument=self.received,
                        datagrams=self.datagrams, drops=self.drops,
                        forwarding_latency=dict(samples=len(self.latency), **percentiles(self.latency)))


class IngressTimings:
    """At most 64 timestamp buckets even for single-byte TCP reads."""
    def __init__(self):
        self.pending = deque()

    def receive(self, size, started):
        if len(self.pending) >= 64: self.pending[-1][0] += size
        else: self.pending.append([size, started])

    def forward(self, size, metrics, upstream):
        while size:
            part = self.pending[0]
            used = min(size, part[0])
            metrics.forward(used, upstream, part[1])
            part[0] -= used; size -= used
            if not part[0]: self.pending.popleft()


class SystemMetrics:
    def __init__(self, relays=lambda: [], clock=time.monotonic, reader=proc_snapshot):
        self.relays, self.clock, self.reader = relays, clock, reader
        self.lock = threading.RLock()
        self.samples = deque(maxlen=14)
        self.lags = deque(maxlen=120)
        self.cached = None
        self.sampled = None
        self.sequence = 0

    def tick(self, expected, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            self.lags.append(max(0, now-expected)*1000)
            self._refresh(now)

    def _refresh(self, now):
        if self.sampled is not None and now-self.sampled < 5: return
        raw = self.reader()
        self.samples.append((now, raw['cpu_seconds']))
        cpu = {}
        for window in (5, 60):
            baseline = next(((stamp, value) for stamp, value in reversed(self.samples)
                             if now-stamp >= window), None)
            cpu[str(window)+'s'] = (round(100*(raw['cpu_seconds']-baseline[1])/(now-baseline[0]), 3)
                if baseline and baseline[1] is not None and raw['cpu_seconds'] is not None
                and raw['cpu_seconds'] >= baseline[1] else None)
        self.sequence += 1
        self.cached = dict(api_version='1', sample_sequence=self.sequence, sample_interval_seconds=5,
            process=dict(rss_bytes=raw['rss_bytes'], threads=raw['threads'], cpu_percent=cpu),
            system={k: raw[k] for k in ('load_average', 'memory_free_bytes', 'memory_available_bytes')},
            http_loop_lag=dict(samples=len(self.lags), **percentiles(self.lags)),
            relays=self.relays(), relay_coverage='userspace_only')
        self.sampled = now

    def read(self):
        with self.lock:
            now = self.clock()
            self._refresh(now)
            return dict(copy.deepcopy(self.cached), sample_age_seconds=round(max(0, now-self.sampled), 3))
