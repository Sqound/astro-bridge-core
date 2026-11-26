"""Native Bearer API parity, resource isolation and browser compatibility."""
import http.client
import json
import threading
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.api import MobileAPI
from wifi_bridge.web import WebServer


class APITests(Fixture):
    def test_wired_home_drives_discovery_authenticated_endpoints_and_failover(self):
        wired={'name':'enp1s0','mac':'02:00:00:20:00:01','kind':'ethernet','label':'enp1s0',
               'usb':False,'address':'192.168.20.2/24'}
        original_address=self.net.address;original_connected=self.net.connected
        self.net.wired_home=lambda:wired
        self.net.address=lambda device,**kw:device['address'] if device.get('kind')=='ethernet' else original_address(device,**kw)
        self.net.connected=lambda role,device,config=None:role=='home' if device and device.get('kind')=='ethernet' else original_connected(role,device,config)
        saved=json.dumps(self.store.config,sort_keys=True)
        self.controller.cycle(10)
        self.assertEqual(self.controller.state['home_ip'],'192.168.20.2')
        self.assertEqual(self.controller.state['management']['paths'][0]['interface'],'enp1s0')
        self.assertNotIn('enp1s0',[d['name'] for d in self.controller.state['devices']])
        self.assertEqual(self.server.api.info('192.168.20.2')['request_path'],'home')
        host={'Host':'192.168.20.2:'+str(self.server.server_port)}
        self.assertEqual(self.request('GET','/api/v1/endpoints',headers=host)[0],401)
        self.login()
        code,body,_=self.request('GET','/api/v1/endpoints',headers=host)
        self.assertEqual(code,200)
        self.assertIn('192.168.20.2',json.dumps(body))
        self.assertEqual(self.controller.retained_forwarding()['home']['kind'],'ethernet')
        self.assertEqual(self.controller.current_management()[0]['device']['kind'],'ethernet')
        self.assertEqual(json.dumps(self.store.config,sort_keys=True),saved)
        self.net.wired_home=lambda:None
        self.controller.cycle(20)
        self.assertEqual(self.controller.state['home_ip'],'192.168.1.42')
        self.assertEqual(self.request('GET','/api/v1/endpoints',headers=host)[0],421)

    def test_event_wait_bearer_cookie_validation_wakeup_and_shutdown(self):
        self.login()
        store=self.controller.event_store
        for value in ('-1','26','1.2','true','','0&wait=1','1&unexpected=2'):
            self.assertEqual(self.request('GET','/api/v1/events?wait='+value)[0],400)
        self.server.sessions[self.token]['scopes']=['read']
        origin='http://127.0.0.1:'+str(self.server.server_port)
        _,_,cookie=self.request('POST','/api/login',{'password':'local-admin-test-password'},{'Origin':origin})
        browser={'Cookie':cookie.split(';')[0],'Authorization':''}
        self.assertEqual(self.request('GET','/api/events?wait=0',headers=browser)[0],200)
        pages=[]
        def waiter(path,headers): pages.append(self.request('GET',path,headers=headers))
        suffix='?after='+str(store.sequence)+'&boot_id='+store.boot_id+'&wait=25'
        thread=threading.Thread(target=waiter,args=('/api/events'+suffix,browser));thread.start()
        import time
        deadline=time.monotonic()+2
        while not store.waiters and time.monotonic()<deadline: threading.Event().wait(.005)
        self.assertEqual(store.waiters,1)
        store.native('instrument.restored','primary');thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(pages[0][0],200)
        self.assertEqual(pages[0][1]['events'][-1]['type'],'instrument.restored')
        suffix='?after='+str(store.sequence)+'&boot_id='+store.boot_id+'&wait=25'
        thread=threading.Thread(target=waiter,args=('/api/v1/events'+suffix,{}));thread.start()
        deadline=time.monotonic()+2
        while not store.waiters and time.monotonic()<deadline: threading.Event().wait(.005)
        self.assertEqual(store.waiters,1)
        self.server.server_close();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(pages[-1][1]['events'],[])

    def test_endpoint_change_hydrates_current_ingress_and_never_persists_addresses(self):
        self.login()
        snapshot=self.controller.status()
        profile=snapshot['config']['telescopes'][0];profile.update(kind='synscan',enabled=True,isolated=True)
        row=snapshot['telescopes'][0]
        row.update(connected=True,target_ip=profile['target_ip'],client_endpoint={'alternatives':[
            {'role':role,'host':host,'port':21180,'transport':'udp','ready':True}
            for role,host in [('home','192.0.2.10'),('ap','192.168.77.1')]]})
        store=self.controller.event_store;cursor=store.sequence
        store.native('instrument.endpoint_changed','primary')
        for role,expected in [('home','192.0.2.10'),('ap','192.168.77.1')]:
            snapshot['management']['paths']=[{'role':role,'ip':'127.0.0.1'}]
            self.controller.state['management']['paths']=snapshot['management']['paths']
            with patch.object(self.controller,'status',return_value=snapshot):
                code,page,_=self.request('GET','/api/v1/events?after='+str(cursor))
            self.assertEqual(code,200)
            endpoint=page['events'][0]['endpoint']
            self.assertEqual(endpoint['primary']['host'],expected)
            self.assertEqual(endpoint['request_path'],'hotspot' if role=='ap' else role)
        row['connected']=False
        with patch.object(self.controller,'status',return_value=snapshot):
            self.assertIsNone(self.request('GET','/api/v1/events?after='+str(cursor))[1]['events'][0]['endpoint'])
        store.last_write=None;store.flush()
        self.assertNotIn('endpoint',json.dumps(store.events[-1]).replace('instrument.endpoint_changed','marker'))
        for address in ('192.0.2.10','192.168.77.1'):self.assertNotIn(address,store.path.read_text())

    def test_system_metrics_admin_scope_cached_read_and_no_payloads(self):
        path = '/api/v1/system/metrics'
        self.assertEqual(self.request('GET', path)[0], 401)
        self.login()
        code, first, _ = self.request('GET', path)
        self.assertEqual(code, 200)
        self.assertEqual(first['relay_coverage'], 'userspace_only')
        self.assertEqual(self.request('GET', path)[1]['sample_sequence'], first['sample_sequence'])
        self.assertEqual(self.request('POST', path, {})[0], 405)
        for scope in ('read', 'connect', 'telemetry', 'camera'):
            self.server.sessions[self.token]['scopes'] = [scope]
            self.assertEqual(self.request('GET', path)[0], 403)
        self.assertNotIn('local-admin-test-password', json.dumps(first))

    def test_time_hint_read_scope_validation_rate_limit_and_synchronized_ignore(self):
        from wifi_bridge.clock import Clock, utc
        self.controller.event_store.clock = Clock(self.store.path.with_name('clock.json'), sync=lambda: False)
        path = '/api/v1/system/time-hint'
        self.assertEqual(self.request('GET', '/api/v1/system/time')[0], 401)
        self.assertEqual(self.request('POST', path, {})[0], 401)
        self.login()
        self.server.sessions[self.token]['scopes'] = ['read']
        code, initial, _ = self.request('GET', '/api/v1/system/time')
        self.assertEqual(code, 200)
        self.assertEqual(initial['clock_status'], 'unverified')
        self.assertEqual(initial['last_sync']['kind'], 'never')
        self.assertEqual(self.request('POST', path, {})[0], 400)
        self.assertEqual(self.request('POST', path, 'null', raw=True)[0], 400)
        hint = {'utc_iso': utc(self.controller.event_store.clock.now() + 60), 'uncertainty_ms': 500}
        code, result, _ = self.request('POST', path, hint)
        self.assertEqual(code, 200)
        self.assertEqual(result['source'], 'client_hint')
        self.assertEqual((result['last_sync']['kind'], result['last_sync']['this_run']), ('client_hint', True))
        self.assertAlmostEqual(result['offset_ms'], 60000, delta=50)
        self.assertEqual(self.request('POST', path, hint)[0], 429)
        self.server.sessions[self.token]['client_id'] = 'synthetic-paired-client'
        self.assertEqual(self.request('POST', path, hint)[0], 200)
        self.login()
        self.server.sessions[self.token].update(scopes=['read'], client_id='synthetic-paired-client')
        self.assertEqual(self.request('POST', path, hint)[0], 429)
        self.server.sessions[self.token]['scopes'] = ['camera']
        self.assertEqual(self.request('GET', '/api/v1/system/time')[0], 403)
        self.assertEqual(self.request('POST', path, hint)[0], 403)
        self.server.sessions[self.token]['scopes'] = ['read']
        self.controller.event_store.clock = Clock(self.store.path.with_name('clock.json'), sync=lambda: True)
        self.assertEqual(self.request('POST', path, hint)[1]['source'], 'ntp')
        synced = self.request('GET', '/api/v1/system/time')[1]['last_sync']
        self.assertEqual((synced['kind'], synced['age_seconds'], synced['this_run']), ('network', 0, True))
        self.assertEqual(self.request('POST', path, hint)[1]['source'], 'ntp')
        self.server.sessions[self.token]['scopes'] = ['admin']
        self.assertEqual(self.request('POST', '/api/v1/system/time', {})[0], 405)

    def test_endpoint_inventory_auth_scope_profile_and_cookie_parity(self):
        path = '/api/v1/endpoints'
        self.assertEqual(self.request('GET', path)[0], 401)
        self.assertEqual(self.request('GET', '/api/endpoints')[0], 401)
        self.login()
        with patch.object(self.net, 'probe', side_effect=AssertionError('GET must never probe')):
            code, result, _ = self.request('GET', path)
        self.assertEqual(code, 200)
        self.assertEqual(set(result), {'api_version', 'config_revision', 'boot_id', 'endpoints'})
        self.assertEqual(result['boot_id'], self.controller.event_store.boot_id)
        self.assertEqual(result['config_revision'], self.server.api.revision())
        self.assertEqual(self.request('GET', path + '/primary')[1], result)
        self.assertEqual(self.request('GET', path + '/missing')[0], 404)
        self.assertEqual(self.request('POST', path, {})[0], 405)
        self.server.sessions[self.token]['scopes'] = ['read']
        self.assertEqual(self.request('GET', path)[0], 200)
        self.server.sessions[self.token]['scopes'] = ['camera']
        code, error, _ = self.request('GET', path)
        self.assertEqual(code, 403); self.assertEqual(error['error']['code'], 'INSUFFICIENT_SCOPE')
        origin = 'http://127.0.0.1:' + str(self.server.server_port)
        _, _, cookie = self.request('POST', '/api/login', {'password': 'local-admin-test-password'}, {'Origin': origin})
        browser = {'Cookie': cookie.split(';')[0], 'Authorization': ''}
        self.assertEqual(self.request('GET', '/api/endpoints', headers=browser)[1], result)
        self.assertEqual(self.request('GET', '/api/endpoints/primary', headers=browser)[1], result)
        self.assertEqual(self.request('GET', '/api/endpoints/missing', headers=browser)[0], 404)
        self.assertEqual(self.request('GET', path, headers=browser)[0], 401)

    def test_endpoint_inventory_preserves_shared_udp_refusals_on_both_paths(self):
        self.login()
        snapshot = self.controller.status()
        profile = snapshot['config']['telescopes'][0]
        profile.update(kind='synscan', enabled=True, isolated=True)
        row = next(row for row in snapshot['telescopes'] if row['id'] == profile['id'])
        row.update(connected=True, target_ip=profile['target_ip'], client_endpoint={'alternatives': [
            {'role': role, 'host': host, 'port': 21180, 'transport': 'udp', 'ready': True,
             'owner': {'address': '192.0.2.10:25000', 'since_seq': 2}, 'refused_clients': 9,
             'private_metadata': 'discard'}
            for role, host in [('home', '192.0.2.1'), ('ap', '192.168.77.1')]]})
        with patch.object(self.controller, 'status', return_value=snapshot):
            for path in ['/api/v1/endpoints', '/api/v1/endpoints/' + profile['id']]:
                with self.subTest(path=path):
                    code, body, _ = self.request('GET', path)
                    self.assertEqual(code, 200)
                    paths = body['endpoints'][0]['paths']
                    self.assertEqual([p['path'] for p in paths], ['home', 'hotspot'])
                    self.assertEqual([p['refused_clients'] for p in paths], [9, 9])
                    self.assertEqual(paths[0]['owner'], paths[1]['owner'])
                    self.assertNotIn('private_metadata', json.dumps(body))

    def test_retained_discovery_lookup_requires_auth_and_returns_only_onboarding_defaults(self):
        from wifi_bridge.candidates import scan_candidate
        row={'ssid':'DWARF_mini_SYNTHETIC','bssid':'02:00:00:77:88:11','signal':80,'frequency':2412,'security':'WPA2'}
        ident=self.controller.candidates.remember(scan_candidate(row,100))
        endpoint='/api/v1/discovery/'+ident
        self.assertEqual(self.request('GET',endpoint)[0],401)
        self.login();code,value,_=self.request('GET',endpoint)
        self.assertEqual(code,200);self.assertEqual(value['observation_id'],ident)
        self.assertEqual(value['suggested']['target_ip'],'192.168.88.1')
        self.assertEqual(value['suggested']['password'],'DWARF_12345678')
        self.assertNotIn('maison-test-pass',json.dumps(value));self.assertNotIn('vespera-test-pass',json.dumps(value))
        self.assertEqual(self.request('GET','/api/v1/discovery/'+'0'*32)[0],404)
        self.assertEqual(self.request('GET','/api/v1/discovery/not-an-id')[0],404)
        self.assertIn(self.request('POST',endpoint,{})[0],(404,405))

    def test_event_resource_auth_replay_validation_and_accepted_operation(self):
        self.assertEqual(self.request('GET', '/api/v1/events')[0], 401)
        self.login()
        code, page, _ = self.request('GET', '/api/v1/events')
        self.assertEqual(code, 200)
        cursor, boot = page['next_cursor'], page['boot_id']
        self.assertTrue(any(event['type'] == 'connected' for event in page['events']))
        self.assertNotIn('maison-test-pass', json.dumps(page))
        self.assertNotIn(self.devices[0]['mac'], json.dumps(page))
        self.assertEqual(self.request('GET', '/api/v1/events', headers={'Origin': 'http://evil.example'})[0], 403)
        for query in ('after=-1', 'after=true', 'after=' + '9' * 100, 'limit=0', 'limit=101',
                      'limit=+2', 'boot_id=../path', 'after=1&after=2', 'unknown=1'):
            code, error, _ = self.request('GET', '/api/v1/events?' + query)
            self.assertEqual(code, 400, query)
            self.assertEqual(error['error']['code'], 'INVALID_EVENT_CURSOR')
        self.assertEqual(self.request('POST', '/api/v1/events', {})[0], 405)
        code, accepted, _ = self.request('POST', '/api/v1/telescopes/primary/disconnect', {})
        self.assertEqual(code, 202); self.assertTrue(accepted['operation_id'])
        path = '/api/v1/events?after=' + str(cursor) + '&boot_id=' + boot
        changes = self.request('GET', path)[1]['events']
        self.assertEqual([event['type'] for event in changes], ['requested'])
        self.controller.cycle(5)
        changes = self.request('GET', path)[1]['events']
        self.assertIn('disconnected', [event['type'] for event in changes])
        self.assertEqual({event['operation_id'] for event in changes if 'operation_id' in event}, {accepted['operation_id']})
        sequence = self.controller.event_store.sequence
        self.request('GET', path); self.request('GET', path)
        self.assertEqual(self.controller.event_store.sequence, sequence)
        self.server.sessions[self.token]['expires'] = 0
        self.assertEqual(self.request('GET', path)[0], 401)

    def test_connect_can_move_radio_and_reject_invalid_or_stale_assignments(self):
        self.login()
        self.net.radios = 3
        devices = self.net.devices()
        self.controller.update(devices=devices)
        path = '/api/v1/telescopes/primary/connect'
        original = self.store.config['home'].copy()
        code, _, _ = self.request('POST', path, {'mac':devices[2]['mac'], 'expected_mac':devices[0]['mac']})
        self.assertEqual(code,202)
        self.assertEqual(self.store.config['telescopes'][0]['mac'],devices[2]['mac'])
        self.assertEqual(self.store.config['home'], original)
        self.assertEqual(self.request('POST', path, {'mac':devices[0]['mac'], 'expected_mac':devices[0]['mac']})[0],400)
        self.assertEqual(self.request('POST', path, {'mac':devices[1]['mac']})[0],400)
        self.assertEqual(self.request('POST', path, {'unrecognized':True})[0],400)
        self.assertEqual(self.request('POST','/api/v1/telescopes/primary/disconnect',{'mac':devices[0]['mac']})[0],400)

    def setUp(self):
        super().setUp();self.configured();self.controller.cycle(0)
        self.server=WebServer(('127.0.0.1',0),self.controller)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.token=''

    def request(self,method,path,data=None,headers=None,raw=False):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=30)
        h={'Host':f'127.0.0.1:{self.server.server_port}','Content-Type':'application/json'}
        if self.token:h['Authorization']='Bearer '+self.token
        h.update(headers or {})
        c.request(method,path, data if raw else json.dumps(data) if data is not None else None,h)
        r=c.getresponse();body=json.loads(r.read());cookie=r.getheader('Set-Cookie');c.close()
        return r.status,body,cookie

    def login(self,password='local-admin-test-password'):
        code,data,cookie=self.request('POST','/api/v1/auth/token',{'password':password})
        self.assertEqual(code,200);self.assertIsNone(cookie)
        self.assertEqual(data['token_type'],'Bearer');self.token=data['access_token']
        return data

    def create(self,**changes):
        data={'id':'other','name':'Other instrument','ssid':'Other-WiFi','mac':self.devices[0]['mac'],
              'target_ip':'10.0.0.1','open':True}
        data.update(changes)
        return self.request('POST','/api/v1/telescopes',data)

    def test_native_auth_does_not_relax_browser_protection(self):
        self.assertEqual(self.request('GET','/api/v1/telescopes')[0],401)
        self.login()
        self.assertEqual(self.request('GET','/api/v1/status')[0],200)
        cookie={'Cookie':'bridge_session='+self.token}
        self.assertEqual(self.request('GET','/api/status',headers=cookie)[0],401)
        self.assertEqual(self.request('GET','/api/v1/status',headers={'Authorization':'','Cookie':cookie['Cookie']})[0],401)
        self.assertEqual(self.request('GET','/api/v1/status',headers={'Origin':'http://evil.example'})[0],403)
        self.assertEqual(self.request('GET','/api/v1/status',headers={'Host':'evil.example'})[0],421)
        self.assertEqual(self.request('POST','/api/reboot',{})[0],403)
        code,data,cookie=self.request('POST','/api/login',{'password':'local-admin-test-password'},headers={'Origin':f'http://127.0.0.1:{self.server.server_port}'})
        self.assertEqual(code,200)
        self.assertEqual(self.request('GET','/api/v1/status',headers={'Authorization':'','Cookie':cookie.split(';')[0]})[0],401)

    def test_profile_crud_is_redacted_and_partial(self):
        self.login();code,data,_=self.create(password='other-private-secret',open=False)
        self.assertEqual(code,201);self.assertFalse(data['telescope']['enabled'])
        self.assertNotIn('other-private-secret',json.dumps(data))
        code,data,_=self.request('PATCH','/api/v1/telescopes/other',{'name':'Renamed'})
        self.assertEqual(code,200);self.assertEqual(data['telescope']['name'],'Renamed')
        self.assertTrue(data['telescope']['has_password'])
        self.assertEqual(self.request('PATCH','/api/v1/telescopes/other',{'ssid':'New WiFi'})[0],400)
        self.assertEqual(self.request('PATCH','/api/v1/telescopes/other',{'id':'injected'})[0],400)
        self.assertEqual(self.create()[0],409)
        self.assertEqual(self.request('DELETE','/api/v1/telescopes/other')[0],200)
        self.assertEqual(self.request('GET','/api/v1/telescopes/other')[0],404)
        self.assertEqual(len(self.store.config['telescopes']),1)

    def test_switch_only_changes_shared_instrument_radio(self):
        self.login();self.create()
        with patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect:
            self.assertEqual(self.request('POST','/api/v1/telescopes/other/connect',{})[0],202)
            self.controller.cycle(5)
        self.assertNotIn('home',[c.args[0] for c in disconnect.call_args_list])
        _,data,_=self.request('GET','/api/v1/telescopes')
        by_id={p['id']:p for p in data['telescopes']}
        self.assertFalse(by_id['primary']['enabled']);self.assertTrue(by_id['other']['connection']['relay_ready'])
        self.assertEqual(self.request('POST','/api/v1/telescopes/other/disconnect',{})[0],202)
        self.assertTrue(self.controller.status()['home_ip'])

    def test_conditional_partial_home_and_settings_preserve_passwords(self):
        self.login();_,data,_=self.request('GET','/api/v1/config')
        revision='"'+data['config_revision']+'"'
        self.assertEqual(self.request('PATCH','/api/v1/hotspot',{'enabled':True},headers={'If-Match':revision})[0],200)
        self.assertEqual(self.request('PATCH','/api/v1/home',{'hidden':True},headers={'If-Match':revision})[0],412)
        self.assertFalse(self.store.config['home']['hidden'])
        self.assertEqual(self.request('PATCH','/api/v1/home',{'hidden':True})[0],200)
        self.assertEqual(self.store.config['home']['password'],'maison-test-pass')
        self.assertEqual(self.request('PATCH','/api/v1/settings',{'country':'FR'})[0],200)
        self.assertEqual(self.store.config['home']['ssid'],'Maison')
        self.assertEqual(self.request('PATCH','/api/v1/settings',{'admin_hash':'bad'})[0],400)

    def test_operation_protection_http_scope_revision_and_round_trip(self):
        self.login()
        body = {'auto_policy':{'protect_running_operations':True}, 'acknowledge_interruption':True}
        path = '/api/v1/settings'
        revision = '"'+self.server.api.revision()+'"'
        for scopes in (['read'], ['connect'], ['telemetry'], ['camera']):
            self.server.sessions[self.token]['scopes'] = scopes
            self.assertEqual(self.request('PATCH', path, body, {'If-Match':revision})[0], 403)
        self.server.sessions[self.token]['scopes'] = ['admin']
        self.assertEqual(self.request('PATCH', path, body)[0], 428)
        self.assertEqual(self.request('PATCH', path, body, {'If-Match':'"stale"'})[0], 412)
        self.assertEqual(self.request('PATCH', path, {'auto_policy':body['auto_policy']}, {'If-Match':revision})[0], 400)
        status, result, _ = self.request('PATCH', path, body, {'If-Match':revision})
        self.assertEqual(status, 200)
        self.assertTrue(result['settings']['auto_policy']['protect_running_operations'])
        self.assertNotIn('acknowledge_interruption', self.store.config)
        self.assertEqual(self.request('PATCH', path, {'auto_policy':{'protect_running_operations':False}}, {'If-Match':revision})[0], 412)
        current = '"'+self.server.api.revision()+'"'
        self.assertEqual(self.request('PATCH', path, {'auto_policy':{'protect_running_operations':False}}, {'If-Match':current})[0], 200)

    def test_tokens_expire_and_logout_is_per_app(self):
        self.login();first=self.token;self.login();second=self.token
        self.assertEqual(self.request('DELETE','/api/v1/auth/token')[0],200)
        self.assertEqual(self.request('GET','/api/v1/status')[0],401)
        self.token=first;self.assertEqual(self.request('GET','/api/v1/status')[0],200)
        self.server.sessions[first]['expires']=0
        self.assertEqual(self.request('GET','/api/v1/status')[0],401)
        self.assertNotIn(second,self.store.path.read_text())

    def test_password_change_revokes_mobile_tokens(self):
        self.login()
        request={'target':'web','current_password':'local-admin-test-password','password':'new-admin-password'}
        self.assertEqual(self.request('POST','/api/v1/credentials',request)[0],200)
        self.assertEqual(self.request('GET','/api/v1/status')[0],401)
        self.login('new-admin-password')
        self.assertEqual(self.request('POST','/api/v1/auth/revoke-all',{})[0],200)
        self.assertEqual(self.request('GET','/api/v1/status')[0],401)

    def test_inventory_scan_and_busy_response(self):
        self.login();code,data,_=self.request('GET','/api/v1/interfaces')
        self.assertEqual(code,200)
        self.assertTrue(data['interfaces'][0]['can_assign_telescope'])
        self.assertFalse(data['interfaces'][0]['available_for_telescope'])
        self.assertFalse(data['interfaces'][1]['available_for_telescope'])
        self.assertEqual(self.request('GET','/api/v1/wifi/networks?interface=wlan1')[0],200)
        self.assertEqual(self.request('GET','/api/v1/wifi/networks?interface=missing')[0],400)
        with self.controller.net_lock:
            code,data,_=self.request('GET','/api/v1/wifi/networks?interface=wlan1')
        self.assertEqual(code,409);self.assertEqual(data['error']['code'],'SCAN_UNAVAILABLE')

    def test_discovery_contract_and_invalid_requests(self):
        self.assertEqual(self.request('GET','/api/v1/info')[1]['product'],'astro-bridge')
        code,spec,_=self.request('GET','/api/v1/openapi.json')
        self.assertEqual(code,200);self.assertEqual(spec['openapi'],'3.1.0')
        self.assertIn('/api/v1/telescopes/{id}/connect',spec['paths'])
        def check_refs(value):
            if isinstance(value,dict):
                if '$ref' in value:
                    target=spec
                    for part in value['$ref'].split('/')[1:]:target=target[part]
                for item in value.values():check_refs(item)
            elif isinstance(value,list):
                for item in value:check_refs(item)
        check_refs(spec)
        operations=[operation['operationId'] for methods in spec['paths'].values() for operation in methods.values()]
        self.assertEqual(len(operations),len(set(operations)))
        self.login()
        for value in ('[]','null','{broken'):
            self.assertEqual(self.request('PATCH','/api/v1/home',value,raw=True)[0],400)
        self.assertEqual(self.request('PATCH','/api/v1/home',{'ssid':'test'},headers={'Content-Type':'text/plain'})[0],400)
        self.assertEqual(self.request('PATCH','/api/v1/home','x'*32769,raw=True)[0],400)
        self.assertEqual(self.request('POST','/api/v1/interfaces',{})[0],405)
        self.assertEqual(self.request('PUT','/api/v1/home',{})[0],405)
        self.assertEqual(self.request('GET','/api/v1/missing')[0],404)

    def test_update_verification_rejects_mutation_with_versioned_error(self):
        self.login()
        from wifi_bridge.ota import atomic
        before=self.store.path.read_bytes()
        atomic(self.server.updates.root/'transaction.json',{'phase':'verifying'})
        code,body,_=self.request('PATCH','/api/v1/settings',{'hostname':'changed'})
        self.assertEqual(code,409);self.assertEqual(body['error']['code'],'UPDATE_IN_PROGRESS')
        self.assertEqual(self.store.path.read_bytes(),before)
        self.assertEqual(self.request('GET','/api/v1/status')[0],200)
        (self.server.updates.root/'transaction.json').write_text('null')
        code,body,_=self.request('PATCH','/api/v1/settings',{'hostname':'changed'})
        self.assertEqual(code,409);self.assertEqual(body['error']['code'],'UPDATE_RECOVERY_REQUIRED')
        self.assertEqual(self.request('GET','/api/v1/system/updates')[1]['state'],'recovery_required')
        self.assertEqual(self.request('GET','/api/v1/status')[0],200)
        self.assertEqual(self.request('DELETE','/api/v1/auth/token')[0],200)

    def test_update_management_requires_bearer_and_demo_cannot_install(self):
        self.assertEqual(self.request('GET','/api/v1/system/updates')[0],401)
        self.login()
        code, body, _ = self.request('GET','/api/v1/system/updates')
        self.assertEqual(code,200)
        self.assertFalse(body['automatic'])
        self.assertEqual(self.request('POST','/api/v1/system/updates',{'action':'install','release_id':1})[0],400)
        code, body, _ = self.request('GET','/api/update-health')
        self.assertEqual(code,200)
        self.assertIn('management_ready',body)
        self.assertNotIn('config',body)


    def test_safe_discovery_auth_csrf_private_metadata_and_versioned_binding_preconditions(self):
        self.assertEqual(self.request('POST','/api/v1/discovery',{'mode':'wifi'})[0],401)
        self.login();self.net.radios=3
        code,page,_=self.request('POST','/api/v1/discovery',{'mode':'wifi'})
        self.assertEqual(code,200);self.assertEqual(page['reason'],'OBSERVED')
        candidate=page['candidates'][0]
        self.assertNotIn(candidate['candidate_id'],json.dumps(self.request('GET','/api/v1/status')[1]))
        self.assertIsNone(candidate['reported_identity'])
        path='/api/v1/telescopes/primary/binding'
        self.assertEqual(self.request('POST',path,{'candidate_id':candidate['candidate_id']})[0],428)
        for wildcard in ('*','"*"'):
            self.assertEqual(self.request('POST',path,{'candidate_id':candidate['candidate_id']},{'If-Match':wildcard})[0],428)
        revision=self.request('GET','/api/v1/config')[1]['config_revision']
        headers={'If-Match':'"'+revision+'"'}
        original=self.store.config['telescopes'][0].copy()
        code,bound,_=self.request('POST',path,{'candidate_id':candidate['candidate_id']},headers)
        self.assertEqual(code,200);binding=bound['telescope']['physical_binding']
        self.assertEqual(binding['aliases'][0]['frequency'],candidate['frequency'])
        self.assertEqual(binding['aliases'][0]['source'],'wifi-observed')
        self.assertIsNone(binding['reported_identity']);self.assertIsNone(binding['client_observation'])
        for field in ('id','password','mac','bssid','enabled'):self.assertEqual(self.store.config['telescopes'][0][field],original[field])
        self.assertEqual(self.request('DELETE',path,{},headers)[0],412)
        current={'If-Match':'"'+self.server.api.revision()+'"'}
        for body in ([],False,0,'',{'path':'/tmp'}):self.assertEqual(self.request('DELETE',path,body,current)[0],400)
        self.assertEqual(self.request('DELETE',path,{},current)[0],200)
        self.assertNotIn('physical_binding',self.store.config['telescopes'][0])
        for data in ([],None,{'mode':[]},{'mode':'wifi','interface':'wlan0'}):
            self.assertEqual(self.request('POST','/api/v1/discovery',data)[0],400)
        self.assertNotIn('vespera-test-pass',json.dumps(bound))

    def test_browser_discovery_binding_requires_cookie_csrf_and_current_revision(self):
        origin='http://127.0.0.1:'+str(self.server.server_port)
        self.assertEqual(self.request('POST','/api/discovery',{'mode':'wifi'},{'Origin':origin})[0],401)
        code,login,cookie=self.request('POST','/api/login',{'password':'local-admin-test-password'},{'Origin':origin})
        self.assertEqual(code,200)
        headers={'Origin':origin,'Cookie':cookie.split(';')[0]}
        self.assertEqual(self.request('POST','/api/discovery',{'mode':'wifi'},headers)[0],403)
        headers['X-CSRF-Token']=login['csrf']
        self.assertEqual(self.request('POST','/api/discovery',{'mode':'wifi'},headers)[1]['scan_source'],'connected_station')
        status=self.request('GET','/api/status',headers=headers)[1]
        data={'id':'primary','action':'bind','config_revision':status['config_revision'],'aliases':[{'ssid':'Instrument','bssid':'02:00:00:00:00:01'}],'owner_confirmed':True}
        self.assertEqual(self.request('POST','/api/telescopes/binding',data,headers)[0],200)
        self.assertEqual(self.request('POST','/api/telescopes/binding',{'id':'primary','action':'unlink','config_revision':status['config_revision']},headers)[0],412)


    def test_explicit_ensure_auth_consent_operations_and_current_revision(self):
        path='/api/v1/telescopes/auto-one/ensure'
        self.assertEqual(self.request('POST',path,{})[0],401);self.login();self.net.radios=3;self.controller.update(devices=self.net.devices())
        self.create(id='auto-one',mac='',ssid='Telescope-DEMO',target_ip='10.3.0.1',auto_eligible=True)
        self.assertEqual(self.request('POST',path,{})[0],428)
        headers={'If-Match':'"'+self.server.api.revision()+'"'}
        self.assertEqual(self.request('POST',path,{},headers)[1]['error']['code'],'AUTO_CONNECT_DISABLED')
        self.request('PATCH','/api/v1/settings',{'auto_connect':True})
        for value in ('*','"*"','"old"'):
            self.assertIn(self.request('POST',path,{}, {'If-Match':value})[0],(428,412))
        headers={'If-Match':'"'+self.server.api.revision()+'"'}
        code,accepted,_=self.request('POST',path,{},headers);self.assertEqual(code,202)
        self.controller.operations.thread.join(5)
        code,result,_=self.request('GET',accepted['status_url']);self.assertEqual(code,200);self.assertEqual(result['state'],'succeeded')
        self.assertEqual(result['endpoint']['profile_id'],'auto-one');self.assertNotIn('password',str(result))
        self.assertEqual(self.request('POST',path,{})[0],200)
        self.assertEqual(self.request('GET','/api/v1/operations/bad')[0],400)
        self.assertEqual(self.request('POST',accepted['status_url'],{})[0],405)
        self.assertEqual(self.request('POST',path,{'target':'http://example.test'},headers)[0],400)
        self.server.sessions[self.token]['expires']=0
        self.assertEqual(self.request('GET',accepted['status_url'])[0],401);self.assertEqual(self.controller.operations.clients,{})


    def test_browser_ensure_csrf_and_expired_auth_do_not_expose_operations(self):
        origin='http://127.0.0.1:'+str(self.server.server_port)
        path='/api/telescopes/ensure'
        self.assertEqual(self.request('POST',path,{'id':'primary'},{'Origin':origin})[0],401)
        _,login,cookie=self.request('POST','/api/login',{'password':'local-admin-test-password'},{'Origin':origin})
        headers={'Origin':origin,'Cookie':cookie.split(';')[0]}
        self.assertEqual(self.request('POST',path,{'id':'primary'},headers)[0],403)
        headers['X-CSRF-Token']=login['csrf']
        code,ready,_=self.request('POST',path,{'id':'primary'},headers)
        self.assertEqual(code,200);self.assertEqual(ready['endpoint']['profile_id'],'primary')
        self.assertEqual(self.request('GET','/api/operations?id=invalid',headers=headers)[0],400)
        self.assertEqual(self.request('GET','/api/operations?id=invalid')[0],401)
        self.assertEqual(self.request('POST',path,{'id':[]},headers)[0],400)


    def test_reservations_native_ownership_revocation_guard_and_owner_release_version(self):
        self.assertEqual(self.request('GET','/api/v1/protection')[0],401);self.login();original=self.token
        revision={'If-Match':'"'+self.server.api.revision()+'"'}
        self.assertEqual(self.request('POST','/api/v1/telescopes/primary/leases',{})[0],428)
        code,lease,_=self.request('POST','/api/v1/telescopes/primary/leases',{},revision);self.assertEqual(code,200);self.assertTrue(lease['owned'])
        own=self.request('GET','/api/v1/status')[1]['connection_protection']['leases'][0];self.assertTrue(own['owned'])
        self.login();foreign=self.request('GET','/api/v1/protection')[1]['leases'][0];self.assertFalse(foreign['owned'])
        self.assertEqual(self.request('DELETE','/api/v1/leases/'+lease['id'],{'version':1})[0],403)
        self.assertEqual(self.request('POST','/api/v1/telescopes/primary/disconnect',{})[0],409)
        self.assertEqual(self.request('POST','/api/v1/system/reboot',{})[0],409)
        self.assertEqual(self.request('GET','/api/v1/activity')[0],200)
        self.token=original;self.assertEqual(self.request('PATCH','/api/v1/leases/'+lease['id'],{'version':1,'ttl_seconds':300})[0],200)
        self.assertEqual(self.request('DELETE','/api/v1/auth/token',{})[0],200)
        self.login();self.assertEqual(len(self.request('GET','/api/v1/protection')[1]['leases']),1)
        release={'version':1,'current_password':'local-admin-test-password','confirm_release':True}
        path='/api/v1/leases/'+lease['id']+'/release'
        self.assertEqual(self.request('POST',path,release,revision)[0],412)
        release['version']=2;self.assertEqual(self.request('POST',path,release,revision)[0],200)
        self.assertEqual(self.request('GET','/api/v1/protection')[1]['leases'],[])
        self.assertEqual(self.request('GET',path)[0],405)
        self.assertNotIn('local-admin-test-password',json.dumps(foreign))

    def test_browser_protection_csrf_foreign_native_owner_lock_and_full_config_boundary(self):
        self.login();native=self.token;rev=self.server.api.revision()
        self.assertEqual(self.request('POST','/api/v1/telescopes/primary/leases',{}, {'If-Match':'"'+rev+'"'})[0],200)
        origin='http://127.0.0.1:'+str(self.server.server_port)
        _,_,cookie=self.request('POST','/api/login',{'password':'local-admin-test-password'},{'Origin':origin})
        headers={'Cookie':cookie.split(';')[0],'Origin':origin,'Authorization':''}
        _,status,_=self.request('GET','/api/status',headers=headers);lease=status['connection_protection']['leases'][0];self.assertFalse(lease['owned'])
        body={'action':'owner_release','id':lease['id'],'version':lease['version'],'config_revision':rev,'confirm_release':True,'current_password':'local-admin-test-password'}
        self.assertEqual(self.request('POST','/api/protection',body,headers)[0],403)
        headers['X-CSRF-Token']=status['csrf'];self.assertEqual(self.request('POST','/api/protection',body,headers)[0],200)
        lock={'action':'owner_lock','id':'primary','locked':True,'current_password':'local-admin-test-password','config_revision':self.server.api.revision()}
        self.assertEqual(self.request('POST','/api/protection',lock,headers)[0],200)
        self.assertTrue(self.controller.store.config['telescopes'][0]['owner_locked'])
        self.assertEqual(self.request('PATCH','/api/v1/telescopes/primary',{'owner_locked':False})[0],400)
        _,status,_=self.request('GET','/api/status',headers=headers);self.assertTrue(status['config']['telescopes'][0]['owner_locked'])
        self.assertEqual(self.request('POST','/api/logout',{},headers)[0],200)
        self.login();self.assertTrue(self.request('GET','/api/v1/protection')[1]['owner_locks'])

    def test_dwarf_policy_auth_actual_revision_ack_browser_csrf_and_stage_a_status(self):
        self.store.config['auto_policy']['dwarf_demand']=False  # Existing opt-out; test a real enable transition.
        policy={'auto_policy':{'dwarf_demand':True},'acknowledge_interruption':True}
        self.assertEqual(self.request('PATCH','/api/v1/settings',policy)[0],401)
        self.login();headers={'If-Match':'"'+self.server.api.revision()+'"'}
        self.assertEqual(self.request('PATCH','/api/v1/settings',policy)[0],428)
        self.assertEqual(self.request('PATCH','/api/v1/settings',policy,{'If-Match':'*'})[0],428)
        self.assertEqual(self.request('PATCH','/api/v1/settings',{'auto_policy':{'dwarf_demand':True}},headers)[0],400)
        self.assertEqual(self.request('PATCH','/api/v1/settings',{'auto_policy':{'dwarf_demand':1}},headers)[0],400)
        code,result,_=self.request('PATCH','/api/v1/settings',policy,headers);self.assertEqual(code,200)
        self.assertTrue(result['settings']['auto_policy']['dwarf_demand']);self.assertNotIn('acknowledge_interruption',result['settings'])
        self.assertEqual(self.request('PATCH','/api/v1/settings',{'auto_policy':{'dwarf_demand':False}},headers)[0],412)
        origin='http://127.0.0.1:'+str(self.server.server_port)
        _,_,cookie=self.request('POST','/api/login',{'password':'local-admin-test-password'},{'Origin':origin})
        browser={'Cookie':cookie.split(';')[0],'Origin':origin,'Authorization':''}
        _,status,_=self.request('GET','/api/status',headers=browser)
        dwarf=status['maker_demands']['dwarf'];self.assertEqual(dwarf['discovery_relay']['maker_app_acceptance'],'unverified')
        self.assertTrue(dwarf['passive_only']);self.assertTrue(dwarf['replies_disabled'])
        self.assertEqual(dwarf['discovery_relay']['last_result'],'REPLIES_DISABLED')
        self.assertFalse(dwarf['discovery_relay']['supported']);self.assertFalse(dwarf['discovery_relay']['relay_plan_available'])
        self.assertIn('dwarf_passive_demand',self.server.api.info()['legacy_capabilities'])
        self.assertNotIn('dwarf_discovery_relay',self.server.api.info()['legacy_capabilities'])
        self.assertEqual(dwarf['protocol'],'dwarf-udp-ping');self.assertFalse(dwarf['supported'])
        body=dict(status['config'],auto_policy={'dwarf_demand':False},config_revision=status['config_revision'])
        self.assertEqual(self.request('POST','/api/config',body,browser)[0],403)
        browser['X-CSRF-Token']=status['csrf'];self.assertEqual(self.request('POST','/api/config',body,browser)[0],200)
        self.assertFalse(self.controller.store.config['auto_policy']['dwarf_demand'])

    def test_vespera_policy_native_browser_csrf_revision_and_diagnostic_privacy(self):
        self.store.config['auto_policy']['vespera_demand']=False
        policy={'auto_policy':{'vespera_demand':True},'acknowledge_interruption':True}
        self.assertEqual(self.request('PATCH','/api/v1/settings',policy)[0],401)
        self.login();revision=self.server.api.revision();headers={'If-Match':'"'+revision+'"'}
        self.assertEqual(self.request('PATCH','/api/v1/settings',policy)[0],428)
        self.assertEqual(self.request('PATCH','/api/v1/settings',policy,{'If-Match':'*'})[0],428)
        self.assertEqual(self.request('PATCH','/api/v1/settings',{'auto_policy':{'vespera_demand':True}},headers)[0],400)
        self.assertEqual(self.request('PATCH','/api/v1/settings',{'auto_policy':{'vespera_demand':1}},headers)[0],400)
        code,result,_=self.request('PATCH','/api/v1/settings',policy,headers);self.assertEqual(code,200)
        self.assertTrue(result['settings']['auto_policy']['vespera_demand']);self.assertNotIn('acknowledge_interruption',result['settings'])
        self.assertEqual(self.request('PATCH','/api/v1/settings',{'auto_policy':{'vespera_demand':False}},headers)[0],412)
        origin='http://127.0.0.1:'+str(self.server.server_port)
        _,_,cookie=self.request('POST','/api/login',{'password':'local-admin-test-password'},{'Origin':origin})
        browser={'Cookie':cookie.split(';')[0],'Origin':origin,'Authorization':''}
        _,status,_=self.request('GET','/api/status',headers=browser)
        self.assertEqual(status['maker_demand']['protocol'],'seestar-scan-iscope')
        self.assertEqual(status['maker_demands']['vespera']['protocol'],'vespera-tcp-syn')
        self.assertFalse(status['maker_demands']['vespera']['supported'])
        self.assertNotIn('local-admin-test-password',str(status['maker_demands']))
        body=dict(status['config'],auto_policy={'vespera_demand':False},config_revision=status['config_revision'])
        self.assertEqual(self.request('POST','/api/config',body,browser)[0],403)
        browser['X-CSRF-Token']=status['csrf']
        self.assertEqual(self.request('POST','/api/config',body,browser)[0],200)
        self.assertFalse(self.controller.store.config['auto_policy']['vespera_demand'])

    def test_replacement_policy_revision_acknowledgement_browser_csrf_and_pause_recovery(self):
        recovery='/api/v1/system/connection-pauses/recover'
        self.assertEqual(self.request('POST',recovery,{})[0],401)
        self.login(); policy={'auto_policy':{'force_first_profile':True},'acknowledge_interruption':True}
        self.assertEqual(self.request('PATCH','/api/v1/settings',policy)[0],428)
        self.assertEqual(self.request('PATCH','/api/v1/settings',policy,{'If-Match':'*'})[0],428)
        revision=self.server.api.revision();headers={'If-Match':'"'+revision+'"'}
        self.assertEqual(self.request('PATCH','/api/v1/settings',{'auto_policy':{'force_first_profile':True}},headers)[0],400)
        code,result,_=self.request('PATCH','/api/v1/settings',policy,headers);self.assertEqual(code,200)
        self.assertTrue(result['settings']['auto_policy']['force_first_profile']);self.assertNotIn('acknowledge_interruption',result['settings'])
        self.assertEqual(self.request('PATCH','/api/v1/settings',{'auto_policy':{'quiet_seconds':30}},headers)[0],412)
        self.controller.pauses.path.unlink()
        from wifi_bridge.paused import PausedRoles
        self.controller.pauses=PausedRoles(self.store.path.parent)
        body={'current_password':'local-admin-test-password','confirm_recovery':True};headers={'If-Match':'"'+self.server.api.revision()+'"'}
        self.assertEqual(self.request('POST',recovery,dict(body,confirm_recovery=False),headers)[0],400)
        self.assertEqual(self.request('POST',recovery,dict(body,current_password='wrong'),headers)[0],403)
        code,result,_=self.request('POST',recovery,body,headers);self.assertEqual(code,200);self.assertIn('primary',result['profiles'])
        self.assertEqual(self.request('POST','/api/v1/telescopes/primary/connect',{})[0],428)
        self.assertEqual(self.request('POST','/api/v1/telescopes/primary/connect',{},headers)[0],202)
        self.assertFalse(self.controller.pauses.held('primary'))
        origin='http://127.0.0.1:'+str(self.server.server_port)
        _,_,cookie=self.request('POST','/api/login',{'password':'local-admin-test-password'},{'Origin':origin})
        browser={'Cookie':cookie.split(';')[0],'Origin':origin,'Authorization':''};status=self.request('GET','/api/status',headers=browser)[1]
        action={'action':'recover_pauses','id':'all','current_password':'local-admin-test-password','confirm_recovery':True,'config_revision':status['config_revision']}
        self.assertEqual(self.request('POST','/api/protection',action,browser)[0],403)
        browser['X-CSRF-Token']=status['csrf'];self.assertEqual(self.request('POST','/api/protection',action,browser)[0],200)
        self.assertEqual(self.request('GET','/api/status',headers=browser)[1]['connection_pauses']['profiles']['primary'],'OWNER_RECOVERY')

    def test_field_management_paths_native_parity_and_ap_is_reserved(self):
        from wifi_bridge.network import NetworkError
        self.login();self.net.radios=3;self.controller.update(devices=self.net.devices())
        self.assertIn('field_management_paths',self.request('GET','/api/v1/info')[1]['legacy_capabilities'])
        self.assertEqual(self.request('PATCH','/api/v1/hotspot',{'enabled':True})[0],200)
        self.controller.cycle(5)
        status=self.request('GET','/api/v1/status')[1]
        self.assertEqual(status['management']['mode'],'dual')
        row=status['telescopes'][0];self.assertEqual({p['role'] for p in row['management_paths']},{'home','ap'})
        self.assertTrue(row['relay_ready'])
        ap=next(d for d in self.request('GET','/api/v1/interfaces')[1]['interfaces'] if any(a['role']=='hotspot' for a in d['assignments']))
        self.assertFalse(ap['can_assign_telescope']);self.assertFalse(ap['available_for_telescope'])
        self.net.disconnect('home');original=self.net.connect
        def connect(role,*args):
            if role=='home':raise NetworkError('synthetic home loss')
            return original(role,*args)
        with patch.object(self.net,'connect',side_effect=connect):self.controller.cycle(50)
        code,status,_=self.request('GET','/api/v1/status');self.assertEqual(code,200)
        self.assertEqual(status['management']['mode'],'ap_only');self.assertEqual(status['home_ip'],'')
        self.assertTrue(status['telescopes'][0]['relay_ready'])
        code,ready,_=self.request('POST','/api/v1/telescopes/primary/ensure',{},headers={'If-Match':'"'+status['config_revision']+'"'})
        self.assertEqual(code,200);self.assertEqual(ready['endpoint']['proxy']['host'],'192.168.77.1')
        self.assertEqual(ready['endpoint']['management_endpoints'][0]['role'],'ap')
        self.assertNotIn('maison-test-pass',json.dumps(status));self.assertNotIn('vespera-test-pass',json.dumps(ready))
        self.token='';self.assertEqual(self.request('GET','/api/v1/status')[0],401)


