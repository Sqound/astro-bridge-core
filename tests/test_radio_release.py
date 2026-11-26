"""An absent instrument never keeps the bridge's radio from an explicit request.

Simulated radios only (DemoNetwork): no command, no hardware, no real network.
One radio is free for instruments. "first" is the instrument that holds it,
"second" is the one an app asks for.
"""
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_bridge import Fixture
import test_api
from wifi_bridge.api import ApiError, MobileAPI
from wifi_bridge.controller import RETRY_CAP, retry_interval
from wifi_bridge.network import NetworkError
from wifi_bridge.released import ReleaseNotes
from helpers.contract_schema import validate

SPEC = json.loads((Path(__file__).resolve().parents[1] / 'wifi_bridge/static/openapi.json').read_text())
FIRST, SECOND = 'primary', 'second'


class RadioFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.configured(); self.controller.cycle(0)
        self.api = MobileAPI(self.controller); self.facade = self.api.instruments
        self.api.update('telescopes', {'id': SECOND, 'name': 'Second fixture', 'kind': 'seestar', 'target_ip': '10.0.0.1',
                                       'ssid': 'Telescope-DEMO', 'open': True}, create=True)
        self.ops = self.controller.operations
        self.now = 0
        self.attempts = []
        self.absent = set()
        connect = self.net.connect

        def simulated(role, device, config):
            self.attempts.append((role, self.now))
            if role in self.absent: raise NetworkError('Simulation: instrument out of reach.')
            return connect(role, device, config)
        self.net.connect = simulated
        scan = self.net.scan
        # Both instruments are heard, each on its own access point.
        self.net.scan = lambda *args, **kwargs: scan(*args, **kwargs) + [
            {'ssid': 'Vespera-ABC', 'bssid': '02:00:00:77:88:99', 'signal': 70, 'frequency': 2437, 'security': 'WPA2'}]
        self.addCleanup(self.finish)

    def finish(self):
        if self.ops.thread: self.ops.thread.join(10)

    def tick(self, seconds=5):
        self.now += seconds
        self.controller.cycle(self.now)

    def lose_first(self, failed_attempts=1):
        """The first instrument goes out of reach and this many new attempts fail."""
        self.absent.add('telescope'); self.net.disconnect('telescope')
        while self.controller.retries.get('telescope', 0) < failed_attempts: self.tick()

    def profile(self, ident):
        return next(p for p in self.store.config['telescopes'] if p['id'] == ident)

    def row(self, ident):
        return next(r for r in self.controller.state['telescopes'] if r['id'] == ident)

    def item(self, ident, client='paired-client'):
        value = self.facade.read(ident, client)
        validate(value, SPEC['components']['schemas']['NativeInstrument'], SPEC)
        return value

    def connect(self, ident=SECOND, client='paired-client', **data):
        return self.facade.connect(ident, data, '"' + self.api.revision() + '"', client, time.monotonic() + 3600, 'home')

    def refused(self, **data):
        with self.assertRaises(ApiError) as error: self.connect(**data)
        self.assertEqual((error.exception.status, error.exception.code), (409, 'NO_FREE_RADIO'))
        validate({'error': {'code': error.exception.code, 'message': error.exception.message, **error.exception.details}},
                 SPEC['components']['schemas']['NativeError'], SPEC)
        return error.exception.details


