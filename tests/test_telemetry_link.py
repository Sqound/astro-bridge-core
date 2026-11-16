"""Cached/synthetic link evidence only; never contact a physical target."""
import copy
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wifi_bridge.link_observations import EchoWindow, remember_link
from wifi_bridge.network import LinuxNetwork
from wifi_bridge.telemetry import Sessions, TelemetryStore
from wifi_bridge.telemetry.link import LinkCollector


class Clock:
    def __init__(self): self.now = 0
    def read(self):
        return {'system_utc':'2026-11-04T20:00:00+00:00', 'corrected_utc':None,
                'clock_status':'unverified', 'source':'lower_bound', 'offset_ms':0}


class CachedNetwork:
    def __init__(self):
        self.calls = 0
        self.data = {'one': {'radio': {'rssi_dbm':-52, 'bitrate_mbps':72.2}, 'generation':'first',
            'activity': {'bytes_to_instrument':0, 'bytes_from_instrument':0, 'consultation':'none'}}}
    def link_observations(self, rows):
        self.calls += 1
        return copy.deepcopy(self.data)


class Probe:
    def __init__(self): self.targets = []; self.closed = False
    def tick(self, targets): self.targets.append(copy.deepcopy(targets))
    def snapshot(self, ident): return {'median_ms':4.5, 'loss_percent':20}
    def close(self): self.closed = True
    def wait(self, stop, seconds): stop.wait(seconds)


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = TelemetryStore(Path(self.tmp.name)/'telemetry.sqlite3', clock=self.clock,
                                    monotonic=lambda:self.clock.now)
        self.sessions = Sessions(self.store); self.addCleanup(self.sessions.close)
        self.profile = {'id':'one','kind':'generic','enabled':True,'mac':'02:00:00:00:00:01', 'target_ip':'192.168.4.1'}
        self.network = CachedNetwork(); self.probe = Probe()
        self.protected = False
        self.c = SimpleNamespace(lock=threading.RLock(), stop=threading.Event(), demo=False,
            telemetry_store=self.store, telemetry_sessions=self.sessions, net=self.network,
            store=SimpleNamespace(config={'telescopes':[self.profile]}),
            state={'telescopes':[{'id':'one','connected':True,'interface':'wlan1'}]},
            protection=SimpleNamespace(protected=lambda ident:self.protected))
        self.collector = LinkCollector(self.c, probe=self.probe)
        self.addCleanup(self.collector.close)

    def advance(self, seconds=1):
        self.clock.now += seconds
        self.collector.tick()

    def traffic(self, sent=100, received=200):
        activity = self.network.data['one']['activity']
        activity['bytes_to_instrument'] += sent; activity['bytes_from_instrument'] += received

    def samples(self):
        return self.store.samples(self.sessions.active['one']['id'])

    def values(self): return {s['metric']:s['num'] if s['text'] is None else s['text'] for s in self.samples()}

    def start_session(self):
        self.collector.tick(); self.traffic(); self.advance()
        self.assertIn('one', self.sessions.active)

    def test_association_and_pings_do_not_start_session_but_traffic_does(self):
        self.collector.tick(); self.advance(30)
        self.assertFalse(self.sessions.active)
        self.traffic(); self.advance()
        values = self.values()
        self.assertEqual(values['link.rssi_dbm'], -52)
        self.assertEqual(values['link.ping_ms'], 4.5)
        self.assertEqual(values['link.loss_pct'], 20)
        self.assertEqual(values['link.bytes_to'], 100)
        self.assertEqual(values['link.bytes_from'], 200)
        self.assertEqual(self.probe.targets[-1], {'one':('192.168.4.1','wlan1')})

    def test_fresh_initial_traffic_starts_session_without_importing_historical_totals(self):
        self.network.data['one']['activity'].update(bytes_to_instrument=50000, interval_bytes=100)
        self.collector.tick()
        self.assertIn('one',self.sessions.active)
        self.assertEqual(self.values()['link.bytes_to'],0)
        self.sessions.end('one'); self.collector.previous.clear()
        self.network.data['one']['activity'].update(interval_bytes=None,last_exchange='normalized-presence',last_activity_age_seconds=5)
        self.collector.tick(); self.assertFalse(self.sessions.active)
        self.collector.previous.clear()
        self.network.data['one']['activity']['last_activity_age_seconds'] = 0
        self.collector.tick(); self.assertIn('one',self.sessions.active)

    def test_start_is_one_shared_thread_and_close_joins_it(self):
        self.collector.start(); thread = self.collector.thread
        self.collector.start(); self.assertIs(self.collector.thread,thread)
        self.assertEqual(thread.name,'bridge-link')
        self.collector.close(); self.assertFalse(thread.is_alive())

    def test_exact_sampling_cadence_and_accumulated_directional_deltas(self):
        self.start_session(); count = len(self.samples())
        for _ in range(29): self.traffic(2,3); self.advance()
        self.assertEqual(len(self.samples()), count)
        self.traffic(2,3); self.advance()
        self.assertEqual(len(self.samples()), 2*count)
        self.assertEqual(self.values()['link.bytes_to'], 60)
        self.assertEqual(self.values()['link.bytes_from'], 90)
        self.assertEqual(self.sessions.active['one']['last_activity'],31)

    def test_state_change_immediately_samples_and_idle_observation_never_extends(self):
        self.start_session(); count = len(self.samples())
        self.protected = True; self.advance()
        self.assertGreater(len(self.samples()), count)
        self.assertEqual(self.values()['link.protected'], 1)
        self.assertEqual(self.sessions.active['one']['last_activity'],1)
        self.advance(1800)
        self.assertFalse(self.sessions.active)

    def test_disconnected_and_disabled_profiles_never_sample_or_probe(self):
        self.start_session(); ident = self.sessions.active['one']['id']; count = len(self.samples())
        self.c.state['telescopes'][0]['connected'] = False
        self.traffic(); self.advance(30)
        self.assertEqual(len(self.store.samples(ident)),count)
        self.assertEqual(self.probe.targets[-1],{})
        self.c.state['telescopes'][0]['connected'] = True; self.profile['enabled'] = False
        self.advance(30)
        self.assertEqual(len(self.store.samples(ident)),count)
        self.assertEqual(self.probe.targets[-1],{})

    def test_opt_out_and_demo_never_probe(self):
        self.store.settings['profiles'] = {'one':{'adapters':[]}}
        self.collector.tick(); self.traffic(); self.advance()
        self.assertFalse(self.sessions.active); self.assertEqual(self.probe.targets[-1],{})
        self.store.settings['profiles'] = {}; self.c.demo = True
        demo = LinkCollector(self.c, probe=self.probe); self.addCleanup(demo.close)
        demo.tick(); self.traffic(); demo.tick()
        self.assertEqual(self.probe.targets[-1],{})

    def test_isolated_profiles_reuse_relay_ping_window(self):
        self.profile['isolated'] = True
        self.network.data['one']['activity']['ping'] = {'median_ms':7, 'loss_percent':50}
        self.start_session()
        self.assertEqual(self.values()['link.ping_ms'],7)
        self.assertEqual(self.values()['link.loss_pct'],50)
        self.assertEqual(self.probe.targets[-1],{})

    def test_counter_reset_and_generation_move_never_fabricate_traffic(self):
        self.start_session(); self.sessions.end('one')
        self.network.data['one']['generation'] = 'new'
        self.traffic(); self.advance()
        self.assertFalse(self.sessions.active)
        self.network.data['one']['activity'].update(bytes_to_instrument=1,bytes_from_instrument=1)
        self.advance(); self.assertFalse(self.sessions.active)
        self.traffic(); self.advance(); self.assertIn('one',self.sessions.active)

    def test_disconnect_count_is_recorded_once_before_closure_and_opt_out_honored(self):
        self.start_session(); ident = self.sessions.active['one']['id']
        self.collector.connection('one',True)
        self.collector.connection('one',False); self.collector.connection('one',False)
        samples = [s for s in self.store.samples(ident) if s['metric']=='link.disconnects']
        self.assertEqual([s['num'] for s in samples],[0,1])
        self.assertFalse(self.sessions.active)
        self.collector.connection('one',True)
        self.collector.tick(); self.traffic(); self.advance()
        next_id = self.sessions.active['one']['id']
        self.store.settings['profiles']['one'] = {'adapters':[]}
        self.collector.connection('one',False)
        self.assertEqual(self.collector.disconnects['one'],1)
        self.assertEqual([s['num'] for s in self.store.samples(next_id) if s['metric']=='link.disconnects'],[1])

    def test_collector_cached_workload_24_profiles_uses_no_subprocess(self):
        self.c.store.config['telescopes'] = [dict(self.profile,id='p'+str(i)) for i in range(24)]
        self.c.state['telescopes'] = [dict(self.c.state['telescopes'][0],id=p['id']) for p in self.c.store.config['telescopes']]
        self.network.data = {p['id']:copy.deepcopy(self.network.data['one']) for p in self.c.store.config['telescopes']}
        with patch('subprocess.run',side_effect=AssertionError('subprocess')), patch('subprocess.Popen',side_effect=AssertionError('subprocess')):
            for _ in range(31): self.advance()
        self.assertEqual(self.network.calls,31)
        self.assertEqual(len(self.collector.previous),24)
        self.assertFalse(self.sessions.active)

    def test_shared_session_hooks_coalesce_real_samples_and_keep_lifecycle_order(self):
        from wifi_bridge.events import EventStore
        events = EventStore(Path(self.tmp.name)/'events.json',clock=self.clock,monotonic=lambda:self.clock.now)
        self.sessions.events = events
        for _ in range(100): self.sessions.activity('one','generic',['link_v1'],'link.bytes_to',1)
        self.assertEqual([e['type'] for e in events.events],['telemetry.session_started'])
        self.clock.now = 1; self.sessions.tick()
        self.assertEqual(events.events[-1]['type'],'telemetry.available')
        self.assertEqual(events.events[-1]['latest_sample_seq'],self.store.sequence)
        for _ in range(100): self.sessions.observe('one','link.bytes_to',2)
        self.clock.now = 30; self.sessions.tick()
        self.assertEqual(len(events.events),2)
        self.clock.now = 31; self.sessions.tick()
        self.assertEqual(len(events.events),3)
        self.assertEqual(events.events[-1]['latest_sample_seq'],self.store.sequence)
        self.sessions.observe('one','link.bytes_to',3)
        self.sessions.end('one')
        self.assertEqual([e['type'] for e in events.events][-2:],['telemetry.available','telemetry.session_ended'])
        self.assertEqual({e['instrument_id'] for e in events.events},{'one'})

    def test_close_is_idempotent_and_late_tick_cannot_write(self):
        self.start_session(); ident = self.sessions.active['one']['id']; count = len(self.samples())
        self.collector.close(); self.collector.close(); self.traffic(); self.advance(30)
        self.assertTrue(self.probe.closed)
        self.assertEqual(len(self.store.samples(ident)),count)


