"""A link that cannot be made says which step failed, and asking again is served.

Simulated radios only (DemoNetwork) and a patched command runner: no command,
no hardware, no real network.
"""
import contextlib
import io
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_radio_release import FIRST, SECOND, SPEC, RadioFixture
from helpers.contract_schema import validate
from wifi_bridge.api import ApiError
from wifi_bridge.config import Invalid
from wifi_bridge.network import LinuxNetwork, NetworkError, link_failure_stage, run

UP = ['nmcli', '--wait', '35', 'connection', 'up', 'id', 'wifi-bridge-telescope']


class StepNamingTests(unittest.TestCase):
    def test_step_is_named_only_from_what_the_system_reports(self):
        for arguments, expected in (
                (dict(activation=53), 'NETWORK_NOT_FOUND'),
                (dict(reason=53, state=30), 'NETWORK_NOT_FOUND'),
                (dict(state=70), 'ADDRESS_NOT_OBTAINED'),
                (dict(activation=5), 'ADDRESS_NOT_OBTAINED'),
                (dict(activation=17), 'ADDRESS_NOT_OBTAINED'),
                (dict(state=50), 'NOT_ASSOCIATED'),
                (dict(activation=11), 'NOT_ASSOCIATED'),
                (dict(), None), (dict(state=30, reason=0), None), (dict(activation='53'), None)):
            self.assertEqual(link_failure_stage(**arguments), expected, arguments)

    def test_only_the_numeric_code_of_a_failed_activation_is_kept(self):
        result = subprocess.CompletedProcess(UP, 4, stdout='', stderr='Error: Connection activation failed: (53) synthetic-private-name\n')
        with patch('wifi_bridge.network.subprocess.run', return_value=result):
            with self.assertRaises(NetworkError) as caught: run(UP)
        self.assertEqual(caught.exception.activation, 53)
        self.assertNotIn('synthetic-private-name', str(caught.exception))
        self.assertIsNone(NetworkError('other').activation)

    def attempt(self, failure, state, reason='0 (No reason given)'):
        with tempfile.TemporaryDirectory() as folder:
            net = LinuxNetwork(folder)

            def command(args, **kwargs):
                if 'up' in args: raise failure
                if 'GENERAL.REASON' in args: return reason
                if 'GENERAL.STATE' in args: return state
                return ''
            with patch.object(net, '_profile', return_value='wifi-bridge-telescope'), \
                    patch('wifi_bridge.network.run', side_effect=command), patch.object(net, 'disconnect') as cancel:
                with self.assertRaises(NetworkError) as caught:
                    net.connect('telescope', {'name': 'wlan2', 'mac': '02:00:00:00:00:02'}, {'ssid': 'Synthetic-Mount'})
                cancel.assert_called_once_with('telescope')
            return caught.exception.stage

    def test_three_ways_a_mount_wifi_was_seen_to_fail(self):
        timed_out = NetworkError('System command unavailable or timed out: nmcli')
        # Associated in two seconds, then no address until the wait ran out.
        self.assertEqual(self.attempt(timed_out, '70 (connecting (getting IP configuration))'), 'ADDRESS_NOT_OBTAINED')
        # Authentication sent, no answer.
        self.assertEqual(self.attempt(timed_out, '50 (connecting (configuring))'), 'NOT_ASSOCIATED')
        # The network is no longer heard.
        absent = NetworkError('System command failed: nmcli'); absent.activation = 53
        self.assertEqual(self.attempt(absent, '30 (disconnected)'), 'NETWORK_NOT_FOUND')
        # Nothing names a step: none is invented.
        self.assertIsNone(self.attempt(NetworkError('System command failed: nmcli'), '30 (disconnected)'))


