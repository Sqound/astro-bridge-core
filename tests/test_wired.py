"""Wired admission is observed from injected NM/sysfs; never operates host NICs."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from wifi_bridge import wired
from wifi_bridge.network import split_nm, LinuxNetwork


class WiredTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        path=self.root/'enp1s0';path.mkdir();(path/'device').mkdir()
        (path/'type').write_text('1\n');(path/'carrier').write_text('1\n')
        self.status='enp1s0:ethernet:connected:wifi-bridge-wired\n'
        self.details='GENERAL.CONNECTION:wifi-bridge-wired\nGENERAL.PERM-HWADDR:02\\:00\\:00\\:00\\:00\\:01\nIP4.ADDRESS[1]:192.168.20.2/24\n'

    def run_command(self,args,**kwargs):
        return self.status if args[-1]=='status' else self.details

    def test_owned_ethernet_admitted_without_becoming_a_radio(self):
        value=wired.home(self.run_command,split_nm,self.root)
        self.assertEqual(value['kind'],'ethernet');self.assertEqual(value['address'],'192.168.20.2/24')
        self.assertNotIn('ap',value);self.assertNotIn('bands',value)

    def test_unowned_ambiguous_disconnected_and_bad_paths_are_refused(self):
        for status in ('enp1s0:ethernet:connected:foreign\n', self.status*2,
                       'enp1s0:ethernet:disconnected:wifi-bridge-wired\n',
                       '../bad:ethernet:connected:wifi-bridge-wired\n'):
            self.status=status;self.assertIsNone(wired.home(self.run_command,split_nm,self.root))

    def test_stale_carrier_wireless_and_unroutable_address_are_refused(self):
        (self.root/'enp1s0/carrier').write_text('0')
        self.assertIsNone(wired.home(self.run_command,split_nm,self.root))
        (self.root/'enp1s0/carrier').write_text('1');(self.root/'enp1s0/wireless').mkdir()
        self.assertIsNone(wired.home(self.run_command,split_nm,self.root))
        (self.root/'enp1s0/wireless').rmdir();self.details=self.details.replace('192.168.20.2/24','169.254.1.2/16')
        self.assertIsNone(wired.home(self.run_command,split_nm,self.root))

    def test_connected_rechecks_actual_identity_without_iw_or_mutations(self):
        value=wired.home(self.run_command,split_nm,self.root)
        backend=object.__new__(LinuxNetwork)
        with patch.object(backend,'wired_home',return_value=value),patch('wifi_bridge.network.run') as command:
            self.assertTrue(backend.connected('home',value,{'ssid':'synthetic'}))
            self.assertFalse(backend.connected('telescope',value))
            self.assertFalse(backend.connected('home',dict(value,address='192.168.20.3/24')))
            command.assert_not_called()


if __name__=='__main__':unittest.main()
