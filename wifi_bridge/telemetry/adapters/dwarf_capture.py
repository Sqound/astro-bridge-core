"""Read-only DWARF capture state from notifications the instrument already sends.

The bridge never opens its own DWARF session (the instrument serves one client)
and never sends a command. A caller that already holds a server-to-client
WebSocket binary payload (a relay tap, a bench capture) passes it to receive();
only the operation state leaves this module, never the payload.

Packet layout follows the community DWARF protobuf schema (WsPacket: module_id
4, cmd 5, type 6, data 7; notification data field 1 = state). Command numbers
come from DWARF mini bench logs (02-03/11/2026), partially verified on hardware;
a JSON-by-JSON record is still to come. An unknown (module, command) pair is
ignored, so a wrong number only leaves the state unobserved (None), never quiet.
Not read here: 15209 stacking progress, 15210 calibration, 15261 after 16405,
module 13 schedule results (15248) and task state (15249).
"""
import threading
import time

from ...dwarf_demand import _varint

PACKET = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 2, 8: 2}
# (module, notification command) -> operation kind. 15208: capture state (module 9);
# 16405: one-shot device state at connection (module 14); 15211: GoTo state (module 9).
NOTIFICATIONS = {(9, 15208): 'capture', (14, 16405): 'capture', (9, 15211): 'pointing'}
# State: idle, running, stopping (still busy), stopped.
STATES = {0: 'idle', 1: 'running', 2: 'running', 3: 'stopped'}
LIMIT = 4096


def _fields(raw, schema):
    result = {}; offset = 0
    while offset < len(raw):
        tag, offset = _varint(raw, offset); number, wire = tag >> 3, tag & 7
        if wire not in (0, 2) or schema.get(number, wire) != wire: raise ValueError('Malformed packet.')
        if wire == 0: value, offset = _varint(raw, offset)
        else:
            size, offset = _varint(raw, offset)
            if offset + size > len(raw): raise ValueError('Malformed packet.')
            value = raw[offset:offset + size]; offset += size
        if number in schema: result[number] = value
    return result


def decode(payload):
    """Return (kind, state) for a known capture notification, else None."""
    if not isinstance(payload, (bytes, bytearray)) or not 0 < len(payload) <= LIMIT: return None
    try:
        packet = _fields(bytes(payload), PACKET)
        kind = NOTIFICATIONS.get((packet.get(4), packet.get(5)))
        if kind is None: return None
        state = STATES.get(_fields(packet.get(7, b''), {1: 0}).get(1, 0))
    except ValueError:
        return None
    return (kind, state) if state else None


class DwarfCapture:
    """Latest observed state per DWARF profile, valid while that profile stays connected."""
    def __init__(self, controller, clock=time.monotonic):
        self.c = controller; self.clock = clock
        self.lock = threading.Lock(); self.observed = {}

    def receive(self, profile_id, payload):
        value = decode(payload)
        if value is None: return False
        with self.lock: self.observed[profile_id] = value
        return True

    def forget(self, profile_id):
        with self.lock: self.observed.pop(profile_id, None)

    def _current(self, profile):
        if profile.get('kind') != 'dwarf': return None
        connected = any(r['id'] == profile['id'] and r.get('connected') for r in self.c.state.get('telescopes', ()))
        with self.lock:
            if not connected: self.observed.pop(profile['id'], None); return None
            return self.observed.get(profile['id'])

    def operation_state(self, profile):
        current = self._current(profile)
        return None if current is None else 'idle' if current[1] == 'stopped' else current[1]

    def operation_kind(self, profile):
        current = self._current(profile)
        return None if current is None or current[1] != 'running' else current[0]
