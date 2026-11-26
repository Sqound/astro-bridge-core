"""Native contract 1.1 additions against the real HTTP application (synthetic radios only)."""
from contextlib import contextmanager
import http.client
import json
import tempfile
import threading
import unittest

from wifi_bridge.config import Store, validate_config
from wifi_bridge.controller import Controller
from wifi_bridge.network import DemoNetwork
from wifi_bridge.web import WebServer


@contextmanager
def bridge(radios=4, extra_profiles=()):
    with tempfile.TemporaryDirectory() as directory:
        net = DemoNetwork(radios); devices = net.devices(); store = Store(directory, devices[0]['mac'])
        telescope = {'id': 'tel-seestar-1', 'name': 'Test telescope', 'kind': 'seestar', 'ssid': 'Telescope-DEMO',
                     'open': True, 'mac': devices[0]['mac'], 'target_ip': '198.51.100.1', 'enabled': True}
        config = {'admin_password': 'test-password-1', 'hostname': 'astro-bridge-test', 'country': 'JP',
                  'home': {'ssid': 'Synthetic home', 'open': True, 'mac': devices[1]['mac']},
                  'hotspot': {'enabled': True, 'mac': devices[2]['mac']}, 'connection_mode': 'private',
                  'telescopes': [telescope, *[dict(p) for p in extra_profiles]]}
        store.save(validate_config(config, store.config, devices))
        c = Controller(store, net, {'password': 'test-password-1'}, demo=True); c.update(devices=net.devices()); c.cycle(0)
        server = WebServer(('127.0.0.1', 0), c); thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            yield c, server, net
        finally:
            if c.operations.thread: c.operations.thread.join(5)
            c.stop.set(); server.shutdown(); server.server_close(); thread.join()


def call(server, method, path, body=None, token=None, headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=30)
    sent = dict(headers or {}, **({'Authorization': 'Bearer ' + token} if token else {}))
    if body is not None: sent['Content-Type'] = 'application/json'
    connection.request(method, path, None if body is None else json.dumps(body), sent)
    response = connection.getresponse(); value = json.loads(response.read())
    result = response.status, {k.lower(): v for k, v in response.getheaders()}, value
    connection.close(); return result


def tokens(server, scopes=('read', 'connect', 'telemetry')):
    _, _, admin = call(server, 'POST', '/api/v1/auth/token', {'password': 'test-password-1'})
    _, _, paired = call(server, 'POST', '/api/v1/auth/clients', {'name': 'Synthetic client', 'scopes': list(scopes)}, admin['access_token'])
    _, _, client = call(server, 'POST', '/api/v1/auth/token', {'client_id': paired['client_id'], 'client_secret': paired['client_secret']})
    return admin['access_token'], client['access_token']


class InstrumentDescriptionTests(unittest.TestCase):
    def test_protocol_model_and_owner_name_rename(self):
        with bridge() as (c, server, _):
            admin, client = tokens(server)
            _, _, catalog = call(server, 'GET', '/api/v1/instruments', token=client)
            item = catalog['instruments'][0]
            self.assertEqual((item['protocol'], item['model'], item['name']), ('seestar', None, 'Test telescope'))
            # The owner's personal name is the profile name, editable by the admin API.
            status, _, _ = call(server, 'PATCH', '/api/v1/telescopes/tel-seestar-1', {'name': 'Garden scope'}, admin,
                                {'If-Match': '"' + catalog['config_revision'] + '"'})
            self.assertEqual(status, 200)
            _, _, item = call(server, 'GET', '/api/v1/instruments/tel-seestar-1', token=client)
            self.assertEqual((item['name'], item['id'], item['family']), ('Garden scope', 'tel-seestar-1', 'seestar'))

    def test_every_family_declares_its_protocol(self):
        from wifi_bridge.plugins import plugin
        self.assertEqual({k: plugin(k).get('protocol') for k in ('generic', 'seestar', 'vespera', 'dwarf', 'zwo_mount', 'synscan')},
                         {'generic': None, 'seestar': 'seestar', 'vespera': 'vespera', 'dwarf': 'dwarf', 'zwo_mount': 'lx200', 'synscan': 'synscan'})


