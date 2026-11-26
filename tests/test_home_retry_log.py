"""Service-log lines for the home Wi-Fi retry: one per change of reason, never per cycle."""
import contextlib
import io
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.controller import home_wifi_log
from wifi_bridge.network import LinuxNetwork, NetworkError


class HomeRetryLogTests(Fixture):
    def setUp(self):
        super().setUp()
        self.net.radios = 3
        self.devices = self.net.devices()
        self.controller.update(devices=self.devices)
        self.configured()
        self.controller.home_log = home_wifi_log
        self.log = io.StringIO()
        self.cycle(0)
        home_mac = self.store.config['home']['mac']
        self.home = next(d for d in self.devices if d['mac'] == home_mac)
        self.boot = self.lines()
        self.log = io.StringIO()

    def cycle(self, now):
        with contextlib.redirect_stdout(self.log):
            self.controller.cycle(now)

    def lines(self):
        return [line for line in self.log.getvalue().splitlines() if line.startswith('Astro Bridge home Wi-Fi: ')]

    def test_first_connection_writes_two_lines_then_nothing(self):
        name = self.home['name']
        self.assertEqual(self.boot, ['Astro Bridge home Wi-Fi: trying the home Wi-Fi on ' + name + '.',
                                     'Astro Bridge home Wi-Fi: the home Wi-Fi is connected on ' + name + '.'])
        for now in (5, 10, 100):
            self.cycle(now)
        self.assertEqual(self.lines(), [])

    def test_home_already_up_when_the_service_starts_writes_nothing(self):
        self.controller.home_retry_logged = None
        self.cycle(5)
        self.assertEqual(self.lines(), [])

    def test_outage_writes_one_line_per_change_and_never_per_cycle(self):
        name = self.home['name']
        self.net.disconnect('home')
        original = self.net.connect

        def refuse(role, *args):
            if role == 'home': raise NetworkError('synthetic unavailable home')
            return original(role, *args)

        with patch.object(self.net, 'connect', side_effect=refuse):
            for now in (50, 55, 60, 95, 100, 140):
                self.cycle(now)
        self.assertEqual(self.lines(), [
            'Astro Bridge home Wi-Fi: trying the home Wi-Fi on ' + name + '.',
            'Astro Bridge home Wi-Fi: the home Wi-Fi retry on ' + name + ' failed. It is tried again every 45 seconds.'])
        # The radio stays busy: one line with what radio_idle read, then nothing while it lasts.
        self.net.radio_observations = {name: {'state': '100', 'link': 'connected'}}
        with patch.object(self.net, 'radio_idle', return_value=False):
            for now in (200, 205, 300, 600):
                self.cycle(now)
        self.assertEqual(self.controller.state['management']['home_retry']['reason'], 'HOME_RETRY_DEFERRED_RADIO_BUSY')
        self.assertEqual(self.lines()[2:], [
            'Astro Bridge home Wi-Fi: the home Wi-Fi retry on ' + name
            + ' is deferred: the radio is busy (NetworkManager state 100, iw link connected).'])
        self.cycle(700)
        self.cycle(705)
        self.assertEqual(self.lines()[3:], [
            'Astro Bridge home Wi-Fi: trying the home Wi-Fi on ' + name + '.',
            'Astro Bridge home Wi-Fi: the home Wi-Fi is connected on ' + name + '.'])
        text = self.log.getvalue()
        self.assertNotIn(self.store.config['home']['ssid'], text)
        self.assertNotIn(self.store.config['home'].get('password') or 'synthetic-pass', text)

    def test_busy_radio_without_observation_says_so(self):
        self.net.disconnect('home')
        with patch.object(self.net, 'radio_idle', return_value=False):
            self.cycle(50)
            self.cycle(55)
        self.assertEqual(self.lines(), ['Astro Bridge home Wi-Fi: the home Wi-Fi retry on ' + self.home['name']
                                        + ' is deferred: the radio is busy (radio state not read).'])

    def test_demo_mode_writes_no_home_line(self):
        self.controller.home_log = lambda message: None
        self.net.disconnect('home')
        with patch.object(self.net, 'radio_idle', return_value=False):
            self.cycle(50)
        self.assertEqual(self.lines(), [])


class RadioObservationTests(Fixture):
    def test_radio_idle_keeps_what_it_read_without_another_command(self):
        net = LinuxNetwork(self.tmp.name)
        answers = {'nmcli': '100 (connected)', 'link': 'Connected to 02:00:00:11:22:33 (on wlan0)\n\tSSID: Maison',
                   'info': '\ttype managed'}
        calls = []

        def run(command, **_):
            calls.append(command)
            return answers['nmcli'] if command[0] == 'nmcli' else answers[command[-1]]

        with patch('wifi_bridge.network.run', side_effect=run), patch.object(net, '_hotspot_device', return_value=''):
            self.assertFalse(net.radio_idle({'name': 'wlan0', 'mac': '02:00:00:00:00:01'}))
        self.assertEqual(len(calls), 3)
        self.assertEqual(net.radio_observations, {'wlan0': {'state': '100', 'link': 'connected'}})
        answers.update(nmcli='30 (disconnected)', link='Not connected.')
        with patch('wifi_bridge.network.run', side_effect=run), patch.object(net, '_hotspot_device', return_value=''):
            self.assertTrue(net.radio_idle({'name': 'wlan0', 'mac': '02:00:00:00:00:01'}))
        self.assertEqual(net.radio_observations['wlan0'], {'state': '30', 'link': 'not connected'})


if __name__ == '__main__':
    import unittest
    unittest.main()
