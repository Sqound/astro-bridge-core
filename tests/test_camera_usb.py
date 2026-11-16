"""Temporary USB trees/fake netlink only; never enumerate the host's USB bus."""
from collections import deque
import errno
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from wifi_bridge.cameras.discovery import CameraDiscovery, camera_tier_allowed
from wifi_bridge.cameras.driver import CameraError
from wifi_bridge.cameras.registry import CameraRegistry, load_registry
from wifi_bridge.cameras.service import CameraService
from wifi_bridge.cameras.simulator import SimulatedCamera
from wifi_bridge.cameras.usb import MAX_ENTRIES, SysfsUSB, UEventMonitor


class USBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 0
        self.source = SysfsUSB(self.root)
        # This suite exercises missing-driver discovery; never let the installed
        # UVC factory resolve synthetic attachment metadata against host sysfs.
        no_driver = patch('wifi_bridge.cameras.registry._uvc',
                          side_effect=CameraError('camera_driver_unavailable'))
        no_driver.start()
        self.addCleanup(no_driver.stop)

    def device(self, port='1-1.3', vendor='1234', product='5678', device_class='00', interfaces=('0e', '0e')):
        path = self.root / port
        path.mkdir()
        for key, value in {'idVendor': vendor, 'idProduct': product, 'bDeviceClass': device_class, 'speed': '480'}.items():
            (path / key).write_text(value + '\n')
        for index, value in enumerate(interfaces):
            interface = self.root / (port + ':1.' + str(index))
            interface.mkdir()
            (interface / 'bInterfaceClass').write_text(value)
        return path

    def service(self):
        service = CameraService(clock=lambda: self.now)
        self.addCleanup(service.close)
        return service

    def test_hub_two_wifi_dongles_and_camera_only_match_camera(self):
        self.device('1-1', device_class='09', interfaces=('09',))
        self.device('1-1.1', vendor='0bda', product='c811', interfaces=('ff',))
        self.device('1-1.2', vendor='0bda', product='c820', interfaces=('e0', '0a'))
        self.device()
        self.device('1-1.4', interfaces=('ff',))  # Unknown non-camera ignored.
        rows = self.source.scan()
        self.assertEqual(len(rows), 5)
        camera = next(row for row in rows if row['port_path'] == '1-1.3')
        self.assertEqual(camera, dict(vendor_id=0x1234, product_id=0x5678, device_class=0,
                                     interface_classes=[14], speed_mbps=480.0, bus=1, port_path='1-1.3'))
        service = self.service()
        events = service.reconcile_usb(rows, load_registry())
        self.assertEqual([event['type'] for event in events], ['camera_detected'])
        inventory = service.inventory()
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]['discovery'], {'state': 'driver_missing', 'power_unstable': False, 'disconnects': 0})
        self.assertIsNone(inventory[0]['alpaca'])
        self.assertEqual(inventory[0]['last_image'], {'bytes': 0, 'seconds': 0.0})
        self.assertEqual(service.configured_devices(), [])

    def test_network_composite_is_never_class_matched_as_camera(self):
        self.device(interfaces=('0e', 'e0'))
        self.assertIsNone(load_registry().match_device(self.source.scan()[0]))

    def test_replug_power_warning_at_three_losses_and_history_expires(self):
        self.device()
        rows, service, registry = self.source.scan(), self.service(), load_registry()
        service.reconcile_usb(rows, registry)
        ident = service.inventory()[0]['id']
        for index in range(3):
            self.now += 20
            events = service.reconcile_usb([], registry)
            self.assertEqual(events[0], {'type': 'camera_removed', 'camera_id': ident})
            self.assertEqual(len(events), 2 if index == 2 else 1)
            service.reconcile_usb(rows, registry)
        self.assertTrue(service.inventory()[0]['discovery']['power_unstable'])
        self.now += 601
        self.assertEqual(service.reconcile_usb(rows, registry), [])
        self.assertEqual(service.inventory()[0]['discovery']['disconnects'], 0)
        self.assertFalse(service.inventory()[0]['discovery']['power_unstable'])

    def test_uncertain_inventory_does_not_remove_devices(self):
        path = self.device()
        service, registry = self.service(), load_registry()
        service.reconcile_usb(self.source.scan(), registry)
        (path / 'idVendor').write_text('x' * 65)
        self.assertIsNone(self.source.scan())
        self.assertEqual(service.reconcile_usb(self.source.scan(), registry), [])
        self.assertEqual(len(service.inventory()), 1)
        (path / 'idVendor').write_text('1234')
        for speed in ('nan', 'inf', '-1', '100001'):
            (path / 'speed').write_text(speed)
            self.assertIsNone(self.source.scan())
        missing = SysfsUSB(self.root / 'absent')
        self.assertIsNone(missing.scan())

    def test_entry_bound_and_nonmetadata_are_ignored(self):
        self.device()
        (self.root / '1-1.3' / 'serial').write_text('unread-metadata')
        self.assertNotIn('serial', str(self.source.scan()))
        for index in range(MAX_ENTRIES):
            (self.root / ('ignored-' + str(index))).mkdir()
        self.assertIsNone(self.source.scan())

    def test_factories_and_close_stay_on_one_worker_and_numbers_never_retarget(self):
        self.device()
        self.device('1-1.4')
        service, rows = self.service(), self.source.scan()
        calls = []
        class Driver(SimulatedCamera):
            def info(driver):
                calls.append(('info', threading.current_thread().name))
                return super().info()
            def close(driver):
                calls.append(('close', threading.current_thread().name))
                return super().close()
        def factory(**options):
            calls.append(('factory', threading.current_thread().name))
            return Driver(width=2, height=2)
        registry = CameraRegistry(list(load_registry().descriptors()), {'uvc': factory})
        service.reconcile_usb(rows, registry)
        service.set_listening(11111)
        devices = service.inventory()
        self.assertEqual([row['discovery']['state'] for row in devices], ['ready', 'ready'])
        self.assertEqual([row['alpaca']['device_number'] for row in devices], [0, 1])
        service.execute(0, 'PUT', 'connected', {'connected': 'true'}, 1, '192.0.2.2')
        service.reconcile_usb(rows[1:], registry)
        self.assertEqual(service.inventory()[0]['alpaca']['device_number'], 1)
        with self.assertRaises(Exception):
            service.execute(0, 'GET', 'name', {}, 1, '192.0.2.2')
        self.assertEqual(service.configured_devices()[0]['DeviceNumber'], 1)
        self.assertIn(('close', 'camera-service'), calls)
        self.assertTrue(all(thread == 'camera-service' for _, thread in calls))

    def test_unsupported_driver_failure_never_exposes_alpaca(self):
        self.device()
        service = self.service()
        def factory(**options):
            raise CameraError('camera_unsupported')
        registry = CameraRegistry(list(load_registry().descriptors()), {'uvc': factory})
        service.reconcile_usb(self.source.scan(), registry)
        self.assertEqual(service.inventory()[0]['discovery']['state'], 'unsupported')
        self.assertFalse(service.enabled())

    def test_replug_reuses_bounded_number_and_ninth_distinct_key_fails_closed(self):
        self.device()
        service, base = self.service(), self.source.scan()[0]
        registry = CameraRegistry(list(load_registry().descriptors()),
                                  {'uvc': lambda **options: SimulatedCamera(width=2, height=2)})
        service.set_listening(11111)
        for _ in range(12):
            service.reconcile_usb([base], registry)
            self.assertEqual(service.inventory()[0]['alpaca']['device_number'], 0)
            service.reconcile_usb([], registry)
        for number in range(1, 8):
            row = dict(base, port_path='1-' + str(number + 1))
            service.reconcile_usb([row], registry)
            self.assertEqual(service.inventory()[0]['alpaca']['device_number'], number)
            service.reconcile_usb([], registry)
        service.reconcile_usb([dict(base, port_path='1-9')], registry)
        self.assertEqual(service.inventory()[0]['discovery']['state'], 'unsupported')
        self.assertIsNone(service.inventory()[0]['alpaca'])
        self.assertEqual(service.configured_devices(), [])
        for number in range(8):
            with self.assertRaises(Exception):
                service.execute(number, 'GET', 'name', {}, 1, '192.0.2.2')
        service.reconcile_usb([base], registry)
        self.assertEqual(service.inventory()[0]['alpaca']['device_number'], 0)
        restarted = self.service()
        restarted.set_listening(11111)
        restarted.reconcile_usb([dict(base, port_path='1-9')], registry)
        self.assertEqual(restarted.inventory()[0]['alpaca']['device_number'], 0)

    def test_coordinator_poll_uevent_rate_bound_and_eligibility(self):
        self.device()
        service, monitor = self.service(), Mock()
        monitor.changed.return_value = False
        source = Mock(wraps=self.source)
        discovery = CameraDiscovery(service, load_registry(), supported=False, source=source, monitor=monitor,
                                    clock=lambda: self.now)
        self.addCleanup(discovery.close)
        discovery.sync()
        source.scan.assert_not_called()
        monitor.start.assert_not_called()
        discovery.supported = True
        callback = Mock()
        discovery.on_event = callback
        self.assertEqual(len(discovery.sync()), 1)
        self.assertEqual(callback.call_count, 1)
        for self.now in (1, 2, 4.99):
            self.assertEqual(discovery.sync(), [])
        self.assertEqual(source.scan.call_count, 1)
        self.now = 5
        discovery.sync()
        self.assertEqual(source.scan.call_count, 2)
        monitor.changed.return_value = True
        self.now = 5.1
        discovery.sync()
        self.assertEqual(source.scan.call_count, 2)
        self.now = 5.5
        discovery.sync()
        self.assertEqual(source.scan.call_count, 3)
        service.update_settings({'serve': False})
        self.now = 20
        discovery.sync()
        self.assertEqual(source.scan.call_count, 3)
        for model in ('Raspberry Pi Zero W', 'Raspberry Pi Zero 2 W', 'Raspberry Pi 5 Model B'):
            self.assertFalse(camera_tier_allowed('Linux', model))
        for model in ('Raspberry Pi 3 Model B', 'Raspberry Pi 4 Model B', ''):
            self.assertTrue(camera_tier_allowed('Linux', model))
        self.assertFalse(camera_tier_allowed('Darwin'))


