"""Camera V3 member coverage, bounded images, leases and real loopback HTTP/UDP."""
import http.client
import json
from pathlib import Path
import socket
import struct
import tempfile
import time
import threading
import unittest
from unittest.mock import patch, MagicMock
from urllib.parse import urlencode

from wifi_bridge.cameras.alpaca import READ, WRITE, AlpacaError, image_bytes, image_json
from wifi_bridge.cameras.frames import Frame
from wifi_bridge.cameras.listener import AlpacaServer, Discovery, ManagementAccess
from wifi_bridge.cameras.service import CameraService
from wifi_bridge.cameras.simulator import SimulatedCamera

# Independent member coverage table transcribed from official Camera V3 interface.
MEMBERS = {
    'get': '''bayeroffsetx bayeroffsety binx biny camerastate cameraxsize cameraysize
        canabortexposure canasymmetricbin canfastreadout cangetcoolerpower canpulseguide
        cansetccdtemperature canstopexposure ccdtemperature cooleron coolerpower
        electronsperadu exposuremax exposuremin exposureresolution fastreadout
        fullwellcapacity gain gainmax gainmin gains hasshutter heatsinktemperature
        imagearray imagearrayvariant imageready ispulseguiding lastexposureduration
        lastexposurestarttime maxadu maxbinx maxbiny numx numy offset offsetmax offsetmin
        offsets percentcompleted pixelsizex pixelsizey readoutmode readoutmodes sensorname
        sensortype setccdtemperature startx starty subexposureduration connected
        description driverinfo driverversion interfaceversion name supportedactions'''.split(),
    'put': '''binx biny cooleron fastreadout gain numx numy offset readoutmode
        setccdtemperature startx starty subexposureduration abortexposure pulseguide
        startexposure stopexposure connected action commandblind commandbool commandstring'''.split(),
}


class AlpacaTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.service = CameraService(clock=lambda: self.now)
        self.camera = SimulatedCamera(width=8, height=6, clock=lambda: self.now)
        self.service.add('simulation', 'simulator', self.camera)
        self.access = ManagementAccess('127.0.0.1', '127.0.0.0/8', ['localhost'])
        self.server = AlpacaServer(self.access, self.service, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()

    def request(self, member, method='GET', params=None, *, host=None, accept=None, path=None, body=None):
        path = path or '/api/v1/camera/0/' + member
        params = dict(params or {})
        if method == 'GET' and params:
            path += '?' + urlencode(params)
        headers = {'Host': host or 'localhost:' + str(self.server.server_port)}
        if accept:
            headers['Accept'] = accept
        if method == 'PUT':
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            body = urlencode(params) if body is None else body
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
            return response.status, data if response.getheader('Content-Type') == 'application/imagebytes' else json.loads(data)
        finally:
            conn.close()

    def connect(self):
        self.assertEqual(self.request('connected', 'PUT', {'Connected': 'true'})[1]['ErrorNumber'], 0)

    def expose(self, client=7):
        return self.request('startexposure', 'PUT', {'ClientID': client, 'Duration': 0.01, 'Light': 'true'})[1]

    def test_member_coverage(self):
        self.assertEqual(READ, set(MEMBERS['get']))
        self.assertEqual(WRITE, set(MEMBERS['put']))
        self.connect()
        for method, members in MEMBERS.items():
            for member in members:
                with self.subTest(method=method, member=member):
                    status, result = self.request(member, method.upper())
                    self.assertEqual(status, 200)
                    self.assertIn(result['ErrorNumber'], (0, 0x400, 0x401, 0x402))
                    self.assertIn('ServerTransactionID', result)
        self.assertEqual(self.request('imagearrayvariant')[1]['ErrorNumber'], 0x400)

    def test_management_transactions_and_route_separation(self):
        status, data = self.request('', params={'cLiEnTtRaNsAcTiOnId': '4294967295'}, path='/management/apiversions')
        self.assertEqual(status, 200)
        self.assertEqual(data['Value'], [1])
        self.assertEqual(data['ClientTransactionID'], 4294967295)
        second = self.request('', path='/management/v1/configureddevices')[1]
        self.assertGreater(second['ServerTransactionID'], data['ServerTransactionID'])
        self.assertEqual(second['Value'][0]['DeviceType'], 'Camera')
        self.assertEqual(self.request('', path='/api/v1/info')[0], 404)
        self.assertEqual(self.request('', path='/api/v1/cameras')[0], 404)
        self.assertEqual(self.request('', path='/management/v1/description')[1]['Value']['ServerName'], 'Astro Bridge')

    def test_monochrome_bayer_offsets_are_not_implemented_and_colour_offsets_are_stable(self):
        for member in ('bayeroffsetx', 'bayeroffsety'):
            self.assertEqual(self.request(member)[1]['ErrorNumber'], 0x407)
        self.connect()
        self.assertEqual(self.request('sensortype')[1]['Value'], 0)
        for member in ('bayeroffsetx', 'bayeroffsety'):
            result = self.request(member)[1]
            self.assertEqual(result['ErrorNumber'], 0x400)
            self.assertNotIn('Value', result)
        self.service.add('colour', 'simulator', SimulatedCamera(width=8, height=6, bayer=True))
        path = '/api/v1/camera/1/'
        self.assertEqual(self.request('', 'PUT', {'Connected': 'true'}, path=path + 'connected')[1]['ErrorNumber'], 0)
        self.assertEqual(self.request('', path=path + 'sensortype')[1]['Value'], 2)
        for member in ('bayeroffsetx', 'bayeroffsety'):
            result = self.request('', path=path + member)[1]
            self.assertEqual((result['ErrorNumber'], result['Value']), (0, 0))
        for member in ('startx', 'starty'):
            self.assertEqual(self.request('', 'PUT', {member: '1'}, path=path + member)[1]['ErrorNumber'], 0)
        for member in ('bayeroffsetx', 'bayeroffsety'):
            self.assertEqual(self.request('', path=path + member)[1]['Value'], 0)

    def test_shutterless_uvc_http_ignores_light_false_and_preserves_real_pixels(self):
        from test_camera_uvc import DEVICE, FakeVideo
        from wifi_bridge.cameras.uvc import UVCCamera
        io = FakeVideo()
        driver = UVCCamera(device=DEVICE, pixel_um=(3.75, 3.75), io=io, clock=lambda: io.now)
        self.service.add('uvc', 'uvc', driver)
        path = '/api/v1/camera/1/'
        self.assertEqual(self.request('', 'PUT', {'Connected': 'true'}, path=path + 'connected')[1]['ErrorNumber'], 0)
        self.assertFalse(self.request('', path=path + 'hasshutter')[1]['Value'])
        for member in ('bayeroffsetx', 'bayeroffsety'):
            self.assertEqual(self.request('', path=path + member)[1]['ErrorNumber'], 0x400)
        images = []
        for light in ('true', 'false'):
            response = self.request('', 'PUT', {'Duration': '.1', 'Light': light}, path=path + 'startexposure')[1]
            self.assertEqual(response['ErrorNumber'], 0)
            self.service._call(lambda: io.frames.append(bytes([12, 23, 34, 45])))
            self.assertTrue(self.request('', path=path + 'imageready')[1]['Value'])
            response = self.request('', path=path + 'imagearray')[1]
            self.assertEqual(response['ErrorNumber'], 0)
            images.append(response['Value'])
        self.assertEqual(images, [[[12, 34], [23, 45]]] * 2)
        self.assertEqual(io.counts['STREAMON'], 2)
        self.assertEqual(io.counts['STREAMOFF'], 2)
        self.assertTrue(all(mapping.closed for mapping in io.mappings))

    def test_shutterless_simulator_facade_ignores_light_without_changing_driver_dark_support(self):
        self.connect()
        self.assertFalse(self.request('hasshutter')[1]['Value'])
        images = []
        for light in ('true', 'false'):
            self.assertEqual(self.request('startexposure', 'PUT', {'Duration': '.1', 'Light': light})[1]['ErrorNumber'], 0)
            self.now += 1
            images.append(self.request('imagearray')[1]['Value'])
        self.assertEqual(images[0], images[1])

    def test_host_subnets_disabled_and_malformed_parameters(self):
        for host in ('example.invalid:' + str(self.server.server_port), 'localhost:80', 'localhost:0', 'user@localhost:' + str(self.server.server_port)):
            self.assertEqual(self.request('name', host=host)[0], 421)
        self.connect()
        self.assertEqual(self.request('gain', 'PUT', {'gAiN': '3'})[1]['ErrorNumber'], 0)
        self.assertEqual(self.request('gain')[1]['Value'], 3)
        self.assertEqual(self.request('gain', 'PUT', body='Gain=2&gain=4')[1]['ErrorNumber'], 0x401)
        self.assertEqual(self.request('name', params={'ClientID': '-1'})[1]['ErrorNumber'], 0x401)
        self.assertEqual(self.request('startexposure', 'PUT', {'Duration': 'nan', 'Light': 'true'})[1]['ErrorNumber'], 0x401)
        self.service.update_settings({'allowed_subnets': ['192.0.2.0/24']})
        self.assertEqual(self.request('name')[0], 403)
        self.service.update_settings({'allowed_subnets': [], 'serve': False})
        self.assertEqual(self.request('name')[0], 403)
        self.assertFalse(self.camera._opened)
        with self.assertRaises(ValueError):
            ManagementAccess('0.0.0.0', '0.0.0.0/0', [])

    def test_imagebytes_and_json_against_simulator(self):
        self.connect()
        self.assertEqual(self.expose()['ErrorNumber'], 0)
        self.now = 1
        status, binary = self.request('imagearray', params={'ClientTransactionID': 42}, accept='application/imagebytes')
        self.assertEqual(status, 200)
        header = struct.unpack('<11I', binary[:44])
        self.assertEqual(header[:3], (1, 0, 42))
        self.assertEqual(header[4:], (44, 2, 8, 2, 8, 6, 0))
        frame = self.service.execute(0, 'GET', 'imagearray', {}, 7, '127.0.0.1')
        expected = [struct.unpack_from('<H', frame.data, (y * 8 + x) * 2)[0] for x in range(8) for y in range(6)]
        self.assertEqual(list(struct.unpack('<48H', binary[44:])), expected)
        result = self.request('imagearray')[1]
        self.assertEqual(result['Type'], 2)
        self.assertEqual(result['Rank'], 2)
        self.assertEqual([p for column in result['Value'] for p in column], expected)
        self.assertEqual(self.request('imagearray', accept='application/imagebytes;q=0')[1]['Value'], result['Value'])

    def test_image_size_guard_and_chunk_bounds(self):
        frame = Frame(1001, 1000, 'Byte', bytes(1001000), {})
        with self.assertRaises(AlpacaError) as raised:
            next(image_json(frame, 0, 1))
        self.assertEqual(raised.exception.number, 0x500)
        header, *chunks = image_bytes(Frame(2, 3, 'Byte', bytes([1, 2, 3, 4, 5, 6]), {}), 0, 1)
        self.assertEqual(struct.unpack('<11I', header)[6], 6)
        self.assertEqual(b''.join(chunks), bytes([1, 3, 5, 2, 4, 6]))
        maximum = max(len(chunk) for chunk in image_bytes(frame, 0, 1))
        self.assertLessEqual(maximum, 16384)
        # Exercise HTTP guard using an injected completed frame, without a huge JSON allocation.
        self.connect()
        self.service._call(lambda: setattr(self.service._cameras['simulation'], 'frame', frame))
        result = self.request('imagearray')[1]
        self.assertEqual(result['ErrorNumber'], 0x500)
        self.assertIn('ImageBytes', result['ErrorMessage'])

    def test_owner_competition_idle_and_explicit_release(self):
        self.connect()
        self.assertEqual(self.expose(7)['ErrorNumber'], 0)
        self.assertEqual(self.expose(8)['ErrorNumber'], 0x40B)
        self.assertEqual(self.request('connected', 'PUT', {'ClientID': 8, 'Connected': 'false'})[1]['ErrorNumber'], 0x40B)
        self.assertEqual(self.request('abortexposure', 'PUT', {'ClientID': 8})[1]['ErrorNumber'], 0x40B)
        with self.assertRaises(AlpacaError):
            self.service.execute(0, 'PUT', 'gain', {'gain': '9'}, 7, '127.0.0.2')
        owner = self.service.inventory()[0]['owner']
        self.assertEqual({key:owner[key] for key in ('client_id','address','since')}, {'client_id': 7, 'address': '127.0.0.1', 'since': 0.0})
        self.assertRegex(owner['generation'], r'^[0-9a-f]{32}$')
        self.now = 1
        self.service.inventory()
        self.now = 120.99
        self.assertEqual(self.expose(8)['ErrorNumber'], 0x40B)
        self.now = 121
        self.assertEqual(self.expose(8)['ErrorNumber'], 0)
        self.service.release('simulation')
        self.assertNotIn('owner', self.service.inventory()[0])
        self.assertFalse(self.camera._opened)

    def test_discovery_real_packet_and_rate_limit(self):
        self.service.set_listening(11111, ['127.0.0.1'])
        discovery = Discovery(self.access, self.service, clock=lambda: self.now, port=0)
        discovery.start()
        self.addCleanup(discovery.close)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.settimeout(2)
        sock.sendto(b'alpacadiscovery1', discovery.socket.getsockname())
        data, _ = sock.recvfrom(512)
        self.assertEqual(json.loads(data), {'AlpacaPort': 11111})
        self.assertIsNone(discovery.response(b'alpacadiscovery1', '127.0.0.1'))
        self.now = 1
        self.assertIsNotNone(discovery.response(b'alpacadiscovery1', '127.0.0.1'))
        self.assertIsNone(discovery.response(b'alpacadiscovery1', '192.0.2.2'))
        self.assertIsNone(discovery.response(b'ALPACADISCOVERY1', '127.0.0.2'))
        self.service.update_settings({'serve': False})
        self.assertIsNone(discovery.response(b'alpacadiscovery1', '127.0.0.2'))

    def test_persistence_and_single_worker(self):
        self.connect()
        caller_threads = []
        original = self.camera.state
        def state():
            caller_threads.append(threading.get_ident())
            return original()
        self.camera.state = state
        workers = [threading.Thread(target=self.service.inventory) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(set(caller_threads), {self.service._worker.ident})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cameras.json'
            service = CameraService(path=path)
            service.update_settings({'serve': False, 'port': 12000})
            service.close()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            service = CameraService(path=path)
            try:
                self.assertFalse(service.get_settings()['serve'])
                self.assertEqual(service.get_settings()['port'], 12000)
            finally:
                service.close()

    def test_imagebytes_errors_and_duplicate_host(self):
        _, data = self.request('imagearray', accept='application/imagebytes')
        fields = struct.unpack('<11I', data[:44])
        self.assertEqual(fields[1], 0x407)
        self.assertEqual(fields[4], 44)
        self.assertEqual(data[44:].decode(), 'Connect the camera first.')
        self.assertFalse(self.access.host(['localhost:11111', 'localhost:11111'], 11111))

    def test_broadcast_socket_is_bound_to_only_management_interface(self):
        access = ManagementAccess('192.0.2.1', '192.0.2.0/24', [], interface='wlan1')
        discovery = Discovery(access, self.service)
        fake = MagicMock()
        with patch('wifi_bridge.cameras.listener.socket.socket', return_value=fake), patch('wifi_bridge.cameras.listener.sys.platform', 'linux'), patch('wifi_bridge.cameras.listener.threading.Thread'):
            discovery.start()
        fake.setsockopt.assert_any_call(socket.SOL_SOCKET, getattr(socket, 'SO_BINDTODEVICE', 25), b'wlan1\0')
        fake.bind.assert_called_once_with(('', 32227))
        discovery.close()

    def test_blocked_driver_bounds_calls_metadata_and_shutdown_without_replacement(self):
        service = CameraService(request_timeout=0.08, shutdown_timeout=0.04)
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        self.addCleanup(service.close)
        driver = SimulatedCamera(width=4, height=4)
        service.add('blocked', 'simulator', driver)
        service.execute(0, 'PUT', 'connected', {'connected':'true'}, 1, '127.0.0.1')
        original = driver.state
        def blocked():
            entered.set()
            gate.wait(2)
            return original()
        driver.state = blocked
        failures = []
        def inventory():
            try:
                service.inventory()
            except AlpacaError as error:
                failures.append(error.number)
        caller = threading.Thread(target=inventory)
        caller.start()
        self.assertTrue(entered.wait(1))
        mutated = []
        started = time.monotonic()
        with self.assertRaises(AlpacaError):
            service._call(lambda: mutated.append(True))
        caller.join(0.3)
        self.assertFalse(caller.is_alive())
        self.assertEqual(failures, [0x501])
        self.assertLess(time.monotonic() - started, 0.4)
        started = time.monotonic()
        self.assertEqual(service.get_settings()['port'], 11111)
        self.assertTrue(service.enabled())
        self.assertLess(time.monotonic() - started, 0.04)
        worker = service._worker
        gate.set()
        caller.join(1)
        deadline = time.monotonic() + 1
        while service._stalled.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(service.inventory()[0]['state'], 'idle')
        self.assertEqual(mutated, [])  # Timed-out queued mutation was cancelled.
        self.assertIs(service._worker, worker)
        gate.clear(); entered.clear()
        caller = threading.Thread(target=inventory)
        caller.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        service.close()
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertFalse(service.enabled())
        self.assertTrue(worker.is_alive())
        gate.set()
        caller.join(1); worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(driver._opened)
