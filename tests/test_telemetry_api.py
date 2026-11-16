"""Real HTTP telemetry authorization plus bounded deterministic journal projections."""
import http.client
import json
import tracemalloc
import threading
from wifi_bridge.web import WebServer

from test_api import APITests
from test_bridge import Fixture
from test_telemetry import TelemetryTests
from wifi_bridge.telemetry.query import Query


class TelemetryHTTPTests(Fixture):
    def setUp(self):
        super().setUp(); self.configured(); self.controller.cycle(0)
        self.server = WebServer(('127.0.0.1', 0), self.controller)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.token = ''
    tearDown = APITests.tearDown
    request = APITests.request
    login = APITests.login
    def test_analysis_read_scope_and_no_raw_fetch_attribution(self):
        self.login()
        store=self.controller.telemetry_store
        ident=store.start_session('primary','vespera',['link_v1'])
        store.sample(ident,'stack.frames_accepted',7)
        route='/api/v1/telemetry/sessions/'+ident+'/analysis'
        self.server.sessions[self.token]['scopes']=['read']
        self.assertEqual(self.request('GET',route)[0],403)
        # Actual pairing/authentication establishes the telemetry-only client.
        self.server.sessions[self.token]['scopes']=['admin']
        code,client,_=self.request('POST','/api/v1/auth/clients',{'name':'Synthetic analysis reader','scopes':['telemetry']})
        self.assertEqual(code,201)
        code,login,_=self.request('POST','/api/v1/auth/token',{'client_id':client['client_id'],'client_secret':client['client_secret']})
        self.assertEqual(code,200);self.token=login['access_token']
        code,value,_=self.request('GET',route)
        self.assertEqual(code,200);self.assertEqual(value['points'][0]['high'],7)
        self.assertEqual(value['reader_coverage']['fetched'],[])
        self.assertFalse(store.reader_queue);self.assertFalse(store.coverage_queue)
        self.assertEqual(self.request('DELETE','/api/v1/telemetry/sessions/'+ident,{})[0],403)

    # APITests supplies the existing HTTP fixture; only these tests are selected here.
    def test_telemetry_scope_settings_preconditions_and_allowlists(self):
        self.login()
        self.server.sessions[self.token]['scopes'] = ['read']
        self.assertEqual(self.request('GET','/api/v1/telemetry')[0],403)
        self.server.sessions[self.token]['scopes'] = ['telemetry']
        code,status,_ = self.request('GET','/api/v1/telemetry')
        self.assertEqual(code,200);self.assertTrue(status['enabled'])
        self.assertIn('link_v1',status['profiles']['primary']['adapters'])
        self.assertEqual(status['latest_seq'],0)
        self.assertEqual(self.request('PATCH','/api/v1/telemetry/settings',{})[0],403)
        self.server.sessions[self.token]['scopes'] = ['admin']
        self.assertEqual(self.request('PATCH','/api/v1/telemetry/settings',{})[0],428)
        headers={'If-Match':'"'+status['config_revision']+'"'}
        for body in ({'retention_nights':0},{'retention_nights':True},{'max_bytes':512*1024**2+1},
                     {'profiles':{'primary':{'adapters':['raw']}}},{'profiles':{'missing':{'adapters':[]}}},
                     {'profiles':{'primary':{'adapters':[],'password':'forbidden-field'}}},{'secret':'forbidden-field'}):
            self.assertEqual(self.request('PATCH','/api/v1/telemetry/settings',body,headers)[0],400,body)
        code,changed,_=self.request('PATCH','/api/v1/telemetry/settings',{'retention_nights':2,'profiles':{'primary':{'adapters':['link_v1']}}},headers)
        self.assertEqual(code,200);self.assertTrue(changed['enabled'])
        self.assertNotEqual(changed['config_revision'],status['config_revision'])
        self.assertEqual(self.request('PATCH','/api/v1/telemetry/settings',{},headers)[0],412)
        self.assertEqual(set(changed),{'enabled','persistent','retention','used_bytes','sessions_count','clock_status','boot_id','latest_seq','config_revision','profiles','adapter_status','card_data'})

    def test_effective_defaults_opt_out_profile_patch_merge_and_persistence(self):
        from wifi_bridge.telemetry.settings import selected_adapters
        self.login()
        source = self.store.config['telescopes'][0]
        self.store.config['telescopes'].append(dict(source, id='second', kind='vespera', enabled=False))
        status = self.request('GET','/api/v1/telemetry')[1]
        self.assertEqual(status['profiles']['second']['adapters'],['link_v1','vespera_status_v1'])
        def change(body):
            rev = self.server.api.revision()
            code, result, _ = self.request('PATCH','/api/v1/telemetry/settings',body,{'If-Match':'"'+rev+'"'})
            self.assertEqual(code,200)
            return result
        first = change({'profiles':{'primary':{'adapters':[]}}})
        self.assertEqual(first['profiles']['primary']['adapters'],[])
        second = change({'profiles':{'second':{'adapters':['link_v1']}}})
        self.assertEqual(second['profiles']['primary']['adapters'],[])
        self.assertEqual(second['profiles']['second']['adapters'],['link_v1'])
        retained = json.loads(self.controller.telemetry_store.db.execute("SELECT value FROM metadata WHERE key='settings'").fetchone()[0])
        self.assertEqual(retained['profiles'],second['profiles'])
        self.assertEqual(selected_adapters(retained,source),[])
        disabled = change({'profiles':{'second':{'adapters':[]}}})
        self.assertFalse(disabled['enabled'])
        self.assertEqual(self.request('GET','/api/v1/telemetry')[1]['profiles'],disabled['profiles'])
        self.store.config['telescopes'] = [p for p in self.store.config['telescopes'] if p['id'] == 'second']
        cleaned = change({'retention_nights':3})
        self.assertEqual(cleaned['profiles'],{'second':{'adapters':[]}})

    def test_real_link_producer_samples_and_native_events_over_http(self):
        self.login()
        collector = self.controller.link_collector
        cached = {'primary':{'generation':'synthetic','activity':{'bytes_to_instrument':0,'bytes_from_instrument':0}}}
        self.net.link_observations = lambda rows: cached
        cursor = self.controller.event_store.sequence
        collector.tick()
        cached['primary']['activity']['bytes_to_instrument'] = 24
        collector.tick()
        active = self.controller.telemetry_sessions.active['primary']
        with self.controller.telemetry_sessions.lock: active['available_due'] = 0
        collector.tick()
        code,page,_ = self.request('GET','/api/v1/telemetry/samples?session_id='+active['id']+'&after=0')
        self.assertEqual(code,200)
        values = {sample['metric']:sample['value'] for sample in page['samples']}
        self.assertEqual(values['link.bytes_to'],24)
        self.assertIs(values['link.protected'],False)
        code,page,_ = self.request('GET','/api/v1/events?after='+str(cursor))
        telemetry = [event for event in page['events'] if event['type'].startswith('telemetry.')]
        self.assertEqual([event['type'] for event in telemetry],['telemetry.session_started','telemetry.available'])
        self.assertEqual(telemetry[-1]['latest_sample_seq'],max(sample['seq'] for sample in self.controller.telemetry_store.samples(active['id'])))
        self.assertEqual({event['session_id'] for event in telemetry},{active['id']})
        self.assertEqual(self.request('GET','/api/v1/instruments')[1]['instruments'][0]['capabilities']['telemetry'],['link_v1'])
        revision = self.server.api.revision()
        self.assertEqual(self.request('PATCH','/api/v1/telemetry/settings',{'profiles':{'primary':{'adapters':[]}}},
            {'If-Match':'"'+revision+'"'})[0],200)
        self.assertEqual(self.request('GET','/api/v1/instruments')[1]['instruments'][0]['capabilities']['telemetry'],[])
        self.controller._disconnect_profile('telescope')
        ended = self.request('GET','/api/v1/events?after='+str(telemetry[-1]['sequence']))[1]['events']
        self.assertEqual([event['type'] for event in ended if event['type'].startswith('telemetry.')],['telemetry.session_ended'])

    def test_telemetry_http_cookie_export_delete_and_reader_audit(self):
        self.login();store=self.controller.telemetry_store
        ident=store.start_session('primary','generic',['link_v1']);store.sample(ident,'link.ping_ms',1)
        for suffix in ('&limit=0','&limit=501','&after=-1','&boot_id=x','&limit=1&limit=2','&secret=x'):
            self.assertEqual(self.request('GET','/api/v1/telemetry/samples?session_id='+ident+suffix)[0],400)
        code,paired,_=self.request('POST','/api/v1/auth/clients',{'name':'Sample reader','scopes':['telemetry']})
        self.assertEqual(code,201)
        self.server.sessions[self.token]['client_id']=paired['client_id']
        writes=store.write_count
        self.assertEqual(self.request('GET','/api/v1/telemetry/samples?session_id='+ident+'&after=0')[0],200)
        detail=self.request('GET','/api/v1/telemetry/sessions/'+ident)[1]
        self.assertEqual(detail['readers'],[{'client_name':'Sample reader','from_seq':2,'to_seq':2}])
        self.assertEqual(store.write_count,writes)
        store.sample(ident,'link.ping_ms',2)
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        c.request('GET','/api/v1/telemetry/sessions/'+ident+'/export?format=csv',headers={'Authorization':'Bearer '+self.token})
        response=c.getresponse();body=response.read();c.close()
        self.assertEqual(response.status,200);self.assertIn('generic-',response.getheader('Content-Disposition'))
        self.assertTrue(body.startswith(b'seq,utc,utc_corrected,metric,value\r\n'))
        self.assertEqual(self.request('GET','/api/v1/telemetry/sessions/'+ident)[1]['readers'][0]['to_seq'],3)
        self.assertEqual(store.write_count,writes)
        origin='http://127.0.0.1:'+str(self.server.server_port)
        _,_,cookie=self.request('POST','/api/login',{'password':'local-admin-test-password'},{'Origin':origin})
        browser={'Cookie':cookie.split(';')[0],'Authorization':''}
        self.assertEqual(self.request('GET','/api/telemetry',headers=browser)[0],200)
        self.assertEqual(self.request('PATCH','/api/telemetry/settings',{},browser)[0],403)
        csrf=self.request('GET','/api/status',headers=browser)[1]['csrf']
        rev=self.server.api.revision()
        browser.update({'Origin':origin,'X-CSRF-Token':csrf,'If-Match':'"'+rev+'"'})
        self.assertEqual(self.request('PATCH','/api/telemetry/settings',{},browser)[0],200)
        self.assertEqual(self.request('DELETE','/api/v1/telemetry/sessions/'+ident)[0],428)
        self.assertEqual(self.request('DELETE','/api/telemetry/sessions/'+ident,headers=browser)[0],200)
        self.assertEqual(self.request('GET','/api/v1/telemetry/sessions/'+ident)[0],404)


