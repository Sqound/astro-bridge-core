"""Wi-Fi mount support: identity, bounded queries, forwarding and safety gates."""
import copy
import socket
from unittest.mock import MagicMock, patch

from test_bridge import Fixture
from test_vespera_demand import packet, kernel_result
from wifi_bridge.api import ApiError, MobileAPI
from wifi_bridge.activity import consultation_delta
from wifi_bridge.candidates import scan_candidate
from wifi_bridge.config import Invalid, auto_policy, default_config
from wifi_bridge.mounts import MOUNTS, mount_probe, zwo_identity, synscan_identity
from wifi_bridge.mount_discovery import synscan_query
from wifi_bridge.network import LinuxNetwork, NetworkError
from wifi_bridge.vespera_demand import syn_filter


class MountIdentityTests(Fixture):
    def test_wifi_names_suggest_family_only_and_never_claim_model(self):
        for ssid, kind in [('AMH_DEMO01','zwo_mount'),('SynScan_WiFi_DEMO','synscan'),('SynScan_0123','synscan'),
                           ('SynScan_WiFi_AB12','synscan'),('SynScan_'+'A'*24,'synscan'),
                           ('AM5 private network','generic'),('SynScanner','generic'),('SynScan','generic')]:
            candidate = scan_candidate({'ssid':ssid,'bssid':'02:00:00:11:22:33','signal':80,'frequency':2412,'security':'WPA2'},123)
            self.assertEqual(candidate['suggested']['kind'],kind)
            self.assertIsNone(candidate['reported_identity'])
            if kind=='zwo_mount':self.assertEqual(candidate['suggested']['password'],'12345678')
            else:self.assertNotIn('password',candidate['suggested'])
            if kind in MOUNTS:self.assertEqual(candidate['suggested']['target_ip'],'192.168.4.1')

    def test_protocol_identity_is_strict_bounded_and_not_an_authenticated_serial(self):
        for name in (b'AM5N#', b'AM3#', b'ZWO AM7#'):
            self.assertEqual(zwo_identity(name)['family'],'zwo_mount')
        for value in (b'OnStep#',b'AM5N#PRIVATE',b'AM5N#\x00',b'\xff',b'A'*41):
            self.assertIsNone(zwo_identity(value))
        result = synscan_identity(b'=020600\r')
        self.assertEqual(result['board_version_hex'],'020600')
        self.assertIsNone(result['model'])
        self.assertEqual(result['authentication'],'unverified')
        for value in (b'!1\r',b'=020600',b'=0206\r',b'=020600\rEXTRA',b'=GGGGGG\r'):
            self.assertIsNone(synscan_identity(value))

    def test_queries_bind_exact_interface_and_never_send_setting_or_motion_commands(self):
        device={'name':'mount0'}
        for kind, expected in [('zwo_mount',b':GVP#'),('synscan',b':e1\r')]:
            stream=MagicMock();stream.__enter__.return_value=stream
            stream.recv.side_effect=[b'AM',b'5N#']
            stream.recvfrom.return_value=(b'=020600\r',('192.168.4.1',11880))
            with self.subTest(kind=kind),patch('wifi_bridge.mounts.socket.socket',return_value=stream),patch.object(socket,'SO_BINDTODEVICE',25,create=True):
                result=mount_probe(device,'192.168.4.2/24','192.168.4.1',kind)
            self.assertEqual(result['protocol_identity']['family'],kind)
            stream.bind.assert_called_once_with(('192.168.4.2',0))
            self.assertIn((socket.SOL_SOCKET,25,b'mount0\0'),[c.args for c in stream.setsockopt.call_args_list])
            if kind=='zwo_mount':stream.sendall.assert_called_once_with(expected)
            else:stream.sendto.assert_called_once_with(expected,('192.168.4.1',11880))
            self.assertTrue(all(0<t.args[0]<=2.5 for t in stream.settimeout.call_args_list))

    def test_offlink_local_and_invalid_peers_never_open_or_accept_a_probe(self):
        for target in ('192.168.5.1','192.168.4.2','192.168.4.0','192.168.4.255','127.0.0.1'):
            with self.subTest(target=target),patch('wifi_bridge.mounts.socket.socket') as create:
                self.assertIsNone(mount_probe({'name':'mount0'},'192.168.4.2/24',target,'synscan'))
                create.assert_not_called()
        stream=MagicMock();stream.__enter__.return_value=stream
        stream.recvfrom.return_value=(b'=020600\r',('192.168.4.3',11880))
        with patch('wifi_bridge.mounts.socket.socket',return_value=stream),patch.object(socket,'SO_BINDTODEVICE',25,create=True):
            self.assertIsNone(mount_probe({'name':'mount0'},'192.168.4.2/24','192.168.4.1','synscan'))

    def test_mount_control_guiding_and_small_transfers_always_protect(self):
        old={'packets':10,'bytes':100,'control':(10,100)}
        for kind in MOUNTS:
            self.assertEqual(consultation_delta(old,11,140,(11,140),5,kind),('mount_control',True))
            self.assertEqual(consultation_delta(old,10,100,(10,100),5,kind),('none',False))
            self.assertEqual(consultation_delta(None,0,0,None,5,kind),('unknown',True))

    def test_probe_does_not_send_after_connect_exhausts_whole_query_deadline(self):
        stream=MagicMock();stream.__enter__.return_value=stream
        now=[10.0]
        stream.connect.side_effect=lambda peer:now.__setitem__(0,13.0)
        with patch('wifi_bridge.mounts.socket.socket',return_value=stream),patch.object(socket,'SO_BINDTODEVICE',25,create=True):
            with self.assertRaises(TimeoutError):
                mount_probe({'name':'mount0'},'192.168.4.2/24','192.168.4.1','zwo_mount',clock=lambda:now[0])
        stream.sendall.assert_not_called()


