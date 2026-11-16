"""Native facade projections and real guarded demo operations, without hardware."""
import copy
import itertools
import json
import threading
import time
from collections import deque
from pathlib import Path
from unittest.mock import patch
import unittest
from test_bridge import Fixture
import test_api
from wifi_bridge.api import ApiError, MobileAPI
from wifi_bridge.instruments import catalog, endpoint
from helpers.contract_schema import validate


class ProjectionTests(unittest.TestCase):
    def test_link_service_cross_product_and_allowlist(self):
        states = [('connected', True, True, True, None), ('connecting', True, False, True, None),
                  ('available', False, False, True, 0), ('saved', False, False, True, 120),
                  ('disabled', False, False, False, None)]
        spec = json.loads(Path('wifi_bridge/static/openapi.json').read_text())
        for (link, enabled, connected, permitted, age), service in itertools.product(states, ('ready','not_answering','not_probed','unknown')):
            with self.subTest(link=link, service=service):
                profile = dict(id='scope', kind='seestar', name='Synthetic', enabled=enabled, client_connect=permitted,
                               ssid='private-fixture', bssid='', password='private-fixture', target_ip='198.51.100.1')
                snapshot = {'config': {'telescopes': [profile]}, 'telescopes': [{'id':'scope', 'connected':connected,
                    'state':'connecting' if link=='connecting' else 'waiting','service':{'state':service},'relay_ready':False}]}
                before=copy.deepcopy(snapshot)
                value=catalog(snapshot,observations=[{'ssid':'private-fixture','scan_source':'fresh'}],observation_age=age)[0]
                self.assertEqual((value['link'],value['service']),(link,service));self.assertEqual(snapshot,before)
                self.assertNotEqual(value['kind'],'camera')
                validate(value,spec['components']['schemas']['NativeInstrument'],spec)
                for secret in ('ssid','bssid','password','private-fixture'):self.assertNotIn(secret,json.dumps(value))
        profile.update(client_connect=False,enabled=True)
        snapshot['telescopes'][0]['connected']=True
        self.assertEqual(catalog(snapshot)[0]['link'],'connected')
        profile.update(enabled=False,client_connect=True)
        self.assertEqual(catalog(snapshot)[0]['link'],'connected')
        snapshot['telescopes'][0]['connected']=False
        self.assertEqual(catalog(snapshot,observations=[{'ssid':'private-fixture','scan_source':'cached'}],observation_age=0)[0]['link'],'saved')

    def test_paths_order_and_route_requirement(self):
        profile={'id':'scope','kind':'seestar','enabled':True,'target_ip':'198.51.100.1'}
        row={'id':'scope','connected':True,'seestar_proxy':True,'management_paths':[
            {'role':'home','host':'192.0.2.10','forwarding_ready':True},
            {'role':'ap','host':'192.168.77.1','forwarding_ready':True}]}
        snapshot={'config':{'telescopes':[profile]},'telescopes':[row]}
        for path,host in [('home','192.0.2.10'),('hotspot','192.168.77.1'),('other','192.0.2.10')]:
            result=endpoint(snapshot,'scope',path)
            self.assertEqual(result['primary']['host'],host);self.assertFalse(result['requires_client_route']);self.assertEqual(len(result['alternatives']),1)
        row['seestar_proxy']=False
        result=endpoint(snapshot,'scope','home');self.assertEqual(result['mode'],'routed');self.assertTrue(result['requires_client_route'])
        profile['enabled']=False;self.assertIsNone(endpoint(snapshot,'scope'))


