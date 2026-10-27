"""Actual controller management paths in an explicit synthetic network backend."""
import copy
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.network import LinuxNetwork,NetworkError


class FieldTests(Fixture):
    def setUp(self):
        super().setUp();self.net.radios=3;self.devices=self.net.devices();self.controller.update(devices=self.devices)
        self.configured();self.controller.cycle(0)

    def lose_home(self):
        self.net.disconnect('home');original=self.net.connect
        def connect(role,*args):
            if role=='home':raise NetworkError('synthetic unavailable home')
            return original(role,*args)
        return patch.object(self.net,'connect',side_effect=connect)

    def test_home_loss_keeps_telescope_forwarding_and_protection_via_spare_ap(self):
        c=self.controller;before=c.event_store.sequence
        c.store.config['telescopes'][0]['owner_locked']=True
        with self.lose_home(),patch.object(self.net,'firewall',wraps=self.net.firewall) as firewall:
            c.cycle(50)
        self.assertEqual(c.state['management']['mode'],'ap_only');self.assertEqual(c.state['home_ip'],'')
        self.assertEqual(c.state['hotspot']['mac'],self.devices[2]['mac']);self.assertTrue(c.state['telescopes'][0]['relay_ready'])
        self.assertTrue(self.net.connected('telescope',self.devices[0]));self.assertTrue(c.protection.protected('primary'))
        self.assertFalse(any(e['type']=='disconnected' and e['profile_id']=='primary' for e in c.event_store.events if e['sequence']>before))
        self.assertTrue(firewall.call_args.kwargs['setup']);self.assertTrue(firewall.call_args.kwargs['telescopes'])

    def test_returning_home_retains_ap_clients_and_shared_proxy_owner(self):
        c=self.controller
        with self.lose_home():c.cycle(50)
        ap=c.state['hotspot']['mac'];before=c.event_store.sequence
        with patch.object(self.net,'hotspot',wraps=self.net.hotspot) as hotspot:c.cycle(100)
        self.assertEqual(c.state['management']['mode'],'dual');self.assertEqual(c.state['hotspot']['mac'],ap);hotspot.assert_not_called()
        self.assertTrue(c.state['telescopes'][0]['relay_ready']);self.assertEqual(len(c.state['telescopes'][0]['management_paths']),2)
        self.assertFalse(any(e['type']=='disconnected' and e['profile_id']=='primary' for e in c.event_store.events if e['sequence']>before))
        with patch.object(self.net,'ap_vacant',return_value=True):c.cycle(105)
        self.assertEqual(c.state['management']['mode'],'home_only')

    def test_same_radio_home_retry_requires_current_vacancy_and_real_access_interval(self):
        c=self.controller;self.net.radios=2;self.controller.update(devices=self.net.devices())
        clock=[50.]
        with self.lose_home():c.cycle(50)
        self.assertEqual(c.state['hotspot']['mac'],self.devices[1]['mac'])
        with patch.object(self.net,'connect',wraps=self.net.connect) as connect:c.cycle(100)
        self.assertFalse(any(call.args[0]=='home' for call in connect.call_args_list));self.assertEqual(c.state['management']['home_retry']['reason'],'HOME_RETRY_DEFERRED_AP_RESERVED')
        original=self.net.connect
        def slow(role,*args):
            if role=='home':clock[0]+=35;raise NetworkError('synthetic home timeout')
            return original(role,*args)
        clock[0]=100.
        with patch('wifi_bridge.controller.time.monotonic',side_effect=lambda:clock[0]),patch.object(self.net,'ap_vacant',return_value=True),patch.object(self.net,'connect',side_effect=slow) as connect:
            c.cycle(100);self.assertEqual(c.next_home_attempt,180);self.assertTrue(c.state['hotspot']['active'])
            c.cycle(145);c.cycle(179);self.assertEqual(sum(call.args[0]=='home' for call in connect.call_args_list),1)
        self.assertTrue(self.net.connected('telescope',self.devices[0]))

    def test_raw_vacancy_does_not_use_filtered_display_or_dhcp(self):
        net=LinuxNetwork();device={'name':'ap0'}
        with patch.object(net,'connected',return_value=True):
            for raw in ('Station 02:00:00:00:00:01 (on ap0)\n\tauthorized: no\n','malformed nonempty output'):
                with patch('wifi_bridge.network.run',return_value=raw):self.assertFalse(net.ap_vacant(device))
            with patch('wifi_bridge.network.run',return_value=''):self.assertTrue(net.ap_vacant(device))
            with patch('wifi_bridge.network.run',side_effect=NetworkError('unavailable')):self.assertFalse(net.ap_vacant(device))

    def test_dual_firewall_allows_only_management_instrument_paths_before_isolation(self):
        net=LinuxNetwork();addresses={'home':'192.168.1.2/24','ap':'192.168.77.1/24','scope':'10.0.0.2/24'}
        with patch.object(net,'address',side_effect=lambda d:addresses[d['name']]),patch.object(net,'configure_activity'),patch('wifi_bridge.network.run') as run:
            net.firewall(home={'name':'home'},setup={'name':'ap'},telescopes=[{'profile_id':'p','device':{'name':'scope'},'target':'10.0.0.1','legacy_ports':True}])
        script=run.call_args.kwargs['input']
        for ingress,prefix in (('home','192.168.1.0/24'),('ap','192.168.77.0/24')):
            self.assertIn('iifname "'+ingress+'" oifname "scope" ip saddr '+prefix+' ip daddr 10.0.0.1 accept',script)
            self.assertLess(script.index('iifname "'+ingress+'" oifname "scope"'),script.index('iifname "'+ingress+'" drop'))
        self.assertNotIn('iifname "ap" oifname "home"',script)
        self.assertIn('ip daddr 192.168.77.1 tcp dport { 8082, 8083 } dnat ip to 10.0.0.1',script)

    def test_proxy_owner_survives_no_management_and_earlier_same_family_profile(self):
        from wifi_bridge.api import MobileAPI
        api=MobileAPI(self.controller);api.action('primary','disconnect')
        api.update('telescopes',{'id':'later-owner','name':'Later owner','kind':'vespera','target_ip':'10.8.0.1',
            'ssid':'Later-owner','password':'synthetic-pass','mac':self.devices[2]['mac'],'enabled':True},create=True)
        self.controller.cycle(5);api.action('primary','connect');self.controller.cycle(10)
        self.assertEqual(self.controller.proxy_owners['legacy_ports'],'later-owner')
        self.store.config['telescopes'][1]['owner_locked']=True
        self.net.disconnect('home')
        original=self.net.connect
        def no_home(role,*args):
            if role=='home':raise NetworkError('home unavailable')
            return original(role,*args)
        with patch.object(self.net,'connect',side_effect=no_home),patch.object(self.net,'radio_idle',return_value=False):self.controller.cycle(50)
        self.assertEqual(self.controller.state['management']['mode'],'none');self.assertEqual(self.controller.proxy_owners['legacy_ports'],'later-owner')
        self.net.radios=4;self.controller.update(devices=self.net.devices())
        with patch.object(self.net,'connect',side_effect=no_home):self.controller.cycle(100)
        self.assertEqual(self.controller.state['management']['mode'],'ap_only')
        rows={r['id']:r for r in self.controller.state['telescopes']}
        self.assertFalse(rows['primary']['legacy_ports']);self.assertTrue(rows['later-owner']['legacy_ports'])
        self.controller.cycle(150);self.assertEqual(self.controller.state['management']['mode'],'dual');self.assertEqual(self.controller.proxy_owners['legacy_ports'],'later-owner')

    def test_new_home_dhcp_overlap_cannot_disconnect_locked_instrument(self):
        c=self.controller;self.store.config['telescopes'][0]['owner_locked']=True
        original=self.net.address
        def address(device,timeout=20):return '10.0.0.100/24' if device['mac']==self.devices[1]['mac'] else original(device)
        with patch.object(self.net,'address',side_effect=address),patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect:
            c.cycle(50)
        self.assertFalse(any(call.args[0]=='telescope' for call in disconnect.call_args_list))
        self.assertTrue(self.net.connected('telescope',self.devices[0]));self.assertEqual(c.state['management']['mode'],'ap_only')
        self.assertEqual(c.state['management']['home_retry']['reason'],'MANAGEMENT_TELESCOPE_SUBNET_CONFLICT')
        self.assertFalse(c.state['telescopes'][0]['relay_ready']);self.assertEqual(c.state['telescopes'][0]['state'],'conflict')

    def test_ap_only_ensure_and_endpoints_then_dual_without_moving_proxy_owner(self):
        from wifi_bridge.api import MobileAPI,ApiError
        api=MobileAPI(self.controller);self.net.radios=4;self.controller.update(devices=self.net.devices())
        api.update('settings',{'auto_connect':True})
        api.update('telescopes',{'id':'ap-target','name':'AP target','kind':'seestar','target_ip':'10.9.0.1','ssid':'Telescope-DEMO',
            'password':'synthetic-password','auto_eligible':True},create=True)
        with self.lose_home():
            self.controller.cycle(50)
            code,value=self.controller.operations.ensure('ap-target','"'+api.revision()+'"',api.revision,'opaque-ap-session',1000000)
            self.assertEqual(code,202);self.controller.operations.thread.join(5)
            self.assertFalse(self.controller.operations.pending)
        value=self.controller.operations.read(value['id'])
        self.assertEqual(value['state'],'succeeded');self.assertEqual(value['endpoint']['proxy']['host'],'192.168.77.1')
        self.assertEqual([p['role'] for p in value['endpoint']['management_endpoints']],['ap'])
        self.controller.cycle(self.controller.next_home_attempt+1);value=self.controller.operations.read(value['id'])
        self.assertEqual(value['endpoint']['proxy']['host'],'192.168.1.42');self.assertEqual({p['role'] for p in value['endpoint']['management_endpoints']},{'home','ap'})
        self.assertEqual(self.controller.proxy_owners['seestar_proxy'],'ap-target')
        self.assertEqual(len(self.controller.discovery_plans),2)
        with self.assertRaises(ApiError) as error:api.action('primary','connect',data={'mac':self.controller.state['hotspot']['mac']})
        self.assertEqual(error.exception.code,'MANAGEMENT_RADIO_RESERVED')

    def test_lease_can_renew_via_ap_during_separate_blocking_home_retry(self):
        import threading
        from wifi_bridge.api import MobileAPI
        c=self.controller;api=MobileAPI(c)
        with self.lose_home():c.cycle(50)
        lease=c.protection.create('primary',{},'ap-client','"'+api.revision()+'"',api.revision)
        entered=threading.Event();release=threading.Event();original=self.net.connect
        def connect(role,*args):
            if role=='home':entered.set();release.wait(3)
            return original(role,*args)
        def cycle():
            with c.net_lock:c.cycle(100)
        with patch.object(self.net,'connect',side_effect=connect):
            worker=threading.Thread(target=cycle);worker.start();self.assertTrue(entered.wait(2))
            self.assertTrue(c.state['hotspot']['active']);renewed=c.protection.change(lease['id'],{'version':1},'ap-client')
            self.assertEqual(renewed['version'],2);release.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertEqual(c.state['management']['mode'],'dual');self.assertTrue(c.protection.protected('primary'))

    def test_rejected_but_associated_home_subnet_is_reserved_before_quiet_replacement(self):
        from wifi_bridge.api import MobileAPI
        api=MobileAPI(self.controller);api.update('hotspot',{'enabled':True});self.controller.cycle(5)
        self.assertEqual(self.controller.state['management']['mode'],'dual')
        self.store.config['telescopes'][0].update(auto_eligible=True,auto_replaceable=True)
        api.update('settings',{'auto_connect':True})
        api.update('telescopes',{'id':'conflicting-target','name':'Conflicting target','kind':'generic','target_ip':'10.5.0.1',
            'ssid':'Telescope-DEMO','password':'synthetic-pass','auto_eligible':True},create=True)
        c=self.controller;c.state['management']['paths']=[p for p in c.state['management']['paths'] if p['role']=='ap'];c.state['management']['mode']='ap_only';c.state['management']['reason']='ROUTING_CONFLICT'
        ops=c.operations;ops.clock=lambda:1000.;ops.started=ops.last_replacement=0
        self.net.sample_activity=lambda **_:None
        self.net.selection_activity=lambda *_:{'state':'quiet','sample_age_seconds':0,'quiet_seconds':120,'cooldown_remaining':0,'last_activity_age_seconds':180}
        original=self.net.address
        def address(device):return '10.5.0.100/24' if device['mac']==self.devices[1]['mac'] else original(device)
        with patch.object(self.net,'address',side_effect=address),patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect:
            _,accepted=ops.ensure('conflicting-target','"'+api.revision()+'"',api.revision,'opaque-client',1000000);ops.thread.join(5)
        self.assertEqual(ops.read(accepted['id'])['reason'],'OVERLAPPING_TARGET_NETWORK')
        self.assertFalse(any(call.args[0]=='telescope' for call in disconnect.call_args_list));self.assertTrue(self.net.connected('telescope',self.devices[0]))

    def test_failed_ap_restore_is_not_management_ready_and_protection_defers_retry(self):
        c=self.controller;self.net.radios=2
        with self.lose_home():c.cycle(50)
        c.store.config['telescopes'][0]['owner_locked']=True
        with patch.object(self.net,'ap_vacant',return_value=True),patch.object(self.net,'connect',wraps=self.net.connect) as connect:c.cycle(100)
        self.assertFalse(any(call.args[0]=='home' for call in connect.call_args_list))
        c.store.config['telescopes'][0]['owner_locked']=False
        with self.lose_home(),patch.object(self.net,'ap_vacant',return_value=True),patch.object(self.net,'hotspot',side_effect=NetworkError('synthetic AP failure')):
            c.cycle(150)
        self.assertEqual(c.state['management']['mode'],'none');self.assertFalse(c.state['relay_ready'])
        self.assertEqual(c.state['management']['home_retry']['reason'],'HOME_RETRY_FAILED_AP_RESTORE_FAILED')
        self.assertTrue(self.net.connected('telescope',self.devices[0]))

    def test_reconcile_failure_withdraws_published_readiness_without_false_disconnection(self):
        c=self.controller
        with patch.object(self.net,'firewall',side_effect=NetworkError('synthetic nft failure')):
            with self.assertRaises(NetworkError):c.cycle(5)
        self.assertFalse(c.state['relay_ready']);self.assertFalse(c.state['telescopes'][0]['relay_ready'])
        self.assertFalse(c.discovery_plans);self.assertTrue(self.net.connected('telescope',self.devices[0]))

    def test_earlier_saved_profile_cannot_reconnect_into_protected_live_subnet(self):
        from wifi_bridge.api import MobileAPI,ApiError
        api=MobileAPI(self.controller);api.action('primary','disconnect');self.controller.cycle(5)
        api.update('telescopes',{'id':'later','name':'Later protected','kind':'vespera','target_ip':'10.0.0.9','ssid':'Later-WiFi','open':True,
            'mac':self.devices[2]['mac'],'enabled':True},create=True);self.controller.cycle(10)
        self.controller.protection.owner_lock('later',{'locked':True,'current_password':'local-admin-test-password'},'"'+api.revision()+'"',api.revision)
        before=copy.deepcopy(self.store.config);sequence=self.controller.event_store.sequence
        with patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect:
            with self.assertRaises(ApiError) as error:api.action('primary','connect')
            self.assertEqual(error.exception.code,'CONNECTION_PROTECTED');self.controller.cycle(15)
        self.assertEqual(self.store.config,before);self.assertEqual(self.controller.event_store.sequence,sequence)
        self.assertTrue(self.controller.state['telescopes'][1]['relay_ready']);disconnect.assert_not_called()
        # A genuinely unexpected DHCP subnet is rejected after observation too;
        # saved order must not give a newly associated earlier profile priority.
        api.update('telescopes',{'target_ip':'10.9.0.1'},ident='primary');api.action('primary','connect')
        original=self.net.address
        def address(device):return '10.0.0.3/24' if device['mac']==self.devices[0]['mac'] else original(device)
        with patch.object(self.net,'address',side_effect=address),patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect:self.controller.cycle(50)
        self.assertTrue(self.controller.state['telescopes'][1]['relay_ready']);self.assertFalse(self.controller.state['telescopes'][0]['relay_ready'])
        self.assertEqual([p['id'] for p in self.store.config['telescopes']],['primary','later'])
        self.assertTrue(self.net.connected('telescope-later',self.devices[2]))
        self.assertTrue(all(call.args[0]=='telescope' for call in disconnect.call_args_list))