class UnreachableHolderTests(RadioFixture):
    def test_request_for_another_instrument_takes_the_radio_of_one_that_only_retries(self):
        # The bench scenario: first linked through the bridge, then switched off; its profile stays enabled.
        code, value = self.connect(FIRST); self.assertEqual(code, 200, value)
        self.lose_first()
        self.assertTrue(self.profile(FIRST)['enabled'])
        self.assertEqual(self.row(FIRST)['state'], 'waiting')
        self.assertEqual(self.item(FIRST)['link'], 'saved')
        code, value = self.connect(wait_seconds=5)
        self.assertEqual(code, 200, value)
        validate(value, SPEC['components']['schemas']['NativeReady'], SPEC)
        self.assertEqual(value['displaced'], {'instrument_id': FIRST, 'name': self.profile(FIRST)['name'],
                                              'kind': 'smart_telescope', 'reason': 'UNREACHABLE'})
        self.assertTrue(self.row(SECOND)['connected'])
        self.assertTrue(self.profile(SECOND)['enabled'])
        # The first one gave up the radio: no more attempts, and the state says why.
        self.assertFalse(self.profile(FIRST)['enabled'])
        self.assertFalse([lease for lease in self.controller.protection.leases.values() if lease['profile_id'] == FIRST])
        released = self.item(FIRST)['released']
        self.assertEqual((released['reason'], released['to']), ('RADIO_YIELDED', SECOND))
        self.assertEqual(self.controller.status()['radio_releases'][FIRST]['reason'], 'RADIO_YIELDED')
        operation = self.facade.operation(self.ops.last, 'home')
        validate(operation, SPEC['components']['schemas']['NativeOperation'], SPEC)
        self.assertEqual((operation['state'], operation['displaced']['instrument_id'], operation['displaced']['reason']),
                         ('succeeded', FIRST, 'UNREACHABLE'))
        before = len([a for a in self.attempts if a[0] == 'telescope'])
        for _ in range(80): self.tick()
        self.assertEqual(len([a for a in self.attempts if a[0] == 'telescope']), before)
        self.assertTrue(self.row(SECOND)['connected'])

    def test_first_keeps_its_place_when_the_second_cannot_be_linked(self):
        self.lose_first()
        self.absent.add('telescope-' + SECOND)
        code, value = self.connect(); self.assertEqual(code, 202, value)
        self.finish()
        operation = self.ops.read(value['operation']['id'])
        self.assertEqual((operation['state'], operation['restoration']), ('failed', {'state': 'not_needed', 'reason': 'ORIGINAL_INTENT_PRESERVED'}))
        self.assertTrue(self.profile(FIRST)['enabled'])
        self.assertFalse(self.profile(SECOND)['enabled'])
        self.assertFalse(self.controller.pauses.held(FIRST))
        self.assertNotIn('released', self.item(FIRST))
        # The first one comes back: the ordinary retries find it again.
        self.absent.clear()
        for _ in range(80): self.tick()
        self.assertTrue(self.row(FIRST)['connected'])

    def test_reserved_or_just_lost_instruments_do_not_give_way(self):
        self.lose_first()
        self.controller.protection.native_create(FIRST, 'other-client', 900)
        holder = self.refused()['holders'][0]
        self.assertEqual((holder['state'], holder['blocker'], holder['forceable']), ('unreachable', 'LEASED_BY_OTHER_CLIENT', False))
        self.refused(force_replace=[FIRST])
        self.controller.protection.leases.clear()
        self.profile(FIRST)['owner_locked'] = True
        self.assertEqual(self.refused()['holders'][0]['blocker'], 'OWNER_LOCKED')
        self.profile(FIRST)['owner_locked'] = False
        # Link lost a moment ago, no new attempt has failed yet: not proven out of reach.
        self.controller.retries.clear()
        self.assertEqual(self.refused()['holders'][0]['state'], 'reconnecting')
        self.assertTrue(self.profile(FIRST)['enabled'])


class ConnectedHolderTests(RadioFixture):
    def test_connected_instrument_is_named_then_replaced_only_when_forced(self):
        self.assertTrue(self.row(FIRST)['connected'])
        details = self.refused()
        self.assertEqual(details['holders'], [{'instrument_id': FIRST, 'name': self.profile(FIRST)['name'], 'kind': 'smart_telescope',
                                               'state': 'connected', 'current_operation': None, 'blocker': None, 'forceable': True}])
        self.assertEqual((details['replaceable'], details['free_radios'], details['simultaneous']), ([], 0, False))
        self.tick()
        self.assertTrue(self.row(FIRST)['connected']); self.assertTrue(self.profile(FIRST)['enabled'])
        # Naming another instrument forces nothing.
        self.refused(force_replace=['someone-else'])
        code, value = self.connect(wait_seconds=5, force_replace=[FIRST])
        self.assertEqual(code, 200, value)
        self.assertEqual((value['displaced']['instrument_id'], value['displaced']['reason']), (FIRST, 'FORCED'))
        self.assertFalse(self.profile(FIRST)['enabled'])
        self.assertTrue(self.row(SECOND)['connected'])
        self.assertEqual(self.item(FIRST)['released']['reason'], 'REPLACED_ON_REQUEST')
        # A user's confirmed switch is not slowed by the automatic replacement cooldown.
        code, value = self.connect(FIRST, wait_seconds=5, force_replace=[SECOND])
        self.assertEqual(code, 200, value)
        self.assertNotIn('released', self.item(FIRST))

    def test_running_operation_or_mount_is_reported_in_service(self):
        with patch.object(self.controller, 'operation_state', return_value='running'):
            holder = self.refused()['holders'][0]
        self.assertEqual((holder['state'], holder['forceable']), ('in_service', True))
        self.profile(FIRST)['kind'] = 'synscan'
        holder = self.refused()['holders'][0]
        self.assertEqual((holder['kind'], holder['state']), ('mount', 'in_service'))
        self.assertTrue(self.row(FIRST)['connected'])

    def test_forced_replacement_restores_the_first_when_the_second_fails(self):
        self.absent.add('telescope-' + SECOND)
        code, value = self.connect(force_replace=[FIRST]); self.assertEqual(code, 202, value)
        self.finish()
        operation = self.ops.read(value['operation']['id'])
        self.assertEqual((operation['state'], operation['restoration']['state']), ('failed', 'restored'))
        self.assertTrue(self.profile(FIRST)['enabled'])
        self.assertTrue(self.net.connected('telescope', self.devices[0]))
        self.assertNotIn('released', self.item(FIRST))

    def test_force_respects_reservations_of_another_client(self):
        self.controller.protection.native_create(FIRST, 'other-client', 900)
        holder = self.refused(force_replace=[FIRST])['holders'][0]
        self.assertEqual((holder['blocker'], holder['forceable']), ('LEASED_BY_OTHER_CLIENT', False))
        self.assertTrue(self.row(FIRST)['connected'])


