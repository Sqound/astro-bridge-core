"""Synthetic endpoint topology, readiness and strict public-field allowlists."""
import copy
import json
from pathlib import Path
import unittest

from wifi_bridge.endpoints import inventory
from helpers.contract_schema import validate


HOME = {'role': 'home', 'host': '192.168.12.20', 'forwarding_ready': True}
AP = {'role': 'ap', 'host': '192.168.77.1', 'forwarding_ready': True}


def profile(ident='scope', kind='vespera', **changes):
    return dict(id=ident, kind=kind, enabled=True, target_ip='192.168.4.1',
                ssid='Synthetic-private-network', bssid='02:00:00:00:00:01',
                password='synthetic-private-value') | changes


def live(ident='scope', **changes):
    return dict(id=ident, connected=True, relay_ready=True,
                service={'state': 'not_answering'}, management_paths=[HOME, AP]) | changes


class EndpointTests(unittest.TestCase):
    def test_topology_table(self):
        cases = [
            ('routed Vespera', [profile()], [live(legacy_ports=True)],
             {'home', 'hotspot'}, {'vespera_http', 'vespera_socketio', 'direct_route'}),
            ('Seestar proxy', [profile(kind='seestar')], [live(seestar_proxy=True)],
             {'home', 'hotspot'}, {'seestar_native', 'direct_route'}),
            ('hotspot only', [profile()], [live(legacy_ports=True, management_paths=[AP])],
             {'hotspot'}, {'vespera_http', 'vespera_socketio', 'direct_route'}),
            ('disconnected', [profile()], [live(connected=False)], set(), set()),
            ('disabled before reconcile', [profile(enabled=False)], [live(legacy_ports=True)], set(), set()),
            ('target edit before reconcile', [profile()], [live(target_ip='192.168.5.1', legacy_ports=True)], set(), set()),
        ]
        for name, profiles, rows, roles, services in cases:
            with self.subTest(name=name):
                snapshot = {'config': {'telescopes': profiles}, 'telescopes': rows}
                previous = copy.deepcopy(snapshot)
                result = inventory(snapshot)
                self.assertEqual(snapshot, previous)
                self.assertEqual({p['path'] for p in result[0]['paths']}, roles)
                self.assertEqual({p['service'] for p in result[0]['paths']}, services)
                for path in result[0]['paths']:
                    self.assertEqual(path['service_state'], 'not_answering')
                    self.assertTrue(path['ready'])
                    self.assertEqual(path['requires_client_route'], path['service'] == 'direct_route')
                    self.assertEqual(path['host'], profiles[0]['target_ip'] if path['requires_client_route']
                                     else HOME['host'] if path['path'] == 'home' else AP['host'])
                self.assert_public(result)

    def test_overlapping_isolated_mounts_and_udp_owner_allowlist(self):
        profiles = [profile('zwo', 'zwo_mount', isolated=True), profile('syn', 'synscan', isolated=True)]
        rows = []
        for ident, transport, port in [('zwo', 'tcp', 14030), ('syn', 'udp', 21180)]:
            alternatives = [dict(p, transport=transport, port=port, ready=p['role'] == 'home',
                refused_clients=7, owner={'address': '192.168.12.21:25000', 'since_seq': 2, 'password': 'discard'}) for p in (HOME, AP)]
            rows.append(live(ident, client_endpoint={'alternatives': alternatives}))
        result = inventory({'config': {'telescopes': profiles}, 'telescopes': rows})
        self.assertEqual(result[0]['target']['ip'], result[1]['target']['ip'])
        self.assertEqual(result[0]['target']['ports'], [4030])
        self.assertEqual(result[1]['target']['ports'], [11880])
        self.assertEqual([p['port'] for p in result[0]['paths']], [14030, 14030])
        self.assertEqual([p['port'] for p in result[1]['paths']], [21180, 21180])
        self.assertTrue(all(not p['requires_client_route'] for entry in result for p in entry['paths']))
        for path in result[0]['paths']:
            self.assertNotIn('owner', path)
            self.assertNotIn('refused_clients', path)
        self.assertEqual([p['refused_clients'] for p in result[1]['paths']], [7, 7])
        self.assertEqual(result[1]['paths'][0]['owner'], {'address': '192.168.12.21:25000', 'since_seq': 2})
        self.assertFalse(result[1]['paths'][1]['ready'])
        self.assert_public(result)

    def test_udp_refusal_count_is_bounded_and_allowlisted(self):
        for value, expected in [(None, 0), ('private-value', 0), ({'ssid': 'discard'}, 0),
                                (True, 0), (-1, 0), (0, 0), (2**53-1, 2**53-1),
                                (2**53, 2**53-1)]:
            with self.subTest(value=value):
                endpoint = dict(HOME, transport='udp', port=21180, refused_clients=value)
                result = inventory({'config': {'telescopes': [profile(kind='synscan', isolated=True)]},
                                    'telescopes': [live(client_endpoint={'alternatives': [endpoint]})]})
                self.assertEqual(result[0]['paths'][0]['refused_clients'], expected)
                self.assertIsNone(result[0]['paths'][0]['owner'])
                self.assert_public(result)
        endpoint.pop('refused_clients')
        result = inventory({'config': {'telescopes': [profile(kind='synscan', isolated=True)]},
                            'telescopes': [live(client_endpoint={'alternatives': [endpoint]})]})
        self.assertEqual(result[0]['paths'][0]['refused_clients'], 0)

    def test_no_management_path_missing_live_profile_and_unknown_service(self):
        for row in [[], [live(management_paths=[])]]:
            self.assertEqual(inventory({'config': {'telescopes': [profile()]}, 'telescopes': row})[0]['paths'], [])
        result = inventory({'config': {'telescopes': [profile(kind='dwarf')]},
                            'telescopes': [live(dwarf_proxy=True, service={'state': 'unreviewed'})]})
        self.assertEqual({p['service_state'] for p in result[0]['paths']}, {'unknown'})
        self.assertEqual({p['port'] for p in result[0]['paths'] if p['service'] == 'dwarf_ws'}, {9900})
        self.assert_public(result)

    def test_declared_port_roles_and_dwarf_proxy_ports(self):
        snapshot = {'config': {'telescopes': [profile(kind='dwarf'), profile('seestar', 'seestar')]},
                    'telescopes': [live(dwarf_proxy=True), live('seestar', seestar_proxy=True)]}
        result = inventory(snapshot)
        self.assert_public(result)
        dwarf = {(p['port'], p['service'], p['role'], p['protocol']) for p in result[0]['paths'] if p['path'] == 'home'}
        self.assertLessEqual({(9900, 'dwarf_ws', 'control', 'websocket'), (8082, 'dwarf_http', 'rest', 'http'),
                              (554, 'dwarf_rtsp', 'preview', 'rtsp'), (554, 'direct_route', 'preview', 'rtsp')}, dwarf)
        seestar = {p['port']: (p['role'], p['protocol']) for p in result[1]['paths'] if p['service'] == 'seestar_native'}
        self.assertEqual(seestar, {4700: ('control', 'json_rpc'), 4800: ('imaging', 'binary'), 4801: ('other', 'unknown'),
                                   4804: ('other', 'unknown'), 4554: ('preview', 'rtsp'), 4555: ('preview', 'rtsp')})

    def assert_public(self, result):
        for entry in result:
            self.assertEqual(set(entry), {'profile_id', 'kind', 'target', 'paths'})
            self.assertEqual(set(entry['target']), {'ip', 'ports'})
            for path in entry['paths']:
                self.assertLessEqual(set(path), {'path', 'host', 'port', 'transport', 'service', 'ready', 'role', 'protocol',
                                                'service_state', 'requires_client_route', 'owner', 'refused_clients'})
                self.assertIn(path['role'], ('control', 'preview', 'imaging', 'rest', 'stream', 'other'))
                if path.get('owner'): self.assertEqual(set(path['owner']), {'address', 'since_seq'})
        spec = json.loads(Path('wifi_bridge/static/openapi.json').read_text())
        for entry in result: validate(entry, spec['components']['schemas']['ProfileEndpoints'], spec)
        encoded = json.dumps(result)
        for forbidden in ('ssid', 'bssid', 'password', 'synthetic-private'):
            self.assertNotIn(forbidden, encoded)
