"""Serial relay safety: one client per port, stop sent by the bridge when the link is lost
and never while a client holds it. Pseudo-terminals only: no real serial port is opened."""
import os
import socket
import tempfile
import time
import unittest

from helpers.fake_nexstar import FakeHandControl
from wifi_bridge.serial_profile import profile
from wifi_bridge.serial_relay import SerialRelay

NEXSTAR = profile('nexstar')
STOP = [f['data'] for f in NEXSTAR['stop']['frames']]
SLEW = b'P\x02\x10\x24\x03\x00\x00\x00'       # fixed rate 3 on the azimuth board (documented pass-through)


def until(condition, timeout=10.0):   # generous: a loaded CI runner can be slow; returns as soon as true
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition(): return True
        time.sleep(.02)
    return condition()


def connect(relay):
    return socket.create_connection(('127.0.0.1', relay.tcp_port), timeout=3)


def closed_by_peer(sock):
    sock.settimeout(1.5)
    try: return sock.recv(16) == b''
    except (ConnectionResetError, BrokenPipeError): return True
    except socket.timeout: return False


class RelayCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        self.items = []

    def mount(self, ident='nx', device=None, prof=NEXSTAR, **options):
        fake = FakeHandControl(**options.pop('fake', {})); self.addCleanup(fake.close)
        self.items.append(ident)
        link = os.path.join(self.dir.name, '%s-%d-link' % (ident, len(self.items)))
        os.symlink(fake.path, link)
        relay = SerialRelay(ident, ident, link, prof, 0, bind='127.0.0.1', auto_identify=False, **options).start()
        self.addCleanup(relay.close)
        return fake, relay, link

    def sockets(self, *socks):
        for s in socks: self.addCleanup(s.close)


class RelayTests(RelayCase):
    def test_bytes_pass_untouched_in_both_directions(self):
        fake, relay, _ = self.mount()
        with connect(relay) as client:
            client.sendall(b'Kx'); self.assertEqual(client.recv(8), b'x#')
            client.sendall(b'V'); self.assertEqual(client.recv(8), bytes([4, 15]) + b'#')
        self.assertEqual(fake.sent()[:2], [b'Kx', b'V'])

    def test_stale_device_bytes_never_reach_a_new_client_and_none_are_invented(self):
        fake, relay, _ = self.mount()
        os.write(fake.master, b'junk#')                    # before any client: discarded
        with connect(relay) as client:
            client.settimeout(.6)
            with self.assertRaises(socket.timeout): client.recv(8)

    def test_no_stop_profile_means_nothing_is_sent(self):
        none = dict(NEXSTAR, stop=None)
        fake, relay, _ = self.mount(prof=none)
        with connect(relay) as client:
            client.sendall(SLEW); client.recv(4)
        self.assertTrue(until(lambda: relay.snapshot()['state'] == 'idle'))
        self.assertEqual(fake.sent(), [SLEW]); self.assertEqual(relay.snapshot()['stops_sent'], 0)


class SingleClientTests(RelayCase):
    def test_second_client_is_refused_and_can_neither_read_nor_write(self):
        fake, relay, _ = self.mount()
        first = connect(relay); self.sockets(first)
        first.sendall(b'Kx'); self.assertEqual(first.recv(8), b'x#')
        second = connect(relay); self.sockets(second)
        self.assertTrue(closed_by_peer(second))               # closed at once, not queued
        try: second.sendall(b'Kz')
        except OSError: pass
        os.write(fake.master, b'zz#')                         # the device speaks: only the owner hears it
        self.assertEqual(first.recv(8), b'zz#')
        snapshot = relay.snapshot()
        self.assertEqual(snapshot['client']['address'], '127.0.0.1')
        self.assertEqual((snapshot['refused_clients'], snapshot['last_refused']['reason']), (1, 'LEASED_BY_OTHER_CLIENT'))
        self.assertIn('client_refused', [e['event'] for e in snapshot['journal']])
        self.assertEqual(fake.sent(), [b'Kx'])                # nothing the refused client wrote got through
        first.sendall(b'V'); self.assertEqual(first.recv(8), bytes([4, 15]) + b'#')   # the owner is unharmed

    def test_a_refusal_never_triggers_a_stop(self):
        fake, relay, _ = self.mount()
        first = connect(relay); self.sockets(first); first.sendall(b'Kx'); first.recv(8)
        for _ in range(3):
            extra = connect(relay); self.sockets(extra); self.assertTrue(closed_by_peer(extra))
        time.sleep(.3)
        self.assertEqual(fake.sent(), [b'Kx']); self.assertEqual(relay.snapshot()['stops_sent'], 0)


