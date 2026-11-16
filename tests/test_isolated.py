"""Dedicated endpoint contracts, byte transparency and controller regression cases."""
import copy
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.api import ApiError, MobileAPI
from wifi_bridge.config import Invalid, Store, validate_config, network_intent
from wifi_bridge.instrument_relay import InstrumentRelay, RelayCounters, UdpOwner, COUNTER_MAX
from wifi_bridge.isolated import IsolatedRadios, allocate_ports, namespace_name, namespace_firewall, namespace_socket
from wifi_bridge.isolated_dhcp import lease_address


class IsolatedConfigTests(Fixture):
    def setUp(self):
        super().setUp()
        self.net.radios = 3; self.devices = self.net.devices()
        self.controller.update(devices=self.devices)
        self.configured()
        self.api = MobileAPI(self.controller)

    def pair(self):
        first = dict(self.store.config['telescopes'][0], kind='zwo_mount', target_ip='192.168.4.1', isolated=True)
        second = dict(first, id='synscan', name='SynScan mount', kind='synscan', mac=self.devices[2]['mac'], ssid='SynScan_TEST')
        value = dict(self.store.config, telescopes=[first, second])
        self.controller.save(value)
        return self.store.config['telescopes']

    def test_two_identical_targets_active_and_ports_survive_reload_and_omitted_patch(self):
        pair = self.pair()
        self.assertEqual([p['client_port'] for p in pair], [14030, 21180])
        self.controller.cycle(0)
        rows = self.api.read('telescopes')['telescopes']
        self.assertTrue(all(p['connection']['connected'] and p['connection']['relay_ready'] for p in rows))
        self.assertEqual([p['client_endpoint']['port'] for p in rows], [14030, 21180])
        self.assertTrue(all(p['client_endpoint']['simulated'] for p in rows))
        self.assertTrue(all(p['client_endpoint']['host'] == self.controller.state['home_ip'] for p in rows))
        self.assertTrue(all(not p['connection'].get(p['kind']+'_proxy') for p in rows))
        self.assertEqual(Store(self.tmp.name, self.devices[0]['mac']).config, self.store.config)
        self.api.update('telescopes', {'name': 'Renamed'}, ident='primary')
        self.assertEqual([p['client_port'] for p in self.store.config['telescopes']], [14030, 21180])
        wire = json.dumps(self.api.read('telescopes'))
        self.assertNotIn('vespera-test-pass', wire)
        self.assertNotIn('password"', wire.replace('has_password"', 'redacted"'))

    def test_explicit_and_retained_ports_reserved_before_allocation_and_duplicate_refused(self):
        entries = [{'isolated': True, 'kind': 'zwo_mount'}, {'isolated': True, 'kind': 'zwo_mount', 'client_port': 14030}]
        allocate_ports(entries, {})
        self.assertEqual(entries[0]['client_port'], 14031)
        for value in (True, 9999, 49152, 18788, '14030'):
            with self.subTest(port=value), self.assertRaises(Invalid):
                allocate_ports([dict(entries[0], client_port=value)], {})
        with self.assertRaises(Invalid): allocate_ports([entries[0], dict(entries[0])], {})

    def test_not_enough_radios_refused_without_disabling_current_mount(self):
        self.pair()
        before = copy.deepcopy(self.store.config)
        with self.assertRaises(ApiError) as error:
            self.api.update('telescopes', {'mac': self.devices[0]['mac'], 'enabled': True}, ident='synscan')
        self.assertEqual(error.exception.code, 'INSUFFICIENT_RADIOS')
        self.assertEqual(self.store.config, before)

    def test_one_profile_switch_preserves_other_association_and_port(self):
        self.pair(); self.controller.cycle(0)
        other = self.net.associations['telescope-synscan']
        self.controller.telescope_action({'id': 'primary', 'action': 'disconnect'})
        self.controller.cycle(20)
        self.assertEqual(self.net.associations['telescope-synscan'], other)
        self.assertTrue(self.controller.state['telescopes'][1]['relay_ready'])
        self.controller.telescope_action({'id': 'primary', 'action': 'connect'})
        self.controller.cycle(40)
        self.assertEqual(self.net.associations['telescope-synscan'], other)
        self.assertTrue(all(r['relay_ready'] for r in self.controller.state['telescopes']))

    def test_unsupported_kind_and_non_boolean_refused_and_mode_part_of_intent(self):
        original = copy.deepcopy(self.store.config)
        for value in (True, 'yes'):
            # Vespera is isolatable since contract 1.2; Seestar is not.
            bad = copy.deepcopy(original); bad['telescopes'][0].update(isolated=value, kind='seestar')
            with self.assertRaises(Invalid): validate_config(bad, original, self.devices)
        self.assertNotEqual(network_intent(dict(original['telescopes'][0], isolated=True)), network_intent(original['telescopes'][0]))

    def test_hotspot_endpoint_and_legacy_host_routes(self):
        self.pair()
        self.net.radios = 4; self.devices = self.net.devices(); self.controller.update(devices=self.devices)
        self.api.update('hotspot', {'enabled': True, 'mac': self.devices[3]['mac']}, expected='"'+self.api.revision()+'"')
        self.controller.cycle(0)
        for row in self.controller.state['telescopes']:
            self.assertEqual({e['role'] for e in row['client_endpoint']['alternatives']}, {'home', 'ap'})
            self.assertEqual(row['client_endpoint']['alternatives'][1]['host'], '192.168.77.1')
        self.assertNotIn('192.168.4.1', self.controller.hotspot_route_targets(self.devices))

    def test_isolation_failure_is_explicit_and_not_retried_each_cycle(self):
        from wifi_bridge.network import NetworkIsolationError
        self.pair()
        original = self.net.connect
        def connect(role, device, config):
            if role == 'telescope': raise NetworkIsolationError('ISOLATION_TOOLS_MISSING')
            return original(role, device, config)
        with patch.object(self.net,'connect',side_effect=connect) as attempt:
            self.controller.cycle(0)
            self.controller.cycle(20)
        self.assertEqual(sum(call.args[0]=='telescope' for call in attempt.call_args_list),1)
        row = self.controller.state['telescopes'][0]
        self.assertEqual(row['reason'],'ISOLATION_TOOLS_MISSING')
        self.assertFalse(row['relay_ready'])
        self.assertTrue(self.controller.state['telescopes'][1]['relay_ready'])

    def test_another_isolated_mount_lock_does_not_block_independent_changes(self):
        self.pair();self.controller.cycle(0)
        self.store.config['telescopes'][1]['owner_locked'] = True
        self.api.update('telescopes',{'client_port':14031},ident='primary')
        self.assertEqual(self.store.config['telescopes'][0]['client_port'],14031)
        self.assertTrue(self.store.config['telescopes'][1]['owner_locked'])

    def test_authenticated_http_contract_advertises_two_distinct_endpoints(self):
        import http.client
        from wifi_bridge.web import WebServer
        self.pair();self.controller.cycle(0)
        server = WebServer(('127.0.0.1',0),self.controller)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def request(method,path,data=None,token=None):
            connection = http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            headers={'Content-Type':'application/json'}
            if token: headers['Authorization']='Bearer '+token
            connection.request(method,path,json.dumps(data) if data is not None else None,headers)
            response=connection.getresponse();body=json.loads(response.read());code=response.status
            connection.close();return code,body
        self.assertEqual(request('GET','/api/v1/telescopes')[0],401)
        code, auth = request('POST','/api/v1/auth/token',{'password':'local-admin-test-password'})
        self.assertEqual(code,200)
        code, result = request('GET','/api/v1/telescopes',token=auth['access_token'])
        self.assertEqual(code,200)
        self.assertEqual([p['client_endpoint']['protocol'] for p in result['telescopes']],['lx200','synscan'])
        self.assertNotIn('vespera-test-pass',json.dumps(result))


    def test_configuration_change_never_advertises_previous_endpoint_as_current(self):
        self.pair();self.controller.cycle(0)
        self.assertTrue(self.api.read('telescopes','primary')['telescope']['client_endpoint']['ready'])
        self.api.update('telescopes',{'client_port':14031},ident='primary')
        endpoint = self.api.read('telescopes','primary')['telescope']['client_endpoint']
        self.assertFalse(endpoint['ready']);self.assertEqual(endpoint['port'],14031)
        self.assertEqual(endpoint['reason'],'CONFIGURATION_PENDING')
        self.assertTrue(all(not path['ready'] for path in endpoint['alternatives']))
        self.controller.cycle(20)
        self.assertTrue(self.api.read('telescopes','primary')['telescope']['client_endpoint']['ready'])


    def test_reconcile_keeps_unchanged_endpoint_ready_but_failure_revokes_it(self):
        self.pair(); self.controller.cycle(0)
        original = self.controller._cycle
        def reconcile(now):
            self.assertFalse(self.controller.last_cycle_ok)
            endpoint = self.api.read('telescopes', 'primary')['telescope']['client_endpoint']
            self.assertTrue(endpoint['ready'])
            self.assertTrue(all(path['ready'] for path in endpoint['alternatives']))
            return original(now)
        with patch.object(self.controller, '_cycle', side_effect=reconcile):
            self.controller.cycle(20)
        with patch.object(self.controller, '_cycle', side_effect=RuntimeError('planning failed')):
            with self.assertRaises(RuntimeError): self.controller.cycle(40)
        endpoint = self.api.read('telescopes', 'primary')['telescope']['client_endpoint']
        self.assertFalse(endpoint['ready'])
        self.assertEqual(endpoint['reason'], 'PLANNING_FAILED')
        self.assertTrue(all(not path['ready'] for path in endpoint['alternatives']))


    def test_udp_release_http_requires_admin_actual_revision_and_empty_body(self):
        import http.client
        from wifi_bridge.web import WebServer
        self.pair(); self.controller.cycle(0)
        manager = self.net.isolated
        owner = manager.udp_owners['synscan']
        owner.accept(('192.0.2.1', 1234)); owner.accept(('192.0.2.2', 5678))
        server = WebServer(('127.0.0.1',0),self.controller)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        def request(method, path, data=None, token=None, expected=None):
            connection = http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            headers={'Content-Type':'application/json'}
            if token: headers['Authorization']='Bearer '+token
            if expected: headers['If-Match']=expected
            connection.request(method,path,json.dumps(data) if data is not None else None,headers)
            response=connection.getresponse();body=json.loads(response.read());code=response.status
            connection.close();return code,body
        path = '/api/v1/endpoints/synscan/release-owner'
        self.assertEqual(request('POST',path,{})[0],401)
        code, auth = request('POST','/api/v1/auth/token',{'password':'local-admin-test-password'})
        self.assertEqual(code,200); token=auth['access_token']
        revision='"'+server.api.revision()+'"'
        for expected, code in ((None,428),('*',428),('"*"',428),('"stale"',412)):
            self.assertEqual(request('POST',path,{},token,expected)[0],code)
            self.assertIsNotNone(owner.snapshot()['owner'])
        self.assertEqual(request('POST',path,{'force':True},token,revision)[0],400)
        server.sessions[token]['scopes']=['read']
        self.assertEqual(request('POST',path,{},token,revision)[0],403)
        server.sessions[token]['scopes']=['admin']
        self.assertEqual(request('POST','/api/v1/endpoints/primary/release-owner',{},token,revision)[0],409)
        self.assertEqual(request('POST','/api/v1/endpoints/missing/release-owner',{},token,revision)[0],404)
        live=request('GET','/api/v1/telescopes',token=token)[1]['telescopes'][1]['client_endpoint']
        self.assertEqual(live['owner']['address'],'192.0.2.1:1234'); self.assertEqual(live['refused_clients'],1)
        code, released=request('POST',path,{},token,revision)
        self.assertEqual(code,200); self.assertIsNone(released['owner']); self.assertEqual(released['refused_clients'],1)
        self.assertEqual(request('POST',path,{},token,revision)[0],200)  # Idempotent.
        live=request('GET','/api/v1/status',token=token)[1]['telescopes'][1]['client_endpoint']
        self.assertIsNone(live['owner']); self.assertEqual(live['refused_clients'],1)
        self.assertEqual(request('GET',path,token=token)[0],405)
        self.assertEqual(request('GET','/api/v1/endpoints',token=token)[0],200)

    def test_safe_onlink_lease_and_firewall(self):
        self.assertEqual(lease_address('192.168.4.2', '255.255.255.0', '192.168.4.1'), '192.168.4.2/24')
        for address, mask, target in [('192.168.4.1','24','192.168.4.1'), ('8.8.8.2','24','8.8.8.1'),
                                      ('192.168.4.2','24','192.168.5.1')]:
            with self.assertRaises(ValueError): lease_address(address, mask, target)
        script = namespace_firewall('192.168.4.1', 'tcp', 4030)
        self.assertIn('policy drop', script)
        self.assertNotIn('11880', script)
        self.assertEqual(namespace_name('02:00:00:00:00:01'), 'ab-radio-020000000001')


