"""SSID identity, persistence and protected live changes."""
from unittest.mock import patch

from test_bridge import Fixture, payload
from wifi_bridge.api import ApiError, MobileAPI
from wifi_bridge.config import Invalid, Store, hotspot_ssid, validate_config
from wifi_bridge.controller import Controller


class HotspotNameTests(Fixture):
    def test_default_uses_device_name_and_actual_ap_mac_with_utf8_byte_limit(self):
        self.assertEqual(hotspot_ssid(self.store.config, '00:11:22:33:44:55'), 'astro-bridge-334455')
        for name in ('observatory', 'x' * 63):
            value = dict(self.store.config, hostname=name)
            ssid = hotspot_ssid(value, self.devices[0]['mac'])
            self.assertEqual(ssid, name[:25] + '-12abcd')
            self.assertLessEqual(len(ssid.encode()), 32)

    def test_custom_utf8_name_roundtrips_and_invalid_names_are_rejected(self):
        data = payload(self.devices)
        for name in ('Mon observatoire', '星' * 10, ''):
            data['hotspot'] = {'enabled': True, 'ssid': name}
            config = validate_config(data, self.store.config, self.devices)
            self.store.save(config)
            self.assertEqual(Store(self.tmp.name, self.devices[0]['mac']).config['hotspot']['ssid'], name)
        for name in ('星' * 11, 'x' * 33, 'bad\nssid', '   ', None, 7):
            data['hotspot']['ssid'] = name
            with self.subTest(name=name), self.assertRaises(Invalid):
                validate_config(data, self.store.config, self.devices)

    def test_live_rename_keeps_profile_intent_requires_revision_and_survives_restart(self):
        self.net.radios = 3
        self.devices = self.net.devices()
        self.controller.update(devices=self.devices)
        self.configured()
        api = MobileAPI(self.controller)
        api.update('hotspot', {'enabled': True})
        self.controller.cycle(0)
        ap = self.controller.state['hotspot']['mac']
        self.assertEqual(self.controller.state['setup_ssid'], 'wifi-bridge-test-000002')
        with self.assertRaises(ApiError) as denied:
            api.update('hotspot', {'ssid': 'Mon observatoire'})
        self.assertEqual(denied.exception.code, 'PRECONDITION_REQUIRED')
        before = self.store.config['telescopes']
        api.update('hotspot', {'ssid': 'Mon observatoire'}, expected='"' + api.revision() + '"')
        with patch.object(self.net, 'hotspot', wraps=self.net.hotspot) as start:
            self.controller.cycle(10)
            self.controller.cycle(15)
            self.assertEqual(start.call_count, 1)
        self.assertEqual(self.controller.state['setup_ssid'], 'Mon observatoire')
        self.assertEqual(self.store.config['telescopes'], before)
        self.assertEqual(self.controller.state['hotspot']['mac'], ap)
        restarted = Controller(self.store, self.net, {'password': 'bootstrap-test-code'}, demo=True)
        restarted.update(devices=self.devices)
        with patch.object(self.net, 'hotspot') as start:
            restarted.cycle(20)
            start.assert_not_called()
        self.assertEqual(restarted.state['setup_ssid'], 'Mon observatoire')
        self.assertEqual(api.read('hotspot')['hotspot']['configured_ssid'], 'Mon observatoire')

    def test_protected_connection_rejects_rename_before_saving(self):
        self.configured()
        self.store.config['telescopes'][0]['owner_locked'] = True
        api = MobileAPI(self.controller)
        before = dict(self.store.config['hotspot'])
        with self.assertRaises(ApiError) as denied:
            api.update('hotspot', {'ssid': 'Changed'}, expected='"' + api.revision() + '"')
        self.assertEqual(denied.exception.code, 'CONNECTION_PROTECTED')
        self.assertEqual(self.store.config['hotspot'], before)
