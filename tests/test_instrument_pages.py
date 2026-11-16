"""Frozen 24-row native pages retain all saved profiles plus attached cameras."""
import base64
import unittest

from wifi_bridge.instrument_pages import InstrumentPageError, ordered_catalog, page

BOOT = 'a' * 32


class InstrumentPagesTests(unittest.TestCase):
    def rows(self, count, prefix='profile'):
        return [{'id': prefix + '-' + str(index), 'service': 'ready'} for index in range(count)]

    def code(self, expected, fn, *args):
        with self.assertRaises(InstrumentPageError) as raised:
            fn(*args)
        self.assertEqual(raised.exception.code, expected)

    def test_24_saved_profiles_and_8_cameras_have_no_silent_drop(self):
        profiles, cameras = self.rows(24), self.rows(8, 'usb')
        items = ordered_catalog(profiles, list(reversed(cameras)))
        first = page(items, BOOT)
        self.assertEqual(first['instruments'], profiles)
        self.assertTrue(first['has_more'])
        self.assertEqual(len(first['next_cursor']), 46)
        second = page(items, BOOT, first['next_cursor'])
        self.assertEqual(second['instruments'], cameras)
        self.assertIsNone(second['next_cursor'])
        self.assertFalse(second['has_more'])
        self.assertEqual(first['instruments'] + second['instruments'], items)

    def test_camera_order_is_deterministic_saved_order_is_preserved(self):
        profiles, cameras = self.rows(3), self.rows(8, 'usb')
        self.assertEqual(ordered_catalog(profiles, cameras), ordered_catalog(profiles, list(reversed(cameras))))
        self.assertEqual(ordered_catalog(list(reversed(profiles)), cameras)[:3], list(reversed(profiles)))

    def test_empty_and_existing_profile_only_catalogs_keep_one_page(self):
        for count in (0, 1, 23, 24):
            items = self.rows(count)
            result = page(items, BOOT)
            self.assertEqual(result, {'instruments': items, 'next_cursor': None, 'has_more': False})

    def test_membership_order_and_restart_require_explicit_restart(self):
        items = self.rows(24) + self.rows(8, 'usb')
        cursor = page(items, BOOT)['next_cursor']
        for changed in (items[:-1], items[1:], list(reversed(items)), items[:24] + self.rows(1, 'replacement')):
            self.code('CATALOG_CHANGED', page, changed, BOOT, cursor)
        self.code('CATALOG_CHANGED', page, items, 'b' * 32, cursor)

    def test_state_changes_preserve_cursor_and_deliver_fresh_rows(self):
        items = self.rows(24) + self.rows(8, 'usb')
        cursor = page(items, BOOT)['next_cursor']
        changed = [dict(row, service='unknown') for row in items]
        second = page(changed, BOOT, cursor)
        self.assertEqual(second['instruments'], changed[24:])

    def test_invalid_unbounded_noncanonical_and_unissued_offsets_are_rejected(self):
        items = self.rows(24) + self.rows(8, 'usb')
        cursor = page(items, BOOT)['next_cursor']
        for invalid in ('', [], True, 'a' * 10000, cursor + '=', cursor[:-1], '!' * 46):
            self.code('INVALID_CURSOR', page, items, BOOT, invalid)
        raw = base64.urlsafe_b64decode(cursor + '==')
        for offset in (0, 1, 23, 25, 32, 255):
            invalid = base64.urlsafe_b64encode(raw[:-1] + bytes([offset])).rstrip(b'=').decode()
            self.code('INVALID_CURSOR', page, items, BOOT, invalid)
        invalid = base64.urlsafe_b64encode(b'\x02' + raw[1:]).rstrip(b'=').decode()
        self.code('INVALID_CURSOR', page, items, BOOT, invalid)

    def test_duplicate_ids_or_catalogs_above_runtime_bounds_fail_closed(self):
        for items in (self.rows(33), [{'id': 'same'}, {'id': 'same'}], [{'id': 'x' * 33}],
                      [{'id': 'camera_has_underscores'}], [{}], [None], None):
            self.code('INVALID_CATALOG', page, items, BOOT)
        self.code('INVALID_CATALOG', ordered_catalog, self.rows(25), [])
        self.code('INVALID_CATALOG', ordered_catalog, [], self.rows(9, 'usb'))
        self.code('INVALID_CATALOG', page, [], 'invalid-boot')