class StopOnLossTests(RelayCase):
    def assert_stop_after(self, fake, before):
        self.assertTrue(until(lambda: len(fake.sent()) >= len(before) + len(STOP)))
        self.assertEqual(fake.sent(), before + STOP)

    def test_close_during_a_movement_sends_the_stop_once_then_frees_the_port(self):
        fake, relay, _ = self.mount()
        client = connect(relay); client.sendall(SLEW); client.recv(4)
        self.assertEqual(fake.sent(), [SLEW])                 # held: no stop while the client is there
        time.sleep(.5); self.assertEqual(fake.sent(), [SLEW])
        client.close()
        self.assert_stop_after(fake, [SLEW])
        self.assertTrue(until(lambda: relay.snapshot()['state'] == 'idle'))
        time.sleep(.5)
        self.assertEqual(fake.sent(), [SLEW] + STOP)          # once
        last = relay.snapshot()['last_stop']
        self.assertEqual((last['reason'], last['frames'], last['acknowledged']), ('CLIENT_CLOSED', 9, 9))
        self.assertIsNone(relay.fd)                           # port released

    def test_loss_at_rest_sends_the_same_stop(self):
        fake, relay, _ = self.mount()
        client = connect(relay); client.sendall(b'Kx'); client.recv(4); client.close()
        self.assert_stop_after(fake, [b'Kx'])

    def test_a_client_that_never_wrote_cannot_have_moved_anything(self):
        fake, relay, _ = self.mount()
        connect(relay).close(); time.sleep(.5)
        self.assertEqual(fake.sent(), [])

    def test_stop_order_is_cancel_goto_then_zero_rate_on_both_boards(self):
        self.assertEqual(STOP[0], b'M')
        self.assertEqual({f[2] for f in STOP[1:]}, {0x10, 0x11})
        for frame in STOP[1:]:
            self.assertEqual((frame[0], len(frame)), (0x50, 8))
            self.assertEqual(frame[4:7], b'\0\0\0')           # rate 0 on a documented message

    def test_idle_timeout_is_adjustable_and_stops(self):
        fake, relay, _ = self.mount(idle_seconds=1)
        client = connect(relay); self.sockets(client); client.sendall(SLEW); client.recv(4)
        self.assert_stop_after(fake, [SLEW])
        self.assertTrue(closed_by_peer(client))
        self.assertEqual(relay.snapshot()['last_stop']['reason'], 'IDLE_TIMEOUT')

    def test_client_returns_after_the_stop_and_is_served_with_no_second_stop(self):
        fake, relay, _ = self.mount()
        first = connect(relay); first.sendall(SLEW); first.recv(4); first.close()
        self.assert_stop_after(fake, [SLEW])
        self.assertTrue(until(lambda: relay.snapshot()['state'] == 'idle'))
        again = connect(relay); self.sockets(again)
        again.sendall(b'Kq'); self.assertEqual(again.recv(8), b'q#')
        time.sleep(.4)
        self.assertEqual(fake.sent(), [SLEW] + STOP + [b'Kq'])
        self.assertEqual(relay.snapshot()['stops_sent'], 1)

    def test_bridge_shutdown_with_a_client_sends_the_stop(self):
        fake, relay, _ = self.mount()
        client = connect(relay); self.sockets(client); client.sendall(SLEW); client.recv(4)
        relay.close()
        self.assertEqual(fake.sent(), [SLEW] + STOP)

    def test_unplugged_while_held_closes_the_client_and_stops_once_the_adapter_returns(self):
        fake, relay, link = self.mount()
        client = connect(relay); self.sockets(client); client.sendall(SLEW); client.recv(4)
        os.unlink(link)                                       # the udev link disappears
        self.assertTrue(closed_by_peer(client))
        self.assertTrue(until(lambda: relay.snapshot()['stop_pending']))
        self.assertEqual(fake.sent(), [SLEW])                 # nothing can be sent yet
        refused = connect(relay); self.sockets(refused); self.assertTrue(closed_by_peer(refused))
        self.assertIn(relay.snapshot()['last_refused']['reason'], ('ADAPTER_UNAVAILABLE', 'STOP_PENDING'))
        os.symlink(fake.path, link)                           # plugged back
        self.assertTrue(until(lambda: len(fake.sent()) >= 1 + len(STOP)))
        self.assertEqual(fake.sent(), [SLEW] + STOP)
        self.assertTrue(until(lambda: not relay.snapshot()['stop_pending']))
        self.assertEqual(relay.snapshot()['stops_sent'], 1)
        again = connect(relay); self.sockets(again); again.sendall(b'Kx'); self.assertEqual(again.recv(4), b'x#')

    def test_unplugged_at_rest_refuses_clients_then_serves_after_replug(self):
        fake, relay, link = self.mount()
        os.unlink(link)
        self.assertTrue(until(lambda: relay.snapshot()['state'] == 'adapter_missing'))
        refused = connect(relay); self.sockets(refused); self.assertTrue(closed_by_peer(refused))
        self.assertEqual(relay.snapshot()['last_refused']['reason'], 'ADAPTER_UNAVAILABLE')
        os.symlink(fake.path, link)
        self.assertTrue(until(lambda: relay.snapshot()['adapter_present']))
        client = connect(relay); self.sockets(client); client.sendall(b'Kx'); self.assertEqual(client.recv(4), b'x#')
        self.assertEqual(fake.sent(), [b'Kx'])                # no stop: no client ever moved it

    def test_the_port_is_exclusive_even_against_another_opener(self):
        from wifi_bridge.serial_port import open_serial, PortBusy
        fake, relay, link = self.mount()
        client = connect(relay); self.sockets(client); client.sendall(b'Kx'); client.recv(4)
        with self.assertRaises(PortBusy): open_serial(link)


