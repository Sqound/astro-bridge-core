"""Wi-Fi onboarding remains passive with respect to association and preserves guards."""
import copy
import json
from unittest.mock import patch
from test_bridge import Fixture
from wifi_bridge.api import ApiError
from wifi_bridge.candidates import CandidateDiscovery, scan_candidate
from wifi_bridge.network import LinuxNetwork, NetworkError
from wifi_bridge.notifications import NotificationSource
from wifi_bridge.plugins import onboarding_defaults, validate_manifest, plugin

ROW={'ssid':'DWARF_mini_DEMO','bssid':'02:00:00:77:88:11','frequency':2412,'signal':81,'security':'WPA2'}

class OnboardingTests(Fixture):
    def test_defaults_match_documented_models_and_never_invent_custom_credentials(self):
        for ssid,ip,password in [('DWARF_mini_DEMO','192.168.88.1','DWARF_12345678'),('DWARF3_DEMO','192.168.88.1','DWARF_12345678'),('S50_demo','10.0.0.1','12345678'),('AMH_DEMO','192.168.4.1','12345678')]:
            value=onboarding_defaults(ssid);self.assertEqual(value['target_ip'],ip);self.assertEqual(value['password'],password)
        self.assertIsNone(onboarding_defaults('DWARF_unknown')['target_ip'])
        self.assertNotIn('password',onboarding_defaults('DWARF_unknown'))
        self.assertNotIn('password',onboarding_defaults('Unknown-home'))
        for rules in ([{'prefix':'','password':'short'}],[{'prefix':'','target_ip':'8.8.8.8'}],[{'prefix':'','target_ip':True}],[{'prefix':'','exec':'bad'}]):
            with self.assertRaises(ValueError):validate_manifest(dict(plugin('seestar'),onboarding=rules))

    def test_busy_radios_use_only_cached_observations_without_scan_or_disconnection(self):
        self.configured();self.controller.cycle(0);c=self.controller.candidates
        before=copy.deepcopy(self.store.config);links=dict(self.net.associations)
        with patch.object(self.net,'scan_station',return_value=False),patch.object(self.net,'radio_idle',return_value=False),patch.object(self.net,'cached_scan',return_value=[ROW]) as cached,patch.object(self.net,'scan') as scan,patch.object(self.net,'disconnect') as disconnect:
            result=c.run({'mode':'wifi'})
        scan.assert_not_called();disconnect.assert_not_called();self.assertTrue(cached.called)
        self.assertEqual(result['scan_source'],'cached');self.assertEqual(len(result['candidates']),1)
        self.assertEqual(result['candidates'][0]['suggested']['target_ip'],'192.168.88.1')
        self.assertEqual(self.store.config,before);self.assertEqual(self.net.associations,links)

    def test_isolated_hotspot_can_scan_a_quiet_owned_station_without_releasing_it(self):
        self.configured();self.controller.cycle(0)
        sample={'primary':{'state':'quiet','sample_age_seconds':2,'tcp_established':0,'raw_active':False}}
        station=self.net.scan_station
        with patch.object(self.net,'scan_station',side_effect=lambda d,p,role='home':role!='home' and station(d,p,role)),patch.object(self.net,'activity_status',create=True,return_value={'profiles':sample}),patch.object(self.net,'scan',return_value=[ROW]) as scan,patch.object(self.net,'disconnect') as disconnect:
            result=self.controller.candidates.run({'mode':'wifi'})
        self.assertEqual(result['scan_source'],'connected_station');scan.assert_called_once_with('wlan0');disconnect.assert_not_called()

    def test_retained_prefill_survives_restart_and_scan_expiry_without_password_storage(self):
        self.configured();self.net.radios=3;self.controller.cycle(0);c=self.controller.candidates
        with patch.object(self.net,'scan',return_value=[ROW]):result=c.run({'mode':'wifi'})
        candidate=result['candidates'][0];ident=candidate['observation_id']
        c.clock=lambda:10**10
        with self.assertRaises(ApiError):c.binding({'candidate_id':candidate['candidate_id']})
        restored=CandidateDiscovery(self.controller)
        self.assertEqual(restored.lookup(ident)['ssid'],ROW['ssid'])
        self.assertEqual(restored.lookup(ident)['suggested']['password'],'DWARF_12345678')
        self.assertNotIn('password',restored.catalog_path.read_text());self.assertEqual(restored.catalog_path.stat().st_mode&0o777,0o600)
        with self.assertRaises(ApiError):restored.lookup('0'*32)

    def test_cache_background_observation_does_not_expire_an_interactive_binding_choice(self):
        self.configured();self.net.radios=3;self.controller.cycle(0);c=self.controller.candidates
        with patch.object(self.net,'scan',return_value=[ROW]),patch.object(self.net,'cached_scan',return_value=[ROW]):
            candidate=c.run({'mode':'wifi'})['candidates'][0]
            c.run({'mode':'wifi'},cached=True)
        self.assertEqual(c.binding({'candidate_id':candidate['candidate_id']})['aliases'][0]['bssid'],ROW['bssid'])

    def test_unsaved_recognized_network_notifies_once_with_durable_add_route(self):
        self.configured();self.controller.cycle(0);c=self.controller.candidates
        c.remember(scan_candidate(ROW,100));c.remember(scan_candidate(dict(ROW,ssid='Home-office',bssid='02:00:00:77:88:12'),100))
        source=NotificationSource(self.store.path.parent,clock=lambda:100,kernel_boot='11111111-1111-1111-1111-111111111111')
        def collect():return source.collect(source.snapshot(self.controller))
        notices=collect();self.assertEqual(len(notices),1);event=notices[0]
        self.assertEqual(event['code'],'new_detected');self.assertEqual(event['category'],'detected')
        self.assertRegex(event['path'],r'^/#configuration/telescopes\?add=[a-f0-9]{32}$')
        self.assertEqual(collect(),[])
        source=NotificationSource(self.store.path.parent,clock=lambda:101,kernel_boot='11111111-1111-1111-1111-111111111111')
        self.assertTrue(source.available);self.assertEqual(collect(),[])
        self.store.config['telescopes'].append(dict(id='saved-dwarf',kind='dwarf',ssid=ROW['ssid'],bssid='',name='My dwarf',target_ip='192.168.88.1'))
        self.assertEqual(collect(),[])
        self.assertNotIn('password',json.dumps(event));self.assertNotIn('DWARF_mini',event['path'])

    def test_station_scan_requires_owned_connected_managed_home_and_never_scans_ap(self):
        net=LinuxNetwork();device={'name':'wlan2'}
        for mode,connected,hotspot,expected in [('managed',True,'wlan1',True),('AP',True,'wlan1',False),('managed',False,'',False),('managed',True,'wlan2',False)]:
            with patch('wifi_bridge.network.run',return_value='type '+mode),patch.object(net,'connected',return_value=connected),patch.object(net,'_hotspot_device',return_value=hotspot):
                self.assertEqual(net.scan_station(device,{}),expected)
        with patch('wifi_bridge.network.run',return_value='') as command:
            net.cached_scan('wlan2')
            self.assertEqual(command.call_args.args[0][-2:],['--rescan','no'])

    def test_lookup_is_bounded_and_background_scan_respects_operations_and_update_gate(self):
        self.configured();self.controller.cycle(0);c=self.controller.candidates
        for i in range(80):c.remember(scan_candidate(dict(ROW,ssid='S50_DEMO'+str(i),bssid=f'02:00:00:77:88:{i:02x}'),100),persist=False)
        self.assertEqual(len(c.observations),64)
        with self.controller.net_lock:
            with self.assertRaises(ApiError):c.run({'mode':'wifi'},cached=True)
        self.assertFalse(self.controller.discovery_pending)
