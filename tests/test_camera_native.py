"""Native HTTP + loopback Alpaca, injected drivers and temporary sysfs only."""
import copy
import http.client
import json
from pathlib import Path
import threading
from unittest.mock import Mock, patch

from test_bridge import Fixture
from helpers.contract_schema import validate
from wifi_bridge.cameras.discovery import CameraDiscovery
from wifi_bridge.cameras.driver import CameraError
from wifi_bridge.cameras.listener import AlpacaServer, ManagementAccess
from wifi_bridge.cameras.registry import CameraRegistry, load_registry
from wifi_bridge.cameras.simulator import SimulatedCamera
from wifi_bridge.cameras.usb import SysfsUSB
from wifi_bridge.web import WebServer


class CameraNativeHTTPTests(Fixture):
    from test_api import APITests as _HTTP
    request, login = _HTTP.request, _HTTP.login
    del _HTTP

    def setUp(self):
        super().setUp()
        self.configured(); self.controller.cycle(0)
        self.server = WebServer(('127.0.0.1', 0), self.controller)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.token = ''
        self.login()
        self.service = self.server.api.cameras.service()
        self.addCleanup(self.service.close)
        self.drivers = []
        def factory(**options):
            driver = SimulatedCamera(width=4, height=4)
            self.drivers.append(driver)
            return driver
        self.registry = CameraRegistry(list(load_registry().descriptors()), {'uvc': factory})
        self.root = Path(self.tmp.name) / 'sysfs'
        self.root.mkdir()
        self.source = SysfsUSB(self.root)
        self.now = 0
        monitor = Mock(); monitor.changed.return_value = False
        self.discovery = CameraDiscovery(self.service, self.registry, supported=True, source=self.source,
                                         monitor=monitor, clock=lambda: self.now,
                                         on_event=self.controller.camera_native.event)
        self.addCleanup(self.discovery.close)
        self.alpaca = AlpacaServer(ManagementAccess('127.0.0.1', '127.0.0.0/8', ['127.0.0.1']), self.service, port=0)
        threading.Thread(target=self.alpaca.serve_forever, daemon=True).start()
        self.addCleanup(self.alpaca.server_close); self.addCleanup(self.alpaca.shutdown)
        self.service.set_listening(self.alpaca.server_port, ['127.0.0.1'])

    def attach(self, port='1-1'):
        path = self.root / port
        path.mkdir()
        for key, text in {'idVendor':'1234','idProduct':'5678','bDeviceClass':'0e','speed':'480'}.items():
            (path / key).write_text(text)
        self.now += 5
        self.discovery.sync()
        return path

    def camera(self):
        return next(row for row in self.request('GET', '/api/v1/instruments')[1]['instruments'] if row['kind'] == 'camera')

    def connect(self, ident):
        revision = self.server.api.revision()
        return self.request('POST', '/api/v1/instruments/' + ident + '/connect', {'lease_seconds':900}, {'If-Match':'"'+revision+'"'})

    def alpaca_call(self, method, member, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.alpaca.server_port)
        connection.request(method, '/api/v1/camera/0/' + member, body, {'Content-Type':'application/x-www-form-urlencoded'})
        response = connection.getresponse(); value = json.loads(response.read()); connection.close()
        self.assertEqual(value['ErrorNumber'], 0, value)
        return value

    def test_catalog_connect_bound_endpoint_lease_and_independent_alpaca_ownership(self):
        self.attach()
        camera = self.camera(); ident = camera['id']
        self.assertEqual((camera['link'], camera['service']), ('connected', 'ready'))
        before = copy.deepcopy(self.store.config)
        self.alpaca_call('PUT', 'connected', 'Connected=true&ClientID=123')
        self.alpaca_call('PUT', 'startexposure', 'Duration=60&Light=true&ClientID=123')
        with patch.object(self.controller.operations, 'ensure', side_effect=AssertionError('no Wi-Fi switch')):
            code, ready, _ = self.connect(ident)
        self.assertEqual(code, 200)
        spec = self.server.api.specification()
        validate(ready, spec['components']['schemas']['NativeReady'], spec)
        endpoint = ready['endpoint']
        self.assertEqual(endpoint['mode'], 'relay')
        self.assertEqual(endpoint['target_ip'], '127.0.0.1')
        self.assertEqual(endpoint['primary']['ports'], [{'port':self.alpaca.server_port,'transport':'tcp','service':'alpaca_camera','role':'control','protocol':'alpaca','device_number':0}])
        self.assertEqual(self.request('GET', '/api/v1/instruments/'+ident+'/endpoint')[1], endpoint)
        lease = ready['lease']['id']; base = '/api/v1/instruments/' + ident
        foreign = self.request('POST', '/api/v1/auth/token', {'password':'local-admin-test-password'})[1]['access_token']
        foreign_headers = {'Authorization':'Bearer '+foreign}
        self.assertEqual(self.request('POST', base+'/release', {'lease_id':lease}, foreign_headers)[0], 403)
        self.assertEqual(self.request('POST', base+'/disconnect', {'lease_id':lease}, foreign_headers)[0], 403)
        self.assertEqual(self.request('POST', base+'/lease', {'lease_id':lease,'lease_seconds':60})[1]['expires_in'], 60)
        owner = self.service.inventory()[0]['owner']
        with patch.object(self.drivers[0], 'abort', side_effect=AssertionError('no abort')), \
             patch.object(self.drivers[0], 'close', side_effect=AssertionError('no close')):
            self.assertEqual(self.request('POST', base+'/disconnect', {'lease_id':lease})[:2], (200, {'ok':True}))
            self.assertEqual(self.service.inventory()[0]['owner'], owner)
            self.assertEqual(self.service.inventory()[0]['state'], 'exposing')
        self.assertEqual(self.store.config, before)
        self.assertFalse(self.controller.protection.leases)
        # No camera scope is needed to discover/reserve the generic endpoint.
        self.server.sessions[self.token]['scopes'] = ['read','connect']
        self.assertEqual(self.connect(ident)[0], 200)
        self.assertEqual(self.request('GET', '/api/v1/cameras')[0], 403)

    def test_unplug_invalidates_endpoint_reservation_and_emits_camera_events(self):
        path = self.attach(); ident = self.camera()['id']
        self.assertEqual(self.connect(ident)[0], 200)
        for entry in path.iterdir(): entry.unlink()
        path.rmdir(); self.now += 5; self.discovery.sync()
        self.assertFalse(self.controller.protection.leases)
        self.assertEqual(self.request('GET', '/api/v1/instruments/'+ident+'/endpoint')[0], 404)
        kinds = {e['type'] for e in self.controller.event_store.events}
        self.assertTrue({'camera.detected','camera.removed','camera_detected','camera_removed','lease.ended'} <= kinds)
        self.assertEqual(self.discovery.callback_errors, 0)
        page = self.request('GET', '/api/v1/events')[1]
        spec = self.server.api.specification()
        validate(page, spec['components']['schemas']['NativeEvents'], spec)

    def test_missing_listener_different_ingress_and_missing_driver_never_connect(self):
        self.attach(); ident = self.camera()['id']
        self.service.set_listening(self.alpaca.server_port, ['192.0.2.1'])
        self.assertEqual(self.connect(ident)[0], 409)
        self.assertEqual(self.request('GET','/api/v1/cameras')[1][0]['alpaca'], None)
        self.assertFalse(self.controller.protection.leases)
        self.service.set_listening(None)
        self.assertEqual(self.connect(ident)[0], 409)
        self.service.reconcile_usb([], self.registry)
        missing = CameraRegistry(list(load_registry().descriptors()),
                                 {'uvc': Mock(side_effect=CameraError('camera_driver_unavailable'))})
        self.service.reconcile_usb(self.source.scan(), missing)
        self.assertEqual(self.camera()['link'], 'available')
        self.assertEqual(self.connect(ident)[0], 409)

    def test_24_profiles_plus_8_cameras_real_pages_change_and_collision_policy(self):
        profile = dict(self.store.config['telescopes'][0])
        self.store.config['telescopes'] = [dict(profile,id='profile-'+str(i),name='Profile '+str(i),enabled=False) for i in range(24)]
        for index in range(8): self.attach('1-'+str(index+1))
        first = self.request('GET','/api/v1/instruments')[1]
        self.assertEqual(len(first['instruments']),24)
        self.assertTrue(all(row['kind'] != 'camera' for row in first['instruments']))
        cursor = first['next_cursor']
        second = self.request('GET','/api/v1/instruments?after='+cursor)[1]
        self.assertEqual(len(second['instruments']),8)
        self.assertTrue(all(row['kind'] == 'camera' for row in second['instruments']))
        self.assertFalse(second['has_more'])
        for result in (first,second):
            spec = self.server.api.specification()
            validate(result,spec['components']['schemas']['NativeInstruments'],spec)
        # Preserve an intentionally colliding saved identity and alias the camera.
        camera_id = second['instruments'][0]['id']
        self.assertEqual(self.connect(camera_id)[0],200)
        self.store.config['telescopes'][0]['id'] = camera_id
        all_rows = self.controller.instrument_catalog(self.controller.status())
        self.assertEqual(len({r['id'] for r in all_rows}),32)
        self.assertEqual(next(r for r in all_rows if r['id']==camera_id)['kind'], 'smart_telescope')
        self.assertEqual(len([r for r in all_rows if r['kind']=='camera']),8)
        self.assertFalse(any(v.get('camera') and v['profile_id']==camera_id for v in self.controller.protection.leases.values()))
        alias = next(row['instrument_id'] for row in self.server.api.camera_inventory() if row['id']==camera_id)
        with patch.object(self.controller.operations,'ensure',side_effect=AssertionError('camera never switches Wi-Fi')):
            self.assertEqual(self.connect(alias)[0],200)
        with patch.object(self.controller.camera_native,'connect',side_effect=AssertionError('saved profile must keep its route')):
            self.assertEqual(self.request('GET','/api/v1/instruments/'+camera_id)[1]['kind'],'smart_telescope')
        self.assertEqual(self.request('GET','/api/v1/instruments?after='+cursor)[0],409)
        # Full-capacity replacement must deliver removal, even with 32 current IDs.
        for event in self.service.reconcile_usb([dict(row,product_id=1) for row in self.source.scan()], self.registry):
            self.controller.camera_native.event(event)
        self.assertTrue(any(e['type']=='camera.removed' for e in self.controller.event_store.events))
        for suffix in ('after=', 'after=x', 'after=x&after=y', 'extra=1'):
            self.assertEqual(self.request('GET','/api/v1/instruments?'+suffix)[0],400)

    def test_native_event_tick_reuses_one_status_and_one_camera_snapshot(self):
        self.attach()
        profile = dict(self.store.config['telescopes'][0])
        live = dict(self.controller.state['telescopes'][0])
        self.store.config['telescopes'] = [dict(profile,id='profile-'+str(i)) for i in range(24)]
        self.controller.state['telescopes'] = [dict(live,id='profile-'+str(i),connected=True) for i in range(24)]
        with patch.object(self.controller,'status',wraps=self.controller.status) as status, \
             patch.object(self.service,'inventory',wraps=self.service.inventory) as inventory:
            self.controller._native_event_snapshot()
        self.assertEqual(status.call_count,1)
        self.assertEqual(inventory.call_count,1)

    def test_camera_state_survives_controller_pruning_and_keeps_debounce(self):
        self.attach(); ident = self.camera()['id']
        self.controller.cycle(1)
        self.controller.cycle(2)
        states = [e for e in self.controller.event_store.events if e['type']=='instrument.state' and e['instrument_id']==ident]
        self.assertEqual(len(states),1)
        self.assertIn(ident,self.controller.connection_events.states)

    def test_native_power_warning_and_lease_expiry_use_existing_bounded_journal(self):
        path=self.attach();ident=self.camera()['id']
        clock=[100.0];self.controller.protection.clock=lambda:clock[0]
        ready=self.connect(ident)[1]
        clock[0]+=901
        self.controller.protection.prune()
        self.assertFalse(self.controller.protection.leases)
        self.assertTrue(any(e['type']=='lease.ended' and e.get('reason')=='EXPIRED' for e in self.controller.event_store.events))
        for _ in range(3):
            for item in path.iterdir():item.unlink()
            path.rmdir();self.now+=5;self.discovery.sync()
            path=self.attach()
        self.assertTrue(self.request('GET','/api/v1/cameras')[1][0]['discovery']['power_unstable'])
        kinds=[e['type'] for e in self.controller.event_store.events]
        self.assertEqual(kinds.count('camera.power_unstable'),1)
        self.assertEqual(kinds.count('camera_power_unstable'),1)

    def test_callback_failure_retries_without_terminating_discovery(self):
        original = self.discovery.on_event
        self.discovery.on_event = Mock(side_effect=[RuntimeError('synthetic failure'), None])
        self.attach()
        self.assertEqual(self.discovery.callback_errors,1)
        self.assertEqual(len(self.discovery.pending_events),1)
        self.now += .25; self.discovery.sync()
        self.assertEqual(len(self.discovery.pending_events),0)
        self.discovery.on_event = original

    def test_zero_tier_lazy_api_never_creates_worker_or_usb_monitor(self):
        self.service.close(); del self.controller.camera_service
        self.controller.camera_supported = False
        with patch('wifi_bridge.cameras.api.CameraService', side_effect=AssertionError('no worker')), \
             patch('wifi_bridge.cameras.usb.UEventMonitor.start', side_effect=AssertionError('no monitor')):
            self.assertIsNone(self.server.api.cameras.service())
            self.assertEqual(self.request('GET','/api/v1/cameras')[:2],(200,[]))
            status = self.request('GET','/api/v1/cameras/settings')[1]
            self.assertEqual(self.request('PATCH','/api/v1/cameras/settings',{'serve':False},
                                          {'If-Match':'"'+status['config_revision']+'"'})[0],200)
            self.assertFalse(self.request('GET','/api/v1/cameras/settings')[1]['settings']['serve'])
