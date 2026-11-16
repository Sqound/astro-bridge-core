"""Normalized metric vocabulary. Adapters must discard raw fields and secrets."""
import math
import re

# Units are part of the name. Link byte values are interval deltas; other
# counters are cumulative within an adapter generation.
NUMERIC = frozenset('''link.rssi_dbm link.bitrate_mbps link.ping_ms link.loss_pct link.bytes_to
link.bytes_from link.disconnects stack.frames_acquired stack.frames_accepted
stack.frames_rejected exposure.seconds target.ra_hours target.dec_deg
battery.percent device.temperature_c env.temperature_c env.humidity_pct
env.dew_point_c storage.free_mb'''.split())
BOOLEAN = frozenset('link.protected mount.tracking mount.slewing mount.at_park battery.charging device.master_present'.split())
TEXT = frozenset('''link.activity operation.kind operation.state operation.step target.name
heater.state filter.name error.code camera.state'''.split())


def bounded_text(value):
    # No content-based secret detector can replace the adapter's field allowlist.
    return (isinstance(value, str) and 1 <= len(value) <= 64 and
            all(c.isprintable() for c in value))


def validate(metric, value):
    """Return the sole numeric/text column pair; reject unreviewed vocabulary."""
    if not isinstance(metric, str) or len(metric) > 64: raise ValueError('Invalid telemetry metric')
    numeric = metric in NUMERIC or bool(re.fullmatch(r'stack\.reject_reason\.[a-z0-9_]{1,32}', metric))
    if metric in BOOLEAN:
        if type(value) is not bool: raise ValueError('Invalid telemetry boolean')
        return int(value), None
    if numeric:
        if type(value) not in (int, float) or abs(value) > 2**53 or not math.isfinite(value):
            raise ValueError('Invalid telemetry number')
        return value, None
    if metric in TEXT and bounded_text(value): return None, value
    raise ValueError('Unknown metric or invalid telemetry value')
