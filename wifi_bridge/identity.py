"""RAM-only verified instrument identities from read-only manufacturer replies.

A verified identity says which physical instrument answered on a saved profile's
current link. Manufacturer identifiers never leave the call stack: only a keyed,
installation-local digest (`hardware_id`) and an allowlisted model string are kept.
Nothing is persisted, logged or added to events. No command is ever sent from here;
sources are replies that existing read-only probes and observers already receive.
"""
import hmac
import re

SOURCES = ('unverified', 'zwo_model', 'synscan_version', 'vespera_status', 'camera_driver')
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}')


def clean_model(value):
    return value if isinstance(value, str) and MODEL.fullmatch(value) else None


def clean_identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.isprintable(): return None
    return value


class Identities:
    def __init__(self, directory):
        from .config import install_key
        self.key = install_key(directory, b'astro-bridge-instrument-identity')
        self.records = {}

    @staticmethod
    def signature(profile):
        return profile.get('mac'), profile.get('target_ip')

    def record(self, profile, source, model=None, identifier=None):
        """Keep only an allowlisted model and a keyed digest; drop raw identifiers."""
        if source not in SOURCES[1:-1]: raise ValueError('Unknown identity source.')
        model, identifier = clean_model(model), clean_identifier(identifier)
        digest = None
        if identifier is not None:
            message = (profile.get('kind', '') + '\0' + identifier).encode()
            digest = hmac.new(self.key, message, 'sha256').hexdigest()[:16]
        self.records[profile['id']] = {'signature': self.signature(profile), 'source': source,
                                       'model': model, 'hardware_id': digest}
        if len(self.records) > 64: self.records.pop(next(iter(self.records)))

    def project(self, profile):
        """Return (verified_identity, identity_source); stale links are unverified."""
        value = self.records.get(profile['id'])
        if not value or value['signature'] != self.signature(profile): return None, 'unverified'
        return {'model': value['model'], 'hardware_id': value['hardware_id']}, value['source']

    def prune(self, current_ids):
        self.records = {k: v for k, v in self.records.items() if k in current_ids}
