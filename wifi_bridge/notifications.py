"""Bounded notification observations; never scans, probes or changes network intent."""
import copy
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import statistics
import time

from . import __version__
from .build import BUILD_ID
from .config import atomic_json

CATEGORIES=('detected','connections','switching','hotspot','mode','updates','boot','failures')
LEGACY_CATEGORIES={key:key=='connections' for key in CATEGORIES}
DEFAULT_CATEGORIES=dict.fromkeys(CATEGORIES,True)
CODES={'new_detected':'detected','detected':'detected','switch_started':'switching','switch_finished':'switching',
       'switch_failed':'failures','hotspot':'hotspot','mode':'mode',
       'update_available':'updates','update_installed':'updates','boot':'boot'}
PROFILE=re.compile(r'[a-z0-9][a-z0-9-]{0,31}')


def clean(value,limit=64):
    return ''.join(c for c in value if c.isprintable())[:limit] if isinstance(value,str) else ''


def route(page='telescopes',section=None,profile=None,observation=None):
    if page=='configuration' and section=='telescopes' and profile is None and isinstance(observation,str) and re.fullmatch('[a-f0-9]{32}',observation):return '/#configuration/telescopes?add='+observation
    if observation is not None:raise ValueError('Invalid discovery destination.')
    if page=='telescopes' and section is None and (profile is None or PROFILE.fullmatch(profile)):
        return '/#telescopes'+('?profile='+profile if profile else '')
    if page=='networks' and section in ('hotspot','connection-mode') and profile is None:
        return '/#networks?section='+section
    if page=='configuration' and section=='updates' and profile is None:return '/#configuration/general?section=updates'
    raise ValueError('Invalid notification destination.')


