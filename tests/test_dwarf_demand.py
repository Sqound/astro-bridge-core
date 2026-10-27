"""Strict synthetic HOME Ping and real demo-controller operation; no real capture."""
import copy
import ipaddress
import socket
import struct
from unittest.mock import Mock,patch
from contextlib import ExitStack

from test_bridge import Fixture
from wifi_bridge.api import MobileAPI,ApiError
from wifi_bridge.config import Invalid
from wifi_bridge.dwarf_demand import ping_filter,ping_timestamp,ping_request,filtered_socket
import test_operations


def varint(number):
    out=bytearray()
    while number>=128:out.append((number&127)|128);number>>=7
    out.append(number);return bytes(out)


def ping(stamp=123456789):return b'\x08\x01\x10'+varint(stamp)+b'\x1a\x04txtl'


def packet(data=None,source='192.168.1.9',destination='192.168.1.255',ihl=20,protocol=17,port=9900,fragment=0):
    data=ping() if data is None else data
    udp=struct.pack('!HHHH',40000,port,8+len(data),0)+data
    ip=bytearray(ihl);ip[0]=0x40+ihl//4;ip[2:4]=struct.pack('!H',ihl+len(udp));ip[9]=protocol;ip[6:8]=struct.pack('!H',fragment)
    ip[12:16]=ipaddress.IPv4Address(source).packed;ip[16:20]=ipaddress.IPv4Address(destination).packed
    return b'\0'*12+b'\x08\x00'+ip+udp


def kernel(frame):
    code=ping_filter(int(ipaddress.IPv4Address('192.168.1.255')));a=x=pc=0
    while pc<len(code):
        op,jt,jf,k=code[pc];pc+=1
        if op in (0x20,0x28,0x30,0x48,0xb1):
            off=k+(x if op==0x48 else 0);size=4 if op==0x20 else 2 if op in (0x28,0x48) else 1
            if off+size>len(frame):return 0
            value=int.from_bytes(frame[off:off+size],'big')
            if op==0xb1:x=(value&15)*4
            else:a=value
        elif op==0x54:a &= k
        elif op==0x15:pc+=jt if a==k else jf
        elif op==0x35:pc+=jt if a>=k else jf
        elif op==0x25:pc+=jt if a>k else jf
        elif op==0x45:pc+=jt if a&k else jf
        elif op==0x87:a=x
        elif op==0x04:a+=k
        elif op==0x16:return a
        elif op==0x06:return k
        else:raise AssertionError('Unknown instruction')
    raise AssertionError('Missing return')


