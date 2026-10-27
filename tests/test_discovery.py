"""Seestar request filtering and LAN-proxy lifecycle regressions."""
import copy
from unittest.mock import patch
from test_bridge import Fixture
from wifi_bridge.config import profiles
from wifi_bridge.discovery import scan_request, SEESTAR_PORTS
from wifi_bridge.network import LinuxNetwork


class DiscoveryTests(Fixture):
    def test_only_bounded_json_scan_requests_are_accepted(self):
        for value in [b'', b'[]', b'null', b'1', b'{', b'\xff', b'{"method":"scope_park"}', b' ' * 4097]:
            self.assertIsNone(scan_request(value))
        self.assertEqual(scan_request(b'{"method":"scan_iscope","id":7}\r\n')['id'], 7)

    def test_proxy_is_removed_when_all_management_paths_are_explicitly_withdrawn(self):
        self.configured()
        c = copy.deepcopy(self.store.config)
        c['telescopes'] = profiles(c)
        c['telescopes'][0]['kind'] = 'seestar'
        self.store.save(c)
        self.controller.cycle(0)
        self.assertIsNotNone(self.controller.discovery_plan)
        self.assertTrue(self.controller.status()['telescopes'][0]['seestar_proxy'])
        self.controller.firewall(home=None,setup=None,telescopes=[])
        self.assertIsNone(self.controller.discovery_plan)

    def test_seestar_nat_preserves_management_and_scopes_home_sources(self):
        net = LinuxNetwork()
        addresses = {'home':'192.168.1.2/24', 'scope':'10.0.0.2/24'}
        with patch.object(net, 'address', side_effect=lambda d:addresses[d['name']]), patch('wifi_bridge.network.run') as run:
            net.firewall(home={'name':'home'}, telescopes=[{'device':{'name':'scope'}, 'target':'10.0.0.1', 'seestar_proxy':True}])
        rules = run.call_args.kwargs['input']
        self.assertIn('ip saddr 192.168.1.0/24 ip daddr 192.168.1.2 tcp dport', rules)
        self.assertIn('dnat ip to 10.0.0.1', rules)
        self.assertNotIn(22, SEESTAR_PORTS)
        self.assertNotIn(80, SEESTAR_PORTS)


