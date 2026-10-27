"""Offline retired Echo codec and active TCP proxy regression tests."""
import ipaddress
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.api import MobileAPI
from wifi_bridge.dwarf_relay import AP,STA,TOP,EchoRejected,encode,fields,sanitize_echo,relay_ping_timestamp,varint
from wifi_bridge.network import LinuxNetwork,NetworkError
from test_dwarf_demand import ping
from wifi_bridge.dwarf_demand import ping_timestamp

SECRET=b'PRIVATE_CREDENTIAL_CANARY'
STAMP=123456


def echo(scheme=b'1.0',stamp=STAMP):
    ap=encode({1:b'wlan',2:1,3:b'XX',4:b'Fictional scope',5:b'WPA2',6:SECRET,7:ipaddress.ip_address('10.0.0.1').packed,8:b'\x01'*16},AP)
    sta=encode({1:b'wlan',2:b'Fictional LAN',3:SECRET,4:0,6:b'\x02'*16},STA)
    return encode({1:2,2:123457,3:b'txtl',4:stamp,5:b'fictional-mac',6:encode({1:4,2:1},{1:0,2:0}),
                   7:b'fictional-serial',8:b'fictional-name',9:SECRET,10:b'1.0',11:scheme,12:7,13:ap,14:sta},TOP)


class CodecTests(Fixture):
    def test_real_identity_preserved_credentials_and_unrouted_ipv6_removed_both_paths(self):
        source=fields(echo(),TOP,4096)
        for endpoint in ('192.168.1.2','192.168.77.1'):
            raw=sanitize_echo(echo(),STAMP,'10.0.0.1',endpoint);value=fields(raw,TOP,4096)
            self.assertNotIn(SECRET,raw);self.assertNotIn(9,value)
            self.assertEqual({k:v for k,v in value.items() if k not in (13,14)},
                             {k:v for k,v in source.items() if k not in (9,13,14)})
            for key,schema,credential,ipv4,ipv6 in ((13,AP,6,7,8),(14,STA,3,5,6)):
                nested=fields(value[key],schema,1024)
                self.assertNotIn(credential,nested);self.assertNotIn(ipv6,nested)
                if key==13:self.assertEqual(nested[ipv4],ipaddress.ip_address(endpoint).packed)
                else:self.assertNotIn(ipv4,nested)

    def test_absent_empty_zero_ipv4_and_all_network_metadata_preserved_without_fake_sta(self):
        for state in ('absent','empty','zero','real'):
            value=fields(echo(),TOP,4096);sta=fields(value[14],STA,1024)
            sta[2]=b'DWARF_STA';sta[4]=2147483647
            if state!='absent':sta[5]={'empty':b'','zero':b'\0'*4,'real':ipaddress.ip_address('10.0.0.1').packed}[state]
            value[14]=encode(sta,STA);raw=sanitize_echo(encode(value,TOP),STAMP,'10.0.0.1','192.168.1.2')
            actual=fields(fields(raw,TOP,4096)[14],STA,1024)
            self.assertEqual(actual[2],sta[2]);self.assertEqual(actual[4],sta[4])
            if state=='absent':self.assertNotIn(5,actual)
            else:self.assertEqual(actual[5],ipaddress.ip_address('192.168.1.2').packed if state=='real' else sta[5])
            self.assertNotIn(3,actual);self.assertNotIn(SECRET,raw)
        for state in ('absent','empty','zero','real'):
            value=fields(echo(),TOP,4096);ap=fields(value[13],AP,1024);sta=fields(value[14],STA,1024)
            if state=='absent':ap.pop(7)
            else:ap[7]={'empty':b'','zero':b'\0'*4,'real':ipaddress.ip_address('10.0.0.1').packed}[state]
            sta[5]=ipaddress.ip_address('10.0.0.1').packed
            value[13]=encode(ap,AP);value[14]=encode(sta,STA)
            result=fields(sanitize_echo(encode(value,TOP),STAMP,'10.0.0.1','192.168.1.2'),TOP,4096);actual=fields(result[13],AP,1024)
            if state=='absent':self.assertNotIn(7,actual)
            else:self.assertEqual(actual[7],ipaddress.ip_address('192.168.1.2').packed if state=='real' else ap[7])
            self.assertEqual(actual[2],ap[2]);self.assertEqual(actual[4],ap[4])
            self.assertEqual(fields(result[14],STA,1024)[5],ipaddress.ip_address('192.168.1.2').packed)
        # An addressless/zero Echo cannot prove the configured target.
        ap[7]=b'\0'*4;sta[5]=b'\0'*4;value[13]=encode(ap,AP);value[14]=encode(sta,STA)
        with self.assertRaises(EchoRejected):sanitize_echo(encode(value,TOP),STAMP,'10.0.0.1','192.168.1.2')

    def test_relay_ping_filters_bounded_canonical_but_automatic_observer_stays_generic(self):
        field=lambda number,data:varint((number<<3)|2)+varint(len(data))+data
        valid=ping(STAMP)+field(4,SECRET)+field(4,b'')+field(5,b'opaque-mute')
        self.assertEqual(relay_ping_timestamp(valid),STAMP);self.assertIsNone(ping_timestamp(valid))
        self.assertEqual(relay_ping_timestamp(ping(STAMP)),STAMP)
        self.assertEqual(relay_ping_timestamp(ping(STAMP)+field(4,b'x')*8+field(5,b'x')*8),STAMP)
        for raw in (ping(STAMP)+field(4,b'x'*65),ping(STAMP)+field(4,b'')*9,
                    ping(STAMP)+field(5,b'')*9,ping(STAMP)+field(5,b'x')+field(4,b'x'),
                    ping(STAMP)+field(6,b'x'),ping(STAMP)+b'\x20\x01',ping(STAMP)+b'\x22\x81\x00x',
                    ping(STAMP)+field(4,b'x'*64)*8,valid[:-1],valid+b'\x10\x01',b'\x08\x02'+valid[2:]):
            self.assertIsNone(relay_ping_timestamp(raw))

    def test_unknown_scheme_schema_duplicate_origin_target_nonce_or_large_never_forwarded(self):
        for raw in (echo(b'123.45'),echo(b'1.234'),echo(b'1.a'),echo(stamp=1),echo(b'wss'),echo(b'wss://'),echo(b'ws://10.0.0.1'),echo(b'unknown'),
                    echo()+b'\x08\x02',echo()+b'\x78\x01',echo()[:-1],b'x'*4097,
                    echo().replace(b'txtl',b'nope')):
            with self.assertRaises(EchoRejected):sanitize_echo(raw,STAMP,'10.0.0.1','192.168.1.2')
        with self.assertRaises(EchoRejected):sanitize_echo(echo(),STAMP,'10.9.0.1','192.168.1.2')
        value=fields(echo(),TOP,4096);value[13]=encode({7:b'bad'},AP)
        with self.assertRaises(EchoRejected):sanitize_echo(encode(value,TOP),STAMP,'10.0.0.1','192.168.1.2')