class IdentificationTests(RelayCase):
    def identity(self, **fake):
        device, relay, _ = self.mount(fake=fake)
        return device, relay, relay.request_identify()

    def test_reads_model_and_versions_with_read_only_frames(self):
        fake, relay, identity = self.identity()
        self.assertEqual((identity['responding'], identity['hand_control_version'], identity['model_code'], identity['model_name']),
                         (True, '4.15', 11, '4/5 SE'))
        self.assertEqual(identity['board_versions'], {'azimuth': '5.14', 'altitude': '5.14'})
        self.assertEqual(fake.sent(), [b'Kx', b'V', b'm', b'P\x01\x10\xfe\0\0\0\x02', b'P\x01\x11\xfe\0\0\0\x02'])
        for frame in fake.sent():                              # never a slew, a goto, a set or a stop
            self.assertNotIn(frame[:1], (b'M', b'R', b'r', b'S', b'T', b'H', b'W'))
            if frame[:1] == b'P': self.assertEqual(frame[3], 0xFE)

    def test_silent_device_leaves_the_relay_usable_and_says_not_identified(self):
        fake, relay, identity = self.identity(responds=False)
        self.assertFalse(identity['responding'])
        fake.responds = True
        client = connect(relay); self.sockets(client); client.sendall(b'Kx'); self.assertEqual(client.recv(4), b'x#')

    def test_two_models_are_told_apart(self):
        _, _, a = self.identity(model=11, version=(4, 15))
        _, _, b = self.identity(model=12, version=(5, 2))
        self.assertEqual((a['model_name'], b['model_name']), ('4/5 SE', '6/8 SE'))

    def test_never_while_a_client_holds_the_link(self):
        fake, relay, _ = self.mount()
        client = connect(relay); self.sockets(client); client.sendall(b'Kx'); client.recv(4)
        with self.assertRaises(PermissionError): relay.request_identify()
        self.assertEqual(fake.sent(), [b'Kx'])

    def test_automatic_read_when_the_adapter_appears(self):
        fake = FakeHandControl(); self.addCleanup(fake.close)
        link = os.path.join(self.dir.name, 'auto')
        relay = SerialRelay('auto', 'auto', link, NEXSTAR, 0, bind='127.0.0.1').start(); self.addCleanup(relay.close)
        time.sleep(.5); self.assertEqual(fake.sent(), [])    # adapter absent: nothing to read
        os.symlink(fake.path, link)
        self.assertTrue(until(lambda: relay.snapshot()['identity'] is not None))
        self.assertEqual(relay.snapshot()['identity']['model_name'], '4/5 SE')


