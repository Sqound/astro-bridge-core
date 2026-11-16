"""Real HTTP authorization plus bounded owner actions and stale verification."""
import contextlib
import io
import json
import logging
import threading
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.api import ApiError
from wifi_bridge.web import WebServer
from wifi_bridge.maker_keys.store import KeyStore
from helpers.maker_keys import pem


class MakerKeyHTTPTests(Fixture):
    def setUp(self):
        super().setUp();self.configured();self.controller.cycle(0)
        self.server=WebServer(('127.0.0.1',0),self.controller)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.token=''
        self.service=self.controller.maker_keys

    from test_api import APITests as _HTTP
    request=_HTTP.request
    login=_HTTP.login
    del _HTTP

    def test_scopes_metadata_import_limits_revision_and_catalog(self):
        from helpers.contract_schema import validate
        path='/api/v1/maker-keys'
        self.assertEqual(self.request('GET',path)[0],401);self.login()
        for scope in ('read','connect','telemetry','camera'):
            self.server.sessions[self.token]['scopes']=[scope]
            self.assertEqual(self.request('GET',path)[0],403)
            self.assertEqual(self.request('PUT',path+'/seestar',{'pem':'invalid'})[0],403)
        self.server.sessions[self.token]['scopes']=['admin']
        first=self.request('GET',path)[1]
        spec=self.server.api.specification();validate(first,spec['components']['schemas']['MakerKeys'],spec)
        for value in ('invalid','x'*8193,123,None):
            code,body,_=self.request('PUT',path+'/seestar',{'pem':value})
            self.assertEqual(code,422);self.assertEqual(body['error']['code'],'KEY_UNUSABLE')
        key=pem().decode('ascii')
        code,stored,_=self.request('PUT',path+'/seestar',{'pem':key})
        self.assertEqual(code,200);self.assertEqual(stored['keys'][0]['state'],'unverified')
        self.assertFalse(key in json.dumps(stored),'Response never includes key')
        self.assertNotEqual(first['config_revision'],stored['config_revision'])
        self.controller.store.config['telescopes'][0]['kind']='seestar'
        item=self.request('GET','/api/v1/instruments')[1]['instruments'][0]
        self.assertEqual(item['capabilities']['maker_key'],'unverified')
        for header,status in (({},428),({'If-Match':'*'},428),({'If-Match':'"stale"'},412)):
            self.assertEqual(self.request('DELETE',path+'/seestar',headers=header)[0],status)
        revision=self.request('GET',path)[1]['config_revision']
        code,deleted,_=self.request('DELETE',path+'/seestar',headers={'If-Match':'"'+revision+'"'})
        self.assertEqual(code,200);self.assertEqual(deleted['keys'][0]['state'],'absent')
        self.assertEqual(self.request('GET',path+'?unexpected=1')[0],400)
        self.assertEqual(self.request('PUT',path+'/unknown',{'pem':'invalid'})[0],404)

    def test_cookie_mirror_csrf_and_no_secret_logs_or_public_config(self):
        self.login();origin='http://127.0.0.1:'+str(self.server.server_port)
        _,body,cookie=self.request('POST','/api/login',{'password':'local-admin-test-password'},{'Origin':origin})
        headers={'Cookie':cookie.split(';')[0],'Authorization':'','Origin':origin}
        self.assertEqual(self.request('GET','/api/maker-keys',headers=headers)[0],200)
        self.assertEqual(self.request('PUT','/api/maker-keys/seestar',{'pem':'invalid'},headers)[0],403)
        headers['X-CSRF-Token']=body['csrf']
        key=pem().decode();capture=io.StringIO();handler=logging.StreamHandler(capture)
        logging.getLogger().addHandler(handler)
        try:
            with contextlib.redirect_stdout(capture),contextlib.redirect_stderr(capture):
                self.assertEqual(self.request('PUT','/api/maker-keys/seestar',{'pem':key},headers)[0],200)
                self.assertEqual(self.request('GET','/api/v1/maker-keys',headers=headers)[0],401)
                code,public,_=self.request('GET','/api/status',headers=headers)
                self.assertEqual(code,200)
                code,diagnostics,_=self.request('GET','/api/https',headers=headers)
                self.assertEqual(code,200)
        finally: logging.getLogger().removeHandler(handler)
        self.assertFalse(any(line in capture.getvalue() for line in key.splitlines()),'No key line in logs')
        self.assertFalse(key in json.dumps(public),'Status/config response excludes key')
        self.assertFalse(key in json.dumps(diagnostics),'Diagnostics exclude key')
        self.assertFalse(key in self.store.path.read_text(),'Config backup source excludes key')
        self.assertFalse(key in json.dumps(self.controller.event_store.events),'Events exclude key')
        headers['Origin']='http://untrusted.invalid'
        self.assertEqual(self.request('DELETE','/api/maker-keys/seestar',headers=headers)[0],403)

    def test_fetch_no_uplink_running_rate_limits_and_operation_schema(self):
        from helpers.contract_schema import validate
        self.login();self.controller.state['home_ip']=''
        path='/api/v1/maker-keys/seestar/fetch'
        self.assertEqual(self.request('POST',path,{})[1]['error']['code'],'NO_INTERNET')
        gate=threading.Event();entered=threading.Event()
        class Fetch:
            journal=[{'step':'complete','verdict':'accepted'}]
            def fetch(self): entered.set();gate.wait(5);return pem()
            def close(self): gate.set()
        self.service.fetch_factory=Fetch;self.controller.state['home_ip']='192.0.2.10'
        self.addCleanup(gate.set)
        code,value,_=self.request('POST',path,{})
        self.assertEqual(code,202);self.assertTrue(entered.wait(2))
        self.assertEqual(self.request('POST',path,{})[1]['error']['code'],'FETCH_RUNNING')
        gate.set()
        for worker in list(self.service.workers): worker.join(3)
        code,result,_=self.request('GET',value['operation']['status_url'])
        self.assertEqual(code,200);self.assertEqual(result['operation']['state'],'succeeded')
        spec=self.server.api.specification();validate(result,spec['components']['schemas']['MakerKeyOperationResult'],spec)
        for _ in range(2):
            self.assertEqual(self.request('POST',path,{})[0],202)
            for worker in list(self.service.workers): worker.join(3)
        self.assertEqual(self.request('POST',path,{})[0],429)
        self.service.clock=lambda:self.service.attempts[-1]+3601
        self.assertEqual(self.request('POST',path,{})[0],202)
        for worker in list(self.service.workers): worker.join(3)

    def test_delete_cancels_fetch_and_late_bytes_never_restore_key(self):
        self.login();entered=threading.Event();gate=threading.Event();self.addCleanup(gate.set)
        class Fetch:
            journal=[]
            def fetch(self):entered.set();gate.wait(3);return pem()
            def close(self):pass
        self.service.fetch_factory=Fetch
        code,value,_=self.request('POST','/api/v1/maker-keys/seestar/fetch',{})
        self.assertEqual(code,202);self.assertTrue(entered.wait(2))
        revision=self.request('GET','/api/v1/maker-keys')[1]['config_revision']
        self.assertEqual(self.request('DELETE','/api/v1/maker-keys/seestar',headers={'If-Match':'"'+revision+'"'})[0],200)
        gate.set()
        for worker in list(self.service.workers):worker.join(3)
        self.assertEqual(self.service.store.status()['state'],'absent')
        self.assertEqual(self.request('GET',value['operation']['status_url'])[1]['operation']['verdict'],'cancelled')
        self.assertIsNone(self.service.store.read('seestar'))

    def test_decryption_failure_discards_verification_transport(self):
        from wifi_bridge.maker_keys.store import StorageUnavailable
        closed=[]
        class Handshake:
            def __init__(self,*args,**kwargs):pass
            def close(self):closed.append(True)
        self.service.handshake_factory=Handshake
        source=('primary','seestar','02:00:00:00:00:01','192.0.2.1',False,'test0','192.0.2.2/24','Test','')
        with patch.object(self.service,'source',return_value=source),patch.object(self.service.store,'read',side_effect=StorageUnavailable()):
            self.service.verify('seestar',self.service.generation)
        self.assertEqual(closed,[True]);self.assertFalse(self.service.transports)
        self.service.handshake_factory=lambda *a,**k: (_ for _ in ()).throw(OSError('unreachable'))
        with patch.object(self.service,'source',return_value=source):self.service.verify('seestar',self.service.generation)
        self.assertFalse(self.service.transports)

    def test_storage_unavailable_is_visible_and_refuses_import(self):
        self.login()
        with patch('ctypes.util.find_library',return_value=None), patch('ctypes.CDLL',side_effect=OSError('unavailable')): self.service.store=KeyStore(self.store.path.parent/'unavailable')
        self.assertEqual(self.request('GET','/api/v1/maker-keys')[1]['keys'][0]['state'],'storage_unavailable')
        self.assertEqual(self.request('PUT','/api/v1/maker-keys/seestar',{'pem':pem().decode()})[0],503)

    def test_verdict_source_generation_and_removal_guards(self):
        self.controller.demo=False
        self.controller.store.config['telescopes'][0]['kind']='seestar'
        row=self.controller.state['telescopes'][0];row.update(connected=True,interface='test0',client_ip='192.0.2.2/24')
        import copy
        self.controller.applied['telescope']=copy.deepcopy(self.controller.store.config['telescopes'][0])
        entered=threading.Event();gate=threading.Event()
        self.addCleanup(gate.set)
        class Handshake:
            verdict='verified'
            def __init__(self,*args,**kwargs): pass
            def run(self,key,current): entered.set();gate.wait(3);return self.verdict
            def close(self): pass
        self.service.handshake_factory=Handshake
        def put():
            with self.controller.lock: self.service.import_key('seestar',pem().decode())
            self.assertTrue(entered.wait(2))
        put()
        with self.controller.lock:
            self.service.cancel();self.service.store.verdict('seestar','absent')
        gate.set()
        for worker in list(self.service.workers): worker.join(3)
        self.assertEqual(self.service.store.status()['state'],'absent')
        entered.clear();gate.clear();put()
        self.controller.store.config['telescopes'][0]['target_ip']='192.0.2.9'
        gate.set()
        for worker in list(self.service.workers): worker.join(3)
        self.assertEqual(self.service.store.status()['state'],'unverified')
        self.controller.applied['telescope']=copy.deepcopy(self.controller.store.config['telescopes'][0])
        entered.clear();Handshake.verdict='refused';put()
        for worker in list(self.service.workers): worker.join(3)
        self.assertEqual(self.service.store.status()['state'],'refused')
        self.assertIsNone(self.service.store.read('seestar'))
