"""Generated nft policy and runner/ownership fakes; never execute nft or setns."""
import contextlib
import copy
import socket
import tempfile
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import Mock, patch

from wifi_bridge.isolated import IsolatedRadios, namespace_firewall, namespace_socket
from wifi_bridge.isolated_observer import OBSERVER_MARK, SETS, flush, input_rule, output_rules
from wifi_bridge.telemetry.alpaca import Transport


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.calls=[]
        def runner(args,**kwargs):self.calls.append((args,kwargs));return ''
        with patch('wifi_bridge.isolated.linux_boot_id',return_value='synthetic-boot'):
            self.manager=IsolatedRadios(self.tmp.name,runner)
        self.p={'id':'mount','kind':'zwo_mount','mac':'02:00:00:00:00:01','target_ip':'192.0.2.1','enabled':True,'isolated':True}
        self.record={'device':{'name':'wlan7','mac':self.p['mac']},'namespace':'ab-radio-020000000001',
            'kind':'zwo_mount','profile_id':'mount','role':'telescope-mount','target':'192.0.2.1','intent':self.manager.intent(self.p),'created':True,'boot_id':'synthetic-boot'}
        self.manager.records[self.p['mac']]=self.record
        self.access=self.manager.observer_access(self.record,self.p,'192.0.2.2/24')
        self.addCleanup(self.access.close)

    def scripts(self):return [kw['input'] for _,kw in self.calls]

    def test_default_empty_expiring_sets_forward_drop_and_native_unmarked_limits(self):
        script=namespace_firewall('192.0.2.1','tcp',4030)
        self.assertIn('chain forward { type filter hook forward priority 0; policy drop; }',script)
        self.assertEqual(script.count('policy drop'),3)
        self.assertIn(f'meta mark != {OBSERVER_MARK} ip daddr 192.0.2.1 tcp dport 4030 accept',script)
        self.assertIn('udp sport 67 udp dport 68 accept',script)
        self.assertIn('udp sport 68 udp dport 67 accept',script)
        self.assertIn('ip daddr 192.0.2.1 icmp type echo-request accept',script)
        self.assertIn('timeout 180s; size 4;',script)
        self.assertEqual(script.count('timeout 5s; size 1;'),2)
        self.assertNotIn('elements =',script)
        self.assertNotIn('0.0.0.0/0',script)
        self.assertNotIn('11880',script)
        self.assertIn(output_rules(),script)
        self.assertEqual(self.calls,[])  # Constructing owner metadata does not run network work.

    def test_discovery_reply_ct_new_has_exact_target_local_socket_path(self):
        self.access.discovery(40001)
        script=self.scripts()[-1]
        self.assertIn('alpaca_reply { 192.0.2.1 . 192.0.2.2 . 40001 timeout 5s }',script)
        self.assertIn('alpaca_broadcast { 192.0.2.255 . 192.0.2.2 . 40001 timeout 5s }',script)
        rule=input_rule()
        self.assertEqual(rule,'ip protocol udp ip saddr . ip daddr . udp dport @alpaca_reply accept; ')
        self.assertNotIn('ct state',rule)  # Broadcast replies may be ct-new, not established.
        self.assertIn('ip protocol udp udp dport 32227',output_rules())
        self.assertIn('ip daddr . ip saddr . udp sport @alpaca_broadcast',output_rules())
        self.assertNotIn('192.0.2.9',script);self.assertNotIn('40002',script)
        self.access.discovery()
        self.assertEqual(self.scripts()[-1],flush(('alpaca_reply','alpaca_broadcast')))
        self.assertEqual(self.calls[0][0],['ip','netns','exec','ab-radio-020000000001','nft','-f','-'])
        self.assertEqual(self.calls[0][1]['timeout'],2)

    def test_http_only_discovered_target_ports_bounded_expiry_and_atomic_replacement(self):
        self.access.endpoints([('192.0.2.1',32323),('192.0.2.1',11111)])
        self.assertIn('192.0.2.1 . 11111 timeout 180s, 192.0.2.1 . 32323 timeout 180s',self.scripts()[-1])
        before=len(self.calls)
        for endpoints in ([('192.0.2.9',32323)],[('192.0.2.1',True)],[('192.0.2.1',65536)],
                          [('192.0.2.1',i) for i in range(1,6)]):
            with self.assertRaises(ValueError):self.access.endpoints(endpoints)
        self.assertEqual(len(self.calls),before)
        self.access.endpoints([('192.0.2.1',32324)])
        self.assertTrue(self.scripts()[-1].startswith('flush set inet ab_instrument alpaca_http\n'))
        self.assertNotIn('32323',self.scripts()[-1])
        self.access.close();self.assertEqual(self.scripts()[-1],flush())
        with self.assertRaises(PermissionError):self.access.endpoints([('192.0.2.1',32323)])

    def test_new_generation_owns_sets_and_old_cleanup_never_revokes_new_permissions(self):
        self.access.endpoints([('192.0.2.1',32323)])
        new=self.manager.observer_access(self.record,self.p,'192.0.2.2/24');self.addCleanup(new.close)
        new.endpoints([('192.0.2.1',32324)])
        before=len(self.calls);self.access.close();self.assertEqual(len(self.calls),before)
        self.assertIs(self.manager.observer_permissions[self.p['mac']],new)
        with self.assertRaises(PermissionError):self.access.discovery(40001)
        self.manager.close_profile('mount')
        self.assertTrue(new.closed);self.assertEqual(self.scripts()[-1],flush())
        self.assertFalse(self.manager.observer_accesses)

    def test_replaced_record_target_intent_or_namespace_teardown_never_touches_foreign_namespace(self):
        self.access.endpoints([('192.0.2.1',32323)])
        before=len(self.calls)
        self.manager.records[self.p['mac']]=copy.deepcopy(self.record)
        with self.assertRaises(PermissionError):self.access.discovery(40001)
        self.access.close();self.assertEqual(len(self.calls),before)
        self.manager.records[self.p['mac']]=self.record
        for key,value in (('target','192.0.2.9'),('intent','invalid'),('created',False),('observer_closing',True)):
            original=self.record.get(key);self.record[key]=value
            with self.assertRaises(PermissionError):self.manager.observer_access(self.record,self.p,'192.0.2.2/24')
            if original is None:self.record.pop(key)
            else:self.record[key]=original
        for field in ('enabled','isolated'):
            with self.assertRaises(PermissionError):self.manager.observer_access(self.record,dict(self.p,**{field:False}),'192.0.2.2/24')

    def test_mark_only_observer_socket_and_failure_closes_without_root_fallback(self):
        stream=Mock()
        with patch('wifi_bridge.isolated.in_namespace',return_value=contextlib.nullcontext()) as enter, \
             patch('wifi_bridge.isolated.socket.socket',return_value=stream), \
             patch.object(socket,'SO_BINDTODEVICE',25,create=True),patch.object(socket,'SO_MARK',36,create=True):
            self.assertIs(self.access.socket(socket.SOCK_STREAM),stream)
            stream.setsockopt.assert_any_call(socket.SOL_SOCKET,36,OBSERVER_MARK)
            enter.assert_called_once_with('/run/netns/ab-radio-020000000001')
            stream.reset_mock();namespace_socket('/synthetic','wlan7',socket.SOCK_DGRAM)
            self.assertEqual(stream.setsockopt.call_count,1)  # Native relays stay unmarked.
            stream.setsockopt.side_effect=PermissionError()
            with self.assertRaises(PermissionError):self.access.socket(socket.SOCK_DGRAM)
            stream.close.assert_called_once()
        self.access.close();stream.shutdown.assert_called_with(socket.SHUT_RDWR)

    def test_command_failure_fails_closed_and_cleanup_still_closes_sockets(self):
        self.manager.run=Mock(side_effect=RuntimeError('synthetic failure'))
        with self.assertRaises(PermissionError):self.access.endpoints([('192.0.2.1',32323)])
        self.assertFalse(self.manager.observer_permissions)
        self.access.close();self.assertFalse(self.manager.observer_accesses)

    def test_discovery_transport_installs_and_revokes_reply_tuple_around_send(self):
        stream=Mock();stream.getsockname.return_value=('192.0.2.2',40001)
        stream.recvfrom.side_effect=socket.timeout()
        order=[]
        original=self.access.discovery
        def grant(port=None):order.append(('grant',port));original(port)
        self.access.discovery=grant
        stream.sendto.side_effect=lambda *_:order.append(('send',None))
        transport=Transport('192.0.2.2/24','wlan7',lambda _:stream,target='192.0.2.1',permission=self.access)
        self.assertEqual(transport.discover(),[])
        self.assertEqual(order,[('grant',40001),('send',None),('grant',None)])
        self.assertEqual(self.scripts()[-1],flush(('alpaca_reply','alpaca_broadcast')))

    def test_release_waits_for_first_policy_write_before_invalidating_or_tearing_down(self):
        entered=threading.Event();unblock=threading.Event();release_started=threading.Event()
        order=[];errors=[]
        def runner(args,**kwargs):
            if not entered.is_set():
                entered.set();order.append('grant-start');unblock.wait(2);order.append('grant-end')
            else:order.append('revoke')
            return ''
        self.manager.run=runner
        def grant():
            try:self.access.endpoints([('192.0.2.1',32323)])
            except Exception as exc:errors.append(type(exc).__name__)
        def release():
            release_started.set();self.manager.release('telescope-mount')
        with patch.object(self.manager,'_stop_processes',side_effect=lambda _:order.append('teardown')), \
             patch('wifi_bridge.isolated.Path.exists',return_value=False):
            one=threading.Thread(target=grant);one.start();self.assertTrue(entered.wait(1))
            started=time.monotonic()
            pending=self.manager.observer_access(self.record,self.p,'192.0.2.2/24')
            self.assertLess(time.monotonic()-started,.1)  # Metadata never waits on nft's manager lock.
            two=threading.Thread(target=release);two.start();self.assertTrue(release_started.wait(1))
            self.assertNotIn('observer_closing',self.record)
            self.assertNotIn('teardown',order)
            unblock.set();one.join(2);two.join(2)
        self.assertFalse(one.is_alive());self.assertFalse(two.is_alive());self.assertFalse(errors)
        self.assertEqual(order,['grant-start','grant-end','revoke','teardown'])
        self.assertTrue(self.access.closed);self.assertTrue(pending.closed);self.assertFalse(self.manager.records)
        self.assertFalse(self.manager.observer_permissions)
        with self.assertRaises(PermissionError):self.access.endpoints([('192.0.2.1',32323)])

    def test_release_waits_for_namespace_socket_creation_then_closes_it(self):
        entered=threading.Event();unblock=threading.Event();release_started=threading.Event()
        stream=Mock();errors=[]
        def create(*args,**kwargs):entered.set();unblock.wait(2);return stream
        def opened():
            try:self.access.socket(socket.SOCK_STREAM)
            except Exception as exc:errors.append(type(exc).__name__)
        def release():release_started.set();self.manager.release('telescope-mount')
        with patch('wifi_bridge.isolated.namespace_socket',side_effect=create), \
             patch.object(self.manager,'_stop_processes'),patch('wifi_bridge.isolated.Path.exists',return_value=False):
            one=threading.Thread(target=opened);one.start();self.assertTrue(entered.wait(1))
            two=threading.Thread(target=release);two.start();self.assertTrue(release_started.wait(1))
            self.assertNotIn('observer_closing',self.record);stream.close.assert_not_called()
            unblock.set();one.join(2);two.join(2)
        self.assertFalse(one.is_alive());self.assertFalse(two.is_alive());self.assertFalse(errors)
        stream.shutdown.assert_called_once_with(socket.SHUT_RDWR);stream.close.assert_called_once()
        self.assertTrue(self.access.closed);self.assertFalse(self.manager.records)

    def test_failed_revocation_closes_live_socket_and_leaves_only_expiring_kernel_permissions(self):
        stream=Mock()
        self.access.endpoints([('192.0.2.1',32323)])
        with patch('wifi_bridge.isolated.namespace_socket',return_value=stream):self.access.socket(socket.SOCK_STREAM)
        self.manager.run=Mock(side_effect=RuntimeError('synthetic policy failure'))
        self.access.close()
        stream.shutdown.assert_called_once_with(socket.SHUT_RDWR);stream.close.assert_called_once()
        self.assertFalse(self.manager.observer_permissions);self.assertFalse(self.manager.observer_accesses)
        with self.assertRaises(PermissionError):self.access.socket(socket.SOCK_STREAM)

    def test_previous_owned_table_is_upgraded_atomically_once_preserving_native_policy(self):
        self.access.discovery(40001)
        script=self.scripts()[0]
        self.assertTrue(script.startswith('delete table inet ab_instrument\n'))
        self.assertIn(namespace_firewall('192.0.2.1','tcp',4030),script)
        self.assertIn('alpaca_reply { 192.0.2.1 . 192.0.2.2 . 40001 timeout 5s }',script)
        self.assertTrue(self.record['observer_policy_v1'])
        self.access.endpoints([('192.0.2.1',32323)])
        self.assertNotIn('delete table',self.scripts()[1])
        self.assertNotIn('chain output',self.scripts()[1])
        self.assertEqual(len(self.calls),2)

    def test_actual_network_error_has_no_member_io_root_fallback_or_tight_restart(self):
        from wifi_bridge.network import NetworkError
        from wifi_bridge.telemetry.adapters.alpaca_observer import ADAPTER, AlpacaCollector, Observer
        now=[0]
        sessions=SimpleNamespace(closed=False,touch=Mock(),observe=Mock(),
            readings=SimpleNamespace(clear=Mock(),receive=Mock()))
        row={'id':'mount','connected':True,'interface':'wlan7','client_ip':'192.0.2.2/24'}
        controller=SimpleNamespace(lock=threading.RLock(),stop=threading.Event(),demo=False,
            telemetry_sessions=sessions,telemetry_store=SimpleNamespace(monotonic=lambda:now[0],settings={'profiles':{'mount':{'adapters':[ADAPTER]}}}),
            store=SimpleNamespace(config={'telescopes':[self.p]}),state={'telescopes':[row]})
        collector=AlpacaCollector(controller)
        observer=Observer(collector,self.p,row,self.access.socket,self.access);self.addCleanup(observer.stop)
        sessions.readings.clear.assert_called_once_with(self.p['id'],ADAPTER)
        self.manager.run=Mock(side_effect=NetworkError('synthetic unavailable runner'))
        stream=Mock();stream.getsockname.return_value=('192.0.2.2',40001)
        with patch('wifi_bridge.isolated.namespace_socket',return_value=stream), \
             patch('wifi_bridge.telemetry.alpaca.socket.socket') as root_socket:
            observer.poll()
            self.assertEqual(self.manager.run.call_count,1)
            for value in (0,.5,1,5,14.999):now[0]=value;observer.poll()
            self.assertEqual(self.manager.run.call_count,1)
            now[0]=15;observer.poll()
            self.assertEqual(self.manager.run.call_count,2)
            root_socket.assert_not_called()
        stream.sendto.assert_not_called();stream.connect.assert_not_called()
        sessions.touch.assert_not_called();sessions.observe.assert_not_called()
        sessions.readings.receive.assert_not_called()
        self.assertFalse(observer.halt.is_set())  # Worker remains alive for bounded cadence retries.
