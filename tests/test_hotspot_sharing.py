"""Explicit sharing policy, scoped firewall/DNS transactions and management safety."""
import copy
import json
from pathlib import Path
import tempfile
from unittest.mock import patch, PropertyMock

from test_bridge import Fixture
from wifi_bridge.api import ApiError, MobileAPI
from wifi_bridge.config import Invalid, public_config, validate_config
from wifi_bridge.network import LinuxNetwork, NetworkError


class SharingConfigTests(Fixture):
    def setUp(self):
        super().setUp();self.configured();self.api=MobileAPI(self.controller)

    def revision(self):return '"'+self.api.revision()+'"'

    def test_default_legacy_omission_and_public_roundtrip_preserve_credentials(self):
        legacy=copy.deepcopy(self.store.config);legacy.pop('connection_mode',None);legacy.pop('hotspot_private_lan',None)
        self.store.config=legacy
        public=public_config(legacy)
        self.assertEqual(public['connection_mode'],'private');self.assertFalse(public['hotspot_private_lan'])
        self.api.update('settings',{'country':'FR'})
        self.assertEqual(self.store.config['connection_mode'],'private');self.assertFalse(self.store.config['hotspot_private_lan'])
        self.api.update('settings',{'connection_mode':'hotspot_shared','hotspot_private_lan':True},expected=self.revision())
        self.api.update('settings',{'hostname':'bridge-test'})
        self.assertEqual(self.store.config['connection_mode'],'hotspot_shared');self.assertTrue(self.store.config['hotspot_private_lan'])
        public=public_config(self.store.config)
        value=validate_config(public,self.store.config,self.devices)
        self.assertEqual(value['home']['password'],legacy['home']['password'])
        self.assertEqual(value['telescopes'][0]['password'],legacy['telescopes'][0]['password'])
        self.assertNotIn(legacy['home']['password'],json.dumps(public))

    def test_strict_mode_private_flag_and_actual_revision(self):
        for key,bad in [('connection_mode',None),('connection_mode',True),('connection_mode','shared'),
                        ('connection_mode',[]),('hotspot_private_lan',1),('hotspot_private_lan','true')]:
            with self.subTest(key=key,bad=bad),self.assertRaises(Invalid):self.api.update('settings',{key:bad},expected=self.revision())
        for expected,code in [(None,428),('*',428),('"*"',428),('"stale"',412)]:
            with self.assertRaises(ApiError) as error:self.api.update('settings',{'connection_mode':'hotspot_shared'},expected=expected)
            self.assertEqual(error.exception.status,code)
        self.assertEqual(self.store.config['connection_mode'],'private')

    def test_protected_connection_and_pending_operation_block_mode_changes(self):
        self.store.config['telescopes'][0]['owner_locked']=True
        with self.assertRaises(ApiError) as error:self.api.update('settings',{'connection_mode':'hotspot_shared'},expected=self.revision())
        self.assertEqual(error.exception.code,'CONNECTION_PROTECTED')
        self.store.config['telescopes'][0]['owner_locked']=False
        with patch.object(type(self.controller.operations),'pending',new_callable=PropertyMock,return_value=True):
            with self.assertRaises(ApiError):self.api.update('settings',{'connection_mode':'hotspot_shared'},expected=self.revision())
        self.assertEqual(self.store.config['connection_mode'],'private')

    def test_isolated_requests_ap_keeps_home_and_existing_telescope_and_status_is_honest(self):
        self.net.radios=3;self.controller.update(devices=self.net.devices());self.controller.cycle(0)
        self.api.update('settings',{'connection_mode':'hotspot_isolated'},expected=self.revision());self.controller.cycle(10)
        self.assertEqual(self.controller.state['management']['mode'],'dual')
        self.assertTrue(self.controller.state['telescopes'][0]['connected'])
        self.assertFalse(self.store.config['hotspot']['enabled'])
        result=self.api.read('settings');self.assertFalse(result['network_access']['configured'])
        self.assertFalse(result['network_access']['internet_verified'])
        self.assertEqual(result['settings']['connection_mode'],'hotspot_isolated')

    def test_failed_reconcile_invalidates_cached_sharing_availability(self):
        self.controller.state['network_access']={'mode':'hotspot_shared','available':True,'reason':'UPLINK_CONFIGURED'}
        self.store.config['connection_mode']='hotspot_shared'
        with patch.object(self.net,'firewall',side_effect=NetworkError('synthetic planning failure')):
            with self.assertRaises(NetworkError):self.controller.cycle(10)
        access=self.controller.status()['network_access']
        self.assertFalse(access['available']);self.assertEqual(access['reason'],'PLANNING_FAILED')
        self.assertTrue(access['configured']);self.assertFalse(access['internet_verified'])