class IdentityTests(unittest.TestCase):
    def test_identity_keeps_only_model_and_keyed_digest_for_the_current_link(self):
        from wifi_bridge.identity import Identities
        with tempfile.TemporaryDirectory() as directory:
            identities = Identities(directory)
            profile = {'id': 'scope', 'kind': 'vespera', 'mac': '02:00:00:00:00:01', 'target_ip': '198.51.100.1'}
            self.assertEqual(identities.project(profile), (None, 'unverified'))
            identities.record(profile, 'vespera_status', 'vespera2', 'private-serial-sentinel')
            verified, source = identities.project(profile)
            self.assertEqual((verified['model'], source), ('vespera2', 'vespera_status'))
            self.assertRegex(verified['hardware_id'], '^[0-9a-f]{16}$')
            self.assertNotIn('private-serial-sentinel', repr(identities.records))
            # Same instrument, same installation: same digest; another installation differs.
            identities.record(profile, 'vespera_status', 'vespera2', 'private-serial-sentinel')
            self.assertEqual(identities.project(profile)[0], verified)
            with tempfile.TemporaryDirectory() as other:
                again = Identities(other); again.record(profile, 'vespera_status', 'vespera2', 'private-serial-sentinel')
                self.assertNotEqual(again.project(profile)[0]['hardware_id'], verified['hardware_id'])
            # A changed radio/target link is no longer verified by the old reply.
            self.assertEqual(identities.project(dict(profile, mac='02:00:00:00:00:02')), (None, 'unverified'))
            identities.record(profile, 'zwo_model', 'AM5N; rm -rf', None)
            self.assertEqual(identities.project(profile)[0], {'model': None, 'hardware_id': None})
            with self.assertRaises(ValueError): identities.record(profile, 'unverified')

    def test_mount_reply_is_recorded_by_owner_discovery_without_new_commands(self):
        from wifi_bridge.mounts import zwo_identity
        reply = zwo_identity(b'AM5N#')
        with tempfile.TemporaryDirectory() as directory:
            from wifi_bridge.identity import Identities
            identities = Identities(directory)
            profile = {'id': 'mount', 'kind': 'zwo_mount', 'mac': '02:00:00:00:00:03', 'target_ip': '198.51.100.3'}
            identities.record(profile, 'zwo_model', reply['model'])
            self.assertEqual(identities.project(profile), ({'model': 'AM5N', 'hardware_id': None}, 'zwo_model'))


if __name__ == '__main__':
    unittest.main()


class SwitchProgressTests(unittest.TestCase):
    def test_progress_projection_is_bounded_and_monotone(self):
        from wifi_bridge.operations import progress
        running = {'state': 'running'}
        etas = [progress(dict(running, phase=phase), elapsed, 100)['eta_seconds']
                for phase, elapsed in (('selecting', 0), ('associating', 10), ('checking_services', 30), ('checking_services', 90))]
        self.assertEqual(etas, sorted(etas, reverse=True))
        self.assertTrue(all(1 <= eta <= 120 for eta in etas))
        self.assertEqual(progress(dict(running, phase='checking_services'), 90, 3)['eta_seconds'], 1)
        self.assertEqual(progress({'state': 'succeeded', 'phase': 'finished'}, 5, 0), {'phase': 'ready', 'eta_seconds': 0})
        self.assertEqual(progress({'state': 'failed', 'phase': 'finished'}, 5, 0), {'phase': 'failed', 'eta_seconds': None})
        self.assertEqual(progress(dict(running, phase='disconnecting_victim'), 0, 100)['phase'], 'leaving')
        self.assertEqual(progress(dict(running, phase='restoring'), 0, 30), {'phase': 'restoring', 'eta_seconds': 30})

    def test_switch_events_are_validated(self):
        from wifi_bridge.events import valid_native
        op = '0' * 32 + '.' + '1' * 32
        self.assertTrue(valid_native('instrument.switch_progress', 'scope', {'operation': op, 'phase': 'verifying', 'eta_seconds': 9}))
        self.assertTrue(valid_native('instrument.switch_completed', 'scope', {'operation': op, 'outcome': 'failed', 'reason': 'TIMED_OUT'}))
        for data in ({'operation': op, 'phase': 'ready', 'eta_seconds': 9}, {'operation': op, 'phase': 'leaving', 'eta_seconds': 0},
                     {'operation': 'x', 'phase': 'leaving', 'eta_seconds': 5}, {'operation': op, 'phase': 'leaving', 'eta_seconds': True}):
            self.assertFalse(valid_native('instrument.switch_progress', 'scope', data), data)
        self.assertFalse(valid_native('instrument.switch_completed', 'scope', {'operation': op, 'outcome': 'ok', 'reason': 'READY'}))