class ReleasedByAppTests(RadioFixture):
    """The app's own "disconnect" releases its reservation only: the link stays up."""

    def released(self):
        code, value = self.connect(FIRST); self.assertEqual(code, 200, value)
        self.facade.action(FIRST, 'release', {'lease_id': value['lease']['id']}, 'paired-client')
        self.tick()

    def test_link_stays_up_but_the_next_request_takes_the_radio_without_forcing(self):
        self.released()
        self.assertTrue(self.profile(FIRST)['enabled']); self.assertTrue(self.row(FIRST)['connected'])
        self.assertEqual(self.item(FIRST)['link'], 'connected')
        code, value = self.connect(wait_seconds=5)
        self.assertEqual(code, 200, value)
        self.assertEqual((value['displaced']['instrument_id'], value['displaced']['reason']), (FIRST, 'STANDBY'))
        self.assertTrue(self.row(SECOND)['connected'])
        self.assertFalse(self.profile(FIRST)['enabled'])
        released = self.item(FIRST)['released']
        self.assertEqual((released['reason'], released['to']), ('RADIO_YIELDED', SECOND))

    def test_released_instrument_still_in_use_or_reserved_again_does_not_give_way(self):
        self.released()
        with patch.object(self.controller, 'operation_state', return_value='running'):
            self.assertEqual(self.refused()['holders'][0]['state'], 'in_service')
        with patch.object(self.net, 'activity_status', return_value={'profiles': {FIRST: {'state': 'active'}}}, create=True):
            self.assertEqual(self.refused()['holders'][0]['state'], 'in_service')
        self.profile(FIRST)['kind'] = 'synscan'
        self.assertEqual(self.refused()['holders'][0]['state'], 'in_service')
        self.profile(FIRST)['kind'] = 'vespera'
        # Another app reserves it: no longer on standby.
        self.controller.protection.native_create(FIRST, 'other-client', 900)
        holder = self.refused()['holders'][0]
        self.assertEqual((holder['state'], holder['forceable']), ('connected', False))
        self.controller.protection.leases.clear()
        # The same app comes back to it: an ordinary connected instrument again.
        code, value = self.connect(FIRST); self.assertEqual(code, 200, value)
        self.controller.protection.leases.clear()
        self.assertEqual(self.refused()['holders'][0]['state'], 'connected')
        self.assertTrue(self.row(FIRST)['connected'])

    def test_released_instrument_is_restored_when_the_second_fails(self):
        self.released()
        self.absent.add('telescope-' + SECOND)
        code, value = self.connect(); self.assertEqual(code, 202, value)
        self.finish()
        self.assertEqual(self.ops.read(value['operation']['id'])['restoration']['state'], 'restored')
        self.assertTrue(self.profile(FIRST)['enabled'])
        self.assertTrue(self.net.connected('telescope', self.devices[0]))