class DwarfPacketTests(Fixture):
    def setUp(self):
        super().setUp();self.net.radios=3;self.configured();self.controller.cycle(0)
        self.observer=self.controller.dwarf_demand;self.plan=self.controller.management_plan

    def enable(self):
        self.controller.demo=False;self.store.config['auto_connect']=True;self.store.config['auto_policy']['dwarf_demand']=True

    def test_ping_canonical_bounded_empty_only_filters_no_echo_or_identity_in_output(self):
        self.assertEqual(ping_timestamp(ping()),123456789)
        self.assertEqual(ping_timestamp(ping()+b'\x22\x00\x2a\x00'),123456789)
        for bad in (b'\x08\x02'+ping()[2:],ping()+b'\x10\x01',ping()+b'\x1a\x04txtl',ping()+b'\x30\x01',
                    ping()+b'\x22\x06SECRET',ping()+b'\x2a\x06SECRET',ping()+b'\x22\x00'*9,
                    b'\x08\x01\x10\x81\x00\x1a\x04txtl',ping(0),ping(2**63),ping()[:-1],b'X'*513):
            self.assertIsNone(ping_timestamp(bad))

    def test_kernel_rejects_echo_camera_unicast_fragments_wrong_ports_and_oversize(self):
        for ihl in (20,24,60):
            frame=packet(ihl=ihl);self.assertEqual(kernel(frame),14+ihl+8+512)
            self.assertIsNotNone(ping_request(frame[:kernel(frame)],0,self.plan))
        for kwargs in ({'data':b'\x08\x02PRIVATE'}, {'data':ping()+b'X'*513}, {'destination':'10.2.0.1'},
                       {'protocol':6},{'port':554},{'fragment':0x2000},{'fragment':1}):
            self.assertEqual(kernel(packet(**kwargs)),0,kwargs)
        self.assertEqual(kernel(packet()[:30]),0)

    def test_scope_rechecks_source_management_broadcast_and_private_status(self):
        self.enable()
        for kwargs,ptype in (({},4),({'source':'192.168.1.42'},0),({'source':'192.168.1.0'},0),
                            ({'source':'192.168.1.255'},0),({'source':'198.51.100.1'},0),({'destination':'10.2.0.1'},0)):
            self.assertIsNone(ping_request(packet(**kwargs),ptype,self.plan))
        with patch.object(self.controller.operations,'demand',return_value='REQUEST_ACCEPTED') as demand:
            self.observer.receive(packet(),0,self.plan);self.observer.receive(packet(),0,self.plan);demand.assert_called_once()
            self.controller.management_plans=();self.observer.receive(packet(ping(42)),0,self.plan);self.assertEqual(demand.call_count,1)
        self.assertNotIn('192.168.1.9',str(self.observer.status()));self.assertTrue(self.observer.status()['discovery_relay']['maker_app_acceptance']=='unverified')

    def test_filter_attachment_before_bind_failure_and_policy_lifecycle_no_real_capture(self):
        stream=Mock();order=[]
        stream.setsockopt.side_effect=lambda level,key,value:order.append('filter' if key==26 else 'buffer')
        stream.bind.side_effect=lambda *_:order.append('bind')
        with patch('wifi_bridge.dwarf_demand.socket.socket',return_value=stream) as create,patch.object(socket,'AF_PACKET',17,create=True):
            filtered_socket(self.plan);self.assertEqual(create.call_args.args,(17,socket.SOCK_RAW,0));self.assertEqual(order,['buffer','filter','bind'])
            stream.setsockopt.side_effect=OSError('PRIVATE')
            with self.assertRaises(OSError):filtered_socket(self.plan)
            stream.close.assert_called_once();self.assertEqual(stream.bind.call_count,1)
        with patch('wifi_bridge.dwarf_demand.filtered_socket',return_value=stream) as create:
            self.observer.sync();create.assert_not_called();self.enable();self.observer.sync();create.assert_called_once_with(self.plan)
            self.store.config['auto_policy']['dwarf_demand']=False;self.observer.sync();self.assertFalse(self.observer.sockets)

    def test_production_startup_has_only_passive_dwarf_thread_and_no_reply_socket(self):
        from wifi_bridge import __main__ as entry
        self.enable();server=Mock();discovery=Mock()
        with ExitStack() as stack:
            for name,value in {'LinuxNetwork':self.net,'Store':self.store,'Controller':self.controller,
                               'WebServer':server,'SeestarDiscovery':discovery}.items():
                stack.enter_context(patch.object(entry,name,return_value=value))
            stack.enter_context(patch.object(entry.os,'geteuid',return_value=0))
            stack.enter_context(patch.object(entry.Path,'exists',return_value=True))
            stack.enter_context(patch.object(entry.Path,'is_file',return_value=False))
            stack.enter_context(patch.object(entry.Path,'read_text',return_value='{"password":"synthetic-bootstrap"}'))
            stack.enter_context(patch.object(entry.signal,'signal'))
            stack.enter_context(patch('sys.argv',['wifi_bridge','--state-dir',self.tmp.name]))
            threads=stack.enter_context(patch.object(entry.threading,'Thread'))
            sockets=stack.enter_context(patch('socket.socket'))
            stack.enter_context(patch('builtins.print'))
            entry.main()
            self.assertEqual([call.kwargs['target'] for call in threads.call_args_list],
                             [self.controller.loop,discovery.loop,self.controller.vespera_demand.loop,self.observer.loop,self.controller.zwo_mount_demand.loop,self.controller.synscan_demand.loop,self.controller.candidates.loop])
            sockets.assert_not_called()

    def test_enabled_passive_observer_never_replies_or_queries_instrument_then_closes(self):
        self.enable();stream=Mock()
        with patch('wifi_bridge.dwarf_demand.filtered_socket',return_value=stream),patch('socket.socket') as sockets:
            self.observer.sync()
            with patch.object(self.controller.operations,'demand',return_value='REQUEST_ACCEPTED') as demand:
                self.observer.receive(packet(),0,self.plan);demand.assert_called_once()
                self.observer.receive(packet(),0,self.plan);demand.assert_called_once()
            for name in ('send','sendto','sendall'):getattr(stream,name).assert_not_called()
            sockets.assert_not_called()
            status=self.controller.status()['maker_demands']['dwarf']
            self.assertTrue(status['passive_only']);self.assertTrue(status['replies_disabled'])
            self.assertFalse(status['discovery_relay']['supported'])
            self.assertEqual(status['discovery_relay']['last_result'],'REPLIES_DISABLED')
            self.assertFalse(status['discovery_relay']['relay_plan_available'])
            self.store.config['auto_policy']['dwarf_demand']=False;self.observer.sync()
            self.assertFalse(self.observer.sockets);stream.close.assert_called_once()

    def test_minutes_of_normal_pings_do_not_exhaust_retransmission_cache_or_change_global_budget(self):
        self.enable();self.store.config['telescopes'][0].update(kind='dwarf',auto_eligible=True,enabled=False)
        self.controller.cycle(10);clock=[1000.];self.observer.clock=lambda:clock[0];self.controller.operations.clock=lambda:clock[0]
        with patch.object(self.controller.operations,'ensure',return_value=(202,{})) as ensure:
            for second in range(301):
                clock[0]=1000.+second;self.observer.receive(packet(ping(second+1)),0,self.plan)
            self.assertEqual(ensure.call_count,3);self.assertLessEqual(len(self.observer.seen),5)
            self.assertEqual(self.controller.operations.clients,{})


