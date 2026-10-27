"""Bounded owned nft/conntrack observations. Unknown coverage never means idle."""
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time

from .config import atomic_json
from .mounts import MOUNTS
from .plugins import all_plugins, ACTIVITY_PORTS

PREFIX = 0xA5700000
MASK = 0xFFFF0000

# Native service channels eligible only for bounded background exchanges. This
# bounded metadata heuristic is not firmware-specific calibration or proof of
# human attention. Mixed/unknown channels remain replacement-blocking whenever
# they transfer data; unusually large control bursts are protected too.
# Seestar home keeps 4800/4801/4804 alive too (S50 Pro home/preview/return-home
# measurements). These ports can carry content: only small, fully attributed
# intervals qualify. Large transfers and preview channels 4554/4555 stay busy.
CONTROL_PORTS = {p['id']:ACTIVITY_PORTS[p['activity']] for p in all_plugins() if ACTIVITY_PORTS[p['activity']]}
CONTROL_BYTES_PER_SECOND = 1024
CONTROL_PACKETS_PER_SECOND = 12


def consultation_delta(previous, packets, bytes_, control, elapsed, kind):
    """Classify interval counters only; never read application payloads."""
    if previous is None or not 0 < elapsed <= 15:
        return 'unknown', True
    dp, db = packets-previous['packets'], bytes_-previous['bytes']
    if dp < 0 or db < 0:
        return 'unknown', True
    if not dp and not db:
        return 'none', False
    if kind in MOUNTS:
        return 'mount_control', True
    if kind not in CONTROL_PORTS or control is None or previous.get('control') is None:
        return 'unclassified', True
    cp, cb = control[0]-previous['control'][0], control[1]-previous['control'][1]
    if not 0 <= cp <= dp or not 0 <= cb <= db:
        return 'unknown', True
    if cp == dp and cb == db and cb <= CONTROL_BYTES_PER_SECOND*elapsed and cp <= CONTROL_PACKETS_PER_SECOND*elapsed:
        return 'background_control', False
    # A transfer is probable content, not a decoded image or live-view assertion.
    if db >= 32768 and db/elapsed >= 4096:
        return 'probable_content', True
    return 'unclassified', True



def bounded_read(args, *, limit=131072, timeout=2):
    """Drain at most limit bytes, killing the child before an unbounded capture."""
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C'})
    deadline=time.monotonic()+timeout;result=bytearray()
    try:
        os.set_blocking(process.stdout.fileno(),False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout,selectors.EVENT_READ)
            while selector.get_map():
                remaining=deadline-time.monotonic()
                if remaining<=0:raise ValueError('COLLECTION_TIMEOUT')
                for key,_ in selector.select(remaining):
                    block=os.read(key.fd,min(8192,limit+1-len(result)))
                    if not block:selector.unregister(key.fileobj);continue
                    result.extend(block)
                    if len(result)>limit:raise ValueError('COLLECTION_SATURATED')
        if process.wait(timeout=max(.001,deadline-time.monotonic())):raise ValueError('COLLECTION_FAILED')
        return result.decode('utf-8')
    finally:
        if process.poll() is None:process.kill()
        process.wait();process.stdout.close()