class SwitchedOffTests(RadioFixture):
    def test_disconnect_with_switched_off_stops_retries_and_says_so(self):
        code, value = self.connect(FIRST); self.assertEqual(code, 200, value)
        self.assertEqual(self.facade.action(FIRST, 'disconnect', {'lease_id': value['lease']['id'], 'reason': 'switched_off'}, 'paired-client'), {'ok': True})
        self.absent.add('telescope')
        self.attempts.clear()
        for _ in range(80): self.tick()
        self.assertEqual(self.attempts, [])
        self.assertFalse(self.profile(FIRST)['enabled'])
        item = self.item(FIRST)
        self.assertEqual((item['link'], item['released']['reason'], item['released']['to']), ('saved', 'SWITCHED_OFF', None))
        self.assertNotIn('retry', item)
        self.assertEqual(self.controller.free_radios(), 1)
        # The note survives a service restart, without its age.
        self.assertEqual(ReleaseNotes(self.store.path.parent).public(FIRST), {'reason': 'SWITCHED_OFF', 'to': None, 'age_seconds': None})
        # The radio serves the next request at once, nothing is displaced.
        code, value = self.connect(wait_seconds=5); self.assertEqual(code, 200, value)
        self.assertNotIn('displaced', value)
        # Linking it again clears the note.
        self.facade.action(SECOND, 'disconnect', {}, 'paired-client'); self.tick()
        self.absent.clear()
        code, value = self.connect(FIRST, wait_seconds=5); self.assertEqual(code, 200, value)
        self.tick()
        self.assertNotIn('released', self.item(FIRST))

    def test_reason_is_validated_and_plain_disconnect_leaves_no_note(self):
        for reason in ('', 'lost', None, 5, ['switched_off']):
            with self.assertRaises(ApiError) as error: self.facade.action(FIRST, 'disconnect', {'reason': reason}, 'paired-client')
            self.assertEqual((error.exception.status, error.exception.code), (400, 'INVALID_FIELDS'))
        with self.assertRaises(ApiError): self.facade.action(FIRST, 'release', {'lease_id': 'x', 'reason': 'switched_off'}, 'paired-client')
        self.assertTrue(self.profile(FIRST)['enabled'])
        self.facade.action(FIRST, 'disconnect', {}, 'paired-client'); self.tick()
        self.assertNotIn('released', self.item(FIRST))


class LostLinkTests(RadioFixture):
    def test_retries_are_spaced_up_to_a_ceiling_and_reported(self):
        self.assertEqual([retry_interval(n) for n in range(9)], [15, 15, 30, 60, 120, 240, 300, 300, 300])
        self.assertEqual(retry_interval(10 ** 6), RETRY_CAP)
        self.absent.add('telescope'); self.net.disconnect('telescope')
        self.attempts.clear()
        while self.now < 2000: self.tick()
        times = [at for role, at in self.attempts if role == 'telescope']
        self.assertEqual([b - a for a, b in zip(times, times[1:])][:8], [15, 30, 60, 120, 240, 300, 300, 300])
        self.assertTrue(self.profile(FIRST)['enabled'])
        retry = self.row(FIRST)['retry']
        self.assertEqual((retry['attempts'], retry['interval_seconds']), (len(times), RETRY_CAP))
        self.assertTrue(0 <= retry['next_in_seconds'] <= RETRY_CAP)
        item = self.item(FIRST)
        self.assertEqual(item['retry'], retry); self.assertNotIn('released', item)
        # The radio is available again to an explicit request for another instrument.
        code, value = self.connect(wait_seconds=5); self.assertEqual(code, 200, value)
        self.assertEqual(value['displaced']['reason'], 'UNREACHABLE')

    def test_pace_starts_again_after_the_instrument_returns(self):
        self.lose_first(failed_attempts=4)
        self.absent.clear()
        for _ in range(60): self.tick()
        self.assertTrue(self.row(FIRST)['connected'])
        self.assertNotIn('telescope', self.controller.retries)
        self.assertNotIn('retry', self.item(FIRST))
        self.absent.add('telescope'); self.net.disconnect('telescope'); self.attempts.clear()
        for _ in range(12): self.tick()
        times = [at for role, at in self.attempts if role == 'telescope']
        self.assertEqual([b - a for a, b in zip(times, times[1:])][:2], [15, 30])

    def test_explicit_request_for_the_lost_instrument_itself_ends_the_wait(self):
        self.lose_first(failed_attempts=5)
        self.absent.clear(); self.attempts.clear()
        # The answer itself is the earlier one (its own radio is not free); the next reconcile tries at once.
        with self.assertRaises(ApiError) as error: self.connect(FIRST)
        self.assertEqual(error.exception.code, 'NO_FREE_RADIO')
        self.tick()
        self.assertEqual(len(self.attempts), 1)
        self.assertTrue(self.row(FIRST)['connected'])


