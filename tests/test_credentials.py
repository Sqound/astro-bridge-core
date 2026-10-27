"""Credential independence, fixed Linux account and private persistence."""
import copy
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.config import Invalid, password_matches, validate_config
from wifi_bridge.network import LinuxNetwork


class CredentialTests(Fixture):
    def setUp(self):
        super().setUp();self.configured()

    def change(self, target, password='12345678', current='local-admin-test-password'):
        return self.controller.credentials({'target':target,'password':password,'current_password':current})

    def test_independent_passwords_and_redaction(self):
        old_home=copy.deepcopy(self.store.config['home'])
        self.change('hotspot','new-hotspot-secret')
        with patch.object(self.net,'linux_password') as change:
            self.change('linux','new-linux-secret')
        change.assert_called_once_with('new-linux-secret')
        self.assertNotIn('new-linux-secret',self.store.path.read_text())
        self.assertTrue(self.change('web')['sessions_invalidated'])
        self.assertTrue(password_matches('12345678',self.store.config['admin_hash']))
        status=json.dumps(self.controller.status())
        self.assertNotIn('new-hotspot-secret',status)
        self.assertEqual(self.store.config['home'],old_home)
        value=validate_config(self.controller.status()['config'],self.store.config,self.devices)
        self.assertEqual(value['hotspot_password'],'new-hotspot-secret')

    def test_current_password_and_validation_before_side_effects(self):
        for target,password,current in [('root','12345678','local-admin-test-password'),('linux','12345678','wrong'),('linux','bad\npassword','local-admin-test-password'),('hotspot','é'*40,'local-admin-test-password')]:
            with self.subTest(target=target,password=password),patch.object(self.net,'linux_password') as change,self.assertRaises(Invalid):
                self.change(target,password,current)
            change.assert_not_called()

    def test_active_ap_rekeys_without_touching_home(self):
        self.store.config['telescopes'][0]['enabled']=False
        self.store.config['hotspot']['enabled']=True
        self.controller.cycle(0)
        self.change('hotspot','new-hotspot-secret')
        with patch.object(self.net,'hotspot',wraps=self.net.hotspot) as hotspot,patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect:
            self.controller.cycle(5)
        self.assertEqual(hotspot.call_args.args[2],'new-hotspot-secret')
        self.assertNotIn('home',[c.args[0] for c in disconnect.call_args_list])

    def test_linux_request_private_and_removed_on_failure(self):
        net=LinuxNetwork(self.tmp.name)
        def command(*args,**kwargs):
            pending=Path(self.tmp.name,'linux-password.pending')
            self.assertEqual(pending.stat().st_mode&0o777,0o600)
            self.assertEqual(pending.read_text(),'12345678')
            self.assertNotIn('12345678',str(args))
            raise RuntimeError('simulated helper failure')
        with patch('wifi_bridge.network.run',side_effect=command),self.assertRaises(RuntimeError):
            net.linux_password('12345678')
        self.assertFalse(Path(self.tmp.name,'linux-password.pending').exists())