class MountControllerTests(Fixture):
    def setUp(self):
        super().setUp();self.net.radios=3;self.devices=self.net.devices();self.controller.update(devices=self.devices)
        self.configured();self.api=MobileAPI(self.controller)

    def select_kind(self,kind):
        self.api.update('telescopes',{'kind':kind,'target_ip':'192.168.4.1'},ident='primary')
        self.controller.cycle(0)

    def test_profiles_advertise_transport_but_reconcile_never_probes_or_moves_mount(self):
        for kind,spec in MOUNTS.items():
            with self.subTest(kind=kind),patch.object(self.net,'discover_mount') as probe:
                self.select_kind(kind)
                row=self.controller.state['telescopes'][0]
                self.assertTrue(row['relay_ready']);self.assertTrue(row[kind+'_proxy'])
                endpoint=self.controller.operations.endpoint(self.store.config['telescopes'][0],row,self.controller.state['home_ip'])
                self.assertEqual(endpoint['proxy']['transport'],spec['transport'])
                self.assertEqual(endpoint['proxy']['ports'],[spec['port']])
                self.assertEqual(endpoint['service_readiness'],'not_probed')
                probe.assert_not_called()
        self.assertIsNone(self.controller.proxy_owners['zwo_mount_proxy'])

    def test_discovery_preserves_reserved_hotspot_adapter_before_ap_startup(self):
        self.controller.cycle(0)
        self.api.update('hotspot',{'enabled':True,'mac':self.devices[2]['mac']},expected='"'+self.api.revision()+'"')
        with patch.object(self.net,'scan',return_value=[]) as scan:
            result=self.controller.candidates.run({'mode':'wifi'})
        self.assertEqual(result['scan_source'],'connected_station')
        scan.assert_called_once_with(next(d['name'] for d in self.devices if d['mac']==self.store.config['home']['mac']))

    def test_mount_flags_are_opt_in_for_legacy_policy_and_preserve_other_settings(self):
        legacy=copy.deepcopy(self.store.config)
        for field in ('zwo_mount_demand','synscan_demand'):legacy['auto_policy'].pop(field)
        self.store.config=legacy
        for field in ('zwo_mount_demand','synscan_demand'):
            self.assertFalse(auto_policy(legacy)[field])
            self.assertTrue(auto_policy(default_config(self.devices[0]['mac']))[field])
            with self.assertRaises(Invalid):self.api.update('settings',{'auto_policy':{field:True}},expected='"'+self.api.revision()+'"')
            self.api.update('settings',{'auto_policy':{field:True},'acknowledge_interruption':True},expected='"'+self.api.revision()+'"')
            self.assertFalse(auto_policy(self.store.config)['force_first_profile'])

    def test_ambiguous_native_mount_demand_never_picks_saved_order(self):
        self.controller.cycle(0);self.store.config['auto_connect']=True
        for kind in MOUNTS:
            self.controller.operations.demand_next=0
            for n in range(2):self.api.update('telescopes',{'id':kind.replace('_','-')+str(n),'kind':kind,'name':'Synthetic mount','ssid':'Synthetic','open':True,'target_ip':'192.168.4.1','auto_eligible':True},create=True)
            plan=self.controller.management_plan
            self.assertEqual(self.controller.operations.demand(plan,'192.168.1.9',kind=kind),'SELECTION_REQUIRED')
            self.assertFalse(self.controller.operations.pending)