class FakeSocket:
    def __init__(self): self.closed = False; self.sent = []; self.reply = None
    def setblocking(self, value): pass
    def setsockopt(self, *args): pass
    def sendto(self, packet, target): self.sent.append((packet,target))
    def recvfrom(self, limit):
        if self.reply is None: raise BlockingIOError
        reply,self.reply = self.reply,None
        return reply
    def close(self): self.closed = True


class EchoTests(unittest.TestCase):
    def setUp(self):
        self.now = 0; self.sockets = []
        def factory(*args):
            sock = FakeSocket(); self.sockets.append(sock); return sock
        self.window = EchoWindow(clock=lambda:self.now, factory=factory)
        self.addCleanup(self.window.close)
        self.targets = {'one':('192.168.4.1','wlan1')}

    def test_echo_cadence_timeout_window_median_loss_and_target_cleanup(self):
        self.window.tick(self.targets); sock = self.sockets[-1]
        packet = sock.sent[0][0]; self.assertEqual(packet[0],8)
        sock.reply = (b'\0'+packet[1:],('192.168.4.1',0))
        self.now = .008; self.window.tick(self.targets)
        self.assertEqual(self.window.snapshot('one'),{'median_ms':8,'loss_percent':0})
        self.now = 9.999; self.window.tick(self.targets); self.assertEqual(len(self.sockets),1)
        self.now = 10; self.window.tick(self.targets); self.assertEqual(len(self.sockets),2)
        self.now = 11; self.window.tick(self.targets)
        self.assertEqual(self.window.snapshot('one'),{'median_ms':8,'loss_percent':50})
        self.now = 12; self.window.tick(self.targets)
        self.assertEqual(self.window.snapshot('one')['loss_percent'],50)
        self.window.tick({}); self.assertFalse(self.window.rows)

    def test_icmp_unavailable_has_no_false_loss_and_invalid_reply_is_discarded(self):
        with patch.object(self.window,'factory',side_effect=PermissionError):
            self.window.tick(self.targets)
        self.assertEqual(self.window.snapshot('one'),{'median_ms':None,'loss_percent':None})
        self.now = 10; self.window.tick(self.targets); sock = self.sockets[-1]
        packet = sock.sent[0][0]; sock.reply = (b'\0'+packet[1:],('192.168.5.1',0))
        self.now = 10.1; self.window.tick(self.targets)
        self.assertIsNone(self.window.snapshot('one')['loss_percent'])
        self.now = 11; self.window.tick(self.targets)
        self.assertEqual(self.window.snapshot('one')['loss_percent'],100)
        self.now = 612
        self.assertIsNone(self.window.snapshot('one')['loss_percent'])

    def test_reply_after_deadline_cannot_become_a_latency_measurement(self):
        self.window.tick(self.targets); sock = self.sockets[-1]
        packet = sock.sent[0][0]; sock.reply = (b'\0'+packet[1:],('192.168.4.1',0))
        self.now = 1.1; self.window.tick(self.targets)
        self.assertEqual(self.window.snapshot('one'),{'median_ms':None,'loss_percent':100})
        self.assertTrue(sock.closed)

    def test_select_timestamps_reply_before_next_profile_sweep(self):
        self.window.tick(self.targets); sock = self.sockets[-1]
        packet = sock.sent[0][0]
        class Stop:
            def is_set(self): return False
            def wait(inner,seconds): self.now += seconds
        def select_ready(readers,writers,errors,timeout):
            self.now = .006
            sock.reply = (b'\0'+packet[1:],('192.168.4.1',0))
            return [sock],[],[]
        with patch('wifi_bridge.link_observations.select.select',side_effect=select_ready):
            self.window.wait(Stop(),1)
        self.assertEqual(self.now,1)
        self.assertEqual(self.window.snapshot('one')['median_ms'],6)

    def test_radio_parser_discards_raw_fields_and_malformed_values(self):
        cache = {}
        remember_link(cache,'wlan1','Connected to fictional\n SSID: fictional\n signal: -61 dBm\n tx bitrate: 72.2 MBit/s\n',clock=lambda:5)
        self.assertEqual(cache,{'wlan1':{'observed':5,'rssi_dbm':-61,'bitrate_mbps':72.2}})
        remember_link(cache,'wlan1',' signal: -999 dBm\n tx bitrate: 1.2.3 MBit/s\n',clock=lambda:6)
        self.assertEqual(cache,{'wlan1':{'observed':6}})

    def test_backend_cache_expires_and_does_not_run_network_tools(self):
        net = LinuxNetwork('/unread-synthetic')
        net.link_cache = {'wlan1':{'rssi_dbm':-60,'observed':100}}
        with patch('wifi_bridge.network.time.monotonic',return_value=130), patch('wifi_bridge.network.run',side_effect=AssertionError):
            self.assertEqual(net.link_observations({'one':{'interface':'wlan1'}})['one']['radio']['rssi_dbm'],-60)
        with patch('wifi_bridge.network.time.monotonic',return_value=131):
            self.assertEqual(net.link_observations({'one':{'interface':'wlan1'}})['one']['radio'],{})


