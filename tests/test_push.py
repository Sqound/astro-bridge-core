"""Real Web Push encryption roundtrip; lifecycle tests never contact a push provider."""
import base64
import json
from pathlib import Path
import plistlib
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from wifi_bridge.api import ApiError
from wifi_bridge.events import EventStore
from wifi_bridge.push import ConnectionNotices, PushService, validate_subscription


def encoded(raw): return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def subscription(suffix='test'):
    return {'endpoint': 'https://web.push.apple.com/' + suffix,
            'keys': {'p256dh': encoded(b'\x04' + b'a' * 64), 'auth': encoded(b'b' * 16)}}


class PushTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.controller = types.SimpleNamespace(store=types.SimpleNamespace(path=self.root/'config.json'), event_store=EventStore(self.root/'events.json'))
        self.tls = {'listener': {'ready': True}, 'identity': {'fingerprint_sha256': 'a'*64}}
        self.sent = []; self.response = 201
        def worker(request):
            if request['action'] == 'probe': return {'available': True}
            if request['action'] == 'keygen': return {'private_key': 'synthetic-private', 'public_key': 'synthetic-public'}
            self.sent.append(request); return {'status': self.response}
        self.service = PushService(self.controller, lambda: self.tls, invoke=worker, start=False)
        self.addCleanup(self.service.close)

    def action(self, action='subscribe', sub=None, **kw):
        return self.service.action(dict(action=action, subscription=sub or subscription(), **kw), 'https://astro-bridge.local')

    def event(self, name='Synthetic telescope', ident='scope', kind='connected', reason='READY', operation='synthetic-operation'):
        self.controller.event_store.append(kind, operation_id=operation, profile_id=ident, profile_name=name,
                                          radio_id='radio', cause='AUTOMATIC', reason=reason, outcome='ready')

    def flush(self):
        self.service.tick()
        with patch('wifi_bridge.push.time.monotonic', return_value=time.monotonic()+4): self.service.tick()

    def test_provider_allowlist_rejects_private_and_redirect_destinations_and_bad_keys(self):
        for url in ('http://web.push.apple.com/test', 'https://127.0.0.1/test', 'https://evil.test/test',
                    'https://web.push.apple.com.evil.test/test', 'https://web.push.apple.com:444/test',
                    'https://user@web.push.apple.com/test', 'https://web.push.apple.com/test#secret',
                    'https://web.push.apple.com/\r\nHost:evil', 'https://push.apple.com/test'):
            with self.subTest(url=url), self.assertRaises(ApiError): validate_subscription(dict(subscription(), endpoint=url))
        for keys in ({}, {'p256dh':'!', 'auth':'a'}, {'p256dh':encoded(b'x'*65),'auth':encoded(b'b'*16)}):
            with self.assertRaises(ApiError): validate_subscription(dict(subscription(), keys=keys))
        self.assertEqual(validate_subscription(subscription()), subscription())

    def test_requires_https_consent_and_private_persistence_no_secret_status(self):
        with self.assertRaises(ApiError): self.service.action({'action':'prepare'}, 'http://astro-bridge.local')
        self.action(locale='fr'); self.assertEqual(self.service.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.service.folder.stat().st_mode & 0o777, 0o700)
        status=json.dumps(self.service.status()); self.assertNotIn('endpoint', status); self.assertNotIn('synthetic-private',status)
        self.assertTrue(self.action('state')['subscribed'])
        for i in range(1,8): self.action(sub=subscription(str(i)))
        with self.assertRaises(ApiError): self.action(sub=subscription('ninth'))
        self.action('unsubscribe'); self.assertFalse(self.action('state')['subscribed'])

    def test_no_history_replay_confirmed_events_only_locale_grouping_and_revocation(self):
        self.event('Historical'); self.action(locale='fr'); self.flush(); self.assertEqual(self.sent, [])
        self.event('First',kind='disconnected'); self.event('Second',ident='scope2'); self.flush()
        self.assertEqual(len(self.sent),1); body=self.sent[0]['payload']['body']
        self.assertIn('First déconnecté.',body); self.assertIn('Second connecté.',body)
        self.assertNotIn('profile_id',json.dumps(self.sent[0]['payload']))
        self.service.revoke_all(); self.event(); self.flush(); self.assertEqual(len(self.sent),1)

    def test_restart_skips_persisted_history_and_expired_device_is_pruned(self):
        self.action(); self.event()
        other=PushService(self.controller,lambda:self.tls,invoke=self.service.invoke,start=False)
        self.addCleanup(other.close); other.tick(); self.assertEqual(self.sent,[])
        self.response=410; self.event(ident='new'); other.tick()
        with patch('wifi_bridge.push.time.monotonic',return_value=time.monotonic()+4): other.tick()
        self.assertEqual(other.status()['device_count'],0); self.assertEqual(other.status()['last_delivery'],'expired')

    def test_offline_retries_keep_journal_but_send_only_one_warning_until_recovery(self):
        self.action()
        for _ in range(6):
            self.event(kind='connecting',reason='CONNECTING')
            self.event(kind='failed',reason='ASSOCIATION_FAILED')
            self.event(kind='waiting',reason='AWAITING_ASSOCIATION')
            self.service.last_sent.clear();self.flush()
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0]['payload']['category'],'failures')
        self.assertEqual(len([e for e in self.controller.event_store.events if e['type']=='failed']),6)
        self.event(kind='restored');self.service.last_sent.clear();self.flush()
        self.assertEqual(len(self.sent),2);self.assertIn('connected',self.sent[-1]['payload']['body'])
        self.event(kind='failed',reason='ASSOCIATION_FAILED');self.service.last_sent.clear();self.flush()
        self.assertEqual(len(self.sent),3)

    def test_restart_seeds_warning_deduplication_and_distinct_problem_still_notifies(self):
        self.action();self.event(kind='failed',reason='ASSOCIATION_FAILED');self.flush()
        other=PushService(self.controller,lambda:self.tls,invoke=self.service.invoke,start=False);self.addCleanup(other.close)
        self.event(kind='failed',reason='ASSOCIATION_FAILED');other.tick();self.assertEqual(len(self.sent),1)
        self.event(kind='failed',reason='RADIO_UNAVAILABLE');other.last_sent.clear();other.tick()
        self.assertEqual(len(self.sent),2)
        self.event(kind='failed',reason='RADIO_UNAVAILABLE');other.last_sent.clear();other.tick();self.assertEqual(len(self.sent),2)
        self.event(kind='failed',reason='ASSOCIATION_FAILED',operation='new-explicit-request');other.last_sent.clear();other.tick()
        self.assertEqual(len(self.sent),3)

    def test_muted_category_and_other_subscribers_do_not_change_warning_deduplication(self):
        self.action(categories={'failures':False});self.action(sub=subscription('second'))
        for _ in range(3):
            self.event(kind='failed',reason='ASSOCIATION_FAILED');self.service.last_sent.clear();self.flush()
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0]['subscription'],subscription('second'))

    def test_gap_replay_and_expired_failures_seed_filter_without_replaying_alerts(self):
        self.action();self.event(kind='failed',reason='ASSOCIATION_FAILED')
        self.controller.event_store.capacity=1
        self.event(kind='failed',reason='ASSOCIATION_FAILED')
        self.service.cursor=0
        self.service.tick();self.assertFalse(self.sent)
        self.event(kind='failed',reason='ASSOCIATION_FAILED');self.service.tick();self.assertFalse(self.sent)
        self.event(kind='failed',reason='INVALID_TARGET')
        with patch.object(self.controller.event_store,'age',return_value=31):self.service.tick()
        self.event(kind='failed',reason='INVALID_TARGET');self.service.tick();self.assertFalse(self.sent)

    def test_reset_identity_stops_old_subscriptions_and_unready_https_drops_pending(self):
        self.action();self.service.last_sent[self.service.ident(subscription())]=time.monotonic()
        self.event(); self.service.tick(); self.tls['listener']['ready']=False; self.service.tick()
        self.assertFalse(self.service.pending); self.assertEqual(self.sent,[])
        self.tls['identity']['fingerprint_sha256']='b'*64;self.service.tick()
        self.assertEqual(self.service.status()['device_count'],0)

    def test_test_is_explicit_rate_limited_and_provider_failure_has_no_retry(self):
        self.action(); self.action('test')
        with self.assertRaises(ApiError): self.action('test')
        self.response=503;self.service.tick();self.service.tick()
        self.assertEqual(len(self.sent),1);self.assertEqual(self.service.status()['last_delivery'],'failed')

    def test_unsafe_runtime_storage_fails_closed(self):
        self.service.folder.chmod(0o755)
        other=PushService(self.controller,lambda:self.tls,invoke=self.service.invoke,start=False)
        self.addCleanup(other.close);self.assertFalse(other.status()['available'])
        with self.assertRaises(ApiError): other.action({'action':'prepare'},'https://astro-bridge.local')

    def test_format1_migration_preserves_consent_key_and_only_connections(self):
        self.action(locale='fr');before=json.loads(self.service.path.read_text());before['format']=1
        for device in before['devices'].values():
            device.pop('categories');device.pop('system_after')
        from wifi_bridge.config import atomic_json
        atomic_json(self.service.path,before)
        other=PushService(self.controller,lambda:self.tls,invoke=self.service.invoke,start=False);self.addCleanup(other.close)
        after=json.loads(other.path.read_text());self.assertEqual(after['format'],2);self.assertEqual(after['key'],before['key'])
        device=next(iter(after['devices'].values()));self.assertEqual(device['subscription'],subscription())
        self.assertEqual([key for key,value in device['categories'].items() if value],['connections'])
        state=other.action({'action':'state','subscription':subscription()},'https://astro-bridge.local')
        self.assertTrue(state['subscribed']);self.assertEqual(state['categories'],device['categories'])

    def test_new_subscription_defaults_to_every_category_and_resubscribe_keeps_opt_outs(self):
        self.action()
        state = self.action('state')
        self.assertTrue(all(state['categories'].values()))
        self.action('preferences', categories={'switching': False, 'boot': False})
        self.action()
        state = self.action('state')
        self.assertFalse(state['categories']['switching'])
        self.assertFalse(state['categories']['boot'])
        self.assertTrue(state['categories']['connections'])
        self.action(sub=subscription('second'), categories={'hotspot': False})
        second = self.action('state', sub=subscription('second'))
        self.assertFalse(second['categories']['hotspot'])
        self.assertTrue(all(value for key, value in second['categories'].items() if key != 'hotspot'))

    def test_preferences_strict_partial_preservation_and_independent_tags(self):
        self.action(categories={'connections':True,'mode':True,'hotspot':True})
        result=self.action('preferences',categories={'mode':False})
        self.assertFalse(result['categories']['mode']);self.assertTrue(result['categories']['connections'])
        self.assertTrue(result['categories']['hotspot'])
        for value in ({'unknown':True},{'boot':1},[],None):
            with self.assertRaises(ApiError):self.action('preferences',categories=value)
        ident=self.service.ident(subscription());now=time.monotonic()
        self.service.queue(ident,'hotspot','hotspot:1','/#networks?section=hotspot',now,
                           system={'code':'hotspot','values':{'name':'Synthetic phone'}})
        self.service.queue(ident,'connections','connection:test','/#telescopes?profile=test',now,
                           event={'profile_id':'test','profile_name':'Synthetic scope','type':'connected'})
        with patch('wifi_bridge.push.time.monotonic',return_value=now+4):self.service.tick()
        with patch('wifi_bridge.push.time.monotonic',return_value=now+15):self.service.tick()
        self.assertEqual(len(self.sent),2)
        self.assertNotEqual(self.sent[0]['payload']['tag'],self.sent[1]['payload']['tag'])
        self.assertEqual(self.sent[0]['payload']['path'],'/#telescopes?profile=test')
        self.assertEqual(self.sent[1]['payload']['path'],'/#networks?section=hotspot')

    def test_connections_send_on_first_tick_and_report_bounded_delivery_timings(self):
        self.action();self.event();self.service.tick()
        self.assertEqual(len(self.sent),1)
        details=self.service.status()['last_delivery_details']
        self.assertEqual(details['provider_status'],201)
        self.assertEqual(details['category'],'connections')
        self.assertLess(details['queue_seconds'],1)
        self.assertNotIn('endpoint',json.dumps(details));self.assertNotIn('synthetic-private',json.dumps(details))

    def test_operation_notices_replace_related_connection_noise_and_expire(self):
        from wifi_bridge.notifications import DEFAULT_CATEGORIES
        self.action(categories=DEFAULT_CATEGORIES)
        ident=self.service.ident(subscription());now=time.monotonic()
        system={'sequence':1,'category':'switching','group':'operation:synthetic-operation',
                'code':'switch_finished','values':{'name':'Synthetic scope','seconds':12},'path':'/#telescopes?profile=scope'}
        self.service.source.snapshot=lambda _:True
        self.service.source.collect=lambda *_:[system]
        self.service.source.op_seen['synthetic-operation']='finished'
        self.event('Victim',ident='victim',kind='disconnected');self.event('Synthetic scope')
        self.service.tick();self.service.source.collect=lambda *_:[]
        with patch('wifi_bridge.push.time.monotonic',return_value=now+4):self.service.tick()
        self.assertEqual(len(self.sent),1);body=self.sent[0]['payload']['body']
        self.assertEqual(body,'Switched to Synthetic scope in 12 s.');self.assertNotIn('Victim',body)
        self.service.queue(ident,'hotspot','stale','/#networks?section=hotspot',now,system={'code':'hotspot','values':{'name':'STALE'}})
        with patch('wifi_bridge.push.time.monotonic',return_value=now+31):self.service.tick()
        self.assertEqual(len(self.sent),1);self.assertFalse(self.service.pending)

    def test_slow_collection_does_not_replay_aged_or_previous_process_events(self):
        self.action();self.event('STALE')
        with patch('wifi_bridge.events.time.monotonic',return_value=time.monotonic()+31):self.service.tick()
        self.assertFalse(self.service.pending);self.assertEqual(self.sent,[])