class MountNativeTests(MountControllerTests):
    def test_zwo_kernel_capture_is_header_only_and_rejects_other_protocols(self):
        for ihl in (20,24,60):
            with patch('test_vespera_demand.syn_filter',return_value=syn_filter((4030,))):
                self.assertEqual(kernel_result(packet(port=4030,ihl=ihl,payload=b'PRIVATE')),14+ihl+20)
                for options in ({'port':8082},{'port':4030,'flags':16},{'port':4030,'fragment':1},{'port':4030,'protocol':17}):
                    self.assertEqual(kernel_result(packet(**options)),0)

    def test_zwo_demand_matches_saved_target_or_explicit_management_endpoint(self):
        self.select_kind('zwo_mount');self.controller.demo=False
        self.store.config['auto_connect']=True
        observer=self.controller.zwo_mount_demand;plan=self.controller.management_plan
        with patch.object(self.controller.operations,'demand',return_value='REQUEST_ACCEPTED') as demand:
            observer.receive(packet(port=4030,target='192.168.4.1'),0,plan)
            self.assertEqual(demand.call_args.kwargs['kind'],'zwo_mount')
            observer.receive(packet(port=4030,target='10.9.0.1',seq=2),0,plan)
            self.assertEqual(demand.call_count,1)
            observer.receive(packet(port=4030,target='192.168.1.42',seq=3),0,plan)
            self.assertIsNone(demand.call_args.kwargs['target'])

    def test_synscan_discovery_rejects_all_non_read_queries_and_foreign_sources(self):
        plan=('home0','192.168.1.42/24')
        self.assertEqual(synscan_query(b':e1\r',('192.168.1.9',1234),plan),'192.168.1.9')
        self.assertEqual(synscan_query(b':e2\r',('192.168.1.9',1234),plan),'192.168.1.9')
        for data in (b':J1\r',b':F1\r',b':e1\rEXTRA',b':e1',b':e3\r',b'PRIVATE'):
            self.assertIsNone(synscan_query(data,('192.168.1.9',1234),plan))
        for source in ('192.168.1.42','192.168.1.0','192.168.1.255','10.9.0.1'):
            self.assertIsNone(synscan_query(b':e1\r',(source,1234),plan))

    def test_synscan_query_can_request_selection_without_inventing_a_reply(self):
        self.controller.cycle(0);self.controller.demo=False;self.store.config['auto_connect']=True
        self.api.update('telescopes',{'id':'waiting-synscan','kind':'synscan','name':'Synthetic mount','ssid':'Synthetic','open':True,'target_ip':'192.168.4.1','auto_eligible':True},create=True)
        observer=self.controller.synscan_demand;listener=MagicMock();plan=self.controller.management_plan
        with patch.object(self.controller.operations,'demand',return_value='REQUEST_ACCEPTED') as demand,patch('wifi_bridge.mount_discovery.socket.socket') as upstream:
            observer.receive(b':e1\r',('192.168.1.9',1234),plan,listener)
        demand.assert_called_once_with(plan,'192.168.1.9',kind='synscan',target=None)
        listener.sendto.assert_not_called();upstream.assert_not_called()

    def test_synscan_relay_forwards_only_real_correlated_version_reply(self):
        self.select_kind('synscan');self.controller.demo=False;observer=self.controller.synscan_demand
        stream=MagicMock();stream.__enter__.return_value=stream
        stream.recvfrom.return_value=(b'=020600\r',('192.168.4.1',11880));listener=MagicMock();plan=self.controller.management_plan
        with patch('wifi_bridge.mount_discovery.socket.socket',return_value=stream),patch.object(socket,'SO_BINDTODEVICE',25,create=True):
            observer.receive(b':e1\r',('192.168.1.9',1234),plan,listener)
        listener.sendto.assert_called_once_with(b'=020600\r',('192.168.1.9',1234))
        stream.sendto.assert_called_once_with(b':e1\r',('192.168.4.1',11880))

    def test_synscan_pending_intent_is_bounded_rechecked_and_removed_on_policy_disable(self):
        self.controller.cycle(0);self.controller.demo=False;self.store.config['auto_connect']=True
        self.api.update('telescopes',{'id':'waiting-synscan','kind':'synscan','name':'Synthetic mount','ssid':'Synthetic','open':True,'target_ip':'192.168.4.1','auto_eligible':True},create=True)
        observer=self.controller.synscan_demand;plan=self.controller.management_plan;now=[100.0];observer.clock=lambda:now[0]
        with patch.object(self.controller.operations,'demand',side_effect=['RECONCILE_PENDING','REQUEST_ACCEPTED']) as demand:
            observer.receive(b':e1\r',('192.168.1.9',1234),plan,MagicMock())
            self.assertEqual(len(observer.pending),1)
            self.controller.last_cycle_ok=True;observer.retry_pending()
            self.assertEqual(demand.call_count,2);self.assertFalse(observer.pending)
        with patch.object(self.controller.operations,'demand',return_value='RECONCILE_PENDING'):
            now[0]+=1;observer.receive(b':e1\r',('192.168.1.9',1235),plan,MagicMock())
        self.assertEqual(len(observer.pending),1)
        self.store.config['auto_connect']=False;observer.retry_pending()
        self.assertFalse(observer.pending)

    def test_synscan_never_returns_foreign_malformed_or_stale_selected_reply(self):
        self.select_kind('synscan');self.controller.demo=False;observer=self.controller.synscan_demand
        plan=self.controller.management_plan
        for reply,origin,change in [(b'=020600\r',('192.168.4.3',11880),False),(b'!1\r',('192.168.4.1',11880),False),(b'=020600\r',('192.168.4.1',11880),True)]:
            stream=MagicMock();stream.__enter__.return_value=stream;listener=MagicMock();observer.next_request=0
            def receive(size):
                if change:self.store.config['telescopes'][0]['enabled']=False
                return reply,origin
            stream.recvfrom.side_effect=receive
            with patch('wifi_bridge.mount_discovery.socket.socket',return_value=stream),patch.object(socket,'SO_BINDTODEVICE',25,create=True):
                observer.receive(b':e1\r',('192.168.1.9',1234),plan,listener)
            listener.sendto.assert_not_called()

    def test_synscan_listener_opens_nothing_in_demo_and_closes_on_policy_disable(self):
        observer=self.controller.synscan_demand
        with patch('wifi_bridge.mount_discovery.socket.socket') as create:
            observer.sync();create.assert_not_called()
        self.controller.demo=False;self.controller.cycle(0)
        listener=MagicMock();observer.sockets[self.controller.management_plan]=listener
        observer.sync();listener.close.assert_called_once();self.assertFalse(observer.sockets)