class ProxyTests(Fixture):
    def test_dwarf_fixed_ports_both_ingresses_and_conflicting_explicit_plan_rejected(self):
        net=LinuxNetwork();addresses={'home':'192.168.1.2/24','ap':'192.168.77.1/24','scope':'10.0.0.2/24'}
        route={'profile_id':'d','device':{'name':'scope'},'target':'10.0.0.1','dwarf_proxy':True}
        with patch.object(net,'address',side_effect=lambda d:addresses[d['name']]),patch.object(net,'configure_activity'),patch('wifi_bridge.network.run') as run:
            net.firewall(home={'name':'home'},setup={'name':'ap'},telescopes=[route])
            script=run.call_args.kwargs['input']
            for address in ('192.168.1.2','192.168.77.1'):
                self.assertIn('ip daddr '+address+' tcp dport { 9900, 8082, 554 } dnat ip to 10.0.0.1',script)
            with self.assertRaises(NetworkError):net.firewall(home={'name':'home'},telescopes=[dict(route,legacy_ports=True)])
            with self.assertRaises(NetworkError):net.firewall(home={'name':'home'},telescopes=[route,dict(route,dwarf_proxy=False,legacy_ports=True)])

    def test_existing_vespera_owner_not_stolen_and_dwarf_owner_preserved_in_reverse(self):
        self.net.radios=3;self.devices=self.net.devices();self.configured();self.controller.cycle(0)
        api=MobileAPI(self.controller)
        api.update('telescopes',{'id':'d','name':'Dwarf','kind':'dwarf','ssid':'Fictional DWARF','password':'synthetic-password',
            'mac':self.devices[2]['mac'],'enabled':True,'target_ip':'10.8.0.1'},create=True)
        self.store.config['telescopes'][0]['owner_locked']=True
        self.controller.cycle(10);rows={r['id']:r for r in self.controller.state['telescopes']}
        self.assertTrue(rows['primary']['legacy_ports']);self.assertTrue(rows['d']['relay_ready']);self.assertFalse(rows['d']['dwarf_proxy'])
        self.assertEqual(rows['d']['native_proxy_reason'],'NATIVE_PROXY_CONFLICT')
        self.store.config['telescopes'][0]['owner_locked']=False;api.action('primary','disconnect');self.controller.cycle(20)
        self.assertEqual(self.controller.proxy_owners['dwarf_proxy'],'d')
        api.action('primary','connect');self.controller.cycle(40);rows={r['id']:r for r in self.controller.state['telescopes']}
        self.assertTrue(rows['d']['dwarf_proxy']);self.assertFalse(rows['primary']['legacy_ports'])
        proxy=self.controller.operations.endpoint(self.store.config['telescopes'][1],rows['d'],'')
        self.assertEqual(proxy['proxy']['protocol'],'dwarf');self.assertEqual(proxy['proxy']['ports'],[9900,8082,554])
