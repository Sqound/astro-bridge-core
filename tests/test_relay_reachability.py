"""What a client must do, and what the bridge does, for a relayed mount port to answer.

A patched command runner only: no packet, no hardware, no real network.
"""
import tempfile
import unittest
from unittest.mock import patch

from wifi_bridge.network import LinuxNetwork, NetworkError

ADDRESSES = {'scope0': '192.168.4.2/24', 'home0': '192.168.12.2/24', 'ap0': '192.168.77.1/24'}
HOME, AP, SCOPE = ({'name': 'home0', 'mac': '02:00:00:00:00:01'}, {'name': 'ap0', 'mac': '02:00:00:00:00:02'},
                   {'name': 'scope0', 'mac': '02:00:00:00:00:03'})


class RelayRuleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.net = LinuxNetwork(self.folder.name)
        self.calls = []

    def publish(self, kind, fail=()):
        def command(args, **kwargs):
            self.calls.append((list(args), kwargs.get('input')))
            if args[0] in fail: raise NetworkError('System command unavailable or timed out: ' + args[0])
            return ''
        route = {'kind': kind, 'device': SCOPE, 'target': '192.168.4.1', kind + '_proxy': True}
        with patch.object(self.net, 'address', side_effect=lambda d, **kw: ADDRESSES[d['name']]), \
                patch.object(self.net, 'configure_activity'), patch('wifi_bridge.network.run', side_effect=command):
            self.net.firewall(home=HOME, setup=AP, telescopes=[route])
        return next((text for args, text in reversed(self.calls) if args == ['nft', '-f', '-']), None)

    def forgotten(self):
        return [args for args, _ in self.calls if args[0] == 'conntrack']

    def test_relay_needs_no_reservation_only_the_management_interface_and_network(self):
        script = self.publish('synscan')
        # Nothing but the entry interface, the client's network and the bridge's own address is asked of a client.
        self.assertIn('iifname "home0" ip saddr 192.168.12.0/24 ip daddr 192.168.12.2 udp dport 11880 dnat ip to 192.168.4.1', script)
        self.assertIn('iifname "ap0" ip saddr 192.168.77.0/24 ip daddr 192.168.77.1 udp dport 11880 dnat ip to 192.168.4.1', script)
        self.assertIn('iifname "home0" oifname "scope0" ip saddr 192.168.12.0/24 ip daddr 192.168.4.1 accept', script)
        # The reply is accepted whichever interface routes to the client (cable and Wi-Fi on one network),
        # before the interface drops of the instrument, and only for an established flow.
        reply = 'iifname "scope0" ip daddr 192.168.12.0/24 ct state established,related accept'
        self.assertIn(reply, script); self.assertNotIn('oifname "home0" ip daddr 192.168.12.0/24', script)
        self.assertLess(script.index(reply), script.index('iifname "scope0" drop'))
        # No rule of the bridge filters what enters for the bridge itself, except its DNS port.
        chain = script[script.index('chain dns_input'):script.index('}', script.index('chain dns_input'))]
        self.assertNotIn('11880', chain); self.assertEqual(chain.count(' drop'), 2)

    def test_datagrams_that_came_before_the_relay_do_not_keep_a_client_out(self):
        self.publish('synscan')
        order = [args[0] for args, _ in self.calls if args[0] in ('nft', 'conntrack')]
        self.assertLess(max(i for i, name in enumerate(order) if name == 'nft'), order.index('conntrack'))
        self.assertEqual(self.forgotten(), [
            ['conntrack', '-D', '-p', 'udp', '--orig-dst', address, '--orig-port-dst', '11880', '--reply-src', address]
            for address in ('192.168.12.2', '192.168.77.1')])
        # The same plan again changes nothing, so nothing is forgotten again.
        self.calls.clear()
        self.assertIsNone(self.publish('synscan'))
        self.assertEqual(self.forgotten(), [])

    def test_stream_relays_and_a_missing_tool_are_left_alone(self):
        self.publish('zwo_mount')
        self.assertEqual(self.forgotten(), [])
        self.net.last_firewall = None; self.calls.clear()
        script = self.publish('synscan', fail=('conntrack',))
        self.assertIn('udp dport 11880 dnat ip to 192.168.4.1', script)
        self.assertEqual(self.net.last_firewall, script)


if __name__ == '__main__':
    unittest.main()
