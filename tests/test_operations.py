"""Explicit free-radio ensure: real reconcile/events, no telescope or host networking."""
import copy
import threading
import time
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.api import MobileAPI, ApiError
from wifi_bridge.config import Invalid
from wifi_bridge.network import NetworkError


class OperationTests(Fixture):
    def setUp(self):
        super().setUp();self.net.radios=3;self.devices=self.net.devices();self.configured()
        # Existing installation fixture: each trigger is explicitly opted in by
        # the demand tests below, independently of fresh-install defaults.
        self.store.config['auto_policy'].update(seestar_demand=False,vespera_demand=False,dwarf_demand=False,zwo_mount_demand=False,synscan_demand=False)
        self.controller.cycle(0)
        self.api=MobileAPI(self.controller);self.api.action('primary','disconnect');self.controller.cycle(5)
        self.api.update('settings',{'auto_connect':True})
        self.api.update('telescopes',{'id':'auto-one','name':'Automatic DEMO','kind':'generic','target_ip':'10.2.0.1',
            'ssid':'Telescope-DEMO','password':'synthetic-auto-pass','auto_eligible':True},create=True)
        self.ops=self.controller.operations

    def request(self, ident='auto-one', revision=None):
        return self.ops.ensure(ident, revision if revision is not None else '"'+self.api.revision()+'"',self.api.revision,'opaque-client',time.monotonic()+3600)

    def finish(self):
        self.ops.thread.join(10);self.assertFalse(self.ops.thread.is_alive());return self.ops.read(self.ops.last)

    def mount_native_connection(self,kind,cause):
        self.api.update('telescopes',{'kind':kind},ident='auto-one')
        self.api.update('settings',{'auto_policy':{kind+'_demand':True},'acknowledge_interruption':True},expected='"'+self.api.revision()+'"')
        result=self.ops.demand(self.controller.management_plan,'192.168.1.9',kind=kind)
        self.assertEqual(result,'REQUEST_ACCEPTED')
        operation=self.finish()
        self.assertEqual(operation['state'],'succeeded');self.assertEqual(operation['cause'],cause)
        self.assertEqual(operation['endpoint']['proxy']['protocol'],kind)
        events=[e for e in self.controller.event_store.events if e['operation_id']==operation['id']]
        self.assertIn('connected',[e['type'] for e in events]);self.assertTrue(all(e['cause']==cause for e in events))

    def test_native_zwo_connection_runs_shared_guarded_operation_and_reports_its_cause(self):
        self.mount_native_connection('zwo_mount','ZWO_MOUNT_CONNECTION')

    def test_native_synscan_query_runs_shared_guarded_operation_and_reports_its_cause(self):
        self.mount_native_connection('synscan','SYNSCAN_DISCOVERY')

    def test_success_actual_readiness_event_id_selector_commit_and_fastpath(self):
        code,accepted=self.request();self.assertEqual(code,202)
        result=self.finish();self.assertEqual(result['state'],'succeeded');self.assertEqual(result['reason'],'READY')
        profile=self.store.config['telescopes'][1];self.assertTrue(profile['enabled']);self.assertTrue(profile['bssid']);self.assertEqual(profile['mac'],self.devices[2]['mac'])
        self.assertEqual(result['endpoint']['profile_id'],'auto-one');self.assertIsNone(result['endpoint']['proxy']);self.assertEqual(result['endpoint']['direct_route']['routability'],'unknown')
        events=[e for e in self.controller.event_store.events if e['operation_id']==accepted['id']]
        self.assertIn('requested',[e['type'] for e in events]);self.assertIn('connected',[e['type'] for e in events]);self.assertTrue(all(e['profile_id']=='auto-one' for e in events))
        self.store.config['auto_connect']=False
        code,ready=self.ops.ensure('auto-one',None,self.api.revision,'opaque-client',time.monotonic()+3600)
        self.assertEqual(code,200);self.assertEqual(ready['state'],'ready');self.assertEqual(ready['endpoint'],result['endpoint'])
        self.assertFalse(self.ops.pending);self.assertNotIn('synthetic-auto-pass',str(result));self.assertNotIn('opaque-client',str(result))

    def test_selected_hotspot_radio_is_not_taken_before_ap_starts(self):
        mac = self.devices[2]['mac']
        self.api.update('hotspot', {'enabled': True, 'mac': mac}, expected='"' + self.api.revision() + '"')
        self.assertFalse(self.controller.state['hotspot']['active'])
        _, accepted = self.request()
        result = self.finish()
        self.assertEqual(result['state'], 'succeeded')
        profile = next(p for p in self.store.config['telescopes'] if p['id'] == 'auto-one')
        self.assertNotEqual(profile['mac'], mac)
        self.controller.cycle(20)
        self.assertEqual(self.controller.state['hotspot']['mac'], mac)

    def test_accepted_operation_waits_for_slow_reconcile_handoff(self):
        entered=threading.Event();start=threading.Event();run=self.ops.run
        def deferred(operation):
            entered.set();start.wait(2);return run(operation)
        with patch.object(self.ops,'run',side_effect=deferred):
            _,accepted=self.request();self.assertTrue(entered.wait(2))
            with self.controller.net_lock:
                start.set();time.sleep(5.2)
                self.assertEqual(self.ops.read(accepted['id'])['state'],'running')
                self.assertFalse(self.store.config['telescopes'][1]['enabled'])
            self.assertEqual(self.finish()['state'],'succeeded')

    def test_network_handoff_wait_keeps_deadline_and_rechecks_revision(self):
        for expire in (True,False):
            entered=threading.Event();start=threading.Event();run=self.ops.run
            def deferred(operation):
                entered.set();start.wait(2);return run(operation)
            self.ops.negative.clear()
            with patch.object(self.ops,'run',side_effect=deferred),patch.object(self.net,'scan',wraps=self.net.scan) as scan:
                _,accepted=self.request();self.assertTrue(entered.wait(2))
                with self.controller.net_lock:
                    if expire:self.ops.deadline=time.monotonic()+.05
                    else:self.ops.expected='superseded-while-waiting'
                    start.set()
                    if expire:self.ops.thread.join(2);self.assertFalse(self.ops.thread.is_alive())
                result=self.finish();scan.assert_not_called()
                self.assertEqual(result['reason'],'TIMED_OUT' if expire else 'SUPERSEDED')
                self.assertFalse(self.store.config['telescopes'][1]['enabled'])

    def test_notification_groups_actual_operation_outcome_and_measured_elapsed_time(self):
        from wifi_bridge.push import PushService
        from wifi_bridge.notifications import DEFAULT_CATEGORIES
        from test_push import subscription
        sent=[]
        def worker(request):
            if request['action']=='probe':return {'available':True}
            if request['action']=='keygen':return {'private_key':'synthetic-private','public_key':'synthetic-public'}
            sent.append(request['payload']);return {'status':201}
        service=PushService(self.controller,lambda:{'listener':{'ready':True},'identity':{'fingerprint_sha256':'a'*64}},invoke=worker,start=False)
        self.addCleanup(service.close)
        service.action({'action':'subscribe','subscription':subscription(),'categories':DEFAULT_CATEGORIES},'https://astro-bridge.local')
        service.tick()
        entered=threading.Event();release=threading.Event();original=self.net.scan
        def scan(name):entered.set();release.wait(5);return original(name)
        with patch.object(self.net,'scan',side_effect=scan):
            _,accepted=self.request();self.assertTrue(entered.wait(2));service.tick()
            self.assertEqual(service.source.op_seen.get(accepted['id']),'running')
            release.set();self.assertEqual(self.finish()['state'],'succeeded')
        service.tick()
        with patch('wifi_bridge.push.time.monotonic',return_value=time.monotonic()+4):service.tick()
        self.assertEqual(len(sent),2)
        self.assertEqual(sent[0]['body'],'Switching to Automatic DEMO.')
        self.assertRegex(sent[1]['body'],r'^Switched to Automatic DEMO in [0-9]+ s\.$')
        self.assertEqual(sent[1]['path'],'/#telescopes?profile=auto-one')
        self.assertEqual(sent[0]['tag'],sent[1]['tag'])
        self.assertEqual(service.source.op_seen[accepted['id']],'finished')
        self.assertNotIn('synthetic-auto-pass',str(sent));self.assertNotIn('connected.',sent[0]['body'])

    def test_consent_strict_omission_preservation_and_metadata_only_events(self):
        self.api.action('primary','connect');self.controller.cycle(10)
        sequence=self.controller.event_store.sequence
        with patch.object(self.net,'connect',wraps=self.net.connect) as connect:
            self.api.update('telescopes',{'auto_eligible':True},ident='primary');self.api.update('settings',{'auto_connect':False});self.controller.cycle(15)
            connect.assert_not_called();self.assertEqual(sequence,self.controller.event_store.sequence)
        for field,resource in [('auto_connect','settings'),('auto_eligible','telescopes')]:
            for bad in (0,1,'true',None,[],{}):
                with self.assertRaises(Invalid):self.api.update(resource,{field:bad},ident='auto-one' if resource=='telescopes' else None)
        self.api.update('telescopes',{'name':'Renamed'},ident='auto-one');self.assertTrue(self.store.config['telescopes'][1]['auto_eligible'])
        with self.assertRaises(ApiError) as error:self.request()
        self.assertEqual(error.exception.code,'AUTO_CONNECT_DISABLED')
        self.api.update('settings',{'auto_connect':True});self.api.update('telescopes',{'auto_eligible':False},ident='auto-one')
        with self.assertRaises(ApiError):self.request()
        for expected in ('*','"*"','"old"'):
            with self.assertRaises(ApiError):self.request(revision=expected)

    def test_concurrency_coalescing_transient_overlay_and_owner_maintenance_guards(self):
        entered=threading.Event();release=threading.Event();original=self.net.scan
        def scan(name):entered.set();release.wait(5);return original(name)
        with patch.object(self.net,'scan',side_effect=scan):
            code,accepted=self.request();self.assertTrue(entered.wait(2));self.assertFalse(self.controller.update_safe())
            self.assertEqual(self.request()[1]['id'],accepted['id']);self.assertFalse(self.store.config['telescopes'][1]['enabled']);self.assertEqual(self.store.config['telescopes'][1]['mac'],'')
            for action in (lambda:self.request('primary'),lambda:self.request(revision='*'),lambda:self.controller.save(copy.deepcopy(self.store.config)),
                           lambda:self.controller.telescope_action({'id':'primary','action':'delete'}),lambda:self.controller.retry(),
                           lambda:self.controller.credentials({'target':'web','current_password':'local-admin-test-password','password':'new-password'}),
                           lambda:self.controller.candidates.run({'mode':'wifi'}),lambda:self.api.binding('primary',{},'"'+self.api.revision()+'"',unlink=True)):
                with self.assertRaises((ApiError,Invalid)):action()
            release.set();self.assertEqual(self.finish()['state'],'succeeded')

    def test_association_failure_restores_only_intent_preserves_new_credentials_no_retry(self):
        original=self.net.connect
        def fail(role,device,config):
            if role=='telescope-auto-one':
                value=copy.deepcopy(self.store.config);value['telescopes'][1]['password']='newer-private-password';self.store.save(value)
                raise NetworkError('PRIVATE raw failure')
            return original(role,device,config)
        with patch.object(self.net,'connect',side_effect=fail) as connect:
            code,accepted=self.request();result=self.finish();self.controller.cycle(100)
            self.assertEqual(connect.call_count,1)
        self.assertEqual(result['state'],'failed');self.assertEqual(result['reason'],'ASSOCIATION_FAILED');self.assertEqual(result['restoration']['state'],'restored')
        profile=self.store.config['telescopes'][1];self.assertFalse(profile['enabled']);self.assertEqual(profile['mac'],'');self.assertEqual(profile['bssid'],'');self.assertEqual(profile['password'],'newer-private-password')
        self.assertNotIn('PRIVATE',str(result));self.assertNotIn('newer-private-password',str(result))
        self.assertNotIn('connected',[e['type'] for e in self.controller.event_store.events if e['operation_id']==accepted['id']])

    def test_wrong_physical_association_never_receives_forwarding_or_proxy(self):
        original=self.net.link_observation
        def wrong(device,profile):return {'bssid':'02:00:00:99:99:99'} if profile['id']=='auto-one' else original(device,profile)
        with patch.object(self.net,'link_observation',side_effect=wrong),patch.object(self.net,'firewall',wraps=self.net.firewall) as firewall:
            _,accepted=self.request();result=self.finish()
        self.assertEqual(result['reason'],'PHYSICAL_SELECTION_MISMATCH');self.assertEqual(result['state'],'failed')
        self.assertFalse(any(route.get('target')=='10.2.0.1' for call in firewall.call_args_list for route in call.kwargs.get('telescopes',[])))
        self.assertNotIn('connected',[e['type'] for e in self.controller.event_store.events if e['operation_id']==accepted['id']])

    def test_service_deadline_failure_cleanup_and_no_late_ready(self):
        self.api.update('telescopes',{'kind':'vespera'},ident='auto-one')
        clock=[100.];self.ops.clock=lambda:clock[0]
        def unavailable(*args):clock[0]+=61;return False
        with patch.object(self.net,'probe',side_effect=unavailable):
            _,accepted=self.request();result=self.finish()
        self.assertEqual(result['reason'],'TIMED_OUT');self.assertEqual(result['restoration']['state'],'restored');self.assertFalse(self.store.config['telescopes'][1]['enabled'])
        self.assertNotIn('connected',[e['type'] for e in self.controller.event_store.events if e['operation_id']==accepted['id']])
        self.assertTrue(self.controller.update_safe())

    def test_band_ambiguity_selector_conflict_external_radio_and_subnet_reasons(self):
        row=self.net.scan('wlan2')[0]
        cases=[([row,dict(row,bssid='02:00:00:11:22:34')],None,'AMBIGUOUS_NETWORK'),
               ([dict(row,frequency=6500)],None,'INCOMPATIBLE_BAND'),([],None,'NETWORK_UNOBSERVED')]
        for rows,_,reason in cases:
            self.ops.negative.clear()
            with patch.object(self.net,'scan',return_value=rows):self.request();self.assertEqual(self.finish()['reason'],reason)
        self.ops.negative.clear()
        with patch.object(self.net,'radio_idle',return_value=False):self.request();self.assertEqual(self.finish()['reason'],'REPLACEMENT_COOLDOWN')
        self.ops.negative.clear()
        self.api.update('telescopes',{'target_ip':'192.168.1.10'},ident='auto-one');self.request();self.assertEqual(self.finish()['reason'],'OVERLAPPING_TARGET_NETWORK')
        self.api.update('telescopes',{'target_ip':'10.2.0.1','bssid':'02:00:00:11:22:33'},ident='auto-one')
        self.api.binding('auto-one',{'owner_confirmed':True,'aliases':[{'ssid':'Telescope-DEMO','bssid':'02:00:00:11:22:34'}]},'"'+self.api.revision()+'"')
        # A distinct session accounts for an independent bounded request budget.
        self.ops.clients.clear()
        with self.assertRaises(ApiError) as error:self.request()
        self.assertEqual(error.exception.code,'BINDING_SELECTOR_CONFLICT')

    def test_record_expiry_epoch_shape_and_orphan_cleanup_when_home_unavailable(self):
        self.request();result=self.finish();self.assertEqual(self.ops.read(result['id'])['id'],result['id'])
        for value in ('',[],None,'x'*70):
            with self.assertRaises(ApiError):self.ops.read(value)
        with self.assertRaises(ApiError) as error:self.ops.read('0'*32+'.'+'1'*32)
        self.assertEqual(error.exception.code,'OPERATION_RUN_CHANGED')
        self.ops.clock=lambda:time.monotonic()+1000
        with self.assertRaises(ApiError) as error:self.ops.read(result['id'])
        self.assertEqual(error.exception.code,'OPERATION_EXPIRED')
        # Crash before commit: disk intent disabled, NM role can outlive the process.
        value=copy.deepcopy(self.store.config);value['telescopes'][1].update(enabled=False,mac='',bssid='');self.store.save(value)
        from wifi_bridge.controller import Controller
        other=Controller(self.store,self.net,{'password':'bootstrap-test-code'},demo=True)
        self.net.disconnect('home')
        original=self.net.connect
        def fail_home(role,*args):
            if role=='home':raise NetworkError('offline')
            return original(role,*args)
        with patch.object(self.net,'connect',side_effect=fail_home):other.cycle(0)
        self.assertNotIn('telescope-auto-one',self.net.active);self.assertEqual(other.state['mode'],'setup');self.assertIsNone(other.operations.latest())

    def test_existing_relay_and_proxy_owner_survive_new_earlier_profile_success_and_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                # Saved order B then A; A already owns the only native proxy.
                self.api.update('telescopes',{'kind':'seestar'},ident='primary')
                self.api.update('telescopes',{'kind':'seestar'},ident='auto-one')
                self.api.action('primary','connect');self.controller.cycle(20)
                value=copy.deepcopy(self.store.config);value['telescopes'].reverse();self.store.save(value)
                self.controller.cycle(25)
                a=next(row for row in self.controller.state['telescopes'] if row['id']=='primary');self.assertTrue(a['seestar_proxy'])
                original=self.net.connect
                def connect(role,device,config):
                    if failure and role=='telescope-auto-one':raise NetworkError('unavailable')
                    return original(role,device,config)
                with patch.object(self.net,'connect',side_effect=connect),patch.object(self.net,'firewall',wraps=self.net.firewall) as firewall:
                    self.request();result=self.finish()
                self.assertEqual(result['state'],'failed' if failure else 'succeeded')
                if not failure:self.assertIsNone(result['endpoint']['proxy'])
                for call in firewall.call_args_list:
                    routes=call.kwargs.get('telescopes',[])
                    a_routes=[route for route in routes if route['target']=='10.0.0.1']
                    self.assertEqual(len(a_routes),1);self.assertTrue(a_routes[0]['seestar_proxy'])
                    self.assertIn(self.devices[2]['mac'],[d['mac'] for d in call.kwargs.get('isolated',[])])
                    self.assertFalse(any(route.get('seestar_proxy') for route in routes if route['target']=='10.2.0.1'))
                self.controller.cycle(30)
                self.assertTrue(next(row for row in self.controller.state['telescopes'] if row['id']=='primary')['seestar_proxy'])
                if not failure:self.api.action('auto-one','disconnect');self.controller.cycle(35)
                value=copy.deepcopy(self.store.config);value['telescopes'].sort(key=lambda p:p['id']!='primary');self.store.save(value)


    def test_cleanup_failure_stays_isolated_until_confirmed_and_blocks_maintenance(self):
        original_link=self.net.link_observation;original_disconnect=self.net.disconnect
        def wrong(device,profile):return {'bssid':'02:00:00:99:99:99'} if profile['id']=='auto-one' else original_link(device,profile)
        def fail(role):
            if role=='telescope-auto-one':raise NetworkError('cleanup failed')
            return original_disconnect(role)
        with patch.object(self.net,'link_observation',side_effect=wrong),patch.object(self.net,'disconnect',side_effect=fail):
            self.request();result=self.finish()
        self.assertEqual(result['restoration']['state'],'failed');self.assertTrue(self.controller.cleanup_required);self.assertFalse(self.controller.update_safe())
        self.controller.cycle(50);self.assertFalse(self.controller.cleanup_required);self.assertNotIn('telescope-auto-one',self.net.active)
        profile=next(p for p in self.store.config['telescopes'] if p['id']=='auto-one')
        self.net.connect('telescope-auto-one',self.devices[0],dict(profile,mac=self.devices[0]['mac']))
        with self.assertRaises(ApiError) as error:self.request()
        self.assertEqual(error.exception.code,'ROLE_CLEANUP_PENDING')

    def test_request_limits_record_capacity_and_worker_start_failure(self):
        with patch.object(self.net,'radio_idle',return_value=False):
            for _ in range(5):self.ops.negative.clear();self.request();self.finish()
            self.ops.negative.clear()
            with self.assertRaises(ApiError) as error:self.request()
            self.assertEqual(error.exception.code,'ENSURE_LIMITED')
            first=self.ops.last
            for i in range(65):
                self.ops.negative.clear()
                self.ops.ensure('auto-one','"'+self.api.revision()+'"',self.api.revision,'client-'+str(i),time.monotonic()+3600);self.finish()
        self.assertEqual(len(self.ops.records),64);self.assertLessEqual(len(self.ops.clients),32)
        with self.assertRaises(ApiError) as error:self.ops.read(first)
        self.assertEqual(error.exception.code,'OPERATION_EXPIRED');self.ops.clients.clear();self.ops.negative.clear()
        with patch('wifi_bridge.operations.threading.Thread.start',side_effect=RuntimeError('fail')):
            with self.assertRaises(ApiError):self.request()
        self.assertFalse(self.ops.pending);self.assertFalse(self.controller.net_lock.locked());self.assertEqual(self.ops.read(self.ops.last)['reason'],'WORKER_UNAVAILABLE')


    def test_same_ssid_physical_access_point_cannot_be_allocated_twice(self):
        self.request();self.assertEqual(self.finish()['state'],'succeeded')
        self.api.update('telescopes',{'id':'auto-two','name':'Second','kind':'generic','target_ip':'10.3.0.1','ssid':'Telescope-DEMO','password':'synthetic-pass','auto_eligible':True},create=True)
        with patch.object(self.net,'connect',wraps=self.net.connect) as connect:
            self.request('auto-two');self.assertEqual(self.finish()['reason'],'PHYSICAL_NETWORK_IN_USE');connect.assert_not_called()
        self.assertTrue(next(row for row in self.controller.state['telescopes'] if row['id']=='auto-one')['relay_ready'])


    def test_completed_record_never_returns_proxy_reassigned_to_another_profile(self):
        self.api.update('telescopes',{'kind':'seestar'},ident='auto-one');self.request();result=self.finish()
        self.assertTrue(result['endpoint_current']);self.assertEqual(result['endpoint']['proxy']['protocol'],'seestar')
        self.api.action('auto-one','disconnect');self.controller.cycle(50)
        self.api.update('telescopes',{'kind':'seestar'},ident='primary');self.api.action('primary','connect');self.controller.cycle(55)
        old=self.ops.read(result['id']);self.assertEqual(old['state'],'succeeded');self.assertFalse(old['endpoint_current']);self.assertIsNone(old['endpoint'])
        code,ready=self.ops.ensure('primary',None,self.api.revision,'opaque-client',time.monotonic()+3600)
        self.assertEqual(code,200);self.assertEqual(ready['endpoint']['profile_id'],'primary');self.assertEqual(ready['endpoint']['proxy']['protocol'],'seestar')


    def test_warm_owned_association_is_adopted_then_retained_without_hiding_network_edits(self):
        from wifi_bridge.controller import Controller
        self.api.action('primary','connect');self.controller.cycle(30)
        self.controller=Controller(self.store,self.net,{'password':'bootstrap-test-code'},demo=True)
        self.api=MobileAPI(self.controller);self.ops=self.controller.operations
        with patch.object(self.net,'connect',wraps=self.net.connect) as connect:
            self.controller.cycle(35);connect.assert_not_called()
        self.assertIn('telescope',self.controller.applied)
        with patch.object(self.net,'firewall',wraps=self.net.firewall) as firewall:
            self.request();self.assertEqual(self.finish()['state'],'succeeded')
        for call in firewall.call_args_list:self.assertTrue(any(route['target']=='10.0.0.1' for route in call.kwargs.get('telescopes',[])))
        with patch.object(self.net,'connect',wraps=self.net.connect) as connect:
            self.api.update('telescopes',{'password':'updated-warm-password'},ident='primary');self.controller.cycle(40)
            self.assertEqual([call.args[0] for call in connect.call_args_list],['telescope'])

    def test_replacement_policy_consent_strict_revision_and_metadata_preservation(self):
        from wifi_bridge.config import auto_policy
        self.assertEqual(auto_policy(self.store.config),{'quiet_seconds':120,'cooldown_seconds':60,'force_first_profile':False,'seestar_demand':False,'vespera_demand':False,'dwarf_demand':False,'zwo_mount_demand':False,'synscan_demand':False})
        self.assertFalse(self.api.read('telescopes','primary')['telescope']['auto_replaceable'])
        for value in (None,[],{'unexpected':True},{'quiet_seconds':True},{'cooldown_seconds':0},{'quiet_seconds':3601},{'force_first_profile':1}):
            with self.assertRaises(Invalid):self.api.update('settings',{'auto_policy':value},expected='"'+self.api.revision()+'"')
        for expected in (None,'*','"*"'):
            with self.assertRaises(ApiError):self.api.update('settings',{'auto_policy':{'quiet_seconds':30}},expected=expected)
        with self.assertRaises(Invalid):self.api.update('settings',{'auto_policy':{'force_first_profile':True}},expected='"'+self.api.revision()+'"')
        self.api.update('settings',{'auto_policy':{'force_first_profile':True},'acknowledge_interruption':True},expected='"'+self.api.revision()+'"')
        with self.assertRaises(Invalid):self.api.update('settings',{'auto_policy':{'seestar_demand':True}},expected='"'+self.api.revision()+'"')
        self.api.update('settings',{'auto_policy':{'seestar_demand':True},'acknowledge_interruption':True},expected='"'+self.api.revision()+'"')
        self.api.update('settings',{'country':'JP'});self.assertTrue(auto_policy(self.store.config)['seestar_demand']);self.assertNotIn('acknowledge_interruption',self.store.config)
        self.api.action('primary','connect');self.controller.cycle(30);sequence=self.controller.event_store.sequence
        with patch.object(self.net,'connect',wraps=self.net.connect) as connect:
            self.api.update('telescopes',{'auto_replaceable':True},ident='primary');self.controller.cycle(35);connect.assert_not_called()
        self.assertEqual(self.controller.event_store.sequence,sequence)
        self.api.update('telescopes',{'name':'Same eligibility'},ident='primary');self.assertTrue(self.store.config['telescopes'][0]['auto_replaceable'])
        for value in (None,1,'true',[]):
            with self.assertRaises(Invalid):self.api.update('telescopes',{'auto_replaceable':value},ident='primary')

    def replacement(self, *, force=False, state='quiet'):
        self.net.radios = 2; self.controller.update(devices=self.net.devices())
        self.api.update('telescopes', {'auto_eligible':True,'auto_replaceable':True}, ident='primary')
        self.api.action('primary','connect'); self.controller.cycle(30)
        self.ops.clock = lambda: 1000.; self.ops.started = self.ops.last_replacement = 0
        self.measured = {'state':state,'reason':'SYNTHETIC_COLLECTION_FIXTURE','sample_age_seconds':0,
            'quiet_seconds':120,'cooldown_remaining':0,'last_activity_age_seconds':180}
        self.net.selection_activity = lambda *args: copy.deepcopy(self.measured)
        self.net.sample_activity = lambda **kwargs: None
        if force:self.api.update('settings',{'auto_policy':{'force_first_profile':True},'acknowledge_interruption':True},expected='"'+self.api.revision()+'"')

    def test_quiet_replacement_commits_only_affected_intent_and_correlates_disconnect(self):
        self.replacement()
        _, accepted = self.request(); value = self.finish()
        self.assertEqual(value['state'],'succeeded');self.assertEqual(value['displaced'],{'profile_id':'primary','reason':'QUIET_REPLACEMENT'})
        self.assertFalse(self.store.config['telescopes'][0]['enabled']);self.assertTrue(self.store.config['telescopes'][1]['enabled'])
        self.assertEqual(value['endpoint']['profile_id'],'auto-one');self.assertFalse(self.controller.pauses.records)
        events=[e for e in self.controller.event_store.events if e['operation_id']==accepted['id']]
        self.assertIn(('primary','disconnected'),[(e['profile_id'],e['type']) for e in events])
        self.assertIn(('auto-one','connected'),[(e['profile_id'],e['type']) for e in events])

    def test_failed_target_actually_restores_victim_current_credentials_one_operation(self):
        self.replacement(); original=self.net.connect
        def connect(role,device,config):
            if role=='telescope-auto-one':
                self.store.config['telescopes'][0]['password']='current-victim-password'
                raise NetworkError('private target failure')
            if role=='telescope':self.assertEqual(config['password'],'current-victim-password')
            return original(role,device,config)
        with patch.object(self.net,'connect',side_effect=connect):
            _,accepted=self.request();value=self.finish()
        self.assertEqual(value['state'],'failed');self.assertEqual(value['restoration'],{'state':'restored','reason':'PREVIOUS_CONNECTION_READY'})
        self.assertTrue(self.net.connected('telescope',self.devices[0]));self.assertTrue(self.store.config['telescopes'][0]['enabled'])
        self.assertFalse(self.store.config['telescopes'][1]['enabled']);self.assertFalse(self.controller.pauses.records)
        self.assertIn('restored',[e['type'] for e in self.controller.event_store.events if e['operation_id']==accepted['id'] and e['profile_id']=='primary'])
        with self.assertRaises(ApiError) as error:self.request()
        self.assertEqual(error.exception.code,'TARGET_BACKOFF')

    def test_failed_restore_durably_pauses_without_background_or_restart_retry(self):
        self.replacement(); original=self.net.connect
        def connect(role,*args):
            if role.startswith('telescope'):raise NetworkError('synthetic association failure')
            return original(role,*args)
        with patch.object(self.net,'connect',side_effect=connect):
            self.request();value=self.finish()
        self.assertEqual(value['restoration'],{'state':'failed','reason':'RESTORATION_FAILED_PAUSED'})
        self.assertTrue(self.controller.pauses.held('primary'));self.assertTrue(self.store.config['telescopes'][0]['enabled'])
        from wifi_bridge.controller import Controller
        restarted=Controller(self.store,self.net,self.controller.bootstrap,demo=True)
        with patch.object(self.net,'connect',wraps=self.net.connect) as connect:
            self.controller.cycle(200);restarted.cycle(250);restarted.cycle(300)
        self.assertFalse(any(call.args[0].startswith('telescope') for call in connect.call_args_list));self.assertFalse(restarted.update_safe())
        self.api.action('primary','connect',expected='"'+self.api.revision()+'"');self.controller.cycle(400)
        self.assertTrue(self.controller.state['relay_ready']);self.assertFalse(self.controller.pauses.held('primary'))

    def test_unknown_not_idle_force_explicit_and_protection_recheck_after_collection(self):
        self.replacement(state='unknown');self.request();value=self.finish()
        self.assertEqual(value['reason'],'NO_REPLACEABLE_CONNECTION');self.assertTrue(self.net.connected('telescope',self.devices[0]))
        self.ops.negative.clear();self.api.update('settings',{'auto_policy':{'force_first_profile':True},'acknowledge_interruption':True},expected='"'+self.api.revision()+'"')
        self.request();value=self.finish();self.assertEqual(value['state'],'succeeded');self.assertEqual(value['displaced']['reason'],'FORCED_INTERRUPTION')

    def test_lease_arriving_before_victim_selection_and_absent_scan_never_escalates(self):
        self.replacement()
        self.controller.protection.create('primary',{},'other-app','"'+self.api.revision()+'"',self.api.revision)
        self.request();value=self.finish();self.assertEqual(value['reason'],'NO_REPLACEABLE_CONNECTION')
        self.assertTrue(self.net.connected('telescope',self.devices[0]))
        self.controller.protection.leases.clear();self.ops.negative.clear()
        with patch.object(self.net,'scan',return_value=[]):self.request();value=self.finish()
        self.assertEqual(value['reason'],'NETWORK_UNOBSERVED');self.assertEqual(value['restoration']['state'],'restored')
        self.assertTrue(self.net.connected('telescope',self.devices[0]))
        self.ops.negative.clear();self.ops.cooldowns.clear();self.ops.last_replacement=0;self.net.radios=3;self.controller.update(devices=self.net.devices());self.controller.cycle(500)
        with patch.object(self.net,'scan',return_value=[]),patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect:
            self.request();value=self.finish()
        self.assertEqual(value['reason'],'NETWORK_UNOBSERVED');self.assertFalse(any(c.args[0]=='telescope' for c in disconnect.call_args_list))

    def test_overlap_removed_only_for_displaced_profile_and_no_hidden_pending_replay(self):
        self.replacement();self.api.update('telescopes',{'target_ip':'10.0.0.9'},ident='auto-one')
        self.request();self.assertEqual(self.finish()['state'],'succeeded')
        self.assertFalse(self.store.config['telescopes'][0]['enabled']);self.assertTrue(self.net.connected('telescope-auto-one',self.devices[0]))

    def test_restoring_is_not_terminal_and_known_incompatible_victim_never_disconnected(self):
        self.replacement(); original = self.net.connect; entered = threading.Event(); release = threading.Event()
        def connect(role,*args):
            if role=='telescope-auto-one': raise NetworkError('target unavailable')
            if role=='telescope': entered.set(); release.wait(3)
            return original(role,*args)
        with patch.object(self.net,'connect',side_effect=connect):
            self.request(); self.assertTrue(entered.wait(2))
            pending=self.ops.read(self.ops.last);self.assertEqual(pending['state'],'running');self.assertEqual(pending['phase'],'restoring')
            release.set();self.assertEqual(self.finish()['restoration']['state'],'restored')
        self.ops.negative.clear();self.ops.last_replacement=0;self.ops.cooldowns.clear()
        binding={'version':1,'aliases':[{'ssid':'Telescope-DEMO','bssid':'02:00:00:11:22:33','source':'wifi-observed','observed_at':0,'frequency':5180}], 'reported_identity':None,'client_observation':None}
        # Fixture represents validated server-observed metadata, not a forged config/API claim.
        self.store.config['telescopes'][1]['physical_binding']=binding
        with patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect:
            self.request();self.assertEqual(self.finish()['reason'],'NO_REPLACEABLE_CONNECTION')
        self.assertFalse(any(call.args[0]=='telescope' for call in disconnect.call_args_list))

    def test_restoration_verification_exception_persists_hold_before_pending_released(self):
        self.replacement(); original = self.net.link_observation; original_connect = self.net.connect
        def connect(role,*args):
            if role=='telescope-auto-one': raise NetworkError('target unavailable')
            return original_connect(role,*args)
        def link(device,profile):
            if self.ops.restoring: raise NetworkError('NM verification unavailable')
            return original(device,profile)
        with patch.object(self.net,'connect',side_effect=connect),patch.object(self.net,'link_observation',side_effect=link):
            self.request(); value = self.finish()
        self.assertEqual(value['state'],'failed');self.assertEqual(value['restoration']['reason'],'RESTORATION_FAILED_PAUSED')
        self.assertFalse(self.ops.pending);self.assertTrue(self.controller.pauses.held('primary'))
        from wifi_bridge.paused import PausedRoles
        self.assertTrue(PausedRoles(self.store.path.parent).held('primary'))


    def vespera_policy(self):
        self.api.update('telescopes',{'kind':'vespera'},ident='auto-one')
        self.api.update('settings',{'auto_policy':{'vespera_demand':True},'acknowledge_interruption':True},expected='"'+self.api.revision()+'"')

    def test_vespera_demand_unique_target_ready_preservation_shared_quota_and_origin(self):
        plan=self.controller.management_plan
        self.assertEqual(self.ops.demand(plan,'192.168.1.9',kind='vespera',target='10.2.0.1'),'DISABLED')
        self.vespera_policy()
        self.assertEqual(self.ops.demand(plan,'192.168.1.9',kind='vespera',target='10.2.0.1'),'REQUEST_ACCEPTED')
        result=self.finish();self.assertEqual(result['state'],'succeeded');self.assertEqual(result['cause'],'VESPERA_CONNECTION')
        events=[e for e in self.controller.event_store.events if e['operation_id']==result['id']]
        self.assertEqual({e['cause'] for e in events},{'VESPERA_CONNECTION'});self.assertIn('connected',[e['type'] for e in events])
        self.api.update('settings',{'auto_policy':{'seestar_demand':True},'acknowledge_interruption':True},expected='"'+self.api.revision()+'"')
        self.ops.retain_clients(set());self.assertEqual(self.ops.clients,{})
        self.assertEqual(self.ops.demand(plan,'192.168.1.10'),'DEMAND_COOLDOWN')
        self.ops.demand_next=0;self.api.update('telescopes',{'auto_eligible':False},ident='auto-one');self.controller.cycle(50)
        self.assertEqual(self.ops.demand(plan,'192.168.1.9',kind='vespera',target='10.2.0.1'),'READY_CONNECTION_PRESERVED')
        self.assertNotIn('192.168.1.9',str(self.controller.status()['maker_demands']))
        self.assertEqual(self.controller.status()['maker_demand']['protocol'],'seestar-scan-iscope')

    def test_vespera_same_target_ambiguity_different_target_and_policy_validation(self):
        plan=self.controller.management_plan
        for invalid in (1,'true',None,[],{}):
            with self.assertRaises(Invalid):self.api.update('settings',{'auto_policy':{'vespera_demand':invalid}},expected='"'+self.api.revision()+'"')
        with self.assertRaises(Invalid):self.api.update('settings',{'auto_policy':{'vespera_demand':True}},expected='"'+self.api.revision()+'"')
        with self.assertRaises(ApiError):self.api.update('settings',{'auto_policy':{'vespera_demand':True},'acknowledge_interruption':True},expected='"stale"')
        self.vespera_policy();self.api.update('settings',{'country':'JP'});self.assertTrue(self.store.config['auto_policy']['vespera_demand'])
        self.api.update('telescopes',{'id':'second-vespera','name':'Second fictional','kind':'vespera','target_ip':'10.2.0.1','ssid':'Other-DEMO','password':'synthetic-other-pass','auto_eligible':True},create=True)
        previous=self.ops.last
        self.assertEqual(self.ops.demand(plan,'192.168.1.9',kind='vespera',target='10.2.0.1'),'SELECTION_REQUIRED');self.assertEqual(self.ops.last,previous)
        self.ops.demand_next=0
        self.assertEqual(self.ops.demand(plan,'192.168.1.9',kind='vespera',target='10.99.0.1'),'NO_ELIGIBLE_VESPERA')
        self.assertEqual(self.ops.last,previous)

    def test_vespera_quiet_replacement_protected_busy_and_shared_failure_gates(self):
        self.replacement();self.vespera_policy();plan=self.controller.management_plan
        self.controller.protection.owner_lock('primary',{'locked':True,'current_password':'local-admin-test-password'},'"'+self.api.revision()+'"',self.api.revision)
        self.assertEqual(self.ops.demand(plan,'192.168.1.9',kind='vespera',target='10.2.0.1'),'REQUEST_ACCEPTED')
        result=self.finish();self.assertEqual(result['state'],'failed');self.assertEqual(result['reason'],'NO_REPLACEABLE_CONNECTION')
        self.assertTrue(self.net.connected('telescope',self.devices[0]))
        self.controller.protection.owner_lock('primary',{'locked':False,'current_password':'local-admin-test-password'},'"'+self.api.revision()+'"',self.api.revision)
        self.ops.demand_next=0;self.ops.negative.clear()
        self.assertEqual(self.ops.demand(plan,'192.168.1.9',kind='vespera',target='10.2.0.1'),'REQUEST_ACCEPTED')
        result=self.finish();self.assertEqual(result['state'],'succeeded');self.assertEqual(result['displaced']['profile_id'],'primary')
        self.assertEqual(result['cause'],'VESPERA_CONNECTION')

    def test_seestar_demand_consent_independent_quota_selection_and_no_fabricated_session(self):
        c=self.controller;self.assertIsNotNone(c.management_plan);management=c.management_plan
        self.api.update('telescopes',{'kind':'seestar'},ident='auto-one')
        self.assertEqual(self.ops.demand(management,'192.168.1.9'),'DISABLED')
        self.api.update('settings',{'auto_policy':{'seestar_demand':True},'acknowledge_interruption':True},expected='"'+self.api.revision()+'"')
        for source in ('10.1.1.1','192.168.1.255','192.168.1.42','127.0.0.1','malformed'):
            self.assertEqual(self.ops.demand(management,source),'UNTRUSTED_MANAGEMENT_SOURCE')
        self.assertEqual(self.ops.demand(management,'192.168.1.9'),'REQUEST_ACCEPTED');self.assertEqual(self.finish()['state'],'succeeded')
        self.assertEqual(self.ops.clients,{});self.ops.retain_clients(set())
        self.assertEqual(self.ops.demand(management,'192.168.1.10'),'DEMAND_COOLDOWN')
        self.ops.demand_next=0
        self.assertEqual(self.ops.demand(management,'192.168.1.10'),'READY_CONNECTION_PRESERVED')
        self.assertNotIn('192.168.1.10',str(c.status()['maker_demand']))
        self.api.action('auto-one','disconnect');c.cycle(50);self.ops.demand_next=0
        self.api.update('telescopes',{'id':'second-seestar','name':'Second','kind':'seestar','target_ip':'10.9.0.1','ssid':'Second','password':'synthetic-pass','auto_eligible':True},create=True)
        last=self.ops.last
        self.assertEqual(self.ops.demand(c.management_plan,'192.168.1.9'),'SELECTION_REQUIRED');self.assertEqual(self.ops.last,last)
        self.assertEqual(self.ops.demand(c.management_plan,'192.168.1.9'),'DEMAND_COOLDOWN')

    def test_pending_native_admission_keeps_source_checks_and_does_not_spend_quota(self):
        c=self.controller;plan=c.management_plan
        self.api.update('telescopes',{'kind':'seestar'},ident='auto-one')
        self.api.update('settings',{'auto_policy':{'seestar_demand':True},'acknowledge_interruption':True},expected='"'+self.api.revision()+'"')
        c.last_cycle_ok=False
        self.assertEqual(self.ops.demand(plan,'192.168.1.9'),'RECONCILE_PENDING');self.assertEqual(self.ops.demand_next,0)
        self.assertEqual(self.ops.demand(plan,'198.51.100.2'),'UNTRUSTED_MANAGEMENT_SOURCE')
        c.last_cycle_ok=True
        with c.net_lock:
            self.assertEqual(self.ops.demand(plan,'192.168.1.9'),'RECONCILE_PENDING')
        self.assertEqual(self.ops.demand_next,0);self.assertFalse(self.ops.pending)
        self.assertEqual(self.ops.demand(plan,'192.168.1.9'),'REQUEST_ACCEPTED');self.assertEqual(self.finish()['state'],'succeeded')

    def test_unrelated_protected_proxy_preserved_in_every_replacement_plan(self):
        self.replacement();self.net.radios=3;self.controller.update(devices=self.net.devices())
        self.api.update('telescopes',{'kind':'seestar'},ident='auto-one')
        self.api.update('telescopes',{'id':'retained','name':'Retained','kind':'seestar','target_ip':'10.8.0.1',
            'ssid':'Retained-AP','bssid':'02:00:00:88:88:88','password':'retained-current-password','mac':self.devices[2]['mac'],'enabled':True},create=True)
        self.controller.cycle(40);self.controller.protection.create('retained',{},'retained-client','"'+self.api.revision()+'"',self.api.revision)
        with patch.object(self.net,'firewall',wraps=self.net.firewall) as firewall:
            self.request();result=self.finish()
        self.assertEqual(result['state'],'succeeded');self.assertIsNone(result['endpoint']['proxy'])
        for call in firewall.call_args_list:
            routes=call.kwargs.get('telescopes',[])
            self.assertTrue(any(r['profile_id']=='retained' and r['seestar_proxy'] for r in routes),routes)
        self.assertTrue(self.net.connected('telescope-retained',self.devices[2]));self.assertTrue(self.controller.protection.protected('retained'))

    def test_fresh_measurement_selects_least_recent_quiet_not_saved_order_or_cached_state(self):
        self.replacement();self.net.radios=3;self.controller.update(devices=self.net.devices())
        self.api.update('telescopes',{'id':'older-quiet','name':'Older quiet','kind':'generic','target_ip':'10.8.0.1',
            'ssid':'Other-AP','bssid':'02:00:00:88:88:88','password':'synthetic-other-pass','mac':self.devices[2]['mac'],
            'enabled':True,'auto_eligible':True,'auto_replaceable':True},create=True)
        self.controller.cycle(40)
        self.net.selection_activity=lambda profile,*_:dict(self.measured,last_activity_age_seconds=360 if profile['id']=='older-quiet' else 180)
        sample=patch.object(self.net,'sample_activity',wraps=self.net.sample_activity)
        with sample as collect:self.request();value=self.finish()
        self.assertEqual(value['state'],'succeeded');self.assertEqual(value['displaced']['profile_id'],'older-quiet')
        self.assertTrue(any(call.kwargs.get('force') is True for call in collect.call_args_list))
        self.assertTrue(self.net.connected('telescope',self.devices[0]))

    def test_pending_replacement_does_not_create_optional_ap_on_released_victim_radio(self):
        self.replacement();self.api.update('hotspot',{'enabled':True});self.controller.cycle(50)
        self.assertFalse(self.controller.state['hotspot']['active'])
        with patch.object(self.net,'hotspot',wraps=self.net.hotspot) as ap:
            self.request();result=self.finish()
        self.assertEqual(result['state'],'succeeded');ap.assert_not_called()

    def test_seestar_origin_correlates_victim_target_and_actual_restoration(self):
        self.replacement();self.api.update('telescopes',{'kind':'seestar'},ident='auto-one')
        self.api.update('settings',{'auto_policy':{'seestar_demand':True},'acknowledge_interruption':True},expected='"'+self.api.revision()+'"')
        original=self.net.connect
        def connect(role,*args):
            if role=='telescope-auto-one':raise NetworkError('target failed')
            return original(role,*args)
        with patch.object(self.net,'connect',side_effect=connect):
            self.assertEqual(self.ops.demand(self.controller.management_plan,'192.168.1.9'),'REQUEST_ACCEPTED');value=self.finish()
        self.assertEqual(value['restoration']['state'],'restored');self.assertEqual(value['cause'],'SEESTAR_DISCOVERY')
        events=[e for e in self.controller.event_store.events if e['operation_id']==value['id']]
        self.assertEqual({e['profile_id'] for e in events},{'primary','auto-one'})
        self.assertEqual({e['cause'] for e in events},{'SEESTAR_DISCOVERY'})
        self.assertIn('restored',[e['type'] for e in events])

    def test_expired_fresh_measurement_never_disconnects_victim(self):
        self.replacement();clock=[1000.];self.ops.clock=lambda:clock[0]
        self.net.sample_activity=lambda **_:clock.__setitem__(0,clock[0]+121)
        with patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect:
            self.request();value=self.finish()
        self.assertEqual(value['reason'],'TIMED_OUT');self.assertNotIn('displaced',value)
        self.assertTrue(self.net.connected('telescope',self.devices[0]));self.assertFalse(self.controller.pauses.records)
        self.assertFalse(any(call.args[0]=='telescope' for call in disconnect.call_args_list))

    def test_individual_pause_reserves_its_role_but_not_unrelated_free_radio(self):
        self.controller.pauses.hold(self.store.config['telescopes'][0])
        with patch.object(self.net,'connect',wraps=self.net.connect) as connect:
            self.request();value=self.finish()
        self.assertEqual(value['state'],'succeeded');self.assertEqual(self.store.config['telescopes'][1]['mac'],self.devices[2]['mac'])
        self.assertTrue(self.controller.pauses.held('primary'));self.assertFalse(self.controller.update_safe())
        self.assertFalse(any(call.args[0]=='telescope' for call in connect.call_args_list))

    def test_slow_candidate_search_refreshes_selected_quiet_without_forced_fallback(self):
        self.replacement(force=True);self.net.radios=3;self.controller.update(devices=self.net.devices())
        self.api.update('telescopes',{'id':'later','name':'Later','kind':'generic','target_ip':'10.8.0.1',
            'ssid':'Other-AP','bssid':'02:00:00:88:88:88','password':'synthetic-other-pass','mac':self.devices[2]['mac'],
            'enabled':True,'auto_eligible':True,'auto_replaceable':True},create=True)
        self.controller.cycle(40);clock=[1000.];self.ops.clock=lambda:clock[0];samples=[0]
        def sample(**kwargs):samples[0]+=1
        def measurement(profile,*args):
            if profile['id']=='later':clock[0]+=16;return dict(self.measured,last_activity_age_seconds=0)
            return dict(self.measured,**changed) if samples[0]>1 else self.measured.copy()
        self.net.sample_activity=sample;self.net.selection_activity=measurement
        for changed in ({'state':'active'},{'state':'unknown','quiet_seconds':None},{'sample_age_seconds':16}):
            self.ops.negative.clear();self.ops.clients.clear();samples[0]=0
            with patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect:
                self.request();value=self.finish()
            self.assertEqual(samples[0],2);self.assertEqual(value['reason'],'QUIET_ELIGIBILITY_CHANGED')
            self.assertNotIn('displaced',value);self.assertTrue(self.net.connected('telescope',self.devices[0]))
            self.assertFalse(any(call.args[0] in ('telescope','telescope-later') for call in disconnect.call_args_list))

    def test_chosen_quiet_sample_can_expire_during_final_association_checks(self):
        self.replacement(force=True);clock=[1000.];self.ops.clock=lambda:clock[0];original=self.net.link_observation;calls=[0]
        def link(device,profile):
            if profile['id']=='primary':
                calls[0]+=1
                if calls[0]==3:clock[0]+=16
            return original(device,profile)
        with patch.object(self.net,'link_observation',side_effect=link),patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect:
            self.request();value=self.finish()
        self.assertEqual(value['reason'],'QUIET_ELIGIBILITY_CHANGED');self.assertNotIn('displaced',value)
        self.assertTrue(self.net.connected('telescope',self.devices[0]));self.assertFalse(any(call.args[0]=='telescope' for call in disconnect.call_args_list))