class NativeActionTests(Fixture):
    def setUp(self):
        super().setUp();self.net.radios=3;self.devices=self.net.devices();self.configured();self.controller.cycle(0)
        self.api=MobileAPI(self.controller);self.facade=self.api.instruments
        self.api.action('primary','disconnect');self.controller.cycle(1)
        self.api.update('telescopes',{'id':'native-one','name':'Native fixture','kind':'seestar','target_ip':'10.2.0.1',
                                    'ssid':'Telescope-DEMO','open':True},create=True)
        self.ops=self.controller.operations
        self.addCleanup(self.finish)

    def finish(self):
        if self.ops.thread:self.ops.thread.join(10)

    def connect(self, **data):
        return self.facade.connect('native-one',data,'"'+self.api.revision()+'"','paired-client',time.monotonic()+3600,'home')

    def test_saved_target_own_lease_does_not_reserve_its_only_free_radio(self):
        self.net.radios=2;self.devices=self.net.devices();self.controller.state['devices']=self.devices
        self.store.config['telescopes'][1]['mac']=self.devices[0]['mac']
        self.store.config['telescopes'][1]['target_ip']='10.0.0.1'
        code,value=self.connect(wait_seconds=2)
        self.assertEqual(code,200,value)
        self.assertEqual(self.store.config['telescopes'][1]['mac'],self.devices[0]['mac'])

    def test_client_consent_independent_of_global_automation(self):
        for global_auto, permitted in itertools.product((True,False),repeat=2):
            with self.subTest(global_auto=global_auto,permitted=permitted):
                self.store.config['auto_connect']=global_auto
                profile=self.store.config['telescopes'][1];profile['client_connect']=permitted
                if not permitted:
                    with self.assertRaises(ApiError) as error:self.connect()
                    self.assertEqual(error.exception.code,'CLIENT_CONNECT_DISABLED')
                else:
                    code,value=self.connect(wait_seconds=2)
                    self.assertEqual(code,200,value);self.assertEqual(value['endpoint']['instrument_id'],'native-one')
                    self.facade.action('native-one','disconnect',{'lease_id':value['lease']['id']},'paired-client');self.controller.cycle()
        self.assertFalse(self.store.config['telescopes'][1]['auto_eligible'])

    def test_lease_lifecycle_fake_clock_renew_release_expiry_and_restore(self):
        clock=[100.0];p=self.controller.protection;p.clock=lambda:clock[0];events=[];p.lease_hook=lambda *args:events.append(args)
        code,value=self.connect(wait_seconds=2,lease_seconds=120);self.assertEqual(code,200,value)
        lease=value['lease']['id'];self.assertEqual(value['lease']['expires_in'],120)
        clock[0]=160;self.controller.cycle();p.prune();self.assertEqual(len(events),1);self.assertEqual(events[0][0],'lease.expiring')
        renewed=self.facade.action('native-one','lease',{'lease_id':lease,'lease_seconds':3600},'paired-client');self.assertEqual(renewed['expires_in'],3600)
        with self.assertRaises(ApiError) as error:self.facade.action('native-one','disconnect',{},'another-client')
        self.assertEqual(error.exception.code,'LEASED_BY_OTHER_CLIENT')
        # Losing the association retains enabled intent, using ordinary controller restoration.
        profile=self.store.config['telescopes'][1];device=next(d for d in self.devices if d['mac']==profile['mac'])
        self.net.disconnect("telescope-native-one");self.controller.attempts.clear();self.controller.cycle()
        self.assertTrue(next(r for r in self.controller.state['telescopes'] if r['id']=='native-one')['connected'])
        clock[0]=3761;p.prune();self.assertFalse(p.leases);self.assertEqual(events[-1][0],'lease.ended')
        code,value=self.connect();self.assertEqual(code,200)
        self.facade.action('native-one','release',{'lease_id':value['lease']['id']},'paired-client')
        self.assertTrue(self.store.config['telescopes'][1]['enabled'])

    def test_owner_foreign_lease_preconditions_and_validation(self):
        profile=self.store.config['telescopes'][1]
        for expected,code in [(None,'PRECONDITION_REQUIRED'),('*','PRECONDITION_REQUIRED'),('"old"','PRECONDITION_FAILED')]:
            with self.assertRaises(ApiError) as error:self.facade.connect('native-one',{},expected,'paired-client',9999,'home')
            self.assertEqual(error.exception.code,code)
        profile['owner_locked']=True
        with self.assertRaises(ApiError) as error:self.connect()
        self.assertEqual(error.exception.code,'INSTRUMENT_PROTECTED');profile['owner_locked']=False
        self.controller.protection.native_create('native-one','other-client',900)
        with self.assertRaises(ApiError) as error:self.connect()
        self.assertEqual(error.exception.code,'LEASED_BY_OTHER_CLIENT')
        self.controller.protection.leases.clear()
        for data in ({'wait_seconds':True},{'wait_seconds':26},{'lease_seconds':59},{'lease_seconds':3601},{'allow_replace':'scope'},{'password':'discard'}):
            with self.assertRaises(ApiError) as error:self.connect(**data)
            self.assertEqual(error.exception.status,400)

    def test_releasing_own_lease_preserves_a_coexisting_legacy_reservation(self):
        code,value=self.connect(wait_seconds=2);self.assertEqual(code,200)
        protection=self.controller.protection
        foreign=protection.create('native-one',{'ttl_seconds':120},'legacy-client','"'+self.api.revision()+'"',self.api.revision)
        own=value['lease']['id']
        self.facade.action('native-one','lease',{'lease_id':own,'lease_seconds':900},'paired-client')
        self.facade.action('native-one','release',{'lease_id':own},'paired-client')
        self.assertNotIn(own,protection.leases);self.assertIn(foreign['id'],protection.leases)

    def test_allocation_rate_limit_rolls_back_new_reservation(self):
        self.ops.clients['paired-client']={'expires':time.monotonic()+3600,'times':deque([self.ops.clock()]*5)}
        with self.assertRaises(ApiError) as error:self.connect()
        self.assertEqual(error.exception.code,'CONNECT_LIMITED');self.assertFalse(self.controller.protection.leases)

    def test_native_operation_does_not_expose_selector(self):
        code,value=self.connect();self.assertEqual(code,202);self.finish()
        result=self.facade.operation(value['operation']['id'],'hotspot')
        self.assertEqual(result['state'],'succeeded');self.assertNotIn('physical_selection',result)
        self.assertNotIn('bssid',json.dumps(result));self.assertEqual(result['endpoint']['request_path'],'hotspot')

    def test_replacement_consent_and_protection_matrix(self):
        self.net.radios=2;self.controller.update(devices=self.net.devices())
        target=self.store.config['telescopes'][1];target['target_ip']='10.0.0.1'
        victim=self.store.config['telescopes'][0]
        self.net.sample_activity=lambda **_:None
        self.net.selection_activity=lambda *_:{'state':'quiet','sample_age_seconds':0,'quiet_seconds':120,'cooldown_remaining':0,'last_activity_age_seconds':180}
        for automatic,permitted,replaceable,allowed,locked,leased in itertools.product((False,True),repeat=6):
            with self.subTest(automatic=automatic,permitted=permitted,replaceable=replaceable,allowed=allowed,locked=locked,leased=leased):
                self.controller.protection.leases.clear()
                # Reset only synthetic fixture intent between independent admissions.
                target.update(enabled=False,client_connect=permitted)
                victim.update(enabled=True,auto_replaceable=replaceable,owner_locked=False)
                self.store.config['auto_connect']=automatic
                self.ops.started=self.ops.last_replacement=0;self.ops.cooldowns.clear();self.ops.negative.clear()
                self.controller.cycle()
                victim['owner_locked']=locked
                if leased:
                    victim['owner_locked']=False
                    self.controller.protection.native_create('primary','other-client',900)
                    victim['owner_locked']=locked
                accepted=permitted and replaceable and allowed and not locked and not leased
                if accepted:
                    code,value=self.connect(wait_seconds=2,allow_replace=['primary'])
                    self.assertEqual(code,200,value)
                    self.assertFalse(self.store.config['telescopes'][0]['enabled'])
                else:
                    with self.assertRaises(ApiError) as error:self.connect(allow_replace=['primary'] if allowed else [])
                    self.assertEqual(error.exception.code,'CLIENT_CONNECT_DISABLED' if not permitted else 'NO_FREE_RADIO')
                    self.assertTrue(self.store.config['telescopes'][0]['enabled'])
                # Successful scheduler commits replace profile dictionaries.
                victim,target=self.store.config['telescopes']

    def test_waiter_limit_returns_accepted_immediately(self):
        for _ in range(4):self.assertTrue(self.facade.waiters.acquire(False))
        entered=threading.Event();resume=threading.Event();run=self.ops.run
        def delayed(ident):entered.set();resume.wait(3);run(ident)
        with patch.object(self.ops,'run',side_effect=delayed):
            started=time.monotonic();code,value=self.connect(wait_seconds=25)
            self.assertEqual(code,202);self.assertLess(time.monotonic()-started,1);resume.set();self.finish()
        for _ in range(4):self.facade.waiters.release()


