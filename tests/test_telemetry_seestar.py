"""Synthetic event schema and generated-key loopback peer; no physical telescope."""
import copy
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from helpers.maker_keys import pem
from wifi_bridge.config import role_for
from wifi_bridge.events import EventStore, valid_event
from wifi_bridge.maker_keys.service import MakerKeys
from wifi_bridge.maker_keys.verify import send, KEEPALIVE_METHODS
from wifi_bridge.telemetry import Sessions, TelemetryStore
from wifi_bridge.telemetry.adapters.seestar_events import ADAPTER, Client, Observer, SeestarCollector, normalize, MAX_LINE
from wifi_bridge.telemetry.adapters.alpaca_observer import AlpacaCollector, Observer as AlpacaObserver
from wifi_bridge.telemetry.engineio import Budget

FIXTURES = [json.loads(line) for line in (Path(__file__).parent/'fixtures/seestar-events-synthetic.jsonl').read_text().splitlines()]


class Peer:
    def __init__(self, refused=False, payload=None, hold=False):
        self.listener=socket.socket();self.listener.bind(('127.0.0.1',0));self.listener.listen(1);self.listener.settimeout(3)
        self.port=self.listener.getsockname()[1];self.methods=[];self.errors=[];self.halt=threading.Event();self.ready=threading.Event()
        def serve():
            try:
                connection,_=self.listener.accept();connection.settimeout(3)
                with connection, connection.makefile('rb') as stream:
                    for index in range(3):
                        value=json.loads(stream.readline(16385));self.methods.append(value['method'])
                        reply={'id':value['id'],'result':{'str':'synthetic challenge'}} if index==0 else {'id':value['id'],'code':9 if refused else 0}
                        data=json.dumps(reply).encode()+b'\n'
                        if index==2: data+=payload or b''
                        connection.sendall(data)
                        if refused and index==1:return
                    self.ready.set()
                    if hold:self.halt.wait(3)
            except (OSError,ValueError): self.errors.append('peer failure')
        self.thread=threading.Thread(target=serve,daemon=True);self.thread.start()
    def factory(self,kind):
        port=self.port
        class Mapped:
            def __init__(self):self.stream=socket.socket(socket.AF_INET,kind)
            def __getattr__(self,name):return getattr(self.stream,name)
            def connect(self,address):self.stream.connect(('127.0.0.1',port))
        return Mapped()
    def close(self):self.halt.set();self.listener.close();self.thread.join(3)


