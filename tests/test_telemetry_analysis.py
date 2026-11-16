"""Retained-session projection, conservative integration and exact reader audit."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wifi_bridge.telemetry.store import TelemetryStore
from wifi_bridge.telemetry.query import Query
from wifi_bridge.telemetry.analysis import project, coverage


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.now=1.0
        clock=SimpleNamespace(read=lambda:{'system_utc':'2026-01-01T00:00:00+00:00','corrected_utc':None,
                                          'source':'system','offset_ms':0,'clock_status':'unverified'})
        self.store=TelemetryStore(Path(self.folder.name)/'telemetry.sqlite3',clock=clock,monotonic=lambda:self.now)
        self.addCleanup(self.store.close);self.q=Query(self.store)
        self.ident=self.store.start_session('scope','generic',[])

    def put(self,metric,value):
        self.now+=.01
        return self.store.sample(self.ident,metric,value)

    def result(self): return project(self.q,self.q.session(self.ident))

    def test_full_retained_timeline_is_bounded_and_reads_do_not_flush(self):
        self.put('exposure.seconds',10);self.put('stack.frames_accepted',0)
        for number in range(1,2001): self.put('stack.frames_accepted',number)
        before=self.store.write_count;queued=len(self.store.queue)
        result=self.result()
        self.assertEqual(result['sample_count'],2002)
        self.assertLessEqual(len(result['points']),240)
        self.assertEqual(min(r['low'] for r in result['points']),0)
        self.assertEqual(max(r['high'] for r in result['points']),2000)
        self.assertEqual(result['exposure'],{'observed_seconds':20000,'observed_frames':2000,'complete':True,'latest_seconds':10})
        self.assertEqual(self.store.write_count,before);self.assertEqual(len(self.store.queue),queued)
        self.assertEqual(self.result(),result)

    def test_changes_resets_and_unknown_initial_frames_never_invent_exposure(self):
        self.put('stack.frames_accepted',100);self.put('exposure.seconds',10)
        self.put('stack.frames_accepted',101);self.put('stack.frames_accepted',102)
        self.put('exposure.seconds',20);self.put('stack.frames_accepted',103)
        self.put('stack.frames_accepted',104);self.put('stack.frames_accepted',0)
        self.put('stack.frames_accepted',1)
        value=self.result()['exposure']
        self.assertEqual(value['observed_seconds'],50);self.assertEqual(value['observed_frames'],3)
        self.assertFalse(value['complete']);self.assertEqual(value['latest_seconds'],20)

    def test_missing_exposure_and_journal_gap_are_unavailable(self):
        self.put('stack.frames_accepted',0);self.put('stack.frames_accepted',2)
        self.assertIsNone(self.result()['exposure']['observed_seconds'])
        seq=self.put('exposure.seconds',10);self.put('stack.frames_accepted',3);self.put('stack.frames_accepted',4)
        self.store.flush()
        with self.store.db:self.store.db.execute('DELETE FROM samples WHERE seq=?',(seq,))
        self.assertIsNone(self.result()['exposure']['observed_seconds'])
        self.assertTrue(self.result()['gaps'])

    def test_exact_fetch_holes_survive_flush_and_repeated_reads_are_deduplicated(self):
        for number in range(20):self.put('stack.frames_accepted',number)
        self.store.reader(self.ident,'Synthetic reader',2,5)
        self.store.reader(self.ident,'Synthetic reader',10,15)
        expected={'unknown_before_seq':0,'fetched':[{'from_seq':2,'to_seq':5},{'from_seq':10,'to_seq':15}]}
        self.assertEqual(coverage(self.store,self.ident),expected)
        self.store.flush();before=self.store.write_count
        self.assertEqual(coverage(self.store,self.ident),expected)
        for _ in range(100):self.store.reader(self.ident,'Synthetic reader',2,5)
        self.assertFalse(self.store.coverage_queue);self.assertFalse(self.store.reader_queue)
        self.assertEqual(self.store.write_count,before)
        self.assertEqual(self.q.summary(self.q.session(self.ident))['readers'][0]['to_seq'],15)

    def test_legacy_and_capped_detail_stays_unknown_and_new_reads_are_exact(self):
        for number in range(400):self.put('stack.frames_accepted',number)
        self.store.flush()
        for seq in range(2,302,2):self.store.reader(self.ident,'Synthetic reader',seq,seq)
        result=coverage(self.store,self.ident)
        self.assertEqual(len(result['fetched']),128);self.assertGreater(result['unknown_before_seq'],0)
        self.assertTrue(all(row['from_seq']==row['to_seq'] for row in result['fetched']))
        self.store.flush()
        with self.store.db:self.store.db.execute('DELETE FROM telemetry_coverage WHERE session_id=?',(self.ident,))
        self.assertIsNone(coverage(self.store,self.ident)['unknown_before_seq'])
        self.store.reader(self.ident,'New reader',350,360);self.store.flush()
        self.assertEqual(coverage(self.store,self.ident),{'unknown_before_seq':None,'fetched':[{'from_seq':350,'to_seq':360}]})

    def test_no_frame_readings_is_empty_and_deletion_cascades_audit(self):
        self.put('link.rssi_dbm',-50)
        self.store.reader(self.ident,'Synthetic reader',2,2);self.store.flush()
        self.assertEqual(self.result()['points'],[])
        with self.store.db:self.store.db.execute('DELETE FROM sessions WHERE id=?',(self.ident,))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM telemetry_coverage').fetchone()[0],0)

    def test_work_limit_never_returns_partial_chart_and_clears_sql_handler(self):
        for number in range(1000):self.put('stack.frames_accepted',number)
        before=self.store.write_count
        with patch('wifi_bridge.telemetry.analysis.SQL_BUDGET',0):
            result=self.result()
        self.assertTrue(result['limited']);self.assertEqual(result['points'],[])
        self.assertIsNone(result['sample_count']);self.assertEqual(self.store.write_count,before)
        self.assertFalse(self.result()['limited'])

    def test_integration_bound_does_not_mislabel_old_setting_as_latest(self):
        self.put('exposure.seconds',10);self.put('stack.frames_accepted',0)
        self.put('stack.frames_accepted',1);self.put('exposure.seconds',20)
        with patch('wifi_bridge.telemetry.analysis.EXPOSURE_ROWS',3):result=self.result()
        self.assertFalse(result['exposure']['complete']);self.assertEqual(result['exposure']['latest_seconds'],20)
        self.assertEqual(result['exposure']['observed_seconds'],10)

    def test_pending_session_cap_degrades_negative_coverage_persistently(self):
        self.store.end_session(self.ident,'idle');ids=[]
        for _ in range(129):
            ident=self.store.start_session('scope','generic',[])
            seq=self.store.sample(ident,'link.rssi_dbm',-50);ids.append((ident,seq))
            self.store.end_session(ident,'idle')
        self.store.flush()
        for ident,seq in ids:self.store.reader(ident,'Synthetic reader',seq,seq)
        self.assertEqual(len(self.store.coverage_queue),128)
        self.assertTrue(self.store.coverage_degraded)
        self.assertIsNone(coverage(self.store,ids[-1][0])['unknown_before_seq'])
        self.store.flush()
        self.assertIsNotNone(self.store.db.execute("SELECT value FROM metadata WHERE key='coverage_degraded'").fetchone())

    def test_scope_and_http_projection_do_not_audit_derived_reads(self):
        from wifi_bridge.api import MobileAPI
        self.assertEqual(MobileAPI.required_scope('GET','/telemetry/sessions/id/analysis'),'telemetry')
        self.put('stack.frames_accepted',2)
        self.result();self.assertFalse(self.store.reader_queue);self.assertFalse(self.store.coverage_queue)


if __name__=='__main__':unittest.main()