class TwoMountsTests(RelayCase):
    def test_two_relays_serve_two_clients_independently_and_stop_independently(self):
        fa, a, link_a = self.mount('a'); fb, b, link_b = self.mount('b', fake={'model': 12})
        self.assertNotEqual(a.tcp_port, b.tcp_port)
        ca, cb = connect(a), connect(b); self.sockets(ca, cb)
        ca.sendall(SLEW); ca.recv(4); cb.sendall(b'Kb'); self.assertEqual(cb.recv(4), b'b#')
        ca.close()
        self.assertTrue(until(lambda: len(fa.sent()) >= 1 + len(STOP)))
        self.assertEqual(fa.sent(), [SLEW] + STOP)
        time.sleep(.4)
        self.assertEqual(fb.sent(), [b'Kb'])                  # b was neither stopped nor disturbed
        cb.sendall(b'V'); self.assertEqual(cb.recv(4), bytes([4, 15]) + b'#')
        self.assertEqual(b.snapshot()['client']['address'], '127.0.0.1')

    def test_a_client_on_one_never_blocks_the_other(self):
        _, a, _ = self.mount('a'); fb, b, _ = self.mount('b')
        ca = connect(a); self.sockets(ca); ca.sendall(b'Kx'); ca.recv(4)
        cb = connect(b); self.sockets(cb); cb.sendall(b'Kx'); self.assertEqual(cb.recv(4), b'x#')
        self.assertEqual(b.snapshot()['refused_clients'], 0)

    def test_stable_links_survive_a_different_plug_order(self):
        fa, fb = FakeHandControl(model=11), FakeHandControl(model=12)
        self.addCleanup(fa.close); self.addCleanup(fb.close)
        d = self.dir.name
        stable_a, stable_b = os.path.join(d, 'by-id-A'), os.path.join(d, 'by-id-B')
        node0, node1 = os.path.join(d, 'ttyUSB0'), os.path.join(d, 'ttyUSB1')
        def plug(first, second):
            for p in (node0, node1, stable_a, stable_b):
                if os.path.lexists(p): os.unlink(p)
            os.symlink(first.path, node0); os.symlink(second.path, node1)
            os.symlink(node0 if first is fa else node1, stable_a)
            os.symlink(node1 if second is fb else node0, stable_b)
        plug(fa, fb)
        relay_a = SerialRelay('a', 'a', stable_a, NEXSTAR, 0, bind='127.0.0.1', auto_identify=False).start(); self.addCleanup(relay_a.close)
        relay_b = SerialRelay('b', 'b', stable_b, NEXSTAR, 0, bind='127.0.0.1', auto_identify=False).start(); self.addCleanup(relay_b.close)
        self.assertEqual(relay_a.request_identify()['model_code'], 11)
        self.assertEqual(relay_b.request_identify()['model_code'], 12)
        plug(fb, fa)                                           # reboot, plugged in the other order: ttyUSB0 is now B
        self.assertEqual(relay_a.request_identify()['model_code'], 11)
        self.assertEqual(relay_b.request_identify()['model_code'], 12)


if __name__ == '__main__':
    unittest.main()
