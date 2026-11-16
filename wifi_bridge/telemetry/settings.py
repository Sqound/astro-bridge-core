"""Effective owner selections, shared with future read-only adapter producers."""
from ..plugins import plugin


def selected_adapters(settings, profile):
    descriptor = plugin(profile['kind'])['telemetry']
    allowed = [a['id'] if isinstance(a, dict) else a for a in descriptor['adapters']]
    explicit = settings['profiles'].get(profile['id'])
    if explicit is not None:
        return [a for a in explicit['adapters'] if a in allowed]
    # Owner-approved defaults: link observations for each profile, plus the
    # plugin's declarative defaults. An explicit empty list remains an opt-out.
    return [a for a in allowed if a == 'link_v1' or a in descriptor['default_enabled']]


def selected_settings(settings, profile):
    """Expose only effective adapters and explicitly configured public options."""
    result = {'adapters': selected_adapters(settings, profile)}
    choice = settings['profiles'].get(profile['id'], {})
    if 'alpaca_interval_seconds' in choice:
        result['alpaca_interval_seconds'] = choice['alpaca_interval_seconds']
    return result


def prune_settings(store, configured):
    """Remove only deleted profile preferences; never historical sessions."""
    import json
    valid = {p['id'] for p in configured}
    with store.lock:
        retained = {k:v for k,v in store.settings['profiles'].items() if k in valid}
        if retained == store.settings['profiles']: return
        value = dict(store.settings, profiles=retained)
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO metadata VALUES ('settings', ?)", (json.dumps(value, sort_keys=True),))
        store.settings = value
