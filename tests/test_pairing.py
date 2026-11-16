"""Paired credentials, persistence and fail-closed native permissions."""
import base64
import hashlib
import hmac
import json
import secrets
import stat
from unittest.mock import patch

import test_api
import threading
from wifi_bridge.web import WebServer
from test_bridge import Fixture
from wifi_bridge.api import MobileAPI
from wifi_bridge.config import password_matches
from wifi_bridge.pairing import PairedClients, SCOPES


class PairingTests(Fixture):
    request = test_api.APITests.request
    login = test_api.APITests.login

    def setUp(self):
        super().setUp()
        self.configured()
        self.controller.cycle(0)
        self.server = WebServer(('127.0.0.1', 0), self.controller)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.token = ''

    def pair(self, scopes=None):
        code, paired, _ = self.request('POST', '/api/v1/auth/clients', {
            'name': 'Synthetic observing app', 'scopes': scopes or ['read', 'connect', 'telemetry', 'camera']})
        self.assertEqual(code, 201)
        return paired

    def client_login(self, paired):
        return self.request('POST', '/api/v1/auth/token', {
            'client_id': paired['client_id'], 'client_secret': paired['client_secret']})

    def test_once_only_hash_persistence_identity_and_restart(self):
        self.login()
        paired = self.pair()
        self.assertEqual(len(base64.urlsafe_b64decode(paired['client_secret'] + '=')), 32)
        private = self.server.paired_clients
        record = private.state['clients'][0]
        self.assertTrue(password_matches(paired['client_secret'], record['secret_hash']))
        self.assertNotIn(paired['client_secret'], private.path.read_text())
        self.assertEqual(stat.S_IMODE(private.path.stat().st_mode), 0o600)
        identity = private.path.parent / 'install-secret'
        self.assertEqual(stat.S_IMODE(identity.stat().st_mode), 0o600)
        self.assertEqual(paired['device_id'], hmac.new(identity.read_bytes(), b'astro-bridge-id', 'sha256').hexdigest()[:16])
        code, token, _ = self.client_login(paired)
        self.assertEqual(code, 200)
        self.assertEqual(set(token), {'access_token', 'token_type', 'expires_in', 'scopes'})
        self.assertEqual(token['scopes'], paired['scopes'])
        self.assertIsNotNone(record['last_used_utc_best_effort'])
        restored = PairedClients(private.path.parent)
        self.assertEqual(restored.device_id, paired['device_id'])
        self.assertEqual(restored.authenticate(paired['client_id'], paired['client_secret'])['scopes'], paired['scopes'])
        self.assertEqual(restored.state['clients'][0]['created_seq'], 1)
        # A real new listener/controller lifetime starts with no token state.
        from wifi_bridge.web import WebServer
        restarted = WebServer(('127.0.0.1', 0), self.controller)
        try:
            self.assertEqual(restarted.sessions, {})
            self.assertEqual(restarted.paired_clients.authenticate(paired['client_id'], paired['client_secret'])['client_id'], paired['client_id'])
        finally:
            restarted.server_close()

    def test_no_secrets_in_get_responses_or_logs(self):
        self.login()
        with patch('builtins.print') as output:
            paired = self.pair()
            self.client_login(paired)
            for path in ['/auth/clients', '/status', '/config', '/info', '/openapi.json', '/telescopes', '/endpoints']:
                code, result, _ = self.request('GET', '/api/v1' + path)
                self.assertEqual(code, 200)
                encoded = json.dumps(result)
                self.assertNotIn(paired['client_secret'], encoded)
                self.assertNotIn(self.server.paired_clients.state['clients'][0]['secret_hash'], encoded)
            self.assertNotIn(paired['client_secret'], str(output.call_args_list))
        listed = self.request('GET', '/api/v1/auth/clients')[1]
        self.assertEqual(set(listed), {'clients', 'config_revision'})
        self.assertEqual(set(listed['clients'][0]), {'client_id', 'name', 'scopes', 'created_seq', 'last_used_utc_best_effort'})

    def test_revocation_requires_fresh_revision_kills_tokens_and_preserves_other_clients(self):
        self.login()
        paired, other = self.pair(), self.pair()
        token = self.client_login(paired)[1]['access_token']
        other_token = self.client_login(other)[1]['access_token']
        path = '/api/v1/auth/clients/' + paired['client_id']
        for revision, status in [(None, 428), ('*', 428), ('"outdated"', 412)]:
            self.assertEqual(self.request('DELETE', path, headers={'If-Match': revision} if revision else {})[0], status)
        revision = self.request('GET', '/api/v1/auth/clients')[1]['config_revision']
        self.assertEqual(self.request('DELETE', path, headers={'If-Match': '"' + revision + '"'})[0], 200)
        self.assertEqual(self.request('GET', '/api/v1/status', headers={'Authorization': 'Bearer ' + token})[0], 401)
        self.assertEqual(self.request('GET', '/api/v1/status', headers={'Authorization': 'Bearer ' + other_token})[0], 200)
        self.assertEqual(self.client_login(paired)[1]['error']['code'], 'INVALID_CLIENT')
        recreated = self.pair()
        self.assertEqual(self.server.paired_clients.state['clients'][-1]['created_seq'], 4)
        self.assertNotEqual(recreated['client_id'], paired['client_id'])

    def test_password_change_keeps_clients_but_revoke_all_removes_them(self):
        self.login()
        paired = self.pair()
        old_token = self.client_login(paired)[1]['access_token']
        password = secrets.token_urlsafe(24)
        code, _, _ = self.request('POST', '/api/v1/credentials', {
            'target': 'web', 'current_password': 'local-admin-test-password', 'password': password})
        self.assertEqual(code, 200)
        self.assertEqual(self.request('GET', '/api/v1/status')[0], 401)
        self.assertEqual(self.request('GET', '/api/v1/status', headers={'Authorization': 'Bearer ' + old_token})[0], 401)
        self.assertEqual(self.client_login(paired)[0], 200)
        self.login(password)
        self.assertEqual(self.request('POST', '/api/v1/auth/revoke-all', {})[0], 200)
        self.assertEqual(self.server.paired_clients.public(), [])
        self.assertEqual(self.client_login(paired)[1]['error']['code'], 'INVALID_CLIENT')
        self.assertEqual(PairedClients(self.store.path.parent).public(), [])

    def test_login_throttling_shared_with_password_and_session_bound(self):
        self.login()
        paired = self.pair()
        for _ in range(4):
            self.assertEqual(self.request('POST', '/api/v1/auth/token', {'password': secrets.token_urlsafe(12)})[0], 401)
        self.assertEqual(self.client_login(dict(paired, client_secret=secrets.token_urlsafe(32)))[0], 401)
        self.assertEqual(self.client_login(paired)[0], 429)
        self.server.attempts.clear()
        session = self.server.sessions[self.token]
        self.server.sessions = {str(i): dict(session) for i in range(32)}
        self.assertEqual(self.client_login(paired)[0], 429)

    def test_bounded_client_validation_and_limit(self):
        self.login()
        invalid = [{}, {'name': '', 'scopes': ['read']}, {'name': ' ' * 48, 'scopes': ['read']},
                   {'name': 'x' * 49, 'scopes': ['read']}, {'name': 'x\n', 'scopes': ['read']},
                   {'name': 'x', 'scopes': []}, {'name': 'x', 'scopes': ['read', 'read']},
                   {'name': 'x', 'scopes': ['unknown']}, {'name': 'x', 'scopes': [{}]},
                   {'name': 'x', 'scopes': ['read'], 'extra': True}]
        for body in invalid:
            self.assertEqual(self.request('POST', '/api/v1/auth/clients', body)[0], 400, body)
        for _ in range(16):
            self.pair(['read'])
        self.assertEqual(self.request('POST', '/api/v1/auth/clients', {'name': 'Overflow', 'scopes': ['read']})[0], 409)
        for body in [{'client_id': 'x'}, {'client_id': 'x', 'client_secret': []},
                     {'client_id': 'x', 'client_secret': 'x', 'password': 'x'}]:
            self.assertEqual(self.request('POST', '/api/v1/auth/token', body)[0], 400)

    def test_scope_matrix_covers_every_documented_route_and_unknowns(self):
        self.login()
        spec = self.server.api.specification()
        read_routes = {'/status', '/interfaces', '/telescopes', '/telescopes/{id}', '/telescopes/{id}/binding',
                       '/plugins', '/events', '/activity', '/protection', '/endpoints', '/endpoints/{id}',
                       '/operations/{id}', '/instruments', '/instruments/{id}', '/instruments/{id}/endpoint', '/system/time',
                       '/devices', '/serial-devices', '/serial-mounts', '/serial-mounts/{id}', '/serial-profiles'}
        for path, methods in spec['paths'].items():
            for method, entry in methods.items():
                route = path.removeprefix('/api/v1')
                expected = ('read' if method == 'get' and route in read_routes else
                            'read' if method == 'post' and route == '/system/time-hint' else
                            'connect' if method == 'post' and route in {'/instruments/{id}/' + action for action in ('connect', 'lease', 'release', 'disconnect')} else
                            'admin' if method == 'post' and route in {'/cameras/demo', '/cameras/{id}/release-owner'} else
                            'camera' if method == 'get' and route == '/cameras' else
                            'telemetry' if method == 'get' and route in {'/telemetry', '/telemetry/samples', '/telemetry/sessions', '/telemetry/sessions/{id}', '/telemetry/sessions/{id}/export', '/telemetry/sessions/{id}/analysis'} else
                            None if (method, route) in [('get', '/info'), ('get', '/openapi.json'), ('post', '/auth/token')] else
                            'authenticated' if (method, route) == ('delete', '/auth/token') else 'admin')
                self.assertEqual(entry['x-required-scopes'], [] if expected is None else [expected], (method, route))
                self.assertEqual(MobileAPI.required_scope(method.upper(), route), expected)
                self.assertEqual(MobileAPI.required_scope(method.upper(), route.replace('{id}', 'primary')), expected)
                for scope in SCOPES:
                    if expected in (None, 'authenticated', scope) or scope == 'admin':
                        continue
                    with self.subTest(method=method, route=route, scope=scope):
                        self.server.sessions[self.token]['scopes'] = [scope]
                        code, body, _ = self.request(method.upper(), path.replace('{id}', 'primary'), None if method == 'get' else {})
                        self.assertEqual(code, 403)
                        self.assertEqual(body['error']['code'], 'INSUFFICIENT_SCOPE')
        self.server.sessions[self.token]['scopes'] = ['read']
        self.assertEqual(self.request('GET', '/api/v1/future-unknown')[0], 403)
        for path in ('/status', '/interfaces', '/telescopes', '/plugins', '/endpoints'):
            self.assertEqual(self.request('GET', '/api/v1' + path)[0], 200)
        self.assertEqual(self.request('DELETE', '/api/v1/auth/token')[0], 200)

    def test_browser_list_revoke_csrf_and_transport_binding(self):
        self.login()
        paired = self.pair()
        scoped_token = self.client_login(paired)[1]['access_token']
        self.server.sessions[scoped_token]['transport'] = 'https'
        self.assertEqual(self.request('GET', '/api/v1/status', headers={'Authorization': 'Bearer ' + scoped_token})[0], 401)
        origin = 'http://127.0.0.1:' + str(self.server.server_port)
        _, body, cookie = self.request('POST', '/api/login', {'password': 'local-admin-test-password'}, {'Origin': origin})
        headers = {'Cookie': cookie.split(';')[0], 'Authorization': '', 'Origin': origin}
        snapshot = self.request('GET', '/api/status', headers=headers)[1]
        self.assertEqual(snapshot['paired_clients'][0]['client_id'], paired['client_id'])
        body = {'client_id': paired['client_id'], 'config_revision': snapshot['config_revision']}
        self.assertEqual(self.request('POST', '/api/auth/clients/revoke', body, headers)[0], 403)
        headers['X-CSRF-Token'] = snapshot['csrf']
        self.assertEqual(self.request('POST', '/api/auth/clients/revoke', body, headers)[0], 200)