class ClientTests(unittest.TestCase):
    def client(self,peer,**kw):
        return Client('127.0.0.1/8','test0','127.0.0.2',factory=peer.factory,budget=Budget(),clock=time.monotonic,**kw)
    def test_guard_empty_keepalive(self):
        self.assertEqual(KEEPALIVE_METHODS,frozenset())
        for method in ('get_device_state','iscope_start_view','set_setting','start_scan','stop_view','get_image','get_plan','pi_is_verified'):
            with self.assertRaises(ValueError):send(None,4,method,None,keepalive=True)
        for method in ('get_device_state','iscope_get_app_state','set_setting','start_scan','stop_view','get_image','get_plan'):
            with self.assertRaises(ValueError):send(None,4,method,None)
        with self.assertRaises(ValueError): self.client(SimpleNamespace(factory=lambda _:None),keepalive='get_device_state')
    def test_handshake_buffered_events_refusal_and_only_three_methods(self):
        for refused in (False,True):
            data=b''.join(json.dumps(e).encode()+b'\n' for e in FIXTURES)
            peer=Peer(refused,payload=data);self.addCleanup(peer.close)
            client=self.client(peer);seen=[]
            verdict=client.listen(pem(),seen.append,lambda:True)
            self.assertEqual(verdict,'refused' if refused else 'verified')
            self.assertEqual(peer.methods,['get_verify_str','verify_client']+([] if refused else ['pi_is_verified']))
            self.assertEqual(len(seen),0 if refused else 5)
            self.assertEqual(client.peer_closed,not refused)
            self.assertFalse(client.transport.sockets)
    def test_auth_watchdog_canceled_before_long_listener_and_prompt_stop(self):
        peer=Peer(payload=b'{"Event":"PiStatus","temp":21}\n',hold=True);self.addCleanup(peer.close)
        client=self.client(peer);received=threading.Event();timers=[];real_timer=threading.Timer
        class Timer:
            def __init__(self,delay,callback,**kw):
                self.cancelled=False;timers.append(self);self.timer=real_timer(.1,callback,**kw)
            def start(self):self.timer.start()
            def cancel(self):self.cancelled=True;self.timer.cancel()
        def receive(_):
            self.assertTrue(timers[0].cancelled);received.set()
        with patch('wifi_bridge.maker_keys.verify.threading.Timer',Timer):
            worker=threading.Thread(target=lambda:client.listen(pem(),receive,lambda:not client.transport.closed));worker.start()
            self.assertTrue(received.wait(2));time.sleep(.15);self.assertTrue(worker.is_alive());client.close();worker.join(2)
        self.assertFalse(worker.is_alive());self.assertFalse(client.peer_closed)
    def test_long_listener_renews_only_target_port_and_stops_on_grant_failure(self):
        from wifi_bridge.maker_keys.verify import Handshake
        now=[0];reads=[0];grants=[]
        client=Client('127.0.0.1/8','test0','127.0.0.2',factory=lambda _:None,budget=Budget(lambda:now[0]),clock=lambda:now[0])
        class Stream:
            def settimeout(self,_):pass
            def recv(self,_):
                reads[0]+=1;now[0]=61*reads[0]
                return b'{"Event":"PiStatus","temp":20}\n'
        def allow(endpoints):
            grants.append(endpoints)
            if len(grants)==2:raise PermissionError('Local grant expired')
        client.transport.allow=allow
        def run(_self,pem,current,*,listener):
            try:listener(Stream(),bytearray())
            except OSError:return 'unverified'
            return 'verified'
        with patch.object(Handshake,'run',run):client.listen(bytearray(),lambda _:None,lambda:True)
        self.assertEqual(grants,[[('127.0.0.2',4700)],[('127.0.0.2',4700)]])
        self.assertFalse(client.peer_closed);client.close()

    def test_unknown_names_and_memory_bound(self):
        for payload in (b'{"Event":"Unknown","private":"discard"}\n'*400, b'x'*(MAX_LINE+1)):
            peer=Peer(payload=payload);self.addCleanup(peer.close);client=self.client(peer)
            tracemalloc.start();client.listen(pem(),lambda _:self.fail('unknown event retained'),lambda:True)
            _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
            self.assertLess(peak,2*1024**2)
            self.assertLessEqual(client.unknown_events,400)
            self.assertFalse(client.transport.sockets)
    def test_local_permission_cancellation_is_not_device_close(self):
        peer=Peer(hold=True);self.addCleanup(peer.close);client=self.client(peer)
        permission=SimpleNamespace(closed=False,endpoints=lambda _:None,release=lambda _:None,close=lambda:None)
        client.transport.permission=permission
        worker=threading.Thread(target=lambda:client.listen(pem(),lambda _:None,lambda:True));worker.start()
        self.assertTrue(peer.ready.wait(2));permission.closed=True;client.close();worker.join(2)
        self.assertFalse(client.peer_closed);self.assertFalse(worker.is_alive())


class ObserverTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.now=0
        self.store=TelemetryStore(Path(temporary.name)/'telemetry.sqlite3',monotonic=lambda:self.now)
        self.events=EventStore(Path(temporary.name)/'events.json',monotonic=lambda:self.now)
        self.sessions=Sessions(self.store,events=self.events);self.addCleanup(self.sessions.close)
        self.p={'id':'one','kind':'seestar','enabled':True,'mac':'02:00:00:00:00:01','target_ip':'127.0.0.2','ssid':'Synthetic'}
        self.row={'id':'one','connected':True,'interface':'test0','client_ip':'127.0.0.1/8'}
        self.c=SimpleNamespace(lock=threading.RLock(),stop=threading.Event(),demo=False,telemetry_store=self.store,telemetry_sessions=self.sessions,
            event_store=self.events,net=SimpleNamespace(),store=SimpleNamespace(config={'telescopes':[self.p]},path=Path(temporary.name)/'config.json'),
            state={'telescopes':[self.row],'devices':[]},applied={role_for(self.p):copy.deepcopy(self.p)},pauses=SimpleNamespace(held=lambda _:False))
        self.keys=MakerKeys(self.c);self.c.maker_keys=self.keys;self.addCleanup(self.keys.close)
        self.keys.store.put('seestar',pem(),'import');self.keys.enable_session_defaults()
        self.collector=SeestarCollector(self.c);self.c.seestar_collector=self.collector;self.addCleanup(self.collector.close)
        self.worker=Observer(self.collector,self.p,self.row);self.collector.workers['one']=self.worker
    def test_card_receipts_pistatus_does_not_freshen_stack(self):
        self.worker.authenticated();self.worker.receive(FIXTURES[1])
        self.now=61;self.worker.receive(FIXTURES[0])
        readings=self.sessions.readings.snapshot('one',[ADAPTER])
        self.assertTrue(readings['stack.frames_accepted']['stale'])
        self.assertTrue(readings['operation.state']['stale'])
        self.assertFalse(readings['battery.percent']['stale'])
        self.assertEqual(self.collector.operation_state(self.p),'unknown')

    def test_exact_mapping_sensitive_fields_never_store(self):
        expected=[{'device.temperature_c':24.5,'battery.percent':78,'battery.charging':True},
            {'stack.frames_accepted':12,'stack.frames_rejected':3,'stack.reject_reason.4':1,'operation.kind':'stacking','operation.state':'running'},
            {'exposure.seconds':10,'operation.kind':'capture','operation.state':'running'},
            {'error.code':'pointing_failed','operation.kind':'pointing','operation.state':'failed'},
            {'operation.kind':'focusing','operation.state':'completed'}]
        for event,result in zip(FIXTURES,expected):self.assertEqual(normalize(event),result)
        self.sessions.touch('one','seestar',['link_v1',ADAPTER],location=self.p['mac'])
        for event in FIXTURES:self.worker.receive(dict(event,password='private-sentinel',token='private-sentinel',image={'private':'private-sentinel'}))
        self.store.flush();dump='\n'.join(self.store.db.iterdump())
        self.assertFalse('private-sentinel' in dump);self.assertFalse('private-sentinel' in repr(self.worker.previous))
        for value in (True,float('nan'),float('inf'),'private-sentinel',{},[]):
            self.assertEqual(normalize({'Event':'PiStatus','temp':value}),{})
        self.assertEqual(normalize({'Event':'AutoGoto','state':'private-sentinel','code':'private-sentinel'}),{})
    def test_operation_freshness_ignores_pistatus_and_unknown_states(self):
        self.worker.authenticated();self.worker.receive(FIXTURES[1]);self.assertEqual(self.collector.operation_state(self.p),'running')
        self.now=60;self.worker.receive(FIXTURES[0]);self.assertEqual(self.collector.operation_state(self.p),'running')
        self.now=60.001;self.worker.receive(FIXTURES[0]);self.assertEqual(self.collector.operation_state(self.p),'unknown')
        self.worker.receive({'Event':'Stack','state':'idle'});self.assertEqual(self.collector.operation_state(self.p),'idle')
        self.worker.receive({'Event':'Exposure','state':'unreviewed'});self.assertEqual(self.collector.operation_state(self.p),'unknown')
    def test_pointing_failure_transitions_reach_generic_session_summary(self):
        from wifi_bridge.telemetry.query import Query
        ident=self.sessions.touch('one','seestar',['link_v1',ADAPTER],location=self.p['mac'])
        failure={'Event':'AutoGoto','state':'failed','code':7}
        self.worker.receive(failure);self.worker.receive(failure)
        self.worker.receive(FIXTURES[0]);self.worker.receive(FIXTURES[1]);self.worker.receive(failure)
        query=Query(self.store)
        self.assertEqual(query.summary(query.session(ident))['pointing_failures'],1)
        self.worker.receive({'Event':'AutoGoto','state':'running','code':0})
        self.worker.receive(failure);self.worker.receive(failure)
        self.assertEqual(query.summary(query.session(ident))['pointing_failures'],2)

    def test_interleaved_operations_never_clear_another_running_state(self):
        from wifi_bridge.controller import Controller
        self.c.vespera_collector=SimpleNamespace(operation_state=lambda _:None)
        self.c.alpaca_collector=SimpleNamespace(operation_state=lambda _:None)
        self.worker.authenticated();self.worker.receive(FIXTURES[1])
        self.worker.receive({'Event':'AutoFocus','state':'completed'})
        self.worker.receive({'Event':'AutoGoto','state':'idle'})
        self.assertEqual(Controller.operation_state(self.c,self.p),'running')
        self.now=61;self.worker.receive({'Event':'AutoFocus','state':'idle'})
        self.assertEqual(Controller.operation_state(self.c,self.p),'unknown')
        self.worker.receive({'Event':'Exposure','state':'running'})
        self.assertEqual(Controller.operation_state(self.c,self.p),'running')
        self.worker.receive({'Event':'Exposure','state':'unreviewed'})
        self.assertEqual(Controller.operation_state(self.c,self.p),'unknown')
        self.assertEqual(len(self.worker.operations),4)

    def test_backoff_yield_window_and_single_event(self):
        for delay in (2,5,10,30,30):
            self.worker.failed();self.assertEqual(self.worker.retry_at,self.now+delay);self.now+=delay
        self.now=100
        for _ in range(4):self.worker.failed(True);self.now+=1
        self.assertEqual(self.worker.adapter_state,'yielded');self.assertEqual(self.worker.yield_until,703)
        self.worker.failed(True);self.assertEqual(len([e for e in self.events.events if e['type']=='telemetry.adapter_yielded']),1)
        self.assertTrue(valid_event(self.events.events[-1]));self.now=703;self.worker.failed();self.assertEqual(self.worker.adapter_state,'waiting')
        self.worker.closures.extend([1,2,3]);self.now=1000;self.worker.failed(True);self.assertEqual(len(self.worker.closures),1)
    def test_source_key_and_settings_retirement(self):
        self.assertIsNotNone(self.worker.selected())
        for mutate,restore in ((lambda:self.row.update(connected=False),lambda:self.row.update(connected=True)),
            (lambda:self.row.update(interface='test1'),lambda:self.row.update(interface='test0')),
            (lambda:self.p.update(enabled=False),lambda:self.p.update(enabled=True)),
            (lambda:self.p.update(target_ip='127.0.0.3'),lambda:self.p.update(target_ip='127.0.0.2')),
            (lambda:self.c.applied.clear(),lambda:self.c.applied.update({role_for(self.p):copy.deepcopy(self.p)})),
            (lambda:self.store.settings['profiles']['one'].update(adapters=[]),lambda:self.store.settings['profiles']['one'].update(adapters=[ADAPTER]))):
            mutate();self.assertIsNone(self.worker.selected());self.worker.receive(FIXTURES[1]);self.assertFalse(self.sessions.active);restore()
        self.keys.cancel();self.assertIsNone(self.worker.selected())
    def test_defaults_preserve_every_explicit_choice_and_new_profiles(self):
        self.store.settings['profiles']['one']={'adapters':['link_v1'],'alpaca_interval_seconds':30}
        self.keys.enable_session_defaults();self.assertEqual(self.store.settings['profiles']['one'],{'adapters':['link_v1'],'alpaca_interval_seconds':30})
        self.store.settings['profiles']['one']={'adapters':[]};self.keys.enable_session_defaults();self.assertEqual(self.store.settings['profiles']['one'],{'adapters':[]})
        self.c.store.config['telescopes'].append(dict(self.p,id='two'));self.keys.enable_session_defaults();self.assertIn(ADAPTER,self.store.settings['profiles']['two']['adapters'])
    def test_refused_stops_and_emits_event(self):
        peer=Peer(refused=True);self.addCleanup(peer.close)
        def factory(*args,**kw):kw['factory']=peer.factory;return Client(*args,**kw)
        self.collector.client_factory=factory;self.worker.attempt()
        self.assertEqual(self.keys.store.status()['state'],'refused');self.assertEqual(self.worker.adapter_state,'refused');self.assertIsNone(self.worker.selected())
        self.assertEqual(self.events.events[-1]['type'],'telemetry.adapter_refused');self.assertTrue(valid_event(self.events.events[-1]))
    def test_real_listener_stores_normalized_data_and_closes(self):
        peer=Peer(payload=b''.join(json.dumps(e).encode()+b'\n' for e in FIXTURES));self.addCleanup(peer.close)
        def factory(*args,**kw):kw['factory']=peer.factory;return Client(*args,**kw)
        self.collector.client_factory=factory;self.worker.attempt()
        self.assertEqual(self.keys.store.status()['state'],'verified');self.assertEqual(len(self.worker.closures),1)
        self.assertIn('one',self.sessions.active);self.assertEqual(self.worker.previous['stack.frames_accepted'],12)
    def test_isolated_priority_both_orders_preserves_selections(self):
        from wifi_bridge.telemetry.adapters.seestar_events import isolated_priority
        from unittest.mock import MagicMock
        self.p['isolated']=True;self.c.applied[role_for(self.p)]=copy.deepcopy(self.p)
        selected=['link_v1','alpaca_observer_v1',ADAPTER]
        self.store.settings['profiles']['one']['adapters']=selected[:]
        alpaca=AlpacaCollector(self.c,transport_factory=lambda *a,**k:MagicMock());self.addCleanup(alpaca.close)
        # Alpaca was selected first: existing worker must retire without reacquiring.
        self.store.settings['profiles']['one']['adapters'].remove(ADAPTER)
        prior=AlpacaObserver(alpaca,self.p,self.row);alpaca.workers['one']=prior
        self.assertIsNotNone(prior.selected())
        self.store.settings['profiles']['one']['adapters'].append(ADAPTER)
        self.assertIsNone(prior.selected())
        with patch('wifi_bridge.telemetry.adapters.alpaca_observer.Observer.start') as start:
            alpaca.tick();start.assert_not_called()
        prior.transport.close.assert_called_once();self.assertFalse(alpaca.workers)
        # Seestar was selected first: Alpaca never creates a worker or grant.
        with patch('wifi_bridge.telemetry.adapters.alpaca_observer.Observer') as create:
            alpaca.tick();create.assert_not_called()
        self.assertTrue(isolated_priority(self.c,self.p));self.assertFalse(alpaca.available('one'))
        self.assertEqual(self.store.settings['profiles']['one']['adapters'],selected)
        self.p['isolated']=False;self.assertTrue(alpaca.available('one'))
        self.p['isolated']=True;self.keys.store.verdict('seestar','absent');self.assertTrue(alpaca.available('one'))

    def test_status_is_bounded_sanitized_and_counts_expire(self):
        self.worker.authenticated()
        for name in ('Stack','PiStatus','unknown'):self.worker.count(name)
        status=self.collector.status()['one'][0]
        self.assertEqual(status['event_counts_minute']['Stack'],1)
        self.assertEqual(status['event_counts_minute']['unknown'],1)
        self.assertEqual(status['handshakes']['verified'],1)
        self.now=60
        self.assertFalse(any(self.collector.status()['one'][0]['event_counts_minute'].values()))
        self.assertEqual(self.collector.status()['one'][0]['connected_seconds'],60)
        for instant in range(60,200):self.now=instant;self.worker.count('Stack')
        self.assertLessEqual(len(self.worker.event_buckets),60)
        self.keys.store.verdict('seestar','refused');self.worker.adapter_state='refused'
        self.collector.tick()
        self.assertEqual(self.collector.status()['one'][0]['adapter_state'],'refused')
        self.assertEqual(self.collector.status()['one'][0]['handshakes']['verified'],1)

    def test_unknown_event_flood_cannot_grow_retained_state(self):
        for index in range(2000):self.worker.receive({'Event':'Unknown'+str(index),'state':'running','private':'discard'})
        self.assertFalse(self.worker.operations);self.assertFalse(self.worker.previous);self.assertFalse(self.sessions.active)