class ForwardActivity:
    def __init__(self, state, *, boot_id=None, clock=time.monotonic, reader=bounded_read):
        self.path=Path(state)/'activity-catalog.json';self.clock,self.reader=clock,reader
        self.conntrack=os.environ.get('ASTRO_BRIDGE_CONNTRACK','/usr/sbin/conntrack')
        if not Path(self.conntrack).is_absolute():raise ValueError('Absolute conntrack executable required')
        self.current={};self.kinds={};self.values={};self.generation=0;self.quiet_seconds=120;self.cooldown_seconds=60;self.next_sample=0;self.published=(self.clock(),{'supported':False,'profiles':{},'idle_selection_implemented':True});self.supported=False
        if Path(self.conntrack).is_file():
            try:
                version=re.search(r'conntrack v([0-9]+)\.([0-9]+)\.([0-9]+)',self.reader([self.conntrack,'--version'],limit=1024))
                self.supported=bool(version and tuple(map(int,version.groups()))>=(1,4,9))
            except (OSError,ValueError,subprocess.SubprocessError):pass
        self.boot_id=boot_id or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.catalog={'version':2,'boot_id':self.boot_id,'next':1,'records':{},'boot_coverage':False,'table_initialized':False,'coverage_lost':False,'active':{}}
        self.bootstrap=False;self.catalog_error=None;self.configured=False;self.rules=None;self.counter_observations={};self.plan_identity=None
        try:
            fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as source:
                info=os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077 or info.st_size>65536:raise ValueError('PRIVATE_BOUNDED_CATALOG_REQUIRED')
                raw=source.read(65537)
            if len(raw)>65536:raise ValueError('CATALOG_TOO_LARGE')
            data=json.loads(raw)
            if (not isinstance(data,dict) or type(data.get('version')) is not int or data.get('version') not in (1,2) or not isinstance(data.get('boot_id'),str)
                    or not re.fullmatch('[0-9a-f-]{36}',data['boot_id']) or type(data.get('next')) is not int
                    or not 1<=data['next']<=65536 or not isinstance(data.get('records'),dict) or len(data['records'])>88):raise ValueError('INVALID_CATALOG')
            for key,record in data['records'].items():
                if (not key.isdecimal() or not PREFIX<int(key)<PREFIX+data['next'] or not isinstance(record,dict)
                        or not isinstance(record.get('profile_id'),str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}',record['profile_id'])
                        or not re.fullmatch('[0-9a-f]{64}',str(record.get('association','')))
                        or type(record.get('coverage')) is not bool):raise ValueError('INVALID_CATALOG')
                for field in ('group','instrument_signature'):
                    if field in record and not re.fullmatch('[0-9a-f]{64}',str(record[field])):raise ValueError('INVALID_ASSOCIATION_GROUP')
                for field in ('target','bridge_ip','home_ip'):ipaddress.IPv4Address(record[field])
                ipaddress.IPv4Network(record['home_network'])
                if (type(record.get('proxy')) is not bool or type(record.get('published')) is not bool or type(record.get('gap_latched')) is not bool
                        or any(not re.fullmatch('[a-zA-Z0-9_.:-]{1,15}',str(record.get(field,''))) for field in ('home_device','device'))):raise ValueError('INVALID_CATALOG')
            if isinstance(data.get('active'),dict):
                data['active']={ident:([marks] if type(marks) is int else marks) for ident,marks in data['active'].items()}
            if (not isinstance(data.get('active'),dict) or len(data['active'])>24
                    or any(not isinstance(marks,list) or not 1<=len(marks)<=88 or any(type(mark) is not int or str(mark) not in data['records'] or data['records'][str(mark)]['profile_id']!=ident for mark in marks) for ident,marks in data['active'].items()) or sum(len(marks) for marks in data['active'].values())>88):raise ValueError('INVALID_ACTIVE_CATALOG')
            changed_boot=data['boot_id']!=self.boot_id
            if any(type(data.get(key)) is not bool for key in ('boot_coverage','table_initialized','coverage_lost')):raise ValueError('INVALID_CATALOG')
            self.bootstrap=changed_boot or data.get('boot_coverage') is True
            if not changed_boot:self.catalog=data
            self.catalog['boot_coverage']=self.bootstrap
            self.catalog['version']=2
        except FileNotFoundError:pass
        except (OSError,ValueError,KeyError,TypeError):
            self.catalog_error='CATALOG_UNCERTAIN';self.catalog.update(next=65536,coverage_lost=True)
        # Persist high-water/coverage before assigning any mark. A real kernel reboot
        # clears old conntrack; an app restart or fresh counter does not establish that.
        try:self.persist()
        except OSError:self.catalog_error='CATALOG_UNAVAILABLE'

    def persist(self):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        atomic_json(self.path,self.catalog)

    @staticmethod
    def association_signature(token, target, bridge_ip, home_ip, home_network, home_device, device):
        return hashlib.sha256(json.dumps([token,target,bridge_ip,home_ip,home_network,home_device,device],sort_keys=True).encode()).hexdigest()

    @staticmethod
    def instrument_signature(token,target,bridge_ip,device):
        return hashlib.sha256(json.dumps([token,target,bridge_ip,device],sort_keys=True).encode()).hexdigest()

    def association_marks(self, ident):
        mark=self.current.get(ident)
        if mark is None:return []
        record=self.catalog['records'][str(mark)];group=record.get('group')
        return [int(key) for key,value in self.catalog['records'].items()
                if value['profile_id']==ident and (value.get('group')==group if group else int(key)==mark)]

    def attach(self, profile_id, token, target, bridge_ip, home_ip, home_network, proxy, home_device, device, *, kind=None):
        if not isinstance(profile_id,str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}',profile_id):return None
        signature=self.association_signature(token,target,bridge_ip,home_ip,home_network,home_device,device)
        instrument=self.instrument_signature(token,target,bridge_ip,device)
        prior=self.catalog['active'].get(profile_id,[])
        if type(prior) is int:prior=[prior]
        matching=None
        for mark in prior:
            record=self.catalog['records'][str(mark)]
            legacy=self.association_signature(token,target,bridge_ip,record['home_ip'],record['home_network'],record['home_device'],device)
            if (record.get('instrument_signature')==instrument or not record.get('group') and record['association']==legacy):
                matching=record;break
        group=matching.get('group') if matching else None
        if not group:
            group=hashlib.sha256((instrument+':'+str(self.catalog['next'])).encode()).hexdigest()
            if matching:matching.update(group=group,instrument_signature=instrument)
        old=next((mark for mark in prior if self.catalog['records'][str(mark)]['association']==signature
                  and self.catalog['records'][str(mark)].get('group')==group),None)
        if not token:
            for record in self.catalog['records'].values():
                if record['profile_id']==profile_id:record['coverage']=False
        if old is None:
            if self.catalog_error or len(self.catalog['records'])>=88 or self.catalog['next']>=65536:
                self.catalog_error=self.catalog_error or 'CATALOG_EXHAUSTED';return None
            old=PREFIX+self.catalog['next'];self.catalog['next']+=1
            self.catalog['records'][str(old)]={'profile_id':profile_id,'association':signature,'group':group,'instrument_signature':instrument,
                'coverage':bool(self.bootstrap and token and not self.catalog.get('coverage_lost') and not any(r['profile_id']==profile_id and not r['coverage'] for r in self.catalog['records'].values())),
                'target':target,'bridge_ip':bridge_ip,'home_ip':home_ip,'home_network':home_network,'proxy':bool(proxy),'published':False,'gap_latched':False,'home_device':home_device,'device':device}
        if proxy:self.catalog['records'][str(old)]['proxy']=True
        existing=self.current.get(profile_id)
        if existing is None or self.catalog['records'][str(existing)].get('group')!=group:self.current[profile_id]=old
        self.catalog['active'][profile_id]=self.association_marks(profile_id)
        if kind in CONTROL_PORTS or kind in MOUNTS:
            for mark in self.association_marks(profile_id):self.kinds[mark]=kind
        try:self.persist()
        except OSError:self.catalog_error='CATALOG_UNAVAILABLE';return None
        return old

    def plan(self):
        self.plan_identity=None
        snapshot=copy.deepcopy(self.published[1])
        for value in snapshot['profiles'].values():value.update(state='unknown',reason='PLAN_RECONCILING',quiet_seconds=None,consultation='unknown')
        self.published=(self.clock(),snapshot)
        self.current={}

    def replaced(self):
        self.generation+=1
        for value in self.values.values():value.update(state='unknown',reason='COUNTER_RESET',quiet_since=None,consultation='unknown')
        self.publish()

    def parse_sessions(self, raw):
        counts={};stale=set();collision=False
        lines=raw.splitlines()
        if len(lines)>256:raise ValueError('COLLECTION_SATURATED')
        for line in lines:
            mark=re.search(r'\bmark=(\d+)\b',line)
            if not mark:raise ValueError('INVALID_SESSION_DATA')
            key=mark[1];record=self.catalog['records'].get(key)
            if not record:collision=True;continue  # Never adopt another application's same-prefix mark.
            src=re.findall(r'\bsrc=([0-9.]+)\b',line);dst=re.findall(r'\bdst=([0-9.]+)\b',line)
            if len(src)!=2 or len(dst)!=2 or 'ESTABLISHED' not in line or not re.search(r'^(?:ipv4\s+2\s+)?tcp\s+6\s',line):raise ValueError('INVALID_SESSION_DATA')
            client=ipaddress.IPv4Address(src[0]);network=ipaddress.IPv4Network(record['home_network'])
            valid=(client in network and str(client) not in (record['home_ip'],str(network.network_address),str(network.broadcast_address))
                   and src[1]==record['target'] and dst[1]==record['bridge_ip']
                   and (dst[0]==record['target'] or record['proxy'] and dst[0]==record['home_ip']))
            if not valid:collision=True;continue
            ident=record['profile_id']
            if int(key) not in self.association_marks(ident):
                stale.add(ident)
                for current,mark in self.current.items():
                    actual=self.catalog['records'][str(mark)]
                    if all(actual[field]==record[field] for field in ('device','target','bridge_ip')):stale.add(current)
            else:counts[ident]=counts.get(ident,0)+1
        return counts,stale,collision

    def control_rule(self, path, mark, direction, record):
        kind=self.kinds.get(mark)
        ports=CONTROL_PORTS.get(kind)
        if not ports:return []
        prefix=path+' ct mark '+str(mark)+' meta l4proto tcp '
        counter=' counter name ab_'+str(mark)+'_control_'+direction
        rules=[prefix+'ct original proto-dst { '+', '.join(map(str,ports))+' }'+counter]
        if kind in ('seestar','dwarf'):
            # Singularity probes the same 10.0.0.1 as a selected Seestar. Its
            # payload-free SYN and the closed-port reset must not continually
            # restart that instrument's quiet window. Count these as bounded
            # background, not as an unmetered exception. Exact IP/TCP header
            # lengths exclude Fast Open/data, fragments and IPv4 options;
            # established traffic on these foreign ports remains unclassified.
            lengths=', '.join(str(20+4*n)+' . '+str(n) for n in range(5,16))
            flags='syn' if direction=='out' else 'rst | ack'
            rules.append(prefix+'ct original proto-dst { 8082, 8083 } '
                         'ip hdrlength 5 ip frag-off & 0x3fff == 0 '
                         'tcp flags & (fin | syn | rst | ack) == ('+flags+') '
                         'ip length . tcp doff { '+lengths+' }'+counter)
        return rules

    def counter_snapshot(self):
        """Read named objects without dumping the retained classifier rules.

        Bracket the bounded counter dump with table identities so replacement
        during collection cannot grant quiet coverage. Keep the command deadline;
        a slow/malformed collection is still unknown, never idle.
        """
        def identity():
            data=json.loads(self.reader(['nft','-a','-j','list','tables','ip'],limit=65536))
            if (not isinstance(data,dict) or not isinstance(data.get('nftables'),list)
                    or any(not isinstance(row,dict) or 'table' in row and not isinstance(row['table'],dict) for row in data['nftables'])):
                raise ValueError('TABLE_IDENTITY_UNKNOWN')
            tables=[row['table'] for row in data['nftables'] if 'table' in row
                    and row['table'].get('family')=='ip' and row['table'].get('name')=='wifi_bridge_activity']
            if len(tables)!=1 or type(tables[0].get('handle')) is not int or tables[0]['handle']<=0:
                raise ValueError('TABLE_IDENTITY_UNKNOWN')
            return tables[0]['handle']
        before=identity()
        if self.catalog.get('table_handle') is not None and self.catalog['table_handle']!=before:
            self.catalog['coverage_lost']=True;self.persist()
        data=json.loads(self.reader(['nft','-a','-j','list','counters','table','ip','wifi_bridge_activity'],limit=262144))
        if not isinstance(data,dict) or not isinstance(data.get('nftables'),list):raise ValueError('INVALID_NAMED_COUNTER')
        counters={}
        for row in data['nftables']:
            if not isinstance(row,dict):raise ValueError('INVALID_NAMED_COUNTER')
            if 'counter' not in row:continue
            counter=row['counter']
            if not isinstance(counter,dict):raise ValueError('INVALID_NAMED_COUNTER')
            name=counter.get('name')
            if (not isinstance(name,str) or not re.fullmatch(r'ab_[0-9]+_(?:out|in|conflict|control_out|control_in)',name)
                    or counter.get('family')!='ip' or counter.get('table')!='wifi_bridge_activity' or name in counters
                    or any(type(counter.get(k)) is not int or counter[k]<0 for k in ('packets','bytes'))):
                raise ValueError('INVALID_NAMED_COUNTER')
            counters[name]=(counter['packets'],counter['bytes'])
            if len(counters)>440:raise ValueError('COLLECTION_SATURATED')
        if identity()!=before:
            self.catalog['coverage_lost']=True;self.persist()
            raise ValueError('TABLE_CHANGED_DURING_COLLECTION')
        return before,counters

    def configure(self, entries, apply):
        """Keep counter objects and retired classifiers; only replace owned rules.

        Old classifiers may conservatively count a now-blocked path during plan
        transitions. They cannot lose a classification gap before the main table
        changes. No statement promises delivery past other tables or a device.
        """
        try:
            existing=set();present=False
            if not self.configured:
                try:
                    _,counters=self.counter_snapshot()
                    existing=set(counters);present=True
                except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):pass
                expected={'ab_'+mark+'_'+direction for mark,r in self.catalog['records'].items() if r['published'] for direction in ('out','in','conflict')}
                if self.catalog.get('table_initialized') and not expected.issubset(existing):
                    self.catalog['coverage_lost']=True;self.persist()
                self.existing=existing;self.present=present
            else:existing=self.existing;present=self.present
            rules=[];objects=[]
            # Current classifiers precede retired association generations.
            live=list(dict.fromkeys(mark for ident in self.current for mark in self.association_marks(ident)))
            marks=list(dict.fromkeys(live+[int(key) for key in self.catalog['records']]))
            # Known marks of this actual association retain their own path counters
            # before any newly added ingress classifier sees them as a conflict.
            for mark in live:
                record=self.catalog['records'][str(mark)]
                forward='iifname "'+record['home_device']+'" oifname "'+record['device']+'" ip saddr '+record['home_network']+' ip daddr '+record['target']
                reply='iifname "'+record['device']+'" oifname "'+record['home_device']+'" ip saddr '+record['target']+' ip daddr '+record['home_network']+' ct state established,related'
                for path,direction in ((forward,'out'),(reply,'in')):
                    rules += self.control_rule(path,mark,direction,record)
                    rules += [path+' ct original protocol udp ct original proto-dst { 1900, 4720, 5353 } return',
                              path+' ct mark '+str(mark)+' counter name ab_'+str(mark)+'_'+direction+' return']
            for mark in marks:
                record=self.catalog['records'][str(mark)]
                for direction in ('out','in','conflict','control_out','control_in'):
                    name='ab_'+str(mark)+'_'+direction
                    if name not in existing:objects.append('add counter ip wifi_bridge_activity '+name)
                forward='iifname "'+record['home_device']+'" oifname "'+record['device']+'" ip saddr '+record['home_network']+' ip daddr '+record['target']
                reply='iifname "'+record['device']+'" oifname "'+record['home_device']+'" ip saddr '+record['target']+' ip daddr '+record['home_network']+' ct state established,related'
                rules += [forward+' ct original protocol udp ct original proto-dst { 1900, 4720, 5353 } return',
                          forward+' ct mark 0 ct mark set '+str(mark)]
                # The first SYN was unmarked in the live-path loop above.
                # Attribute it after ownership is assigned, before returning.
                rules += self.control_rule(forward,mark,'out',record)
                rules += [forward+' ct mark != '+str(mark)+' counter name ab_'+str(mark)+'_conflict',
                          forward+' counter name ab_'+str(mark)+'_out return',
                          reply+' ct original protocol udp ct original proto-dst { 1900, 4720, 5353 } return',
                          reply+' ct mark != '+str(mark)+' counter name ab_'+str(mark)+'_conflict',
                          reply+' counter name ab_'+str(mark)+'_in return']
            script=('add table ip wifi_bridge_activity\nadd chain ip wifi_bridge_activity forward { type filter hook forward priority -20; policy accept; }\n' if not present else '')
            script+='\n'.join(objects)+'\nflush chain ip wifi_bridge_activity forward\n'
            script+='\n'.join('add rule ip wifi_bridge_activity forward '+line for line in rules)+'\n'
            if rules!=self.rules or objects or not present:
                apply(script)
                self.replaced();self.rules=rules
                self.existing=existing|{line.rsplit(' ',1)[-1] for line in objects};self.present=True
                for record in self.catalog['records'].values():record['published']=True
                self.catalog['table_initialized']=True;self.persist()
            self.catalog['active']={ident:self.association_marks(ident) for ident in self.current};self.persist()
            self.configured=True
        except Exception:
            self.catalog['coverage_lost']=True
            try:self.persist()
            except OSError:pass
            for value in self.values.values():value.update(state='unknown',reason='CLASSIFICATION_UNAVAILABLE',quiet_since=None,consultation='unknown')

    def sample(self, *, force=False, quiet_seconds=120, cooldown_seconds=60):
        if any(type(value) is not int or not 30<=value<=3600 for value in (quiet_seconds,cooldown_seconds)):raise ValueError('INVALID_ACTIVITY_POLICY')
        self.quiet_seconds,self.cooldown_seconds=quiet_seconds,cooldown_seconds
        now=self.clock()
        if not force and now<self.next_sample:return
        self.next_sample=now+5
        if not self.current:
            self.values={};self.publish();return
        try:
            if not self.supported:raise ValueError('CONNTRACK_UNAVAILABLE')
            if self.catalog_error:raise ValueError(self.catalog_error)
            table_handle,counters=self.counter_snapshot()
            if self.catalog.get('table_handle') is not None and self.catalog['table_handle']!=table_handle:
                self.catalog['coverage_lost']=True
            if self.catalog.get('table_handle')!=table_handle:
                self.catalog['table_handle']=table_handle;self.persist()
            # Retired classifiers retain unsampled gaps. They can concern a
            # different profile reusing the same interface/target tuple, so any
            # retired uncertainty conservatively poisons coverage for this boot.
            changed_catalog=False
            active_marks={mark for ident in self.current for mark in self.association_marks(ident)}
            for key,record in self.catalog['records'].items():
                if not record['published'] and int(key) not in active_marks:continue
                names=['ab_'+key+'_'+direction for direction in ('out','in','conflict')]
                if not all(name in counters for name in names):
                    self.catalog['coverage_lost']=True;self.persist();raise ValueError('RETAINED_COUNTER_MISSING')
                pairs=tuple(counters[name] for name in names)
                if any(type(value) is not int or value<0 for pair in pairs for value in pair):
                    self.catalog['coverage_lost']=True;self.persist();raise ValueError('INVALID_RETAINED_COUNTER')
                previous=self.counter_observations.get(key,pairs)
                if any(new<old for pair,prior in zip(pairs,previous) for new,old in zip(pair,prior)):
                    self.catalog['coverage_lost']=True;changed_catalog=True
                self.counter_observations[key]=pairs
                if pairs[2][0] and not record['gap_latched']:
                    record['gap_latched']=True;changed_catalog=True
                if record['gap_latched'] and int(key) not in active_marks and not self.catalog['coverage_lost']:
                    self.catalog['coverage_lost']=True;changed_catalog=True
            if changed_catalog:self.persist()
            raw=self.reader([self.conntrack,'-L','-f','ipv4','--mark','0xa5700000/0xffff0000','-p','tcp','--state','ESTABLISHED','-o','extended'])
            sessions,stale,collision=self.parse_sessions(raw)
            if collision and not self.catalog.get('mark_collision'):
                self.catalog['mark_collision']=True;self.persist()
            for ident,mark in self.current.items():
                old=self.values.get(ident,{});record=self.catalog['records'][str(mark)]
                marks=tuple(self.association_marks(ident));records=[self.catalog['records'][str(m)] for m in marks]
                pairs=tuple(tuple(counters['ab_'+str(m)+'_'+direction] for direction in ('out','in','conflict')) for m in marks)
                packets=sum(pair[0][0]+pair[1][0] for pair in pairs);bytes_=sum(pair[0][1]+pair[1][1] for pair in pairs)
                continuous=(old.get('generation')==self.generation and old.get('marks')==marks and now-old.get('observed',-100)>0
                            and now-old.get('observed',-100)<=15 and packets>=old.get('packets',0) and bytes_>=old.get('bytes',0))
                changed=continuous and (packets>old['packets'] or bytes_>old['bytes'])
                control_names=['ab_'+str(m)+'_control_'+d for m in marks for d in ('out','in')]
                control_pairs=tuple(counters[n] for n in control_names) if all(n in counters for n in control_names) else None
                if control_pairs is not None:
                    if any(type(v) is not int or v<0 for pair in control_pairs for v in pair):raise ValueError('INVALID_CONTROL_COUNTER')
                    if continuous and old.get('control_pairs') is not None and any(new<prior for pair,previous in zip(control_pairs,old['control_pairs']) for new,prior in zip(pair,previous)):
                        raise ValueError('CONTROL_COUNTER_RESET')
                control=(sum(pair[0] for pair in control_pairs),sum(pair[1] for pair in control_pairs)) if control_pairs is not None else None
                consultation,busy=consultation_delta(old if continuous else None,packets,bytes_,control,now-old.get('observed',now),self.kinds.get(mark))
                if consultation=='unknown' and continuous:raise ValueError('CONTROL_COUNTER_RESET')
                reason=('MARK_COLLISION' if self.catalog.get('mark_collision') or any(r['gap_latched'] for r in records) else 'STALE_ASSOCIATION_SESSIONS' if ident in stale else
                        'BOOTSTRAP_COVERAGE_UNKNOWN' if any(not r['coverage'] for r in records) or self.catalog.get('coverage_lost') else None)
                quiet_since=old.get('quiet_since') if continuous and not busy and old.get('quiet_since') is not None else now
                if busy:quiet_since=now
                state='unknown' if reason or not continuous else 'active' if busy else 'quiet' if quiet_since is not None and now-quiet_since>=self.quiet_seconds else 'unknown'
                if not reason:reason='CONTENT_TRANSFER_PROBABLE' if state=='active' and consultation=='probable_content' else 'UNCLASSIFIED_TRANSFER' if state=='active' else 'QUIET_WINDOW_OBSERVED' if state=='quiet' else 'WARMING_UP'
                self.values[ident]={'state':state,'reason':reason,'observed':now,'generation':self.generation,'mark':mark,'marks':marks,'group':record.get('group'),
                    'packets':packets,'bytes':bytes_,'tcp_established':sessions.get(ident,0),'quiet_since':quiet_since,
                    'last_active':now if busy else old.get('last_active',now), 'control':control, 'control_pairs':control_pairs, 'consultation':consultation,
                    'raw_active':bool(changed or sessions.get(ident,0)), 'interval_seconds':now-old.get('observed',now) if continuous else None,
                    'interval_packets':packets-old.get('packets',packets) if continuous else None,
                    'interval_bytes':bytes_-old.get('bytes',bytes_) if continuous else None, 'counter_pairs':pairs, 'association_since':old.get('association_since',now) if old.get('group')==record.get('group') else now}
        except (OSError,ValueError,KeyError,TypeError,StopIteration,subprocess.SubprocessError):
            for ident in self.current:
                self.values[ident]=dict(self.values.get(ident,{}),state='unsupported' if not self.supported else 'unknown',reason='CONNTRACK_UNAVAILABLE' if not self.supported else 'COLLECTION_UNCERTAIN',observed=now,quiet_since=None,generation=-1,consultation='unknown',raw_active=None,interval_seconds=None,interval_packets=None,interval_bytes=None)
        self.values={key:value for key,value in self.values.items() if key in self.current}
        self.publish()

    def publish(self):
        self.published=(self.clock(),self.snapshot())

    def status(self):
        observed,snapshot=self.published
        result=copy.deepcopy(snapshot);elapsed=max(0,int(self.clock()-observed))
        for value in result["profiles"].values():
            value["sample_age_seconds"]+=elapsed;value["last_activity_age_seconds"]+=elapsed
            if value["quiet_seconds"] is not None:value["quiet_seconds"]+=elapsed
            value["cooldown_remaining"]=max(0,value["cooldown_remaining"]-elapsed)
            if value["sample_age_seconds"]>15:value.update(state="unknown",reason="SAMPLE_STALE",quiet_seconds=None,consultation="unknown")
        return result

    def snapshot(self):
        now=self.clock();result={}
        for ident in self.current:
            value=self.values.get(ident,{});age=now-value.get('observed',now)
            result[ident]={key:value.get(key) for key in ('state','reason','packets','bytes','tcp_established','consultation','raw_active','interval_seconds','interval_packets','interval_bytes')}
            result[ident].update(sample_age_seconds=max(0,int(age)),generation=self.generation,classifier='bounded_metadata_v1',calibrated=False,quiet_required_seconds=self.quiet_seconds,cooldown_seconds=self.cooldown_seconds,
                last_activity_age_seconds=max(0,int(now-value.get('last_active',now))),
                quiet_seconds=max(0,int(now-value['quiet_since'])) if value.get('quiet_since') is not None else None,
                cooldown_remaining=max(0,int(self.cooldown_seconds-(now-value.get('association_since',now)))))
            if not value or age>15:result[ident].update(state='unknown',reason='SAMPLE_STALE',quiet_seconds=None,consultation='unknown')
        return {'supported':self.supported and not bool(self.catalog_error),'profiles':result,'reason':self.catalog_error or (None if self.supported else 'CONNTRACK_UNAVAILABLE'),'idle_selection_implemented':True}
