"""Disposable real HTTP/native facade flows with explicit synthetic network inputs."""
import copy
import http.client
import json
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit, urlencode
from wifi_bridge.config import Store, validate_config
from wifi_bridge.controller import Controller
from wifi_bridge.network import DemoNetwork
from wifi_bridge.web import WebServer
from .clock_fixture import event_clock

LAST_PATHS = []  # Actual request paths of the latest run, for fixture maintenance.
NAMES = {'station-instrument','instrument-operation','night-reservation','connect-switch-progress','instrument-identity','camera-indi','camera-attached','connect-operation-running','telemetry-catch-up','instruments-list','connect-ready','connect-allocating','connect-needs-replacement',
         'connect-protected','connect-disabled-by-owner','stale-revision','lease-renew-and-release',
         'disconnect-by-user','wired-home','hotspot-path','lost-and-restored','moved-to-other-radio',
         'events-wait-timeout','events-gap-reset'}


class Network(DemoNetwork):
    def wired_home(self): return getattr(self, 'wired', None)

    def connected(self, role, device, config=None):
        if device and device.get('kind') == 'ethernet': return role == 'home' and device == self.wired_home()
        return super().connected(role, device, config)

    def address(self, device, timeout=20):
        if device.get('kind') == 'ethernet': return device['address']
        if self.connected('home', device): return getattr(self, 'home_address', '192.0.2.10')+'/24'
        return super().address(device, timeout)


def exchange(port, request):
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=30)
    headers=dict(request['headers']);headers['Content-Type']='application/json'
    c.request(request['method'],request['path'],None if request['body'] is None else json.dumps(request['body']),headers)
    r=c.getresponse();result=(r.status,json.loads(r.read()));c.close();return result


