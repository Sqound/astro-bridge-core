"""Wi-Fi mounts that switch their own Wi-Fi off when no client stays joined.

Observed on one SynScan mount (an AZ-GTiX), not documented by the maker: powered
on, with no client joined and no movement, it stopped its Wi-Fi 11 to 12 minutes
after power-on and its status light turned steady. With a client joined the whole
time, or while it moved, it did not. Switching it off and on again brought it back.
Other SynScan models are not established: the bridge says "probable", never "proven".

The bridge keeps such a mount joined while it is linked (its radio association
plus the read-only identification query of its service check, every 30 s). It
never sends anything else to the mount from here.
"""
import copy

WIFI_SLEEP = {
    'synscan': {
        'without_client_after_seconds': 660,
        'observed_on': ['AZ-GTiX'],
        'documented_by_maker': False,
        # What the owner saw each time: the mount's status light turns steady.
        'sign': 'STATUS_LIGHT_STEADY',
        'recovery': 'POWER_CYCLE',
    },
}

# Not heard by the last scans (one every 45 s) nor answering over its link.
SILENT_AFTER_SECONDS = 60


def wifi_sleep(kind):
    """The published behaviour of this mount family, or None."""
    value = WIFI_SLEEP.get(kind)
    return copy.deepcopy(value) if value else None


def silence(kind, link_age, scan_age):
    """How long a sleep-prone mount has been silent, and the probable cause.

    link_age: seconds since the bridge last had it joined and answering, or None.
    scan_age: seconds since a bridge scan last heard its network, or None.
    The cause is probable only, and only when the bridge was not joined to it when
    it was last heard: with a client joined, the mount was not seen to stop its Wi-Fi.
    """
    spec = WIFI_SLEEP.get(kind)
    link_age = link_age if isinstance(link_age, (int, float)) and link_age >= 0 else None
    scan_age = scan_age if isinstance(scan_age, (int, float)) and scan_age >= 0 else None
    if not spec or (link_age is None and scan_age is None):
        return None
    since = int(min(age for age in (link_age, scan_age) if age is not None))
    if since < SILENT_AFTER_SECONDS:
        return None
    # Joined within a minute of the last time it was heard: it went silent with a client.
    joined = link_age is not None and (scan_age is None or link_age <= scan_age + SILENT_AFTER_SECONDS)
    return {'since_seconds': since,
            'last_heard': 'link' if link_age is not None and (scan_age is None or link_age <= scan_age) else 'scan',
            'probable_cause': 'UNKNOWN' if joined else 'MOUNT_WIFI_SLEEP',
            'cause_established': False,
            'sign': None if joined else spec['sign'],
            'recovery': None if joined else spec['recovery']}
