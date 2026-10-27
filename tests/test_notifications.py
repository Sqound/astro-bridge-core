"""Private notification observations with synthetic state, no device/provider calls."""
import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wifi_bridge.config import atomic_json
from wifi_bridge.notifications import NotificationSource, route, private_json
from wifi_bridge.notification_text import TEXT, message

BOOT1='11111111-1111-1111-1111-111111111111'
BOOT2='22222222-2222-2222-2222-222222222222'

class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name);self.now=100
        self.source=NotificationSource(self.folder,clock=lambda:self.now,kernel_boot=BOOT1)
        self.profile=dict(id='test-scope',name='Synthetic scope',kind='dwarf',ssid='DEMO-ONLY',bssid='',target_ip='192.168.88.1')
        self.config={'connection_mode':'private','telescopes':[self.profile]}
        self.state={'notification_cycle_ok':True,'setup_ssid':'AstroBridge-Setup-DEMO','hotspot':{'active':False}}
        self.operations={};self.scans=[]
    def collect(self,updates=None):
        return self.source.collect((copy.deepcopy(self.config),copy.deepcopy(self.state),copy.deepcopy(self.operations),tuple(self.scans)),updates)
    def test_service_restart_is_not_kernel_boot_and_real_boot_is_once(self):
        self.assertEqual(self.collect(),[])
        self.source=NotificationSource(self.folder,clock=lambda:self.now,kernel_boot=BOOT1)
        self.assertEqual(self.collect(),[])
        self.source=NotificationSource(self.folder,clock=lambda:self.now,kernel_boot=BOOT2)
        self.state['notification_cycle_ok']=False;self.assertEqual(self.collect(),[])
        self.state['notification_cycle_ok']=True
        notices=self.collect();self.assertEqual([n['category']for n in notices],['boot'])
        self.assertEqual(notices[0]['values']['hotspot'],'AstroBridge-Setup-DEMO')
        self.assertEqual(self.collect(),[])
        self.assertEqual(self.source.path.stat().st_mode&0o777,0o600)
    def test_successful_distinct_stable_scans_and_two_missing_scans_rearm(self):
        points=(('DEMO-ONLY','02:00:00:00:00:01'),)
        self.scans=[(100,points)];self.assertEqual(self.collect(),[])
        self.assertEqual(self.collect(),[])  # Polling one scan is not evidence.
        self.now=106;self.scans.append((106,points))
        self.assertEqual([n['code']for n in self.collect()],['detected']);self.assertEqual(self.collect(),[])
        self.now=112;self.scans.append((112,()));self.collect()
        self.now=118;self.scans.append((118,()));self.collect()
        self.now=125;self.scans=[(125,points)];self.collect()
        self.now=131;self.scans.append((131,points));self.assertEqual(len(self.collect()),1)
        # Saved BSSID is a selector; a same-name different AP cannot satisfy it.
        self.profile['bssid']='02:00:00:00:00:02'
        self.now=140;self.scans=[(140,points)];self.collect()
        self.now=146;self.scans.append((146,points));self.assertEqual(self.collect(),[])
    def test_clients_require_current_stable_membership_mode_changes_confirmed(self):
        self.collect();self.state['hotspot']={'active':True,'clients_available':True,'clients':[{'ip':'192.168.77.12','hostname':'Synthetic phone','mac':'02:00:00:00:00:12'}]}
        self.assertEqual(self.collect(),[]);self.assertEqual(self.collect()[0]['values'],{'name':'Synthetic phone'})
        self.assertEqual(self.collect(),[])
        self.source=NotificationSource(self.folder,clock=lambda:self.now,kernel_boot=BOOT1)
        self.assertEqual(self.collect(),[])  # Existing membership survives app restart.
        self.config['connection_mode']='hotspot_shared';self.state['notification_cycle_ok']=False;self.assertEqual(self.collect(),[])
        self.state['notification_cycle_ok']=True;self.assertEqual(self.collect()[0]['code'],'mode')
        self.state['hotspot']['clients']=[{'ip':'192.168.77.21','hostname':''},{'ip':'8.8.8.8','hostname':'UNSAFE'}]
        self.collect();notices=self.collect();self.assertEqual(notices[0]['values']['name'],'192.168.77.21')
    def test_measured_duration_same_saved_identity_three_successes_no_model_promise(self):
        for i,seconds in enumerate([12,18,24]):
            self.now+=1;record={'started':self.now,'public':{'profile_id':'test-scope','state':'running'}};self.operations[str(i)]=record
            notice=self.collect()[0];self.assertNotIn('usual',notice['values'])
            self.now+=seconds;record.update(finished=self.now);record['public']['state']='succeeded'
            notice=self.collect()[0];self.assertEqual(notice['values']['seconds'],seconds)
        self.now+=1;self.operations['next']={'started':self.now,'public':{'profile_id':'test-scope','state':'running'}}
        notice=self.collect()[0];self.assertEqual(notice['values']['usual'],18)
        self.profile['ssid']='DIFFERENT-DEMO';self.now+=1;self.operations['different']={'started':self.now,'public':{'profile_id':'test-scope','state':'running'}}
        self.assertNotIn('usual',self.collect()[0]['values'])
        self.now+=5;self.operations['different'].update(finished=self.now);self.operations['different']['public']['state']='failed'
        self.assertEqual(self.collect()[0]['code'],'switch_failed');self.assertEqual(self.collect(),[])
    def test_only_complete_transaction_matching_loaded_version_and_build_announces_install(self):
        self.collect();self.source=NotificationSource(self.folder,clock=lambda:self.now,kernel_boot=BOOT1)
        status={'state':'installed','version':'9.8.7'};tx={'phase':'verifying','version':'9.8.7','build_id':'abc'}
        with patch('wifi_bridge.notifications.__version__','9.8.7'),patch('wifi_bridge.notifications.BUILD_ID','abc'):
            self.assertEqual(self.collect((status,tx)),[])
            tx['phase']='complete';tx['build_id']='wrong';self.assertEqual(self.collect((status,tx)),[])
            tx['build_id']='abc';self.assertEqual(self.collect((status,tx))[0]['code'],'update_installed')
            self.assertEqual(self.collect((status,tx)),[])
        available={'state':'available','version':'9.8.8'}
        self.assertEqual(self.collect((available,{}))[0]['code'],'update_available');self.assertEqual(self.collect((available,{})),[])
    def test_first_feature_install_observes_verifying_then_complete_without_replaying_old_install(self):
        status={'state':'installed','version':'9.8.7'};tx={'phase':'verifying','version':'9.8.7','build_id':'abc'}
        with patch('wifi_bridge.notifications.__version__','9.8.7'),patch('wifi_bridge.notifications.BUILD_ID','abc'):
            self.assertEqual(self.collect((status,tx)),[])
            tx['phase']='complete';self.assertEqual(self.collect((status,tx))[0]['code'],'update_installed')
            self.assertEqual(self.collect((status,tx)),[])
            # A first installation into an already completed old transaction is a baseline.
            self.source.path.unlink();self.source=NotificationSource(self.folder,clock=lambda:self.now,kernel_boot=BOOT1)
            self.assertEqual(self.collect((status,tx)),[])

    def test_bounded_private_journal_corruption_fifo_and_invalid_routes_fail_closed(self):
        for i in range(200):self.source.emit('detected','detected',{'name':'x'*64},route(profile='test-scope'))
        self.assertTrue(self.source.persist());self.assertLess(self.source.path.stat().st_size,65536)
        self.assertEqual(len(NotificationSource(self.folder,kernel_boot=BOOT1).data['events']),128)
        self.source.path.write_text('[]');self.assertFalse(NotificationSource(self.folder,kernel_boot=BOOT1).available)
        for page in ('https://evil.test','../networks','telescopes?profile=evil'):
            with self.assertRaises(ValueError):route(page)
        import os
        fifo=self.folder/'fifo';os.mkfifo(fifo,0o600)
        with self.assertRaises(ValueError):private_json(fifo)
    def test_all_ten_template_keys_and_placeholders_are_complete(self):
        self.assertEqual(len(TEXT),10)
        variables=lambda text:set(re.findall(r'\{[a-z]+\}',text))
        for locale,values in TEXT.items():
            self.assertEqual(set(values),set(TEXT['en']))
            for key,text in values.items():self.assertEqual(variables(text),variables(TEXT['en'][key]),(locale,key))
            self.assertIn('18',message(locale,'switch_started',{'name':'DEMO','usual':18}))
