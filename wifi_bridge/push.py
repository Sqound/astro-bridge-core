"""Optional bounded Web Push, separate from network/controller locks and journals."""
import base64
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from .api import ApiError
from .config import atomic_json

LOCALES = ('en', 'fr', 'ja', 'de', 'es', 'it', 'pt-BR', 'zh-Hans', 'zh-Hant', 'ru')
from .notifications import (CATEGORIES, DEFAULT_CATEGORIES, LEGACY_CATEGORIES,
                            NotificationSource, clean, private_json, route)
from .notification_text import message
from .push_worker import CryptoWorker

PRIORITY_CATEGORIES=('connections','switching','failures')

ERRORS = {
    'PUSH_UNAVAILABLE': 'Notification support is not installed on this bridge.',
    'PUSH_HTTPS_REQUIRED': 'Open trusted HTTPS before enabling notifications.',
    'PUSH_INVALID_SUBSCRIPTION': 'The notification subscription is invalid or unsupported.',
    'PUSH_LIMIT': 'The bridge already has eight notification devices. Remove an unused device first.',
    'PUSH_RATE_LIMITED': 'Wait before sending another test notification.',
    'PUSH_STORAGE_UNAVAILABLE': 'Notification settings could not be saved.',
}


def reject(code, status=409):
    raise ApiError(status, code, ERRORS[code])


def validate_subscription(value):
    if not isinstance(value, dict) or set(value) - {'endpoint', 'keys', 'expirationTime'}:
        reject('PUSH_INVALID_SUBSCRIPTION', 400)
    endpoint = value.get('endpoint')
    if not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 2048 or any(ord(c) <= 32 or ord(c) > 126 for c in endpoint):
        reject('PUSH_INVALID_SUBSCRIPTION', 400)
    try:
        url = urlsplit(endpoint)
        host = url.hostname or ''
        allowed = (host.endswith('.push.apple.com') and host != 'push.apple.com' or
                   host == 'fcm.googleapis.com' or host == 'updates.push.services.mozilla.com')
        if (url.scheme != 'https' or not allowed or url.port not in (None, 443) or url.username is not None
                or url.password is not None or url.fragment or not url.path.startswith('/') or '\\' in endpoint):
            raise ValueError
    except ValueError:
        reject('PUSH_INVALID_SUBSCRIPTION', 400)
    keys = value.get('keys')
    if not isinstance(keys, dict) or set(keys) != {'p256dh', 'auth'}:
        reject('PUSH_INVALID_SUBSCRIPTION', 400)
    for key, size in (('p256dh', 65), ('auth', 16)):
        text = keys[key]
        if not isinstance(text, str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}', text):
            reject('PUSH_INVALID_SUBSCRIPTION', 400)
        try:
            raw = base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
            if len(raw) != size or key == 'p256dh' and raw[0] != 4:
                raise ValueError
        except ValueError:
            reject('PUSH_INVALID_SUBSCRIPTION', 400)
    return {'endpoint': endpoint, 'keys': dict(keys)}


class ConnectionNotices:
    """One warning per profile/operation/reason; retain retries in the journal."""
    def __init__(self):
        self.failures = OrderedDict()

    def accept(self, event):
        profile = event['profile_id']
        if event['type'] in ('connected', 'restored'):
            self.failures = OrderedDict((key, value) for key, value in self.failures.items() if key[0] != profile)
        if event['type'] != 'failed': return True
        key = (profile, event['operation_id'], event['reason'])
        repeated = key in self.failures
        self.failures[key] = None
        self.failures.move_to_end(key)
        while len(self.failures) > 128: self.failures.popitem(last=False)
        return not repeated


