"""One bridge password: web interface, hotspot and Linux account, all or nothing, never in clear."""
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_bridge import Fixture
from wifi_bridge.api import ApiError
from wifi_bridge.config import Invalid, Store, hotspot_ssid, password_matches
from wifi_bridge.controller import Controller
from wifi_bridge.network import DemoNetwork, LinuxNetwork, NetworkError
from wifi_bridge.password import DEFAULT, wpa_psk

NEW = 'Orion-nebula-42'


class CredentialTests(Fixture):
    def setUp(self):
        super().setUp();self.configured()

    def change(self, password=NEW, current='local-admin-test-password', target=None):
        payload = {'password': password, 'current_password': current}
        if target is not None: payload['target'] = target
        return self.controller.credentials(payload)

    def assert_unified(self, password):
        config = self.store.config
        self.assertTrue(password_matches(password, config['admin_hash']))
        key = config['hotspot_key']
        self.assertEqual(key['psk'], wpa_psk(password, key['ssid']))
        self.assertNotIn('hotspot_password', config)
        self.assertNotIn(password, self.store.path.read_text())

    def test_one_change_sets_linux_web_and_hotspot(self):
        old_home = copy.deepcopy(self.store.config['home'])
        self.store.config['linux_password_default'] = True
        self.assertTrue(self.controller.status()['default_credentials']['password'])
        with patch.object(self.net, 'linux_password', return_value=None) as linux:
            result = self.change()
        linux.assert_called_once_with(NEW)
        self.assert_unified(NEW)
        self.assertEqual(set(result), {'ok', 'sessions_invalidated', 'hotspot_reconnect'})
        self.assertTrue(result['sessions_invalidated'])
        self.assertEqual(self.store.config['home'], old_home)
        status = self.controller.status()
        self.assertEqual(status['default_credentials'], {'password': False, 'web': False, 'hotspot': False, 'linux': False})
        self.assertNotIn(NEW, json.dumps(status))
        # The key belongs to the name the hotspot uses: that name stays it on every radio.
        for device in self.devices:
            self.assertEqual(hotspot_ssid(self.store.config, device['mac']), self.store.config['hotspot_key']['ssid'])

    def test_linux_failure_changes_nothing(self):
        before = self.store.path.read_bytes()
        with patch.object(self.net, 'linux_password', side_effect=NetworkError('System command failed: chpasswd')):
            with self.assertRaises(NetworkError):
                self.change()
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertTrue(password_matches('local-admin-test-password', self.store.config['admin_hash']))
        self.assertNotIn('hotspot_key', self.store.config)

    def test_failed_configuration_write_puts_the_linux_account_back(self):
        undo = Mock()
        with patch.object(self.net, 'linux_password', return_value=undo), \
                patch.object(self.store, 'save', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.change()
        undo.assert_called_once_with()

    def test_earlier_setup_targets_change_all_three(self):
        for number, target in enumerate(('web', 'hotspot', 'linux', 'all')):
            password = NEW + str(number)
            current = 'local-admin-test-password' if number == 0 else NEW + str(number - 1)
            with self.subTest(target=target), patch.object(self.net, 'linux_password', return_value=None) as linux:
                result = self.change(password, current, target)
                linux.assert_called_once_with(password)
                self.assert_unified(password)
                self.assertEqual(set(result), {'ok', 'sessions_invalidated', 'hotspot_reconnect'})

    def test_rule_and_current_password_are_checked_before_any_change(self):
        cases = [('root', NEW, 'local-admin-test-password'), (None, NEW, 'wrong-current'),
                 (None, 'short', 'local-admin-test-password'), (None, 'é' * 10, 'local-admin-test-password'),
                 (None, 'with a space', 'local-admin-test-password'), (None, 'x' * 64, 'local-admin-test-password'),
                 (None, DEFAULT, 'local-admin-test-password'), (None, 'bad\npassword', 'local-admin-test-password')]
        before = self.store.path.read_bytes()
        for target, password, current in cases:
            with self.subTest(target=target, password=password), patch.object(self.net, 'linux_password') as linux, \
                    self.assertRaises(Invalid) as refused:
                self.change(password, current, target)
            linux.assert_not_called()
            if password != DEFAULT:  # The initial password is public; others never appear in a message.
                self.assertNotIn(password, str(refused.exception))
        self.assertEqual(self.store.path.read_bytes(), before)
        # 63 printable ASCII characters are the longest accepted password.
        with patch.object(self.net, 'linux_password', return_value=None):
            self.change('A!' * 31 + 'z')

    def test_refused_while_a_connection_is_protected(self):
        with patch.object(self.controller.protection, 'protected', return_value=True), \
                patch.object(self.net, 'linux_password') as linux, self.assertRaises(ApiError) as refused:
            self.change()
        self.assertEqual(refused.exception.code, 'CONNECTION_PROTECTED')
        linux.assert_not_called()

    def test_active_hotspot_restarts_with_the_derived_key_only(self):
        self.store.config['telescopes'][0]['enabled']=False
        self.store.config['hotspot']['enabled']=True
        self.controller.cycle(0)
        self.assertTrue(self.controller.state['hotspot']['active'])
        with patch.object(self.net, 'linux_password', return_value=None):
            self.assertTrue(self.change()['hotspot_reconnect'])
        with patch.object(self.net,'hotspot',wraps=self.net.hotspot) as hotspot,patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect:
            self.controller.cycle(5)
        ssid = hotspot.call_args.args[1]
        self.assertEqual(hotspot.call_args.args[2], wpa_psk(NEW, ssid))
        self.assertNotIn(NEW, repr(hotspot.call_args))
        self.assertNotIn('home',[c.args[0] for c in disconnect.call_args_list])

    def test_no_clear_password_in_configuration_logs_or_replies(self):
        from wifi_bridge.api import MobileAPI
        output = io.StringIO()
        self.store.config['telescopes'][0]['enabled']=False
        self.store.config['hotspot']['enabled']=True
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.controller.cycle(0)
            with patch.object(self.net, 'linux_password', return_value=None):
                reply = self.change()
            self.controller.cycle(5)
            api = MobileAPI(self.controller)
            replies = [reply, self.controller.status()] + [api.read(name) for name in ('status', 'config', 'settings', 'hotspot')]
        for path in Path(self.tmp.name).rglob('*'):
            if path.is_file():
                self.assertNotIn(NEW.encode(), path.read_bytes(), path.name)
        self.assertNotIn(NEW, output.getvalue())
        self.assertNotIn(NEW, json.dumps(replies, default=str))

    def test_renaming_the_hotspot_needs_the_password_for_its_key(self):
        with patch.object(self.net, 'linux_password', return_value=None):
            self.change()
        from wifi_bridge.api import MobileAPI
        api = MobileAPI(self.controller)
        save = lambda value: self.controller.save(value, expected='"' + api.revision() + '"', revision=api.revision)
        data = self.controller.status()['config']
        data['hotspot'] = dict(data['hotspot'], ssid='Observatory')
        with self.assertRaises(ApiError) as refused:
            save(copy.deepcopy(data))
        self.assertEqual(refused.exception.code, 'PASSWORD_REQUIRED')
        with self.assertRaises(ApiError):
            save(dict(copy.deepcopy(data), current_password='not-the-password'))
        save(dict(copy.deepcopy(data), current_password=NEW))
        self.assertEqual(self.store.config['hotspot_key'], {'ssid': 'Observatory', 'psk': wpa_psk(NEW, 'Observatory')})
        self.assertNotIn(NEW, self.store.path.read_text())
        # Native clients rename it the same way.
        with self.assertRaises(ApiError) as native:
            api.update('hotspot', {'ssid': 'Field-AP'}, expected='"' + api.revision() + '"')
        self.assertEqual(native.exception.code, 'PASSWORD_REQUIRED')
        api.update('hotspot', {'ssid': 'Field-AP', 'current_password': NEW}, expected='"' + api.revision() + '"')
        self.assertEqual(self.store.config['hotspot_key'], {'ssid': 'Field-AP', 'psk': wpa_psk(NEW, 'Field-AP')})

    def test_first_administrator_password_in_a_settings_save_is_the_single_password(self):
        data = self.controller.status()['config']
        with patch.object(self.net, 'linux_password', return_value=None) as linux:
            result = self.controller.save(dict(copy.deepcopy(data), admin_password=NEW))
        linux.assert_called_once_with(NEW)
        self.assertTrue(result['sessions_invalidated'])
        self.assert_unified(NEW)
        with self.assertRaises(Invalid):
            self.controller.save(dict(copy.deepcopy(data), admin_password='accentué-mot'))

    def test_linux_request_private_and_removed_on_failure(self):
        net=LinuxNetwork(self.tmp.name)
        def command(*args,**kwargs):
            pending=Path(self.tmp.name,'linux-password.pending')
            self.assertEqual(pending.stat().st_mode&0o777,0o600)
            self.assertEqual(pending.read_text(),'12345678')
            self.assertNotIn('12345678',str(args))
            raise RuntimeError('simulated helper failure')
        with patch('wifi_bridge.network.run',side_effect=command),patch('wifi_bridge.runtime.pi5_appliance',return_value=False),self.assertRaises(RuntimeError):
            net.linux_password('12345678')
        self.assertFalse(Path(self.tmp.name,'linux-password.pending').exists())
