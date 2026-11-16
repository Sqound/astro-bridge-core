"""Camera bridge facade validation independent of the Alpaca protocol listener."""
from wifi_bridge.api import ApiError, MobileAPI
from wifi_bridge.cameras.simulator import SimulatedCamera
from test_bridge import Fixture


class CameraAPITests(Fixture):
    def setUp(self):
        super().setUp()
        self.api = MobileAPI(self.controller)
        self.facade = self.api.cameras
        self.service = self.facade.service()
        self.addCleanup(self.service.close)

    def handle(self, method, path, data=None, revision=None, query=None):
        return self.facade.handle(method, path, query or {}, data or {}, revision)

    def test_inventory_defaults_and_permissions(self):
        self.assertEqual(self.handle('GET', '/cameras'), [])
        self.assertEqual(self.handle('GET', '/cameras/settings')['settings'],
                         {'serve': True, 'discover': True, 'indi': False, 'port': 11111, 'allowed_subnets': []})
        self.assertEqual(MobileAPI.required_scope('GET', '/cameras'), 'camera')
        self.assertEqual(MobileAPI.required_scope('PATCH', '/cameras/settings'), 'admin')
        self.assertEqual(MobileAPI.required_scope('GET', '/cameras/settings'), 'admin')
        self.service.add('simulation', 'simulator', SimulatedCamera(width=4, height=4))
        self.assertEqual(self.handle('GET', '/cameras'), [dict(id='simulation',instrument_id='simulation',name='simulator',driver='simulator',state='idle',
            alpaca=None,last_image={'bytes':0,'seconds':0.0},sensor={'width':4,'height':4,'bits':16,'max_bin':4},defaults={'bin':1,'gain':0},current={'bin':1,'exposure_seconds':0.0})])

    def test_settings_revision_strictness_and_persistence(self):
        revision = self.handle('GET', '/cameras/settings')['config_revision']
        for value, status in ((None,428),('*',428),('"stale"',412)):
            with self.subTest(value=value), self.assertRaises(ApiError) as raised:
                self.handle('PATCH', '/cameras/settings', {'serve':False}, value)
            self.assertEqual(raised.exception.status,status)
        changed = self.handle('PATCH','/cameras/settings',{'serve':False,'port':12000},'"'+revision+'"')
        self.assertNotEqual(revision,changed['config_revision'])
        self.assertFalse(changed['settings']['serve'])
        self.assertEqual(changed['settings']['port'],12000)
        self.assertTrue((self.store.path.parent/'cameras.json').exists())
        for patch in ({'serve':1},{'port':True},{'port':80},{'allowed_subnets':['0.0.0.0/0']},{'allowed_subnets':['192.0.2.3/24']},{'raw':'hidden'},{}):
            with self.subTest(patch=patch), self.assertRaises(ApiError) as raised:
                self.handle('PATCH','/cameras/settings',patch,'"'+changed['config_revision']+'"')
            self.assertEqual(raised.exception.status,400)
        with self.assertRaises(ApiError):
            self.handle('GET','/cameras',query={'extra':['1']})

    def test_owner_action_is_explicit_and_revision_guarded(self):
        self.service.add('simulation','simulator',SimulatedCamera(width=4,height=4))
        self.service.execute(0,'PUT','connected',{'connected':'true'},1,'192.0.2.2')
        self.service.execute(0,'PUT','startexposure',{'duration':'10','light':'true'},1,'192.0.2.2')
        self.assertIn('owner',self.service.inventory()[0])
        revision='"'+self.api.revision()+'"'
        with self.assertRaises(ApiError):
            self.facade.release_owner('simulation',{},revision)
        self.assertEqual(self.facade.release_owner('simulation',{'confirm_release':True,'owner_generation':self.service.inventory()[0]['owner']['generation']},revision),{'ok':True})
        self.assertNotIn('owner',self.service.inventory()[0])