class NativeHTTPTests(Fixture):
    def setUp(self):
        super().setUp();self.configured();self.controller.cycle(0)
        from wifi_bridge.web import WebServer
        self.server=WebServer(('127.0.0.1',0),self.controller)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.token=''

    request=test_api.APITests.request
    login=test_api.APITests.login

    def test_client_connect_migration_edit_revision_and_invalid_lease_body(self):
        self.login()
        code,listing,_=self.request('GET','/api/v1/instruments')
        self.assertTrue(listing['instruments'][0]['client_connect'])
        self.assertEqual(self.request('PATCH','/api/v1/telescopes/primary',{'client_connect':False},{'If-Match':'"stale"'})[0],412)
        self.assertEqual(self.request('PATCH','/api/v1/telescopes/primary',{'client_connect':False},{'If-Match':'"'+listing['config_revision']+'"'})[0],200)
        self.assertFalse(self.request('GET','/api/v1/instruments/primary')[1]['client_connect'])
        for lease_id in (None,[],{},True,'x'*129):
            self.assertEqual(self.request('POST','/api/v1/instruments/primary/disconnect',{'lease_id':lease_id})[0],400)

    def test_pair_identity_rate_accounting_survives_authentication(self):
        self.login()
        self.server.sessions[self.token]['client_id']='paired-fixture'
        self.controller.operations.clients['paired-fixture']={'expires':time.monotonic()+300,'times':deque([time.monotonic()]*5)}
        self.assertEqual(self.request('GET','/api/v1/instruments')[0],200)
        self.assertEqual(len(self.controller.operations.clients['paired-fixture']['times']),5)

    def test_scope_pair_identity_revision_and_socket_ingress(self):
        self.login();admin=self.token
        code,client,_=self.request('POST','/api/v1/auth/clients',{'name':'Native fixture','scopes':['read','connect']});self.assertEqual(code,201)
        code,token,_=self.request('POST','/api/v1/auth/token',{'client_id':client['client_id'],'client_secret':client['client_secret']});self.assertEqual(code,200);self.token=token['access_token']
        code,listing,_=self.request('GET','/api/v1/instruments');self.assertEqual(code,200)
        revision={'If-Match':'"'+listing['config_revision']+'"'}
        code,ready,_=self.request('POST','/api/v1/instruments/primary/connect',{},revision);self.assertEqual(code,200,ready)
        self.assertEqual(ready['endpoint']['request_path'],'other')
        self.assertEqual(next(iter(self.controller.protection.leases.values()))['client'],client['client_id'])
        self.server.sessions[self.token]['scopes']=['read']
        self.assertEqual(self.request('POST','/api/v1/instruments/primary/connect',{},revision)[0],403)
        self.server.sessions[self.token]['scopes']=['connect']
        self.assertEqual(self.request('GET','/api/v1/instruments')[0],403)
        self.server.sessions[self.token]['scopes']=['read','connect']
        self.controller.state['management']['paths'][0]['ip']='127.0.0.1'
        self.assertEqual(self.request('GET','/api/v1/instruments/primary/endpoint')[1]['request_path'],'home')
        self.assertEqual(self.request('POST','/api/v1/instruments/primary/lease',{'lease_id':ready['lease']['id'],'lease_seconds':3600})[0],200)
        self.assertEqual(self.request('POST','/api/v1/instruments/primary/disconnect',{'lease_id':ready['lease']['id']})[0],200)
