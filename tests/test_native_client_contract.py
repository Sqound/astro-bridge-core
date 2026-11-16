"""Executable v1 examples: schemas, strict HTTP replay and implemented demo flows."""
import copy
import http.client
import importlib.util
import json
from pathlib import Path
import re
import selectors
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

if __package__:
    from .helpers.contract_schema import validate
    from .helpers.contract_compatibility import assert_additive, native_snapshot
    from .helpers.native_instrument_flows import NAMES as INSTRUMENT_FLOWS, run as run_instruments
else:  # unittest discovery imports this file as a top-level module.
    from helpers.contract_schema import validate
    from helpers.contract_compatibility import assert_additive, native_snapshot
    from helpers.native_instrument_flows import NAMES as INSTRUMENT_FLOWS, run as run_instruments
from wifi_bridge.config import Store, password_hash
from wifi_bridge.network import DemoNetwork

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / 'contracts/native-client/v1'
SPEC = json.loads((ROOT / 'wifi_bridge/static/openapi.json').read_text())
FLOWS = [json.loads(p.read_text()) for p in sorted(CONTRACT.glob('*.json')) if p.name not in ('STATUS.json', 'openapi-snapshot.json')]
STATUS = json.loads((CONTRACT / 'STATUS.json').read_text())
SNAPSHOT = json.loads((CONTRACT / 'openapi-snapshot.json').read_text())
loader = importlib.util.spec_from_file_location('contract_mock', ROOT / 'tools/contract-mock.py')
mock = importlib.util.module_from_spec(loader)
loader.loader.exec_module(mock)


def operation(request):
    path = urlsplit(request['path']).path
    for template, methods in SPEC['paths'].items():
        pattern = re.escape(template).replace(r'\{id\}', '([^/]+)').replace(r'\{family\}', '([^/]+)')
        match = re.fullmatch(pattern, path)
        if match and request['method'].lower() in methods:
            return methods[request['method'].lower()], match.groups()
    raise AssertionError('No OpenAPI route for ' + path)


def send(port, request):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    headers = dict(request['headers'])
    body = None if request['body'] is None else json.dumps(request['body'])
    if body is not None:
        headers['Content-Type'] = 'application/json'
    connection.request(request['method'], request['path'], body, headers)
    response = connection.getresponse()
    value = json.loads(response.read())
    result = response.status, {k.lower(): v for k, v in response.getheaders()}, value
    connection.close()
    return result


def pointer_parent(value, pointer):
    """Resolve a bounded response JSON pointer without accepting executable patterns."""
    assert pointer.startswith('/') and len(pointer) <= 256
    parts = [part.replace('~1', '/').replace('~0', '~') for part in pointer[1:].split('/')]
    assert len(parts) <= 20
    for part in parts[:-1]:
        value = value[int(part)] if isinstance(value, list) else value[part]
    key = int(parts[-1]) if isinstance(value, list) else parts[-1]
    return value, key


def substitute(value, bindings):
    if isinstance(value, dict):
        return {key: substitute(item, bindings) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, bindings) for item in value]
    if isinstance(value, str):
        for placeholder, actual in sorted(bindings.items(), key=lambda pair: -len(pair[0])):
            value = value.replace(placeholder, actual)
    return value


