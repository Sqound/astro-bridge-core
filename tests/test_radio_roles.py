"""Persistent adapter purposes and restart/availability boundaries."""
import copy
from unittest.mock import patch
from test_bridge import Fixture
from wifi_bridge.api import MobileAPI, ApiError
from wifi_bridge.config import Invalid, Store, validate_config, reserved_hotspot_mac
from wifi_bridge.controller import Controller
from wifi_bridge.radio_roles import interface_roles


class RadioRoleTests(Fixture):
    def setUp(self):
        super().setUp()
        self.net.radios = 4
        self.devices = self.net.devices()
        self.controller.update(devices=self.devices)
        self.configured()
        self.api = MobileAPI(self.controller)

    def save_roles(self, roles):
        return self.api.update('settings', {'interface_roles': roles}, expected='"'+self.api.revision()+'"')

    def roles(self):
        return {self.devices[0]['mac']: 'telescope', self.devices[1]['mac']: 'home',
                self.devices[2]['mac']: 'hotspot', self.devices[3]['mac']: 'auto'}

    def test_roles_persist_and_hotspot_disabled_still_reserved(self):
        self.save_roles(self.roles())
        ap_mac = self.devices[2]['mac']
        self.api.update('hotspot', {'enabled': False}, expected='"'+self.api.revision()+'"')
        self.assertEqual(reserved_hotspot_mac(self.store.config), ap_mac)
        self.assertEqual(Store(self.tmp.name,self.devices[0]['mac']).config['interface_roles'], self.roles())
        info = next(d for d in self.api.read('interfaces')['interfaces'] if d['mac'] == ap_mac)
        self.assertEqual(info['configured_role'], 'hotspot')
        self.assertFalse(info['can_assign_telescope'])
        with self.assertRaises(ApiError):
            self.controller.telescope_action({'id':'primary','action':'connect','mac':ap_mac})

    def test_default_setup_reserves_and_restarts_hotspot(self):
        from wifi_bridge.config import default_config
        self.store.save(default_config(self.devices[0]['mac']))
        self.controller = Controller(self.store,self.net,{'password':'bootstrap-test-code'},demo=True)
        self.controller.cycle(0)
        ap_mac=self.controller.state['hotspot']['mac']
        self.assertEqual(interface_roles(self.store.config)[ap_mac],'hotspot')
        self.assertTrue(self.store.config['hotspot']['enabled'])
        self.net.disconnect('setup')
        reopened=Store(self.tmp.name,self.devices[0]['mac'])
        restarted=Controller(reopened,self.net,{'password':'bootstrap-test-code'},demo=True)
        restarted.cycle(20)
        self.assertTrue(restarted.state['hotspot']['active'])
        self.assertEqual(restarted.state['hotspot']['mac'],ap_mac)
        info=next(d for d in MobileAPI(restarted).read('interfaces')['interfaces'] if d['mac']==ap_mac)
        self.assertFalse(info['can_assign_telescope'])

    def test_missing_reserved_hotspot_is_not_given_to_telescopes(self):
        self.save_roles(self.roles());self.controller.cycle(0)
        absent=self.devices[2]['mac'];self.net.disconnect('setup')
        with patch.object(self.net,'devices',return_value=[d for d in self.devices if d['mac']!=absent]):
            self.controller.cycle(20)
        self.assertTrue(self.controller.state['hotspot']['active'])
        self.assertEqual(self.controller.state['hotspot']['mac'],self.devices[3]['mac'])
        self.assertEqual(reserved_hotspot_mac(self.store.config),absent)
        self.assertEqual(self.store.config['interface_roles'][absent],'hotspot')

    def test_move_hotspot_updates_policy_and_service_without_moving_instrument(self):
        self.save_roles(self.roles());self.controller.cycle(0)
        association=self.net.associations['telescope']
        new=self.roles();new[self.devices[2]['mac']]='auto';new[self.devices[3]['mac']]='hotspot'
        self.save_roles(new);self.controller.cycle(20)
        self.assertEqual(self.controller.state['hotspot']['mac'],self.devices[3]['mac'])
        self.assertEqual(self.net.associations['telescope'],association)

    def test_release_role_for_instrument_requires_explicit_change(self):
        self.save_roles(self.roles());self.controller.cycle(0)
        self.api.update('hotspot',{'enabled':False},expected='"'+self.api.revision()+'"');self.controller.cycle(20)
        new=self.roles();new[self.devices[2]['mac']]='telescope'
        self.save_roles(new);self.controller.cycle(40)
        self.assertFalse(reserved_hotspot_mac(self.store.config))
        self.assertEqual(self.store.config['interface_roles'][self.devices[2]['mac']],'telescope')

    def test_invalid_duplicate_and_unsupported_roles_leave_configuration_unchanged(self):
        before=copy.deepcopy(self.store.config)
        bad=[{self.devices[2]['mac']:'invalid'}, {self.devices[2]['mac']:'hotspot',self.devices[3]['mac']:'hotspot'},
             {self.devices[2]['mac']:True}, {'00:00:00:00:00:01':'home'}, []]
        for roles in bad:
            with self.subTest(roles=roles),self.assertRaises(Invalid):self.save_roles(roles)
            self.assertEqual(self.store.config,before)
        with patch.dict(self.devices[2],ap=False),self.assertRaises(Invalid):self.save_roles(self.roles())

    def test_role_change_cannot_take_over_connected_mount_and_requires_revision(self):
        self.controller.cycle(0)
        before=copy.deepcopy(self.store.config)
        roles=self.roles();roles[self.devices[0]['mac']]='hotspot';roles[self.devices[2]['mac']]='auto'
        with self.assertRaises(Invalid):self.save_roles(roles)
        self.assertEqual(self.store.config,before)
        with self.assertRaises(ApiError) as error:self.api.update('settings',{'interface_roles':self.roles()})
        self.assertEqual(error.exception.code,'PRECONDITION_REQUIRED')

    def test_network_specific_controls_keep_role_policy_in_sync(self):
        self.save_roles(self.roles())
        self.api.update('hotspot', {'mac':self.devices[3]['mac']},expected='"'+self.api.revision()+'"')
        self.assertEqual(self.store.config['interface_roles'][self.devices[3]['mac']],'hotspot')
        self.assertEqual(self.store.config['interface_roles'][self.devices[2]['mac']],'auto')

    def test_home_role_can_reuse_an_inactive_saved_instrument_assignment(self):
        self.api.action('primary','disconnect');self.controller.cycle(0)
        roles=self.roles();roles[self.devices[0]['mac']]='home';roles[self.devices[1]['mac']]='auto'
        self.save_roles(roles)
        self.assertEqual(self.store.config['home']['mac'],self.devices[0]['mac'])
        self.assertFalse(self.store.config['telescopes'][0]['enabled'])

    def test_failed_initial_hotspot_start_keeps_its_default_reservation(self):
        from wifi_bridge.config import default_config
        from wifi_bridge.network import NetworkError
        self.store.save(default_config(self.devices[0]['mac']))
        fresh=Controller(self.store,self.net,{'password':'bootstrap-test-code'},demo=True)
        with patch.object(self.net,'hotspot',side_effect=NetworkError('synthetic hotspot failure')):
            fresh.cycle(0)
        self.assertFalse(fresh.state['hotspot']['active'])
        self.assertEqual(fresh.state['hotspot']['reason'],'start_failed')
        ap_mac=reserved_hotspot_mac(self.store.config)
        self.assertTrue(ap_mac)
        self.assertEqual(interface_roles(Store(self.tmp.name,self.devices[0]['mac']).config)[ap_mac],'hotspot')

    def test_automatic_connection_never_allocates_disabled_hotspot_adapter(self):
        import time
        self.save_roles(self.roles());self.controller.cycle(0)
        self.api.update('hotspot',{'enabled':False},expected='"'+self.api.revision()+'"');self.controller.cycle(20)
        self.api.update('settings',{'auto_connect':True})
        self.api.update('telescopes',{'id':'auto-scope','name':'Automatic scope','kind':'generic','target_ip':'10.2.0.1',
            'ssid':'Telescope-DEMO','password':'synthetic-test-pass','auto_eligible':True},create=True)
        ops=self.controller.operations
        _,result=ops.ensure('auto-scope','"'+self.api.revision()+'"',self.api.revision,'opaque-test-client',time.monotonic()+3600)
        ops.thread.join(10);self.assertFalse(ops.thread.is_alive())
        self.assertEqual(ops.read(result['id'])['state'],'succeeded')
        chosen=next(p for p in self.store.config['telescopes'] if p['id']=='auto-scope')
        self.assertEqual(chosen['mac'],self.devices[3]['mac'])