class DwarfOperationTests(Fixture):
    replacement=test_operations.OperationTests.replacement
    finish=test_operations.OperationTests.finish

    def setUp(self):
        super().setUp();self.store.config['auto_policy']['dwarf_demand']=False;self.net.radios=3;self.devices=self.net.devices();self.configured();self.controller.cycle(0)
        self.api=MobileAPI(self.controller);self.api.action('primary','disconnect');self.controller.cycle(5)
        self.api.update('settings',{'auto_connect':True})
        self.api.update('telescopes',{'id':'auto-one','name':'Fictional DWARF','kind':'dwarf','target_ip':'10.2.0.1',
            'ssid':'Telescope-DEMO','password':'synthetic-auto-pass','auto_eligible':True},create=True)
        self.ops=self.controller.operations

    def policy(self):
        self.api.update('settings',{'auto_policy':{'dwarf_demand':True},'acknowledge_interruption':True},expected='"'+self.api.revision()+'"')

    def trigger(self):return self.ops.demand(self.controller.management_plan,'192.168.1.9',kind='dwarf')

    def test_actual_packet_to_free_operation_actual_proxy_without_native_success_claim(self):
        self.policy();self.controller.demo=False
        with patch('socket.socket') as sockets:
            self.controller.dwarf_demand.receive(packet(),0,self.controller.management_plan);result=self.finish()
            sockets.assert_not_called()
        self.assertEqual(result['state'],'succeeded');self.assertEqual(result['cause'],'DWARF_DISCOVERY');self.assertEqual(result['endpoint']['proxy']['protocol'],'dwarf')
        self.assertEqual(result['endpoint']['maker_app_acceptance'],'unverified')
        events=[e for e in self.controller.event_store.events if e['operation_id']==result['id']]
        self.assertIn('connected',[e['type'] for e in events]);self.assertEqual({e['cause'] for e in events},{'DWARF_DISCOVERY'})
        self.assertTrue(self.controller.status()['maker_demands']['dwarf']['discovery_relay']['maker_app_acceptance']=='unverified')

    def test_passive_consent_toggle_keeps_ready_manual_pi_proxy_without_echo_capability(self):
        self.policy();self.assertEqual(self.trigger(),'REQUEST_ACCEPTED');self.finish()
        for enabled in (False,True,False):
            self.api.update('settings',{'auto_policy':{'dwarf_demand':enabled},'acknowledge_interruption':True},
                            expected='"'+self.api.revision()+'"')
            with patch('socket.socket') as sockets:self.controller.cycle(50);sockets.assert_not_called()
            row=next(r for r in self.controller.state['telescopes'] if r['id']=='auto-one')
            self.assertTrue(row['connected']);self.assertTrue(row['relay_ready']);self.assertTrue(row['dwarf_proxy'])
            profile=next(p for p in self.store.config['telescopes'] if p['id']=='auto-one')
            endpoint=self.ops.endpoint(profile,row,self.controller.state['home_ip'])
            self.assertEqual(endpoint['proxy']['protocol'],'dwarf');self.assertEqual(endpoint['proxy']['ports'],[9900,8082,554])
            self.assertTrue(self.controller.status()['maker_demands']['dwarf']['replies_disabled'])

    def test_policy_ack_revision_strict_omission_and_ambiguous_family_never_guess(self):
        self.assertEqual(self.trigger(),'DISABLED')
        for bad in (1,'true',None,[],{}):
            with self.assertRaises(Invalid):self.api.update('settings',{'auto_policy':{'dwarf_demand':bad}},expected='"'+self.api.revision()+'"')
        with self.assertRaises(Invalid):self.api.update('settings',{'auto_policy':{'dwarf_demand':True}},expected='"'+self.api.revision()+'"')
        with self.assertRaises(ApiError):self.api.update('settings',{'auto_policy':{'dwarf_demand':True},'acknowledge_interruption':True},expected='"old"')
        self.policy();self.api.update('settings',{'country':'JP'});self.assertTrue(self.store.config['auto_policy']['dwarf_demand'])
        self.api.update('telescopes',{'id':'other-dwarf','name':'Other','kind':'dwarf','ssid':'Other-DEMO','password':'synthetic-pass','target_ip':'10.8.0.1','auto_eligible':True},create=True)
        before=copy.deepcopy(self.store.config)
        self.assertEqual(self.trigger(),'SELECTION_REQUIRED');self.assertIsNone(self.ops.last);self.assertEqual(before,self.store.config)

    def test_reachable_dwarf_preserved_without_eligibility_and_quota_shared_with_seestar_vespera(self):
        self.policy();self.assertEqual(self.trigger(),'REQUEST_ACCEPTED');self.finish()
        self.api.update('telescopes',{'auto_eligible':False},ident='auto-one');self.controller.cycle(50)
        for kind in ('vespera','seestar'):
            self.api.update('settings',{'auto_policy':{kind+'_demand':True},'acknowledge_interruption':True},expected='"'+self.api.revision()+'"')
            self.assertEqual(self.ops.demand(self.controller.management_plan,'192.168.1.9',kind=kind,target='10.2.0.1'),'DEMAND_COOLDOWN')
        self.ops.demand_next=0;self.assertEqual(self.trigger(),'READY_CONNECTION_PRESERVED');self.assertEqual(self.ops.clients,{})

    def test_quiet_vespera_replaced_with_one_correlated_operation_and_notifications(self):
        self.replacement();self.api.update('telescopes',{'kind':'vespera'},ident='primary');self.controller.cycle(40);self.policy()
        self.assertEqual(self.trigger(),'REQUEST_ACCEPTED');result=self.finish()
        self.assertEqual(result['state'],'succeeded');self.assertEqual(result['displaced']['profile_id'],'primary')
        self.assertEqual(result['cause'],'DWARF_DISCOVERY');self.assertEqual(result['endpoint']['proxy']['protocol'],'dwarf')
        events=[e for e in self.controller.event_store.events if e['operation_id']==result['id']]
        self.assertIn(('primary','disconnected'),[(e['profile_id'],e['type']) for e in events]);self.assertIn(('auto-one','connected'),[(e['profile_id'],e['type']) for e in events])
        self.assertEqual({e['cause'] for e in events},{'DWARF_DISCOVERY'})

    def test_active_unknown_locked_victim_never_displaced(self):
        for state in ('active','unknown','quiet'):
            self.replacement(state=state);self.policy()
            if state=='quiet':self.controller.protection.owner_lock('primary',{'locked':True,'current_password':'local-admin-test-password'},'"'+self.api.revision()+'"',self.api.revision)
            self.ops.demand_next=0;self.ops.negative.clear()
            self.assertEqual(self.trigger(),'REQUEST_ACCEPTED');result=self.finish()
            self.assertEqual(result['state'],'failed');self.assertEqual(result['reason'],'NO_REPLACEABLE_CONNECTION')
            self.assertTrue(self.net.connected('telescope',self.devices[0]))
            if state=='quiet':self.controller.protection.owner_lock('primary',{'locked':False,'current_password':'local-admin-test-password'},'"'+self.api.revision()+'"',self.api.revision)

    def test_client_lease_also_blocks_dwarf_victim_replacement(self):
        self.replacement();self.policy()
        self.controller.protection.create('primary',{},'synthetic-client','"'+self.api.revision()+'"',self.api.revision)
        self.assertEqual(self.trigger(),'REQUEST_ACCEPTED');result=self.finish()
        self.assertEqual(result['reason'],'NO_REPLACEABLE_CONNECTION');self.assertTrue(self.net.connected('telescope',self.devices[0]))

    def test_failed_dwarf_association_restores_previous_instrument_with_same_origin(self):
        self.replacement();self.policy();original=self.net.connect
        from wifi_bridge.network import NetworkError
        def connect(role,*args):
            if role=='telescope-auto-one':raise NetworkError('synthetic failure')
            return original(role,*args)
        with patch.object(self.net,'connect',side_effect=connect):
            self.assertEqual(self.trigger(),'REQUEST_ACCEPTED');result=self.finish()
        self.assertEqual(result['state'],'failed');self.assertEqual(result['restoration']['state'],'restored')
        self.assertTrue(self.net.connected('telescope',self.devices[0]))
        events=[e for e in self.controller.event_store.events if e['operation_id']==result['id']]
        self.assertEqual({e['cause'] for e in events},{'DWARF_DISCOVERY'});self.assertIn('restored',[e['type'] for e in events])