class PushService:
    def __init__(self, controller, tls, invoke=None, start=True):
        self.controller, self.tls = controller, tls
        self.folder = controller.store.path.parent / 'push'
        self.path = self.folder / 'settings.json'
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.invoke = invoke or self.worker
        self.available = False
        self.data = {'format': 2, 'key': None, 'devices': {}}
        history = controller.event_store.read(limit=100)
        self.cursor = history['latest_sequence']
        self.connection_notices = ConnectionNotices()
        for event in history['events']: self.connection_notices.accept(event)
        self.pending = {}; self.test_times = {}; self.last_delivery = None; self.last_sent = {}
        self.last_delivery_details = None
        self.storage_ok = True
        self.crypto = None
        try:
            self.folder.mkdir(mode=0o700, exist_ok=True)
            self.private(self.folder, directory=True)
            self.source = NotificationSource(self.folder)
            if self.path.exists():
                value = private_json(self.path)
                if (set(value) != {'format', 'key', 'devices'} or type(value['format']) is not int or value['format'] not in (1,2)
                        or not isinstance(value['devices'], dict) or len(value['devices']) > 8): raise ValueError
                key=value['key']
                if key is not None and (not isinstance(key,dict) or set(key)!={'private_key','public_key'}
                    or any(not isinstance(key[k],str) or not 1<=len(key[k])<=8192 for k in key)):raise ValueError
                for ident, device in value['devices'].items():
                    required={'subscription','locale','origin','fingerprint','after'}
                    if (not isinstance(device,dict) or not required<=set(device) or set(device)-required-{'categories','system_after'}
                            or ident != self.ident(validate_subscription(device['subscription'])) or device['locale'] not in LOCALES
                            or not isinstance(device['origin'],str) or len(device['origin'])>255
                            or not device['origin'].startswith('https://')
                            or not isinstance(device['fingerprint'],str) or not re.fullmatch('[0-9a-f]{64}',device['fingerprint'])
                            or type(device['after']) is not int or device['after']<0):raise ValueError
                    device['categories']=self.categories(device.get('categories',LEGACY_CATEGORIES),complete=True)
                    device.setdefault('system_after',self.source.data['sequence'])
                    if type(device['system_after']) is not int or device['system_after']<0:raise ValueError
                migrated=value['format']==1;value['format']=2;self.data=value
                if migrated:atomic_json(self.path,self.data)
        except (OSError, ValueError, KeyError, TypeError, ApiError):
            self.storage_ok = False
        try:
            self.available = self.invoke({'action': 'probe'}).get('available') is True
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        self.thread = threading.Thread(target=self.run, daemon=True, name='bridge-push')
        if start: self.thread.start()

    @staticmethod
    def private(path, directory=False):
        info = path.lstat()
        if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                or info.st_uid != os.geteuid() or info.st_mode & 0o077): raise ValueError('Private storage required')

    def worker(self, request):
        root = self.controller.store.path.parent / 'push-runtime'
        env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'PYTHONPATH': str(Path(__file__).resolve().parent.parent)}
        if root.exists():
            self.private(root, directory=True)
            version = f'python{sys.version_info.major}.{sys.version_info.minor}'
            env['PYTHONPATH'] += os.pathsep + str(root / 'usr/lib' / version / 'site-packages') + os.pathsep + str(root / 'vapid')
            env['LD_LIBRARY_PATH'] = str(root / 'usr/lib') + ':' + str(root / 'lib')
        elif Path('/usr/lib/astro-bridge-push/vapid').is_dir():
            env['PYTHONPATH'] += os.pathsep + '/usr/lib/astro-bridge-push/vapid'
        if self.crypto is None:
            self.crypto=CryptoWorker([sys.executable,'-m','wifi_bridge.push_crypto','--serve'],env)
        return self.crypto.request(request)

    @staticmethod
    def ident(subscription):
        return hashlib.sha256(subscription['endpoint'].encode()).hexdigest()

    def save(self):
        try:
            if not self.storage_ok: raise OSError
            atomic_json(self.path, self.data)
        except OSError:
            reject('PUSH_STORAGE_UNAVAILABLE')

    def status(self):
        with self.lock:
            return {'available': self.available and self.storage_ok, 'public_key': (self.data['key'] or {}).get('public_key'),
                    'device_count': len(self.data['devices']), 'last_delivery': self.last_delivery,
                    'last_delivery_details':self.last_delivery_details,
                    'categories':list(CATEGORIES),'default_categories':dict(DEFAULT_CATEGORIES),
                    'system_events_available':bool(getattr(self,'source',None) and self.source.available)}

    def action(self, data, origin):
        if not isinstance(data, dict) or set(data) - {'action', 'subscription', 'locale', 'categories'}:
            reject('PUSH_INVALID_SUBSCRIPTION', 400)
        action = data.get('action')
        if action not in ('prepare', 'subscribe', 'unsubscribe', 'state', 'test', 'preferences'):
            reject('PUSH_INVALID_SUBSCRIPTION', 400)
        if 'categories' in data and (action not in ('subscribe','preferences') or not isinstance(data['categories'],dict)):
            reject('PUSH_INVALID_SUBSCRIPTION',400)
        if 'categories' in data:self.categories(data['categories'])
        if not self.available: reject('PUSH_UNAVAILABLE')
        if not self.storage_ok: reject('PUSH_STORAGE_UNAVAILABLE')
        tls = self.tls()
        fingerprint = (tls.get('identity') or {}).get('fingerprint_sha256')
        if not origin.startswith('https://') or not tls['listener']['ready'] or not fingerprint:
            reject('PUSH_HTTPS_REQUIRED')
        with self.lock:
            if not self.data['key']:
                try: self.data['key'] = self.invoke({'action': 'keygen'})
                except (OSError, ValueError, subprocess.SubprocessError): reject('PUSH_UNAVAILABLE')
                self.save()
            if action == 'prepare': return self.status()
            sub = validate_subscription(data.get('subscription')); ident = self.ident(sub)
            if action == 'unsubscribe':
                self.data['devices'].pop(ident, None); self.pending.pop(ident, None); self.test_times.pop(ident,None);self.last_sent.pop(ident,None); self.save()
                return {'subscribed': False}
            if action == 'state':
                return {'subscribed': ident in self.data['devices'] and self.data['devices'][ident]['fingerprint'] == fingerprint,
                        'categories':dict(self.data['devices'].get(ident,{}).get('categories',DEFAULT_CATEGORIES))}
            if action == 'subscribe':
                locale = data.get('locale', 'en')
                if locale not in LOCALES: reject('PUSH_INVALID_SUBSCRIPTION', 400)
                if ident not in self.data['devices'] and len(self.data['devices']) >= 8: reject('PUSH_LIMIT')
                categories=dict(self.data['devices'].get(ident,{}).get('categories',DEFAULT_CATEGORIES))
                categories.update(data.get('categories',{}))
                self.data['devices'][ident] = {'subscription': sub, 'locale': locale, 'origin': origin,
                                              'fingerprint': fingerprint, 'after': self.controller.event_store.read()['latest_sequence'],
                                              'categories':categories,'system_after':self.source.data['sequence']}
                self.save(); return {'subscribed': True}
            if ident not in self.data['devices']: reject('PUSH_INVALID_SUBSCRIPTION', 400)
            if action=='preferences':
                if 'categories' not in data:reject('PUSH_INVALID_SUBSCRIPTION',400)
                self.data['devices'][ident]['categories'].update(data['categories']);self.save()
                return {'subscribed':True,'categories':dict(self.data['devices'][ident]['categories'])}
            now = time.monotonic()
            if now - self.test_times.get(ident, -60) < 30: reject('PUSH_RATE_LIMITED', 429)
            self.test_times[ident] = now
            self.pending.setdefault(ident,{})['test'] = {'events': [], 'due': now, 'expires': now + 30, 'test': True,
                'category':'test','path':route(),'group':'test'}
            return {'queued': True}  # Provider acceptance and iPhone receipt are separate evidence.

    def revoke_all(self):
        with self.lock:
            self.data['devices'] = {}; self.pending.clear(); self.test_times.clear(); self.last_sent.clear()
            if self.storage_ok: self.save()

    @staticmethod
    def categories(value,complete=False):
        if (not isinstance(value,dict) or set(value)-set(CATEGORIES)
                or complete and set(value)!=set(CATEGORIES) or any(type(v) is not bool for v in value.values())):
            reject('PUSH_INVALID_SUBSCRIPTION',400)
        return dict(value)

    def queue(self,ident,category,group,path,now,event=None,system=None):
        pending=self.pending.setdefault(ident,{})
        entry=pending.setdefault(group,{'events':[],'due':now if category in PRIORITY_CATEGORIES else now+3,'expires':now+30,'test':False,
                                        'category':category,'path':path,'group':group,'queued_at':now,'event_at':now})
        if system:
            entry.update(system=system,events=[],category=category,path=path,expires=now+30,queued_at=now,event_at=system.get('created',now))
        elif not entry.get('system'):
            entry['events']=[e for e in entry['events'] if e['profile_id']!=event['profile_id']][-7:]+[event]
            age=self.controller.event_store.age(event.get('sequence'))
            entry['event_at']=now-age if age is not None else now
        while len(pending)>16:pending.pop(next(iter(pending)))

    def tick(self):
        now=time.monotonic()
        page=self.controller.event_store.read(after=self.cursor,limit=100);self.cursor=page['next_cursor']
        # Feed expired/gap events too: replay stays silent and an ongoing outage
        # must not become a new warning after a restart or temporary TLS outage.
        connection_events=[event for event in page['events'] if self.connection_notices.accept(event)]
        source=getattr(self,'source',None)
        systems=source.collect(source.snapshot(self.controller),source.update_state()) if source else []
        now=time.monotonic()
        tls=self.tls();fingerprint=(tls.get('identity') or {}).get('fingerprint_sha256');ready=tls['listener']['ready']
        with self.lock:
            if not self.available or not self.storage_ok:return
            for ident,device in list(self.data['devices'].items()):
                if fingerprint and device['fingerprint']!=fingerprint:
                    self.data['devices'].pop(ident);self.pending.pop(ident,None);self.test_times.pop(ident,None);self.last_sent.pop(ident,None);self.save()
            if ready:
                for event in systems:
                    for ident,device in self.data['devices'].items():
                        if event['sequence']<=device['system_after'] or not device['categories'][event['category']]:continue
                        self.queue(ident,event['category'],event['group'],event['path'],now,system=event)
                if not page['gap']:
                    for event in connection_events:
                        if event['type'] not in ('connected','restored','disconnected','failed'):continue
                        age=self.controller.event_store.age(event['sequence'])
                        if age is None or age>30:continue
                        category='failures' if event['type']=='failed' else 'connections'
                        group='operation:'+event['operation_id'] if event['operation_id'] else 'connection:'+event['profile_id']
                        for ident,device in self.data['devices'].items():
                            if event['sequence']<=device['after'] or not device['categories'][category]:continue
                            # The single operation notice owns target/victim/restoration changes.
                            if device['categories']['switching'] and source and event['operation_id'] in source.op_seen:continue
                            self.queue(ident,category,group,route(profile=event['profile_id']),now,event=event)
            deliveries=[]
            for ident,groups in list(self.pending.items()):
                device=self.data['devices'].get(ident)
                if not device:self.pending.pop(ident,None);continue
                for group,entry in sorted(groups.items(),key=lambda pair:(pair[1]['category'] not in PRIORITY_CATEGORIES,pair[1]['due'])):
                    if now>entry['expires'] or not ready or not entry['test'] and not device['categories'][entry['category']]:
                        groups.pop(group);continue
                    spacing=1 if entry['category'] in PRIORITY_CATEGORIES else 10
                    if now<entry['due'] or now-self.last_sent.get(ident,-60)<spacing:continue
                    groups.pop(group)
                    if entry['test']:body=message(device['locale'],'test',{})
                    elif entry.get('system'):
                        event=entry['system'];body=message(device['locale'],event['code'],event['values'])
                    else:
                        body='\n'.join(message(device['locale'],'disconnected' if e['type']=='disconnected' else 'failed' if e['type']=='failed' else 'connected',
                                               {'name':clean(e['profile_name'])}) for e in entry['events'])
                    tag_category='switching' if group.startswith('operation:') and entry.get('system') else entry['category']
                    payload={'title':'Astro Bridge','body':body[:800],'expires':int(time.time())+30,
                             'path':entry['path'],'category':entry['category'],
                             'tag':'astro-bridge-'+tag_category+'-'+hashlib.sha256(group.encode()).hexdigest()[:16]}
                    deliveries.append((ident,device['subscription'],self.data['key']['private_key'],payload,entry.get('queued_at',now),entry.get('event_at',now)))
                    self.last_sent[ident]=now  # Only one delivery/device per tick, including slow worker intervals.
                if not groups:self.pending.pop(ident,None)
        # Worker I/O runs outside controller/network/settings locks, with no retry queue.
        for ident,subscription,private,payload,queued_at,event_at in deliveries:
            if self.stop.is_set():break
            with self.lock:
                device=self.data['devices'].get(ident)
                if not device or time.time()>payload['expires'] or payload['category']!='test' and not device['categories'][payload['category']]:continue
            started=time.monotonic()
            try:
                result=self.invoke({'action':'send','subscription':subscription,'private_key':private,'payload':payload});code=result.get('status')
            except (OSError,ValueError,subprocess.SubprocessError):code=None
            if type(code) is not int or not 100<=code<=599:code=None
            with self.lock:
                self.last_delivery='accepted' if code in (200,201,202) else 'expired' if code in (404,410) else 'failed'
                self.last_delivery_details={'category':payload['category'],'queue_seconds':round(max(0,started-queued_at),3),
                                            'event_to_send_seconds':round(max(0,started-event_at),3),
                                            'send_seconds':round(max(0,time.monotonic()-started),3),'provider_status':code}
                if code in (404,410):
                    self.data['devices'].pop(ident,None);self.pending.pop(ident,None);self.test_times.pop(ident,None);self.last_sent.pop(ident,None);self.save()

    def run(self):
        while not self.stop.wait(1):
            try: self.tick()
            except (OSError, ValueError, KeyError, TypeError, ApiError): pass

    def close(self):
        self.stop.set()
        if self.thread.is_alive(): self.thread.join(timeout=13)
        if self.crypto is not None:self.crypto.close()
