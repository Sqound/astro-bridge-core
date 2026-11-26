"""Devices heard by the bridge are listed and linked by a paired app, with the link scope only."""
import http.client
import json
import threading

from wifi_bridge.candidates import scan_candidate
from wifi_bridge.config import profiles
from wifi_bridge.web import WebServer
from test_bridge import Fixture

# Synthetic radio observations: recognized types, an unrecognized module, open and protected networks.
HEARD = [
    {'ssid': 'vespera2-a1b2c3', 'bssid': '02:00:00:00:00:11', 'signal': 100, 'frequency': 2462, 'security': ''},
    {'ssid': 'S50 Pro_0a1b2c3d', 'bssid': '02:00:00:00:00:12', 'signal': 90, 'frequency': 5200, 'security': 'WPA2'},
    {'ssid': 'SynScan_ab12', 'bssid': '02:00:00:00:00:13', 'signal': 80, 'frequency': 2417, 'security': ''},
    {'ssid': 'Celestron-AB12', 'bssid': '02:00:00:00:00:14', 'signal': 70, 'frequency': 2437, 'security': ''},
    {'ssid': 'Home network', 'bssid': '02:00:00:00:00:15', 'signal': 60, 'frequency': 2412, 'security': 'WPA2'},
]


class NearbyDeviceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.configured()
        self.server = WebServer(("127.0.0.1", 0), self.controller)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        wall = int(self.controller.candidates.wall())
        for row in HEARD:
            self.controller.candidates.remember(scan_candidate(row, wall), persist=False)

    def app(self, method, path, body=None, scopes=('read', 'connect', 'telemetry', 'camera')):
        return self.native(method, path, body, token=self.controller_token(list(scopes)))

    def controller_token(self, scopes):
        # A paired client token through the real pairing store and token route.
        created = self.server.paired_clients.create({'name': 'Phone', 'scopes': scopes})
        status, body = self.native('POST', '/auth/token', {'client_id': created['client_id'], 'client_secret': created['client_secret']})
        self.assertEqual(status, 200, body)
        return body['access_token']

    def native(self, method, path, body=None, token=''):
        port = self.server.server_port
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
        self.addCleanup(conn.close)
        headers = {'Host': f'127.0.0.1:{port}', 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}
        conn.request(method, '/api/v1' + path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read())

    def by_kind(self, listing):
        return {row['kind']: row for row in listing['devices']}

    def test_recognized_devices_are_listed_with_readable_names_and_the_radio_rule(self):
        status, listing = self.app('GET', '/instruments/nearby', scopes=['read'])
        self.assertEqual(status, 200)
        rows = self.by_kind(listing)
        self.assertEqual(set(rows), {'vespera', 'seestar', 'synscan'})
        self.assertEqual(rows['vespera']['label'], 'Vespera · vespera2-a1b2c3')
        self.assertEqual((rows['vespera']['state'], rows['vespera']['password'], rows['vespera']['linkable']), ('in_range', 'none', True))
        self.assertEqual((rows['seestar']['password'], rows['seestar']['band']), ('factory_default', '5'))
        self.assertEqual(listing['rule'], 'ONE_DEVICE_PER_FREE_RADIO')
        self.assertGreaterEqual(listing['free_radios'], 1)
        self.assertNotIn('Home network', json.dumps(listing))

    def test_paired_app_links_reads_and_unlinks_without_admin(self):
        token = self.controller_token(['read', 'connect'])
        listing = self.native('GET', '/instruments/nearby', token=token)[1]
        vespera = self.by_kind(listing)['vespera']
        status, linked = self.native('POST', f"/instruments/nearby/{vespera['nearby_id']}/link", {}, token=token)
        self.assertEqual(status, 201, linked)
        profile = next(p for p in profiles(self.controller.store.config) if p['id'] == linked['instrument_id'])
        self.assertEqual((profile['kind'], profile['ssid'], profile['target_ip'], profile['enabled'], profile['password']),
                         ('vespera', 'vespera2-a1b2c3', '10.0.0.1', False, ''))
        # The device stays listed, now linked, and the instrument is readable.
        again = self.by_kind(self.native('GET', '/instruments/nearby', token=token)[1])['vespera']
        self.assertEqual((again['state'], again['instrument_id']), ('linked', linked['instrument_id']))
        self.assertEqual(self.native('GET', '/instruments/' + linked['instrument_id'], token=token)[0], 200)
        self.assertEqual(self.native('DELETE', f"/instruments/{linked['instrument_id']}/link", token=token)[0], 200)
        self.assertFalse(any(p['id'] == linked['instrument_id'] for p in profiles(self.controller.store.config)))

    def test_protected_network_password_is_kept_but_never_returned(self):
        token = self.controller_token(['read', 'connect'])
        seestar = self.by_kind(self.native('GET', '/instruments/nearby', token=token)[1])['seestar']
        status, linked = self.native('POST', f"/instruments/nearby/{seestar['nearby_id']}/link", {'password': 'synthetic-pass'}, token=token)
        self.assertEqual(status, 201)
        self.assertNotIn('synthetic-pass', json.dumps(linked))
        self.assertNotIn('synthetic-pass', json.dumps(self.native('GET', '/instruments/nearby', token=token)[1]))
        profile = next(p for p in profiles(self.controller.store.config) if p['id'] == linked['instrument_id'])
        self.assertEqual(profile['password'], 'synthetic-pass')
        synscan = self.by_kind(self.native('GET', '/instruments/nearby', token=token)[1])['synscan']
        self.assertEqual(self.native('POST', f"/instruments/nearby/{synscan['nearby_id']}/link", {'password': 'not-for-open'}, token=token)[0], 400)

    def test_link_scope_is_bounded(self):
        reader = self.controller_token(['read'])
        vespera = self.by_kind(self.native('GET', '/instruments/nearby', token=reader)[1])['vespera']
        self.assertEqual(self.native('POST', f"/instruments/nearby/{vespera['nearby_id']}/link", {}, token=reader)[0], 403)
        linker = self.controller_token(['connect'])
        # No arbitrary address, network or radio, and no bridge settings.
        for body in ({'target_ip': '192.0.2.10'}, {'ssid': 'Other'}, {'mac': '02:00:00:00:00:99'}):
            self.assertEqual(self.native('POST', f"/instruments/nearby/{vespera['nearby_id']}/link", body, token=linker)[0], 400, body)
        self.assertEqual(self.native('POST', '/telescopes', {'name': 'x', 'ssid': 'x', 'open': True}, token=linker)[0], 403)
        self.assertEqual(self.native('PATCH', '/settings', {'connection_mode': 'private'}, token=linker)[0], 403)
        self.assertEqual(self.native('POST', '/instruments/nearby/' + 'f' * 32 + '/link', {}, token=linker)[0], 404)
        # A profile the owner created is never changed or removed through this route.
        owner = self.configured_profile_id()
        self.assertEqual(self.native('DELETE', f'/instruments/{owner}/link', token=linker)[0], 404)

    def configured_profile_id(self):
        return profiles(self.controller.store.config)[0]['id']

    def test_list_stays_while_another_profile_is_active(self):
        value = json.loads(json.dumps(self.controller.store.config))
        value['telescopes'][0]['enabled'] = True
        self.controller.store.save(value)
        listing = self.app('GET', '/instruments/nearby', scopes=['read'])[1]
        self.assertEqual(set(self.by_kind(listing)), {'vespera', 'seestar', 'synscan'})
        # The legacy status field still hides suggestions while a profile is active.
        self.assertEqual(self.controller.candidates.nearby()['candidates'], [])
