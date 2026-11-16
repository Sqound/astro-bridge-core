"""Normalized cached Wi-Fi metadata and bounded ICMP echo (no protocol bytes)."""
from collections import deque
import ipaddress
import math
import re
import socket
import select
import statistics
import struct
import time


def remember_link(cache, name, raw, clock=time.monotonic):
    """Reuse an existing iw association read; never retain identifiers/raw text."""
    raw = raw[:65536]
    value = {'observed': clock()}
    for field, pattern, low, high in (
            ('rssi_dbm', r'^\s*signal:\s*(-?[0-9.]+)\s*dBm', -150, 0),
            ('bitrate_mbps', r'^\s*tx bitrate:\s*([0-9.]+)\s*MBit/s', 0, 100000)):
        match = re.search(pattern, raw, re.M)
        if match:
            try:
                number = float(match[1])
                if math.isfinite(number) and low <= number <= high: value[field] = number
            except ValueError: pass
    if name not in cache and len(cache) >= 32: cache.pop(next(iter(cache)))
    cache[name] = value


class EchoWindow:
    """One nonblocking socket/profile; shared collector polls all, never forks."""
    def __init__(self, *, clock=time.monotonic, factory=socket.socket):
        self.clock, self.factory = clock, factory
        self.rows = {}

    def tick(self, targets):
        now = self.clock()
        for ident in set(self.rows) - set(targets):
            row = self.rows.pop(ident)
            if row['socket']: row['socket'].close()
        for ident, (target, interface) in list(targets.items())[:24]:
            address = ipaddress.IPv4Address(target)
            if not address.is_private or address.is_multicast or address.is_loopback: continue
            context = (str(address), interface)
            row = self.rows.get(ident)
            if row is None or row['context'] != context:
                if row and row['socket']: row['socket'].close()
                row = self.rows[ident] = {'context': context, 'socket': None, 'next': now,
                                          'sent': None, 'window': deque(maxlen=60)}
            self._receive(row, now)
            if now >= row['next'] and row['socket'] is None:
                row['next'] = now+10
                sock = None
                try:
                    sock = self.factory(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
                    sock.setblocking(False)
                    sock.setsockopt(socket.SOL_SOCKET, getattr(socket, 'SO_BINDTODEVICE', 25), interface.encode()+b'\0')
                    # Kernel assigns the datagram ICMP identifier and checksum.
                    packet = struct.pack('!BBHHHQ', 8, 0, 0, 0, int(now) % 65536, int(now*1000))
                    sock.sendto(packet, (context[0], 0))
                    row.update(socket=sock, sent=now, packet=packet, reads=0)
                except OSError:
                    if sock: sock.close()

    def _receive(self, row, now):
        sock = row['socket']
        if sock:
            if now-row['sent'] >= 1:
                row['window'].append((now, None))
                sock.close(); row['socket'] = None
                return
            try:
                data, peer = sock.recvfrom(128)
                row['reads'] += 1
                if (len(data) == 16 and data[:2] == b'\0\0' and peer[0] == row['context'][0]
                        and data[6:] == row['packet'][6:]):
                    row['window'].append((now, (now-row['sent'])*1000))
                    sock.close(); row['socket'] = None
                elif row['reads'] >= 8:
                    sock.close(); row['socket'] = None
            except BlockingIOError: pass
            except OSError:
                sock.close(); row['socket'] = None

    def wait(self, stop, seconds):
        """Timestamp echo arrival promptly without resampling every profile."""
        deadline = self.clock()+seconds
        while not stop.is_set():
            remaining = deadline-self.clock()
            if remaining <= 0: return
            pending = {row['socket']:row for row in self.rows.values() if row['socket']}
            if not pending:
                stop.wait(remaining)
                return
            ready, _, _ = select.select(list(pending), [], [], remaining)
            now = self.clock()
            for sock in ready: self._receive(pending[sock], now)

    def snapshot(self, ident):
        row = self.rows.get(ident)
        window = [value for stamp, value in row['window'] if self.clock()-stamp <= 600] if row else []
        replies = [v for v in window if v is not None]
        return {'median_ms': round(statistics.median(replies), 3) if replies else None,
                'loss_percent': round(100*(len(window)-len(replies))/len(window), 3) if window else None}

    def close(self):
        for row in self.rows.values():
            if row['socket']: row['socket'].close()
        self.rows.clear()