class CameraHTTPTests(Fixture):
    def setUp(self):
        super().setUp()
        import threading
        from wifi_bridge.web import WebServer
        self.configured(); self.controller.cycle(0)
        self.server = WebServer(('127.0.0.1',0),self.controller)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.token=''

    from test_api import APITests as _HTTP
    request = _HTTP.request
    login = _HTTP.login
    del _HTTP

    def test_real_http_camera_scope_settings_and_schema(self):
        from helpers.contract_schema import validate
        self.assertEqual(self.request('GET','/api/v1/cameras')[0],401)
        self.login()
        for scope in ('read','connect','telemetry'):
            self.server.sessions[self.token]['scopes']=[scope]
            self.assertEqual(self.request('GET','/api/v1/cameras')[0],403)
        self.server.sessions[self.token]['scopes']=['camera']
        self.assertEqual(self.request('GET','/api/v1/cameras')[:2],(200,[]))
        self.assertEqual(self.request('GET','/api/v1/cameras/settings')[0],403)
        self.assertEqual(self.request('PATCH','/api/v1/cameras/settings',{'serve':False})[0],403)
        self.server.sessions[self.token]['scopes']=['admin']
        status=self.request('GET','/api/v1/cameras/settings')[1]
        for headers,expected in (({},428),({'If-Match':'*'},428),({'If-Match':'"stale"'},412)):
            self.assertEqual(self.request('PATCH','/api/v1/cameras/settings',{'serve':False},headers)[0],expected)
        code,changed,_=self.request('PATCH','/api/v1/cameras/settings',{'serve':False},{'If-Match':'"'+status['config_revision']+'"'})
        self.assertEqual(code,200)
        spec=self.server.api.specification()
        validate(changed,spec['components']['schemas']['NativeCameraSettings'],spec)
        self.assertFalse(changed['settings']['serve'])
        self.assertEqual(self.request('PATCH','/api/v1/cameras/settings',{'serve':True},{'If-Match':'"'+status['config_revision']+'"'})[0],412)
        self.assertEqual(self.request('GET','/api/v1/cameras?unknown=1')[0],400)
        self.assertEqual(self.request('GET','/api/v1/cameras',headers={'Host':'attacker.invalid'})[0],421)

    def test_cookie_parity_preserves_csrf_and_native_routes_require_bearer(self):
        origin='http://127.0.0.1:'+str(self.server.server_port)
        _,_,cookie=self.request('POST','/api/login',{'password':'local-admin-test-password'},{'Origin':origin})
        browser={'Cookie':cookie.split(';')[0],'Authorization':''}
        self.assertEqual(self.request('GET','/api/cameras',headers=browser)[:2],(200,[]))
        self.assertEqual(self.request('GET','/api/v1/cameras',headers=browser)[0],401)
        status=self.request('GET','/api/cameras/settings',headers=browser)[1]
        self.assertEqual(self.request('PATCH','/api/cameras/settings',{'serve':False},browser)[0],403)
        csrf=self.request('GET','/api/status',headers=browser)[1]['csrf']
        browser.update({'Origin':origin,'X-CSRF-Token':csrf,'If-Match':'"'+status['config_revision']+'"'})
        self.assertEqual(self.request('PATCH','/api/cameras/settings',{'serve':False},browser)[0],200)
        browser['Origin']='http://attacker.invalid'
        self.assertEqual(self.request('PATCH','/api/cameras/settings',{'serve':True},browser)[0],403)

    def test_camera_inventory_projects_only_this_ingress_and_has_no_route_collision(self):
        import http.client,json,threading
        from helpers.contract_schema import validate
        from wifi_bridge.cameras.listener import AlpacaServer,ManagementAccess
        self.login()
        service=self.server.api.cameras.service()
        service.add('simulation','simulator',SimulatedCamera(width=4,height=4))
        self.assertNotIn('alpaca-camera',self.server.api.info()['capabilities'])
        service.set_listening(11111,['192.0.2.1'])
        self.assertIn('alpaca-camera',self.server.api.info()['capabilities'])
        self.assertIsNone(self.request('GET','/api/v1/cameras')[1][0]['alpaca'])
        alpaca=AlpacaServer(ManagementAccess('127.0.0.1','127.0.0.0/8',[]),service,port=0)
        threading.Thread(target=alpaca.serve_forever,daemon=True).start()
        self.addCleanup(alpaca.server_close);self.addCleanup(alpaca.shutdown)
        service.set_listening(alpaca.server_port,['127.0.0.1'])
        rows=self.request('GET','/api/v1/cameras')[1]
        self.assertEqual(rows[0]['alpaca'],{'port':alpaca.server_port,'device_number':0})
        spec=self.server.api.specification()
        validate(rows,spec['components']['schemas']['NativeCameras'],spec)
        self.assertEqual(self.request('GET','/api/v1/camera/0/name')[0],404)
        conn=http.client.HTTPConnection('127.0.0.1',alpaca.server_port)
        conn.request('GET','/api/v1/camera/0/name')
        response=conn.getresponse()
        self.assertEqual(response.status,200)
        self.assertEqual(json.loads(response.read())['Value'],'simulator')
        conn.close()
        conn=http.client.HTTPConnection('127.0.0.1',alpaca.server_port)
        conn.request('GET','/api/v1/cameras')
        response=conn.getresponse()
        self.assertEqual(response.status,404)
        response.read();conn.close()
