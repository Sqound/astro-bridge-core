"""Real bounded child IPC; no push provider or private device subscription."""
import os
import sys
import time
import unittest
from wifi_bridge.push_worker import CryptoWorker

ECHO="import sys,json,os\nfor line in sys.stdin.buffer: print(json.dumps({'pid':os.getpid(),'action':json.loads(line)['action']}),flush=True)"


class WorkerTests(unittest.TestCase):
    def worker(self, code=ECHO,timeout=2):
        worker=CryptoWorker([sys.executable,'-u','-c',code],{'PATH':os.defpath},timeout)
        self.addCleanup(worker.close);return worker

    def test_warm_process_reused_and_close_is_final(self):
        worker=self.worker();one=worker.request({'action':'first'});two=worker.request({'action':'second'})
        self.assertEqual(one['pid'],two['pid']);self.assertEqual(two['action'],'second')
        process=worker.process;worker.close();self.assertIsNotNone(process.poll())
        with self.assertRaises(ValueError):worker.request({'action':'closed'})

    def test_stalled_reader_deadline_kills_without_automatic_replay(self):
        worker=self.worker('import time;time.sleep(10)',timeout=.15);start=time.monotonic()
        with self.assertRaises(ValueError):worker.request({'action':'send','data':'x'*32000})
        self.assertLess(time.monotonic()-start,2);self.assertIsNone(worker.process)
        worker.command=[sys.executable,'-u','-c',ECHO]
        self.assertEqual(worker.request({'action':'new-request'})['action'],'new-request')

    def test_input_output_and_protocol_are_bounded_and_errors_are_redacted(self):
        for code in ("print('x'*8192,flush=True)","print('PRIVATE_CANARY',flush=True)","print('{}\\n{}',flush=True)"):
            worker=self.worker(code)
            with self.assertRaises(ValueError) as error:worker.request({'action':'PRIVATE_CANARY'})
            self.assertNotIn('PRIVATE_CANARY',str(error.exception));self.assertIsNone(worker.process)
        worker=self.worker()
        with self.assertRaises(ValueError):worker.request({'data':'x'*32768})
        self.assertIsNone(worker.process)