from test_bridge import Fixture
import test_api as api_fixture


class SeestarHTTPTests(Fixture):
    request=api_fixture.APITests.request
    def test_actual_producer_events_metrics_settings_and_contract(self):
        from wifi_bridge.web import WebServer
        from helpers.contract_schema import validate
        self.configured();self.controller.cycle(0)
        self.server=WebServer(('127.0.0.1',0),self.controller)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.token='';api_fixture.APITests.login(self)
        c=self.controller;p=self.store.config['telescopes'][0];p['kind']='seestar'
        row=c.state['telescopes'][0];row.update(connected=True,interface='test0',client_ip='127.0.0.1/8')
        c.state['home_ip']='127.0.0.1';p['target_ip']='127.0.0.2';c.applied[role_for(p)]=copy.deepcopy(p)
        c.maker_keys.store.put('seestar',pem(),'import');c.maker_keys.enable_session_defaults();c.demo=False
        worker=Observer(c.seestar_collector,p,row);c.seestar_collector.workers[p['id']]=worker
        worker.authenticated();worker.receive(FIXTURES[1]);worker.receive(FIXTURES[0])
        self.assertEqual(c.operation_state(p),'running')
        ident=c.telemetry_sessions.active[p['id']]['id']
        code,page,_=self.request('GET','/api/v1/telemetry/samples?session_id='+ident+'&after=0')
        self.assertEqual(code,200);self.assertTrue(any(s['metric']=='battery.charging' and s['value'] is True for s in page['samples']))
        for _ in range(4):worker.failed(True)
        # Actual refused handshake, while retaining the real shared event store.
        peer=Peer(refused=True);self.addCleanup(peer.close)
        def factory(*args,**kw):kw['factory']=peer.factory;return Client(*args,**kw)
        c.seestar_collector.client_factory=factory;worker.attempt()
        code,page,_=self.request('GET','/api/v1/events?after=0&limit=100');self.assertEqual(code,200)
        spec=self.server.api.specification();validate(page,spec['components']['schemas']['NativeEvents'],spec)
        self.assertTrue(any(e['type']=='telemetry.adapter_yielded' and e['adapter_id']==ADAPTER for e in page['events']))
        self.assertTrue(any(e['type']=='telemetry.adapter_refused' for e in page['events']))
        code,status,_=self.request('GET','/api/v1/telemetry');self.assertEqual(code,200)
        validate(status,spec['components']['schemas']['TelemetryStatus'],spec)
        diagnostic=status['adapter_status'][p['id']][0]
        self.assertEqual(diagnostic['handshakes']['refused'],1)
        self.assertEqual(diagnostic['connection_closes'],4)
        self.assertEqual(diagnostic['adapter_state'],'refused')
        revision=status['config_revision']
        code,body,_=self.request('PATCH','/api/v1/telemetry/settings',{'profiles':{p['id']:{'adapters':['link_v1']}}},{'If-Match':'"'+revision+'"'})
        self.assertEqual(code,200);self.assertNotIn(ADAPTER,body['profiles'][p['id']]['adapters'])
        self.assertIsNone(c.operation_state(p));c.demo=True
