"""Synthetic packet/kernel-filter and socket lifecycle checks; never opens capture."""
import ctypes
import ipaddress
import socket
import struct
from unittest.mock import patch, Mock

from test_bridge import Fixture
from wifi_bridge.vespera_demand import syn_filter, filtered_socket, connection_request


def packet(*, flags=2, port=8083, source='192.168.1.9', target='10.2.0.1', ihl=20, fragment=0, protocol=6, payload=b'', seq=100):
    tcp=struct.pack('!HHI',40000,port,seq)+b'\0'*4+bytes([0x50,flags])+b'\0'*6+payload
    ip=bytearray(ihl);ip[0]=0x40+ihl//4;ip[2:4]=struct.pack('!H',ihl+len(tcp));ip[6:8]=struct.pack('!H',fragment);ip[9]=protocol
    ip[12:16]=ipaddress.IPv4Address(source).packed;ip[16:20]=ipaddress.IPv4Address(target).packed
    return b'\0'*12+b'\x08\x00'+ip+tcp


def kernel_result(frame):
    """Execute the supported classic BPF instructions, including OOB rejection."""
    a=x=pc=0;code=syn_filter()
    while pc<len(code):
        op,jt,jf,k=code[pc];pc+=1
        if op in (0x28,0x30,0x48,0x50,0xb1):
            offset=k+(x if op in (0x48,0x50) else 0);size=2 if op in (0x28,0x48) else 1
            if offset+size>len(frame):return 0
            value=int.from_bytes(frame[offset:offset+size],'big')
            if op==0xb1:x=(value&15)*4
            else:a=value
        elif op==0x54:a &= k
        elif op==0x15:pc += jt if a==k else jf
        elif op==0x35:pc += jt if a>=k else jf
        elif op==0x45:pc += jt if a&k else jf
        elif op==0x87:a=x
        elif op==0x04:a+=k
        elif op==0x16:return a
        elif op==0x06:return k
        else:raise AssertionError('Unexpected filter instruction')
    raise AssertionError('Missing filter return')


