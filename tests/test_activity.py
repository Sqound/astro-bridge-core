"""Counter/catalog/clock processing; Linux packet proof is in the namespace runner."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from wifi_bridge.activity import ForwardActivity, PREFIX, bounded_read, consultation_delta

BOOT='11111111-1111-1111-1111-111111111111'
OTHER='22222222-2222-2222-2222-222222222222'


class ActivityTests(unittest.TestCase):
    def test_deployment_supplied_conntrack_is_version_checked_without_replacing_host_binary(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            executable=Path(folder)/'conntrack'
            executable.write_text('#!/bin/sh\nprintf "conntrack v1.4.9\\n"\n');executable.chmod(0o700)
            with patch.dict(os.environ,{'ASTRO_BRIDGE_CONNTRACK':str(executable)}):
                self.assertTrue(ForwardActivity(folder,boot_id=BOOT).supported)
            with patch.dict(os.environ,{'ASTRO_BRIDGE_CONNTRACK':'relative-tool'}):
                with self.assertRaises(ValueError):ForwardActivity(folder,boot_id=BOOT)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=[100.];self.table_handle=10;self.packets=0;self.conflicts=0;self.raw='';self.collection_failed=False
        self.a=ForwardActivity(self.temp.name,boot_id=BOOT,clock=lambda:self.now[0],reader=self.read)
        self.a.supported=True
        # A different kernel boot with a valid prior catalogue establishes the
        # explicit bootstrap boundary. It is not a claim about this host's kernel.
        self.a=ForwardActivity(self.temp.name,boot_id=OTHER,clock=lambda:self.now[0],reader=self.read);self.a.supported=True
        self.mark=self.attach('active-1');self.a.current={'one':self.mark}

    def attach(self,token):return self.a.attach('one',token,'10.0.0.1','10.0.0.2','192.168.42.1','192.168.42.0/24',True,'home0','scope0')
    def table(self):
        return {'nftables':[{'table':{'family':'ip','name':'wifi_bridge_activity','handle':self.table_handle}}]+[
            {'counter':{'family':'ip','table':'wifi_bridge_activity','name':'ab_'+str(mark)+'_'+direction,'packets':self.conflicts if direction=='conflict' else self.packets,'bytes':self.packets*100}}
            for mark in self.a.catalog['records'] for direction in ('out','in','conflict')]}
    def read(self,args,**kwargs):
        if self.collection_failed:raise ValueError('failure')
        return json.dumps(self.table()) if args[0]=='nft' else self.raw
    def sample(self,seconds=5):self.now[0]+=seconds;self.a.sample();return self.a.status()['profiles']['one']
    def session(self,mark=None,src='192.168.42.2',dst='192.168.42.1'):
        return 'ipv4 2 tcp 6 431999 ESTABLISHED src='+src+' dst='+dst+' sport=45678 dport=8082 src=10.0.0.1 dst=10.0.0.2 sport=8082 dport=45678 [ASSURED] mark='+str(mark or self.mark)+' use=1\n'

    def test_silent_established_session_does_not_prevent_monotonic_quiet(self):
        self.sample();self.raw=self.session();value=self.sample();self.assertEqual(value['state'],'unknown');self.assertEqual(value['tcp_established'],1)
        for _ in range(30):value=self.sample()
        self.assertEqual(value['state'],'quiet');self.assertEqual(value['consultation'],'none');self.assertTrue(value['raw_active'])
        self.raw='';self.packets+=1;self.assertEqual(self.sample()['state'],'active')
        for _ in range(23):self.assertNotEqual(self.sample()['state'],'quiet')
        self.assertEqual(self.sample()['state'],'quiet');self.assertEqual(self.a.status()['profiles']['one']['cooldown_remaining'],0)
        self.now[0]+=16;self.assertEqual(self.a.status()['profiles']['one']['reason'],'SAMPLE_STALE');self.assertNotEqual(self.sample()['state'],'quiet')

    def test_control_metadata_deltas_release_but_mixed_and_large_transfers_protect(self):
        self.a.attach('one','active-1','10.0.0.1','10.0.0.2','192.168.42.1','192.168.42.0/24',True,'home0','scope0',kind='vespera')
        control=[0,0]
        original=self.read
        def reader(args,**kwargs):
            if args[0]!='nft':return self.raw
            table=self.table()
            table['nftables'] += [{'counter':{'family':'ip','table':'wifi_bridge_activity','name':'ab_'+str(self.mark)+'_control_'+direction,'packets':control[0],'bytes':control[1]}} for direction in ('out','in')]
            return json.dumps(table)
        self.a.reader=reader;self.a.configure([],lambda script:None);self.sample()
        self.raw=self.session()
        for _ in range(25):
            self.packets+=1;control[0]+=1;control[1]+=100;value=self.sample()
        self.assertEqual(value['state'],'quiet');self.assertEqual(value['consultation'],'background_control')
        self.assertTrue(value['raw_active']);self.assertEqual(value['interval_bytes'],200)
        self.packets+=1;value=self.sample()
        self.assertEqual(value['state'],'active');self.assertEqual(value['consultation'],'unclassified')
        self.packets+=1000;value=self.sample()
        self.assertEqual(value['consultation'],'probable_content');self.assertFalse(value['calibrated'])
        control[0]=0;control[1]=0;self.packets+=1
        self.assertEqual(self.sample()['state'],'unknown')
        for _ in range(26):value=self.sample()
        self.assertEqual(value['state'],'quiet') # Fresh continuous metadata recovers; silence alone is not a counter reset.

    def test_table_identity_reset_missing_data_and_conflicts_stay_unknown_across_restart(self):
        self.sample();self.conflicts=1;self.assertEqual(self.sample()['reason'],'MARK_COLLISION')
        # Normal main-table changes do not replace activity counter objects.
        self.a.replaced();self.assertEqual(self.sample()['state'],'unknown')
        self.table_handle+=1;self.conflicts=0;self.sample();self.assertTrue(self.a.catalog['coverage_lost'])
        restarted=ForwardActivity(self.temp.name,boot_id=OTHER,clock=lambda:self.now[0],reader=self.read);restarted.supported=True
        self.a=restarted;self.mark=self.attach('active-1')
        for _ in range(30):self.assertEqual(self.sample()['state'],'unknown')
        self.collection_failed=True;self.sample();self.assertTrue(self.a.catalog['coverage_lost'])

    def test_verified_unchanged_network_plan_keeps_quiet_but_changed_identity_cannot_reuse_it(self):
        import ipaddress
        from wifi_bridge.network import LinuxNetwork
        net=LinuxNetwork(self.temp.name);net.telemetry=self.a
        scope={'name':'scope0'};home={'name':'home0'};token=['active-1']
        entries=[{'profile_id':'one','device':scope,'target':'10.0.0.1','seestar_proxy':True,'kind':'seestar'}]
        with patch.object(net,'address',return_value='10.0.0.2/24'),patch.object(net,'association_identity',side_effect=lambda device:token[0]),patch('wifi_bridge.network.run'):
            net.configure_activity(entries,home,ipaddress.ip_interface('192.168.42.1/24'))
            for _ in range(27):value=self.sample()
            self.assertEqual(value['state'],'quiet')
            before=self.a.generation
            net.configure_activity(entries,home,ipaddress.ip_interface('192.168.42.1/24'))
            self.assertEqual(net.activity_status()['profiles']['one']['state'],'quiet')
            self.assertEqual(self.a.generation,before)
            token[0]='reconnected-same-ip-and-radio'
            # Selection rechecks the physical identity even before replanning.
            self.assertEqual(net.selection_activity({'id':'one','target_ip':'10.0.0.1'},scope,home)['state'],'unknown')
            net.configure_activity(entries,home,ipaddress.ip_interface('192.168.42.1/24'))
            self.assertNotEqual(net.activity_status()['profiles']['one']['state'],'quiet')
            token[0]=None
            net.configure_activity(entries,home,ipaddress.ip_interface('192.168.42.1/24'))
            for _ in range(27):value=self.sample()
            self.assertEqual(value['state'],'unknown')

    def test_long_reconcile_plus_sleep_does_not_create_sampling_gaps(self):
        from types import SimpleNamespace
        from threading import RLock
        from wifi_bridge.controller import Controller
        controller=Controller.__new__(Controller)
        controller.lock=RLock()
        controller.store=SimpleNamespace(config={'auto_policy':{}})
        controller.net=SimpleNamespace(sample_activity=lambda **kw:self.a.sample(quiet_seconds=kw['policy']['quiet_seconds'],cooldown_seconds=kw['policy']['cooldown_seconds']))
        controller.operations=SimpleNamespace(gate_cycle=lambda:None)
        controller._observe_cycle=lambda **kw:None
        controller._cycle=lambda now:self.now.__setitem__(0,self.now[0]+11)
        values=[]
        for _ in range(30):
            controller.cycle();values.append(self.a.status()['profiles']['one']);self.now[0]+=5
        self.assertEqual(values[-1]['state'],'quiet')
        self.assertTrue(all(v['interval_seconds']<=11 for v in values[1:]))

    def test_seestar_background_channels_remain_bounded_and_preview_is_not_whitelisted(self):
        from wifi_bridge.activity import CONTROL_PORTS
        self.assertEqual(CONTROL_PORTS['seestar'],(4700,4800,4801,4804))
        previous={'packets':100,'bytes':10000,'control':(100,10000)}
        self.assertEqual(consultation_delta(previous,118,11531,(118,11531),5,'seestar'),('background_control',False))
        self.assertEqual(consultation_delta(previous,118,11531,(100,10000),5,'seestar'),('unclassified',True))
        self.assertEqual(consultation_delta(previous,200,100000,(200,100000),5,'seestar'),('probable_content',True))
        self.assertEqual(consultation_delta(previous,118,11531,(118,11531),16,'seestar'),('unknown',True))

    def test_cross_family_connection_attempts_remain_metered_and_cannot_whitelist_data(self):
        record=self.a.catalog['records'][str(self.mark)]
        self.a.kinds[self.mark]='seestar'
        out=self.a.control_rule('path',self.mark,'out',record)
        reply=self.a.control_rule('path',self.mark,'in',record)
        self.assertEqual(len(out),2)
        self.assertIn('ct original proto-dst { 8082, 8083 }',out[1])
        self.assertIn('ip hdrlength 5 ip frag-off & 0x3fff == 0',out[1])
        self.assertIn('== (syn)',out[1]);self.assertIn('== (rst | ack)',reply[1])
        self.assertIn('64 . 11',out[1]);self.assertIn('80 . 15',out[1])
        self.assertTrue(out[1].endswith('_control_out'))
        self.a.kinds[self.mark]='vespera'
        self.assertEqual(len(self.a.control_rule('path',self.mark,'out',record)),1)
        previous={'packets':0,'bytes':0,'control':(0,0)}
        self.assertEqual(consultation_delta(previous,2,104,(2,104),5,'seestar'),('background_control',False))
        self.assertEqual(consultation_delta(previous,200,10400,(200,10400),5,'seestar'),('unclassified',True))

    def test_first_install_inherited_coverage_never_becomes_quiet_by_waiting_or_app_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            fresh=ForwardActivity(directory,boot_id=BOOT);self.assertFalse(fresh.bootstrap)
            fresh.attach('one','inherited','10.0.0.1','10.0.0.2','192.168.42.1','192.168.42.0/24',False,'home0','scope0')
            again=ForwardActivity(directory,boot_id=BOOT);self.assertFalse(again.bootstrap);self.assertEqual(again.catalog['next'],2)
            reboot=ForwardActivity(directory,boot_id=OTHER);self.assertTrue(reboot.bootstrap)

    def test_owned_marks_only_strict_tuple_attribution_and_prior_generation_sessions(self):
        counts,stale,collision=self.a.parse_sessions(self.session());self.assertEqual(counts,{'one':1});self.assertFalse(collision)
        _,_,collision=self.a.parse_sessions(self.session(src='10.0.0.2'));self.assertTrue(collision) # LOCAL bridge probe cannot become activity.
        _,_,collision=self.a.parse_sessions(self.session(mark=PREFIX+100));self.assertTrue(collision)
        old=self.mark;self.mark=self.attach('active-2');self.raw=self.session(mark=old);value=self.sample();self.assertEqual(value['reason'],'STALE_ASSOCIATION_SESSIONS')
        self.assertGreater(self.mark,old)

    def test_catalog_corrupt_shapes_highwater_and_no_reuse(self):
        for bad in (None,[],{'version':1},dict(self.a.catalog,next=True)):
            Path(self.temp.name,'activity-catalog.json').write_text(json.dumps(bad))
            value=ForwardActivity(self.temp.name,boot_id=OTHER);self.assertFalse(value.bootstrap);self.assertIsNotNone(value.catalog_error)
        self.a.catalog['next']=65536;self.assertIsNone(self.attach('exhausted'));self.assertEqual(self.a.catalog_error,'CATALOG_EXHAUSTED')

    def test_counter_objects_survive_classifier_changes_and_foreign_marks_are_not_overwritten(self):
        scripts=[];self.a.configure([],scripts.append);first=scripts[-1]
        self.assertIn('ct mark 0 ct mark set',first);self.assertNotIn('delete table',first);self.assertNotIn('reset counter',first)
        self.mark=self.attach('new-association');self.a.configure([],scripts.append);next_script=scripts[-1]
        self.assertIn('flush chain ip wifi_bridge_activity forward',next_script);self.assertNotIn('delete counter',next_script)
        self.assertIn('ab_'+str(PREFIX+1)+'_out',next_script) # Retired classifier retained conservatively.
        self.assertNotIn('ct mark set 0',next_script)

    def test_process_collection_is_bounded_before_capture_fifo_timeout_or_large_output(self):
        import sys
        self.assertEqual(bounded_read([sys.executable,'-c','print("public")']).strip(),'public')
        with self.assertRaises(ValueError):bounded_read([sys.executable,'-c','print("x"*1000000)'],limit=1024)
        with self.assertRaises(ValueError):bounded_read([sys.executable,'-c','import time;time.sleep(5)'],timeout=.05)

    def test_named_collection_avoids_rule_dump_and_keeps_bounded_identity_checks(self):
        calls=[]
        def reader(args,**kwargs):
            calls.append((args,kwargs));return self.read(args,**kwargs)
        self.a.reader=reader;self.sample()
        nft=[(args,options) for args,options in calls if args[0]=='nft']
        self.assertEqual([args[4] for args,_ in nft],['tables','counters','tables'])
        self.assertEqual([options['limit'] for _,options in nft],[65536,262144,65536])
        self.assertTrue(all('timeout' not in options for _,options in nft))

    def test_table_replacement_between_identity_checks_latches_coverage(self):
        self.sample();count=[0]
        def reader(args,**kwargs):
            if args[0]=='nft' and args[4]=='tables':
                count[0]+=1
                if count[0]==2:self.table_handle+=1
            return self.read(args,**kwargs)
        self.a.reader=reader
        self.assertEqual(self.sample()['state'],'unknown');self.assertTrue(self.a.catalog['coverage_lost'])
        self.a.reader=self.read
        for _ in range(30):self.assertEqual(self.sample()['state'],'unknown')
        restarted=ForwardActivity(self.temp.name,boot_id=OTHER,clock=lambda:self.now[0],reader=self.read)
        self.assertTrue(restarted.catalog['coverage_lost'])

    def test_invalid_or_duplicate_named_objects_never_grant_quiet(self):
        original=self.read
        cases=('duplicate','foreign_family','foreign_table','negative','boolean','malformed')
        for case in cases:
            def reader(args,**kwargs):
                if args[0]!='nft' or args[4]!='counters':return original(args,**kwargs)
                table=self.table();counter=table['nftables'][1]['counter']
                if case=='duplicate':table['nftables'].append({'counter':dict(counter)})
                elif case=='foreign_family':counter['family']='ip6'
                elif case=='foreign_table':counter['table']='unrelated'
                elif case=='negative':counter['packets']=-1
                elif case=='boolean':counter['bytes']=True
                else:table['nftables'][1]['counter']=None
                return json.dumps(table)
            with self.subTest(case=case):
                self.a.reader=reader
                value=self.sample();self.assertEqual(value['state'],'unknown');self.assertIsNone(value['quiet_seconds'])
        self.a.reader=original;self.sample()
        for _ in range(25):value=self.sample()
        self.assertEqual(value['state'],'quiet')

    def test_missing_ambiguous_or_foreign_table_identity_is_unknown(self):
        for case in ('missing','duplicate','foreign','malformed'):
            def reader(args,**kwargs):
                if args[0]!='nft' or args[4]!='tables':return self.read(args,**kwargs)
                table=self.table()
                if case=='missing':table['nftables']=[]
                elif case=='duplicate':table['nftables'].append(dict(table['nftables'][0]))
                elif case=='foreign':table['nftables'][0]['table']['family']='ip6'
                else:table['nftables'][0]['table']=None
                return json.dumps(table)
            with self.subTest(case=case):
                self.a.reader=reader
                self.assertEqual(self.sample()['state'],'unknown')


    def test_transient_collection_failure_recovers_but_direction_reset_and_gap_are_sticky(self):
        self.sample();self.collection_failed=True;self.sample();self.assertFalse(self.a.catalog['coverage_lost'])
        self.collection_failed=False;self.sample()
        for _ in range(24):value=self.sample()
        self.assertEqual(value['state'],'quiet')
        self.conflicts=1;self.sample();self.assertTrue(self.a.catalog['records'][str(self.mark)]['gap_latched'])
        self.conflicts=0;self.sample();self.assertTrue(self.a.catalog['coverage_lost'])
        self.assertEqual(self.a.status()['profiles']['one']['reason'],'MARK_COLLISION')

    def test_association_token_includes_actual_bssid_with_same_nm_active_path(self):
        from unittest.mock import patch
        from wifi_bridge.network import LinuxNetwork
        net=LinuxNetwork(self.temp.name)
        with patch('wifi_bridge.network.networkmanager_process_identity',return_value='99:1000'),patch('wifi_bridge.network.run',side_effect=[
            '/org/freedesktop/NetworkManager/ActiveConnection/7','Connected to 02:00:00:00:00:01 (on wlan0)',
            '/org/freedesktop/NetworkManager/ActiveConnection/7',
            '/org/freedesktop/NetworkManager/ActiveConnection/7','Connected to 02:00:00:00:00:02 (on wlan0)',
            '/org/freedesktop/NetworkManager/ActiveConnection/7']):
            first=net.association_identity({'name':'wlan0'});second=net.association_identity({'name':'wlan0'})
        self.assertNotEqual(first,second);self.assertIn('02:00:00:00:00:01',first)


    def test_proxy_ownership_can_be_gained_and_lost_without_losing_old_session_protection(self):
        record=self.a.catalog['records'][str(self.mark)];record['proxy']=False;self.a.persist()
        mark=self.attach('active-1');self.assertEqual(mark,self.mark);self.assertTrue(record['proxy'])
        self.a.attach('one','active-1','10.0.0.1','10.0.0.2','192.168.42.1','192.168.42.0/24',False,'home0','scope0')
        self.assertTrue(record['proxy']);self.assertEqual(self.a.parse_sessions(self.session())[0],{'one':1})

    def test_public_status_is_immutable_during_concurrent_plan_and_sample_updates(self):
        import threading
        failures=[]
        def write():
            try:
                for i in range(60):
                    self.a.plan();self.attach('active-'+str(i%3));self.a.replaced();self.now[0]+=5;self.a.sample()
            except BaseException as error:failures.append(error)
        thread=threading.Thread(target=write);thread.start()
        while thread.is_alive():
            snapshot=self.a.status()
            for value in snapshot['profiles'].values():self.assertIsInstance(value['generation'],int)
            snapshot['profiles'].clear()
        thread.join();self.assertEqual(failures,[]);self.assertTrue(self.a.status()['profiles'])


    def test_retired_signature_cannot_reuse_mark_and_uncertain_identity_stays_unknown(self):
        first=self.mark;second=self.attach('active-2');again=self.attach('active-1')
        self.assertGreater(second,first);self.assertGreater(again,second)
        self.attach(None);known=self.attach('active-3')
        self.assertFalse(self.a.catalog['records'][str(known)]['coverage'])
        restarted=ForwardActivity(self.temp.name,boot_id=OTHER,clock=lambda:self.now[0],reader=self.read)
        self.assertEqual(restarted.attach('one','active-3','10.0.0.1','10.0.0.2','192.168.42.1','192.168.42.0/24',True,'home0','scope0'),known)
        self.assertFalse(restarted.catalog['records'][str(known)]['coverage'])


    def test_unsampled_retired_foreign_gap_protects_different_profile_with_same_tuple(self):
        old=self.mark
        new=self.a.attach('two','different-profile','10.0.0.1','10.0.0.2','192.168.42.1','192.168.42.0/24',True,'home0','scope0')
        self.a.current={'two':new}
        snapshot=self.table()
        for row in snapshot['nftables']:
            counter=row.get('counter',{})
            if counter.get('name')=='ab_'+str(old)+'_conflict':counter.update(packets=1,bytes=64)
        self.a.reader=lambda args,**kwargs:json.dumps(snapshot) if args[0]=='nft' else ''
        self.a.configure([],lambda script:None);self.a.sample()
        self.assertTrue(self.a.catalog['coverage_lost']);self.assertTrue(self.a.catalog['records'][str(old)]['gap_latched'])
        self.assertEqual(self.a.status()['profiles']['two']['state'],'unknown')
        again=ForwardActivity(self.temp.name,boot_id=OTHER,clock=lambda:self.now[0],reader=self.a.reader);again.supported=True
        again.attach('two','different-profile','10.0.0.1','10.0.0.2','192.168.42.1','192.168.42.0/24',True,'home0','scope0');again.sample()
        self.assertTrue(again.catalog['coverage_lost']);self.assertEqual(again.status()['profiles']['two']['state'],'unknown')

    def test_forced_fresh_collection_and_configurable_quiet_cooldown(self):
        self.a.sample(force=True,quiet_seconds=30,cooldown_seconds=90)
        calls=[];reader=self.a.reader
        self.a.reader=lambda args,**kwargs:(calls.append(args) or reader(args,**kwargs))
        self.now[0]+=1;self.a.sample();self.assertEqual(calls,[])
        self.a.sample(force=True,quiet_seconds=30,cooldown_seconds=90);self.assertTrue(calls)
        for _ in range(6):self.now[0]+=5;self.a.sample(force=True,quiet_seconds=30,cooldown_seconds=90)
        value=self.a.status()['profiles']['one'];self.assertEqual(value['state'],'quiet');self.assertEqual(value['quiet_required_seconds'],30);self.assertGreater(value['cooldown_remaining'],0)
        self.a.catalog['coverage_lost']=True;self.now[0]+=5;self.a.sample(force=True,quiet_seconds=30,cooldown_seconds=90)
        self.assertEqual(self.a.status()['profiles']['one']['state'],'unknown')

    def test_multiple_paths_share_association_and_keep_old_silent_sessions_when_home_leaves(self):
        self.a.configure([],lambda script:None);home=self.mark
        ap=self.a.attach('one','active-1','10.0.0.1','10.0.0.2','192.168.77.1','192.168.77.0/24',True,'ap0','scope0')
        self.a.configure([],lambda script:None)
        self.assertNotEqual(home,ap);self.assertEqual(set(self.a.association_marks('one')),{home,ap})
        self.raw=self.session(mark=home)+self.session(mark=ap,src='192.168.77.12',dst='192.168.77.1')
        value=self.sample();self.assertEqual(value['tcp_established'],2);self.assertNotEqual(value['reason'],'STALE_ASSOCIATION_SESSIONS')
        self.a.plan();self.a.attach('one','active-1','10.0.0.1','10.0.0.2','192.168.77.1','192.168.77.0/24',True,'ap0','scope0')
        self.a.configure([],lambda script:None)
        self.assertEqual(self.sample()['tcp_established'],2);self.assertEqual(self.a.catalog['next'],3)
        self.assertEqual(self.attach('active-1'),home);self.assertEqual(self.a.catalog['next'],3)
        next_mark=self.attach('active-2');self.assertGreater(next_mark,ap);self.assertEqual(self.sample()['reason'],'STALE_ASSOCIATION_SESSIONS')

    def test_v1_path_migration_requires_actual_signature_and_preserves_uncertainty(self):
        data=json.loads(self.a.path.read_text());data['version']=1;data['active']={'one':self.mark}
        for record in data['records'].values():record.pop('group');record.pop('instrument_signature');record['coverage']=False
        self.a.path.write_text(json.dumps(data))
        self.a=ForwardActivity(self.temp.name,boot_id=OTHER,clock=lambda:self.now[0],reader=self.read);self.a.supported=True
        ap=self.a.attach('one','active-1','10.0.0.1','10.0.0.2','192.168.77.1','192.168.77.0/24',True,'ap0','scope0')
        self.assertEqual(set(self.a.association_marks('one')),{self.mark,ap});self.assertFalse(self.a.catalog['records'][str(ap)]['coverage'])
        self.assertEqual(json.loads(self.a.path.read_text())['version'],2)
        self.raw=self.session();self.assertEqual(self.sample()['tcp_established'],1);self.assertEqual(self.a.status()['profiles']['one']['state'],'unknown')

    def test_retired_owned_mark_on_reused_tuple_protects_different_profile(self):
        old=self.mark
        new=self.a.attach('two','different-instrument','10.0.0.1','10.0.0.2','192.168.42.1','192.168.42.0/24',True,'home0','scope0')
        self.a.current={'two':new};self.raw=self.session(mark=old)
        counts,stale,collision=self.a.parse_sessions(self.raw)
        self.assertIn('two',stale);self.assertFalse(collision);self.assertNotIn('two',counts)

    def test_maximum_catalog_shape_reopens_within_private_read_bound(self):
        # Publicly bounded field maxima, including retained paths of one actual
        # instrument association; this does not assert physical traffic delivery.
        self.a.catalog['records']={};self.a.catalog['active']={};self.a.current={}
        for index in range(88):
            mark=self.a.attach('p'*64,'same-real-association','172.255.255.254','172.255.255.253',
                '192.168.255.'+str(index+1),'192.168.255.0/24',True,'h'*15,'s'*15,kind='vespera')
            self.assertIsNotNone(mark)
        self.a.catalog['active']={'p'*64:self.a.association_marks('p'*64)}
        self.a.persist()
        self.assertLess(self.a.path.stat().st_size,65536)
        reopened=ForwardActivity(self.temp.name,boot_id=OTHER,clock=lambda:self.now[0],reader=self.read)
        self.assertIsNone(reopened.catalog_error);self.assertEqual(len(reopened.catalog['records']),88)
        reopened.attach('p'*64,'same-real-association','172.255.255.254','172.255.255.253','192.168.255.1','192.168.255.0/24',True,'h'*15,'s'*15,kind='vespera')
        self.assertEqual(len(reopened.association_marks('p'*64)),88)


class ConsultationTests(unittest.TestCase):
    def classify(self, *, packets=10, bytes_=1000, control=(10,1000), kind='vespera', elapsed=5):
        return consultation_delta({'packets':0,'bytes':0,'control':(0,0)},packets,bytes_,control,elapsed,kind)

    def test_small_known_control_and_silent_sockets_can_release_after_existing_delay(self):
        for kind in ('vespera','dwarf','seestar'):
            self.assertEqual(self.classify(kind=kind),('background_control',False))
        self.assertEqual(self.classify(packets=0,bytes_=0,control=(0,0)),('none',False))

    def test_mixed_channels_unknown_models_and_large_control_bursts_stay_protected(self):
        self.assertEqual(self.classify(control=(1,100)),('unclassified',True))
        self.assertEqual(self.classify(kind='unknown'),('unclassified',True))
        self.assertEqual(self.classify(bytes_=10000,control=(10,10000)),('unclassified',True))
        self.assertEqual(self.classify(packets=100,bytes_=100000,control=(0,0)),('probable_content',True))

    def test_missing_reset_stale_or_inconsistent_counters_cannot_become_quiet(self):
        self.assertEqual(self.classify(control=None),('unclassified',True))
        for args in ({'control':(11,1000)},{'control':(-1,0)},{'control':(10,1001)},{'elapsed':16}):
            self.assertEqual(self.classify(**args),('unknown',True))
        self.assertEqual(consultation_delta(None,0,0,(0,0),5,'vespera'),('unknown',True))
