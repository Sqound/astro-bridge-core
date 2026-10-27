"""Reservations/owner protection use real config/controller, never host networking."""
import copy
from unittest.mock import patch
from test_bridge import Fixture
from wifi_bridge.api import MobileAPI, ApiError
from wifi_bridge.config import Invalid, validate_config
from wifi_bridge.protection import ConnectionProtection


class ProtectionTests(Fixture):
    def setUp(self):
        super().setUp();self.configured();self.controller.cycle(0)
        self.clock=[100.];self.protection=ConnectionProtection(self.controller,lambda:self.clock[0]);self.controller.protection=self.protection
        self.api=MobileAPI(self.controller)

    def revision(self):return '"'+self.api.revision()+'"'
    def lease(self,client='opaque-client',ident='primary',**data):
        return self.protection.create(ident,data,client,self.revision(),self.api.revision)
    def lock(self,locked=True):
        return self.protection.owner_lock('primary',{'locked':locked,'current_password':'local-admin-test-password'},self.revision(),self.api.revision)

    def test_expiry_renew_versions_and_auth_revocation_does_not_end_protection(self):
        lease=self.lease();self.assertTrue(lease['owned']);self.assertEqual(lease['remaining_seconds'],120)
        self.controller.operations.retain_clients(set())  # Actual shared auth pruning has no lease side effect.
        self.assertTrue(self.protection.protected('primary'));self.assertFalse(self.protection.status('other')['leases'][0]['owned'])
        newer=self.protection.change(lease['id'],{'version':1,'ttl_seconds':900},'opaque-client');self.assertEqual(newer['version'],2)
        with self.assertRaises(ApiError) as error:self.protection.change(lease['id'],{'version':1},'opaque-client',release=True)
        self.assertEqual(error.exception.code,'LEASE_VERSION_CHANGED')
        self.clock[0]+=899;self.assertTrue(self.protection.protected());self.clock[0]+=1;self.assertFalse(self.protection.protected())
        with self.assertRaises(ApiError):self.protection.change(lease['id'],{'version':2},'opaque-client',release=True)

    def test_foreign_session_cannot_release_owner_requires_exact_fresh_view_and_reauth(self):
        lease=self.lease()
        with self.assertRaises(ApiError):self.protection.change(lease['id'],{'version':1},'foreign',release=True)
        data={'version':1,'current_password':'local-admin-test-password','confirm_release':True}
        for bad in ('*','"stale"',None):
            with self.assertRaises(ApiError):self.protection.change(lease['id'],data,'foreign',release=True,owner=True,expected=bad,revision=self.api.revision)
        self.protection.change(lease['id'],{'version':1},'opaque-client')
        with self.assertRaises(ApiError) as error:self.protection.change(lease['id'],data,'foreign',release=True,owner=True,expected=self.revision(),revision=self.api.revision)
        self.assertEqual(error.exception.code,'LEASE_VERSION_CHANGED');data['version']=2
        self.protection.change(lease['id'],data,'foreign',release=True,owner=True,expected=self.revision(),revision=self.api.revision)
        self.assertFalse(self.protection.protected())

    def test_persistent_owner_lock_metadata_omission_and_no_connection_events(self):
        sequence=self.controller.event_store.sequence
        with patch.object(self.net,'connect',wraps=self.net.connect) as connect:
            self.lock();self.controller.cycle(5);connect.assert_not_called()
        self.assertEqual(self.controller.event_store.sequence,sequence);self.assertFalse(self.controller.update_safe())
        data=copy.deepcopy(self.store.config);data['telescopes'][0].pop('owner_locked')
        self.assertTrue(validate_config(data,self.store.config,self.devices)['telescopes'][0]['owner_locked'])
        data['telescopes'][0]['owner_locked']=False
        with self.assertRaises(Invalid):validate_config(data,self.store.config,self.devices)
        self.controller.protection=ConnectionProtection(self.controller);self.assertTrue(self.controller.protection.protected())
        self.protection=self.controller.protection;self.lock(False);self.assertFalse(self.protection.protected())

    def test_direct_and_indirect_mutations_are_protected_safe_metadata_and_reads_work(self):
        self.lease()
        actions=[lambda:self.api.action('primary','disconnect'),lambda:self.api.action('primary','delete'),lambda:self.controller.retry(),
                 lambda:self.api.update('settings',{'country':'FR'}),lambda:self.api.update('home',{'hidden':True}),lambda:self.api.update('hotspot',{'enabled':True}),
                 lambda:self.controller.credentials({'target':'web','current_password':'local-admin-test-password','password':'replacement-password'}),
                 lambda:self.api.binding('primary',{},self.revision(),unlink=True)]
        for action in actions:
            with self.assertRaises(ApiError):action()
        self.api.update('telescopes',{'auto_eligible':True},ident='primary');self.assertTrue(self.api.read('status')['telescopes'][0]['relay_ready'])
        self.api.update('telescopes',{'id':'other','name':'Other','ssid':'Other','password':'synthetic-pass','mac':self.devices[0]['mac'],'target_ip':'10.2.0.1'},create=True)
        with self.assertRaises(ApiError):self.api.action('other','connect')
        code,value=self.controller.operations.ensure('primary',None,self.api.revision,'opaque-client',1000);self.assertEqual(code,200)
        self.assertTrue(self.protection.protected());self.assertFalse(self.controller.update_safe())

    def test_invalid_fields_bounds_pending_and_reauth_rate_limit(self):
        for ttl in (True,0,29,901,'120',None):
            with self.assertRaises(ApiError):self.lease(ttl_seconds=ttl)
        with self.assertRaises(ApiError):self.lease(extra=True)
        self.controller.operations.active='pending'
        with self.assertRaises(ApiError):self.lease()
        self.controller.operations.active=None
        for i in range(32):self.lease('client-'+str(i))
        with self.assertRaises(ApiError) as error:self.lease('overflow')
        self.assertEqual(error.exception.code,'LEASE_LIMITED')
        for i in range(5):
            with self.assertRaises(ApiError):self.protection.owner_lock('primary',{'locked':True,'current_password':'wrong'},self.revision(),self.api.revision)
        with self.assertRaises(ApiError) as error:self.lock()
        self.assertEqual(error.exception.code,'REAUTHENTICATION_LIMITED')

    def test_restart_loses_ram_leases_but_owner_lock_remains_and_emergency_ap_never_takes_it(self):
        self.lease();self.lock();self.controller.protection=ConnectionProtection(self.controller)
        self.assertEqual(self.controller.protection.status()['leases'],[]);self.assertTrue(self.controller.protection.protected())
        with patch.object(self.net,'hotspot',wraps=self.net.hotspot) as hotspot:
            self.controller._ap([self.devices[0]],{self.devices[0]['mac']},emergency=True);hotspot.assert_not_called()


    def test_optional_ap_transition_is_deferred_but_emergency_uses_unprotected_free_radio(self):
        self.api.update('hotspot',{'enabled':True});self.controller.cycle(5);self.assertFalse(self.controller.state['hotspot']['active'])
        self.lease();self.net.radios=3
        with patch.object(self.net,'hotspot',wraps=self.net.hotspot) as hotspot,patch.object(self.net,'firewall',wraps=self.net.firewall) as firewall:
            self.controller.cycle(10);hotspot.assert_not_called()
        self.assertEqual(self.controller.state['hotspot']['reason'],'connection_protected')
        for call in firewall.call_args_list:self.assertTrue(any(r['target']=='10.0.0.1' for r in call.kwargs.get('telescopes',[])))
        self.lock();self.net.disconnect('home')
        with patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect:
            self.controller.setup(self.net.devices(),'Synthetic home unavailable')
        self.assertTrue(self.controller.state['hotspot']['active']);self.assertNotEqual(self.controller.state['hotspot']['mac'],self.devices[0]['mac'])
        self.assertTrue(self.controller.state['setup_ssid'].endswith(self.controller.state['hotspot']['mac'].replace(':','')[-6:]));self.assertTrue(self.net.connected('telescope',self.devices[0]));self.assertNotIn('telescope',[call.args[0] for call in disconnect.call_args_list])
        self.controller._ap([self.devices[0]],{self.devices[0]['mac']},emergency=True)
        self.assertEqual(self.controller.state['hotspot']['reason'],'protected_no_recovery_radio')


    def test_binding_replacement_cannot_indirectly_unlink_protected_conflicting_profile(self):
        alias={'ssid':self.store.config['telescopes'][0]['ssid'],'bssid':'02:00:00:77:88:99'}
        data={'owner_confirmed':True,'aliases':[alias]}
        self.api.binding('primary',data,self.revision())
        self.api.update('telescopes',{'id':'other','name':'Other','ssid':alias['ssid'],'password':'synthetic-pass','target_ip':'10.2.0.1'},create=True)
        self.lock();before=copy.deepcopy(self.store.config);sequence=self.controller.event_store.sequence
        correction=dict(data,confirm_replace=True,conflicting_profile_ids=['primary'])
        with patch.object(self.net,'connect') as connect,patch.object(self.net,'disconnect') as disconnect,patch.object(self.net,'firewall') as firewall:
            with self.assertRaises(ApiError) as error:self.api.binding('other',correction,self.revision())
            self.assertEqual(error.exception.code,'CONNECTION_PROTECTED');connect.assert_not_called();disconnect.assert_not_called();firewall.assert_not_called()
        self.assertEqual(self.store.config,before);self.assertEqual(self.controller.event_store.sequence,sequence)
        self.lock(False);self.api.binding('other',correction,self.revision())
        self.assertNotIn('physical_binding',self.store.config['telescopes'][0]);self.assertEqual(self.store.config['telescopes'][1]['physical_binding']['aliases'][0]['bssid'],alias['bssid'])
        self.assertEqual(self.controller.event_store.sequence,sequence)
