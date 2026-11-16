"""Synthetic Alpaca loopback evidence; never contacts an instrument."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from wifi_bridge.telemetry import Sessions, TelemetryStore
from wifi_bridge.telemetry.alpaca import MEMBERS, MAX_BODY, Transport
from wifi_bridge.telemetry.adapters.alpaca_observer import ADAPTER, AlpacaCollector, Observer, normalize


class Clock:
    now = 0
    def read(self):
        return {'system_utc':'2026-11-04T20:00:00+00:00','corrected_utc':None,
                'clock_status':'unverified','source':'lower_bound','offset_ms':0}


class FakeAlpaca:
    def __init__(self):
        self.requests = []
        self.errors = {}
        self.overrides = {}
        self.transaction_offset = 0
        self.delay = None
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                parsed = urlsplit(self.path)
                args = parse_qs(parsed.query)
                outer.requests.append(('GET',parsed.path,args))
                if outer.delay: outer.delay.wait(3)
                member = parsed.path.rsplit('/',1)[-1]
                values = {'configureddevices':[{'DeviceName':'discarded','UniqueID':'discarded','DeviceType':'Telescope','DeviceNumber':0},
                                               {'DeviceType':'Camera','DeviceNumber':1}],
                          'rightascension':12.5,'declination':-23.2,'tracking':True,'slewing':False,
                          'atpark':False,'camerastate':2,'ccdtemperature':-10.25}
                response = {'ClientTransactionID':int(args['ClientTransactionID'][0])+outer.transaction_offset,
                            'ServerTransactionID':len(outer.requests),'ErrorNumber':outer.errors.get(member,0),
                            'ErrorMessage':'discarded','Value':outer.overrides.get(member,values.get(member))}
                payload = json.dumps(response).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(payload)));self.end_headers()
                try:self.wfile.write(payload)
                except (BrokenPipeError,ConnectionResetError):pass
            def do_PUT(self): outer.requests.append(('PUT',self.path,{}));self.send_error(405)
            def do_POST(self): outer.requests.append(('POST',self.path,{}));self.send_error(405)
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        self.thread.start()
        self.endpoint = ('127.0.0.1',self.server.server_port)

    def close(self):
        if self.delay:self.delay.set()
        self.server.shutdown();self.server.server_close();self.thread.join(2)


def local_transport(address, interface, factory=None, *, target=None, permission=None):
    transport = Transport(address,interface,lambda kind:socket.socket(socket.AF_INET,kind),target=target,permission=permission)
    # macOS exposes only 127.0.0.1 without privileged alias setup.
    transport.peer = lambda host: host == '127.0.0.1' and (target is None or host == target)
    return transport


class TransportTests(unittest.TestCase):
    def test_udp_bound_to_selected_interface_source_and_broadcast_with_response_bounds(self):
        stream = Mock()
        stream.recvfrom.side_effect = [(b'x'*1025,('192.0.2.9',32227)),
            (b'{"AlpacaPort":32323}',('198.51.100.2',32227)),
            (b'{"AlpacaPort":true}',('192.0.2.8',32227)),
            (b'{"AlpacaPort":32323}',('192.0.2.2',32227)),socket.timeout()]
        with patch('wifi_bridge.telemetry.alpaca.socket.socket',return_value=stream),patch.object(socket,'SO_BINDTODEVICE',25,create=True):
            transport = Transport('192.0.2.1/24','wlan7')
            self.assertEqual(transport.discover(),[('192.0.2.2',32323)])
        stream.setsockopt.assert_any_call(socket.SOL_SOCKET,25,b'wlan7\0')
        stream.bind.assert_called_once_with(('192.0.2.1',0))
        stream.sendto.assert_called_once_with(b'alpacadiscovery1',('192.0.2.255',32227))
        self.assertTrue(all(c.args == (1025,) for c in stream.recvfrom.call_args_list))
        stream.close.assert_called_once()

    def test_real_loopback_udp_and_namespace_factory_only(self):
        server = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        server.bind(('127.0.0.1',0));server.settimeout(2);self.addCleanup(server.close)
        seen = []
        def reply():
            payload,peer=server.recvfrom(100)
            seen.append(payload);server.sendto(b'{"aLpAcApOrT":32323}',peer)
        thread=threading.Thread(target=reply,daemon=True);thread.start()
        class UDP:
            def __init__(self):self.sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            def __getattr__(self,name):return getattr(self.sock,name)
            def sendto(self,data,peer):
                seen.append(peer);return self.sock.sendto(data,server.getsockname())
        created=[]
        def factory(kind):created.append(kind);return UDP()
        transport=Transport('127.0.0.1/8','isolated0',factory)
        transport.peer=lambda host:host=='127.0.0.1'
        self.assertEqual(transport.discover(),[('127.0.0.1',32323)])
        thread.join(2);self.assertEqual(created,[socket.SOCK_DGRAM])
        self.assertIn(b'alpacadiscovery1',seen);self.assertIn(('127.255.255.255',32227),seen)

    def test_discovery_flood_bounded_to_32_packets_and_four_endpoints(self):
        stream=Mock();stream.recvfrom.return_value=(b'{}',('192.0.2.2',32227))
        transport=Transport('192.0.2.1/24','wlan7',lambda _:stream)
        self.assertEqual(transport.discover(),[]);self.assertEqual(stream.recvfrom.call_count,32)
        stream.reset_mock();stream.recvfrom.side_effect=[(json.dumps({'AlpacaPort':i}).encode(),('192.0.2.2',32227)) for i in range(1,10)]
        self.assertEqual(len(transport.discover()),4);self.assertEqual(stream.recvfrom.call_count,4)

    def test_http_allowlist_rejects_all_control_paths_before_socket(self):
        factory=Mock();transport=Transport('192.0.2.1/24','wlan1',factory)
        allowed={'telescope':('rightascension','declination','tracking','slewing','atpark'),
                 'camera':('camerastate','ccdtemperature')}
        self.assertEqual(MEMBERS,allowed)
        for kind in ('telescope','camera','switch',None):
            for member in ('action','commandblind','commandbool','commandstring','pulseguide','connected',
                           'slewtoaltaz','startexposure','tracking?Value=true','configureddevices/../action'):
                with self.subTest(kind=kind,member=member),self.assertRaises(ValueError):
                    transport.get(('192.0.2.2',32323),kind,0,member,1,1)
        for endpoint in (('198.51.100.2',32323),('192.0.2.1',32323),('192.0.2.255',32323),('192.0.2.2',True)):
            with self.assertRaises(ValueError):transport.get(endpoint,'camera',0,'camerastate',1,1)
        factory.assert_not_called()

    def test_real_http_only_get_transactions_size_and_mismatch(self):
        server=FakeAlpaca();self.addCleanup(server.close)
        transport=local_transport('127.0.0.1/8','loop');self.addCleanup(transport.close)
        self.assertEqual(transport.get(server.endpoint,'telescope',0,'rightascension',42,1),(0,12.5))
        self.assertEqual(server.requests,[('GET','/api/v1/telescope/0/rightascension',{'ClientID':['42'],'ClientTransactionID':['1']})])
        server.transaction_offset=1
        with self.assertRaises(ValueError):transport.get(server.endpoint,'camera',1,'camerastate',42,2)
        server.overrides['ccdtemperature']='x'*MAX_BODY
        with self.assertRaises(ValueError):transport.get(server.endpoint,'camera',1,'ccdtemperature',42,3)

    def test_close_promptly_cancels_stalled_request(self):
        server=FakeAlpaca();server.delay=threading.Event();self.addCleanup(server.close)
        transport=local_transport('127.0.0.1/8','loop')
        errors=[]
        def get():
            try:transport.get(server.endpoint,'camera',1,'camerastate',1,1)
            except (OSError,ValueError,http.client.HTTPException) as exc:errors.append(type(exc).__name__)
        thread=threading.Thread(target=get,daemon=True);thread.start()
        deadline=time.monotonic()+2
        while not server.requests and time.monotonic()<deadline:time.sleep(.005)
        started=time.monotonic();transport.close();thread.join(1)
        self.assertFalse(thread.is_alive());self.assertLess(time.monotonic()-started,1);self.assertTrue(errors)


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.clock=Clock()
        self.store=TelemetryStore(Path(self.tmp.name)/'telemetry.sqlite3',clock=self.clock,monotonic=lambda:self.clock.now)
        self.sessions=Sessions(self.store);self.addCleanup(self.sessions.close)
        self.p={'id':'one','kind':'generic','mac':'02:00:00:00:00:01','target_ip':'127.0.0.1','enabled':True}
        self.row={'id':'one','connected':True,'interface':'loop','client_ip':'127.0.0.1/8'}
        self.c=SimpleNamespace(lock=threading.RLock(),stop=threading.Event(),demo=False,telemetry_store=self.store,
            telemetry_sessions=self.sessions,store=SimpleNamespace(config={'telescopes':[self.p]}),net=SimpleNamespace(),
            state={'telescopes':[self.row],'devices':[]})
        self.store.settings['profiles']['one']={'adapters':[ADAPTER]}
        self.server=FakeAlpaca();self.addCleanup(self.server.close)
        self.collector=AlpacaCollector(self.c,transport_factory=local_transport);self.addCleanup(self.collector.close)
        self.observer=Observer(self.collector,self.p,self.row);self.addCleanup(self.observer.stop)
        self.observer.transport.discover=lambda:[self.server.endpoint]

    def samples(self):return self.store.samples(self.sessions.active['one']['id'],limit=500)

    def test_all_members_real_server_cadence_transactions_and_no_raw_fields(self):
        self.observer.poll()
        self.assertEqual(len(self.server.requests),8)
        self.assertEqual({s['metric']:s['num'] if s['text'] is None else s['text'] for s in self.samples()},
            {'target.ra_hours':12.5,'target.dec_deg':-23.2,'mount.tracking':1,'mount.slewing':0,
             'mount.at_park':0,'camera.state':'exposing','device.temperature_c':-10.25})
        for now in (0,1,14.999):self.clock.now=now;self.observer.poll()
        self.assertEqual(len(self.server.requests),8)
        self.clock.now=15;self.observer.poll();self.assertEqual(len(self.server.requests),15)
        self.assertEqual({r[0] for r in self.server.requests},{'GET'})
        self.assertEqual([int(r[2]['ClientTransactionID'][0]) for r in self.server.requests],list(range(1,16)))
        self.assertEqual(len({r[2]['ClientID'][0] for r in self.server.requests}),1)
        self.store.flush();dump=str(list(self.store.db.iterdump()))+repr(vars(self.observer))
        for forbidden in ('discarded','DeviceName','UniqueID','ErrorMessage'):self.assertNotIn(forbidden,dump)

    def test_alpaca_error_omits_only_member_for_exact_ten_minutes(self):
        self.server.errors={'ccdtemperature':1024,'rightascension':1031}
        self.observer.poll()
        self.assertNotIn('device.temperature_c',{s['metric'] for s in self.samples()})
        self.server.errors={}
        for now in (15,30,599):self.clock.now=now;self.observer.poll()
        for member in ('ccdtemperature','rightascension'):
            self.assertEqual(sum(r[1].endswith('/'+member) for r in self.server.requests),1)
        self.clock.now=600;self.observer.next_poll=600;self.observer.poll()
        for member in ('ccdtemperature','rightascension'):
            self.assertEqual(sum(r[1].endswith('/'+member) for r in self.server.requests),2)
        self.assertFalse(self.observer.unavailable)

    def test_management_error_cooldown_survives_empty_rediscovery(self):
        self.server.errors={'configureddevices':1024};self.observer.poll()
        self.clock.now=60;self.observer.transport.discover=lambda:[];self.observer.poll()
        self.clock.now=120;self.observer.transport.discover=lambda:[self.server.endpoint];self.observer.poll()
        self.assertEqual(len(self.server.requests),1)
        self.clock.now=600;self.server.errors={};self.observer.poll()
        self.assertEqual(len(self.server.requests),9)

    def test_configurable_cadence_and_idle_does_not_start_or_extend(self):
        self.store.settings['profiles']['one']['alpaca_interval_seconds']=120
        self.server.overrides={'tracking':False,'camerastate':0};self.observer.poll()
        self.assertFalse(self.sessions.active)
        self.clock.now=119;self.observer.poll();self.assertEqual(len(self.server.requests),8)
        self.clock.now=120;self.server.overrides={};self.observer.poll();self.assertIn('one',self.sessions.active)
        self.server.overrides={'tracking':False,'camerastate':5};self.clock.now=240;self.observer.poll()
        self.assertEqual(self.sessions.active['one']['last_activity'],120)
        self.clock.now=1920;self.observer.poll();self.assertFalse(self.sessions.active)

    def test_normalize_strict_values_and_ranges(self):
        for kind,member,value in (('telescope','rightascension',True),('telescope','declination',91),
                ('telescope','tracking','true'),('camera','camerastate',True),('camera','camerastate',6),
                ('camera','ccdtemperature',float('nan')),('camera','ccdtemperature',-274),('camera','action',0)):
            self.assertIsNone(normalize(kind,member,value))
        for i,name in enumerate(('idle','waiting','exposing','reading','downloading','error')):
            self.assertEqual(normalize('camera','camerastate',i),('camera.state',name))

    def test_device_inventory_bounds_select_one_deterministic_device_per_type(self):
        self.server.overrides['configureddevices']=[{'DeviceType':'Camera','DeviceNumber':4},
            {'DeviceType':'Telescope','DeviceNumber':3},{'DeviceType':'Telescope','DeviceNumber':0},
            {'DeviceType':'Camera','DeviceNumber':1},{'DeviceType':'Switch','DeviceNumber':0},
            {'DeviceType':'Camera','DeviceNumber':True}]
        self.observer.poll();self.assertEqual(len(self.server.requests),8)
        self.assertEqual(self.observer.devices,[(self.server.endpoint,'telescope',0),(self.server.endpoint,'camera',1)])
        self.server.overrides['configureddevices']=[{}]*33
        self.clock.now=600;self.observer.poll();self.assertFalse(self.observer.devices)

    def test_owner_disable_disconnect_move_demo_and_late_response_are_rejected(self):
        for field,value in (('demo',True),('connected',False),('interface','changed'),('client_ip','127.0.0.3/8'),('target_ip','127.0.0.9'),('enabled',False)):
            target=self.c if field=='demo' else self.row if field in ('connected','interface','client_ip') else self.p
            old=getattr(target,field) if field=='demo' else target[field]
            if field=='demo':target.demo=value
            else:target[field]=value
            self.observer.poll();self.assertFalse(self.server.requests)
            if field=='demo':target.demo=old
            else:target[field]=old
        self.store.settings['profiles']['one']={'adapters':[]};self.observer.poll();self.assertFalse(self.server.requests)
        self.store.settings['profiles']['one']={'adapters':[ADAPTER]}
        original=self.observer.transport.get
        def disable(*a):
            result=original(*a)
            if a[3] == 'ccdtemperature': self.store.settings['profiles']['one']={'adapters':[]}
            return result
        self.observer.transport.get=disable;self.observer.poll();self.assertFalse(self.sessions.active)
        self.assertEqual(len(self.server.requests),8)

    def test_metadata_does_not_wait_for_stalled_worker_and_optout_cancels(self):
        self.server.delay=threading.Event();self.observer.start();self.collector.workers['one']=self.observer
        deadline=time.monotonic()+2
        while not self.server.requests and time.monotonic()<deadline:time.sleep(.005)
        started=time.monotonic()
        with self.c.lock:self.assertTrue(self.collector.available('one'))
        self.assertLess(time.monotonic()-started,.1)
        self.store.settings['profiles']['one']={'adapters':[]};self.collector.tick()
        self.assertFalse(self.observer.thread.is_alive());self.assertFalse(self.collector.workers)
        self.assertFalse(self.sessions.active)

    def test_collector_optin_demo_namespace_and_fixed_bridge_identity(self):
        factory=Mock();self.collector.transport_factory=factory
        self.c.demo=True;self.collector.tick();factory.assert_not_called()
        self.c.demo=False;self.store.settings['profiles'].pop('one');self.collector.tick();factory.assert_not_called()
        self.store.settings['profiles']['one']={'adapters':[]};self.collector.tick();factory.assert_not_called()
        self.store.settings['profiles']['one']={'adapters':[ADAPTER]};self.p['isolated']=True
        self.collector.tick();factory.assert_not_called()
        self.c.state['devices']=[{'mac':self.p['mac']}]
        namespace_factory=Mock()
        access=SimpleNamespace(socket=namespace_factory,close=lambda:None)
        self.c.net.isolated=SimpleNamespace(record_for=lambda d:'namespace-record',observer_access=lambda *a:access)
        with patch.object(Observer,'start'):
            self.collector.tick()
        factory.assert_called_once_with('127.0.0.1/8','loop',namespace_factory,target='127.0.0.1',permission=access)
        other=AlpacaCollector(self.c)
        self.assertEqual(other.client_id,self.collector.client_id)
        self.collector.counter=2**32-1;self.assertEqual(self.collector.transaction(),1)

from test_bridge import Fixture
import test_api as api_fixture


class AlpacaHTTPTests(Fixture):
    def setup_http(self):
        from wifi_bridge.web import WebServer
        self.configured();self.controller.cycle(0)
        self.server=WebServer(('127.0.0.1',0),self.controller)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.token='';api_fixture.APITests.login(self)
        self.addCleanup(self.controller.telemetry_sessions.close)

    def request(self,*args,**kwargs):return api_fixture.APITests.request(self,*args,**kwargs)

    def test_real_http_settings_optin_bounds_merge_persistence_and_scopes(self):
        from helpers.contract_schema import validate
        self.setup_http()
        spec=json.loads((Path(__file__).resolve().parents[1]/'wifi_bridge/static/openapi.json').read_text())
        def change(selection):
            revision=self.server.api.revision()
            return self.request('PATCH','/api/v1/telemetry/settings',{'profiles':{'primary':selection}}, {'If-Match':'"'+revision+'"'})
        self.assertEqual(change({})[0],400)
        for bad in (True,9,121,15.5,'15'):
            self.assertEqual(change({'adapters':[ADAPTER],'alpaca_interval_seconds':bad})[0],400)
        status,body,_=change({'adapters':[ADAPTER],'alpaca_interval_seconds':10})
        self.assertEqual(status,200)
        validate(body,spec['components']['schemas']['TelemetryStatus'],spec)
        self.assertEqual(body['profiles']['primary'],{'adapters':[ADAPTER],'alpaca_interval_seconds':10})
        self.assertEqual(change({'adapters':[]})[1]['profiles']['primary'],{'adapters':[],'alpaca_interval_seconds':10})
        source=self.store.config['telescopes'][0]
        self.store.config['telescopes'].append(dict(source,id='second',enabled=False))
        for patch_body in ({'retention_nights':3},{'profiles':{'second':{'adapters':['link_v1']}}}):
            revision=self.server.api.revision()
            code,retained_body,_=self.request('PATCH','/api/v1/telemetry/settings',patch_body,{'If-Match':'"'+revision+'"'})
            self.assertEqual(code,200)
            self.assertEqual(retained_body['profiles']['primary'],{'adapters':[],'alpaca_interval_seconds':10})
        retained=json.loads(self.controller.telemetry_store.db.execute("SELECT value FROM metadata WHERE key='settings'").fetchone()[0])
        self.assertEqual(retained['profiles']['primary']['alpaca_interval_seconds'],10)
        self.server.sessions[self.token]['scopes']=['telemetry']
        self.assertEqual(change({'adapters':[ADAPTER],'alpaca_interval_seconds':120})[0],403)
        self.assertEqual(self.request('GET','/api/v1/telemetry')[0],200)
        self.assertEqual(self.controller.instrument_catalog(self.controller.status())[0]['capabilities']['telemetry'],[])

    def test_actual_loopback_observer_metrics_native_http_and_selected_capability(self):
        self.setup_http()
        fake=FakeAlpaca();self.addCleanup(fake.close)
        p=self.store.config['telescopes'][0]
        self.controller.telemetry_store.settings['profiles']['primary']={'adapters':[ADAPTER]}
        p['target_ip']='127.0.0.1'
        row=dict(self.controller.state['telescopes'][0],client_ip='127.0.0.1/8')
        self.controller.state['telescopes'][0]=row
        self.controller.demo=False
        collector=self.controller.alpaca_collector;collector.transport_factory=local_transport
        observer=Observer(collector,p,row);self.addCleanup(observer.stop)
        observer.transport.discover=lambda:[fake.endpoint]
        observer.poll()
        ident=self.controller.telemetry_sessions.active['primary']['id']
        self.controller.demo=True
        code,body,_=self.request('GET','/api/v1/telemetry/samples?session_id='+ident+'&after=0')
        self.assertEqual(code,200)
        values={s['metric']:s['value'] for s in body['samples']}
        self.assertEqual(values['camera.state'],'exposing');self.assertEqual(values['target.ra_hours'],12.5)
        self.assertIs(values['mount.tracking'],True)
        from helpers.contract_schema import validate
        spec=json.loads((Path(__file__).resolve().parents[1]/'wifi_bridge/static/openapi.json').read_text())
        validate(body,spec['components']['schemas']['NativeTelemetryPage'],spec)
        self.controller.demo=False
        self.assertIn(ADAPTER,self.controller.instrument_catalog(self.controller.status())[0]['capabilities']['telemetry'])
        self.controller.demo=True
        self.assertNotIn(ADAPTER,self.controller.instrument_catalog(self.controller.status())[0]['capabilities']['telemetry'])

class IdentityTests(unittest.TestCase):
    setUp = ObserverTests.setUp
    def test_unrelated_discovery_peer_never_observed_and_target_change_retires(self):
        self.observer.transport.discover=lambda:[('127.0.0.9',32323),self.server.endpoint]
        self.observer.poll()
        self.assertEqual(len(self.server.requests),8)
        self.assertEqual({device[0] for device in self.observer.devices},{self.server.endpoint})
        self.assertIsNone(self.observer.read(('127.0.0.9',32323),'telescope',0,'tracking'))
        self.collector.workers['one']=self.observer
        self.p['target_ip']='127.0.0.9'
        with patch.object(Observer,'start'):
            self.collector.tick()
        self.assertTrue(self.observer.halt.is_set())
        self.assertIsNot(self.collector.workers['one'],self.observer)
        self.assertEqual(self.collector.workers['one'].profile['target_ip'],'127.0.0.9')

    def test_transport_target_filter_rejects_other_alpaca_on_same_subnet(self):
        stream=Mock();stream.recvfrom.side_effect=[(b'{"AlpacaPort":32323}',('192.0.2.8',32227)),
            (b'{"AlpacaPort":32323}',('192.0.2.2',32227)),socket.timeout()]
        transport=Transport('192.0.2.1/24','wlan1',lambda _:stream,target='192.0.2.2')
        self.assertEqual(transport.discover(),[('192.0.2.2',32323)])
        self.assertFalse(transport.peer('192.0.2.8'))
        with self.assertRaises(ValueError):transport.get(('192.0.2.8',32323),'telescope',0,'tracking',1,1)

    def test_minimum_ten_second_cadence_is_enforced(self):
        self.store.settings['profiles']['one']['alpaca_interval_seconds']=10
        self.observer.poll();self.assertEqual(len(self.server.requests),8)
        self.clock.now=9.999;self.observer.poll();self.assertEqual(len(self.server.requests),8)
        self.clock.now=10;self.observer.poll();self.assertEqual(len(self.server.requests),15)

    def test_firewall_endpoint_refresh_is_per_poll_not_per_member(self):
        permission=Mock()
        observer=Observer(self.collector,self.p,self.row,permission=permission);self.addCleanup(observer.stop)
        observer.transport.discover=lambda:[self.server.endpoint]
        observer.poll();self.assertEqual(len(self.server.requests),8)
        self.assertEqual(permission.endpoints.call_count,2)
        self.clock.now=15;observer.poll();self.assertEqual(len(self.server.requests),15)
        self.assertEqual(permission.endpoints.call_count,3)

    def test_owner_optout_closes_namespace_permission_with_transport(self):
        permission=Mock()
        observer=Observer(self.collector,self.p,self.row,permission=permission)
        self.collector.workers['one']=observer
        self.store.settings['profiles']['one']={'adapters':[]}
        self.collector.tick()
        self.assertTrue(observer.halt.is_set());permission.close.assert_called_once()
        self.assertFalse(self.collector.workers)