class FreshFieldTests(Fixture):
    def test_fresh_ap_independent_settings_crud_manual_connection_then_home(self):
        from wifi_bridge.api import MobileAPI, ApiError
        from wifi_bridge.config import Invalid
        api=MobileAPI(self.controller);c=self.controller
        self.net.radios=3;self.devices=self.net.devices();c.update(devices=self.devices);c.cycle(0)
        self.assertEqual(c.state['management']['mode'],'ap_only');ap=c.state['hotspot']['mac']
        api.update('settings',{'country':'FR','hostname':'fresh-bridge'})
        self.assertEqual(self.store.config['home'],{});self.assertEqual(self.store.config['country'],'FR')
        api.update('telescopes',{'id':'fresh','name':'Fresh scope','ssid':'Fresh synthetic network',
            'password':'synthetic-scope-password','kind':'seestar','target_ip':'10.8.0.1'},create=True)
        self.assertEqual(self.store.config['telescopes'][0]['mac'],'');self.assertFalse(self.store.config['telescopes'][0]['enabled'])
        api.update('telescopes',{'name':'Renamed'},'fresh')
        # Whole-config UI save must preserve the truly absent home too.
        from wifi_bridge.config import public_config
        value=public_config(self.store.config);value['telescopes'][0]['name']='Browser saved';c.save(value)
        self.assertEqual(api.read('telescopes','fresh')['telescope']['name'],'Browser saved')
        with self.assertRaises(ApiError):api.action('fresh','connect',data={'mac':ap})
        free=next(d for d in self.devices if d['mac']!=ap)
        api.action('fresh','connect',data={'mac':free['mac']});c.cycle(5)
        row=c.state['telescopes'][0];self.assertTrue(row['relay_ready']);self.assertEqual(c.state['management']['mode'],'ap_only')
        self.assertEqual(row['management_paths'][0]['role'],'ap')
        revision=lambda:'"'+api.revision()+'"'
        c.protection.owner_lock('fresh',{'locked':True,'current_password':'bootstrap-test-code'},revision(),api.revision)
        home=next(d for d in self.devices if d['mac']not in (ap,free['mac']))
        with self.assertRaises(ApiError):api.update('home',{'ssid':'Synthetic home','password':'synthetic-home-password','mac':home['mac']})
        self.assertEqual(self.store.config['home'],{})
        c.protection.owner_lock('fresh',{'locked':False,'current_password':'bootstrap-test-code'},revision(),api.revision)
        api.update('home',{'ssid':'Synthetic home','password':'synthetic-home-password','mac':home['mac']});c.cycle(50)
        self.assertEqual(c.state['management']['mode'],'dual');self.assertTrue(c.state['telescopes'][0]['relay_ready'])
        self.assertEqual(self.store.config['telescopes'][0]['password'],'synthetic-scope-password')
        public=public_config(self.store.config);self.assertTrue(public['home']['has_password'])
        self.assertNotIn('synthetic-home-password',str(public));c.save(public)
        self.assertEqual(self.store.config['home']['password'],'synthetic-home-password')
        self.assertEqual(self.store.config['telescopes'][0]['password'],'synthetic-scope-password')
        for patch in ({'ssid':''},{'ssid':'Changed without password'}):
            before=copy.deepcopy(self.store.config)
            with self.assertRaises(Invalid):api.update('home',patch)
            self.assertEqual(self.store.config,before)
        api.update('telescopes',{},'fresh');self.assertEqual(self.store.config['home']['password'],'synthetic-home-password')
        api.action('fresh','disconnect');api.action('fresh','delete');c.cycle(55)
        self.assertEqual(api.read('telescopes')['telescopes'],[]);self.assertEqual(self.store.config['home']['ssid'],'Synthetic home')

    def test_only_truly_absent_home_is_allowed_without_clearing_existing_network(self):
        from wifi_bridge.api import MobileAPI
        from wifi_bridge.config import Invalid, validate_config
        api=MobileAPI(self.controller);c=self.controller;c.cycle(0)
        api.update('settings',{'country':'FR'})
        previous=copy.deepcopy(self.store.config)
        for invalid in (None,[],False,'',{'ssid':''},{'mac':''},{'hidden':True},{'ssid':'','password':'secret'}):
            value=copy.deepcopy(previous);value['home']=invalid
            with self.subTest(home=type(invalid).__name__),self.assertRaises(Invalid):validate_config(value,previous,self.devices)
        api.update('home',{'ssid':'Home synthetic','password':'synthetic-home-pass','mac':self.devices[1]['mac']})
        previous=copy.deepcopy(self.store.config)
        for absent in ({},None,[]):
            value=copy.deepcopy(previous);value['home']=absent
            with self.assertRaises(Invalid):c.save(value)
            self.assertEqual(self.store.config,previous)
