"""Bounded native catalog paging; cursors bind to membership/order and process.

Cursors are opaque navigation values, never authorization or secrets. Link and
service updates do not invalidate a cursor; attach/remove/reorder and restart do.
"""
import base64
import hashlib
import json
import re

PAGE_SIZE = 24
MAX_PROFILES = 24
MAX_CAMERAS = 8
MAX_ITEMS = MAX_PROFILES + MAX_CAMERAS
IDENT = re.compile(r'[a-z0-9][a-z0-9-]{0,31}')


class InstrumentPageError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def ordered_catalog(profiles, cameras):
    """Retain the complete saved-profile order, then deterministic camera IDs."""
    if not isinstance(profiles, list) or len(profiles) > MAX_PROFILES or not isinstance(cameras, list) or len(cameras) > MAX_CAMERAS:
        raise InstrumentPageError('INVALID_CATALOG')
    _ids(profiles + cameras)
    return profiles + sorted(cameras, key=lambda row: row['id'])


def _ids(items):
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise InstrumentPageError('INVALID_CATALOG')
    result = []
    for row in items:
        ident = row.get('id') if isinstance(row, dict) else None
        if not isinstance(ident, str) or not IDENT.fullmatch(ident) or ident in result:
            raise InstrumentPageError('INVALID_CATALOG')
        result.append(ident)
    return result


def _encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def page(items, boot_id, after=None):
    """Return up to 24 rows without dropping any row from subsequent pages.

    CATALOG_CHANGED tells the caller to discard collected pages and restart at
    the first page. INVALID_CURSOR never starts an implicit new traversal.
    """
    identities = _ids(items)
    if not isinstance(boot_id, str) or not re.fullmatch('[0-9a-f]{32}', boot_id):
        raise InstrumentPageError('INVALID_CATALOG')
    digest = hashlib.sha256(json.dumps([boot_id, identities], separators=(',', ':')).encode('ascii')).digest()
    offset = 0
    if after is not None:
        if not isinstance(after, str) or not re.fullmatch('[A-Za-z0-9_-]{46}', after):
            raise InstrumentPageError('INVALID_CURSOR')
        try:
            raw = base64.b64decode(after + '==', altchars=b'-_', validate=True)
        except ValueError:
            raise InstrumentPageError('INVALID_CURSOR') from None
        if len(raw) != 34 or raw[0] != 1 or _encode(raw) != after:
            raise InstrumentPageError('INVALID_CURSOR')
        if raw[1:33] != digest:
            raise InstrumentPageError('CATALOG_CHANGED')
        offset = raw[-1]
        if offset != PAGE_SIZE or offset >= len(items):
            raise InstrumentPageError('INVALID_CURSOR')
    end = min(offset + PAGE_SIZE, len(items))
    more = end < len(items)
    cursor = _encode(b'\x01' + digest + bytes([end])) if more else None
    return {'instruments': items[offset:end], 'next_cursor': cursor, 'has_more': more}
