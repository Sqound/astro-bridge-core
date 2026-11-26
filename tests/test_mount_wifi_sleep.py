"""A SynScan mount left without a client may stop its own Wi-Fi: the bridge keeps it joined and says so.

Simulated radios only (DemoNetwork) and a fake socket: no command reaches any
hardware. "primary" plays the linked SynScan mount, "second" the instrument an
app asks for next.
"""
import time
import unittest
from unittest.mock import patch

from test_radio_release import FIRST, SECOND, SPEC, RadioFixture
from helpers.contract_schema import validate
from wifi_bridge import mounts, readiness
from wifi_bridge.mount_sleep import SILENT_AFTER_SECONDS, silence, wifi_sleep


class MountFixture(RadioFixture):
    def setUp(self):
        super().setUp()
        self.profile(FIRST)['kind'] = 'synscan'
        self.probes = []
        self.disconnects = []
        disconnect = self.net.disconnect

        def recorded(role, *args, **kwargs):
            self.disconnects.append((role, self.now))
            return disconnect(role, *args, **kwargs)
        self.net.disconnect = recorded
        # The service check runs as on a real bridge; its only exchange is recorded here.
        self.controller.service_readiness.demo = False

        def probe(device, address, target, kind):
            self.probes.append((self.now, kind, target))
            return True
        patcher = patch.object(readiness, 'service_probe', side_effect=probe)
        patcher.start(); self.addCleanup(patcher.stop)

    def linked_then_released(self):
        code, value = self.connect(FIRST); self.assertEqual(code, 200, value)
        self.facade.action(FIRST, 'release', {'lease_id': value['lease']['id']}, 'paired-client')
        self.tick()


class KeptJoinedTests(MountFixture):
    def test_released_mount_stays_joined_and_only_answers_the_read_only_check(self):
        self.linked_then_released()
        self.disconnects.clear(); self.probes.clear()
        start = self.now
        # A quarter of an hour, longer than the 11 to 12 minutes after which the mount was seen to stop.
        while self.now - start < 900:
            self.tick()
            self.assertTrue(self.row(FIRST)['connected'], self.now)
        self.assertTrue(self.profile(FIRST)['enabled'])
        self.assertFalse([d for d in self.disconnects if d[0] == 'telescope'])
        self.assertEqual({kind for _, kind, _ in self.probes}, {'synscan'})
        times = [at for at, _, _ in self.probes]
        self.assertGreaterEqual(len(times), 29)
        self.assertLessEqual(max(b - a for a, b in zip(times, times[1:])), 35)
        self.assertNotIn('silence', self.item(FIRST))

    def test_the_check_sends_the_version_query_and_nothing_else(self):
        sent = []

        class Socket:
            def __init__(self, *args): pass
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def setsockopt(self, *args): pass
            def bind(self, *args): pass
            def settimeout(self, *args): pass
            def sendto(self, data, address): sent.append(data)
            def recvfrom(self, size): return b'=031102\r', ('192.168.4.1', 11880)
        with patch.object(mounts.socket, 'socket', Socket):
            result = mounts.mount_probe({'name': 'wlan2'}, '192.168.4.2/24', '192.168.4.1', 'synscan')
        self.assertEqual(sent, [b':e1\r'])
        self.assertEqual(result['protocol_identity']['family'], 'synscan')


class RefusalTests(MountFixture):
    def test_refusal_names_the_mount_and_says_it_may_stop_its_wifi(self):
        self.linked_then_released()
        with self.assertRaises(Exception) as error: self.connect()
        self.assertEqual(error.exception.code, 'NO_FREE_RADIO')
        self.assertIn('switch its Wi-Fi off', error.exception.message)
        details = self.refused()
        holder = details['holders'][0]
        self.assertEqual((holder['instrument_id'], holder['kind'], holder['state'], holder['forceable']),
                         (FIRST, 'mount', 'in_service', True))
        self.assertEqual(holder['wifi_sleep'], wifi_sleep('synscan'))
        self.assertEqual((holder['wifi_sleep']['sign'], holder['wifi_sleep']['recovery'], holder['wifi_sleep']['documented_by_maker']),
                         ('STATUS_LIGHT_STEADY', 'POWER_CYCLE', False))
        # Never given away silently: still joined after the refusal.
        self.assertTrue(self.row(FIRST)['connected']); self.assertTrue(self.profile(FIRST)['enabled'])

    def test_a_telescope_holder_carries_no_wifi_sleep(self):
        self.profile(FIRST)['kind'] = 'seestar'
        code, value = self.connect(FIRST); self.assertEqual(code, 200, value)
        with patch.object(self.controller, 'operation_state', return_value='running'):
            holder = self.refused()['holders'][0]
        self.assertNotIn('wifi_sleep', holder)

    def test_confirmed_replacement_is_named_and_warns_of_wifi_sleep(self):
        self.linked_then_released()
        code, value = self.connect(wait_seconds=5, force_replace=[FIRST])
        self.assertEqual(code, 200, value)
        validate(value, SPEC['components']['schemas']['NativeReady'], SPEC)
        self.assertEqual((value['displaced']['instrument_id'], value['displaced']['reason'], value['displaced']['kind']),
                         (FIRST, 'FORCED', 'mount'))
        self.assertEqual(value['displaced']['wifi_sleep']['recovery'], 'POWER_CYCLE')
        operation = self.facade.operation(self.ops.last, 'home')
        validate(operation, SPEC['components']['schemas']['NativeOperation'], SPEC)
        self.assertEqual(operation['displaced']['wifi_sleep'], wifi_sleep('synscan'))
        self.assertEqual(self.item(FIRST)['released']['reason'], 'REPLACED_ON_REQUEST')


