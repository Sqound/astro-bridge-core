"""The real CLI uses the documented initial password and preserves owner credentials."""
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from wifi_bridge.config import Store,password_hash
from wifi_bridge.network import DemoNetwork

ROOT=Path(__file__).resolve().parents[1]


class DefaultPasswordTests(unittest.TestCase):
    def start_demo(self,state):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        process=subprocess.Popen([sys.executable,'-m','wifi_bridge','--demo','--state-dir',str(state),'--port',str(port)],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        def stop():
            process.terminate()
            try:process.communicate(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.communicate()
        self.addCleanup(stop)
        for _ in range(100):
            try:
                connection=http.client.HTTPConnection('127.0.0.1',port,timeout=1)
                connection.request('GET','/api/ui-info');response=connection.getresponse();response.read();connection.close()
                if response.status==200:return port
            except OSError:pass
            if process.poll() is not None:self.fail('Demo exited before accepting requests')
            time.sleep(.05)
        self.fail('Demo did not become reachable')

    def login(self,port,password):
        connection=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
        connection.request('POST','/api/login',json.dumps({'password':password}),{'Content-Type':'application/json','Origin':f'http://127.0.0.1:{port}'})
        response=connection.getresponse();response.read();code=response.status;connection.close();return code

    def test_fresh_demo_accepts_the_same_initial_password_as_hardware(self):
        with tempfile.TemporaryDirectory() as folder:
            port=self.start_demo(Path(folder))
            self.assertEqual(self.login(port,'12345678'),200)
            self.assertEqual(self.login(port,'incorrect-initial-code'),401)

    def test_existing_owner_password_is_not_reset_to_the_new_default(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder);store=Store(state,DemoNetwork().devices()[0]['mac'])
            store.save(dict(store.config,admin_hash=password_hash('owner-custom-passphrase')))
            port=self.start_demo(state)
            self.assertEqual(self.login(port,'12345678'),401)
            self.assertEqual(self.login(port,'owner-custom-passphrase'),200)
