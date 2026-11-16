"""Native contract 1.2 additions against the real HTTP application (synthetic radios only)."""
import unittest

if __package__:
    from .test_native_contract_1_1 import bridge, call, tokens
else:  # unittest discovery imports this file as a top-level module.
    from test_native_contract_1_1 import bridge, call, tokens


class TelemetryBootIdTests(unittest.TestCase):
    def test_page_carries_the_cursor_boot_domain(self):
        with bridge() as (c, server, _):
            _, client = tokens(server)
            session = c.telemetry_store.start_session('tel-seestar-1', 'seestar', ['link_v1'])
            c.telemetry_store.sample(session, 'link.rssi_dbm', -50)
            status, _, page = call(server, 'GET', '/api/v1/telemetry/samples?session_id=' + session, token=client)
            self.assertEqual((status, page['boot_id']), (200, c.telemetry_store.boot_id))
            # Echoing the pair continues without a reset; another domain resets.
            query = '/api/v1/telemetry/samples?session_id=%s&after=%d&boot_id=%s' % (session, page['next_cursor'], page['boot_id'])
            _, _, same = call(server, 'GET', query, token=client)
            self.assertEqual((same['reset_reason'], same['boot_id']), (None, page['boot_id']))
            _, _, other = call(server, 'GET', query.replace(page['boot_id'], '1' * 32), token=client)
            self.assertEqual(other['reset_reason'], 'BOOT_CHANGED')


def dwarf_packet(state, cmd=15208, module=9):
    from wifi_bridge.dwarf_relay import encode
    return encode({4: module, 5: cmd, 6: 2, 7: encode({1: state}, {1: 0})}, {4: 0, 5: 0, 6: 0, 7: 2})


class OperationStateTests(unittest.TestCase):
    DWARF = {'id': 'tel-dwarf-1', 'name': 'Test DWARF', 'kind': 'dwarf', 'ssid': 'Telescope-DEMO-3', 'open': True,
             'target_ip': '203.0.113.1', 'enabled': True}

    def test_dwarf_decoder_reads_only_known_capture_notifications(self):
        from wifi_bridge.telemetry.adapters.dwarf_capture import decode
        self.assertEqual(decode(dwarf_packet(1)), ('capture', 'running'))
        # Bench numbers (DWARF mini, 02-03/11): 16405 module 14 at connection, 15211 = GoTo.
        self.assertEqual(decode(dwarf_packet(1, 16405, 14)), ('capture', 'running'))
        self.assertEqual(decode(dwarf_packet(1, 15211)), ('pointing', 'running'))
        for unread in (dwarf_packet(1, 15212), dwarf_packet(1, 15209), dwarf_packet(1, 16405), dwarf_packet(1, 15208, 14)):
            self.assertIsNone(decode(unread))
        self.assertEqual(decode(dwarf_packet(3)), ('capture', 'stopped'))
        self.assertEqual(decode(dwarf_packet(0)), ('capture', 'idle'))
        for bad in (dwarf_packet(1, 1), dwarf_packet(9), b'', b'\xff' * 8, dwarf_packet(1)[:-1], b'\x00' * 5000, 'text'):
            self.assertIsNone(decode(bad))

    def test_dwarf_state_protects_and_projects_while_connected(self):
        with bridge(radios=5, extra_profiles=[dict(self.DWARF, mac='02:00:00:00:00:04')]) as (c, server, net):
            c.cycle(0)
            profile = next(p for p in c.store.config['telescopes'] if p['id'] == 'tel-dwarf-1')
            self.assertIsNone(c.operation_state(profile))
            _, client = tokens(server)
            _, _, item = call(server, 'GET', '/api/v1/instruments/tel-dwarf-1', token=client)
            self.assertEqual((item['link'], item['current_operation']), ('connected', None))
            self.assertTrue(c.dwarf_capture.receive('tel-dwarf-1', dwarf_packet(1)))
            self.assertFalse(c.dwarf_capture.receive('tel-dwarf-1', dwarf_packet(1, 1)))
            self.assertEqual(c.operation_state(profile), 'running')
            _, _, item = call(server, 'GET', '/api/v1/instruments/tel-dwarf-1', token=client)
            running = item['current_operation']
            self.assertEqual((running['state'], running['kind']), ('running', 'capture'))
            _, _, again = call(server, 'GET', '/api/v1/instruments/tel-dwarf-1', token=client)
            self.assertEqual(again['current_operation']['since'], running['since'])  # Same state keeps its start.
            c.dwarf_capture.receive('tel-dwarf-1', dwarf_packet(3))
            self.assertEqual(c.operation_state(profile), 'idle')
            # A lost link drops the evidence instead of keeping a stale state.
            c.dwarf_capture.receive('tel-dwarf-1', dwarf_packet(1))
            next(r for r in c.state['telescopes'] if r['id'] == 'tel-dwarf-1')['connected'] = False
            self.assertIsNone(c.operation_state(profile))
            self.assertNotIn('tel-dwarf-1', c.dwarf_capture.observed)

    def test_seestar_running_event_kind(self):
        from types import SimpleNamespace
        from wifi_bridge.telemetry.adapters.seestar_events import SeestarCollector
        collector = SeestarCollector.__new__(SeestarCollector)
        worker = SimpleNamespace(operations={'Stack': ('running', 10.0), 'AutoFocus': ('complete', 10.0)})
        collector.c = SimpleNamespace(lock=__import__('threading').RLock())
        collector.workers = {'s': worker}; collector.clock = lambda: 11.0; collector.operation_cadence = 5
        collector.operation_state = lambda profile: 'running'
        self.assertEqual(collector.operation_kind({'id': 's'}), 'stacking')
        worker.operations['Stack'] = ('running', -100.0)  # Stale evidence names no kind.
        self.assertIsNone(collector.operation_kind({'id': 's'}))


