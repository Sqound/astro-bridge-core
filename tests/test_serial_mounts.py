"""Declared serial mounts, detection, home list and the API (one code path for the interface and apps)."""
import os
import socket
import tempfile
import time
import unittest
from unittest import mock

from helpers.fake_nexstar import FakeHandControl
from test_native_contract_1_1 import bridge, call, tokens
from test_serial_relay import until, connect, STOP, SLEW
from wifi_bridge import serial_mounts, serial_port
from wifi_bridge.config import Invalid
from wifi_bridge.serial_mounts import validate_serial_mounts

ROOT = '/dev/serial'
BY_ID = ROOT + '/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0'
BY_PATH = ROOT + '/by-path/platform-3f980000.usb-usb-0:1.2:1.0-port0'


class Tree:
    """A fake /dev/serial and /sys on a temporary directory, adapters being pseudo-terminals."""
    def __init__(self, test):
        self.dir = tempfile.TemporaryDirectory(); test.addCleanup(self.dir.cleanup)
        self.root = self.dir.name
        self.test = test
        for name in ('by-id', 'by-path'): os.makedirs(os.path.join(self.root, 'serial', name))
        patches = [mock.patch.object(serial_port, 'DEV_ROOT', os.path.join(self.root, 'serial')),
                   mock.patch.object(serial_port, 'SYS_ROOT', os.path.join(self.root, 'sys')),
                   # Pseudo-terminals: /dev/ttys003 on macOS, /dev/pts/3 on Linux.
                   mock.patch.object(serial_port, 'NODE_PATTERN', r'(?:ttys)?[0-9]+'),
                   # Never touch 2000-2099 in tests: a bench relay may be listening there.
                   mock.patch.object(serial_mounts, 'PORT_BASE', 2800), mock.patch.object(serial_mounts, 'PORT_LIMIT', 2899)]
        for patch in patches: patch.start(); test.addCleanup(patch.stop)
        self.devices = {}

    def path(self, *parts): return os.path.join(self.root, 'serial', *parts)

    def plug(self, key, serial='A50285BI', vendor='0403', product='6001', fake=None):
        fake = fake or FakeHandControl(); self.test.addCleanup(fake.close)
        node = os.path.basename(fake.path)
        usb = os.path.join(self.root, 'sys', 'devices', 'usb-' + key)
        os.makedirs(os.path.join(usb, 'if0'), exist_ok=True)
        for name, value in (('idVendor', vendor), ('idProduct', product), ('manufacturer', 'FTDI'), ('product', 'FT232R USB UART'), *((('serial', serial),) if serial else ())):
            with open(os.path.join(usb, name), 'w') as f: f.write(value + '\n')
        tty = os.path.join(self.root, 'sys', 'class', 'tty', node); os.makedirs(tty, exist_ok=True)
        if not os.path.lexists(os.path.join(tty, 'device')): os.symlink(os.path.join(usb, 'if0'), os.path.join(tty, 'device'))
        by_path = self.path('by-path', 'port-' + key)
        os.symlink(fake.path, by_path)
        link = None
        if serial:
            link = self.path('by-id', 'usb-FTDI_FT232R_USB_UART_%s-if00-port0' % serial)
            os.symlink(fake.path, link)
        self.devices[key] = (fake, link, by_path)
        return fake, link or by_path, by_path

    def unplug(self, key):
        _, link, by_path = self.devices[key]
        for p in (link, by_path):
            if p and os.path.lexists(p): os.unlink(p)