class QueryTests(__import__("unittest").TestCase):
    setUp = TelemetryTests.setUp
    store = TelemetryTests.store
    def test_query_paging_resets_interleaving_no_flush_and_reservations(self):
        store=self.store();q=Query(store)
        first=store.start_session('first','generic',[]);second=store.start_session('second','generic',[])
        store.sample(first,'link.ping_ms',1);store.sample(second,'link.ping_ms',2);store.sample(first,'link.ping_ms',3)
        before=store.write_count
        page=q.page(first,0,store.boot_id,1)
        self.assertTrue(page['has_more']);self.assertEqual(page['next_cursor'],1);self.assertFalse(page['gap'])
        page=q.page(first,1,store.boot_id,1);self.assertEqual(page['next_cursor'],3)
        page=q.page(first,3,store.boot_id,1);self.assertEqual(page['next_cursor'],5);self.assertFalse(page['has_more'])
        self.assertEqual(q.sessions(limit=1)['next_cursor'],1)
        self.assertEqual(len(q.sessions(after=1)['sessions']),1)
        self.assertEqual(q.page(first,999)['reset_reason'],'CURSOR_AHEAD')
        self.assertEqual(q.page(first,0,'f'*32)['reset_reason'],'BOOT_CHANGED')
        self.assertEqual(store.write_count,before)
        store.flush()
        with store.db:store.db.execute('DELETE FROM samples WHERE seq=3')
        self.assertEqual(q.page(first,0)['reset_reason'],'RETENTION_GAP')
        store.history_available=False
        self.assertEqual(q.page(first)['reset_reason'],'HISTORY_UNAVAILABLE')
        store.sequence=store.reserved_sequence
        self.assertEqual(q.latest(),5)
        store.reader(first,'Reader',5,5);store.flush()
        self.assertEqual(q.latest(),5)

    def test_reader_audit_is_bounded_coalesced_and_retains_ranges(self):
        store=self.store();q=Query(store)
        ident=store.start_session('demo','generic',[])
        for _ in range(10):store.sample(ident,'link.ping_ms',1)
        before=store.write_count
        for i in range(32):store.reader(ident,'Reader %02d' % i,2,3)
        self.assertEqual(len(store.reader_queue),16)
        for _ in range(1000):store.reader(ident,'Reader 00',5,11)
        self.assertEqual(store.write_count,before)
        self.time.mono=30;store.maintain()
        self.assertFalse(store.reader_queue)
        store.reader(ident,'Reader 00',6,9)
        reader=next(r for r in q.summary(q.session(ident))['readers'] if r['client_name']=='Reader 00')
        self.assertEqual(reader,{'client_name':'Reader 00','from_seq':2,'to_seq':11})
        with store.db:store.db.execute('DELETE FROM sessions WHERE id=?',(ident,))
        store.reader_queue.clear()
        store.reader(ident,'Reader 00',2,11)
        self.assertFalse(store.reader_queue)

    def test_exact_export_summary_and_large_export_memory(self):
        store=self.store();store.boot_id='0'*32;q=Query(store)
        ident=store.start_session('demo','generic',['link_v1'])
        store.sample(ident,'stack.frames_accepted',2)
        store.sample(ident,'target.name','Target, "A"')
        row=q.session(ident)
        expected=('seq,utc,utc_corrected,metric,value\r\n2,2026-10-01T20:00:00+00:00,,stack.frames_accepted,2\r\n'
                  '3,2026-10-01T20:00:00+00:00,,target.name,"Target, ""A"""\r\n').encode()
        self.assertEqual(b''.join(q.export(row,'csv')),expected)
        raw_json=b''.join(q.export(row,'json'))
        exported=json.loads(raw_json)
        self.assertEqual(raw_json, json.dumps(exported,ensure_ascii=False,separators=(',',':')).encode())
        store.flush()
        self.assertEqual(b''.join(q.export(row,'csv')),expected)
        self.assertEqual(exported['session']['frames_accepted'],2)
        self.assertEqual(exported['samples'],q.page(ident,0)['samples'])
        self.assertEqual(exported['events'],q.page(ident,0)['events'])
        for _ in range(20000):store.sample(ident,'link.ping_ms',1)
        before=store.write_count
        tracemalloc.start();size=sum(len(chunk) for chunk in q.export(row,'json'));_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        self.assertGreater(size,1000000);self.assertLess(peak,2*1024*1024)
        print('Telemetry streamed export bytes=%d Python peak_bytes=%d' % (size, peak))
        self.assertEqual(store.write_count,before)

# Imported fixture classes are not additional suites in this module.
del APITests, TelemetryTests, Fixture
