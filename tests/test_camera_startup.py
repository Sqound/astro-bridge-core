"""Signal-context shutdown stays lock-free; cleanup runs in normal finally."""
from contextlib import ExitStack
import signal
from unittest.mock import Mock, patch
import io

from test_bridge import Fixture


class CameraStartupTests(Fixture):
    def test_platform_gate_decides_zero_and_pi_tier_before_workers(self):
        from wifi_bridge.cameras.discovery import platform_supported
        with patch('wifi_bridge.cameras.discovery.platform.system',return_value='Linux'):
            for model, expected in ((b'Raspberry Pi Zero W\0',False),(b'Raspberry Pi Zero 2 W\0',False),
                                    (b'Raspberry Pi 3 Model B\0',True),(b'Raspberry Pi 4 Model B\0',True)):
                with patch('wifi_bridge.cameras.discovery.Path.open',return_value=io.BytesIO(model)):
                    self.assertEqual(platform_supported(),expected)
                with patch('wifi_bridge.cameras.discovery.Path.open',return_value=io.BytesIO(model)):
                    self.assertEqual(platform_supported(indi=True),model.startswith(b'Raspberry Pi 4 '))
            with patch('wifi_bridge.cameras.discovery.Path.open',side_effect=PermissionError()):
                self.assertFalse(platform_supported())
            for machine,expected in (('armv6l',False),('armv7l',False),('aarch64',False),('x86_64',True)):
                with patch('wifi_bridge.cameras.discovery.Path.open',side_effect=FileNotFoundError()), \
                     patch('wifi_bridge.cameras.discovery.platform.machine',return_value=machine):
                    self.assertEqual(platform_supported(),expected)
        with patch('wifi_bridge.cameras.discovery.Path.open',side_effect=AssertionError('demo metadata read')):
            self.assertTrue(platform_supported(demo=True))
            self.assertFalse(platform_supported(demo=True, indi=True))

    def test_pi5_marker_is_required_and_indi_remains_disabled(self):
        from wifi_bridge.cameras.discovery import camera_tier_allowed, pi5_test_enabled
        from pathlib import Path
        import os, stat, tempfile
        model = 'Raspberry Pi 5 Model B Rev 1.0'
        self.assertFalse(camera_tier_allowed('Linux', model))
        self.assertTrue(camera_tier_allowed('Linux', model, pi5_test=True))
        self.assertFalse(camera_tier_allowed('Linux', model, pi5_test=True, indi=True))
        self.assertFalse(camera_tier_allowed('Linux', 'Raspberry Pi Zero W', pi5_test=True))
        original = Path.lstat
        def trusted(path):
            fields = list(original(Path('/'))); fields[4] = 0
            fields[0] = stat.S_IFDIR | 0o755
            return os.stat_result(fields)
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / 'marker'; marker.touch(mode=0o644)
            def fake(path):

                if str(path) == '/etc/wifi-bridge/pi5-asi220-test':
                    fields = list(original(marker)); fields[4] = 0; return os.stat_result(fields)
                return trusted(path)
            with patch.object(Path, 'lstat', fake):
                self.assertTrue(pi5_test_enabled())
                marker.chmod(0o666)
                self.assertFalse(pi5_test_enabled())
                marker.unlink(); marker.symlink_to('/etc/passwd')
                self.assertFalse(pi5_test_enabled())

    def test_zero_startup_skips_service_listener_and_usb_coordinator_creation(self):
        from wifi_bridge import __main__ as entry
        server, discovery = Mock(), Mock()
        self.controller.camera_supported = False
        with ExitStack() as stack:
            for name,value in {'LinuxNetwork':self.net,'Store':self.store,'Controller':self.controller,
                               'WebServer':server,'SeestarDiscovery':discovery}.items():
                stack.enter_context(patch.object(entry,name,return_value=value))
            stack.enter_context(patch.object(entry.os,'geteuid',return_value=0))
            stack.enter_context(patch.object(entry.Path,'exists',return_value=True))
            stack.enter_context(patch.object(entry.Path,'is_file',return_value=False))
            stack.enter_context(patch.object(entry.Path,'read_text',return_value='{"password":"synthetic-bootstrap"}'))
            stack.enter_context(patch.object(entry,'load_registry',return_value=Mock()))
            stack.enter_context(patch.object(entry.signal,'signal'))
            stack.enter_context(patch('sys.argv',['wifi_bridge','--state-dir',self.tmp.name]))
            stack.enter_context(patch.object(entry.threading,'Thread'))
            stack.enter_context(patch('builtins.print'))
            runtime=stack.enter_context(patch.object(entry,'CameraRuntime',side_effect=AssertionError('no listener')))
            usb=stack.enter_context(patch.object(entry,'CameraDiscovery',side_effect=AssertionError('no USB')))
            entry.main()
            runtime.assert_not_called();usb.assert_not_called()
        server.api.cameras.service.assert_not_called()

    def test_signal_callback_only_requests_shutdown_and_cleanup_stays_ordered(self):
        from wifi_bridge import __main__ as entry
        from wifi_bridge.cameras.runtime import CameraRuntime
        server, discovery = Mock(), Mock()
        callbacks, order = {}, []
        camera_service = server.api.cameras.service.return_value
        camera_service.close.side_effect = lambda: order.append('camera-service')
        def runtime_close(runtime):
            order.append('camera-listeners')
        def serve(**kwargs):
            # Execute the actual registered signal handler as if it interrupted
            # service_actions. No OS signal, appliance, USB or socket is involved.
            callbacks[signal.SIGTERM](signal.SIGTERM, None)
            self.assertTrue(self.controller.stop.is_set())
            self.assertEqual(order, [])
            self.assertEqual(threads.call_args_list[-1].kwargs['target'], server.shutdown)
        server.serve_forever.side_effect = serve
        server.server_close.side_effect = lambda: order.append('web-close')
        with ExitStack() as stack:
            for name,value in {'LinuxNetwork':self.net,'Store':self.store,'Controller':self.controller,
                               'WebServer':server,'SeestarDiscovery':discovery}.items():
                stack.enter_context(patch.object(entry,name,return_value=value))
            stack.enter_context(patch.object(entry.os,'geteuid',return_value=0))
            stack.enter_context(patch.object(entry.Path,'exists',return_value=True))
            stack.enter_context(patch.object(entry.Path,'is_file',return_value=False))
            stack.enter_context(patch.object(entry.Path,'read_text',return_value='{"password":"synthetic-bootstrap"}'))
            stack.enter_context(patch.object(entry,'load_registry',return_value=Mock()))
            stack.enter_context(patch.object(entry.signal,'signal',side_effect=lambda sig,fn:callbacks.update({sig:fn})))
            stack.enter_context(patch('sys.argv',['wifi_bridge','--state-dir',self.tmp.name]))
            threads=stack.enter_context(patch.object(entry.threading,'Thread'))
            sockets=stack.enter_context(patch('socket.socket'))
            stack.enter_context(patch('builtins.print'))
            stack.enter_context(patch.object(CameraRuntime,'close',runtime_close))
            for name in ('link_collector','vespera_collector','telemetry_sessions'):
                stack.enter_context(patch.object(getattr(self.controller,name),'close',side_effect=lambda name=name:order.append(name)))
            entry.main()
            sockets.assert_not_called()
        self.assertEqual(order[:3],['camera-listeners','camera-service','web-close'])
        self.assertEqual(order[3:],['link_collector','vespera_collector','telemetry_sessions'])