class MountFirewallTests(Fixture):
    def test_native_mount_nat_has_exact_ingress_target_port_and_transport(self):
        linux=LinuxNetwork(self.tmp.name);scope={'name':'scope0'};home={'name':'home0'};ap={'name':'ap0'}
        addresses={'scope0':'192.168.4.2/24','home0':'192.168.12.2/24','ap0':'192.168.77.1/24'}
        for kind,spec in MOUNTS.items():
            linux.last_firewall=None
            route={'kind':kind,'device':scope,'target':'192.168.4.1',kind+'_proxy':True}
            with patch.object(linux,'address',side_effect=lambda d,**kw:addresses[d['name']]),patch.object(linux,'configure_activity'),patch('wifi_bridge.network.run',return_value='') as run:
                linux.firewall(home=home,setup=ap,telescopes=[route])
            script=next(c.kwargs['input'] for c in reversed(run.call_args_list) if c.args[0]==['nft','-f','-'])
            self.assertIn(f'ip daddr 192.168.12.2 {spec["transport"]} dport {spec["port"]} dnat ip to 192.168.4.1',script)
            self.assertIn(f'ip daddr 192.168.77.1 {spec["transport"]} dport {spec["port"]} dnat ip to 192.168.4.1',script)
            self.assertNotIn('dport 11881',script)
            if kind=='synscan':
                self.assertIn('type filter hook output priority raw',script)
                self.assertIn('oifname "home0" ip saddr 192.168.12.2 ip daddr 192.168.12.0/24 udp sport 11880 notrack',script)
                self.assertIn('oifname "ap0" ip saddr 192.168.77.1 ip daddr 192.168.77.0/24 udp sport 11880 notrack',script)
                self.assertEqual(script.count(' notrack'),2)
            else:self.assertNotIn('notrack',script)

    def test_duplicate_or_wrong_protocol_proxy_owners_fail_before_publication(self):
        linux=LinuxNetwork(self.tmp.name)
        for rows in ([{'kind':'synscan','synscan_proxy':True}]*2,[{'kind':'generic','zwo_mount_proxy':True}]):
            with patch('wifi_bridge.network.run') as run,patch.object(linux,'withdraw_sharing'),self.assertRaises(NetworkError):
                linux.firewall(telescopes=rows)
            run.assert_not_called()
