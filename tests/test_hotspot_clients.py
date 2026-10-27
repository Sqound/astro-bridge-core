"""Associated hotspot clients must not be confused with stale DHCP leases."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from wifi_bridge.network import LinuxNetwork, NetworkError


class HotspotClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.net = LinuxNetwork(self.directory)

    def test_only_associated_stations_get_current_matching_lease(self):
        (self.directory / 'hotspot.leases').write_text(
            f'{int(time.time()) + 3600} 02:00:00:00:00:01 192.168.77.12 Phone *\n'
            f'{int(time.time()) + 3600} 02:00:00:00:00:02 192.168.77.13 Old-phone *\n')
        with patch('wifi_bridge.network.run', return_value='Station 02:00:00:00:00:01 (on wlan2)\n\tinactive time: 20 ms\n\tauthorized: yes\nStation 02:00:00:00:00:02 (on wlan2)\n\tauthorized: no\n'):
            result = self.net.hotspot_clients({'name':'wlan2'})
        self.assertEqual(result, {'clients_available':True, 'clients':[{'mac':'02:00:00:00:00:01','ip':'192.168.77.12','hostname':'Phone'}]})

    def test_expired_or_foreign_addresses_are_not_attached_to_clients(self):
        (self.directory / 'hotspot.leases').write_text(
            '1 02:00:00:00:00:01 192.168.77.12 Expired *\n'
            '0 02:00:00:00:00:02 10.0.0.1 Foreign *\n'
            'nonsense\n')
        with patch('wifi_bridge.network.run', return_value='Station 02:00:00:00:00:01 (on wlan2)\nStation 02:00:00:00:00:02 (on wlan2)\n'):
            result = self.net.hotspot_clients({'name':'wlan2'})
        self.assertEqual(len(result['clients']),2)
        self.assertTrue(all(not c['ip'] and not c['hostname'] for c in result['clients']))

    def test_station_failure_is_unknown_not_a_false_empty_list(self):
        with patch('wifi_bridge.network.run', side_effect=NetworkError('unavailable')):
            self.assertEqual(self.net.hotspot_clients({'name':'wlan2'}), {'clients':[], 'clients_available':False})
        with patch('wifi_bridge.network.run', return_value=''):
            self.assertEqual(self.net.hotspot_clients({'name':'wlan2'}), {'clients':[], 'clients_available':True})