class StationInstrumentTests(unittest.TestCase):
    STATION = {'id': 'tel-station-1', 'name': 'Garden Seestar', 'kind': 'seestar', 'target_ip': '198.51.100.20'}

    def revision(self, server, token):
        _, _, value = call(server, 'GET', '/api/v1/config', token=token)
        return {'If-Match': '"' + value['config_revision'] + '"'}

    def test_validation_rejects_radio_fields_and_unsupported_kinds(self):
        from wifi_bridge.config import Invalid
        from wifi_bridge.stations import validate_stations
        self.assertEqual(validate_stations([self.STATION], set())[0]['access'], 'station')
        for bad in (dict(self.STATION, ssid='Telescope-DEMO'), dict(self.STATION, kind='vespera'),
                    dict(self.STATION, target_ip='8.8.8.8'), dict(self.STATION, target_ip='127.0.0.1'),
                    dict(self.STATION, client_connect='yes'), dict(self.STATION, name=' ')):
            with self.assertRaises(Invalid): validate_stations([bad], set())
        with self.assertRaises(Invalid): validate_stations([self.STATION], {'tel-station-1'})

    def test_admin_api_lists_connects_directly_and_never_takes_a_radio(self):
        with bridge() as (c, server, _):
            admin, client = tokens(server)
            radios = c.free_radios()
            status, _, created = call(server, 'POST', '/api/v1/stations', self.STATION, admin, self.revision(server, admin))
            self.assertEqual((status, created['station']['access'], created['station']['reachable']), (201, 'station', None))
            status, _, _ = call(server, 'POST', '/api/v1/stations', dict(self.STATION, id='tel-seestar-1'), admin, self.revision(server, admin))
            self.assertEqual(status, 409)  # IDs are unique across radio profiles and stations.
            _, _, item = call(server, 'GET', '/api/v1/instruments/tel-station-1', token=client)
            self.assertEqual((item['link'], item['service'], item['simultaneous']), ('saved', 'not_probed', True))
            c.stations.tick()
            _, _, catalog = call(server, 'GET', '/api/v1/instruments', token=client)
            status, _, ready = call(server, 'POST', '/api/v1/instruments/tel-station-1/connect', {'reserve_seconds': 36000},
                                    client, {'If-Match': '"' + catalog['config_revision'] + '"'})
            self.assertEqual((status, ready['endpoint']['mode'], ready['endpoint']['primary']['host']), (200, 'direct', '198.51.100.20'))
            self.assertEqual(ready['lease']['reservation_expires_in'], 36000)
            # No radio is spent and no network change is protected by this lease.
            self.assertEqual((c.free_radios(), c.protection.protected()), (radios, False))
            _, _, endpoint = call(server, 'GET', '/api/v1/instruments/tel-station-1/endpoint', token=client)
            self.assertEqual(endpoint['mode'], 'direct')
            # Reserved and answering: session telemetry records the handshake time.
            c.stations.record('tel-station-1', 2.5)
            self.assertIn('tel-station-1', c.telemetry_sessions.active)
            status, _, _ = call(server, 'DELETE', '/api/v1/stations/tel-station-1', None, admin, self.revision(server, admin))
            self.assertEqual(status, 409)  # A held reservation keeps it.
            status, _, body = call(server, 'POST', '/api/v1/instruments/tel-station-1/disconnect', {}, client)
            self.assertEqual((status, body), (200, {'ok': True}))
            self.assertEqual(c.protection.leases, {})
            status, _, _ = call(server, 'PATCH', '/api/v1/stations/tel-station-1', {'name': 'Roof Seestar'}, admin, self.revision(server, admin))
            self.assertEqual(status, 200)
            status, _, deleted = call(server, 'DELETE', '/api/v1/stations/tel-station-1', None, admin, self.revision(server, admin))
            self.assertEqual((status, deleted['deleted']), (200, True))

    def test_unreachable_station_is_listed_and_refuses_connect(self):
        with bridge() as (c, server, _):
            admin, client = tokens(server)
            call(server, 'POST', '/api/v1/stations', self.STATION, admin, self.revision(server, admin))
            c.stations.record('tel-station-1', None)
            _, _, catalog = call(server, 'GET', '/api/v1/instruments', token=client)
            item = next(i for i in catalog['instruments'] if i['id'] == 'tel-station-1')
            self.assertEqual((item['link'], item['reason']), ('unreachable', 'NOT_ANSWERING'))
            status, _, body = call(server, 'POST', '/api/v1/instruments/tel-station-1/connect', {}, client,
                                   {'If-Match': '"' + catalog['config_revision'] + '"'})
            self.assertEqual((status, body['error']['code']), (409, 'INSTRUMENT_UNREACHABLE'))
            status, _, _ = call(server, 'GET', '/api/v1/instruments/tel-station-1/endpoint', token=client)
            self.assertEqual(status, 409)

    def test_owner_can_refuse_night_reservations(self):
        with bridge() as (c, server, _):
            admin, client = tokens(server)
            call(server, 'POST', '/api/v1/stations', self.STATION, admin, self.revision(server, admin))
            c.stations.tick()
            status, _, _ = call(server, 'PATCH', '/api/v1/settings', {'auto_policy': {'night_reservations': False}}, admin, self.revision(server, admin))
            self.assertEqual(status, 200)
            for ident in ('tel-station-1', 'tel-seestar-1'):
                _, _, catalog = call(server, 'GET', '/api/v1/instruments', token=client)
                status, _, body = call(server, 'POST', '/api/v1/instruments/%s/connect' % ident, {'reserve_seconds': 3600}, client,
                                       {'If-Match': '"' + catalog['config_revision'] + '"'})
                self.assertEqual((status, body['error']['code']), (403, 'RESERVATIONS_DISABLED'))
            # Ordinary leases are unaffected.
            _, _, catalog = call(server, 'GET', '/api/v1/instruments', token=client)
            status, _, _ = call(server, 'POST', '/api/v1/instruments/tel-station-1/connect', {}, client,
                                {'If-Match': '"' + catalog['config_revision'] + '"'})
            self.assertEqual(status, 200)

    def test_handshake_probe_sends_nothing(self):
        import socket, threading
        from wifi_bridge.stations import tcp_probe
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen(1)
            received = []
            def accept():
                peer, _ = listener.accept()
                with peer:
                    peer.settimeout(1)
                    try: received.append(peer.recv(16))
                    except OSError: received.append(b'')
            thread = threading.Thread(target=accept); thread.start()
            self.assertIsNotNone(tcp_probe('127.0.0.1', listener.getsockname()[1]))
            thread.join(5)
            self.assertEqual(received, [b''])
        with socket.socket() as closed:
            closed.bind(('127.0.0.1', 0)); port = closed.getsockname()[1]
        self.assertIsNone(tcp_probe('127.0.0.1', port, timeout=0.5))