class RelayTests(unittest.TestCase):
    def test_tcp_fragmented_pipeline_half_close_reconnect_and_large_reply(self):
        server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(4); server.settimeout(3)
        self.addCleanup(server.close)
        payload = b'A'*180000+b'\x00\xff#'
        failure = []
        def serve():
            try:
                for _ in range(2):
                    client, _ = server.accept()
                    with client:
                        data = b''
                        while True:
                            part = client.recv(1024)
                            if not part: break
                            data += part
                        if data != b':GVP#:GVP#': raise AssertionError(data)
                        client.sendall(payload)
            except Exception as error: failure.append(error)
        thread = threading.Thread(target=serve, daemon=True); thread.start()
        relay = InstrumentRelay('127.0.0.1', 0, 'tcp', '127.0.0.1', server.getsockname()[1],
                                lambda kind: socket.socket(socket.AF_INET, kind)).start()
        self.addCleanup(relay.close)
        for _ in range(2):
            with socket.create_connection(('127.0.0.1', relay.port), timeout=4) as client:
                client.sendall(b':GV'); client.sendall(b'P#:GVP#'); client.shutdown(socket.SHUT_WR)
                received = bytearray()
                while True:
                    part = client.recv(32768)
                    if not part: break
                    received.extend(part)
                self.assertEqual(received, payload)
        thread.join(3); self.assertFalse(failure)
        stats = relay.counters.snapshot()
        self.assertEqual(stats['bytes_to_instrument'], 20)
        self.assertEqual(stats['bytes_from_instrument'], 2*len(payload))

    def test_udp_owner_refuses_other_client_expires_and_preserves_opaque_bytes(self):
        now = [100.0]
        owner = UdpOwner(clock=lambda: now[0])
        server = socket.socket(type=socket.SOCK_DGRAM); server.bind(('127.0.0.1', 0)); server.settimeout(.3)
        self.addCleanup(server.close)
        relay = InstrumentRelay('127.0.0.1', 0, 'udp', '127.0.0.1', server.getsockname()[1],
            lambda kind: socket.socket(socket.AF_INET, kind), owner=owner).start()
        self.addCleanup(relay.close)
        def exchange(client, payload):
            client.sendto(payload, ('127.0.0.1', relay.port))
            data, source = server.recvfrom(100)
            self.assertEqual(data, payload)
            server.sendto(b'REPLY'+data, source)
            self.assertEqual(client.recvfrom(100), (b'REPLY'+payload, ('127.0.0.1', relay.port)))
        with socket.socket(type=socket.SOCK_DGRAM) as a, socket.socket(type=socket.SOCK_DGRAM) as b:
            a.settimeout(1); b.settimeout(.3)
            exchange(a, b'\x00\xff:e1\r')
            first = owner.snapshot()['owner']
            self.assertEqual(first['address'], '127.0.0.1:'+str(a.getsockname()[1]))
            b.sendto(b':e2\r', ('127.0.0.1', relay.port))
            with self.assertRaises(socket.timeout): server.recvfrom(100)
            with self.assertRaises(socket.timeout): b.recvfrom(100)
            self.assertEqual(owner.snapshot()['refused_clients'], 1)
            self.assertEqual(relay.counters.snapshot()['bytes_to_instrument'], 6)
            now[0] += 19.9
            exchange(a, b':e1\r')  # Owner datagrams alone extend the interval.
            self.assertEqual(owner.snapshot()['owner'], first)
            now[0] += 20
            self.assertIsNone(owner.snapshot()['owner'])
            exchange(b, b':e2\r')
            self.assertEqual(owner.snapshot()['owner']['since_seq'], first['since_seq']+1)
            self.assertEqual(owner.snapshot()['refused_clients'], 1)

    def test_udp_release_shared_paths_drops_late_reply_before_new_owner(self):
        owner = UdpOwner()
        server = socket.socket(type=socket.SOCK_DGRAM); server.bind(('127.0.0.1', 0)); server.settimeout(.3)
        self.addCleanup(server.close)
        relays = [InstrumentRelay('127.0.0.1', 0, 'udp', '127.0.0.1', server.getsockname()[1],
            lambda kind: socket.socket(socket.AF_INET, kind), owner=owner).start() for _ in range(2)]
        for relay in relays: self.addCleanup(relay.close)
        with socket.socket(type=socket.SOCK_DGRAM) as a, socket.socket(type=socket.SOCK_DGRAM) as b:
            a.settimeout(.3); b.settimeout(.3)
            a.sendto(b'first', ('127.0.0.1', relays[0].port)); _, previous = server.recvfrom(100)
            b.sendto(b'blocked', ('127.0.0.1', relays[1].port))
            with self.assertRaises(socket.timeout): server.recvfrom(100)
            self.assertEqual(owner.snapshot()['refused_clients'], 1)
            released = owner.release()
            self.assertIsNone(released['owner']); self.assertEqual(released['refused_clients'], 1)
            b.sendto(b'next', ('127.0.0.1', relays[1].port)); data, current = server.recvfrom(100)
            self.assertEqual(data, b'next')
            self.assertNotEqual(previous, current)
            server.sendto(b'late', previous); server.sendto(b'current', current)
            self.assertEqual(b.recvfrom(100)[0], b'current')
            with self.assertRaises(socket.timeout): a.recvfrom(100)
            with self.assertRaises(socket.timeout): b.recvfrom(100)

    def test_udp_owner_counters_saturate_and_replies_do_not_extend_idle(self):
        now = [0.0]; owner = UdpOwner(clock=lambda: now[0])
        owner.seq = COUNTER_MAX; owner.refused_clients = COUNTER_MAX
        epoch = owner.accept(('192.0.2.1', 1234))
        self.assertIsNone(owner.accept(('192.0.2.2', 5678)))
        self.assertEqual(owner.snapshot()['refused_clients'], COUNTER_MAX)
        self.assertEqual(owner.snapshot()['owner']['since_seq'], COUNTER_MAX)
        now[0] = 19.9; self.assertTrue(owner.current(epoch))
        now[0] = 20; self.assertFalse(owner.current(epoch))
        self.assertIsNot(owner.accept(('192.0.2.2', 5678)), epoch)
        for invalid in (True, 0, -1, float('nan'), float('inf'), 3601, '20'):
            with self.subTest(value=invalid), self.assertRaises(ValueError): UdpOwner(invalid)

    def test_port_conflict_is_not_silently_reassigned(self):
        server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(1)
        self.addCleanup(server.close)
        relay = InstrumentRelay('127.0.0.1', server.getsockname()[1], 'tcp', '127.0.0.1', 1, None)
        with self.assertRaises(OSError): relay.start()

    def test_ping_statistics_do_not_count_as_consultation(self):
        counters = RelayCounters()
        counters.ping(10); counters.ping(None); counters.ping(30)
        stats = counters.snapshot()
        self.assertEqual(stats['ping']['median_ms'], 20)
        self.assertAlmostEqual(stats['ping']['loss_percent'], 33.333, places=3)
        self.assertEqual(stats['bytes'], 0)
        self.assertIsNone(stats['last_exchange'])
        self.assertEqual(stats['state'], 'unknown')


