"""Actual private registry/migration and reconciler retry boundaries in temporary roots."""
import copy
import json
import os
from pathlib import Path
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.api import ApiError, MobileAPI
from wifi_bridge.controller import Controller
from wifi_bridge.paused import PausedRoles


class PausedTests(Fixture):
    def setUp(self):
        super().setUp(); self.configured(); self.api = MobileAPI(self.controller)

    def test_first_install_marker_repair_and_lost_or_malformed_registry_fail_closed(self):
        root = Path(self.tmp.name)
        p = self.controller.pauses
        self.assertFalse(p.error); self.assertEqual(p.records, {})
        profile = self.store.config['telescopes'][0]; p.hold(profile)
        self.assertEqual(p.path.stat().st_mode & 0o777, 0o600)
        p.marker.unlink(); repaired = PausedRoles(root)
        self.assertEqual(repaired.records, p.records); self.assertTrue(repaired.marker.exists())
        for raw in (None, 'null', '[]', '{"version":1,"holds":[]}', '{"version":1,"holds":{"bad":{}}}'):
            if raw is None: p.path.unlink()
            else: p.path.write_text(raw)
            lost = PausedRoles(root); self.assertTrue(lost.error); self.assertTrue(lost.held('anything'))
        p.persist(p.records); p.path.unlink(); os.mkfifo(p.path)
        self.assertTrue(PausedRoles(root).error)
        p.path.unlink(); p.path.symlink_to(p.marker)
        self.assertTrue(PausedRoles(root).error)

    def test_no_secrets_fingerprint_and_holds_survive_current_password_or_intent_changes(self):
        p = self.controller.pauses; profile = self.store.config['telescopes'][0]
        p.hold(profile); before = p.path.read_text()
        changed = dict(profile, password='changed-private-password')
        self.assertEqual(p.fingerprint(changed), p.fingerprint(profile))
        self.assertNotIn(profile['password'], before); self.assertNotIn('changed-private-password', before)
        changed['ssid'] = 'Other private network'
        self.assertEqual(p.status({'telescopes': [changed]})['profiles']['primary'], 'PAUSED_INTENT_CHANGED')
        self.store.config['telescopes'][0]['password'] = changed['password']
        restart = Controller(self.store, self.net, self.controller.bootstrap, demo=True)
        with patch.object(self.net, 'connect', wraps=self.net.connect) as connect:
            restart.cycle(0); restart.cycle(100)
        self.assertEqual([c.args[0] for c in connect.call_args_list], ['home'])
        self.assertEqual(restart.state['telescopes'][0]['state'], 'paused')
        self.assertFalse(restart.update_safe())

    def test_guarded_manual_retry_and_metadata_omission_preserve_holds(self):
        self.controller.pauses.hold(self.store.config['telescopes'][0])
        self.api.update('telescopes', {'auto_replaceable': True}, ident='primary')
        self.assertTrue(self.controller.pauses.held('primary'))
        for rev in (None, '*', '"old"'):
            with self.assertRaises(ApiError): self.api.action('primary', 'connect', expected=rev)
        self.api.action('primary', 'connect', expected='"'+self.api.revision()+'"')
        self.assertFalse(self.controller.pauses.held('primary'))
        self.controller.cycle(0); self.assertTrue(self.controller.state['relay_ready'])

    def test_recovery_reauth_revision_protection_and_all_profiles_paused(self):
        c = self.controller; c.pauses.path.unlink(); c.pauses = PausedRoles(self.store.path.parent)
        body = {'confirm_recovery': True, 'current_password': 'local-admin-test-password'}
        for value, rev in (({}, None), (body, '*'), (dict(body,current_password='wrong'), '"'+self.api.revision()+'"')):
            with self.assertRaises(ApiError): c.pauses.recover(c, value, rev, self.api.revision)
        c.store.config['telescopes'][0]['owner_locked'] = True
        with self.assertRaises(ApiError): c.pauses.recover(c, body, '"'+self.api.revision()+'"', self.api.revision)
        c.store.config['telescopes'][0]['owner_locked'] = False
        self.assertTrue(c.pauses.recover(c, body, '"'+self.api.revision()+'"', self.api.revision)['profiles'])
        self.assertFalse(c.pauses.error); self.assertTrue(c.pauses.held('primary'))
        c.cycle(0); self.assertFalse(c.state['relay_ready'])
        self.api.action('primary','disconnect',expected='"'+self.api.revision()+'"'); self.assertFalse(c.pauses.held('primary'))

    def test_persistence_failure_retains_ram_hold_and_never_reports_clear_success(self):
        p = self.controller.pauses
        with patch('wifi_bridge.paused.atomic_json', side_effect=OSError('private failure')): p.hold(self.store.config['telescopes'][0])
        self.assertTrue(p.held('primary')); self.assertEqual(p.error,'QUARANTINE_PERSISTENCE_FAILED')
        p.persist(p.records)
        with patch('wifi_bridge.paused.atomic_json', side_effect=OSError('private failure')):
            with self.assertRaises(ApiError): p.clear({'primary'})
        self.assertTrue(p.held('primary'))

    def test_corrupt_marker_cannot_masquerade_as_first_install(self):
        p = self.controller.pauses
        for raw in ('null', '[]', '{"version":true,"initialized":true}', '{"version":1,"initialized":1}'):
            p.marker.write_text(raw); p.path.unlink(missing_ok=True)
            self.assertTrue(PausedRoles(self.store.path.parent).error)
