"""CI-friendly HTTP acquisition sequence against the conformance demo process."""
import http.client
import json
from pathlib import Path
import selectors
import struct
import subprocess
import sys
import time
import unittest
from urllib.parse import urlencode, urlsplit


class DemoArgumentsTests(unittest.TestCase):
    def test_requires_explicit_demo_and_valid_port_before_startup(self):
        root = Path(__file__).resolve().parents[1]
        script = root / 'scripts/alpaca_conformance_demo.py'
        for args in ([], ['--demo', '--port', '-1'], ['--demo', '--port', '65536']):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(script), *args],
                                        cwd=root, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertLess(len(result.stderr), 1024)


class AlpacaSmokeTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.process = subprocess.Popen(
            [sys.executable, str(root / 'scripts/alpaca_conformance_demo.py'),
             '--demo', '--port', '0'], cwd=root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.close_demo)
        with selectors.DefaultSelector() as ready:
            ready.register(self.process.stdout, selectors.EVENT_READ)
            self.assertTrue(ready.select(10), 'Camera demo startup timed out')
        endpoint = json.loads(self.process.stdout.readline())
        self.assertTrue(endpoint['demo'])
        parsed = urlsplit(endpoint['camera'])
        self.assertEqual(parsed.hostname, '127.0.0.1')
        self.port = parsed.port
        self.transaction = self.server_transaction = 0

    def close_demo(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            _, errors = self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()
            self.fail('Camera demo did not shut down after SIGTERM')
        self.assertEqual(self.process.returncode, 0, errors)
        self.assertEqual(errors, '')

    def request(self, member, method='GET', values=None, binary=False):
        self.transaction += 1
        params = dict(values or {}, ClientID=73, ClientTransactionID=self.transaction)
        path = '/api/v1/camera/0/' + member
        body, headers = None, {}
        if method == 'GET':
            path += '?' + urlencode(params)
        else:
            body = urlencode(params)
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        if binary:
            headers['Accept'] = 'application/imagebytes'
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            raw = response.read()
            self.assertEqual(response.status, 200)
            if binary:
                self.assertEqual(response.getheader('Content-Type'), 'application/imagebytes')
                header = struct.unpack('<11I', raw[:44])
                self.assertEqual(header[:3], (1, 0, self.transaction))
                server_transaction = header[3]
                result = header, raw[44:]
            else:
                self.assertEqual(response.getheader('Content-Type'), 'application/json')
                data = json.loads(raw)
                self.assertEqual(data['ErrorNumber'], 0, data['ErrorMessage'])
                self.assertEqual(data['ClientTransactionID'], self.transaction)
                server_transaction = data['ServerTransactionID']
                result = data.get('Value')
            self.assertGreater(server_transaction, self.server_transaction)
            self.server_transaction = server_transaction
            return result
        finally:
            connection.close()

    def test_connect_info_expose_imagebytes_abort_disconnect(self):
        self.request('connected', 'PUT', {'Connected': 'true'})
        self.assertTrue(self.request('connected'))
        self.assertEqual(self.request('interfaceversion'), 3)
        self.assertEqual(self.request('name'), 'simulator')
        self.assertTrue(self.request('description'))
        width, height = self.request('cameraxsize'), self.request('cameraysize')
        self.assertEqual((width, height), (320, 240))
        self.assertFalse(self.request('imageready'))
        self.request('startexposure', 'PUT', {'Duration': '0.1', 'Light': 'true'})
        deadline = time.monotonic() + 5
        while not self.request('imageready'):
            self.assertLess(time.monotonic(), deadline, 'ImageReady deadline exceeded')
            time.sleep(0.02)
        self.assertEqual(self.request('lastexposureduration'), 0.1)
        header, pixels = self.request('imagearray', binary=True)
        self.assertEqual(header[4:], (44, 2, 8, 2, width, height, 0))
        self.assertEqual(len(pixels), width * height * 2)
        values = struct.unpack('<' + str(width * height) + 'H', pixels)
        self.assertGreater(max(values), min(values))
        # Independent JSON decoding verifies the byte order and x/y layout.
        columns = self.request('imagearray')
        self.assertEqual(len(columns), width)
        self.assertTrue(all(len(column) == height for column in columns))
        self.assertEqual(tuple(value for column in columns for value in column), values)
        self.request('startexposure', 'PUT', {'Duration': '2', 'Light': 'true'})
        self.assertEqual(self.request('camerastate'), 2)
        self.request('abortexposure', 'PUT')
        self.assertEqual(self.request('camerastate'), 0)
        self.assertFalse(self.request('imageready'))
        self.request('connected', 'PUT', {'Connected': 'false'})
        self.assertFalse(self.request('connected'))


if __name__ == '__main__':
    unittest.main()