@event_clock(synchronized=False)
def run(flow):
    """Execute declared requests. Only radios, service measurements and time are simulated."""
    if flow['name'] in ('camera-attached','camera-indi'):
        from .native_camera_flow import run as run_camera
        return run_camera(flow)
    name=flow['name'];result=[];LAST_PATHS.clear()
    with tempfile.TemporaryDirectory() as directory:
        net=Network(5 if name=='instrument-operation' else 4);devices=net.devices();store=Store(directory,devices[0]['mac'])
        if name == 'wired-home':
            net.wired={'kind':'ethernet','name':'enp1s0','mac':'02:00:00:20:00:01','address':'192.0.2.10/24','usb':False,'label':'enp1s0'}
        allocating=name in ('connect-switch-progress','connect-allocating','connect-needs-replacement','connect-operation-running')
        telescope={'id':'tel-seestar-1','name':'Test telescope','kind':'seestar','ssid':'Telescope-DEMO',
                   'open':True,'mac':devices[0]['mac'],'target_ip':'198.51.100.1','enabled':not allocating}
        config={'admin_password':'test-password-1','hostname':'astro-bridge-test','country':'JP','home':{'ssid':'Synthetic home','open':True,'mac':devices[1]['mac']},
                'hotspot':{'enabled':True,'mac':devices[2]['mac']},'connection_mode':'private','telescopes':[telescope]}
        if name in ('connect-needs-replacement','connect-operation-running'):
            net.radios=3
            config['telescopes'].append(dict(telescope,id='tel-vespera-1',name='Test telescope two',kind='vespera',enabled=True,auto_replaceable=True))
        if name == 'instrument-operation':
            # Two connected instruments on their own radios; each reports its own operation.
            telescope['enabled'] = False
            config['telescopes'].append(dict(telescope,id='tel-vespera-1',name='Test telescope two',kind='vespera',ssid='Telescope-DEMO-2',mac=devices[3]['mac'],enabled=True))
            config['telescopes'].append(dict(telescope,id='tel-dwarf-1',name='Test telescope three',kind='dwarf',ssid='Telescope-DEMO-3',mac=devices[4]['mac'],target_ip='203.0.113.1',enabled=True))  # Another subnet: no isolation needed.
        if name == 'station-instrument':
            # Joined to the bridge network: no radio, SSID or password.
            config['stations'] = [{'id':'tel-station-1','name':'Test station telescope','kind':'seestar','target_ip':'198.51.100.20'}]
        if name == 'instrument-identity':
            # One associated instrument at a time on this synthetic subnet.
            telescope['enabled'] = False
            config['telescopes'].append(dict(telescope,id='tel-vespera-1',name='Test telescope two',kind='vespera',ssid='Telescope-DEMO-2',mac=devices[3]['mac'],enabled=True))
        store.save(validate_config(config,store.config,devices))
        c=Controller(store,net,{'password':'test-password-1'},demo=True);c.update(devices=net.devices());c.cycle(0)
        if name == 'instrument-operation': c.cycle(0)  # Associates the second instrument.
        c.protection.clock=lambda:100.0
        c.operations.started=c.operations.last_replacement=0
        net.sample_activity=lambda **_:None
        net.selection_activity=lambda *_:{'state':'quiet','sample_age_seconds':0,'quiet_seconds':120,'cooldown_remaining':0,'last_activity_age_seconds':180}
        if name == 'connect-operation-running':
            from types import SimpleNamespace
            from wifi_bridge.telemetry.adapters.vespera_status import VesperaCollector, Observer
            store.config['auto_policy'].update(protect_running_operations=True, force_first_profile=True)
            # Only source status is synthetic; use the actual normalizer, cache,
            # policy, native scheduler, HTTP handler and deferred-event producer.
            source = SimpleNamespace(**dict(vars(c), demo=False))
            c.vespera_collector = VesperaCollector(source)
            c.vespera_collector.clock = lambda: 1.0
            profile = store.config['telescopes'][1]
            row = next(r for r in c.state['telescopes'] if r['id'] == profile['id'])
            observer = Observer(c.vespera_collector, profile, row['interface'])
            c.vespera_collector.workers[profile['id']] = observer
            observer.receive({'currentOperation':{'kind':'stacking', 'state':'running'}})
        if name == 'instrument-operation':
            from types import SimpleNamespace
            from wifi_bridge.dwarf_relay import encode
            from wifi_bridge.telemetry.adapters.vespera_status import VesperaCollector, Observer
            store.config['auto_policy'].update(protect_running_operations=True)
            # Only the manufacturer status and notification bytes are synthetic.
            source = SimpleNamespace(**dict(vars(c), demo=False))
            c.vespera_collector = VesperaCollector(source); c.vespera_collector.clock = lambda: 1.0
            profile = store.config['telescopes'][1]
            row = next(r for r in c.state['telescopes'] if r['id'] == profile['id'])
            assert row.get('connected'), row
            observer = Observer(c.vespera_collector, profile, row['interface'])
            c.vespera_collector.workers[profile['id']] = observer
            observer.receive({'currentOperation':{'kind':'stacking', 'state':'running'}})
            packet = lambda state: encode({4: 9, 5: 15208, 6: 2, 7: encode({1: state}, {1: 0})}, {4: 0, 5: 0, 6: 0, 7: 2})
            assert c.dwarf_capture.receive('tel-dwarf-1', packet(1))
        if name == 'instrument-identity':
            from types import SimpleNamespace
            from wifi_bridge.telemetry.adapters.vespera_status import VesperaCollector, Observer
            # Only the manufacturer status reply is synthetic; the real observer reduces it.
            source = SimpleNamespace(**dict(vars(c), demo=False))
            c.vespera_collector = VesperaCollector(source)
            profile = store.config['telescopes'][1]
            row = next(r for r in c.state['telescopes'] if r['id'] == profile['id'])
            assert row.get('connected'), row
            Observer(c.vespera_collector, profile, row['interface']).receive({'telescopeId':'vespera-synthetic-000001','model':'vespera2'})
        server=WebServer(('127.0.0.1',0),c);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        gate=threading.Event();original_run=c.operations.run
        if name in ('connect-allocating','connect-switch-progress'):
            def delayed(ident):gate.wait(5);original_run(ident)
            c.operations.run=delayed
        try:
            _,login=exchange(server.server_port,{'method':'POST','path':'/api/v1/auth/token','headers':{},'body':{'password':'test-password-1'}})
            _,paired=exchange(server.server_port,{'method':'POST','path':'/api/v1/auth/clients','headers':{'Authorization':'Bearer '+login['access_token']},'body':{'name':'Synthetic native client','scopes':['read','connect','telemetry']+(['admin'] if name == 'telemetry-catch-up' else [])}})
            _,token=exchange(server.server_port,{'method':'POST','path':'/api/v1/auth/token','headers':{},'body':{'client_id':paired['client_id'],'client_secret':paired['client_secret']}})
            operation_id=None;lease_id=None
            if name=='disconnect-by-user':lease_id=c.protection.native_create('tel-seestar-1',paired['client_id'],900)['id']
            event_cursor = c.event_store.sequence
            event_now = [100.0]
            if name in ('lost-and-restored','moved-to-other-radio','events-wait-timeout','events-gap-reset'):
                c.event_store.monotonic=lambda:event_now[0]
                c.event_store.last_write=event_now[0]
                for state in c.connection_events.states.values(): state['state_emitted']=event_now[0]
                # Establish ingress before the measured transition.
                for path in c.state['management']['paths']:
                    if path['role']=='home': path['ip']='127.0.0.1'
                c._native_event_snapshot()
                event_cursor=c.event_store.sequence
            if name == 'telemetry-catch-up':
                from types import SimpleNamespace
                journal = c.telemetry_store
                journal.boot_id = '0'*32
                journal.monotonic = lambda: 1.0
                journal.clock = SimpleNamespace(read=lambda: {'system_utc':'2026-01-01T00:00:00+00:00', 'corrected_utc':'2026-01-01T00:00:00+00:00', 'clock_status':'unverified', 'source':'lower_bound', 'offset_ms':0})
                with patch('wifi_bridge.telemetry.store.uuid.uuid4', return_value=SimpleNamespace(hex='a'*32)):
                    telemetry_id = journal.start_session('tel-seestar-1','seestar',['link_v1'])
                journal.sample(telemetry_id,'link.rssi_dbm',-50)
                journal.sample(telemetry_id,'mount.tracking',True)
            for index,step in enumerate(flow['steps']):
                if name == 'connect-operation-running' and index == 1:
                    c.operations.thread.join(5)
                    assert any(e.get('reason') == 'OPERATION_RUNNING' for e in c.event_store.events)
                    assert store.config['telescopes'][1]['enabled']
                if name == 'telemetry-catch-up' and index == 4:
                    journal.flush()
                    with journal.db: journal.db.execute('DELETE FROM samples WHERE seq=2')
                if name == 'telemetry-catch-up' and index == 5: journal.history_available = False
                if name=='lost-and-restored' and index==1:
                    event_now[0]+=5
                    with patch.object(net,'devices',return_value=devices[1:]): c.cycle(5)
                    event_now[0]+=5; c.cycle(20)
                if name=='moved-to-other-radio' and index==0:
                    # Move on the controller's owner path, then observe a new primary proxy address.
                    c.telescope_action({'id':'tel-seestar-1','action':'connect','mac':devices[3]['mac']})
                    net.home_address='192.0.2.11'
                    event_now[0]+=5; c.cycle(20)
                    for path in c.state['management']['paths']:
                        if path['role']=='home': path['ip']='127.0.0.1'
                    c._native_event_snapshot()
                if name=='events-gap-reset' and index==6: c.event_store.history_available=False
                if name=='events-gap-reset' and index==2:
                    # A bounded journal overflow is a real retention gap, not a mocked HTTP response.
                    for _ in range(300): c.event_store.native('instrument.restored','tel-seestar-1')

                if name == 'station-instrument':
                    # Only the handshake outcome is synthetic; the real projection, lease and endpoint run.
                    if index == 0: c.stations.tick()
                    if index == 2: c.stations.record('tel-station-1', None)
                    if index == 3: c.stations.record('tel-station-1', 1.0)
                if name == 'instrument-operation' and index == 2: assert c.dwarf_capture.receive('tel-dwarf-1', packet(3))
                if name=='connect-disabled-by-owner':store.config['telescopes'][0]['client_connect']=False
                if name=='connect-protected':
                    store.config['telescopes'][0]['owner_locked']=index==0
                    if index==1:c.protection.native_create('tel-seestar-1','another-client',900)
                if name=='instruments-list':
                    # Synthetic association/service observations exercise the production projection.
                    row=c.state['telescopes'][0];p=store.config['telescopes'][0]
                    link,service=[('connected','ready'),('connecting','unknown'),('available','not_probed'),('saved','unknown'),('connected','not_answering'),('disabled','unknown'),('disabled','unknown')][index]
                    p['client_connect']=link!='disabled';p['enabled']=link in ('connected','connecting')
                    row.update(connected=link=='connected',state='connecting' if link=='connecting' else 'waiting',reason=None,service=dict(row.get('service', {}), state=service))
                    c.candidates.nearby_at=c.candidates.clock() if link=='available' else None
                    c.candidates.nearby_rows=({'ssid':p['ssid'],'bssid':p['bssid'],'scan_source':'fresh','signal':90,'suggested':{'kind':'seestar'}},)
                if name in ('connect-allocating','connect-switch-progress') and index==2:
                    gate.set();c.operations.thread.join(5)
                # Local accepted socket selects the synthetic management path; clients cannot supply it.
                for path in c.state['management']['paths']:
                    if path['role']==('ap' if name=='hotspot-path' else 'home'):path['ip']='127.0.0.1'
                request=copy.deepcopy(step['request'])
                if 'Authorization' in request['headers']:request['headers']['Authorization']='Bearer '+token['access_token']
                if 'If-Match' in request['headers'] and not (name=='stale-revision' and index==0):request['headers']['If-Match']='"'+server.api.revision()+'"'
                if '/operations/' in request['path']:request['path']='/api/v1/operations/'+operation_id
                if request['body'] and 'lease_id' in request['body']:request['body']['lease_id']=lease_id
                if name in ('lost-and-restored','moved-to-other-radio','events-wait-timeout','events-gap-reset','connect-switch-progress') and '/events?' in request['path']:
                    query=parse_qs(urlsplit(request['path']).query)
                    query['after']=[str(0 if name=='events-gap-reset' and index==2 else event_cursor)]
                    if name=='events-gap-reset' and index==4: query['after']=[str(c.event_store.sequence+1)]
                    if name=='events-gap-reset' and index==6: query.pop('after',None)
                    query['boot_id']=['1'*32 if name=='events-gap-reset' and index==0 else c.event_store.boot_id]
                    request['path']='/api/v1/events?'+urlencode({key:value[0] for key,value in query.items()})
                if name=='events-wait-timeout':
                    with patch.object(c.event_store.condition,'wait',side_effect=lambda seconds:event_now.__setitem__(0,event_now[0]+seconds)):
                        status,body=exchange(server.server_port,request)
                else: status,body=exchange(server.server_port,request)
                if body.get('operation'):operation_id=body['operation']['id']
                if body.get('lease',{}).get('id'):lease_id=body['lease']['id']
                LAST_PATHS.append(request['path'])
                result.append((status,body))
        finally:
            gate.set()
            if c.operations.thread:c.operations.thread.join(5)
            c.stop.set();server.shutdown();server.server_close();thread.join()
    return result