class SharingNetworkTests(Fixture):
    def setUp(self):
        super().setUp();self.net=LinuxNetwork(self.tmp.name)
        self.home={'name':'home'};self.ap={'name':'ap'};self.scope={'name':'scope'}
        self.addresses={'home':'192.168.12.2/24','ap':'192.168.77.1/24','scope':'10.0.0.2/24'}
        self.row={'profile_id':'primary','device':self.scope,'target':'10.0.0.1','legacy_ports':True,'kind':'vespera'}
        self.nm='GENERAL.CONNECTION:wifi-bridge-home\nIP4.GATEWAY:192.168.12.1\nIP4.DNS[1]:192.168.12.1'
        self.routes=[{'dst':'default','gateway':'192.168.12.1','dev':'home'}]
        self.calls=[]
        self.addCleanup(patch.stopall)
        patch.object(self.net,'address',side_effect=lambda d,**kw:self.addresses[d['name']]).start()
        patch.object(self.net,'configure_activity').start()
        patch('wifi_bridge.network.run',side_effect=self.run_command).start()

    def run_command(self,args,**kwargs):
        self.calls.append((args,kwargs))
        if args[0]=='nmcli':return self.nm
        if args[:4]==['ip','-j','-4','route']:return json.dumps(self.routes)
        return ''

    def apply(self,**kwargs):
        return self.net.firewall(home=self.home,setup=self.ap,telescopes=[self.row],hotspot_targets=['10.0.0.1'],**kwargs)

    def plans(self):return [kw['input'] for args,kw in self.calls if args==['nft','-f','-']]

    def test_sharing_explicit_scoped_nat_dns_and_telescope_paths_precede_restrictions(self):
        status=self.apply(sharing=True)
        self.assertTrue(status['available']);script=self.plans()[-1]
        self.assertIn('iifname "ap" oifname "home" ip saddr 192.168.77.0/24 masquerade',script)
        self.assertIn('iifname "home" oifname "ap" ip daddr 192.168.77.0/24 ct state established,related accept',script)
        self.assertNotIn('ct state established,related accept\niifname',script.split('chain forward')[0])
        self.assertLess(script.index('iifname "ap" oifname "scope"'),script.index('jump sharing_forward'))
        self.assertIn('192.168.12.0/24 } drop',script)
        self.assertIn('ip daddr 192.168.77.1 udp dport 53 drop',script)
        for plan in self.plans():self.assertIn('tcp dport { 8082, 8083 } dnat ip to 10.0.0.1',plan)
        dns=Path(self.tmp.name,'dnsmasq.conf').read_text()
        for line in ('port=53','listen-address=192.168.77.1','except-interface=lo','no-resolv','server=192.168.12.1@home','dhcp-option=6,192.168.77.1'):
            self.assertIn(line+'\n',dns)
        self.assertIn('dhcp-option=121,10.0.0.1/32,192.168.77.1,0.0.0.0/0,192.168.77.1',dns)
        self.assertFalse(any('conntrack' in args or args[:2]==['nft','flush'] for args,_ in self.calls))
        before=len(self.calls);self.apply(sharing=True)
        self.assertFalse(any(args[:2]==['systemctl','restart'] or args==['nft','-f','-'] for args,_ in self.calls[before:]))

    def test_default_off_and_private_lan_permission_is_separate_with_return_only(self):
        self.apply();script=self.plans()[-1]
        self.assertNotIn('iifname "ap" oifname "home"',script)
        self.assertIn('port=0\n',Path(self.tmp.name,'dnsmasq.conf').read_text())
        self.apply(sharing=True,private_lan=True);script=self.plans()[-1]
        self.assertIn('ip daddr 192.168.12.0/24 ct state new,established,related accept',script)
        self.assertNotIn('iifname "home" oifname "ap" ct state new',script)
        self.apply(sharing=False);script=self.plans()[-1]
        self.assertNotIn('oifname "home" ip saddr 192.168.77.0/24 masquerade',script)

    def test_uplink_loss_invalid_dns_default_and_overlap_fail_closed_keep_instrument_plan(self):
        self.apply(sharing=True)
        for change in ('dns','route','owner','overlap'):
            old_nm=self.nm;old_routes=self.routes;old_address=self.addresses['home']
            if change=='dns':self.nm=self.nm.replace('192.168.12.1\nIP4.DNS[1]:192.168.12.1','192.168.12.1\nIP4.DNS[1]:127.0.0.1')
            if change=='route':self.routes=[]
            if change=='owner':self.nm=self.nm.replace('wifi-bridge-home','external')
            if change=='overlap':self.addresses['home']='192.168.77.2/24'
            with self.subTest(change=change):
                if change=='overlap':
                    with self.assertRaises(NetworkError):self.apply(sharing=True)
                    self.assertIn('flush chain ip wifi_bridge sharing_forward',self.plans()[-1])
                    self.assertNotIn('flush ruleset',self.plans()[-1])
                    self.assertIsNone(self.net.sharing_active)
                else:
                    status=self.apply(sharing=True);self.assertFalse(status['available'])
                    self.assertNotIn('oifname "home" ip saddr 192.168.77.0/24 masquerade',self.plans()[-1])
                    self.assertIn('ip daddr 10.0.0.1 masquerade',self.plans()[-1])
            self.nm,self.routes,self.addresses['home']=old_nm,old_routes,old_address
        self.routes=[];self.apply(sharing=True)
        self.assertFalse(any(args[:2] in (['systemctl','stop'],['ip','link']) for args,_ in self.calls))

    def test_failed_dns_restart_withdraws_sharing_and_retries_without_ap_restart(self):
        self.apply(sharing=True);self.calls=[]
        self.nm=self.nm.replace('IP4.DNS[1]:192.168.12.1','IP4.DNS[1]:9.9.9.9')
        original=self.run_command;failed=[False]
        def run(args,**kwargs):
            result=original(args,**kwargs)
            if args==['systemctl','restart','wifi-bridge-dhcp'] and not failed[0]:
                failed[0]=True;raise NetworkError('synthetic service failure')
            return result
        with patch('wifi_bridge.network.run',side_effect=run):self.assertFalse(self.apply(sharing=True)['available'])
        self.assertNotIn('oifname "home" ip saddr 192.168.77.0/24 masquerade',self.plans()[-1])
        self.assertTrue(self.apply(sharing=True)['available'])
        self.assertFalse(any('wifi-bridge-ap' in args for args,_ in self.calls))


