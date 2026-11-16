"""Single-radio shared mode retains management across owned Ethernet failover."""
from unittest.mock import patch

from test_bridge import Fixture


class SharedWiredRadioTests(Fixture):
    def setUp(self):
        super().setUp()
        self.pi5=self.enterContext(patch('wifi_bridge.cameras.discovery.pi5_gphoto_enabled',return_value=True))
        self.net.radios = 1
        self.devices = self.net.devices()[:1]
        self.radio = self.devices[0]
        self.store.config.update(home={'ssid':'Maison','password':'maison-test-pass','mac':self.radio['mac']},
            interface_roles={self.radio['mac']:'auto'}, connection_mode='hotspot_shared',
            hotspot={'enabled':True,'mac':''}, telescopes=[], telescope={})
        self.wired={'name':'eth0','mac':'02:00:00:20:00:01','kind':'ethernet',
                    'usb':False,'address':'192.168.20.2/24'}
        original=self.net.address
        self.net.address=lambda d,**kw:d['address'] if d.get('kind')=='ethernet' else original(d,**kw)
        self.net.wired_home=lambda:self.wired
        self.controller.update(devices=self.devices)

    def reconcile(self, now):
        return self.controller._management(self.devices,self.store.config,now)

    def test_wired_takes_single_radio_and_failover_respects_ap_clients(self):
        self.net.connect('home',self.radio,self.store.config['home'])
        with patch.object(self.net,'identity') as identity:
            home,ap=self.reconcile(0)
        self.assertEqual(home,self.wired)
        self.assertEqual(ap,self.radio)
        self.assertFalse(self.net.connected('home',self.radio))
        self.assertEqual([d['name'] for d in identity.call_args.args[1]],['eth0','wlan0'])
        self.assertEqual([p['role'] for p in self.controller.state['management']['paths']],['home','ap'])
        self.reconcile(10)
        self.assertTrue(self.net.connected('setup',self.radio))
        self.wired=None
        with patch.object(self.net,'ap_vacant',return_value=False):
            self.reconcile(60)
        self.assertTrue(self.net.connected('setup',self.radio))
        self.assertEqual(self.controller.state['management']['home_retry']['reason'],'HOME_RETRY_DEFERRED_AP_RESERVED')
        with patch.object(self.net,'ap_vacant',return_value=True):
            self.reconcile(61)
        self.assertTrue(self.net.connected('home',self.radio))
        self.assertFalse(self.net.connected('setup',self.radio))
        self.assertEqual(self.controller.state['management']['mode'],'home_only')

    def test_generic_platform_keeps_wifi_association(self):
        self.pi5.return_value=False
        self.net.connect('home',self.radio,self.store.config['home'])
        self.reconcile(0)
        self.assertTrue(self.net.connected('home',self.radio))
        self.assertFalse(self.net.connected('setup',self.radio))

    def test_failed_ap_and_lost_ethernet_retry_wifi(self):
        from wifi_bridge.network import NetworkError
        self.net.connect('home',self.radio,self.store.config['home'])
        def failed(*args):
            self.wired=None
            raise NetworkError('AP failed while Ethernet was lost')
        with patch.object(self.net,'hotspot',side_effect=failed):
            self.reconcile(0)
        self.assertFalse(self.net.connected('home',self.radio))
        self.assertFalse(self.net.connected('setup',self.radio))
        self.reconcile(10)
        self.assertTrue(self.net.connected('home',self.radio))
        self.assertEqual(self.controller.state['management']['mode'],'home_only')

    def test_wired_boot_starts_ap_without_trying_wifi(self):
        with patch.object(self.net,'connect',wraps=self.net.connect) as connect:
            home,ap=self.reconcile(0)
        self.assertEqual(home,self.wired);self.assertEqual(ap,self.radio)
        self.assertNotIn('home',[call.args[0] for call in connect.call_args_list])

    def test_overlapping_ethernet_does_not_displace_wifi(self):
        self.wired['address']='192.168.77.2/24'
        self.net.connect('home',self.radio,self.store.config['home'])
        self.reconcile(0)
        self.assertTrue(self.net.connected('home',self.radio))
        self.assertFalse(self.net.connected('setup',self.radio))

    def test_protection_and_role_reservation_do_not_displace_home(self):
        self.net.connect('home',self.radio,self.store.config['home'])
        with patch.object(self.controller.protection,'protected',return_value=True):
            self.reconcile(0)
        self.assertTrue(self.net.connected('home',self.radio))
        self.store.config['interface_roles'][self.radio['mac']]='home'
        self.reconcile(10)
        self.assertTrue(self.net.connected('home',self.radio))
        self.assertFalse(self.net.connected('setup',self.radio))

    def test_wifi_only_stays_reachable_and_failure_recovers_ap(self):
        self.wired=None
        home,ap=self.reconcile(0)
        self.assertEqual(home,self.radio);self.assertIsNone(ap)
        self.assertEqual(self.controller.state['management']['mode'],'home_only')
        self.net.disconnect('home')
        self.store.config['home']['ssid']='ECHEC'
        home,ap=self.reconcile(50)
        self.assertIsNone(home);self.assertEqual(ap,self.radio)
        self.assertEqual(self.controller.state['management']['mode'],'ap_only')

    def test_two_radios_keep_wifi_and_ap(self):
        self.wired=None
        self.net.radios=2;self.devices=self.net.devices()
        self.net.connect('home',self.radio,self.store.config['home'])
        with patch.object(self.net,'identity') as identity:
            home,ap=self.reconcile(0)
        self.assertEqual(home,self.radio);self.assertEqual(ap,self.devices[1])
        self.assertTrue(self.net.connected('home',self.radio))
        self.assertEqual([d['name'] for d in identity.call_args.args[1]],['wlan0','wlan1'])
