"""Camera listener lifecycle without physical interface/device changes."""
import threading
import unittest

from wifi_bridge.cameras.runtime import CameraRuntime
from wifi_bridge.cameras.service import CameraService
from wifi_bridge.cameras.simulator import SimulatedCamera
from wifi_bridge.cameras.listener import Discovery, ManagementAccess


class Listener:
    def __init__(self, access, service, port):
        self.address, self.server_port = access.address, port
        self.stop = threading.Event()
        self.closed = False

    def serve_forever(self, poll_interval):
        self.stop.wait(2)

    def shutdown(self):
        self.stop.set()

    def server_close(self):
        self.closed = True


class Responder:
    def __init__(self, access, service):
        self.closed = False

    def start(self):
        pass

    def close(self):
        self.closed = True


class CameraRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.service = CameraService()
        self.addCleanup(self.service.close)
        self.plan = {'paths':[{'role':'home','ip':'192.0.2.1','subnet':'192.0.2.0/24','interface':'wlan1'}],
                     'hosts':['bridge.local']}
        self.now = 1
        self.runtime = CameraRuntime(self.service,lambda:self.plan,server_factory=Listener,
                                     discovery_factory=Responder,clock=lambda:self.now)
        self.addCleanup(self.runtime.close)

    def register(self):
        self.service.add('simulation','simulator',SimulatedCamera(width=4,height=4))

    def test_no_detection_no_listener_and_successful_path_lifecycle(self):
        self.runtime.sync()
        self.assertEqual(self.runtime.listeners,{})
        self.register()
        self.assertIsNone(self.service.inventory()[0]['alpaca'])
        self.runtime.sync()
        self.assertEqual(self.service.inventory()[0]['alpaca'],{'port':11111,'device_number':0})
        server,thread,discovery=next(iter(self.runtime.listeners.values()))
        self.runtime.sync()
        self.assertIs(next(iter(self.runtime.listeners.values()))[0],server)
        self.plan['paths']=[]
        self.runtime.sync()
        self.assertTrue(server.closed)
        self.assertTrue(discovery.closed)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.service.inventory()[0]['alpaca'])

    def test_disable_and_configured_port_never_advertise_an_unbound_endpoint(self):
        self.register();self.runtime.sync()
        access=ManagementAccess('192.0.2.1','192.0.2.0/24',[])
        discovery=Discovery(access,self.service,clock=lambda:self.now)
        self.service.update_settings({'port':12000})
        self.assertEqual(discovery.response(b'alpacadiscovery1','192.0.2.2'),b'{"AlpacaPort":11111}')
        self.runtime.sync();self.now+=1
        self.assertEqual(discovery.response(b'alpacadiscovery1','192.0.2.2'),b'{"AlpacaPort":12000}')
        self.service.update_settings({'serve':False})
        self.assertIsNone(discovery.response(b'alpacadiscovery1','192.0.2.2'))
        self.runtime.sync()
        self.assertEqual(self.runtime.listeners,{})

    def test_failed_tcp_or_udp_bind_is_not_advertised_and_retries_are_bounded(self):
        self.register()
        attempts=[]
        def failed(*args,**kwargs):
            attempts.append(True)
            raise OSError('synthetic bind failure')
        self.runtime.server_factory=failed
        self.runtime.sync();self.runtime.sync()
        self.assertEqual(len(attempts),1)
        self.assertIsNone(self.service.listening_port())
        self.now+=5;self.runtime.sync()
        self.assertEqual(len(attempts),2)
        self.runtime.server_factory=Listener
        self.runtime.discovery_factory=failed
        self.now+=5;self.runtime.sync()
        self.assertIsNone(self.service.listening_port())
        self.assertEqual(self.runtime.listeners,{})
        self.runtime.discovery_factory=Responder
        self.now+=5;self.runtime.sync()
        self.assertEqual(self.service.listening_port(),11111)

    def test_home_and_hotspot_paths_share_service_and_published_port(self):
        self.register()
        self.plan['paths'].append({'role':'ap','ip':'198.51.100.1','subnet':'198.51.100.0/24','interface':'wlan2'})
        self.runtime.sync()
        self.assertEqual(len(self.runtime.listeners), 2)
        self.assertEqual(self.service.listening_port(), 11111)
        self.plan['paths'] = self.plan['paths'][1:]
        self.runtime.sync()
        self.assertEqual(len(self.runtime.listeners), 1)
        self.assertEqual(self.service.listening_port(), 11111)

    def test_only_confirmed_management_roles_and_addresses(self):
        for path in ({'role':'instrument','ip':'192.0.2.1','subnet':'192.0.2.0/24'},
                     {'role':'home','ip':'0.0.0.0','subnet':'0.0.0.0/0'},
                     {'role':'home','ip':'192.0.2.1','subnet':'198.51.100.0/24'}):
            with self.subTest(path=path):
                self.assertEqual(CameraRuntime.plans({'paths':[path]},11111),{})