class SharingHTTPTests(Fixture):
    from test_api import APITests
    request=APITests.request
    login=APITests.login

    def setUp(self):
        super().setUp();self.configured();self.controller.cycle(0)
        from wifi_bridge.web import WebServer
        import threading
        self.server=WebServer(('127.0.0.1',0),self.controller)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown);self.token=''

    def test_native_auth_revision_protection_and_browser_csrf_parity(self):
        body={'connection_mode':'hotspot_shared'}
        self.assertEqual(self.request('PATCH','/api/v1/settings',body)[0],401)
        self.login()
        self.assertEqual(self.request('PATCH','/api/v1/settings',body)[0],428)
        self.assertEqual(self.request('PATCH','/api/v1/settings',body,{'If-Match':'*'})[0],428)
        revision=self.server.api.revision()
        code,result,_=self.request('PATCH','/api/v1/settings',body,{'If-Match':'"'+revision+'"'})
        self.assertEqual(code,200);self.assertEqual(result['settings']['connection_mode'],'hotspot_shared')
        self.assertTrue(result['network_access']['configured']);self.assertFalse(result['network_access']['available'])
        self.assertEqual(self.request('PATCH','/api/v1/settings',{'hotspot_private_lan':True},{'If-Match':'"'+revision+'"'})[0],412)
        origin='http://127.0.0.1:'+str(self.server.server_port)
        _,_,cookie=self.request('POST','/api/login',{'password':'local-admin-test-password'},{'Origin':origin})
        browser={'Cookie':cookie.split(';')[0],'Origin':origin,'Authorization':''}
        _,status,_=self.request('GET','/api/status',headers=browser)
        payload=dict(status['config'],connection_mode='hotspot_isolated',config_revision=status['config_revision'])
        self.assertEqual(self.request('POST','/api/config',payload,browser)[0],403)
        browser['X-CSRF-Token']=status['csrf']
        self.assertEqual(self.request('POST','/api/config',payload,browser)[0],200)
        self.assertEqual(self.store.config['connection_mode'],'hotspot_isolated')