class ConnectionNoticeTests(unittest.TestCase):
    def test_profile_operation_reason_are_independent_and_memory_is_bounded(self):
        gate=ConnectionNotices()
        event={'profile_id':'scope','operation_id':'incident','reason':'ASSOCIATION_FAILED','type':'failed'}
        self.assertTrue(gate.accept(event));self.assertFalse(gate.accept(event))
        self.assertTrue(gate.accept(dict(event,profile_id='other')))
        self.assertTrue(gate.accept(dict(event,operation_id='manual-retry')))
        self.assertTrue(gate.accept(dict(event,reason='INVALID_TARGET')))
        for n in range(200):
            gate.accept(dict(event,operation_id=str(n)));gate.accept(event)
        self.assertEqual(len(gate.failures),128);self.assertFalse(gate.accept(event))

try:
    import http_ece
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from py_vapid import Vapid02
    CRYPTO=True
except ImportError: CRYPTO=False


@unittest.skipUnless(CRYPTO, 'Install the optional push dependencies to exercise real cryptography')
class EncryptionTests(unittest.TestCase):
    def test_real_crypto_worker_stays_warm_and_rejects_invalid_delivery_without_egress(self):
        import os,sys
        from wifi_bridge.push_worker import CryptoWorker
        worker=CryptoWorker([sys.executable,'-m','wifi_bridge.push_crypto','--serve'],
            {'PATH':os.defpath,'PYTHONPATH':str(Path(__file__).resolve().parents[1])})
        self.addCleanup(worker.close)
        self.assertTrue(worker.request({'action':'probe'})['available']);pid=worker.process.pid
        key=worker.request({'action':'keygen'});self.assertIn('private_key',key)
        failed=worker.request({'action':'send','subscription':{'endpoint':'http://127.0.0.1','keys':{}},'private_key':key['private_key'],'payload':{}})
        self.assertEqual(failed['code'],'PUSH_DELIVERY_FAILED')
        self.assertTrue(worker.request({'action':'probe'})['available']);self.assertEqual(worker.process.pid,pid)

    def test_aes128gcm_roundtrip_vapid_signature_and_public_dns_guard(self):
        from wifi_bridge.push_crypto import encrypt, send, unb64
        vapid=Vapid02();vapid.generate_keys();peer=ec.generate_private_key(ec.SECP256R1())
        private=vapid.private_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
        sub=subscription();sub['keys']['p256dh']=encoded(peer.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint))
        payload={'title':'Astro Bridge','body':'Synthetic connected.','expires':int(time.time())+30}
        encrypted,headers=encrypt(private,sub,payload)
        self.assertNotIn(b'Synthetic',encrypted)
        plain=http_ece.decrypt(encrypted,private_key=peer,auth_secret=unb64(sub['keys']['auth']),version='aes128gcm')
        self.assertEqual(json.loads(plain),payload);self.assertEqual(headers['TTL'],'30')
        self.assertEqual(headers['Urgency'],'normal');self.assertLessEqual(len(headers['Topic']),32)
        _,urgent=encrypt(private,sub,dict(payload,category='switching',tag='different-operation'))
        self.assertEqual(urgent['Urgency'],'high');self.assertNotEqual(headers['Topic'],urgent['Topic'])
        jwt=headers['Authorization'].split('t=',1)[1].split(',')[0];a,b,c=jwt.split('.')
        claims=json.loads(unb64(b));self.assertEqual(claims['aud'],'https://web.push.apple.com')
        signature=unb64(c);r,s=int.from_bytes(signature[:32]),int.from_bytes(signature[32:])
        vapid.public_key.verify(encode_dss_signature(r,s),(a+'.'+b).encode(),ec.ECDSA(hashes.SHA256()))
        for address in ('127.0.0.1','169.254.169.254','192.168.1.1','::1','::ffff:192.168.1.1'):
            with patch('wifi_bridge.push_crypto.socket.getaddrinfo',return_value=[(2,1,6,'',(address,443))]),self.assertRaises(ValueError):
                send({'subscription':sub,'private_key':private,'payload':payload})