class ValidationTests(unittest.TestCase):
    def entry(self, **kw): return dict({'id': 'm1', 'name': 'NexStar', 'device': BY_ID}, **kw)

    def test_ports_are_assigned_from_2000_and_stay_predictable(self):
        rows = validate_serial_mounts([self.entry(), self.entry(id='m2', device=BY_PATH)], set())
        self.assertEqual([r['tcp_port'] for r in rows], [2000, 2001])
        self.assertEqual((rows[0]['baud'], rows[0]['format'], rows[0]['profile'], rows[0]['idle_timeout_seconds']), (9600, '8N1', 'nexstar', 120))
        again = validate_serial_mounts([self.entry(id='m2', device=BY_PATH), self.entry()], set(), rows)
        self.assertEqual({r['id']: r['tcp_port'] for r in again}, {'m1': 2000, 'm2': 2001})   # kept, not reshuffled

    def test_explicit_port_and_collisions(self):
        rows = validate_serial_mounts([self.entry(tcp_port=2050), self.entry(id='m2', device=BY_PATH)], set())
        self.assertEqual([r['tcp_port'] for r in rows], [2050, 2000])
        for bad in ([self.entry(tcp_port=1999)], [self.entry(tcp_port=2100)], [self.entry(tcp_port=True)],
                    [self.entry(tcp_port=2000), self.entry(id='m2', device=BY_PATH, tcp_port=2000)]):
            with self.assertRaises(Invalid): validate_serial_mounts(bad, set())

    def test_the_saved_adapter_is_never_a_bare_tty_number(self):
        for bad in ('/dev/ttyUSB0', '/dev/ttyACM1', '/dev/serial/by-id/../../ttyUSB0', '/dev/serial/by-id/', 'COM3', None, ROOT + '/by-foo/x'):
            with self.assertRaises(Invalid): validate_serial_mounts([self.entry(device=bad)], set())

    def test_other_rules(self):
        for bad in ([self.entry(), self.entry(id='m2')],                                  # same adapter twice
                    [self.entry(baud=1234)], [self.entry(format='9N1')], [self.entry(profile='lx200')],
                    [self.entry(idle_timeout_seconds=3)], [self.entry(name=' ')], [self.entry(extra=1)],
                    [self.entry(id='dup')] * 2, [dict(self.entry(), id=str(i), device=ROOT + '/by-id/d%d' % i) for i in range(9)]):
            with self.assertRaises(Invalid): validate_serial_mounts(bad, set())
        with self.assertRaises(Invalid): validate_serial_mounts([self.entry()], {'m1'})
        self.assertEqual(validate_serial_mounts([self.entry(idle_timeout_seconds=0)], set())[0]['idle_timeout_seconds'], 0)


class InventoryTests(unittest.TestCase):
    def test_serial_number_decides_the_stable_path(self):
        tree = Tree(self)
        tree.plug('a', serial='A50285BI'); tree.plug('b', serial=None)
        rows = {r['by_path'].rsplit('-', 1)[1]: r for r in serial_port.inventory()}
        a, b = rows['a'], rows['b']
        self.assertEqual((a['stable_kind'], a['serial_number'], a['manufacturer'], a['vendor_id']), ('by-id', 'A50285BI', 'FTDI', '0403'))
        self.assertEqual((b['stable_kind'], b['serial_number']), ('by-path', None))      # identical adapters without a serial number: the socket identifies them

    def test_adapters_without_sysfs_or_links_are_still_listed_honestly(self):
        tree = Tree(self)
        fake = FakeHandControl(); self.addCleanup(fake.close)
        os.symlink(fake.path, tree.path('by-id', 'usb-Unknown-if00'))
        self.assertEqual(serial_port.inventory()[0]['stable_kind'], 'by-id')


class ApiBase(unittest.TestCase):
    def setUp(self):
        self.tree = Tree(self)

    def run_bridge(self):
        context = bridge(); c, server, net = context.__enter__(); self.addCleanup(context.__exit__, None, None, None)
        c.serial_mounts.active = True; self.addCleanup(c.serial_mounts.close)
        admin, client = tokens(server)
        return c, server, admin, client

    def rev(self, server, token):
        _, _, value = call(server, 'GET', '/api/v1/config', token=token)
        return {'If-Match': '"' + value['config_revision'] + '"'}

