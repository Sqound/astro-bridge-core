"""Readiness is evidence from the radio, not only running AP services."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from wifi_bridge.network import LinuxNetwork, NetworkError


class HotspotRadioReadiness(unittest.TestCase):
    def test_processes_and_ip_do_not_prove_ap_broadcast(self):
        with tempfile.TemporaryDirectory() as tmp:
            net = LinuxNetwork(tmp)
            Path(tmp, 'hotspot.json').write_text('{"interface":"wlan0"}')
            device = {'name':'wlan0'}
            def check(kind, status):
                def command(args, **kwargs):
                    if args[0] == 'systemctl': return 'active\nactive'
                    if args[0] == 'iw': return 'Interface wlan0\n\ttype ' + kind + '\n'
                    if args[0] == 'hostapd_cli': return 'state=' + status + '\n'
                    raise AssertionError(args)
                with patch('wifi_bridge.network.run', side_effect=command), patch.object(net, 'address', return_value='192.168.77.1/24'):
                    return net.connected('setup', device)
            self.assertTrue(check('AP', 'ENABLED'))
            self.assertFalse(check('managed', 'ENABLED'))
            self.assertFalse(check('AP', 'DISABLED'))
            self.assertFalse(check('AP', ''))
            with patch('wifi_bridge.network.run', side_effect=NetworkError('unavailable')):
                self.assertFalse(net.connected('setup', device))

    def test_waits_for_networkmanager_before_start_and_accepts_delayed_ap(self):
        with tempfile.TemporaryDirectory() as tmp:
            net = LinuxNetwork(tmp); calls = []; states = iter(['30 (disconnected)', '10 (unmanaged)'])
            def command(args, **kwargs):
                calls.append(args)
                if args == ['nmcli','-g','GENERAL.STATE','device','show','wlan0']: return next(states)
                return ''
            with patch('wifi_bridge.network.run', side_effect=command), patch('wifi_bridge.network.time.sleep'), patch.object(net, 'connected', side_effect=[False, True]):
                net.hotspot({'name':'wlan0'}, 'Astro', 'password-example')
            self.assertLess(calls.index(['nmcli','-g','GENERAL.STATE','device','show','wlan0']),
                            calls.index(['systemctl','restart','wifi-bridge-ap','wifi-bridge-dhcp']))
            self.assertEqual(sum(c[0] == 'nmcli' and 'GENERAL.STATE' in c for c in calls), 2)

    def test_failure_to_release_does_not_start_services_and_cleans_own_partial_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            net = LinuxNetwork(tmp); calls = []
            def command(args, **kwargs):
                calls.append(args)
                if args == ['nmcli','-g','GENERAL.STATE','device','show','wlan0']: return '30 (disconnected)'
                return ''
            with patch('wifi_bridge.network.run', side_effect=command), patch('wifi_bridge.network.time.sleep'):
                with self.assertRaises(NetworkError): net.hotspot({'name':'wlan0'}, 'Astro', 'password-example')
            self.assertNotIn(['systemctl','restart','wifi-bridge-ap','wifi-bridge-dhcp'], calls)
            self.assertFalse(Path(tmp, 'hotspot.json').exists())

    def test_unverified_existing_owner_is_never_released_or_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            net = LinuxNetwork(tmp)
            marker = Path(tmp, 'hotspot.json'); marker.write_text('{"interface":"wlan0"}')
            with patch.object(net, 'connected', return_value=False), patch.object(net, 'disconnect') as disconnect, patch('wifi_bridge.network.run') as command:
                with self.assertRaises(NetworkError):
                    net.hotspot({'name':'wlan1'}, 'Astro', 'password-example')
            disconnect.assert_not_called()
            command.assert_not_called()
            self.assertTrue(marker.exists())

    def test_unready_ap_is_not_accepted_after_bounded_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            net = LinuxNetwork(tmp)
            with patch('wifi_bridge.network.time.sleep'), patch.object(net, 'connected', return_value=False) as connected:
                with self.assertRaises(NetworkError): net._wait_hotspot_ready({'name':'wlan0'})
            self.assertEqual(connected.call_count, 8)
