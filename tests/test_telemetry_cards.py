"""Offline card receipts, bounded history, demo gaps and preference churn."""
import copy
import json
from unittest.mock import patch
from test_bridge import Fixture
from wifi_bridge.telemetry.cards import project
from wifi_bridge.telemetry import TelemetryStore
from helpers.maker_keys import pem


class CardTests(Fixture):
    def setUp(self):
        super().setUp();self.configured();self.controller.cycle(0)
        self.c=self.controller;self.s=self.c.telemetry_store
        self.now=0;self.s.monotonic=lambda:self.now
        self.addCleanup(self.c.telemetry_sessions.close)
        self.addCleanup(self.c.maker_keys.close)

    def test_receipt_clocks_not_batching_and_independent_fields(self):
        readings=self.c.telemetry_sessions.readings
        readings.receive('primary','vespera_status_v1',{'operation.kind':'stacking','stack.frames_accepted':142},1.5)
        self.now=4.5
        self.assertFalse(readings.snapshot('primary',['vespera_status_v1'])['stack.frames_accepted']['stale'])
        self.now=4.501
        readings.receive('primary','vespera_status_v1',{'battery.percent':78},1.5)
        value=readings.snapshot('primary',['vespera_status_v1'])
        self.assertTrue(value['stack.frames_accepted']['stale']);self.assertFalse(value['battery.percent']['stale'])
        self.assertEqual(value['stack.frames_accepted']['value'],142)
        self.assertEqual(readings.snapshot('primary',[]),{})
        self.assertEqual(self.s.sequence,0)

    def test_demo_source_gap_yield_and_opt_out_without_network(self):
        demo=self.c.demo_telemetry;demo.started=0
        p=self.store.config['telescopes'][0];p['kind']='vespera'
        with patch('socket.socket',side_effect=AssertionError('Network forbidden')):
            demo.tick();self.assertTrue(self.c.telemetry_sessions.active)
            self.now=70;demo.tick()
            self.assertTrue(project(self.c)['primary']['readings']['stack.frames_accepted']['stale'])
            self.now=88;demo.tick();self.assertEqual(demo.status()['primary'][0]['adapter_state'],'yielded')
            self.now=96;demo.tick();self.assertFalse(project(self.c)['primary']['readings']['stack.frames_accepted']['stale'])
            self.s.settings['profiles']['primary']={'adapters':[]};demo.tick()
            self.assertEqual(project(self.c)['primary']['readings'],{})

    def test_history_is_bounded_cached_includes_pending_and_never_flushes(self):
        sessions=self.c.telemetry_sessions
        for _ in range(4):
            sessions.touch('primary','vespera',['vespera_status_v1'])
            sessions.observe('primary','target.name','M 27')
            sessions.observe('primary','stack.frames_accepted',142)
            sessions.observe('primary','stack.frames_rejected',9)
            sessions.end('primary')
        writes=self.s.write_count
        result=project(self.c)['primary']
        self.assertEqual(len(result['recent']),1);self.assertTrue(result['history_partial'])
        self.assertEqual(result['tonight'][0]['frames_accepted'],142)
        with patch('wifi_bridge.telemetry.cards.history',side_effect=AssertionError('Cache missed')):project(self.c)
        self.assertEqual(self.s.write_count,writes)
        self.assertLessEqual(len(result['tonight']),8)

    def test_runtime_key_profile_churn_prunes_preferences_preserves_history_and_restart(self):
        maker=self.c.maker_keys;maker.store.put('seestar',pem(),'import')
        source=copy.deepcopy(self.store.config['telescopes'][0]);source['kind']='seestar'
        stable=dict(source,id='stable');self.s.settings['profiles']['stable']={'adapters':[],'alpaca_interval_seconds':37}
        session=self.s.start_session('removed','seestar',['link_v1']);self.s.end_session(session, 'disconnect')
        for index in range(70):
            self.store.config['telescopes']=[stable,dict(source,id='rotating-'+str(index))]
            maker.enable_session_defaults()
            self.assertEqual(len(self.s.settings['profiles']),2)
            self.assertEqual(self.s.settings['profiles']['stable'],{'adapters':[],'alpaca_interval_seconds':37})
        self.s.flush();path=self.s.path;expected=copy.deepcopy(self.s.settings)
        self.c.telemetry_sessions.close()
        restored=TelemetryStore(path);self.addCleanup(restored.close)
        self.assertEqual(restored.settings,expected)
        self.assertEqual(restored.db.execute('SELECT COUNT(*) FROM sessions WHERE id=?',(session,)).fetchone()[0],1)
        self.assertEqual(json.loads(restored.db.execute("SELECT value FROM metadata WHERE key='settings'").fetchone()[0]),expected)

    def test_clipped_window_same_night_and_keyless_pruning_are_explicit(self):
        sessions=self.c.telemetry_sessions
        for target in ('M 31','M 27'):
            sessions.touch('primary','vespera',['vespera_status_v1'])
            sessions.observe('primary','target.name',target)
            for count in range(510):sessions.observe('primary','stack.frames_accepted',count)
            sessions.end('primary')
        card=project(self.c)['primary']
        self.assertTrue(card['history_partial'])
        self.assertEqual(len(card['recent']),1)
        self.assertEqual(card['recent'][0]['frames_accepted'],509)
        self.assertEqual(card['tonight'],[]) # Clipped-out target is never invented.
        self.s.settings['profiles']={'primary':{'adapters':[],'alpaca_interval_seconds':47},'deleted':{'adapters':['link_v1']}}
        self.assertEqual(self.c.maker_keys.store.status()['state'],'absent')
        self.c.maker_keys.enable_session_defaults()
        self.assertEqual(self.s.settings['profiles'],{'primary':{'adapters':[],'alpaca_interval_seconds':47}})
        self.assertEqual(len(project(self.c)['primary']['recent']),1)

    def test_recent_selects_three_distinct_nights_and_latest_session_per_night(self):
        from datetime import datetime, timedelta
        from types import SimpleNamespace
        original=self.s.clock
        base=datetime.fromisoformat(original.read()['system_utc'])
        ids=[]
        for days in (4,3,2,1,1):
            self.s.clock=SimpleNamespace(read=lambda days=days:dict(original.read(),system_utc=(base-timedelta(days=days)).isoformat(),corrected_utc=None))
            ident=self.s.start_session('primary','vespera',['vespera_status_v1']);ids.append(ident)
            self.s.sample(ident,'stack.frames_accepted',days*10)
            self.s.end_session(ident,'disconnect')
        self.s.clock=original
        card=project(self.c)['primary']
        self.assertEqual([row['id'] for row in card['recent']],[ids[4],ids[2],ids[1]])
        self.assertEqual([row['frames_accepted'] for row in card['recent']],[10,20,30])
        self.assertEqual([row['frames_rejected'] for row in card['recent']],[None,None,None])

    def test_clipped_counters_are_not_assigned_to_a_later_target(self):
        sessions=self.c.telemetry_sessions
        sessions.touch('primary','vespera',['vespera_status_v1'])
        for count in range(510):sessions.observe('primary','stack.frames_accepted',count)
        sessions.observe('primary','target.name','M 27')
        sessions.observe('primary','stack.frames_accepted',510)
        card=project(self.c)['primary']
        self.assertTrue(card['history_partial'])
        self.assertEqual(card['tonight'],[{'target':'M 27','frames_accepted':1,'frames_rejected':None}])