class ApiTests(ApiBase):
    def test_detection_event_proposal_declaration_and_home_list(self):
        c, server, admin, client = self.run_bridge()
        c.serial_events(c.serial_mounts.tick())                                         # baseline at start
        fake, link, _ = self.tree.plug('a')
        c.serial_events(c.serial_mounts.scan(force=True))
        _, _, events = call(server, 'GET', '/api/v1/events?after=0', token=client)
        self.assertIn('serial.detected', [e['type'] for e in events['events']])
        self.assertTrue(until(lambda: c.serial_mounts.detected()[0]['identification'] == 'identified'))   # FTDI cable: read-only identification at plug
        status, _, body = call(server, 'GET', '/api/v1/serial-devices', token=client)
        found = body['serial_devices'][0]
        self.assertEqual((status, found['link_type'], found['serial_number'], found['stable_kind'], found['identification']), (200, 'serial_usb', 'A50285BI', 'by-id', 'identified'))
        self.assertEqual((found['proposal']['name'], found['proposal']['tcp_port'], found['proposal']['declared_identity']['model_name']), ('NexStar 4/5 SE', 2800, '4/5 SE'))
        self.assertEqual(c.serial_mounts.relays, {})                                    # seen is not declared: nothing relayed
        with self.assertRaises(OSError): socket.create_connection(('127.0.0.1', 2800), timeout=.3).close()
        # A paired app without admin rights reads but cannot declare.
        self.assertEqual(call(server, 'POST', '/api/v1/serial-mounts', {'device': found['stable_path']}, client, self.rev(server, admin))[0], 403)
        status, _, created = call(server, 'POST', '/api/v1/serial-mounts', {'device': found['stable_path']}, admin, self.rev(server, admin))
        self.assertEqual(status, 201)
        mount = created['serial_mount']
        self.assertEqual((mount['name'], mount['tcp_port'], mount['profile'], mount['link_type']), ('NexStar 4/5 SE', 2800, 'nexstar', 'serial_usb'))
        self.assertEqual(c.serial_mounts.detected(), [])                                # no longer only "seen"
        status, _, devices = call(server, 'GET', '/api/v1/devices', token=client)
        row = next(d for d in devices['devices'] if d['id'] == mount['id'])
        self.assertEqual((row['link_type'], row['kind'], row['state'], row['client_connected'], row['endpoint']['port']), ('serial_usb', 'mount', 'online', False, 2800))
        self.assertIn('wifi', {d['link_type'] for d in devices['devices']})            # Wi-Fi telescope in the same list
        # Connect to the relay as the app would.
        with socket.create_connection(('127.0.0.1', 2800), timeout=3) as app:
            app.sendall(b'Kx'); self.assertEqual(app.recv(4), b'x#')
            _, _, devices = call(server, 'GET', '/api/v1/devices', token=client)
            self.assertTrue(next(d for d in devices['devices'] if d['id'] == mount['id'])['client_connected'])
            # Removal or a settings change while a client holds the link is refused.
            self.assertEqual(call(server, 'DELETE', '/api/v1/serial-mounts/' + mount['id'], None, admin, self.rev(server, admin))[0], 409)
            self.assertEqual(call(server, 'PATCH', '/api/v1/serial-mounts/' + mount['id'], {'idle_timeout_seconds': 60}, admin, self.rev(server, admin))[0], 409)
            # Renaming is harmless and keeps the link.
            self.assertEqual(call(server, 'PATCH', '/api/v1/serial-mounts/' + mount['id'], {'name': 'Garden NexStar'}, admin, self.rev(server, admin))[0], 200)
            app.sendall(b'V'); self.assertEqual(app.recv(4), bytes([4, 15]) + b'#')
            _, _, state = call(server, 'GET', '/api/v1/serial-mounts/' + mount['id'], token=client)
            self.assertEqual(state['serial_mount']['name'], 'Garden NexStar')
        self.assertTrue(until(lambda: c.serial_mounts.relays[mount['id']].snapshot()['state'] == 'idle'))
        # Identification is read-only and refused while held; free now.
        status, _, ident = call(server, 'POST', '/api/v1/serial-mounts/%s/identify' % mount['id'], {}, admin)
        self.assertEqual((status, ident['identity']['model_code']), (200, 11))
        status, _, deleted = call(server, 'DELETE', '/api/v1/serial-mounts/' + mount['id'], None, admin, self.rev(server, admin))
        self.assertEqual((status, deleted['deleted']), (200, True))
        self.assertEqual(c.serial_mounts.relays, {})
        self.assertEqual(len(c.serial_mounts.detected()), 1)                           # back to "detected"

    def test_removal_event_and_unplugged_state(self):
        c, server, admin, client = self.run_bridge()
        c.serial_events(c.serial_mounts.tick())
        fake, link, _ = self.tree.plug('a')
        c.serial_events(c.serial_mounts.scan(force=True))
        status, _, created = call(server, 'POST', '/api/v1/serial-mounts', {'device': link, 'name': 'Mount A'}, admin, self.rev(server, admin))
        self.assertEqual(status, 201); ident = created['serial_mount']['id']
        self.tree.unplug('a')
        c.serial_events(c.serial_mounts.scan(force=True))
        _, _, events = call(server, 'GET', '/api/v1/events?after=0', token=client)
        self.assertIn('serial.removed', [e['type'] for e in events['events']])
        self.assertEqual(c.serial_mounts.detected(), [])      # an unplugged declared adapter is not offered as new
        self.assertTrue(until(lambda: c.serial_mounts.public()[0]['adapter_present'] is False))
        row = next(d for d in call(server, 'GET', '/api/v1/devices', token=client)[2]['devices'] if d['id'] == ident)
        self.assertEqual(row['state'], 'offline')

    def test_two_declared_mounts_have_distinct_ports_and_never_disturb_each_other(self):
        c, server, admin, client = self.run_bridge()
        fa, la, _ = self.tree.plug('a', serial='AAA')
        fb, lb, _ = self.tree.plug('b', serial='BBB', fake=FakeHandControl(model=12))
        c.serial_mounts.scan(force=True)
        ids = []
        for link, name in ((la, 'A'), (lb, 'B')):
            status, _, created = call(server, 'POST', '/api/v1/serial-mounts', {'device': link, 'name': name}, admin, self.rev(server, admin))
            self.assertEqual(status, 201); ids.append(created['serial_mount'])
        self.assertEqual([m['tcp_port'] for m in ids], [2800, 2801])
        for relay in c.serial_mounts.relays.values(): self.assertTrue(until(lambda: relay.snapshot()['identity'] is not None))
        base_a, base_b = len(fa.sent()), len(fb.sent())          # the read-only identification at plug is behind us
        a = socket.create_connection(('127.0.0.1', 2800), timeout=3); self.addCleanup(a.close)
        a.sendall(SLEW); a.recv(4)
        # Hot add of a third adapter while the first is in service.
        fc, lc, _ = self.tree.plug('c', serial='CCC')
        status, _, third = call(server, 'POST', '/api/v1/serial-mounts', {'device': lc, 'name': 'C'}, admin, self.rev(server, admin))
        self.assertEqual((status, third['serial_mount']['tcp_port']), (201, 2802))
        a.sendall(b'Kx'); self.assertEqual(a.recv(4), b'x#')                           # first one still served
        self.assertEqual(fa.sent()[base_a:], [SLEW, b'Kx'])
        # Hot removal of the third touches nobody.
        self.assertEqual(call(server, 'DELETE', '/api/v1/serial-mounts/' + third['serial_mount']['id'], None, admin, self.rev(server, admin))[0], 200)
        a.sendall(b'V'); self.assertEqual(a.recv(4), bytes([4, 15]) + b'#')
        self.assertEqual(c.serial_mounts.public()[0]['client']['address'], '127.0.0.1')
        b = socket.create_connection(('127.0.0.1', 2801), timeout=3); self.addCleanup(b.close)
        b.sendall(b'Kz'); self.assertEqual(b.recv(4), b'z#')                           # two clients, one per relay
        a.close()
        self.assertTrue(until(lambda: len(fa.sent()) >= base_a + 3 + len(STOP)))
        time.sleep(.3)
        self.assertEqual(fa.sent()[base_a + 3:], STOP)
        self.assertEqual(fb.sent()[base_b:], [b'Kz'])                                           # only A was stopped

    def test_declaring_an_unlisted_adapter_needs_a_name_and_a_stable_path(self):
        c, server, admin, _ = self.run_bridge()
        self.assertEqual(call(server, 'POST', '/api/v1/serial-mounts', {'device': BY_ID.replace(ROOT, self.tree.path())}, admin, self.rev(server, admin))[0], 400)
        self.assertEqual(call(server, 'POST', '/api/v1/serial-mounts', {'device': '/dev/ttyUSB0', 'name': 'x'}, admin, self.rev(server, admin))[0], 400)
        self.assertEqual(call(server, 'POST', '/api/v1/serial-mounts', {'device': '/dev/ttyUSB0', 'name': 'x', 'bogus': 1}, admin, self.rev(server, admin))[0], 400)

    def test_saved_in_configuration_and_survives_unrelated_saves(self):
        c, server, admin, _ = self.run_bridge()
        fake, link, _ = self.tree.plug('a'); c.serial_mounts.scan(force=True)
        call(server, 'POST', '/api/v1/serial-mounts', {'device': link, 'name': 'Mount A'}, admin, self.rev(server, admin))
        self.assertEqual(call(server, 'PATCH', '/api/v1/settings', {'hostname': 'astro-renamed'}, admin, self.rev(server, admin))[0], 200)
        self.assertEqual(len(c.store.config['serial_mounts']), 1)
        self.assertEqual(c.store.config['serial_mounts'][0]['device'], link)
        self.assertEqual(c.status()['config']['serial_mounts'][0]['name'], 'Mount A')