from test_bridge import Fixture


class ControllerLinkTests(Fixture):
    def test_confirmed_loss_and_same_cycle_reconnect_close_old_session(self):
        self.addCleanup(self.controller.telemetry_sessions.close)
        self.configured(); self.controller.cycle(0)
        sessions = self.controller.telemetry_sessions
        profile = self.store.config['telescopes'][0]
        ident = sessions.touch('primary',profile['kind'],['link_v1'],location=profile['mac'])
        self.net.disconnect('telescope')
        self.controller.cycle(20)
        self.assertTrue(self.controller.state['telescopes'][0]['connected'])
        self.assertNotIn('primary',sessions.active)
        self.assertEqual(self.controller.link_collector.disconnects['primary'],1)
        from wifi_bridge.telemetry.query import Query
        self.assertEqual(Query(self.controller.telemetry_store).session(ident)['ended_reason'],'disconnect')
        records = self.controller.telemetry_store.samples(ident)
        self.assertEqual([(s['metric'],s['num']) for s in records],[('link.disconnects',1)])

    def test_real_producer_events_and_public_capabilities_respect_owner_selection(self):
        from wifi_bridge.api import MobileAPI
        from wifi_bridge.announce import txt_records
        self.addCleanup(self.controller.telemetry_sessions.close)
        self.configured(); self.controller.cycle(0)
        collector = self.controller.link_collector
        cached = {'primary':{'activity':{'bytes_to_instrument':0,'bytes_from_instrument':0},'generation':'synthetic'}}
        self.net.link_observations = lambda rows:copy.deepcopy(cached)
        collector.tick()
        cached['primary']['activity']['bytes_to_instrument'] = 12
        collector.tick()
        events = [e for e in self.controller.event_store.events if e['type'].startswith('telemetry.')]
        self.assertEqual([e['type'] for e in events],['telemetry.session_started'])
        self.controller.telemetry_sessions.active['primary']['available_due'] = 0
        collector.tick()
        self.assertEqual(self.controller.event_store.events[-1]['type'],'telemetry.available')
        self.assertEqual(self.controller.event_store.events[-1]['latest_sample_seq'],self.controller.telemetry_store.sequence)
        api = MobileAPI(self.controller)
        self.assertIn('telemetry',api.info()['capabilities'])
        settings,runtime = self.controller.announcement()
        self.assertIn('telemetry',dict(s.split('=',1) for s in txt_records(settings,runtime))['caps'].split(','))
        catalog = self.controller.instrument_catalog(self.controller.status())
        self.assertEqual(catalog[0]['capabilities']['telemetry'],['link_v1'])
        self.assertNotIn('vespera_status_v1',catalog[0]['capabilities']['telemetry'])
        self.controller.telemetry_store.settings['profiles']['primary'] = {'adapters':[]}
        self.assertEqual(self.controller.instrument_catalog(self.controller.status())[0]['capabilities']['telemetry'],[])
        self.controller._disconnect_profile('telescope')
        self.assertEqual([e['type'] for e in self.controller.event_store.events if e['type'].startswith('telemetry.')][-1],
                         'telemetry.session_ended')
        self.controller.telemetry_sessions.close()
        self.assertNotIn('telemetry',api.info()['capabilities'])

    def test_normalized_backend_uses_cached_directional_totals_and_relay_counters(self):
        from wifi_bridge.instrument_relay import RelayCounters
        net = LinuxNetwork(self.tmp.name)
        activity = {'one':{'sample_age_seconds':0,'consultation':'background_control'}}
        net.telemetry = SimpleNamespace(status=lambda:{'profiles':activity}, generation=2,
            values={'one':{'generation':2,'group':'synthetic','bytes_to_instrument':10,'bytes_from_instrument':20}},
            current={'one':1},catalog={'records':{'1':{'device':'wlan1'}}})
        counters = RelayCounters(); counters.exchange(30,True); counters.exchange(40,False)
        counters.ping(6); counters.ping(None)
        net.isolated = SimpleNamespace(activity=lambda:{'two':counters.snapshot()},
            records={'02:00:00:00:00:02':{'profile_id':'two','intent':'second','link_observation':{'observed':100,'rssi_dbm':-55}}})
        with patch('wifi_bridge.network.time.monotonic',return_value=100), patch('wifi_bridge.network.run',side_effect=AssertionError):
            result = net.link_observations({'one':{'interface':'wlan1'},'two':{'interface':'wlan2'}})
        self.assertEqual(result['one']['activity']['bytes_to_instrument'],10)
        self.assertEqual(result['one']['activity']['bytes_from_instrument'],20)
        self.assertEqual(result['two']['activity']['bytes_to_instrument'],30)
        self.assertEqual(result['two']['activity']['bytes_from_instrument'],40)
        self.assertEqual(result['two']['activity']['ping']['median_ms'],6)
        self.assertEqual(result['two']['activity']['ping']['loss_percent'],50)
        self.assertEqual(result['two']['radio']['rssi_dbm'],-55)
        net.telemetry.values['one']['generation'] = -1
        self.assertNotIn('activity',net.link_observations({'one':{'interface':'wlan1'}})['one'])
