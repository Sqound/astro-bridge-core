"""Synthetic status and loopback Engine.IO evidence; no physical telescope."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

from wifi_bridge.events import EventStore, valid_event
from wifi_bridge.telemetry import Sessions, TelemetryStore
from wifi_bridge.telemetry.adapters.vespera_status import ADAPTER, Observer, VesperaCollector, normalize
from wifi_bridge.telemetry.engineio import Budget, Client, MAX_PAYLOAD, MINUTE_BYTES, MINUTE_REQUESTS, PeerClosed, PollingHTTP, ProtocolError, encode, frames

ROOT = Path(__file__).resolve().parents[1]
STATUS = json.loads((ROOT/'tests/fixtures/vespera-status-synthetic.json').read_text())


class Clock:
    def __init__(self): self.now = 0
    def read(self):
        return {'system_utc':'2026-11-04T20:00:00+00:00', 'corrected_utc':None,
                'clock_status':'unverified', 'source':'lower_bound', 'offset_ms':0}


class FakeEngineServer:
    def __init__(self, *, pong=True, close=False):
        self.pong, self.close_peer = pong, close
        self.requests = []
        self.responses = queue.Queue()
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def reply(self, body):
                self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers()
                try: self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError): pass
            def do_GET(self):
                parsed = urlsplit(self.path); args = parse_qs(parsed.query)
                outer.requests.append(('GET',parsed.path,args,None))
                if 'sid' not in args:
                    opening = '0'+json.dumps({'sid':'synthetic-session', 'pingInterval':250,'pingTimeout':250,'upgrades':['websocket']})
                    self.reply(encode(opening)+encode('40')+encode('42'+json.dumps(['STATUS_UPDATED', STATUS])))
                elif outer.close_peer: self.reply(encode('1'))
                else:
                    try: data = outer.responses.get(timeout=.2)
                    except queue.Empty: data = encode('6')
                    self.reply(data)
            def do_POST(self):
                body = self.rfile.read(min(1024,int(self.headers.get('Content-Length','0'))))
                outer.requests.append(('POST',urlsplit(self.path).path,parse_qs(urlsplit(self.path).query),body))
                if body == encode('2') and outer.pong: outer.responses.put(encode('3'))
                self.reply(b'ok')
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        self.thread.start()

    def transport(self): return PollingHTTP('127.0.0.1',port=self.server.server_port)
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)


class EngineTests(unittest.TestCase):
    def test_multiple_packets_utf16_lengths_and_binaryless_rejection(self):
        packets = ['0{}','40','42["STATUS_UPDATED",{"name":"星 🌌"}]','3','6']
        self.assertEqual(list(frames(b''.join(encode(p) for p in packets))),packets)
        for data in (b'',b'3:40',b'0:',b'1000000:4',b'1:b',b'\x00\x014',b'2:4\xff',b'1:4junk',b'2:4'):
            with self.subTest(data=data), self.assertRaises(ProtocolError): list(frames(data))
        with self.assertRaises(ProtocolError): list(frames(b'x'*(MAX_PAYLOAD+1)))

    def test_open_validation_and_only_heartbeat_posts_are_possible(self):
        transport = SimpleNamespace(request=lambda *a,**k:b'ok',close=lambda:None)
        client = Client(transport,clock=lambda:0)
        for opening in ('0{}','0[]','0{"sid":"invalid?","pingInterval":1,"pingTimeout":1}',
                        '0{"sid":"synthetic","pingInterval":1,"pingTimeout":1}'):
            with self.assertRaises(ProtocolError): client.open(opening)
        with self.assertRaises(ProtocolError): client.send('42["takeControl",{}]')
        with self.assertRaises(ProtocolError): client.request('42["takeControl",{}]')
        client.receive(encode('0{"sid":"synthetic","pingInterval":1000,"pingTimeout":500}')+encode('40'),lambda s:self.fail())
        for packet in ('5','45[]','42not-json','42["STATUS_UPDATED",{}]'):
            if packet == '42["STATUS_UPDATED",{}]':
                result = []; client.receive(encode(packet),result.append); self.assertEqual(result,[{}])
            else:
                with self.assertRaises(ProtocolError): client.receive(encode(packet),lambda s:self.fail())

    def test_ping_interval_pong_timeout_and_recovery_with_fake_clock(self):
        now = [0]; sent = []
        transport = SimpleNamespace(request=lambda method,path,body,**k:(sent.append(body) or b'ok'),close=lambda:None)
        client = Client(transport,clock=lambda:now[0])
        client.open('0{"sid":"synthetic","pingInterval":1000,"pingTimeout":500}')
        now[0] = .999; client.heartbeat(); self.assertFalse(sent)
        now[0] = 1; client.heartbeat(); self.assertEqual(sent,[encode('2')])
        now[0] = 1.49; client.heartbeat()
        client.receive(encode('3'),lambda s:None)
        now[0] = 2.49; client.heartbeat(); self.assertEqual(len(sent),2)
        now[0] = 2.99
        with self.assertRaises(TimeoutError): client.heartbeat()

    def test_real_http_push_ping_and_no_commands_or_upgrade(self):
        server = FakeEngineServer(); self.addCleanup(server.close)
        received = threading.Event(); client = Client(server.transport())
        errors = []
        def run():
            try: client.run(lambda status:received.set())
            except (OSError,ValueError):
                if not client.done.is_set(): errors.append('unexpected')
        worker = threading.Thread(target=run,daemon=True); worker.start()
        self.addCleanup(client.close)
        self.assertTrue(received.wait(2))
        limit = time.monotonic()+2
        while not any(r[0]=='POST' for r in server.requests) and time.monotonic()<limit: time.sleep(.01)
        client.close(); worker.join(2); self.assertFalse(worker.is_alive()); self.assertFalse(errors)
        posts = [r[3] for r in server.requests if r[0]=='POST']
        self.assertTrue(posts); self.assertEqual(set(posts),{encode('2')})
        for method,path,args,body in server.requests:
            self.assertEqual(path,'/socket.io/'); self.assertEqual(args['EIO'],['3']); self.assertEqual(args['transport'],['polling'])

    def test_real_http_missing_pong_cancels_long_poll(self):
        server = FakeEngineServer(pong=False); self.addCleanup(server.close)
        client = Client(server.transport()); started = time.monotonic()
        with self.assertRaises(TimeoutError): client.run(lambda s:None)
        self.assertLess(time.monotonic()-started,2)
        self.assertFalse(client.heartbeat_thread.is_alive())

    def test_real_http_peer_close_and_size_guard(self):
        server = FakeEngineServer(close=True); self.addCleanup(server.close)
        with self.assertRaises(PeerClosed): Client(server.transport()).run(lambda s:None)
        # Oversized declared bodies are rejected before any body read/allocation.
        response = SimpleNamespace(status=200,getheader=lambda k:str(MAX_PAYLOAD+1),read=lambda n:self.fail('read oversized body'))
        connection = MagicMock(sock=None,request=lambda *a:None,getresponse=lambda:response,close=lambda:None)
        stream = SimpleNamespace(settimeout=lambda t:None,connect=lambda a:None)
        with patch('wifi_bridge.telemetry.engineio.http.client.HTTPConnection',return_value=connection), patch('wifi_bridge.telemetry.engineio.socket.socket',return_value=stream):
            with self.assertRaises(ProtocolError): Client(PollingHTTP('127.0.0.1')).request()

    def test_budget_rolling_window_and_bounded_memory(self):
        now = [0]; budget = Budget(lambda:now[0])
        for _ in range(8): budget.add(MAX_PAYLOAD)
        with self.assertRaises(ProtocolError): budget.add(1)
        now[0] = 60; self.assertRaises(ProtocolError,budget.add,1)
        now[0] = 61; budget.add(1)
        for i in range(10000): now[0] += 1; budget.add(1)
        self.assertLessEqual(len(budget.buckets),61)
        tracemalloc.start()
        try:
            source = encode('42'+json.dumps(['STATUS_UPDATED',dict(STATUS,unknown='x'*220000)]))
            for _ in range(30):
                for packet in frames(source): normalize(json.loads(packet[2:])[1])
            peak = tracemalloc.get_traced_memory()[1]
        finally: tracemalloc.stop()
        self.assertLess(peak,4*1024*1024)

    def test_tiny_payload_request_flood_is_bounded_across_reconnections(self):
        now = [0]; budget = Budget(lambda:now[0]); sent = []
        transport = SimpleNamespace(request=lambda *a,**k:(sent.append(1) or encode('6')),close=lambda:None)
        for _ in range(MINUTE_REQUESTS):
            # A newly constructed client still shares the profile's budgets.
            Client(transport,budget=budget,clock=lambda:now[0]).request()
        with self.assertRaises(ProtocolError): Client(transport,budget=budget,clock=lambda:now[0]).request()
        self.assertEqual(len(sent),MINUTE_REQUESTS)
        self.assertLess(sum(row[1] for row in budget.buckets),1024)
        now[0] = 60; self.assertRaises(ProtocolError,budget.request)
        now[0] = 61; budget.request()
        self.assertLessEqual(len(budget.requests),61)


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = TelemetryStore(Path(self.tmp.name)/'telemetry.sqlite3',clock=self.clock,monotonic=lambda:self.clock.now)
        self.events = EventStore(Path(self.tmp.name)/'events.json',clock=self.clock,monotonic=lambda:self.clock.now)
        self.sessions = Sessions(self.store,events=self.events); self.addCleanup(self.sessions.close)
        self.p = {'id':'one','kind':'vespera','enabled':True,'mac':'02:00:00:00:00:01','target_ip':'192.168.4.1'}
        self.c = SimpleNamespace(lock=threading.RLock(),stop=threading.Event(),demo=False,telemetry_store=self.store,
            telemetry_sessions=self.sessions,event_store=self.events,net=SimpleNamespace(),store=SimpleNamespace(config={'telescopes':[self.p]}),
            state={'telescopes':[{'id':'one','connected':True,'interface':'wlan1'}],'devices':[]})
        self.collector = VesperaCollector(self.c); self.addCleanup(self.collector.close)
        self.observer = Observer(self.collector,self.p,'wlan1')

    def samples(self): return self.store.samples(self.sessions.active['one']['id'],limit=500)

    def test_controller_lock_delay_cannot_refresh_an_old_source_packet(self):
        from contextlib import contextmanager
        @contextmanager
        def delayed():
            self.clock.now += 10
            yield
        self.collector.workers['one'] = self.observer
        with patch.object(self.c, 'lock', delayed()):
            self.observer.receive(STATUS)
        self.assertEqual(self.observer.received_at, 0)
        self.assertEqual(self.observer.sampled, 10)
        self.assertEqual(self.collector.operation_state(self.p), 'unknown')

    def test_operation_freshness_tracks_source_pushes_not_storage_samples(self):
        self.collector.workers['one'] = self.observer
        self.assertEqual(self.collector.operation_state(self.p), 'unknown')
        self.observer.receive(STATUS)
        self.assertEqual(self.collector.operation_state(self.p), 'running')
        self.clock.now = 4.5
        self.assertEqual(self.collector.operation_state(self.p), 'running')
        self.clock.now = 4.501
        self.assertEqual(self.collector.operation_state(self.p), 'unknown')
        self.observer.receive(STATUS)
        self.assertEqual(self.observer.sampled, 0)
        self.assertEqual(self.collector.operation_state(self.p), 'running')
        self.clock.now = 9.002
        self.assertEqual(self.collector.operation_state(self.p), 'unknown')
        self.observer.receive({'currentOperation':{'state':'idle'}})
        self.clock.now += 60
        self.assertEqual(self.collector.operation_state(self.p), 'idle')
        self.clock.now += .001
        self.assertEqual(self.collector.operation_state(self.p), 'unknown')
        self.observer.receive({'battery':{'level':50}})
        self.assertEqual(self.collector.operation_state(self.p), 'unknown')
        self.observer.receive(STATUS)
        self.observer.failed(PeerClosed(), established=True)
        self.assertEqual(self.collector.operation_state(self.p), 'unknown')
        self.observer.adapter_state = 'yielded'; self.observer.yield_until = self.clock.now+600
        self.assertFalse(self.collector.available('one'))
        self.assertEqual(self.collector.operation_state(self.p), 'unknown')
        self.observer.adapter_state = 'connecting'
        self.assertEqual(self.collector.operation_state(self.p), 'unknown')
        self.observer.receive({'currentOperation':{'state':'idle'}})
        self.assertEqual(self.collector.operation_state(self.p), 'idle')
        self.store.settings['profiles']['one'] = {'adapters':[]}
        self.assertIsNone(self.collector.operation_state(self.p))

    def test_allowlist_only_normalized_values_reach_store_and_no_raw_retention(self):
        self.observer.receive(copy.deepcopy(STATUS))
        samples = self.samples(); values = {s['metric']:s['num'] if s['text'] is None else s['text'] for s in samples}
        expected = {'operation.kind':'observation','operation.state':'running','operation.step':'stacking',
            'stack.frames_acquired':14,'stack.frames_accepted':11,'stack.frames_rejected':3,
            'stack.reject_reason.astrometry':2,'stack.reject_reason.blur':1,'error.code':'pointing_failed',
            'battery.percent':73,'battery.charging':0,'storage.free_mb':12000,'filter.name':'dual_band',
            'heater.state':'on','env.temperature_c':8.5,'env.humidity_pct':65,'env.dew_point_c':2.4,
            'target.name':'Synthetic nebula','mount.tracking':1,'mount.slewing':0,'mount.at_park':0,'device.master_present':1}
        self.assertEqual(values,expected)
        self.store.flush()
        stored = str(list(self.store.db.iterdump()))+repr(self.store.queue)+repr(self.observer.previous)
        self.assertNotIn('discarded',stored)
        for forbidden in ('challenge','telescopeId','bootCount','connectedDevices','masterDeviceId','ssid','unreviewed'):
            self.assertNotIn(forbidden,stored)
        self.assertEqual(set(self.observer.profile),{'id','kind','mac','target_ip'})
        self.assertTrue(all(isinstance(v,(int,float,str,bool)) for v in self.observer.previous.values()))

    def test_invalid_leaf_types_ranges_unrecognized_enums_and_sensitive_nested_fields(self):
        status = {'battery':{'level':True,'charging':'yes'},'storage':{'freeMb':float('inf')},
            'currentOperation':{'kind':{},'state':'discarded','step':'discarded','steps':[{}]*33,'target':{'name':'x\n'},
                'capture':{'acquired':-1},'stacking':{'accepted':1.2,'rejected':False}},
            'masterDeviceId':{'id':'discarded'},'filter':'discarded','settings':{'defog':1},
            'motors':{'tracking':'discarded'},'sensors':{'humidity':101,'temperature':float('nan')}}
        self.assertEqual(normalize(status),{})
        self.assertEqual(normalize({'masterDeviceId':None}),{'device.master_present':False})
        self.assertEqual(normalize({'masterDeviceId':'x'*129}),{})

    def test_idle_push_does_not_open_or_extend_session_and_failed_transition_once(self):
        idle = copy.deepcopy(STATUS); idle['currentOperation'] = {'kind':'idle','state':'idle'}
        self.observer.receive(idle); self.assertFalse(self.sessions.active)
        self.observer.receive(STATUS); last = self.sessions.active['one']['last_activity']
        count = len(self.samples())
        self.clock.now = 1; self.observer.receive(STATUS)
        self.assertEqual(len(self.samples()),count)
        self.clock.now = 31; self.observer.receive(STATUS)
        self.assertEqual(len([s for s in self.samples() if s['metric']=='error.code']),1)
        self.observer.receive(idle); last = self.sessions.active['one']['last_activity']
        self.clock.now += 1700; self.observer.receive(idle)
        self.assertEqual(self.sessions.active['one']['last_activity'],last)
        self.clock.now += 101; self.observer.receive(idle); self.assertFalse(self.sessions.active)

    def test_counter_increase_confirms_activity_but_initial_counter_and_reset_do_not(self):
        status = {'currentOperation':{'capture':{'acquired':12}}}
        self.observer.receive(status); self.assertFalse(self.sessions.active)
        status['currentOperation']['capture']['acquired'] = 13
        self.observer.receive(status); self.assertIn('one',self.sessions.active)
        self.sessions.end('one'); status['currentOperation']['capture']['acquired'] = 1
        self.observer.receive(status); self.assertFalse(self.sessions.active)

    def test_owner_optout_disconnect_move_disabled_and_late_callbacks_cannot_write(self):
        for mode in ('optout','disconnect','move','disabled','halt'):
            with self.subTest(mode=mode):
                self.store.settings['profiles'] = {}; self.c.state['telescopes'][0]['connected'] = True
                self.p.update(enabled=True,target_ip='192.168.4.1'); self.observer.halt.clear()
                if mode=='optout': self.store.settings['profiles']['one'] = {'adapters':[]}
                elif mode=='disconnect': self.c.state['telescopes'][0]['connected'] = False
                elif mode=='move': self.p['target_ip'] = '192.168.4.2'
                elif mode=='disabled': self.p['enabled'] = False
                else: self.observer.halt.set()
                self.observer.receive(STATUS); self.assertFalse(self.sessions.active)

    def test_reconnect_backoff_repeated_peer_close_yield_and_exact_expiry(self):
        for delay in (2,5,10,30,30):
            self.observer.failed(OSError(),False)
            self.assertEqual(self.observer.retry_at,self.clock.now+delay)
            self.clock.now += delay
        self.assertFalse(self.events.events)
        self.clock.now = 100
        for index in range(4):
            self.observer.failed(PeerClosed(),True)
            if index<3: self.assertEqual(self.observer.adapter_state,'waiting')
            self.clock.now += 1
        self.assertEqual(self.observer.adapter_state,'yielded')
        self.assertEqual(self.observer.yield_until,703)
        self.assertFalse(self.sessions.active)
        self.assertEqual(len(self.events.events),1)
        event = self.events.events[0]
        self.assertTrue(valid_event(event)); self.assertEqual(event['type'],'telemetry.adapter_yielded')
        self.assertEqual(event['retry_after'],600); self.assertEqual(event['adapter_id'],ADAPTER)
        self.collector.workers['one'] = self.observer
        self.assertFalse(self.collector.available('one'))
        self.clock.now = 702.999; self.assertFalse(self.collector.available('one'))
        self.clock.now = 703; self.assertTrue(self.collector.available('one'))
        self.assertEqual(len(self.events.events),1)

    def test_only_established_peer_closes_within_two_minutes_count(self):
        for i in range(10): self.observer.failed(PeerClosed(),False)
        self.assertFalse(self.observer.closures)
        for i in range(4):
            self.clock.now = i*120
            self.observer.failed(PeerClosed(),True)
        self.assertEqual(self.observer.adapter_state,'waiting'); self.assertFalse(self.events.events)
        for i in range(4): self.observer.failed(TimeoutError(),True)
        self.assertEqual(len(self.observer.closures),1)

    def test_manager_demo_optout_and_disconnect_do_not_create_sockets(self):
        with patch.object(Observer,'start') as start:
            self.c.demo = True; self.collector.tick(); start.assert_not_called()
            self.c.demo = False; self.store.settings['profiles']['one'] = {'adapters':[]}
            self.collector.tick(); start.assert_not_called()
            self.store.settings['profiles'] = {}; self.collector.tick(); start.assert_called_once()
            self.assertEqual(len(self.collector.workers),1)
            self.c.state['telescopes'][0]['connected'] = False
            self.collector.tick(); self.assertFalse(self.collector.workers)

    def test_real_polling_reconnections_feed_shared_store_and_yield_event_once(self):
        server = FakeEngineServer(close=True); self.addCleanup(server.close)
        self.collector.client_factory = lambda transport,**kwargs:Client(server.transport(),**kwargs)
        for expected in (2,5,10):
            self.observer.attempt()
            self.assertEqual(self.observer.retry_at,self.clock.now+expected)
            self.clock.now += expected
        self.observer.attempt()
        self.assertEqual(self.observer.adapter_state,'yielded')
        self.assertIsNone(self.observer.client)
        self.assertEqual(len(self.samples()),22)
        yielded = [e for e in self.events.events if e['type']=='telemetry.adapter_yielded']
        self.assertEqual(len(yielded),1)
        self.observer.failed(PeerClosed(),True)
        self.assertEqual(self.observer.adapter_state,'yielded')
        self.assertEqual(len([e for e in self.events.events if e['type']=='telemetry.adapter_yielded']),1)

    def test_coordinator_and_polling_worker_shutdown_join_before_session_close(self):
        server = FakeEngineServer(); self.addCleanup(server.close)
        self.collector.client_factory = lambda transport,**kwargs:Client(server.transport(),**kwargs)
        self.collector.start(); coordinator = self.collector.thread
        self.collector.start(); self.assertIs(self.collector.thread,coordinator)
        deadline = time.monotonic()+2
        while not self.sessions.active and time.monotonic()<deadline: time.sleep(.01)
        self.assertIn('one',self.sessions.active)
        with self.c.lock: worker = self.collector.workers['one']
        self.collector.close()
        self.assertFalse(coordinator.is_alive()); self.assertFalse(worker.thread.is_alive())
        self.assertFalse(self.collector.workers)
        self.assertFalse(self.sessions.closed)
        count = len(self.samples()); worker.receive(STATUS)
        self.assertEqual(len(self.samples()),count)


from test_bridge import Fixture
import test_api as api_fixture


class VesperaHTTPTests(Fixture):
    def test_real_producer_metrics_and_yield_event_over_native_http(self):
        from wifi_bridge.web import WebServer
        from helpers.contract_schema import validate
        self.configured(); self.controller.cycle(0)
        self.server = WebServer(('127.0.0.1',0),self.controller)
        thread = threading.Thread(target=self.server.serve_forever,daemon=True); thread.start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.token = ''
        api_fixture.APITests.login(self)
        p = self.store.config['telescopes'][0]
        observer = Observer(self.controller.vespera_collector,p,self.controller.state['telescopes'][0]['interface'])
        self.controller.vespera_collector.workers['primary'] = observer
        observer.receive(STATUS)
        ident = self.controller.telemetry_sessions.active['primary']['id']
        status,page,_ = api_fixture.APITests.request(self,'GET','/api/v1/telemetry/samples?session_id='+ident+'&after=0')
        self.assertEqual(status,200)
        values = {sample['metric']:sample['value'] for sample in page['samples']}
        self.assertIs(values['device.master_present'],True)
        self.assertIs(values['mount.slewing'],False)
        for _ in range(4): observer.failed(PeerClosed(),True)
        status,page,_ = api_fixture.APITests.request(self,'GET','/api/v1/events?after=0&limit=100')
        self.assertEqual(status,200)
        event = next(e for e in page['events'] if e['type']=='telemetry.adapter_yielded')
        spec = json.loads((ROOT/'wifi_bridge/static/openapi.json').read_text())
        validate(event,spec['components']['schemas']['NativeEvent'],spec)
        validate(page,spec['components']['schemas']['NativeEvents'],spec)
        self.assertNotIn('discarded',json.dumps(page))
        # Available implementation follows owner selection and the yield deadline.
        self.controller.demo = False
        self.assertNotIn(ADAPTER,self.controller.instrument_catalog(self.controller.status())[0]['capabilities']['telemetry'])
        observer.yield_until = 0
        self.assertIn(ADAPTER,self.controller.instrument_catalog(self.controller.status())[0]['capabilities']['telemetry'])
        self.controller.telemetry_store.settings['profiles']['primary'] = {'adapters':[]}
        self.assertEqual(self.controller.instrument_catalog(self.controller.status())[0]['capabilities']['telemetry'],[])
        self.controller.demo = True

    request = api_fixture.APITests.request


if __name__ == '__main__': unittest.main()