class NightReservationTests(unittest.TestCase):
    def test_reservation_bounds_protection_and_owner_release(self):
        with bridge() as (c, server, _):
            admin, client = tokens(server)
            _, _, catalog = call(server, 'GET', '/api/v1/instruments', token=client)
            revision = {'If-Match': '"' + catalog['config_revision'] + '"'}
            for bad in (59, 50401, True, '36000'):
                status, _, body = call(server, 'POST', '/api/v1/instruments/tel-seestar-1/connect', {'reserve_seconds': bad}, client, revision)
                self.assertEqual((status, body['error']['code']), (400, 'INVALID_FIELDS'), bad)
            status, _, ready = call(server, 'POST', '/api/v1/instruments/tel-seestar-1/connect', {'reserve_seconds': 50400}, client, revision)
            self.assertEqual(status, 200)
            self.assertEqual(ready['lease']['expires_in'], 3600)
            self.assertGreater(ready['lease']['reservation_expires_in'], 50000)
            # A reservation is ordinary protection: no replacement or reassignment without release.
            self.assertTrue(c.protection.protected('tel-seestar-1'))
            # The owner can revoke it with the existing administrator release.
            _, _, protection = call(server, 'GET', '/api/v1/protection', token=admin)
            lease = protection['leases'][0]
            _, _, settings = call(server, 'GET', '/api/v1/settings', token=admin)
            status, _, _ = call(server, 'POST', '/api/v1/leases/' + lease['id'] + '/release',
                                {'version': lease['version'], 'confirm_release': True, 'current_password': 'test-password-1'},
                                admin, {'If-Match': '"' + settings['config_revision'] + '"'})
            self.assertEqual(status, 200)
            self.assertFalse(c.protection.protected('tel-seestar-1'))


class AnnouncementAndRetryTests(unittest.TestCase):
    SETTINGS = {'name': 'Test bridge', 'device_id': '0123456789abcdef', 'version': '0.0.0'}

    def test_txt_hints_are_bounded_and_fingerprint_needs_https(self):
        from wifi_bridge.announce import txt_records
        records = txt_records(self.SETTINGS, {'pairing': True, 'free_radios': 2, 'hotspot': True,
                                              'https_port': 443, 'tls_fingerprint': 'ab' * 32})
        txt = dict(r.split('=', 1) for r in records)
        self.assertEqual((txt['fp'], txt['free_radios'], txt['hotspot'], txt['contract-minor']), ('ab' * 32, '2', '1', '2'))
        plain = dict(r.split('=', 1) for r in txt_records(self.SETTINGS, {'free_radios': 0, 'hotspot': False, 'tls_fingerprint': 'ab' * 32}))
        self.assertNotIn('fp', plain); self.assertNotIn('hotspot', plain); self.assertEqual(plain['free_radios'], '0')
        for bad in ({'free_radios': -1}, {'free_radios': 17}, {'free_radios': True},
                    {'https_port': 443, 'tls_fingerprint': 'AB' * 32}, {'https_port': 443, 'tls_fingerprint': 'ab'}):
            with self.subTest(runtime=bad), self.assertRaises(ValueError): txt_records(self.SETTINGS, bad)

    def test_info_and_retry_after_over_http(self):
        with bridge() as (c, server, _):
            c.event_store.clock.sync = lambda: None  # The host's chrony must not decide the limit.
            _, _, info = call(server, 'GET', '/api/v1/info')
            self.assertEqual((info['contract_minor'], info['free_radios'], info['tls_fingerprint']), (2, c.free_radios(), None))
            _, client = tokens(server)
            body = {'utc_iso': '2026-11-04T00:00:00Z', 'uncertainty_ms': 500}
            self.assertEqual(call(server, 'POST', '/api/v1/system/time-hint', body, client)[0], 200)
            status, headers, error = call(server, 'POST', '/api/v1/system/time-hint', body, client)
            self.assertEqual((status, headers['retry-after'], error['error']['retry_after']), (429, '60', 60))

    def test_connect_limit_reports_remaining_seconds(self):
        from wifi_bridge.api import ApiError
        self.assertEqual(ApiError(429, 'X', 'm').retry_after, 60)
        self.assertIsNone(ApiError(409, 'X', 'm').retry_after)
        self.assertEqual(ApiError(429, 'X', 'm', retry_after=7).retry_after, 7)

    def test_saturated_event_waits_answer_now_with_retry_after(self):
        from pathlib import Path
        from wifi_bridge.events import EventStore
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / 'events.json')
            store.waiters = 4
            page = store.read(store.sequence, store.boot_id, 10, 5)
            self.assertEqual(page['_retry_after'], 2)
            store.waiters = 0
            self.assertNotIn('_retry_after', store.read(store.sequence, store.boot_id, 10, 0))