def normalize(body):
    """Deterministic fixture placeholders; never writes live credentials."""
    body=copy.deepcopy(body);patterns={}
    def scrub(value,path=''):
        if isinstance(value,dict):
            for key,item in value.items():
                pointer=path+'/'+key
                if key in ('timestamp','utc_corrected'):
                    value[key]='2026-01-01T00:00:00.000+00:00';patterns[pointer]='^\\d{4}-\\d{2}-\\d{2}T'
                elif key=='operation_id':
                    value[key]='0'*32;patterns[pointer]='^[0-9a-f]{32}$'
                elif key in ('radio_id','from_radio_id','to_radio_id') and item is not None:
                    value[key]='radio-'+('1' if key=='to_radio_id' else '0')*16;patterns[pointer]='^radio-[0-9a-f]{16}$'
                elif key=='lease_id':
                    value[key]='lease-1';patterns[pointer]='^[0-9a-f]{32}$'
                elif key=='config_revision':value[key]='revision-1';patterns[pointer]='^[0-9a-f]{64}$'
                elif key=='boot_id':value[key]='0'*32;patterns[pointer]='^[0-9a-f]{32}$'
                elif key=='device_id':value[key]='0123456789abcdef';patterns[pointer]='^[0-9a-f]{16}$'
                elif key=='id' and path in ('/lease','/operation','') and isinstance(item,str) and len(item) in (32,65):
                    value[key]='lease-1' if len(item)==32 else '0'*32+'.'+'1'*32
                    patterns[pointer]='^[0-9a-f]{32}$' if len(item)==32 else '^[0-9a-f]{32}\\.[0-9a-f]{32}$'
                elif key=='status_url':
                    value[key]='/api/v1/operations/'+'0'*32+'.'+'1'*32
                    patterns[pointer]='^/api/v1/operations/[0-9a-f]{32}\\.[0-9a-f]{32}$'
                else:scrub(item,pointer)
        elif isinstance(value,list):
            for i,item in enumerate(value):scrub(item,path+'/'+str(i))
    scrub(body)
    return body,patterns
