"""Only loopback fakes execute. Linux launch, socket and USB policy are inspected."""
from pathlib import Path
import os
import socket
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
import xml.etree.ElementTree as ET

from helpers.indi_fake import DEVICE, PATH, FakePlatform
from wifi_bridge.announce import service_xml
from wifi_bridge.cameras.alpaca import AlpacaError
from wifi_bridge.cameras.indi_platform import LinuxIndi, sandbox_command, usb_node
from wifi_bridge.cameras.listener import ManagementAccess
from wifi_bridge.cameras.registry import CameraRegistry, load_registry, validate_manifest
from wifi_bridge.cameras.service import CameraService, CameraOwnerChanged
from wifi_bridge.cameras.simulator import SimulatedCamera


class IndiTests(unittest.TestCase):
    def setUp(self):
        self.platform = FakePlatform()
        self.sdk = SimulatedCamera(width=4, height=4)
        self.sdk.handoff_ready = lambda: True
        self.factory = Mock(return_value=self.sdk)
        self.registry = CameraRegistry(list(load_registry().descriptors()), {'zwo_asi': self.factory})
        self.service = CameraService(indi_platform=self.platform)
        self.addCleanup(self.service.close)

    def start(self):
        self.service.update_settings({'indi': True})
        self.service.reconcile_usb([DEVICE], self.registry)
        self.platform.wait_ready(self.service)

    def test_raw_loopback_getproperties_then_path_loss_and_return_to_alpaca(self):
        self.start()
        row = self.service.inventory('127.0.0.1')[0]
        self.assertEqual(row['indi'], {'state':'ready', 'port':7624})
        self.assertTrue(self.service.advertises_indi({('127.0.0.1','lo','127.0.0.0/8')}))
        self.assertFalse(self.service.advertises_indi({('127.0.0.1','other','127.0.0.0/8')}))
        self.assertFalse(self.service.advertises_indi(set()))
        self.assertIsNone(row['alpaca'])
        self.assertEqual(self.service.configured_devices(), [])
        self.factory.assert_not_called()
        self.assertEqual(self.service.inventory('192.0.2.1')[0]['indi']['state'], 'unavailable')
        with socket.create_connection(('127.0.0.1', self.platform.port), timeout=2) as client:
            client.sendall(b'<getProperties version="1.7"/>\n')
            self.assertIn(b'defTextVector', client.recv(256))
        self.service.sync_indi(())
        self.assertIsNotNone(self.platform.starts[0].poll())
        self.assertNotEqual(self.service.indi_status()['state'], 'ready')
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.platform.port), timeout=0.2)
        self.service.update_settings({'indi':False})
        self.service.reconcile_usb([DEVICE], self.registry)
        self.factory.assert_called_once()
        self.assertEqual(len(self.service.configured_devices()), 1)

    def test_active_or_unreaped_alpaca_refuses_handoff_and_keeps_lease(self):
        self.service.reconcile_usb([DEVICE], self.registry)
        self.service.execute(0, 'PUT', 'connected', {'connected':'true'}, 5, '127.0.0.1')
        with self.assertRaises(CameraOwnerChanged):
            self.service.update_settings({'indi':True})
        self.assertTrue(self.service.execute(0,'GET','connected',{},5,'127.0.0.1'))
        self.service.execute(0, 'PUT', 'connected', {'connected':'false'}, 5, '127.0.0.1')
        self.sdk.handoff_ready = lambda: False
        with self.assertRaises(CameraOwnerChanged):
            self.service.update_settings({'indi':True})
        self.sdk.handoff_ready = lambda: True
        self.start()
        with self.assertRaises(AlpacaError):
            self.service.execute(0, 'PUT', 'connected', {'connected':'true'}, 5, '127.0.0.1')

    def test_unreaped_indi_cannot_be_handed_to_alpaca(self):
        self.start()
        child = self.platform.starts[0]
        with patch.object(child, 'wait', side_effect=subprocess.TimeoutExpired('indi', 1)):
            with self.assertRaises(OSError):
                self.service.update_settings({'indi':False})
            self.assertTrue(self.service.get_settings()['indi'])
            self.assertEqual(self.service.configured_devices(), [])
            self.assertNotEqual(self.service.indi_status()['state'], 'ready')
        self.service.update_settings({'indi':False})
        self.assertIsNotNone(child.poll())

    def test_dead_outer_monitor_alone_does_not_release_the_usb_claim(self):
        self.start()
        # Independent lifetime witness stays open even if outer wait succeeds.
        old = self.service.indi.guard
        guard, writer = os.pipe()
        self.service.indi.guard = guard
        os.close(old)
        try:
            with self.assertRaises(OSError):
                self.service.update_settings({'indi':False})
            self.assertIsNotNone(self.platform.starts[0].poll())
            self.assertTrue(self.service.get_settings()['indi'])
            self.assertEqual(self.service.configured_devices(), [])
        finally:
            os.close(writer)
        self.service.update_settings({'indi':False})

    def test_ambiguous_peer_uncertain_inventory_unplug_and_replug(self):
        self.start()
        for extra in ([DEVICE, dict(DEVICE, port_path='1-3', product_id=0x0001)], None, []):
            self.service.reconcile_usb(extra, self.registry)
            self.service.sync_indi((PATH,))
            self.assertIsNotNone(self.platform.starts[-1].poll())
            self.assertNotEqual(self.service.indi_status()['state'], 'ready')
            self.assertEqual(self.service.configured_devices(), [])
            self.service.reconcile_usb([DEVICE], self.registry)
            self.platform.wait_ready(self.service)
        self.factory.assert_not_called()

    def test_peer_restrictions_and_crash_withdrawal(self):
        self.service.update_settings({'allowed_subnets':['127.0.0.8/32']})
        self.start()
        with socket.create_connection(('127.0.0.1', self.platform.port), timeout=1) as peer:
            try:
                peer.sendall(b'<getProperties version="1.7"/>\n')
                self.assertEqual(peer.recv(128), b'')
            except ConnectionResetError:
                pass
        # Real service exit on control EOF causes worker and fake driver to die.
        self.service.indi.channel.shutdown(socket.SHUT_WR)
        self.platform.starts[0].wait(timeout=2)
        self.service.sync_indi((PATH,))
        self.assertNotEqual(self.service.indi_status()['state'], 'ready')
        self.assertIsNone(self.service.indi.child)

    def test_missing_packages_and_partial_binding_never_advertise(self):
        self.service.update_settings({'indi': True})
        self.service.reconcile_usb([DEVICE], self.registry)
        with patch.object(self.platform, 'launch', side_effect=FileNotFoundError):
            self.service.sync_indi((PATH,))
        self.assertEqual(self.service.indi_status()['state'], 'unavailable')
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.platform.port), timeout=0.2)
        self.assertEqual(self.platform.starts, [])
        self.factory.assert_not_called()

    def test_linux_command_restricts_device_network_driver_and_identity(self):
        device = Path('/dev/bus/usb/001/007')
        attributes = Path('/sys/devices/example/usb1/1-2')
        command = sandbox_command(device, DEVICE, attributes)
        self.assertEqual(command.count('--dev-bind'), 1)
        i = command.index('--dev-bind')
        self.assertEqual(command[i+1:i+3], [str(device)]*2)
        self.assertTrue({'--unshare-net','--unshare-pid','--as-pid-1','--die-with-parent','--cap-drop'} <= set(command))
        self.assertNotIn('--share-net', command)
        for path in ('/dev/bus/usb', '/run', '/var', '/home', '/etc', '/sys', '/sys/devices'):
            self.assertNotIn(path, command)
        self.assertIn(['--remount-ro', '/'], [command[i:i+2] for i in range(len(command))])
        manifest = next(row for row in self.registry.descriptors() if row['driver']=='zwo_asi')
        for forged in ('indi_lx200', '/usr/bin/indi_asi_ccd', 'indi_asi_ccd@remote', '-f', ['indi_asi_ccd']):
            with self.subTest(forged=forged), self.assertRaises(ValueError):
                validate_manifest(dict(manifest, indi_driver=forged))
            with self.assertRaises(ValueError):
                LinuxIndi().launch(forged, DEVICE, None)
        with patch('os.access', return_value=True), patch('grp.getgrgid',return_value=Mock(gr_name='astro-indi')), patch('pwd.getpwnam',return_value=Mock(pw_uid=44,pw_gid=45)), \
                patch('wifi_bridge.cameras.indi_platform.usb_node',return_value=device), \
                patch.object(Path,'resolve',autospec=True,side_effect=lambda p, **kw: attributes if p.name=='1-2' else p), \
                patch('subprocess.Popen') as spawn:
            LinuxIndi().launch('indi_asi_ccd', DEVICE, None)
            self.assertEqual(spawn.call_args.args, (command,))
            self.assertEqual(spawn.call_args.kwargs['user'],44)
            self.assertEqual(spawn.call_args.kwargs['extra_groups'],[])

    def test_health_requires_bounded_indi_property_evidence(self):
        from wifi_bridge.cameras.indi_worker import healthy
        responses = (([b'HTTP/1.0 200 OK', b''], False),
                     ([b'<message device="Other"/>', b''], False),
                     ([b' ' * 1024] * 8, False),
                     ([b'<defNumberVec', b'tor device="Camera" name="Exposure"/>'], True))
        for blocks, expected in responses:
            peer = MagicMock()
            peer.__enter__.return_value = peer
            peer.recv.side_effect = blocks
            with patch('socket.create_connection',return_value=peer):
                self.assertEqual(healthy(7624),expected)
            peer.sendall.assert_called_once_with(b'<getProperties version="1.7"/>\n')

    def test_namespace_worker_refuses_extra_devices_interfaces_and_wrong_pid(self):
        from wifi_bridge.cameras.indi_worker import confined
        with tempfile.TemporaryDirectory() as tmp:
            usbfs, sysfs = Path(tmp)/'usb', Path(tmp)/'sys'
            attrs = sysfs/'1-2'
            attrs.mkdir(parents=True)
            for key, val in dict(busnum='1',devnum='7',idVendor='03c3',idProduct='120a').items():
                (attrs/key).write_text(val)
            node = usbfs/'001/007'
            node.parent.mkdir(parents=True)
            node.touch()
            def check():
                return confined(node,'1-2',0x03c3,0x120a,sysfs=sysfs,usbfs=usbfs)
            with patch('os.getpid',return_value=1), patch('os.geteuid',return_value=44), \
                    patch.object(Path,'is_char_device',return_value=True), patch('socket.if_nameindex',return_value=[(1,'lo')]):
                self.assertTrue(check())
                with patch('os.getpid',return_value=2):
                    self.assertFalse(check())
                with patch('socket.if_nameindex',return_value=[(1,'lo'),(2,'wlan0')]):
                    self.assertFalse(check())
                alias = node.with_name('008')
                alias.touch()
                self.assertFalse(check())
                alias.unlink()
                (attrs/'devnum').write_text('8')
                self.assertFalse(check())

    def test_linux_checks_usb_metadata_and_exact_bound_interface(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); attrs = root/'1-2'; attrs.mkdir()
            for key, val in dict(busnum='1', devnum='7',idVendor='03c3',idProduct='120a').items():
                (attrs/key).write_text(val)
            node = root/'001/007'; node.parent.mkdir(); node.touch()
            with self.assertRaises(ValueError):
                usb_node(DEVICE,45,root,root)
            mode = 0o600
            old_lstat = Path.lstat
            def metadata(path):
                return Mock(st_mode=stat.S_IFCHR | mode,st_uid=0,st_gid=45,st_rdev=os.makedev(189,6)) if path == node else old_lstat(path)
            with patch.object(Path,'lstat',metadata):
                with self.assertRaises(ValueError):
                    usb_node(DEVICE,45,root,root)
                mode = 0o660
                self.assertEqual(usb_node(DEVICE,45,root,root),node)
                (attrs/'idProduct').write_text('120b')
                with self.assertRaises(ValueError):
                    usb_node(DEVICE,45,root,root)
        access = ManagementAccess('192.0.2.4','192.0.2.0/24',[],'wlan1')
        with patch.object(socket,'SO_BINDTODEVICE',25,create=True), patch('socket.socket') as create:
            LinuxIndi().listen(access)
            listener = create.return_value
            listener.setsockopt.assert_any_call(socket.SOL_SOCKET,25,b'wlan1\0')
            listener.bind.assert_called_once_with(('192.0.2.4',7624))
            with self.assertRaises(ValueError):
                LinuxIndi().listen(ManagementAccess('192.0.2.4','192.0.2.0/24',[]))
            listener.bind.side_effect = OSError()
            with self.assertRaises(OSError):
                LinuxIndi().listen(access)
            listener.close.assert_called_once()

    def test_announcement_is_primary_only_and_health_gated(self):
        identity = dict(name='Test',device_id='0123456789abcdef')
        for runtime in ({}, {'indi':7624}):
            root = ET.fromstring(service_xml(identity,runtime))
            values = {svc.findtext('type'): [v.text for v in svc.findall('txt-record')] for svc in root.findall('service')}
            self.assertNotIn('_indi._tcp', values)
            for kind, txt in values.items():
                self.assertEqual('indi=7624' in txt, bool(runtime) and kind=='_astrobridge._tcp')
