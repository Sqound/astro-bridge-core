"""Camera UI boundaries: actual ownership, migration, discovery and demo isolation."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.cameras.alpaca import AlpacaError, Camera
from wifi_bridge.cameras.listener import Discovery, ManagementAccess
from wifi_bridge.cameras.service import CameraService, load_settings
from wifi_bridge.cameras.simulator import SimulatedCamera


class CameraPolicyTests(unittest.TestCase):
    def test_legacy_settings_discovery_and_persistence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'cameras.json'
            path.write_text(json.dumps({'serve':True,'port':11111,'allowed_subnets':[]}))
            service = CameraService(path=path)
            self.addCleanup(service.close)
            self.assertTrue(load_settings(path)['discover'])
            service.add('simulator','simulator',SimulatedCamera(width=4,height=4))
            service.set_listening(11111,['192.0.2.1'])
            responder = Discovery(ManagementAccess('192.0.2.1','192.0.2.0/24',[]),service,clock=lambda:1)
            self.assertIsNotNone(responder.response(b'alpacadiscovery1','192.0.2.2'))
            service.update_settings({'discover':False})
            self.assertIsNone(responder.response(b'alpacadiscovery1','192.0.2.3'))
            self.assertEqual(service.listening_port('192.0.2.1'),11111)
            self.assertFalse(load_settings(path)['discover'])
            service.update_settings({'discover':True})
            self.assertIsNotNone(responder.response(b'alpacadiscovery1','192.0.2.4'))
            with self.assertRaises(ValueError):service.update_settings({'discover':1})

    def test_supported_default_and_explicit_client_choice(self):
        for bayer,expected in ((False,2),(True,1)):
            camera = Camera('test','simulator',SimulatedCamera(width=2048,height=1024,bayer=bayer),lambda:1,2)
            self.assertEqual(camera.default_bin,expected)
            self.assertEqual(camera.values['numx'],2048//expected)
            camera.call('PUT','connected',{'connected':'true'},1,'192.0.2.2')
            camera.call('PUT','binx',{'binx':'1'},1,'192.0.2.2')
            self.assertEqual(camera.values['binx'],1)
            camera.release()
            self.assertEqual(camera.values['binx'],1)

    def test_stale_owner_same_identity_and_attachment_are_rejected_atomically(self):
        service = CameraService()
        self.addCleanup(service.close)
        service.demo_plug(True)
        def expose():
            service.execute(0,'PUT','connected',{'connected':'true'},7,'192.0.2.2')
            service.execute(0,'PUT','startexposure',{'duration':'10','light':'true'},7,'192.0.2.2')
            return service.inventory()[0]['owner']['generation']
        old = expose()
        service.release('demo-camera')
        current = expose()
        self.assertNotEqual(old,current)
        with self.assertRaises(ValueError):service.release('demo-camera',old)
        self.assertEqual(service.inventory()[0]['state'],'exposing')
        service.demo_plug(False);service.demo_plug(True)
        replacement = expose()
        with self.assertRaises(ValueError):service.release('demo-camera',current)
        self.assertEqual(service.inventory()[0]['owner']['generation'],replacement)
        with self.assertRaises(AlpacaError):
            service.execute(0,'PUT','startexposure',{'duration':'10','light':'true'},8,'192.0.2.3')
        self.assertEqual(service.inventory()[0]['refused']['client_id'],8)
        service.release('demo-camera',replacement)
        self.assertNotIn('owner',service.inventory()[0])
        self.assertNotIn('refused',service.inventory()[0])
        for _ in range(12):service.demo_plug(False);service.demo_plug(True)
        self.assertEqual(service.inventory()[0]['alpaca'],None)


class CameraOwnerHTTPTests(Fixture):
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

    def test_release_scopes_confirmation_revision_and_owner_generation(self):
        self.login()
        service = self.server.api.cameras.service()
        service.demo_plug(True)
        service.execute(0,'PUT','connected',{'connected':'true'},7,'192.0.2.2')
        service.execute(0,'PUT','startexposure',{'duration':'10','light':'true'},7,'192.0.2.2')
        owner = service.inventory()[0]['owner']
        self.controller.state['hotspot']['clients'] = [{'ip':'192.0.2.2','hostname':'Fictional laptop'}]
        row = self.request('GET','/api/v1/cameras')[1][0]
        self.assertEqual(row['owner']['dhcp_name'],'Fictional laptop')
        self.controller.state['hotspot']['clients'] = [{'ip':'192.0.2.3','hostname':'Other device'}]
        self.assertNotIn('dhcp_name',self.request('GET','/api/v1/cameras')[1][0]['owner'])
        path = '/api/v1/cameras/demo-camera/release-owner'
        data = {'confirm_release':True,'owner_generation':owner['generation']}
        headers = {'If-Match':'"'+self.server.api.revision()+'"'}
        for scope in ('read','connect','telemetry','camera'):
            self.server.sessions[self.token]['scopes']=[scope]
            self.assertEqual(self.request('POST',path,data,headers)[0],403)
            self.assertEqual(self.request('POST','/api/v1/cameras/demo',{'plugged':False},headers)[0],403)
        self.server.sessions[self.token]['scopes']=['admin']
        self.assertEqual(self.request('POST',path,data)[0],428)
        self.assertEqual(self.request('POST',path,data,{'If-Match':'"stale"'})[0],412)
        self.assertEqual(self.request('POST',path,{'confirm_release':True},headers)[0],400)
        self.assertEqual(self.request('POST',path,dict(data,owner_generation='0'*32),headers)[0],409)
        self.assertEqual(service.inventory()[0]['state'],'exposing')
        self.assertEqual(self.request('POST',path,data,headers)[:2],(200,{'ok':True}))
        self.assertNotIn('owner',service.inventory()[0])
        self.assertEqual(self.request('POST',path,data,headers)[0],409)
        self.assertEqual(self.request('POST','/api/v1/cameras/demo',{'plugged':False},headers)[0],200)
        self.assertEqual(service.inventory(),[])
        with patch.object(self.controller,'demo',False):
            self.assertEqual(self.request('POST','/api/v1/cameras/demo',{'plugged':True},dict(headers,Host=self.store.config['hostname']+'.local:'+str(self.server.server_port)))[0],404)

    def test_release_cookie_csrf_and_no_implicit_owner_change(self):
        origin='http://127.0.0.1:'+str(self.server.server_port)
        _,_,cookie=self.request('POST','/api/login',{'password':'local-admin-test-password'},{'Origin':origin})
        browser={'Cookie':cookie.split(';')[0],'Authorization':''}
        service=self.server.api.cameras.service();service.demo_plug(True)
        service.execute(0,'PUT','connected',{'connected':'true'},7,'192.0.2.2')
        service.execute(0,'PUT','startexposure',{'duration':'10','light':'true'},7,'192.0.2.2')
        data={'confirm_release':True,'owner_generation':service.inventory()[0]['owner']['generation']}
        path='/api/cameras/demo-camera/release-owner'
        browser['If-Match']='"'+self.server.api.revision()+'"'
        self.assertEqual(self.request('POST',path,data,browser)[0],403)
        browser.update({'Origin':origin,'X-CSRF-Token':self.request('GET','/api/status',headers=browser)[1]['csrf']})
        self.assertEqual(self.request('POST',path,data,browser)[0],200)