class AddressFallbackTests(unittest.TestCase):
    """A mount that accepts the link and gives no address (seen on a SynScan mount)."""
    MOUNT = {'ssid': 'Synthetic-Mount', 'kind': 'synscan', 'target_ip': '192.168.4.1'}

    def attempt(self, profile=None, *, enabled=True, taken=(), silent=False, role='telescope-mount'):
        self.profiles, self.commands = [], []
        with tempfile.TemporaryDirectory() as folder:
            net = LinuxNetwork(folder); net.address_fallback = enabled

            def write(role, sections):
                self.profiles.append(dict(sections['ipv4'])); return 'wifi-bridge-' + role

            def command(args, **kwargs):
                self.commands.append(list(args))
                if 'up' in args:
                    current = self.profiles[-1]
                    if current['method'] == 'auto': raise NetworkError('System command unavailable or timed out: nmcli')
                    if current['address1'] in taken:
                        error = NetworkError('System command failed: nmcli'); error.activation = 64
                        raise error
                if args[0] == 'ping' and silent: raise NetworkError('System command failed: ping')
                if 'GENERAL.STATE' in args: return '70 (connecting (getting IP configuration))'
                return '0 (No reason given)' if 'GENERAL.REASON' in args else ''
            with patch.object(net, '_profile', side_effect=write), patch('wifi_bridge.network.run', side_effect=command), \
                    patch.object(net, 'disconnect'), contextlib.redirect_stdout(io.StringIO()) as log:
                try: net.connect(role, {'name': 'wlan2', 'mac': '02:00:00:00:00:02'}, dict(profile or self.MOUNT))
                except NetworkError as error: return error.stage, net.fallback_roles, log.getvalue()
                return None, net.fallback_roles, log.getvalue()

    def test_free_address_of_the_fixed_network_is_taken_probed_and_checked(self):
        stage, roles, log = self.attempt()
        self.assertIsNone(stage); self.assertEqual(roles, {'telescope-mount'})
        self.assertEqual(self.profiles[-1], {'method': 'manual', 'address1': '192.168.4.250/24', 'dad-timeout': 2000,
                                             'route-metric': 600, 'never-default': 'true', 'ignore-auto-dns': 'true'})
        self.assertEqual(self.commands[-1], ['ping', '-c', '2', '-W', '2', '-I', 'wlan2', '192.168.4.1'])
        self.assertNotIn('Synthetic-Mount', log); self.assertIn('telescope-mount gave no address', log)

    def test_address_that_answers_is_left_to_its_owner(self):
        stage, roles, _ = self.attempt(taken={'192.168.4.250/24'})
        self.assertIsNone(stage); self.assertEqual(self.profiles[-1]['address1'], '192.168.4.249/24')
        stage, roles, _ = self.attempt(taken={'192.168.4.250/24', '192.168.4.249/24', '192.168.4.248/24'})
        self.assertEqual((stage, roles), ('ADDRESS_NOT_OBTAINED', set()))

    def test_no_fallback_without_an_answering_mount_a_known_plan_or_the_owner_switch(self):
        for arguments in (dict(silent=True), dict(enabled=False),
                          dict(profile=dict(self.MOUNT, kind='seestar', target_ip='10.0.0.1')),
                          dict(profile=dict(self.MOUNT, target_ip='192.168.9.1')),
                          dict(profile={'ssid': 'Synthetic-Home'}, role='home')):
            stage, roles, log = self.attempt(**arguments)
            self.assertEqual((stage, roles, log), ('ADDRESS_NOT_OBTAINED', set(), ''), arguments)
        # Without the owner switch or a known plan, no address is ever written.
        self.attempt(enabled=False); self.assertEqual([p['method'] for p in self.profiles], ['auto'])

    def test_next_attempt_asks_the_mount_for_an_address_first(self):
        self.attempt()
        self.assertEqual([p['method'] for p in self.profiles], ['auto', 'manual'])