class EarlierClientTests(RadioFixture):
    """A client that sends and reads nothing new."""

    def test_connected_holder_is_still_preserved_with_the_same_refusal(self):
        details = self.refused()
        self.assertEqual((details['replaceable'], details['free_radios'], details['simultaneous']), ([], 0, False))
        self.assertTrue(self.row(FIRST)['connected'])
        item = self.item(FIRST)
        self.assertFalse({'retry', 'released'} & set(item))

    def test_earlier_request_shape_links_past_an_unreachable_instrument(self):
        self.lose_first()
        # As sent by an earlier app: it names the night's other instrument, which is not owner-replaceable.
        code, value = self.connect(wait_seconds=5, lease_seconds=900, allow_replace=[FIRST])
        self.assertEqual(code, 200, value)
        self.assertLessEqual({'state', 'instrument_id', 'lease', 'endpoint'}, set(value))
        self.assertEqual(value['endpoint']['instrument_id'], SECOND)

    def test_free_radio_and_plain_disconnect_answers_are_unchanged(self):
        self.facade.action(FIRST, 'disconnect', {}, 'paired-client'); self.tick()
        code, value = self.connect(wait_seconds=5); self.assertEqual(code, 200, value)
        self.assertEqual(set(value), {'state', 'instrument_id', 'lease', 'endpoint'})
        self.assertEqual(self.facade.action(SECOND, 'disconnect', {'lease_id': value['lease']['id']}, 'paired-client'), {'ok': True})

    def test_busy_network_is_waited_for_only_within_wait_seconds(self):
        self.lose_first()
        self.assertTrue(self.controller.net_lock.acquire(blocking=False))
        try:
            with self.assertRaises(ApiError) as error: self.connect()
            self.assertEqual(error.exception.code, 'NETWORK_BUSY')
            threading.Timer(.4, self.controller.net_lock.release).start()
            code, value = self.connect(wait_seconds=5)
            self.assertEqual(code, 200, value)
        finally:
            if self.controller.net_lock.locked() and not self.ops.pending:
                try: self.controller.net_lock.release()
                except RuntimeError: pass


class HTTPTests(Fixture):
    def setUp(self):
        super().setUp(); self.configured(); self.controller.cycle(0)
        from wifi_bridge.web import WebServer
        self.server = WebServer(('127.0.0.1', 0), self.controller)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.token = ''

    request = test_api.APITests.request
    login = test_api.APITests.login

    def test_refusal_names_the_holder_and_switched_off_is_accepted_over_http(self):
        self.login()
        code, created, _ = self.request('POST', '/api/v1/telescopes', {'id': SECOND, 'name': 'Second fixture', 'kind': 'seestar',
                                                                       'target_ip': '10.0.0.1', 'ssid': 'Telescope-DEMO', 'open': True})
        self.assertEqual(code, 201, created)
        revision = {'If-Match': '"' + self.request('GET', '/api/v1/instruments')[1]['config_revision'] + '"'}
        code, body, _ = self.request('POST', '/api/v1/instruments/' + SECOND + '/connect', {}, revision)
        self.assertEqual((code, body['error']['code']), (409, 'NO_FREE_RADIO'))
        self.assertEqual([(h['instrument_id'], h['state'], h['forceable']) for h in body['error']['holders']], [(FIRST, 'connected', True)])
        self.assertEqual(self.request('POST', '/api/v1/instruments/' + SECOND + '/connect', {'force_replace': 'primary'}, revision)[0], 400)
        self.assertEqual(self.request('POST', '/api/v1/instruments/' + FIRST + '/disconnect', {'reason': 'parked'})[0], 400)
        self.assertEqual(self.request('POST', '/api/v1/instruments/' + FIRST + '/disconnect', {'reason': 'switched_off'})[0], 200)
        self.assertEqual(self.request('GET', '/api/v1/instruments/' + FIRST)[1]['released']['reason'], 'SWITCHED_OFF')


if __name__ == '__main__':
    unittest.main()
