"""OpenRC command translation preserves existing systemd call semantics."""
import subprocess
import unittest
from wifi_bridge.init_system import openrc_command


class InitTests(unittest.TestCase):
    def test_status_returns_one_active_line_per_service(self):
        calls = []
        def execute(args):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0 if args[1] == 'wifi-bridge-ap' else 3, '', '')
        result = openrc_command(['systemctl','is-active','wifi-bridge-ap','wifi-bridge-dhcp'],execute)
        self.assertEqual(result.stdout,'active\ninactive\n')
        self.assertEqual(calls[1],['/sbin/rc-service','wifi-bridge-dhcp','status'])

    def test_actions_password_and_reboot_use_argument_lists(self):
        calls = []
        def execute(args):
            calls.append(args)
            return subprocess.CompletedProcess(args,0,'','')
        openrc_command(['systemctl','restart','wifi-bridge-ap','wifi-bridge-dhcp'],execute)
        openrc_command(['systemctl','start','wifi-bridge-password'],execute)
        openrc_command(['systemctl','reboot'],execute)
        self.assertEqual(calls,[['/sbin/rc-service','wifi-bridge-ap','restart'],['/sbin/rc-service','wifi-bridge-dhcp','restart'],['/sbin/rc-service','wifi-bridge-password','restart'],['/sbin/reboot']])
        with self.assertRaises(ValueError):
            openrc_command(['systemctl','enable','arbitrary'],execute)