class ManagementEndpointTests(unittest.TestCase):
    def test_info_lists_web_interface_per_confirmed_path(self):
        from unittest.mock import patch
        with bridge() as (c, server, _):
            api = server.api
            c.state['management'] = {'paths': []}
            self.assertEqual(api.info('127.0.0.1')['management_endpoints'], [])  # Unknown: empty.
            c.state['management'] = {'paths': [{'role': 'home', 'ip': '192.0.2.10'}, {'role': 'ap', 'ip': '192.168.77.1'}]}
            info = api.info('192.168.77.1', 80)
            self.assertEqual(info['request_path'], 'hotspot')
            self.assertEqual(info['management_endpoints'], [{'url': 'http://192.0.2.10/', 'path': 'home', 'tls': False},
                                                            {'url': 'http://192.168.77.1/', 'path': 'hotspot', 'tls': False}])
            self.assertEqual(api.info(None, 8797)['management_endpoints'][0]['url'], 'http://192.0.2.10:8797/')
            settings, runtime = c.announcement()
            with patch.object(c, 'announcement', return_value=(settings, dict(runtime, https_port=443))):
                secure = [e for e in api.info(None)['management_endpoints'] if e['tls']]
            self.assertEqual(secure, [{'url': 'https://astro-bridge-test.local/', 'path': 'home', 'tls': True},
                                      {'url': 'https://astro-bridge-test.local/', 'path': 'hotspot', 'tls': True}])


if __name__ == '__main__':
    unittest.main()
