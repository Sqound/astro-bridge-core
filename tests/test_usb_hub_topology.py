"""A USB Wi-Fi adapter plugged through a hub is told apart from one on a board port.

Fake sysfs trees only: no USB device is read.
"""
import tempfile
import unittest
from pathlib import Path

from wifi_bridge.network import behind_usb_hub


class UsbHubTopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'devices/platform/axi/xhci-hcd.0/usb1'

    def usb(self, relative, klass='00', vendor='0bda', product='c811'):
        folder = self.root / relative
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'bDeviceClass').write_text(klass + '\n')
        (folder / 'idVendor').write_text(vendor + '\n'); (folder / 'idProduct').write_text(product + '\n')
        return folder

    def interface(self, adapter):
        folder = adapter / (adapter.name + ':1.0')
        folder.mkdir()
        return folder

    def test_adapter_on_a_board_port(self):
        # Raspberry Pi 5: the ports hang from the root hub.
        self.assertFalse(behind_usb_hub(self.interface(self.usb('1-1'))))

    def test_adapter_behind_an_external_hub(self):
        self.usb('1-1', klass='09', vendor='1a40', product='0101')
        self.assertTrue(behind_usb_hub(self.interface(self.usb('1-1/1-1.2'))))
        self.usb('1-2', klass='09', vendor='05e3', product='0610'); self.usb('1-2/1-2.4', klass='09', vendor='05e3', product='0610')
        self.assertTrue(behind_usb_hub(self.interface(self.usb('1-2/1-2.4/1-2.4.1'))))

    def test_board_hubs_of_older_raspberry_pi_are_not_counted(self):
        # Raspberry Pi 4 USB 2.0 ports sit behind its own VL805 hub.
        self.usb('1-1', klass='09', vendor='2109', product='3431')
        self.assertFalse(behind_usb_hub(self.interface(self.usb('1-1/1-1.3'))))
        # The same board, with an external hub on one of its ports.
        self.usb('1-1/1-1.4', klass='09', vendor='1a40', product='0101')
        self.assertTrue(behind_usb_hub(self.interface(self.usb('1-1/1-1.4/1-1.4.2'))))

    def test_unreadable_parents_are_not_a_hub(self):
        (self.root / '1-1').mkdir(parents=True)
        self.assertFalse(behind_usb_hub(self.interface(self.usb('1-1/1-1.1'))))


if __name__ == '__main__':
    unittest.main()