class NetlinkTests(unittest.TestCase):
    def setUp(self):
        family = patch.object(socket, 'AF_NETLINK', 16, create=True)
        family.start()
        self.addCleanup(family.stop)

    def test_optional_socket_failure_and_kernel_only_bounded_hints(self):
        factory = Mock(side_effect=OSError(errno.EPERM, 'denied'))
        monitor = UEventMonitor(factory)
        monitor.start()
        monitor.start()
        factory.assert_called_once()
        self.assertFalse(monitor.changed())
        sock = Mock()
        monitor = UEventMonitor(Mock(return_value=sock))
        monitor.start()
        sock.bind.assert_called_once_with((0, 1))
        sock.recvfrom.side_effect = [(b'SUBSYSTEM=usb\0ACTION=add\0', (42, 1)),
                                    (b'SUBSYSTEM=net\0ACTION=add\0', (0, 1)), BlockingIOError()]
        self.assertFalse(monitor.changed())
        sock.recvfrom.side_effect = [(b'SUBSYSTEM=usb\0ACTION=remove\0', (0, 1)), BlockingIOError()]
        self.assertTrue(monitor.changed())
        sock.recvfrom.side_effect = OSError(errno.ENOBUFS, 'overflow')
        self.assertTrue(monitor.changed())
        sock.close.assert_called_once()
        self.assertIsNone(monitor.socket)

    def test_flood_drains_at_most_32_messages_and_reconciles(self):
        sock = Mock()
        monitor = UEventMonitor(Mock(return_value=sock))
        monitor.start()
        sock.recvfrom.return_value = (b'SUBSYSTEM=net\0ACTION=add\0', (0, 1))
        self.assertTrue(monitor.changed())
        self.assertEqual(sock.recvfrom.call_count, 32)
        monitor.close()
