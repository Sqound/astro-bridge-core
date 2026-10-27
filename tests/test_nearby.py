"""Automatic nearby suggestions use idle radios only; status never scans."""
import copy
from unittest.mock import patch
from test_bridge import Fixture
from wifi_bridge.candidates import CandidateDiscovery

ROW={'ssid':'S50_NEW_SCOPE','bssid':'02:00:00:44:88:01','frequency':2412,'signal':81,'security':'WPA2'}

class NearbyTests(Fixture):
    def idle_bridge(self):
        self.configured();self.store.config['telescopes'][0]['enabled']=False
        self.net.radios=3;self.controller.cycle(0)
        self.now=100;self.controller.candidates.clock=lambda:self.now
        return self.controller.candidates

    def test_automatic_scan_uses_only_spare_radio_and_has_45_second_schedule(self):
        c=self.idle_bridge();before=copy.deepcopy(self.store.config);associations=dict(self.net.associations)
        with patch.object(self.net,'scan',return_value=[ROW]) as scan,patch.object(self.net,'connect') as connect,patch.object(self.net,'disconnect') as disconnect,patch.object(self.net,'scan_station') as station:
            c.background_tick();self.assertEqual(scan.call_count,1)
            scanned=next(d for d in self.net.devices() if d['name']==scan.call_args.args[0]);self.assertNotEqual(scanned['mac'],self.store.config['home']['mac']);self.assertNotEqual(scanned['mac'],self.controller.state['hotspot'].get('mac'))
            self.assertEqual(scan.call_args.kwargs,{'timeout':10});self.assertEqual(c.next_background,145)
            self.now=144;c.background_tick();self.assertEqual(scan.call_count,1)
            self.now=145;c.background_tick();self.assertEqual(scan.call_count,2)
            connect.assert_not_called();disconnect.assert_not_called();station.assert_not_called()
        self.assertEqual(self.store.config,before);self.assertEqual(self.net.associations,associations)
        found=c.nearby()['candidates'];self.assertEqual(len(found),1);self.assertEqual(found[0]['ssid'],ROW['ssid']);self.assertNotIn('password',str(found))

    def test_connected_or_starting_instrument_suppresses_active_scan_and_suggestions(self):
        c=self.idle_bridge()
        with patch.object(self.net,'scan',return_value=[ROW]):c.background_tick()
        self.store.config['telescopes'][0]['enabled']=True;self.now=145
        with patch.object(self.net,'scan') as scan,patch.object(self.net,'cached_scan',return_value=[ROW]),patch.object(self.net,'scan_station') as station:
            c.background_tick();scan.assert_not_called();station.assert_not_called()
        self.assertEqual(c.nearby()['reason'],'CONNECTED');self.assertEqual(c.nearby()['candidates'],[])

    def test_no_free_radio_never_borrows_home_or_ap_and_cached_rows_are_not_nearby(self):
        c=self.idle_bridge()
        with patch.object(self.net,'radio_idle',return_value=False),patch.object(self.net,'scan') as scan,patch.object(self.net,'scan_station') as station,patch.object(self.net,'cached_scan',return_value=[ROW]):
            c.background_tick();scan.assert_not_called();station.assert_not_called()
        self.assertEqual(c.nearby()['reason'],'NO_SPARE_RADIO');self.assertEqual(c.nearby()['candidates'],[])
        self.assertTrue(c.observations)  # Existing passive notification observations remain.

    def test_filters_saved_unknown_duplicate_networks_and_expires_then_clears_missing(self):
        c=self.idle_bridge()
        rows=[ROW,dict(ROW,bssid='02:00:00:44:88:02',signal=30),dict(ROW,ssid='Office',bssid='02:00:00:44:88:03'),dict(ROW,ssid=self.store.config['telescopes'][0]['ssid'],bssid='02:00:00:44:88:04')]
        with patch.object(self.net,'scan',return_value=rows):c.background_tick()
        found=c.nearby()['candidates'];self.assertEqual(len(found),1);self.assertEqual(found[0]['signal'],81)
        self.now=220;self.assertEqual(c.nearby()['candidates'],[])
        self.now=230
        with patch.object(self.net,'scan',return_value=[]):c.background_tick()
        self.assertEqual(c.nearby()['candidates'],[]);self.assertEqual(c.nearby()['scan_age_seconds'],0)
        restored=CandidateDiscovery(self.controller);self.assertEqual(restored.nearby()['candidates'],[])
        self.assertEqual(restored.lookup(found[0]['observation_id'])['ssid'],ROW['ssid'])

    def test_newly_saved_profiles_disappear_without_waiting_for_another_scan(self):
        c=self.idle_bridge()
        with patch.object(self.net,'scan',return_value=[ROW]):c.background_tick()
        ident=c.nearby()['candidates'][0]['observation_id']
        self.store.config['telescopes'].append(dict(self.store.config['telescopes'][0],id='new',ssid=ROW['ssid'],bssid=''))
        self.assertEqual(c.nearby()['candidates'],[]);self.assertEqual(c.lookup(ident)['suggested']['kind'],'seestar')

    def test_busy_network_retries_without_scanning_and_background_preserves_binding_choices(self):
        c=self.idle_bridge()
        with self.controller.net_lock:c.background_tick()
        self.assertEqual(c.background_reason,'NETWORK_BUSY');self.assertEqual(c.next_background,105)
        self.now=105
        with patch.object(self.net,'scan',return_value=[ROW]):candidate=c.run({'mode':'wifi'})['candidates'][0]
        self.now=121
        with patch.object(self.net,'scan',return_value=[ROW]):c.background_tick()
        self.assertEqual(c.binding({'candidate_id':candidate['candidate_id']})['aliases'][0]['bssid'],ROW['bssid'])
        with patch.object(self.net,'scan') as scan,patch.object(self.net,'scan_station') as station:
            for _ in range(5):self.assertEqual(self.controller.status()['nearby_telescopes']['candidates'][0]['ssid'],ROW['ssid'])
            scan.assert_not_called();station.assert_not_called()