class ProposalExamplesTests(ApiBase):
    """The documented JSON examples carry exactly the fields the API returns."""
    def test_examples_match_live_responses(self):
        import json, re
        from pathlib import Path
        blocks = [json.loads(b) for b in re.findall(r'```json\n(.*?)\n```', (Path(__file__).parents[1] / 'docs/SERIAL-MOUNTS-CONTRACT-PROPOSAL.md').read_text(), re.S)]
        c, server, admin, client = self.run_bridge()
        fake, link, _ = self.tree.plug('a'); c.serial_mounts.scan(force=True)
        self.assertTrue(until(lambda: c.serial_mounts.detected()[0]['identification'] == 'identified'))
        live_device = call(server, 'GET', '/api/v1/serial-devices', token=client)[2]['serial_devices'][0]
        status, _, created = call(server, 'POST', '/api/v1/serial-mounts', {'device': link, 'name': 'Garden 4SE'}, admin, self.rev(server, admin))
        doc_device, doc_declare, doc_created = blocks[0]['serial_devices'][0], blocks[1], blocks[2]
        self.assertEqual(set(doc_device), set(live_device))
        self.assertEqual(set(doc_device['proposal']) - {'declared_identity'}, set(live_device['proposal']) - {'declared_identity'})
        self.assertEqual(set(doc_device['identity']), set(live_device['identity']))
        self.assertLessEqual(set(doc_declare), set(live_device['proposal']) | {'name', 'device'})
        self.assertEqual(status, 201)
        self.assertLessEqual(set(doc_created['serial_mount']), set(created['serial_mount']))
        self.assertEqual(set(doc_created['serial_mount']['client_setup']), set(created['serial_mount']['client_setup']))


class ProfileDataTests(unittest.TestCase):
    def test_profile_is_data_and_another_device_can_have_its_own_or_no_stop(self):
        from wifi_bridge.serial_profile import validate_profile, load_profiles
        import json
        from wifi_bridge.serial_profile_data import PROFILES
        raw = json.loads(json.dumps(PROFILES[0]))
        other = dict(raw, id='other', stop_on_loss={'reply_terminator': '0d', 'frame_timeout_seconds': .3, 'frames': [{'id': 'halt', 'hex': '3a4b310d'}]})
        self.assertEqual(validate_profile(other)['stop']['frames'][0]['data'], b':K1\r')
        self.assertIsNone(validate_profile(dict(raw, id='plain', stop_on_loss=None))['stop'])
        for bad in (dict(raw, stop_on_loss={'frames': []}), dict(raw, serial={'baud': 7, 'format': '8N1', 'flow_control': 'none'}), dict(raw, id='Bad Id')):
            with self.assertRaises(ValueError): validate_profile(bad)
        self.assertEqual(list(load_profiles()), ['nexstar'])


if __name__ == '__main__':
    unittest.main()
