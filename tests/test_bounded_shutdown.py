"""A requested restart or a stop starts no new link attempt, and shutdown never waits forever."""
import threading
import time
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.__main__ import close_network
from wifi_bridge.network import LinuxNetwork, NetworkError


class RestartDuringLinkTests(Fixture):
    def setUp(self):
        super().setUp()
        self.net.radios = 3
        self.devices = self.net.devices()
        self.controller.update(devices=self.devices)
        self.configured()
        self.controller.cycle(0)
        self.assertTrue(self.net.connected('telescope', self.devices[0]))

    def slow_home(self, during):
        """connect blocks on the home Wi-Fi until `during` ran, as a long nmcli command would."""
        calls = []
        original = self.net.connect

        def connect(role, *args):
            # The recovery hotspot (role setup) is outside this rule.
            if role != 'setup': calls.append(role)
            if role == 'home':
                during()
                time.sleep(0.2)
                raise NetworkError('synthetic home timeout')
            return original(role, *args)
        return calls, patch.object(self.net, 'connect', side_effect=connect)

    def test_restart_requested_during_a_long_command_starts_nothing_new_then_reboots(self):
        c = self.controller
        self.net.disconnect('home')
        self.net.disconnect('telescope')

        def request_restart():
            with c.lock:
                c.action_at = 10

        calls, slow = self.slow_home(request_restart)
        with slow, patch.object(self.net, 'reboot') as reboot:
            with c.net_lock:
                c.cycle(50)
            # The home command in progress ends, and no telescope attempt follows it.
            self.assertEqual(calls, ['home'])
            reboot.assert_not_called()
            with c.net_lock:
                c.cycle(55)
            reboot.assert_called_once_with()
        self.assertEqual(calls, ['home'])

    def test_stop_during_a_long_command_starts_nothing_new(self):
        c = self.controller
        self.net.disconnect('home')
        self.net.disconnect('telescope')
        calls, slow = self.slow_home(c.stop.set)
        with slow:
            c.cycle(50)
            c.cycle(200)
        self.assertEqual(calls, ['home'])

    def test_without_restart_or_stop_both_attempts_still_run(self):
        self.net.disconnect('home')
        self.net.disconnect('telescope')
        calls, slow = self.slow_home(lambda: None)
        with slow:
            self.controller.cycle(50)
        self.assertEqual(calls, ['home', 'telescope'])

    def test_controller_gives_the_network_its_halting_flag(self):
        net = LinuxNetwork(self.tmp.name)
        self.assertFalse(net.halting())
        controller = type(self.controller)(self.store, net, {'password': 'bootstrap-test-code'}, demo=True)
        self.assertFalse(net.halting())
        controller.stop.set()
        self.assertTrue(net.halting())


class FallbackAddressStopTests(Fixture):
    def fallback(self, net):
        sections = {}
        return net._fallback_address('telescope', {'name': 'wlan1'}, {'target_ip': '192.168.4.1', 'kind': 'synscan'}, sections)

    def test_each_address_checks_the_flag_before_its_command(self):
        net = LinuxNetwork(self.tmp.name)
        halted = []
        net.halting = lambda: bool(halted)
        commands = []

        def run(command, **_):
            commands.append(command)
            # The first address answers (duplicate), and a restart is requested meanwhile.
            halted.append(True)
            error = NetworkError('synthetic duplicate')
            error.activation = 64
            raise error

        with patch('wifi_bridge.network.run', side_effect=run), patch.object(net, '_profile', return_value='wifi-bridge-telescope'), \
                patch.object(net, 'disconnect'):
            self.assertFalse(self.fallback(net))
        self.assertEqual(len(commands), 1)

    def test_without_a_stop_every_address_is_still_tried(self):
        net = LinuxNetwork(self.tmp.name)
        commands = []

        def run(command, **_):
            commands.append(command)
            error = NetworkError('synthetic duplicate')
            error.activation = 64
            raise error

        with patch('wifi_bridge.network.run', side_effect=run), patch.object(net, '_profile', return_value='wifi-bridge-telescope'), \
                patch.object(net, 'disconnect'):
            self.assertFalse(self.fallback(net))
        self.assertEqual(len(commands), 3)


class CloseNetworkTests(Fixture):
    def test_shutdown_with_the_lock_held_ends_without_waiting_forever(self):
        closed = []

        class Net:
            def close(self): closed.append(True)

        held = threading.Event()
        release = threading.Event()

        def hold():
            with self.controller.net_lock:
                held.set()
                release.wait(5)

        worker = threading.Thread(target=hold)
        worker.start()
        self.assertTrue(held.wait(2))
        started = time.monotonic()
        try:
            with patch('builtins.print') as printed:
                self.assertFalse(close_network(self.controller, Net(), timeout=0.2))
        finally:
            release.set()
            worker.join(5)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(closed, [])
        self.assertIn('network still busy', printed.call_args.args[0])

    def test_free_lock_closes_the_network(self):
        closed = []

        class Net:
            def close(self): closed.append(True)

        self.assertTrue(close_network(self.controller, Net()))
        self.assertEqual(closed, [True])
        self.assertTrue(self.controller.net_lock.acquire(blocking=False))
        self.controller.net_lock.release()

    def test_sigterm_handler_path_with_the_lock_held_still_ends(self):
        # The signal handler only sets the stop flag. The final close is then bounded.
        closed = []

        class Net:
            def close(self): closed.append(True)

        self.controller.net_lock.acquire()
        try:
            self.controller.stop.set()
            with patch('builtins.print'):
                self.assertFalse(close_network(self.controller, Net(), timeout=0.1))
        finally:
            self.controller.net_lock.release()
        self.assertEqual(closed, [])


if __name__ == '__main__':
    import unittest
    unittest.main()