class VesperaPacketTests(Fixture):
    def setUp(self):
        super().setUp();self.net.radios=3;self.configured();self.controller.cycle(0)
        self.observer=self.controller.vespera_demand;self.controller.demo=False
        self.plan=self.controller.management_plan

    def enable(self):
        self.store.config['auto_connect']=True
        self.store.config['auto_policy']['vespera_demand']=True

    def test_kernel_filter_rejects_camera_payload_ack_fragments_and_other_protocols(self):
        for ihl in (20,24,60):
            frame=packet(ihl=ihl,flags=0xc2,payload=b'PRIVATE_BODY_CANARY')
            size=kernel_result(frame);self.assertEqual(size,14+ihl+20)
            self.assertNotIn(b'PRIVATE_BODY',frame[:size])
        for args in ({'flags':16},{'flags':18},{'flags':3},{'flags':6},{'port':554},
                     {'port':80},{'fragment':0x2000},{'fragment':1},{'protocol':17}):
            self.assertEqual(kernel_result(packet(**args)),0,args)
        self.assertEqual(kernel_result(b'\0'*30),0)
        self.assertEqual(kernel_result(packet()[:45]),0)

    def test_scope_validation_accepts_only_initial_inbound_management_syn(self):
        for port in (8082,8083):
            frame=packet(port=port);result=connection_request(frame[:kernel_result(frame)],0,self.plan)
            self.assertEqual(result[:2],('192.168.1.9','10.2.0.1'))
        for args in ({'source':'192.168.1.42'},{'source':'192.168.1.0'}, {'source':'192.168.1.255'},
                     {'source':'198.51.100.1'},{'flags':18},{'flags':16},{'flags':6},{'fragment':0x2000}):
            self.assertIsNone(connection_request(packet(**args),0,self.plan),args)
        self.assertIsNone(connection_request(packet(),getattr(socket,'PACKET_OUTGOING',4),self.plan))
        bad=bytearray(packet());bad[14+20+12]=0x10
        self.assertIsNone(connection_request(bad,0,self.plan))

    def test_mandatory_filter_attached_before_bind_and_failure_never_falls_back(self):
        stream=Mock();order=[]
        stream.setsockopt.side_effect=lambda *args:order.append('filter' if args[1]==26 else 'buffer')
        stream.bind.side_effect=lambda *args:order.append('bind')
        with patch('wifi_bridge.vespera_demand.socket.socket',return_value=stream) as create,patch.object(socket,'AF_PACKET',17,create=True):
            self.assertIs(filtered_socket('wlan-test'),stream)
            self.assertEqual(create.call_args.args,(17,socket.SOCK_RAW,0));self.assertEqual(order,['buffer','filter','bind'])
            stream.setsockopt.side_effect=OSError('PRIVATE failure')
            with self.assertRaises(OSError):filtered_socket('wlan-test')
            self.assertEqual(stream.bind.call_count,1);stream.close.assert_called_once()

    def test_policy_management_shutdown_failure_backoff_and_demo_never_capture(self):
        with patch('wifi_bridge.vespera_demand.filtered_socket') as create:
            self.observer.sync();create.assert_not_called()
            self.enable();self.controller.demo=True;self.observer.sync();create.assert_not_called()
            self.controller.demo=False;clock=[100.];self.observer.clock=lambda:clock[0]
            create.side_effect=OSError('PRIVATE failure');self.observer.sync()
            self.assertEqual(self.observer.status()['observer'],'OBSERVER_UNAVAILABLE');self.assertFalse(self.observer.supported)
            self.observer.sync();self.assertEqual(create.call_count,1)
            clock[0]+=31;stream=Mock();create.side_effect=None;create.return_value=stream;self.observer.sync()
            self.assertEqual(self.observer.status()['observer'],'LISTENING')
            self.controller.management_plans=();self.observer.sync();stream.close.assert_called_once()
            self.assertEqual(self.observer.status()['observer'],'MANAGEMENT_UNAVAILABLE')
            self.observer.close();self.assertFalse(self.observer.sockets)

    def test_loop_has_bounded_reads_and_closes_descriptor_on_shutdown(self):
        self.enable();stream=Mock();reads=[]
        def receive(size):
            reads.append(size)
            if len(reads)==4:self.controller.stop.set()
            return packet(), ('wlan-test',0x0800,0,0,b'')
        stream.recvfrom.side_effect=receive
        with patch('wifi_bridge.vespera_demand.filtered_socket',return_value=stream),patch('wifi_bridge.vespera_demand.select.select',return_value=([stream],[],[])),patch.object(self.observer,'receive') as consume:
            self.observer.loop()
        self.assertEqual(reads,[94]*4);self.assertEqual(consume.call_count,4)
        stream.close.assert_called_once();self.assertFalse(self.observer.sockets)

    def test_retransmissions_bounds_target_filter_and_no_source_in_status(self):
        self.enable();self.store.config['telescopes'][0]['kind']='vespera'
        self.store.config['telescopes'][0]['target_ip']='10.2.0.1'
        with patch.object(self.controller.operations,'demand',return_value='REQUEST_ACCEPTED') as demand:
            self.observer.receive(packet(),0,self.plan);self.observer.receive(packet(),0,self.plan)
            demand.assert_called_once_with(self.plan,'192.168.1.9',kind='vespera',target='10.2.0.1')
            self.observer.receive(packet(target='10.99.0.1'),0,self.plan);self.assertEqual(demand.call_count,1)
            for seq in range(1,150):self.observer.receive(packet(seq=seq),0,self.plan)
            self.assertEqual(len(self.observer.seen),128);self.assertEqual(demand.call_count,128)
        self.assertNotIn('192.168.1.9',str(self.observer.status()))
        self.assertNotIn('PRIVATE',str(self.observer.status()))

    def test_busy_reconcile_retransmission_can_retry_without_queued_jobs(self):
        self.enable();self.store.config['telescopes'][0]['kind']='vespera';self.store.config['telescopes'][0]['target_ip']='10.2.0.1'
        with patch.object(self.controller.operations,'demand',side_effect=['UNTRUSTED_MANAGEMENT_SOURCE','REQUEST_ACCEPTED']) as demand:
            self.observer.receive(packet(),0,self.plan);self.assertFalse(self.observer.seen)
            self.observer.receive(packet(),0,self.plan);self.observer.receive(packet(),0,self.plan)
            self.assertEqual(demand.call_count,2)

    def test_pending_header_intent_retries_after_reconcile_without_another_syn(self):
        self.enable();self.store.config['telescopes'][0]['kind']='vespera';self.store.config['telescopes'][0]['target_ip']='10.2.0.1'
        now=[100.];self.observer.clock=lambda:now[0]
        with patch.object(self.controller.operations,'demand',side_effect=['RECONCILE_PENDING','REQUEST_ACCEPTED']) as demand:
            self.observer.receive(packet(),0,self.plan);self.observer.receive(packet(),0,self.plan)
            self.assertEqual(demand.call_count,1);self.assertEqual(len(self.observer.pending),1)
            self.controller.last_cycle_ok=False;self.observer.retry_pending();self.assertEqual(demand.call_count,1)
            self.controller.last_cycle_ok=True
            with self.controller.net_lock:self.observer.retry_pending();self.assertEqual(demand.call_count,1)
            self.observer.retry_pending();self.assertEqual(demand.call_count,2);self.assertFalse(self.observer.pending)
            self.observer.retry_pending();self.assertEqual(demand.call_count,2)
        self.assertNotIn('192.168.1.9',str(self.observer.status()))

    def test_pending_intents_expire_are_bounded_and_discarded_on_policy_or_path_change(self):
        self.enable();self.store.config['telescopes'][0]['kind']='vespera';self.store.config['telescopes'][0]['target_ip']='10.2.0.1'
        now=[100.];self.observer.clock=lambda:now[0]
        with patch.object(self.controller.operations,'demand',return_value='RECONCILE_PENDING') as demand:
            for seq in range(20):self.observer.receive(packet(seq=seq),0,self.plan)
            self.assertEqual(len(self.observer.pending),8)
            count=demand.call_count;now[0]+=16;self.observer.retry_pending()
            self.assertFalse(self.observer.pending);self.assertEqual(demand.call_count,count)
            self.observer.receive(packet(),0,self.plan);self.store.config['auto_connect']=False
            self.observer.retry_pending();self.assertFalse(self.observer.pending)
            self.enable();self.observer.receive(packet(),0,self.plan);self.controller.management_plans=()
            self.observer.retry_pending();self.assertFalse(self.observer.pending)