class FreshAPITests(Fixture):
    request = APITests.request
    login = APITests.login

    def setUp(self):
        super().setUp();self.controller.cycle(0)
        self.server=WebServer(('127.0.0.1',0),self.controller)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown);self.token=''

    def test_fresh_settings_and_profile_http_auth_revision_and_browser_csrf(self):
        self.assertEqual(self.request('PATCH','/api/v1/settings',{'country':'FR'})[0],401)
        self.login('bootstrap-test-code')
        code,value,_=self.request('PATCH','/api/v1/settings',{'country':'FR','hostname':'fresh-api'})
        self.assertEqual(code,200);revision=value['config_revision']
        code,value,_=self.request('POST','/api/v1/telescopes',{'id':'fresh','name':'Synthetic fresh','ssid':'Synthetic scope',
            'password':'synthetic-scope-secret','target_ip':'10.8.0.1'},headers={'If-Match':'"'+revision+'"'})
        self.assertEqual(code,201);self.assertFalse(value['telescope']['enabled']);self.assertEqual(value['telescope']['mac'],'')
        self.assertNotIn('synthetic-scope-secret',json.dumps(value));self.assertEqual(self.store.config['home'],{})
        self.assertEqual(self.request('PATCH','/api/v1/settings',{'country':'JP'},headers={'If-Match':'"'+revision+'"'})[0],412)
        origin=f'http://127.0.0.1:{self.server.server_port}'
        code,value,cookie=self.request('POST','/api/login',{'password':'bootstrap-test-code'},headers={'Origin':origin})
        self.assertEqual(code,200)
        headers={'Origin':origin,'Cookie':cookie.split(';')[0],'Authorization':''}
        import copy
        payload=copy.deepcopy(self.store.config);payload['country']='JP'
        self.assertEqual(self.request('POST','/api/config',payload,headers=headers)[0],403)
        headers['X-CSRF-Token']=value['csrf']
        self.assertEqual(self.request('POST','/api/config',payload,headers=headers)[0],200)
        self.assertEqual(self.store.config['country'],'JP');self.assertEqual(self.store.config['home'],{})
