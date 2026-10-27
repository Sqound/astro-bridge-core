"""Foreground NetworkManager identity from bounded realistic temporary /proc trees."""
from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wifi_bridge.network import LinuxNetwork, networkmanager_process_identity, networkmanager_owned_process_identity


class NetworkManagerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.proc=Path(self.temporary.name)/'proc';self.proc.mkdir()
        self.active='/org/freedesktop/NetworkManager/ActiveConnection/7'
        self.link='Connected to 02:00:00:00:00:01 (on wlan0)\n\tSSID: Fictional telescope'

    def process(self,pid='2206',started='1000',comm='NetworkManager',executable='/usr/sbin/NetworkManager',uid=0):
        path=self.proc/pid;path.mkdir(exist_ok=True)
        (path/'comm').write_text(comm+'\n')
        (path/'stat').write_text(pid+' ('+comm+') '+' '.join(['S']+['0']*18+[started]+['0']*8)+'\n')
        (path/'status').write_text('Name:\t'+comm+'\nUid:\t'+('\t'.join([str(uid)]*4))+'\n')
        (path/'cmdline').write_bytes((executable+'\0-n\0').encode())
        (path/'exe').unlink(missing_ok=True);(path/'exe').symlink_to(executable)
        return path

    def association(self,responses=None):
        identify=lambda:networkmanager_process_identity(self.proc)
        with patch('wifi_bridge.network.networkmanager_process_identity',side_effect=identify),patch('wifi_bridge.network.run',side_effect=responses or [self.active,self.link,self.active]):
            return LinuxNetwork(self.temporary.name).association_identity({'name':'wlan0'})

    def test_real_foreground_daemon_without_pidfile_ignores_supervisor_pid(self):
        self.process('2205',comm='supervise-daemo',executable='/sbin/supervise-daemon')
        self.process()
        run=Path(self.temporary.name)/'run';run.mkdir();(run/'supervise-networkmanager.pid').write_text('2205\n')
        self.assertEqual(networkmanager_process_identity(self.proc),'2206:1000')
        token=self.association();self.assertEqual(token,'2206:1000:'+self.active+':02:00:00:00:00:01')
        self.assertFalse((run/'NetworkManager/NetworkManager.pid').exists())

    def test_supported_daemon_executable_paths_and_same_pid_reuse_changes_token(self):
        for executable in ['/usr/sbin/NetworkManager','/sbin/NetworkManager']:
            with self.subTest(executable=executable):
                self.process(executable=executable);before=self.association()
                self.process(started='2000',executable=executable);after=self.association()
                self.assertIsNotNone(before);self.assertIsNotNone(after);self.assertNotEqual(before,after)

    def test_multiple_real_daemons_are_unknown_even_with_a_plausible_pidfile(self):
        self.process();self.process('3100')
        (self.proc.parent/'NetworkManager.pid').write_text('2206\n')
        self.assertIsNone(networkmanager_process_identity(self.proc))
        with patch('wifi_bridge.network.networkmanager_process_identity',return_value=None),patch('wifi_bridge.network.run') as run:
            self.assertIsNone(LinuxNetwork(self.temporary.name).association_identity({'name':'wlan0'}))
        run.assert_not_called()

    def test_wrong_executable_nonroot_malformed_stat_and_missing_metadata_fail_closed(self):
        for change in ['executable','uid','stat','missing','oversize']:
            with self.subTest(change=change):
                path=self.process()
                if change=='executable':(path/'exe').unlink();(path/'exe').symlink_to('/sbin/supervise-daemon')
                elif change=='uid':(path/'status').write_text('Uid:\t1000\t1000\t1000\t1000\n')
                elif change=='stat':(path/'stat').write_text('2206 (NetworkManager) S malformed\n')
                elif change=='missing':(path/'exe').unlink()
                else:(path/'status').write_text('Name: NetworkManager\n'+'x'*16385)
                self.assertIsNone(networkmanager_process_identity(self.proc))

    def test_process_reuse_or_restart_during_connection_observation_is_unknown(self):
        for changed_pid in ['2206','3200']:
            with self.subTest(changed_pid=changed_pid):
                self.process();calls=[]
                def run(args,**kwargs):
                    calls.append(args)
                    if len(calls)==1:
                        if changed_pid!='2206':
                            old=self.proc/'2206'
                            for file in old.iterdir():file.unlink()
                            old.rmdir()
                        self.process(changed_pid,started='2000')
                        return self.active
                    return self.link if len(calls)==2 else self.active
                identify=lambda:networkmanager_process_identity(self.proc)
                with patch('wifi_bridge.network.networkmanager_process_identity',side_effect=identify),patch('wifi_bridge.network.run',side_effect=run):
                    self.assertIsNone(LinuxNetwork(self.temporary.name).association_identity({'name':'wlan0'}))
                if changed_pid!='2206':
                    for file in (self.proc/changed_pid).iterdir():file.unlink()
                    (self.proc/changed_pid).rmdir()

    def test_changed_active_object_or_unavailable_actual_bssid_is_unknown(self):
        self.process()
        self.assertIsNone(self.association([self.active,self.link,self.active+'1']))
        self.assertIsNone(self.association([self.active,'Not connected.']))
        self.assertIsNone(self.association(['not-an-active-object']))

    def test_truncated_process_inventory_is_unknown_not_a_unique_daemon(self):
        self.process()
        class Entry:
            def __init__(self,name):self.name=name
        # Include a valid daemon, then exceed the scan bound: uniqueness is unproven.
        entries=[Entry('2206')]+[Entry('unrelated')]*4096
        with patch('wifi_bridge.network.os.scandir',return_value=nullcontext(iter(entries))):
            self.assertIsNone(networkmanager_process_identity(self.proc))

    def test_daemon_pid_reused_while_reading_metadata_is_unknown(self):
        self.process();original=Path.open;reads=[0]
        def changing(path,*args,**kwargs):
            if path==self.proc/'2206'/'stat':
                reads[0]+=1
                if reads[0]==2:self.process(started='2000')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',changing):self.assertIsNone(networkmanager_process_identity(self.proc))

    def fast(self,responses=None,owner_after=':1.5'):
        device_reply='method return\n   object path "/org/freedesktop/NetworkManager/Devices/3"\n'
        active_reply='method return\n   variant object path "'+self.active+'"\n'
        replies=['string ":1.5"','uint32 2206']+(responses or [device_reply,active_reply,self.link,active_reply])+['string "'+owner_after+'"']
        identify=lambda pid:networkmanager_owned_process_identity(pid,self.proc)
        with patch('wifi_bridge.network.networkmanager_owned_process_identity',side_effect=identify),patch('wifi_bridge.network.networkmanager_process_identity') as scan,patch('wifi_bridge.activity.bounded_read',side_effect=replies) as read,patch('wifi_bridge.network.run') as slow:
            result=LinuxNetwork(self.temporary.name).fast_association_identity({'name':'wlan0'},timeout=1)
        slow.assert_not_called();scan.assert_not_called()
        for call in read.call_args_list:
            self.assertEqual(call.kwargs['limit'],4096);self.assertLessEqual(call.kwargs['timeout'],1)
        return result,read

    def test_fast_dbus_returns_same_real_process_active_object_and_bssid_token(self):
        self.process();slow=self.association();fast,read=self.fast()
        self.assertEqual(fast,slow);self.assertEqual(read.call_count,7)
        self.assertEqual(read.call_args_list[0].args[0][-2:],['org.freedesktop.DBus.GetNameOwner','string:org.freedesktop.NetworkManager'])
        self.assertEqual(read.call_args_list[1].args[0][-2:],['org.freedesktop.DBus.GetConnectionUnixProcessID','string::1.5'])
        self.assertEqual(read.call_args_list[2].args[0],['dbus-send','--system','--print-reply','--dest=:1.5',
            '/org/freedesktop/NetworkManager','org.freedesktop.NetworkManager.GetDeviceByIpIface','string:wlan0'])
        self.assertIn('string:ActiveConnection',read.call_args_list[-2].args[0])

    def test_fast_untrusted_process_ambiguous_paths_changed_active_and_missing_bssid_unknown(self):
        self.process(uid=1000);value,read=self.fast();self.assertIsNone(value);self.assertEqual(read.call_count,2)
        self.process();device='object path "/org/freedesktop/NetworkManager/Devices/3"';active='object path "'+self.active+'"'
        for replies in ([device+'\n'+device],[device.replace('/3','/bad')],[device,'object path "/"'],
                        [device,active,'Not connected.'],[device,active,self.link,active.replace('/7','/8')],
                        [OSError('PRIVATE_ERROR_CANARY')],[ValueError('COLLECTION_SATURATED')]):
            result,_=self.fast(replies);self.assertIsNone(result)
        self.assertIsNone(self.fast(owner_after=':1.6')[0])

    def test_fast_process_restart_during_dbus_collection_cannot_publish_identity(self):
        self.process();calls=[0]
        replies=['string ":1.5"','uint32 2206','object path "/org/freedesktop/NetworkManager/Devices/3"',
            'object path "'+self.active+'"',self.link,'object path "'+self.active+'"','string ":1.5"']
        def read(args,**kwargs):
            calls[0]+=1
            if calls[0]==6:self.process(started='2000')
            return replies[calls[0]-1]
        with patch('wifi_bridge.network.networkmanager_owned_process_identity',side_effect=lambda pid:networkmanager_owned_process_identity(pid,self.proc)),patch('wifi_bridge.activity.bounded_read',side_effect=read):
            self.assertIsNone(LinuxNetwork(self.temporary.name).fast_association_identity({'name':'wlan0'}))

    def test_fast_explicit_total_deadline_stops_without_extra_commands_or_unbounded_output(self):
        self.process();now=[100.];calls=[]
        def read(args,**kwargs):
            calls.append(args);self.assertLessEqual(kwargs['timeout'],.5)
            now[0]+=.6;return 'string ":1.5"'
        with patch('wifi_bridge.activity.bounded_read',side_effect=read),patch('wifi_bridge.network.time.monotonic',side_effect=lambda:now[0]):
            self.assertIsNone(LinuxNetwork(self.temporary.name).fast_association_identity({'name':'wlan0'},timeout=.5))
        self.assertEqual(len(calls),1)
        from wifi_bridge.activity import bounded_read
        import sys
        with self.assertRaises(ValueError):bounded_read([sys.executable,'-c','print("X"*4097)'],limit=4096,timeout=1)

    def test_bus_owned_process_metadata_pid_reuse_wrong_exe_uid_and_malformed_identity(self):
        path=self.process();self.assertEqual(networkmanager_owned_process_identity('2206',self.proc),'2206:1000')
        original=Path.open;reads=[0]
        def changing(path,*args,**kwargs):
            if path==self.proc/'2206'/'stat':
                reads[0]+=1
                if reads[0]==2:self.process(started='2000')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',changing):self.assertIsNone(networkmanager_owned_process_identity('2206',self.proc))
        for change in ('uid','exe','stat','missing','invalid_pid'):
            self.process()
            if change=='uid':(path/'status').write_text('Uid: 1000 1000 1000 1000\n')
            if change=='exe':(path/'exe').unlink();(path/'exe').symlink_to('/sbin/supervise-daemon')
            if change=='stat':(path/'stat').write_text('malformed')
            if change=='missing':(path/'comm').unlink()
            self.assertIsNone(networkmanager_owned_process_identity('../2206' if change=='invalid_pid' else '2206',self.proc))
