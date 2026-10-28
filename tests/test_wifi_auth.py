"""Credential rejection must stop retries without mislabeling absent equipment."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.api import ApiError, MobileAPI
from wifi_bridge.auth_failures import AuthenticationFailures
from wifi_bridge.config import profiles
from wifi_bridge.network import LinuxNetwork, NetworkAuthenticationError, NetworkError, run


class CredentialHoldTests(unittest.TestCase):
    def test_same_boot_survives_service_restart_and_only_credentials_or_reboot_release(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = dict(id='scope', ssid='Synthetic-Scope', password='private-test-pass', bssid='')
            holds = AuthenticationFailures(root, 'boot-one'); holds.hold(profile)
            self.assertTrue(AuthenticationFailures(root, 'boot-one').held(profile))
            for change in ({'name': 'New name'}, {'enabled': False}, {'mac': '02:00:00:00:00:01'},
                           {'bssid': '02:00:00:00:00:02'}, {'target_ip': '10.0.0.2'}):
                self.assertTrue(holds.held(dict(profile, **change)))
            self.assertFalse(AuthenticationFailures(root, 'boot-two').held(profile))
            self.assertFalse(holds.held(dict(profile, password='new-test-password')))
            self.assertFalse(holds.held(dict(profile, ssid='Another-Scope')))
            self.assertNotIn(profile['password'], holds.path.read_text())
            self.assertNotIn(profile['ssid'], holds.path.read_text())
            self.assertEqual(holds.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(holds.prune([dict(profile, password='new-test-password')]), {'scope'})
            self.assertFalse(AuthenticationFailures(root, 'boot-one').held(profile))

    def test_unwritable_storage_still_holds_in_the_running_process(self):
        with tempfile.TemporaryDirectory() as folder:
            holds = AuthenticationFailures(Path(folder), 'boot-one')
            profile = dict(id='scope', ssid='Synthetic', password='synthetic-password')
            with patch('wifi_bridge.auth_failures.atomic_json', side_effect=OSError): holds.hold(profile)
            self.assertTrue(holds.held(profile))


class NetworkAuthenticationTests(unittest.TestCase):
    def test_only_explicit_nm_auth_reason_is_classified_and_raw_output_is_not_exposed(self):
        args = ['nmcli', '--wait', '35', 'connection', 'up', 'id', 'wifi-bridge-telescope']
        for reason, expected in [(7, NetworkAuthenticationError), (11, NetworkError), (53, NetworkError)]:
            result = subprocess.CompletedProcess(args, 4, stdout='', stderr=f'Error: Connection activation failed: ({reason}) synthetic-private-detail\n')
            with patch('wifi_bridge.network.subprocess.run', return_value=result):
                with self.assertRaises(expected) as caught: run(args)
                self.assertEqual(type(caught.exception), expected)
                self.assertNotIn('synthetic-private-detail', str(caught.exception))

    def test_failed_activation_has_bounded_auth_and_cancels_only_its_owned_role(self):
        with tempfile.TemporaryDirectory() as folder:
            net = LinuxNetwork(folder); device = dict(name='wlan0', mac='02:00:00:00:00:01')
            profile = dict(ssid='Synthetic-Scope', password='private-test-pass')
            def command(args, **kwargs):
                if 'up' in args: raise NetworkError('Timed out')
                if 'GENERAL.REASON' in args: return '7 (No secrets were provided)'
                return ''
            with patch.object(net, '_profile', return_value='wifi-bridge-telescope') as write, patch('wifi_bridge.network.run', side_effect=command), patch.object(net, 'disconnect') as cancel:
                with self.assertRaises(NetworkAuthenticationError): net.connect('telescope', device, profile)
                self.assertEqual(write.call_args.args[1]['connection']['auth-retries'], 1)
                cancel.assert_called_once_with('telescope')

    def test_absent_network_or_supplicant_timeout_is_not_a_password_rejection(self):
        for reason in ('53 (The Wi-Fi network could not be found)', '11 (Supplicant timeout)', '0 (No reason)'):
            with tempfile.TemporaryDirectory() as folder:
                net = LinuxNetwork(folder)
                def command(args, **kwargs):
                    if 'up' in args: raise NetworkError('Activation failed')
                    return reason
                with patch.object(net, '_profile', return_value='owned-profile'), patch('wifi_bridge.network.run', side_effect=command), patch.object(net, 'disconnect') as cancel:
                    with self.assertRaises(NetworkError) as caught: net.connect('telescope', {'name':'wlan0', 'mac':'02:00:00:00:00:01'}, {'ssid':'Synthetic', 'password':'test-password'})
                    self.assertNotIsInstance(caught.exception, NetworkAuthenticationError)
                    cancel.assert_called_once_with('telescope')


class ControllerAuthenticationTests(Fixture):
    def fail_scope(self, exception=None):
        original = self.net.connect
        def connect(role, *args):
            if role == 'telescope': raise exception or NetworkAuthenticationError()
            return original(role, *args)
        return patch.object(self.net, 'connect', side_effect=connect)

    def test_rejection_latches_one_failure_and_manual_reconnect_cannot_bypass_it(self):
        self.configured()
        with self.fail_scope() as attempts:
            for now in (0, 20, 60, 3600): self.controller.cycle(now)
        self.assertEqual(sum(c.args[0]=='telescope' for c in attempts.call_args_list), 1)
        row = self.controller.status()['telescopes'][0]
        self.assertEqual(row['state'], 'authentication_failed'); self.assertTrue(row['retry_blocked'])
        self.assertTrue(self.controller.status()['home_ip'])
        events = [e for e in self.controller.event_store.events if e['reason']=='WIFI_AUTHENTICATION_FAILED']
        self.assertEqual(len(events), 1); self.assertEqual(events[0]['type'], 'failed')
        with self.assertRaises(ApiError) as error: self.controller.telescope_action({'id':'primary', 'action':'connect'})
        self.assertEqual(error.exception.code, 'WIFI_AUTHENTICATION_FAILED')

    def test_password_change_releases_immediately_but_rename_and_same_password_do_not(self):
        self.configured(); api = MobileAPI(self.controller)
        with self.fail_scope(): self.controller.cycle(0)
        secret = profiles(self.store.config)[0]['password']
        api.update('telescopes', {'name':'New name', 'password':secret}, ident='primary')
        self.controller.cycle(2)
        self.assertTrue(self.controller.state['telescopes'][0]['retry_blocked'])
        self.controller.operations.negative['primary'] = 1e12
        api.update('telescopes', {'password':'new-synthetic-password'}, ident='primary')
        self.assertNotIn('primary', self.controller.operations.negative)
        self.controller.cycle(3)
        self.assertTrue(self.controller.state['telescopes'][0]['connected'])

    def test_unavailable_telescope_keeps_automatic_reconnection(self):
        self.configured()
        with self.fail_scope(NetworkError('No network found')) as attempts:
            self.controller.cycle(0); self.controller.cycle(20)
        self.assertEqual(sum(c.args[0]=='telescope' for c in attempts.call_args_list), 2)
        self.assertFalse(self.controller.state['telescopes'][0].get('retry_blocked'))
        self.controller.cycle(40)
        self.assertTrue(self.controller.state['telescopes'][0]['connected'])
