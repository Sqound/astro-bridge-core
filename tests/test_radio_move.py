"""Atomic cross-radio reassignment preserves credentials and unrelated connections."""
import copy
from unittest.mock import patch
from test_bridge import Fixture
from wifi_bridge.config import Invalid, profiles, validate_config


class RadioMoveTests(Fixture):
    def prepare(self):
        self.configured()
        self.net.radios = 4
        self.devices = self.net.devices()
        self.controller.update(devices=self.devices)
        config = copy.deepcopy(self.store.config)
        config['telescopes'] = profiles(config)
        config['telescopes'] += [dict(config['telescopes'][0], id='second', name='Second', ssid='Other', mac=self.devices[2]['mac'], target_ip='192.168.4.1', enabled=True)]
        self.store.save(validate_config(config, self.store.config, self.devices))
        self.controller.cycle(0)

    def test_move_releases_source_and_replaces_target_without_touching_home(self):
        self.prepare()
        before = copy.deepcopy(self.store.config)
        with patch.object(self.net, 'disconnect', wraps=self.net.disconnect) as disconnect:
            self.controller.telescope_action({'id':'primary', 'action':'connect', 'mac':self.devices[2]['mac'], 'expected_mac':self.devices[0]['mac']})
            self.controller.cycle(5)
        saved = profiles(self.store.config)
        self.assertEqual(saved[0]['mac'], self.devices[2]['mac'])
        self.assertTrue(saved[0]['enabled']);self.assertFalse(saved[1]['enabled'])
        self.assertEqual(saved[0]['password'], profiles(before)[0]['password'])
        self.assertEqual(saved[1]['password'], profiles(before)[1]['password'])
        self.assertEqual(self.store.config['home'], before['home'])
        self.assertNotIn('home', [c.args[0] for c in disconnect.call_args_list])
        self.assertTrue(self.controller.status()['telescopes'][0]['connected'])
        self.assertNotIn('telescope-second', self.net.active)
        self.assertNotIn(self.devices[0]['mac'], self.net.associations.values())

    def test_invalid_destination_or_stale_source_does_not_write(self):
        self.prepare()
        before = copy.deepcopy(self.store.config)
        for data in [{'mac':self.devices[1]['mac']}, {'mac':'02:00:00:ff:ff:ff'}, {'mac':[]}, {'mac':self.devices[2]['mac'], 'expected_mac':self.devices[3]['mac']}]:
            with self.assertRaises(Invalid):
                self.controller.telescope_action(dict(data, id='primary', action='connect'))
            self.assertEqual(self.store.config,before)

    def test_move_to_empty_radio_retains_other_enabled_instrument(self):
        self.prepare()
        self.controller.telescope_action({'id':'primary','action':'connect','mac':self.devices[3]['mac']})
        p = profiles(self.store.config)
        self.assertTrue(p[0]['enabled']);self.assertTrue(p[1]['enabled'])
        self.assertEqual(p[1]['mac'],self.devices[2]['mac'])

    def test_unassigned_profile_can_be_saved_then_connected_to_selected_radio(self):
        self.prepare()
        value = copy.deepcopy(self.store.config)
        value['telescopes'].append(dict(value['telescopes'][0], id='saved', name='Saved', mac='', enabled=False, target_ip='192.168.5.1'))
        self.store.save(validate_config(value, self.store.config, self.devices))
        saved = next(p for p in profiles(self.store.config) if p['id'] == 'saved')
        self.assertEqual(saved['mac'], '')
        self.controller.cycle(5)
        self.assertNotIn('telescope-saved', self.net.active)
        self.controller.telescope_action({'id': 'saved', 'action': 'connect', 'mac': self.devices[3]['mac'], 'expected_mac': ''})
        self.controller.cycle(10)
        saved = next(p for p in profiles(self.store.config) if p['id'] == 'saved')
        self.assertEqual(saved['mac'], self.devices[3]['mac'])
        self.assertTrue(saved['enabled'])
        self.assertTrue(next(p for p in self.controller.status()['telescopes'] if p['id'] == 'saved')['connected'])

    def test_enabled_profile_requires_a_real_radio(self):
        self.prepare()
        value = copy.deepcopy(self.store.config)
        value['telescopes'][0]['mac'] = ''
        with self.assertRaises(Invalid):
            validate_config(value, self.store.config, self.devices)