class NativeContractTests(unittest.TestCase):
    def test_frozen_v1_openapi_is_additive(self):
        assert_additive(SNAPSHOT, SPEC)
        # Prevent a changed generator from hiding behind a stale shipped contract.
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/generate_openapi.py')],
                                capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(result.stderr, '')
        self.assertEqual(SPEC, json.loads((ROOT / 'wifi_bridge/static/openapi.json').read_text()))

    def test_freeze_requires_every_flow_to_be_real(self):
        self.assertEqual({name for name, status in STATUS.items() if status != 'real'}, set())

    def test_service_event_outcomes_match_the_frozen_http_schema(self):
        from wifi_bridge.events import ConnectionEvents, EventStore
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / 'events.json')
            events = ConnectionEvents(store)
            for outcome in ('not_answering', 'unknown', 'ready'):
                events.service({'id': 'test-instrument', 'name': 'Test instrument'},
                               {'state': outcome, 'method': 'tcp_connect'}, True)
                record = store.events[-1]
                self.assertEqual(record['outcome'], outcome)
                validate(record, SPEC['components']['schemas']['ConnectionEvent'], SPEC)

    def test_scenario_inventory_and_schema_validation(self):
        required = {'discovery-txt', 'info-public', 'pair-and-token', 'token-revoked-repair',
                    'instruments-list', 'connect-ready', 'connect-allocating', 'connect-needs-replacement',
                    'connect-protected', 'connect-disabled-by-owner', 'stale-revision',
                    'lease-renew-and-release', 'disconnect-by-user', 'lost-and-restored',
                    'moved-to-other-radio', 'events-wait-timeout', 'events-gap-reset',
                    'hotspot-path', 'telemetry-catch-up'}
        self.assertLessEqual(required, set(STATUS))
        self.assertEqual(set(STATUS), {f['name'] for f in FLOWS})
        for flow in FLOWS:
            with self.subTest(scenario=flow['name']):
                self.assertEqual(flow['contract_version'], 1)
                self.assertIn(STATUS[flow['name']], ('real', 'mock'))
                self.assertTrue(flow['description'])
                self.assertTrue(flow['steps'])
                self.assertIsInstance(flow['requires'], list)
                for step in flow['steps']:
                    request, response = step['request'], step['response']
                    entry, ids = operation(request)
                    self.assertIn('native-client', entry['tags'])
                    self.assertIn(entry['x-contract-status'], ('planned', 'implemented'))
                    if request['body'] is not None:
                        validate(request['body'], entry['requestBody']['content']['application/json']['schema'], SPEC)
                    validate(response['body'], entry['responses'][str(response['status'])]['content']['application/json']['schema'], SPEC)
                    query = parse_qs(urlsplit(request['path']).query, keep_blank_values=True)
                    for parameter in entry.get('parameters', []):
                        name, location = parameter['name'], parameter['in']
                        value = ids[0] if location == 'path' else request['headers'].get(name) if location == 'header' else query.get(name, [None])[0]
                        if value is None:
                            self.assertFalse(parameter.get('required', False), name)
                            continue
                        if parameter['schema'].get('type') == 'integer':
                            self.assertRegex(value, r'^\d+$')
                            value = int(value)
                        validate(value, parameter['schema'], SPEC)

    def test_schema_validator_rejects_invalid_values(self):
        schema = {'type': 'object', 'properties': {'n': {'type': 'integer', 'minimum': 1, 'maximum': 2}, 's': {'type': 'string', 'maxLength': 2, 'enum': ['ok']}}, 'required': ['n', 's'], 'additionalProperties': False}
        for bad in ({'n': True, 's': 'ok'}, {'n': 0, 's': 'ok'}, {'n': 3, 's': 'ok'}, {'n': 1}, {'n': 1, 's': 'bad'}, {'n': 1, 's': 'ok', 'extra': 1}):
            with self.assertRaises(AssertionError):
                validate(bad, schema, SPEC)
        with self.assertRaises(AssertionError):
            validate(['x'], {'type': 'array', 'items': {'type': 'integer'}}, SPEC)

    def test_all_scenarios_replay_over_http(self):
        server = mock.ReplayServer(('127.0.0.1', 0), FLOWS)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for flow in FLOWS:
                for step in flow['steps']:
                    with self.subTest(scenario=flow['name'], step=step['note']):
                        status, headers, body = send(server.server_port, step['request'])
                        self.assertEqual(status, step['response']['status'])
                        self.assertEqual(body, step['response']['body'])
                        for name, value in step['response']['headers'].items():
                            self.assertEqual(headers[name.lower()], value)
            self.assertEqual(server.index, len(server.steps))
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_strict_mismatch_does_not_advance_or_echo_credentials(self):
        server = mock.ReplayServer(('127.0.0.1', 0), [next(f for f in FLOWS if f['name'] == 'current-admin-token')])
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            request = copy.deepcopy(server.steps[0]['request'])
            request['body']['password'] = 'not-the-documented-placeholder'
            status, _, body = send(server.server_port, request)
            self.assertEqual(status, 599)
            self.assertEqual(body['diff'], ['JSON body differs'])
            self.assertNotIn(request['body']['password'], json.dumps(body))
            self.assertEqual(server.index, 0)
            self.assertEqual(send(server.server_port, server.steps[0]['request'])[0], 200)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_discovery_and_fixture_privacy(self):
        for flow in FLOWS:
            serialized = json.dumps(flow)
            self.assertIsNone(re.search(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', serialized))
            self.assertNotIn('-----BEGIN ', serialized)
            for address in re.findall(r'(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])', serialized):
                self.assertTrue(address in ('192.0.2.10', '192.0.2.11', '192.168.77.1') or re.fullmatch(r'198\.51\.100\.[0-9]{1,3}', address), address)
            self.assertIsNone(re.search(r'(?i)(?:seestar|vespera|dwarf)[-_][0-9a-f]{6}', serialized))
            def walk(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if 'password' in key:
                            self.assertEqual(item, 'test-password-1')
                        if key.lower() in ('ssid', 'bssid', 'mac', 'serial', 'serial_number'):
                            self.fail('Forbidden identifier field ' + key)
                        walk(item)
                elif isinstance(value, list):
                    for item in value: walk(item)
            walk(flow)
        discovery = next(f for f in FLOWS if f['name'] == 'discovery-txt')['discovery']
        self.assertEqual({row['name'] for row in discovery}, {'minimal', 'full', 'hotspot'})
        discovery_flow = next(f for f in FLOWS if f['name'] == 'discovery-txt')
        for row, step in zip(discovery, discovery_flow['steps'], strict=True):
            self.assertEqual(row['port'], 80)
            self.assertEqual(row['service'], '_astrobridge._tcp')
            self.assertTrue(all(len(s.encode()) <= 255 for s in row['txt']))
            self.assertLessEqual(sum(len(s.encode()) + 1 for s in row['txt']), 1300)
            txt = dict(s.split('=', 1) for s in row['txt'])
            self.assertEqual(txt['id'], '0123456789abcdef')
            self.assertEqual(txt['id'], step['response']['body']['device_id'])
            self.assertEqual([cap for cap in txt['caps'].split(',') if cap], step['response']['body']['capabilities'])
            self.assertLessEqual(len(txt['name'].encode()), 63)

    def test_instrument_scenarios_against_real_http_controller(self):
        for flow in FLOWS:
            if flow['name'] not in INSTRUMENT_FLOWS: continue
            with self.subTest(scenario=flow['name']):
                self.assertEqual(STATUS[flow['name']], 'real')
                actual = run_instruments(flow)
                self.assertEqual(len(actual), len(flow['steps']))
                if flow['name'] in ('lost-and-restored','moved-to-other-radio'):
                    events=[event for _,body in actual for event in body.get('events',[])]
                    kinds={event['type'] for event in events}
                    required={'instrument.lost','instrument.restored','instrument.state'} if flow['name']=='lost-and-restored' else {'instrument.moved','instrument.endpoint_changed'}
                    self.assertLessEqual(required,kinds)
                    self.assertNotIn('lease.ended',kinds)
                    for event in events:
                        if event['type']=='instrument.moved':self.assertNotEqual(event['from_radio_id'],event['to_radio_id'])
                if flow['name']=='events-gap-reset':
                    self.assertEqual([body['reset_reason'] for _,body in actual if 'reset_reason' in body],
                                     ['BOOT_CHANGED','RETENTION_GAP','CURSOR_AHEAD','HISTORY_UNAVAILABLE'])
                if flow['name']=='events-wait-timeout':
                    self.assertEqual(actual[0][1]['events'],[])
                    self.assertEqual(actual[0][1]['next_cursor'],actual[0][1]['latest_sequence'])
                for step, (status, body) in zip(flow['steps'], actual):
                    self.assertEqual(status, step['response']['status'], body)
                    for pointer, pattern in step.get('real_patterns', {}).items():
                        parent, key = pointer_parent(body, pointer)
                        self.assertRegex(str(parent[key]), pattern)
                        expected, expected_key = pointer_parent(step['response']['body'], pointer)
                        parent[key] = expected[expected_key]
                    self.assertEqual(body, step['response']['body'])

    def test_cli_lists_every_scenario(self):
        result = subprocess.run([sys.executable, str(ROOT / 'tools/contract-mock.py'), '--list'], capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(set(result.stdout.splitlines()), set(STATUS))

    def test_implemented_scenarios_and_planned_guards_against_demo_app(self):
        with tempfile.TemporaryDirectory() as directory:
            net = DemoNetwork()
            store = Store(directory, net.devices()[0]['mac'])
            config = dict(store.config, admin_hash=password_hash('test-password-1'))
            store.save(config)
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
            process = subprocess.Popen([sys.executable, '-m', 'tests.helpers.clock_fixture', '--demo', '--state-dir', directory, '--bind', '127.0.0.1', '--port', str(port)], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(15), 'Demo startup timed out')
                    self.assertIn('SIMULATION', process.stdout.readline())
                for flow in FLOWS:
                    if flow['name'] in INSTRUMENT_FLOWS:
                        continue  # Dedicated real HTTP fixture below supplies synthetic radios.
                    if STATUS[flow['name']] != 'real':
                        # Every fully implemented flow must be upgraded in STATUS.
                        implemented = all(operation(s['request'])[0]['x-contract-status'] == 'implemented' for s in flow['steps'])
                        self.assertFalse(implemented, flow['name'] + ' must run against the app')
                        continue
                    bindings = {}
                    for step in flow['steps']:
                        request = substitute(step['request'], bindings)
                        status, headers, actual = send(port, request)
                        self.assertEqual(status, step['response']['status'])
                        for pointer, pattern in (flow.get('real_patterns', {}) | step.get('real_patterns', {})).items():
                            try:
                                expected_parent, key = pointer_parent(step['response']['body'], pointer)
                                expected_value = expected_parent[key]
                            except (KeyError, IndexError):
                                continue  # A flow-level pattern may apply to another step.
                            actual_parent, actual_key = pointer_parent(actual, pointer)
                            actual_value = actual_parent[actual_key]
                            self.assertRegex(str(actual_value), pattern)
                            if isinstance(expected_value, str) and isinstance(actual_value, str):
                                bindings[expected_value] = actual_value
                            actual_parent[actual_key] = expected_value
                        self.assertEqual(actual, step['response']['body'])
                        self.assertEqual(headers['cache-control'], 'no-store')
                _, _, login = send(port, {'method': 'POST', 'path': '/api/v1/auth/token', 'headers': {}, 'body': {'password': 'test-password-1'}})
                for method, path, body in [('GET', '/telemetry/samples', None)]:
                    status, _, body = send(port, {'method': method, 'path': '/api/v1' + path, 'headers': {'Authorization': 'Bearer ' + login['access_token']}, 'body': body})
                    self.assertEqual(status, 400, path)
                    self.assertEqual(body['error']['code'], 'INVALID_FIELDS', path)
            finally:
                process.terminate()
                try: process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)
                process.stdout.close()


class NativeCompatibilityTests(unittest.TestCase):
    def test_rejects_breaking_route_schema_and_security_changes(self):
        route = ('paths', '/api/v1/instruments/{id}/connect', 'post')
        event = ('components', 'schemas', 'NativeEvent', 'oneOf')
        request = ('components', 'schemas', 'NativeConnectRequest')
        response = ('components', 'schemas', 'NativeInstrument')
        telemetry = ('components', 'schemas', 'NativeTelemetryPage')
        cases = [
            (('paths', '/api/v1/instruments'), None),
            (route, None),
            (route + ('x-required-scopes',), ['admin']),
            (route + ('security',), []),
            (route + ('tags',), []),
            (route + ('x-contract-status',), 'planned'),
            (route + ('responses', '202'), None),
            (route + ('responses', '200', 'content', 'application/json'), None),
            (route + ('parameters',), []),
            (route + ('parameters', 1, 'schema', 'pattern'), '^changed$'),
            (route + ('parameters', 1, 'name'), 'New-Revision'),
            (route + ('parameters', 1, 'in'), 'query'),
            (route + ('requestBody',), None),
            (route + ('requestBody', 'content', 'application/json', 'encoding'), {'lease_seconds': {'style': 'form'}}),
            (request + ('required',), ['lease_seconds']),
            (request + ('properties', 'lease_seconds', 'maximum'), 900),
            (request + ('properties', 'lease_seconds', 'default'), 120),
            (request + ('properties', 'allow_replace'), None),
            (response + ('properties', 'id', 'type'), 'integer'),
            (response + ('properties', 'link', 'enum'), ['connected']),
            (response + ('properties', 'service'), None),
            (response + ('required',), ['id']),
            (response + ('properties', 'name', 'maxLength'), 128),
            (response + ('additionalProperties',), True),
            (event, SNAPSHOT['components']['schemas']['NativeEvent']['oneOf'][1:]),
            (event, SNAPSHOT['components']['schemas']['NativeEvent']['oneOf'] + [{}]),
            (event, SNAPSHOT['components']['schemas']['NativeEvent']['oneOf'] + [{'$ref': '#/components/schemas/ConnectionEvent'}]),
            (event + (7, 'properties', 'type', 'enum'), ['telemetry.session_started', 'telemetry.session_ended']),
            (('security',), []),
            (('components', 'securitySchemes', 'BearerAuth', 'scheme'), 'basic'),
            (('servers',), []),
            (('components', 'schemas', 'TelemetrySample', 'properties', 'metric'), None),
            (('components', 'schemas', 'TelemetryEvent', 'properties', 'reason'), None),
            (telemetry + ('properties', 'samples', 'items'), {'type':'object'}),
            (telemetry + ('properties', 'samples', 'type'), 'object'),
            (telemetry + ('properties', 'samples', 'maxItems'), 501),
            (telemetry + ('properties', 'samples', 'maxItems'), None),
            (telemetry + ('properties', 'samples', 'minItems'), 1),
            (telemetry + ('properties', 'next_cursor', 'type'), 'string'),
            (telemetry + ('required',), ['samples', 'events']),
            (('paths', '/api/v1/telemetry/samples', 'get', 'x-required-scopes'), ['read']),
        ]
        for path, replacement in cases:
            with self.subTest(path=path, replacement=replacement):
                changed = copy.deepcopy(SNAPSHOT)
                parent = changed
                for key in path[:-1]: parent = parent[key]
                if replacement is None: del parent[path[-1]]
                else: parent[path[-1]] = replacement
                with self.assertRaises(AssertionError): assert_additive(SNAPSHOT, changed)

    def test_allows_additive_routes_optional_inputs_and_outputs(self):
        changed = copy.deepcopy(SNAPSHOT)
        self.assertNotIn('/api/v1/future-test-resource', changed['paths'])
        changed['paths']['/api/v1/future-test-resource'] = {'get': {'tags': ['native-client']}}
        changed['paths']['/api/v1/instruments']['get'].setdefault('parameters', []).append(
            {'in': 'query', 'name': 'future', 'required': False, 'schema': {'type': 'boolean'}})
        request = changed['components']['schemas']['NativeConnectRequest']
        request['properties']['future_option'] = {'type': 'boolean'}
        response = changed['components']['schemas']['NativeInstrument']
        response['properties']['future_state'] = {'type': 'string'}
        response['required'].append('future_state')
        response['properties']['link']['enum'].append('future_link')
        changed['paths']['/api/v1/instruments']['get']['summary'] = 'Updated explanation'
        event = copy.deepcopy(changed['components']['schemas']['NativeEvent']['oneOf'][0])
        event['properties']['type']['enum'] = ['camera.attached']
        changed['components']['schemas']['NativeEvent']['oneOf'].insert(0, event)
        assert_additive(SNAPSHOT, changed)

    def test_new_required_parameter_is_not_an_additive_extension(self):
        changed = copy.deepcopy(SNAPSHOT)
        changed['paths']['/api/v1/instruments']['get'].setdefault('parameters', []).append(
            {'in': 'query', 'name': 'future', 'required': True, 'schema': {'type': 'boolean'}})
        with self.assertRaises(AssertionError): assert_additive(SNAPSHOT, changed)

    def test_snapshot_follows_transitive_schemas_and_excludes_unrelated_routes(self):
        snapshot = native_snapshot(SPEC)
        self.assertIn('NativeAddress', snapshot['components']['schemas'])
        self.assertIn('ConnectionEvent', snapshot['components']['schemas'])
        self.assertNotIn('/api/v1/system/metrics', snapshot['paths'])
        self.assertNotIn('SystemMetrics', snapshot['components']['schemas'])
        assert_additive(SNAPSHOT, snapshot)


if __name__ == '__main__':
    unittest.main()