class SilenceTests(MountFixture):
    def heard(self, ago):
        discovery = self.controller.candidates
        ssid = self.profile(FIRST)['ssid']
        discovery.observations = {'a' * 32: {'ssid': ssid, 'bssid': self.profile(FIRST).get('bssid') or '02:00:00:00:00:aa',
                                             'signal': 60, 'frequency': 2437, 'security': '', 'observed_at': int(discovery.wall()) - ago}}

    def test_left_without_the_bridge_and_no_longer_heard_is_probably_asleep(self):
        code, value = self.connect(wait_seconds=5, force_replace=[FIRST])
        self.assertEqual(code, 200, value)
        # The bridge left it ten minutes ago, and its network was last heard 700 s ago.
        self.controller.mount_contact[FIRST] = self.controller.cycle_now - 900
        self.heard(700)
        item = self.item(FIRST)
        self.assertEqual((item['silence']['probable_cause'], item['silence']['last_heard'], item['silence']['cause_established']),
                         ('MOUNT_WIFI_SLEEP', 'scan', False))
        self.assertGreaterEqual(item['silence']['since_seconds'], 700)
        self.assertEqual((item['silence']['sign'], item['silence']['recovery']), ('STATUS_LIGHT_STEADY', 'POWER_CYCLE'))

    def test_silent_while_joined_has_no_probable_cause(self):
        code, value = self.connect(FIRST); self.assertEqual(code, 200, value)
        self.tick()
        self.absent.add('telescope'); self.net.disconnect('telescope')
        for _ in range(20): self.tick()
        item = self.item(FIRST)
        self.assertEqual((item['silence']['probable_cause'], item['silence']['last_heard']), ('UNKNOWN', 'link'))
        self.assertIsNone(item['silence']['recovery'])
        self.assertGreaterEqual(item['silence']['since_seconds'], SILENT_AFTER_SECONDS)

    def test_answering_mount_has_no_silence(self):
        code, value = self.connect(FIRST); self.assertEqual(code, 200, value)
        self.heard(5000)
        for _ in range(30): self.tick()
        self.assertNotIn('silence', self.item(FIRST))

    def test_unobserved_mount_operation_carries_the_silence(self):
        # No radio is taken: the mount is linked from the nearby list, not heard any more.
        self.facade.action(FIRST, 'disconnect', {}, 'paired-client')
        self.tick()
        self.heard(800)
        scan = self.net.scan
        ssid = self.profile(FIRST)['ssid']
        self.net.scan = lambda *args, **kwargs: [row for row in scan(*args, **kwargs) if row['ssid'] != ssid]
        code, value = self.connect(FIRST)
        if code == 202:
            self.finish()
            operation = self.facade.operation(value['operation']['id'], 'home')
        else:
            self.fail(value)
        validate(operation, SPEC['components']['schemas']['NativeOperation'], SPEC)
        self.assertEqual((operation['state'], operation['reason']), ('failed', 'NETWORK_UNOBSERVED'))
        self.assertEqual(operation['silence']['probable_cause'], 'MOUNT_WIFI_SLEEP')


class SilenceRuleTests(unittest.TestCase):
    def test_rule(self):
        self.assertIsNone(silence('seestar', None, 900))
        self.assertIsNone(silence('synscan', None, None))
        self.assertIsNone(silence('synscan', 10, 900))
        self.assertEqual(silence('synscan', None, 700)['probable_cause'], 'MOUNT_WIFI_SLEEP')
        self.assertEqual(silence('synscan', 700, 680)['probable_cause'], 'UNKNOWN')
        self.assertEqual(silence('synscan', 900, 700)['probable_cause'], 'MOUNT_WIFI_SLEEP')
        self.assertEqual(silence('synscan', 900, 700)['since_seconds'], 700)


if __name__ == '__main__':
    unittest.main()
