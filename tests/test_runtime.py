"""Non-appliance installs cannot change host credentials or reboot the host."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from wifi_bridge.runtime import capabilities, platform
from wifi_bridge.updates import Updates
from test_bridge import Fixture


class DeploymentTests(Fixture):
    def test_native_and_container_host_actions_are_guarded(self):
        self.controller.demo=False
        for name in ('native','docker'):
            with patch.dict(os.environ,{'ASTRO_BRIDGE_PLATFORM':name}):
                self.assertFalse(capabilities()['host_reboot'])
                self.assertFalse(capabilities()['linux_credentials'])
                self.assertFalse(Updates(self.tmp.name).supported())
                with patch.object(self.net,'linux_password') as password,patch.object(self.net,'reboot') as reboot:
                    with self.assertRaises(ValueError):self.controller.credentials({'target':'linux','password':'valid-password','current_password':'bootstrap-test-code'})
                    with self.assertRaises(ValueError):self.controller.retry()
                    password.assert_not_called();reboot.assert_not_called()
                self.assertIsNone(self.controller.action_at)

    def test_unknown_platform_fails_closed_and_demo_actions_are_simulated(self):
        with patch.dict(os.environ,{'ASTRO_BRIDGE_PLATFORM':'arbitrary'}):
            with self.assertRaises(ValueError):platform()
        with patch.dict(os.environ,{'ASTRO_BRIDGE_PLATFORM':'docker'}):
            self.assertTrue(capabilities(demo=True)['host_reboot'])
            self.assertEqual(capabilities(demo=True)['platform'],'demo')

    def test_extra_http_authority_does_not_authorize_a_foreign_origin(self):
        import http.client
        import json
        import threading
        from wifi_bridge.web import WebServer
        self.controller.demo=False
        self.controller.management_hosts=frozenset({'management.example'})
        with patch.dict(os.environ,{'ASTRO_BRIDGE_PLATFORM':'native'}):
            server=WebServer(('127.0.0.1',0),self.controller)
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            try:
                client=http.client.HTTPConnection('127.0.0.1',server.server_port)
                authority='management.example:'+str(server.server_port)
                headers={'Host':authority,'Content-Type':'application/json','Origin':'http://evil.example'}
                body=json.dumps({'password':'bootstrap-test-code'})
                client.request('POST','/api/login',body,headers);response=client.getresponse();self.assertEqual(response.status,403);response.read()
                headers['Origin']='http://'+authority
                client.request('POST','/api/login',body,headers);response=client.getresponse();self.assertEqual(response.status,200);response.read()
                client.close()
            finally:
                server.shutdown();server.server_close();worker.join(timeout=2)

    def test_legacy_config_does_not_schedule_a_native_host_reboot(self):
        from test_bridge import payload
        self.controller.demo=False
        with patch.dict(os.environ,{'ASTRO_BRIDGE_PLATFORM':'native'}):
            result=self.controller.save(payload(self.devices))
            self.assertEqual(result['reboot_in'],0)
            self.assertIsNone(self.controller.action_at)
