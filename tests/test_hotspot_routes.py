"""Actual private DHCP configuration publication and controller update boundaries."""
import ipaddress
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.api import MobileAPI
from wifi_bridge.network import LinuxNetwork, NetworkError, hotspot_route_targets


class HotspotRouteTests(unittest.TestCase):
    def test_host_targets_are_private_deduplicated_and_exclude_management_local_and_unsafe(self):
        targets=['192.168.88.1','10.0.0.1','192.168.88.1','172.16.4.2','192.168.77.1',
                 '192.168.77.50','192.168.1.90','10.0.0.20','10.0.0.0','10.0.0.255',
                 '8.8.8.8','127.0.0.1','169.254.1.1','224.0.0.1','0.0.0.0',
                 '255.255.255.255','240.1.2.3','192.0.2.1','::1','broken\noption',None]
        result=hotspot_route_targets(targets,[ipaddress.IPv4Network('192.168.1.0/24')],
            {ipaddress.IPv4Address(ip) for ip in ('10.0.0.20','10.0.0.0','10.0.0.255')})
        self.assertEqual(result,['10.0.0.1','172.16.4.2','192.168.88.1'])

    def test_maximum_twenty_four_routes_and_default_fit_one_option_without_broad_subnets(self):
        with tempfile.TemporaryDirectory() as directory:
            net=LinuxNetwork(directory);targets=['10.8.0.'+str(n) for n in range(1,25)]
            with patch('wifi_bridge.network.run'):
                net.hotspot_dhcp({'name':'wlan0'},targets+targets,restart=False)
            config=Path(directory,'dnsmasq.conf').read_text()
            option=next(line for line in config.splitlines() if line.startswith('dhcp-option=121,'))
            values=option.split(',')[1:];routes=list(zip(values[::2],values[1::2]))
            self.assertEqual(len(routes),25);self.assertEqual(routes[-1],('0.0.0.0/0','192.168.77.1'))
            self.assertTrue(all(prefix.endswith('/32') for prefix,gateway in routes[:-1]))
            self.assertTrue(all(gateway=='192.168.77.1' for prefix,gateway in routes))
            # RFC3442: prefix byte + significant destination bytes + four gateway bytes.
            length=sum(1+(ipaddress.IPv4Network(prefix).prefixlen+7)//8+4 for prefix,gateway in routes)
            self.assertEqual(length,221);self.assertLess(length,256)
            self.assertIn('dhcp-option=3,192.168.77.1\n',config);self.assertIn('dhcp-option=6\n',config)
            with self.assertRaises(NetworkError):
                hotspot_route_targets(targets+['10.8.0.25'])

    def test_initial_hotspot_contains_saved_routes_before_services_start(self):
        with tempfile.TemporaryDirectory() as directory:
            net=LinuxNetwork(directory);seen=[]
            def run(args,**kwargs):
                if args==['systemctl','restart','wifi-bridge-ap','wifi-bridge-dhcp']:
                    seen.append(Path(directory,'dnsmasq.conf').read_text())
                return ''
            with patch('wifi_bridge.network.run',side_effect=run),patch.object(net,'connected',return_value=True):
                net.hotspot({'name':'wlan0'},'abcdef','fictional-test-password',['192.168.88.1'])
            self.assertEqual(len(seen),1)
            self.assertIn('dhcp-option=121,192.168.88.1/32,192.168.77.1,0.0.0.0/0,192.168.77.1\n',seen[0])
            self.assertNotIn('fictional-test-password',seen[0])

    def test_update_checks_staged_config_and_restarts_only_dhcp_once_then_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            net=LinuxNetwork(directory);device={'name':'wlan0'};path=Path(directory,'dnsmasq.conf')
            with patch('wifi_bridge.network.run'):
                net.hotspot_dhcp(device,['10.0.0.1'],restart=False)
            previous=path.read_text();calls=[]
            def run(args,**kwargs):
                calls.append(args)
                if args[0]=='/usr/sbin/dnsmasq':
                    self.assertEqual(path.read_text(),previous)
                    staged=Path(args[-1].removeprefix('--conf-file='))
                    self.assertIn('192.168.88.1/32',staged.read_text())
                    self.assertEqual(staged.stat().st_mode&0o777,0o600)
                return ''
            with patch('wifi_bridge.network.run',side_effect=run):
                self.assertTrue(net.hotspot_dhcp(device,['192.168.88.1','10.0.0.1']))
                self.assertFalse(net.hotspot_dhcp(device,['10.0.0.1','192.168.88.1','10.0.0.1']))
            self.assertEqual(len(calls),2);self.assertEqual(calls[-1],['systemctl','restart','wifi-bridge-dhcp'])
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertFalse(Path(directory,'dnsmasq.conf.pending').exists())

    def test_invalid_syntax_or_failed_restart_preserves_previous_config_without_link_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            net=LinuxNetwork(directory);device={'name':'wlan0'};path=Path(directory,'dnsmasq.conf')
            with patch('wifi_bridge.network.run'):net.hotspot_dhcp(device,['10.0.0.1'],restart=False)
            previous=path.read_bytes()
            with patch('wifi_bridge.network.run',side_effect=NetworkError('synthetic syntax failure')) as run:
                with self.assertRaises(NetworkError):net.hotspot_dhcp(device,['192.168.88.1'])
            self.assertEqual(run.call_count,1);self.assertEqual(path.read_bytes(),previous)
            with patch('wifi_bridge.network.run',side_effect=['',NetworkError('synthetic start failure'),'']) as run:
                with self.assertRaises(NetworkError):net.hotspot_dhcp(device,['192.168.88.1'])
            self.assertEqual(path.read_bytes(),previous)
            self.assertEqual([c.args[0] for c in run.call_args_list[1:]],
                [['systemctl','restart','wifi-bridge-dhcp']]*2)
            self.assertFalse(Path(directory,'dnsmasq.conf.pending').exists())

    def test_dhcp_only_restart_uses_both_platform_service_adapters(self):
        with tempfile.TemporaryDirectory() as directory:
            net=LinuxNetwork(directory)
            for openrc in (False,True):
                with patch('wifi_bridge.network.using_openrc',return_value=openrc),patch('wifi_bridge.network.subprocess.run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
                    net.hotspot_dhcp({'name':'wlan0'},['10.0.0.1' if openrc else '192.168.88.1'])
                commands=[call.args[0] for call in run.call_args_list]
                self.assertEqual(commands[-1],['/sbin/rc-service','wifi-bridge-dhcp','restart'] if openrc else ['systemctl','restart','wifi-bridge-dhcp'])
                self.assertTrue(all('wifi-bridge-ap' not in command for command in commands))

    def test_failed_first_publication_removes_config_and_later_retry_restarts_service(self):
        with tempfile.TemporaryDirectory() as directory:
            net=LinuxNetwork(directory);device={'name':'wlan0'};path=Path(directory,'dnsmasq.conf')
            with patch('wifi_bridge.network.run',side_effect=['',NetworkError('synthetic first start failure')]):
                with self.assertRaises(NetworkError):net.hotspot_dhcp(device,['192.168.88.1'])
            self.assertFalse(path.exists())
            with patch('wifi_bridge.network.run') as run:
                self.assertTrue(net.hotspot_dhcp(device,['192.168.88.1']))
            self.assertEqual(run.call_args.args[0],['systemctl','restart','wifi-bridge-dhcp'])
            self.assertTrue(path.exists())


class HotspotControllerTests(Fixture):
    def setUp(self):
        super().setUp();self.net.radios=3;self.devices=self.net.devices();self.controller.update(devices=self.devices)
        self.configured();self.api=MobileAPI(self.controller)
        self.api.update('hotspot',{'enabled':True});self.controller.cycle(0)
        self.publisher=LinuxNetwork(self.tmp.name)

    def add_saved(self):
        return self.api.update('telescopes',{'id':'saved-dwarf','name':'Fictional saved telescope','kind':'dwarf',
            'ssid':'Fictional-scope','open':True,'target_ip':'192.168.88.1'},create=True)

    def test_saved_inactive_targets_updated_without_disrupting_protected_connections_or_ap(self):
        c=self.controller;c.store.config['telescopes'][0]['owner_locked']=True
        ap=c.state['hotspot']['mac'];self.add_saved();before=c.event_store.sequence
        with patch.object(self.net,'hotspot_dhcp',side_effect=self.publisher.hotspot_dhcp),patch('wifi_bridge.network.run') as run,patch.object(self.net,'hotspot') as hotspot,patch.object(self.net,'disconnect',wraps=self.net.disconnect) as disconnect:
            c.cycle(10);first=run.call_count;c.cycle(15)
            self.assertEqual(run.call_count,first)
        hotspot.assert_not_called();disconnect.assert_not_called()
        self.assertEqual(c.state['hotspot']['mac'],ap);self.assertTrue(c.state['telescopes'][0]['relay_ready'])
        self.assertTrue(c.protection.protected('primary'))
        self.assertIn('121,10.0.0.1/32,192.168.77.1,192.168.88.1/32,192.168.77.1,0.0.0.0/0,192.168.77.1',Path(self.tmp.name,'dnsmasq.conf').read_text())
        self.assertFalse(any(e['type'] in ('disconnected','connected','requested') for e in c.event_store.events if e['sequence']>before))

    def test_rejected_but_associated_home_subnet_is_not_advertised_as_telescope_route(self):
        c=self.controller;self.add_saved();original=self.net.address
        def address(device,timeout=20):return '10.0.0.90/24' if device['mac']==self.devices[1]['mac'] else original(device,timeout=timeout)
        c.store.config['telescopes'][0]['owner_locked']=True
        with patch.object(self.net,'address',side_effect=address),patch.object(self.net,'hotspot_dhcp',side_effect=self.publisher.hotspot_dhcp),patch('wifi_bridge.network.run'):
            c.cycle(10)
        text=Path(self.tmp.name,'dnsmasq.conf').read_text()
        self.assertNotIn('10.0.0.1/32',text);self.assertIn('192.168.88.1/32',text)
        self.assertTrue(self.net.connected('telescope',self.devices[0]))
        self.assertFalse(c.state['telescopes'][0]['relay_ready'])

    def test_manual_switch_between_saved_targets_does_not_change_dhcp_or_restart_ap(self):
        c=self.controller;self.add_saved();ap=c.state['hotspot']['mac']
        with patch.object(self.net,'hotspot_dhcp',side_effect=self.publisher.hotspot_dhcp),patch('wifi_bridge.network.run') as run:
            c.cycle(5);before=Path(self.tmp.name,'dnsmasq.conf').read_bytes();run.reset_mock()
            self.api.action('primary','disconnect')
            self.api.action('saved-dwarf','connect',data={'mac':self.devices[0]['mac']})
            with patch.object(self.net,'hotspot') as hotspot:c.cycle(10)
        run.assert_not_called();hotspot.assert_not_called()
        self.assertEqual(Path(self.tmp.name,'dnsmasq.conf').read_bytes(),before)
        self.assertEqual(c.state['hotspot']['mac'],ap)
        rows={row['id']:row for row in c.state['telescopes']}
        self.assertTrue(rows['saved-dwarf']['relay_ready']);self.assertFalse(rows['primary']['connected'])