class NamespaceLifecycleTests(unittest.TestCase):
    def manager(self):
        import tempfile
        from pathlib import Path
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            if args == ['busybox', '--list']: return 'udhcpc'
            if args[0] == 'wpa_cli': return 'wpa_state=COMPLETED'
            return ''
        with patch('wifi_bridge.isolated.linux_boot_id', return_value='test-boot'):
            manager = IsolatedRadios(root.name, runner)
        self.assertEqual(manager.records, {})
        return manager, calls

    def test_radio_move_is_journaled_and_only_own_namespace_is_configured(self):
        manager, calls = self.manager()
        device = {'name': 'wlan-test', 'mac': '02:00:00:00:00:01'}
        profile = {'id':'mount','kind':'zwo_mount','ssid':'AMH_TEST','password':'private-test-password',
                   'isolated':True,'enabled':True,'target_ip':'192.168.4.1','client_port':14030}
        def spawn(record, args, **env):
            self.assertEqual(json.loads(manager._file(device['mac']).read_text())['created'], True)
            self.assertNotIn(profile['password'], json.dumps(args))
        with patch('wifi_bridge.isolated.shutil.which', return_value='/test/tool'), \
             patch('wifi_bridge.isolated.phy_name', return_value='phy99'), \
             patch.object(manager, '_spawn', side_effect=spawn), \
             patch.object(manager, 'address', return_value='192.168.4.2/24'):
            manager.connect('telescope-mount', device, profile)
        namespace = namespace_name(device['mac'])
        self.assertIn((['iw','phy','phy99','set','netns','name',namespace], {}), calls)
        configs = [kw['input'] for args,kw in calls if 'input' in kw]
        self.assertEqual(len(configs), 1)
        self.assertIn('tcp dport 4030', configs[0]); self.assertNotIn('21180', configs[0])
        wpa = (manager.directory/namespace/'wpa.conf').read_text()
        self.assertNotIn(profile['password'], wpa)
        self.assertEqual((manager.directory/namespace/'wpa.conf').stat().st_mode & 0o777, 0o600)
        # Recovery after a service restart finds ownership rather than forgetting
        # the moved PHY and attempting to allocate its radio to another role.
        with patch('wifi_bridge.isolated.linux_boot_id', return_value='test-boot'):
            recovered = IsolatedRadios(manager.directory.parent, manager.run)
        self.assertEqual(recovered.records[device['mac']]['role'], 'telescope-mount')

    def test_restart_rejects_stale_secret_or_transport_intent(self):
        manager, calls = self.manager()
        device={'name':'wlan-test','mac':'02:00:00:00:00:01'}
        profile={'id':'mount','kind':'zwo_mount','ssid':'AMH_TEST','password':'private-old-password',
                 'mac':device['mac'],'enabled':True,'isolated':True,'target_ip':'192.168.4.1','client_port':14030}
        record={'profile_id':'mount','role':'telescope-mount','intent':manager.intent(profile)}
        manager.records[device['mac']]=record
        changed=dict(profile,password='private-new-password')
        self.assertFalse(manager.connected('telescope-mount',device,changed))
        self.assertFalse(manager.connected('telescope-mount',device,dict(profile,isolated=False)))
        self.assertNotEqual(manager.intent(profile),manager.intent(changed))
        self.assertNotIn(profile['password'],json.dumps(record))
        self.assertFalse(calls)


    def test_failed_association_rolls_back_selected_radio_only(self):
        from wifi_bridge.network import NetworkError
        manager, calls = self.manager()
        device = {'name':'wlan-test','mac':'02:00:00:00:00:01'}
        profile = {'id':'mount','kind':'synscan','ssid':'SynScan_TEST','password':'',
                   'isolated':True,'target_ip':'192.168.4.1','enabled':True,'client_port':21180}
        with patch('wifi_bridge.isolated.shutil.which', return_value='/test/tool'), \
             patch('wifi_bridge.isolated.phy_name', return_value='phy99'), \
             patch.object(manager,'_spawn',side_effect=NetworkError('Synthetic association failure')), \
             patch.object(manager,'release') as release:
            with self.assertRaises(NetworkError): manager.connect('telescope-mount',device,profile)
        release.assert_called_once_with('telescope-mount')

    def test_unavailable_ping_is_not_fabricated_packet_loss(self):
        manager, _ = self.manager()
        record={'device':{'mac':'02:00:00:00:00:01','name':'wlan-test'},'target':'192.168.4.1'}
        manager.records[record['device']['mac']]=record
        manager.counters['mount']=RelayCounters()
        with patch('wifi_bridge.isolated.shutil.which',return_value='/test/ping'), \
             patch.object(manager,'command',return_value=''):
            manager._ping('mount',record)
            deadline=time.monotonic()+2
            while 'mount' in manager.pinging and time.monotonic()<deadline: time.sleep(.01)
        result=manager.counters['mount'].snapshot()['ping']
        self.assertEqual(result['reason'],'PROBE_UNAVAILABLE')
        self.assertIsNone(result['loss_percent']);self.assertEqual(result['samples'],0)


    def test_invalid_ownership_never_authorizes_network_mutation(self):
        manager, calls = self.manager()
        path = manager.directory/'radio-020000000001.json'
        path.write_text(json.dumps({'boot_id':'test-boot','device':{'mac':'02:00:00:00:00:01','name':'unsafe/name'},
                                    'namespace':'unrelated','phy':'phy99'}))
        with patch('wifi_bridge.isolated.linux_boot_id',return_value='test-boot'):
            with self.assertRaises(RuntimeError): IsolatedRadios(manager.directory.parent,manager.run)
        self.assertFalse(calls)

    def test_setns_failure_does_not_create_upstream_in_root_namespace(self):
        with patch('wifi_bridge.isolated.in_namespace',side_effect=PermissionError('Denied')), \
             patch('wifi_bridge.isolated.socket.socket') as create:
            with self.assertRaises(PermissionError): namespace_socket('/unavailable','wlan-test',socket.SOCK_STREAM)
        create.assert_not_called()


class NamespaceDescriptorTests(unittest.TestCase):
    def test_target_open_failure_closes_root_descriptor(self):
        from wifi_bridge.isolated import in_namespace
        with patch('wifi_bridge.isolated.os.open',side_effect=[31,FileNotFoundError()]), \
             patch('wifi_bridge.isolated.os.close') as close:
            with self.assertRaises(FileNotFoundError):
                with in_namespace('/missing'): pass
        close.assert_called_once_with(31)