class CandidateTests(Fixture):
    def test_documented_seestar_model_ssids_prefill_family_only_without_association(self):
        self.configured();self.net.radios=3;self.controller.cycle(0)
        expected=[('S50_demo01','seestar'),('S30_demo02','seestar'),('S50 Pro_demo03','seestar'),
                  ('s50 pro_DEMO04','seestar'),('Seestar-demo05','seestar'),
                  ('Vespera-demo06','vespera'),('DWARF-demo07','dwarf'),
                  ('S500_office','generic'),('S50 ProOffice','generic'),
                  ('MyS50_demo08','generic'),('S30_','generic'),('S50 Pro_','generic'),
                  ('Unknown-demo09','generic')]
        rows=[{'ssid':ssid,'bssid':f'02:00:00:00:01:{i:02x}','signal':75,'frequency':2462,'security':'WPA2'}
              for i,(ssid,_) in enumerate(expected,1)]
        before=copy.deepcopy(self.store.config);associations=dict(self.net.associations)
        with patch.object(self.net,'scan',return_value=rows) as scan,patch.object(self.net,'connect') as connect,patch.object(self.net,'disconnect') as disconnect:
            result=self.controller.candidates.run({'mode':'wifi'})
        scan.assert_called_once_with('wlan2');connect.assert_not_called();disconnect.assert_not_called()
        self.assertEqual(self.store.config,before);self.assertEqual(self.net.associations,associations)
        self.assertEqual(len(result['candidates']),len(expected))
        for candidate,(ssid,kind) in zip(result['candidates'],expected):
            with self.subTest(ssid=ssid):
                self.assertEqual(candidate['ssid'],ssid);self.assertEqual(candidate['suggested']['kind'],kind)
                self.assertIsNone(candidate['reported_identity']);self.assertEqual(candidate['source'],'wifi-scan')
                self.assertEqual(candidate['suggested']['source'],'ssid-hint-and-defaults')
                self.assertNotIn('password',candidate);self.assertNotIn('serial',candidate)
                if kind=='seestar':
                    self.assertEqual(candidate['suggested']['name'],'Seestar')
                    self.assertEqual(candidate['suggested']['target_ip'],'10.0.0.1')
                elif kind=='generic':
                    self.assertEqual(candidate['suggested']['name'],'');self.assertIsNone(candidate['suggested']['target_ip'])

    def test_spare_then_owned_home_station_then_cached_results_without_disconnecting(self):
        self.configured();self.controller.cycle(0);discovery=self.controller.candidates
        before=copy.deepcopy(self.store.config);links=dict(self.net.associations)
        with patch.object(self.net,'scan',wraps=self.net.scan) as scan,patch.object(self.net,'disconnect') as disconnect:
            result=discovery.run({'mode':'wifi'})
            self.assertEqual(result['scan_source'],'connected_station');scan.assert_called_once_with('wlan1');disconnect.assert_not_called()
            self.assertEqual(self.store.config,before);self.assertEqual(self.net.associations,links)
            self.net.radios=3;discovery.last=None;scan.reset_mock()
            self.assertEqual(discovery.run({'mode':'wifi'})['scan_source'],'fresh');scan.assert_called_once_with('wlan2')
            discovery.last=None;self.net.associations['external']=self.net.devices()[2]['mac'];scan.reset_mock()
            with patch.object(self.net,'scan_station',return_value=False),patch.object(self.net,'cached_scan',return_value=[]):
                self.assertEqual(discovery.run({'mode':'wifi'})['reason'],'NO_SPARE_RADIO');scan.assert_not_called()
        from wifi_bridge.api import ApiError
        with self.controller.net_lock:
            with self.assertRaises(ApiError) as error:discovery.run({'mode':'wifi'})
            self.assertEqual(error.exception.code,'NETWORK_BUSY')
        self.assertFalse(self.controller.discovery_pending)

    def test_malformed_duplicate_ssids_expiry_and_pending_mutation_guard(self):
        import threading
        from wifi_bridge.api import ApiError
        self.configured(); self.net.radios=3; self.controller.cycle(0)
        rows=[{'ssid':'Same','bssid':f'02:00:00:00:00:{i:02x}','signal':80,'frequency':2412,'security':'WPA2'} for i in range(1,4)]
        rows += [rows[0],{},None,dict(rows[0],signal=True),dict(rows[0],ssid='<script>')]
        entered=threading.Event();release=threading.Event()
        def scan(name): entered.set();release.wait(2);return rows
        result=[]
        with patch.object(self.net,'scan',side_effect=scan):
            thread=threading.Thread(target=lambda:result.append(self.controller.candidates.run({'mode':'wifi'})));thread.start();self.assertTrue(entered.wait(1))
            self.assertFalse(self.controller.update_safe())
            with self.assertRaises(ApiError): self.controller.telescope_action({'id':'primary','action':'disconnect'})
            with self.assertRaises(Exception): self.controller.save(dict(self.store.config))
            self.assertEqual(self.controller.status()['config']['home']['ssid'],'Maison')
            release.set();thread.join(3)
        self.assertFalse(self.controller.discovery_pending);self.assertEqual(len(result[0]['candidates']),3)
        key=result[0]['candidates'][0]['candidate_id']
        self.controller.candidates.clock=lambda:10**10
        with self.assertRaises(ApiError) as error:self.controller.candidates.binding({'candidate_id':key})
        self.assertEqual(error.exception.code,'CANDIDATE_EXPIRED')

    def test_manual_binding_preserves_ids_credentials_selection_and_rejects_forged_echo(self):
        from wifi_bridge.api import MobileAPI,ApiError
        from wifi_bridge.config import Invalid,validate_config
        self.configured(); api=MobileAPI(self.controller)
        original=copy.deepcopy(self.store.config['telescopes'][0])
        manual={'aliases':[{'ssid':'Other-band','bssid':'02:00:00:00:00:08'}],'owner_confirmed':True}
        with self.assertRaises(ApiError):api.binding('primary',manual)
        api.binding('primary',manual,'"'+api.revision()+'"')
        bound=self.store.config['telescopes'][0]
        for key in original:self.assertEqual(bound[key],original[key])
        self.assertIsNone(bound['physical_binding']['reported_identity']);self.assertIsNone(bound['physical_binding']['client_observation'])
        data=copy.deepcopy(self.store.config);data['telescopes'][0].pop('physical_binding')
        self.assertEqual(validate_config(data,self.store.config,self.devices)['telescopes'][0]['physical_binding'],bound['physical_binding'])
        forged=copy.deepcopy(self.store.config);forged['telescopes'][0]['physical_binding']['reported_identity']={'serial':'invented'}
        with self.assertRaises(Invalid):validate_config(forged,self.store.config,self.devices)
        old='"'+api.revision()+'"';api.binding('primary',{},old,unlink=True)
        self.assertNotIn('physical_binding',self.store.config['telescopes'][0]);self.assertEqual(self.store.config['telescopes'][0],original)
        with self.assertRaises(ApiError) as error:api.binding('primary',manual,old)
        self.assertEqual(error.exception.code,'PRECONDITION_FAILED')

    def test_conflicting_alias_requires_explicit_exact_correction_and_active_binding_unchanged(self):
        from wifi_bridge.api import MobileAPI,ApiError
        self.configured();api=MobileAPI(self.controller)
        api.update('telescopes',{'id':'second','name':'Second','ssid':'Other','password':'other-password','kind':'generic','target_ip':'10.0.0.1'},create=True)
        data={'aliases':[{'ssid':'Instrument','bssid':'02:00:00:00:00:01'}],'owner_confirmed':True}
        api.binding('primary',data,'"'+api.revision()+'"')
        with self.assertRaises(ApiError) as error:api.binding('second',data,'"'+api.revision()+'"')
        self.assertEqual(error.exception.code,'BINDING_CONFLICT');self.assertEqual(error.exception.details['conflicting_profile_ids'],['primary'])
        api.binding('second',dict(data,confirm_replace=True,conflicting_profile_ids=['primary']),'"'+api.revision()+'"')
        first,second=self.store.config['telescopes'];self.assertNotIn('physical_binding',first);self.assertIn('physical_binding',second)
        self.assertEqual(first['bssid'],'');self.assertTrue(first['enabled']);self.assertFalse(second['enabled'])

    def test_reachable_targets_only_attached_associated_seestar_and_no_identity_fabrication(self):
        self.configured(); self.controller.cycle(0); c=self.controller.candidates
        with patch.object(self.net,'discover_seestar',return_value=None) as probe:
            self.assertEqual(c.run({'mode':'reachable'})['reason'],'NO_SUPPORTED_REACHABLE_DEVICE');probe.assert_not_called()
            self.store.config['telescopes'][0]['kind']='seestar';c.last=None
            self.assertEqual(c.run({'mode':'reachable'})['candidates'],[]);probe.assert_called_once()
            self.net.disconnect('telescope');c.last=None;probe.reset_mock();c.run({'mode':'reachable'});probe.assert_not_called()

    def test_seestar_wire_is_interface_bound_source_and_request_correlated_and_bounded(self):
        import json
        from unittest.mock import MagicMock
        from wifi_bridge.candidates import seestar_probe
        sock=MagicMock();sock.__enter__.return_value=sock
        def receive(size):
            sent=json.loads(sock.sendto.call_args.args[0]);return json.dumps({'id':sent['id'],'method':'scan_iscope','result':{'serial':'IGNORED'}}).encode(),('10.0.0.1',4720)
        sock.recvfrom.side_effect=receive
        with patch('wifi_bridge.candidates.socket.socket',return_value=sock):
            result=seestar_probe({'name':'wlan2'},'10.0.0.2/24','10.0.0.1')
            self.assertIsNone(result['reported_identity']);self.assertEqual(result['address'],'10.0.0.1')
            self.assertEqual(json.loads(sock.sendto.call_args.args[0])['method'],'scan_iscope');self.assertEqual(sock.sendto.call_args.args[1],('10.0.0.1',4720))
            sock.bind.assert_called_with(('10.0.0.2',0));sock.recvfrom.assert_called_with(4097)
            self.assertIsNone(seestar_probe({'name':'wlan2'},'10.0.0.2/24','192.168.1.1'))
            for raw,origin in [(b'[]',('10.0.0.1',4720)),(b'{}',('10.0.0.3',4720)),(b'x'*4097,('10.0.0.1',4720))]:
                sock.recvfrom.side_effect=None;sock.recvfrom.return_value=(raw,origin)
                self.assertIsNone(seestar_probe({'name':'wlan2'},'10.0.0.2/24','10.0.0.1'))


    def test_alias_merge_remove_preserves_observed_provenance_frequency_and_bound(self):
        from wifi_bridge.api import MobileAPI
        from wifi_bridge.config import Invalid
        self.configured();self.net.radios=3;self.controller.cycle(0);api=MobileAPI(self.controller)
        candidate=self.controller.candidates.run({'mode':'wifi'})['candidates'][0]
        api.binding('primary',{'candidate_id':candidate['candidate_id']},'"'+api.revision()+'"')
        original=copy.deepcopy(self.store.config['telescopes'][0]['physical_binding']['aliases'][0])
        for i in range(1,8):
            api.binding('primary',{'aliases':[{'ssid':'Band alias','bssid':f'02:00:00:00:00:{i:02x}'}],'owner_confirmed':True,'append_aliases':True},'"'+api.revision()+'"')
        binding=self.store.config['telescopes'][0]['physical_binding'];self.assertEqual(len(binding['aliases']),8);self.assertEqual(binding['aliases'][0],original)
        self.assertEqual(original['frequency'],2412)
        with self.assertRaises(Invalid):api.binding('primary',{'aliases':[{'ssid':'Overflow','bssid':'02:00:00:00:00:09'}],'owner_confirmed':True,'append_aliases':True},'"'+api.revision()+'"')
        api.binding('primary',{'remove_bssid':'02:00:00:00:00:01'},'"'+api.revision()+'"')
        self.assertEqual(len(self.store.config['telescopes'][0]['physical_binding']['aliases']),7)
        self.assertEqual(self.store.config['telescopes'][0]['physical_binding']['aliases'][0],original)

    def test_linux_free_state_is_positive_and_scan_failure_releases_gates(self):
        from wifi_bridge.network import NetworkError
        from wifi_bridge.api import ApiError
        net=LinuxNetwork()
        with patch.object(net,'_hotspot_device',return_value=''),patch('wifi_bridge.network.run',side_effect=['30 (disconnected)','Not connected.','type managed']):
            self.assertTrue(net.radio_idle({'name':'wlan2'}))
        for state,link in [('100 (connected)','Not connected.'),('30 (disconnected)',''),('10 (unmanaged)','Not connected.')]:
            with patch('wifi_bridge.network.run',side_effect=[state,link,'type managed']):self.assertFalse(net.radio_idle({'name':'wlan2'}))
        for mode in ('AP','monitor','unknown',''):
            with patch.object(net,'_hotspot_device',return_value=''),patch('wifi_bridge.network.run',side_effect=['30 (disconnected)','Not connected.','type '+mode]):
                self.assertFalse(net.radio_idle({'name':'wlan2'}))
        self.configured();self.net.radios=3;self.controller.cycle(0)
        with patch.object(self.net,'scan',side_effect=NetworkError('PRIVATE SYSTEM DETAIL')):
            with self.assertRaises(ApiError) as error:self.controller.candidates.run({'mode':'wifi'})
        self.assertEqual(error.exception.code,'DISCOVERY_UNAVAILABLE');self.assertNotIn('PRIVATE',error.exception.message)
        self.assertFalse(self.controller.net_lock.locked());self.assertFalse(self.controller.discovery_pending)


    def test_binding_metadata_does_not_reassociate_or_publish_network_outcomes_on_reconcile(self):
        from wifi_bridge.api import MobileAPI
        self.configured();self.controller.cycle(0);api=MobileAPI(self.controller)
        sequence=self.controller.event_store.sequence
        point={'aliases':[{'ssid':'Physical band','bssid':'02:00:00:77:88:01'}],'owner_confirmed':True}
        with patch.object(self.net,'connect',wraps=self.net.connect) as connect,patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect,patch.object(self.controller,'firewall',wraps=self.controller.firewall) as firewall:
            api.binding('primary',point,'"'+api.revision()+'"');self.controller.cycle(5)
            api.binding('primary',{'aliases':[{'ssid':'Other band','bssid':'02:00:00:77:88:02'}],'owner_confirmed':True,'append_aliases':True},'"'+api.revision()+'"');self.controller.cycle(10)
            api.binding('primary',{'remove_bssid':'02:00:00:77:88:01'},'"'+api.revision()+'"');self.controller.cycle(15)
            api.binding('primary',{},'"'+api.revision()+'"',unlink=True);self.controller.cycle(20)
            connect.assert_not_called();self.assertNotIn('telescope',[call.args[0] for call in disconnect.call_args_list])
            self.assertTrue(all(call.kwargs for call in firewall.call_args_list))  # No empty/reset forwarding plan.
            self.assertEqual(self.controller.event_store.sequence,sequence);self.assertTrue(self.controller.status()['telescopes'][0]['relay_ready'])
            api.update('telescopes',{'ssid':'Changed instrument','password':'new-instrument-pass','bssid':'02:00:00:77:88:03'},ident='primary')
            self.controller.cycle(25);connect.assert_called_once();self.assertEqual(connect.call_args.args[2]['bssid'],'02:00:00:77:88:03')
            self.assertIn('telescope',[call.args[0] for call in disconnect.call_args_list])

    def test_read_only_listener_survives_transient_demand_network_failure_without_proxy(self):
        import itertools
        from unittest.mock import MagicMock
        from wifi_bridge.discovery import SeestarDiscovery
        from wifi_bridge.network import NetworkError
        self.configured();self.controller.cycle(0)
        self.assertIsNone(self.controller.discovery_plan);self.assertIsNotNone(self.controller.management_plan)
        listener=MagicMock();count=[0]
        def receive(*args):
            count[0]+=1
            if count[0]>2:self.controller.stop.set();raise OSError('test loop complete')
            return b'{"method":"scan_iscope","id":"bounded-string-correlation","params":[]}',('192.168.1.9',12345)
        listener.recvfrom.side_effect=receive
        with patch('wifi_bridge.discovery.socket.socket',return_value=listener),patch('wifi_bridge.discovery.time.monotonic',side_effect=itertools.count()),patch.object(self.controller.operations,'demand',side_effect=[NetworkError('transient'), 'REQUEST_ACCEPTED']) as demand:
            SeestarDiscovery(self.controller).loop()
        self.assertEqual(demand.call_count,2);listener.sendto.assert_not_called();listener.close.assert_called()
        self.assertIsNotNone(scan_request(b'{"method":"scan_iscope","id":"vendor-id","params":[]}'))
        self.assertIsNone(scan_request(b'{"method":"scan_iscope","id":{}}'))

    def test_dual_management_listeners_are_device_bound_and_share_demand_budget(self):
        import itertools,socket
        from unittest.mock import MagicMock
        from wifi_bridge.discovery import SeestarDiscovery
        self.configured();self.controller.cycle(0)
        plans=(('home0','192.168.1.42/24'),('ap0','192.168.77.1/24'))
        self.controller.management_plans=plans;self.controller.discovery_plans=()
        listeners=[MagicMock(),MagicMock()];received=[0]
        for listener,source in zip(listeners,('192.168.1.9','192.168.77.9')):
            def receive(*_,source=source):
                received[0]+=1
                if received[0]>2:self.controller.stop.set();raise OSError('test complete')
                return b'{"method":"scan_iscope","id":7}',(source,12345)
            listener.recvfrom.side_effect=receive
        with patch('wifi_bridge.discovery.socket.socket',side_effect=listeners),patch('wifi_bridge.discovery.time.monotonic',side_effect=itertools.count()),patch.object(self.controller.operations,'demand',return_value='DEMAND_COOLDOWN') as demand:
            SeestarDiscovery(self.controller).loop()
        self.assertEqual(demand.call_args_list[0].args,(plans[0],'192.168.1.9'));self.assertEqual(demand.call_args_list[1].args,(plans[1],'192.168.77.9'))
        for listener,plan in zip(listeners,plans):
            listener.setsockopt.assert_any_call(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,plan[0].encode()+b'\0')
            listener.close.assert_called();listener.sendto.assert_not_called()