class FailedLinkTests(RadioFixture):
    def setUp(self):
        super().setUp()
        self.facade.action(FIRST, 'disconnect', {}, 'paired-client'); self.tick()
        self.stage = 'ADDRESS_NOT_OBTAINED'
        attempt = self.net.connect

        def staged(role, device, config):
            try: return attempt(role, device, config)
            except NetworkError as error:
                error.stage = self.stage
                raise
        self.net.connect = staged

    def fail_once(self):
        self.absent.add('telescope-' + SECOND)
        code, value = self.connect(); self.assertEqual(code, 202, value)
        self.finish()
        return value['operation']['id']

    def test_failed_request_keeps_its_reason_and_names_the_step(self):
        for self.stage in ('NETWORK_NOT_FOUND', 'NOT_ASSOCIATED', 'ADDRESS_NOT_OBTAINED'):
            operation = self.facade.operation(self.fail_once(), 'home')
            validate(operation, SPEC['components']['schemas']['NativeOperation'], SPEC)
            self.assertEqual((operation['state'], operation['reason'], operation['failure_stage']),
                             ('failed', 'ASSOCIATION_FAILED', self.stage))
            self.assertIn('ASSOCIATION_FAILED', [event.get('reason') for event in self.controller.event_store.events])
        self.stage = None
        operation = self.facade.operation(self.fail_once(), 'home')
        validate(operation, SPEC['components']['schemas']['NativeOperation'], SPEC)
        self.assertEqual(operation['reason'], 'ASSOCIATION_FAILED'); self.assertNotIn('failure_stage', operation)

    def test_asking_again_right_after_a_failure_starts_a_new_attempt(self):
        self.fail_once()
        self.assertEqual(len([role for role, _ in self.attempts if role == 'telescope-' + SECOND]), 1)
        # The instrument was restarted in the meantime: the same request, seconds later, is tried and succeeds.
        self.absent.clear()
        code, value = self.connect(wait_seconds=5)
        self.assertEqual(code, 200, value)
        self.assertTrue(self.row(SECOND)['connected'])
        self.assertEqual(len([role for role, _ in self.attempts if role == 'telescope-' + SECOND]), 2)

    def test_automatic_attempts_still_pause_after_a_failure(self):
        self.fail_once()
        self.controller.protection.leases.clear()  # The app gave up its reservation.
        self.store.config['auto_connect'] = True; self.profile(SECOND)['auto_eligible'] = True
        with self.assertRaises(ApiError) as refused:
            self.ops.ensure(SECOND, '"' + self.api.revision() + '"', self.api.revision, None, 0, internal=True, origin='seestar')
        self.assertEqual(refused.exception.code, 'TARGET_BACKOFF')

    def test_instrument_the_bridge_keeps_retrying_reports_the_step(self):
        code, value = self.connect(FIRST, wait_seconds=5); self.assertEqual(code, 200, value)
        self.lose_first()
        item = self.item(FIRST)
        self.assertEqual((item['failure_stage'], item['retry']['attempts']), ('ADDRESS_NOT_OBTAINED', 1))
        self.absent.clear()
        while not self.row(FIRST)['connected']: self.tick()
        self.assertNotIn('failure_stage', self.item(FIRST))


class AddressLostBetweenReadingsTests(RadioFixture):
    def test_link_without_address_is_waited_for_not_an_internal_error(self):
        self.assertTrue(self.row(FIRST)['connected'])
        address = self.net.address
        device = next(d for d in self.devices if d['mac'] == self.profile(FIRST)['mac'])
        # Still reported as linked, but its address is gone when it is read.
        self.net.address = lambda d, timeout=20: '' if d['mac'] == device['mac'] else address(d, timeout)
        self.tick()
        row = self.row(FIRST)
        self.assertEqual((row['connected'], row['state'], row['failure_stage']), (False, 'waiting', 'ADDRESS_NOT_OBTAINED'))
        self.assertTrue(self.controller.last_cycle_ok)
        self.assertNotIn('RECONCILE_FAILED', [event.get('reason') for event in self.controller.event_store.events])
        self.assertEqual(self.item(FIRST)['failure_stage'], 'ADDRESS_NOT_OBTAINED')
        self.net.address = address
        self.tick()
        self.assertTrue(self.row(FIRST)['connected']); self.assertNotIn('failure_stage', self.row(FIRST))

    def test_address_taken_by_the_bridge_is_said_and_the_owner_can_switch_it_off(self):
        self.net.fallback_roles = {'telescope'}
        self.tick()
        self.assertTrue(self.row(FIRST)['address_fallback']); self.assertTrue(self.item(FIRST)['address_fallback'])
        self.net.fallback_roles = set(); self.tick()
        self.assertNotIn('address_fallback', self.item(FIRST))
        self.net.address_fallback = True
        self.assertNotIn('address_fallback', self.store.config['auto_policy'])
        self.api.update('settings', {'auto_policy': {'address_fallback': False}}, expected='"' + self.api.revision() + '"'); self.tick()
        self.assertFalse(self.net.address_fallback)
        with self.assertRaises(Invalid):
            self.api.update('settings', {'auto_policy': {'address_fallback': 'no'}}, expected='"' + self.api.revision() + '"')

    def test_internal_error_line_says_where_without_the_message(self):
        def broken(*args, **kwargs): raise ValueError('synthetic-private-name')
        output = io.StringIO()
        with patch.object(self.controller, 'cycle', side_effect=broken), \
                patch.object(self.controller.stop, 'wait', side_effect=lambda seconds: self.controller.stop.set()), \
                contextlib.redirect_stdout(output):
            self.controller.loop()
        self.controller.stop.clear()
        line = output.getvalue().strip()
        self.assertRegex(line, r'^wifi-bridge: internal error ValueError at test_link_failure_steps\.py:\d+$')
        self.assertNotIn('synthetic-private-name', line)


if __name__ == '__main__':
    unittest.main()