def private_json(path,limit=65536):
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(descriptor,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077 or info.st_size>limit:raise ValueError('Private bounded state required.')
        raw=stream.read(limit+1)
        if len(raw)>limit:raise ValueError('State too large.')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ValueError('Object required.')
    return value


class NotificationSource:
    """Private system journal separate from the strict connection event schema.

    Only changed kernel boot IDs are boots. A first installation establishes a
    baseline rather than misreporting an app installation as a Pi restart.
    """
    def __init__(self,folder,clock=time.monotonic,kernel_boot=None):
        self.folder=Path(folder);self.path=self.folder/'notifications.json';self.clock=clock
        self.started=clock();self.available=True;self.op_seen={};self.client_pending={};self.scan_after=-1;self.detection={}
        try:
            self.boot=kernel_boot if kernel_boot is not None else Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',self.boot):raise ValueError()
        except (OSError,ValueError):self.boot=None
        self.data={'format':1,'kernel_boot':self.boot,'sequence':0,'events':[], 'history':{},
                   'mode':None,'clients':[],'detected':[],'update_available':None,'installed':None}
        self.new_boot=False
        self.fresh=True
        try:
            value=private_json(self.path)
            if (set(value)!=set(self.data) or type(value['format']) is not int or value['format']!=1
                    or type(value['sequence']) is not int or not 0<=value['sequence']<2**63
                    or not isinstance(value['events'],list) or len(value['events'])>128
                    or not isinstance(value['history'],dict) or len(value['history'])>64
                    or not isinstance(value['clients'],list) or len(value['clients'])>64
                    or not isinstance(value['detected'],list) or len(value['detected'])>24):raise ValueError()
            if value['kernel_boot'] is not None and not re.fullmatch(r'[0-9a-f-]{36}',value['kernel_boot']):raise ValueError()
            for entries in (value['clients'],value['detected']):
                if any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in entries):raise ValueError()
            for key,values in value['history'].items():
                if (not re.fullmatch('[0-9a-f]{64}',key) or not isinstance(values,list) or len(values)>8
                        or any(type(v) is not int or not 0<=v<=300 for v in values)):raise ValueError()
            for event in value['events']:
                if (not isinstance(event,dict) or set(event)!={'sequence','category','code','values','path','group','created'}
                        or type(event['sequence']) is not int or event['category'] not in CATEGORIES
                        or not isinstance(event['code'],str) or CODES.get(event['code'])!=event['category']
                        or not isinstance(event['values'],dict) or len(event['values'])>5
                        or any(k not in ('name','seconds','usual','mode','hotspot','version') or not isinstance(v,(str,int)) or isinstance(v,bool)
                               or isinstance(v,str) and len(v)>128 for k,v in event['values'].items())
                        or not isinstance(event['path'],str) or not re.fullmatch(r'/#(?:telescopes(?:\?profile=[a-z0-9][a-z0-9-]{0,31})?|networks\?section=(?:hotspot|connection-mode)|configuration/general\?section=updates|configuration/telescopes\?add=[a-f0-9]{32})',event['path'])
                        or not isinstance(event['group'],str) or len(event['group'])>128
                        or type(event['created']) not in (int,float) or not math.isfinite(event['created'])):raise ValueError()
            if value['mode'] not in (None,'private','hotspot_isolated','hotspot_shared'):raise ValueError()
            for key in ('update_available','installed'):
                if value[key] is not None and (not isinstance(value[key],str) or len(value[key])>160):raise ValueError()
            self.new_boot=bool(self.boot and value['kernel_boot'] and self.boot!=value['kernel_boot'])
            self.data=value;self.fresh=False
            if self.new_boot:self.data.update(clients=[],detected=[])
        except FileNotFoundError:pass
        except (OSError,ValueError,KeyError,TypeError):self.available=False
        self.baseline=True

    def persist(self):
        if not self.available:return False
        try:
            while len(json.dumps(self.data,ensure_ascii=True).encode())>60000 and self.data['events']:
                self.data['events'].pop(0)
            atomic_json(self.path,self.data);return True
        except OSError:self.available=False;return False

    @staticmethod
    def profile_key(profile):
        identity=[profile.get(k) for k in ('id','kind','ssid','bssid','target_ip')]
        return hashlib.sha256(json.dumps(identity,ensure_ascii=True).encode()).hexdigest()

    def emit(self,category,code,values,path,group=None):
        self.data['sequence']+=1
        event={'sequence':self.data['sequence'],'category':category,'code':code,'values':values,
               'path':path,'group':group or category+':'+str(self.data['sequence']),'created':self.clock()}
        self.data['events']=(self.data['events']+[event])[-128:]
        return event

    def collect(self,snapshot,updates=None):
        if not self.available or not snapshot:return []
        now=self.clock();out=[];before=json.dumps(self.data,sort_keys=True)
        config,state,operations,scans=snapshot[:4]
        observations=snapshot[4] if len(snapshot)>4 else {}
        profiles={p['id']:p for p in config.get('telescopes',[])[:24]}
        live=bool(state.get('notification_cycle_ok'))
        if live:
            mode=config.get('connection_mode','private')
            if self.data['mode'] is not None and self.data['mode']!=mode:
                out.append(self.emit('mode','mode',{'mode':mode},route('networks','connection-mode')))
            self.data['mode']=mode
            if self.new_boot:
                out.append(self.emit('boot','boot',{'mode':mode,'hotspot':clean(state.get('setup_ssid'))},route()))
                self.new_boot=False
            self.data['kernel_boot']=self.boot or self.data['kernel_boot']
            ap=state.get('hotspot',{})
            if ap.get('active') and ap.get('clients_available'):
                clients={}
                for client in ap.get('clients',[])[:64]:
                    try:
                        ip=ipaddress.IPv4Address(client.get('ip',''))
                        if ip not in ipaddress.ip_network('192.168.77.0/24') or str(ip) in ('192.168.77.0','192.168.77.1','192.168.77.255'):continue
                    except (ValueError,TypeError):continue
                    key=hashlib.sha256(str(client.get('mac',str(ip))).encode()).hexdigest()
                    clients[key]=clean(client.get('hostname')) or str(ip)
                previous=set(self.data['clients'])
                for key,name in clients.items():
                    if key not in previous:
                        count=self.client_pending.get(key,0)+1;self.client_pending[key]=count
                        if count>=2:
                            out.append(self.emit('hotspot','hotspot',{'name':name},route('networks','hotspot')));previous.add(key)
                self.client_pending={k:v for k,v in self.client_pending.items() if k in clients}
                self.data['clients']=sorted(previous&clients.keys())
        # Two independent successful Wi-Fi scans, never two polls of one scan.
        for stamp,points in scans[-4:]:
            if stamp<=self.scan_after or now-stamp>120:continue
            self.scan_after=stamp
            for ident,profile in profiles.items():
                key=self.profile_key(profile);selector=profile.get('bssid')
                found=any(ssid==profile.get('ssid') and (not selector or bssid==selector) for ssid,bssid in points)
                prior=self.detection.get(key,(None,0,0));first,hits,misses=prior
                if found:
                    first=stamp if first is None or stamp-first>120 else first;hits+=1;misses=0
                    if hits>=2 and stamp-first>=5 and key not in self.data['detected']:
                        out.append(self.emit('detected','detected',{'name':clean(profile['name'])},route(profile=ident)))
                        self.data['detected']=(self.data['detected']+[key])[-24:]
                else:
                    misses+=1;hits=0
                    if misses>=2 and key in self.data['detected']:self.data['detected'].remove(key)
                self.detection[key]=(first,hits,misses)
        # New unsaved Wi-Fi observations need no profile. The stable opaque route
        # resolves authenticated defaults; no credentials or SSIDs travel in URLs.
        from .plugins import ssid_plugin
        unknown_keys=set()
        for ident,point in list(observations.items())[-24:]:
            spec=ssid_plugin(point.get('ssid'))
            if spec['id']=='generic':continue
            if any(p.get('ssid')==point['ssid'] and (not p.get('bssid') or p['bssid']==point['bssid']) for p in profiles.values()):continue
            key=hashlib.sha256(('new:'+ident).encode()).hexdigest();unknown_keys.add(key)
            if key not in self.data['detected']:
                out.append(self.emit('detected','new_detected',{'name':clean(point['ssid'])},route('configuration','telescopes',observation=ident),'detected:'+ident))
                self.data['detected']=(self.data['detected']+[key])[-24:]
        keys={self.profile_key(p) for p in profiles.values()}|unknown_keys
        self.detection={k:v for k,v in self.detection.items() if k in keys}
        self.data['detected']=[k for k in self.data['detected'] if k in keys]
        # Operation timings are process-monotonic, additive fields supplied by the engine.
        for ident,record in list(operations.items())[-64:]:
            value=record.get('public',{});profile=profiles.get(value.get('profile_id'))
            started=record.get('started');finished=record.get('finished')
            if not profile or type(started) not in (int,float) or not math.isfinite(started) or started<self.started:continue
            if value.get('state') in ('succeeded','failed') and type(finished) not in (int,float):continue
            stage='finished' if value.get('state') in ('succeeded','failed') else 'running'
            if self.op_seen.get(ident)==stage or self.op_seen.get(ident)=='finished':continue
            self.op_seen[ident]=stage;name=clean(profile['name']);values={'name':name};key=self.profile_key(profile)
            if stage=='running':
                history=self.data['history'].get(key,[])
                if len(history)>=3:values['usual']=int(statistics.median(history))
                out.append(self.emit('switching','switch_started',values,route(profile=profile['id']),'operation:'+ident))
            else:
                elapsed=finished-started
                if not math.isfinite(elapsed) or not 0<=elapsed<=300 or not 0<=now-finished<=30:continue
                values['seconds']=int(round(elapsed))
                if value['state']=='succeeded':
                    self.data['history'][key]=(self.data['history'].get(key,[])+[values['seconds']])[-8:]
                    out.append(self.emit('switching','switch_finished',values,route(profile=profile['id']),'operation:'+ident))
                else:out.append(self.emit('failures','switch_failed',values,route(profile=profile['id']),'operation:'+ident))
        self.op_seen=dict(list(self.op_seen.items())[-64:]);self.data['history']=dict(list(self.data['history'].items())[-64:])
        if updates:
            status,transaction=updates
            available=status.get('version') if status.get('state')=='available' else None
            if isinstance(available,str) and re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)',available):
                if not self.baseline and available!=self.data['update_available']:
                    out.append(self.emit('updates','update_available',{'version':available},route('configuration','updates')))
                self.data['update_available']=available
            installed=transaction.get('version')
            if (transaction.get('phase')=='complete' and installed==__version__ and status.get('state')=='installed'
                    and status.get('version')==__version__ and BUILD_ID and transaction.get('build_id')==BUILD_ID):
                identity=installed+':'+BUILD_ID
                if (not self.fresh or not self.baseline) and identity!=self.data['installed']:
                    out.append(self.emit('updates','update_installed',{'version':installed},route('configuration','updates')))
                self.data['installed']=identity
        self.baseline=False
        if json.dumps(self.data,sort_keys=True)!=before and not self.persist():return []
        return out

    def snapshot(self,controller):
        lock=getattr(controller,'lock',None)
        if lock is None:return None
        with lock:
            state=copy.deepcopy(controller.state);state['notification_cycle_ok']=controller.last_cycle_ok
            return (copy.deepcopy(controller.store.config),state,copy.deepcopy(controller.operations.records),
                    tuple(getattr(controller.candidates,'notification_scans',())),copy.deepcopy(getattr(controller.candidates,'observations',{})))

    def update_state(self):
        try:return (private_json(self.folder.parent/'updates/status.json',32768),private_json(self.folder.parent/'updates/transaction.json',32768))
        except (OSError,ValueError,TypeError):return None
