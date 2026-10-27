import copy
import http.client
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from wifi_bridge.config import Invalid, Store, password_matches, public_config, validate_config
from wifi_bridge.controller import Controller
from wifi_bridge.network import DemoNetwork, LinuxNetwork, NetworkError, keyfile, split_nm, radio_bands
from wifi_bridge.web import WebServer


def payload(devices):
    return {"hostname": "wifi-bridge-test", "country": "JP", "telescope_ip": "10.0.0.1",
            "admin_password": "local-admin-test-password",
            "home": {"ssid": "Maison", "password": "maison-test-pass", "mac": devices[1]["mac"]},
            "telescope": {"ssid": "Vespera-ABC", "password": "vespera-test-pass", "mac": devices[0]["mac"]}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.net = DemoNetwork()
        self.devices = self.net.devices()
        self.store = Store(self.tmp.name, self.devices[0]["mac"])
        self.controller = Controller(self.store, self.net, {"password": "bootstrap-test-code"}, demo=True)
        self.controller.update(devices=self.devices)

    def configured(self, changes=None):
        data = payload(self.devices)
        if changes:
            changes(data)
        self.store.save(validate_config(data, self.store.config, self.devices))


class ConfigTests(Fixture):
    def test_new_install_includes_supported_types_but_legacy_omissions_stay_disabled(self):
        from wifi_bridge.config import auto_policy
        self.assertFalse(self.store.config['auto_connect'])
        self.assertFalse(auto_policy(self.store.config)['force_first_profile'])
        for flag in ('seestar_demand','vespera_demand','dwarf_demand'):
            self.assertTrue(auto_policy(self.store.config)[flag])
            self.assertFalse(auto_policy({})[flag])
            self.assertFalse(auto_policy({'auto_policy':{flag:False}})[flag])
        self.configured()
        self.store.config['auto_policy']['dwarf_demand']=False
        self.store.save(self.store.config)
        reopened=Store(self.tmp.name,self.devices[0]['mac'])
        self.assertFalse(auto_policy(reopened.config)['dwarf_demand'])
        self.assertTrue(auto_policy(reopened.config)['vespera_demand'])

    def test_stable_hostname_from_mac(self):
        self.assertEqual(self.store.config["hostname"], "astro-bridge-12abcd")

    def test_save_root_only_and_no_cleartext_admin(self):
        self.configured()
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("local-admin-test-password", self.store.path.read_text())
        self.assertTrue(password_matches("local-admin-test-password", self.store.config["admin_hash"]))
        self.assertFalse(password_matches("wrong", self.store.config["admin_hash"]))
        self.assertEqual(Store(self.tmp.name, "other").config, self.store.config)

    def test_no_secrets_in_public_status(self):
        self.configured()
        public = json.dumps(self.controller.status())
        for secret in ("maison-test-pass", "vespera-test-pass", "admin_hash", "bootstrap-test-code"):
            self.assertNotIn(secret, public)

    def test_password_preserved_only_for_same_ssid(self):
        self.configured()
        data = payload(self.devices)
        data["home"]["password"] = ""
        self.assertEqual(validate_config(data, self.store.config, self.devices)["home"]["password"], "maison-test-pass")
        data["home"]["ssid"] = "Autre"
        with self.assertRaises(Invalid):
            validate_config(data, self.store.config, self.devices)

    def test_network_input_rejections(self):
        bad = [lambda d: d.update(hostname="wifi_bridge_abc"),
               lambda d: d["home"].update(mac=self.devices[0]["mac"]),
               lambda d: d["home"].update(mac="missing"),
               lambda d: d["home"].update(ssid="é" * 17),
               lambda d: d["home"].update(ssid="bad\nssid"),
               lambda d: d["home"].update(password="tiny"),
               lambda d: d.update(admin_password="short"),
               lambda d: d.update(telescope_ip="8.8.8.8"),
               lambda d: d.update(telescope_ip="127.0.0.1"),
               lambda d: d["telescope"].update(bssid="malformed"),
               lambda d: d.update(home=[]),
               lambda d: d["home"].update(password=[1, 2])]
        for mutation in bad:
            data = payload(self.devices)
            mutation(data)
            with self.subTest(data=str(mutation)), self.assertRaises(Invalid):
                validate_config(data, self.store.config, self.devices)

    def test_open_network_and_hidden_are_explicit(self):
        data = payload(self.devices)
        data["telescope"].update(open=True, password="", hidden=True)
        c = validate_config(data, self.store.config, self.devices)
        self.assertEqual(c["telescope"]["password"], "")
        self.assertTrue(c["telescope"]["hidden"])


class StateTests(Fixture):
    def test_first_boot_hotspot(self):
        self.controller.cycle(0)
        self.assertEqual(self.controller.status()["mode"], "setup")
        self.assertEqual(self.controller.status()["setup_ssid"], "astro-bridge-12abcd")

    def test_online_and_reconnect_telescope(self):
        self.configured()
        self.controller.cycle(0)
        self.assertTrue(self.controller.status()["relay_ready"])
        self.net.disconnect("telescope")
        self.controller.cycle(1)
        self.assertEqual(self.controller.status()["mode"], "waiting_telescope")
        self.assertTrue(self.controller.status()["home_ip"])
        self.controller.cycle(16)
        self.assertEqual(self.controller.status()["mode"], "online")

    def test_home_failure_fallback_and_automatic_recovery(self):
        self.configured(lambda d: d["home"].update(ssid="ECHEC"))
        self.controller.cycle(0)
        self.assertEqual(self.controller.status()["mode"], "field")
        self.assertTrue(self.controller.status()["relay_ready"])
        self.store.config["home"]["ssid"] = "Maison"
        with patch.object(self.net, "ap_vacant", return_value=True):
            self.controller.cycle(46)
        self.assertEqual(self.controller.status()["mode"], "online")
        self.assertNotIn("setup", self.net.active)

    def test_telescope_off_keeps_home_up(self):
        self.configured(lambda d: d["telescope"].update(ssid="ECHEC"))
        self.controller.cycle(0)
        self.assertEqual(self.controller.status()["mode"], "waiting_telescope")
        self.assertTrue(self.controller.status()["home_ip"])
        self.assertNotIn("setup", self.net.active)

    def test_dongle_removed_fallback(self):
        self.configured()
        self.controller.cycle(0)
        with patch.object(self.net, "devices", return_value=self.devices[:1]):
            self.controller.cycle(5)
        self.assertEqual(self.controller.status()["mode"], "no_management")
        self.assertIn("telescope", self.net.active)  # No takeover of an associated radio.

    def test_save_acknowledged_before_reboot(self):
        with patch("wifi_bridge.controller.time.monotonic", return_value=100):
            result = self.controller.save(payload(self.devices))
        self.assertEqual(result["reboot_in"], 4)
        with patch.object(self.net, "reboot") as reboot:
            self.controller.cycle(103)
            reboot.assert_not_called()
            self.controller.cycle(104)
            reboot.assert_called_once()
        self.controller.cycle(105)
        self.assertEqual(self.controller.status()["mode"], "online")


class NetworkTests(unittest.TestCase):
    def test_radio_bands_accept_integer_and_decimal_iw_frequencies(self):
        self.assertEqual(radio_bands("* 2412.0 MHz [1]"), ["2.4"])
        for frequency in ("5180", "5180.0"):
            self.assertEqual(radio_bands("* 2412 MHz [1]\n* " + frequency + " MHz [36]"), ["2.4", "5"])

    def test_scan_preserves_ssid_spaces_colons_and_frequency(self):
        from wifi_bridge.network import run
        completed = type("Result", (), {"stdout": " café\\: salon :02\\:aa\\:bb\\:cc\\:dd\\:ee:89:2412 MHz:WPA2\n", "returncode": 0})()
        with patch("wifi_bridge.network.subprocess.run", return_value=completed):
            networks = LinuxNetwork().scan("wlan0")
        self.assertEqual(networks[0]["ssid"], " café: salon ")
        self.assertEqual(networks[0]["frequency"], 2412)
        self.assertEqual(networks[0]["bssid"], "02:aa:bb:cc:dd:ee")

    def test_nm_escaping(self):
        self.assertEqual(split_nm(r"Maison\: invité:02\:aa\:bb\:cc\:dd\:ee:80:WPA2"),
                         ["Maison: invité", "02:aa:bb:cc:dd:ee", "80", "WPA2"])
        encoded = keyfile({"wifi": {"ssid": " café \\ "}})
        self.assertIn(r"ssid=\scafé\s\\\s", encoded)

    def test_firewall_scoped_and_overlap_rejected(self):
        net = LinuxNetwork()
        home, scope = {"name": "wlan1"}, {"name": "wlan0"}
        with patch.object(net, "address", side_effect=["192.168.1.42/24", "10.0.0.2/24"]), patch("wifi_bridge.network.run") as command:
            net.firewall(home, scope)
            rules = command.call_args.kwargs["input"]
            self.assertIn("tcp dport { 8082, 8083 }", rules)
            self.assertIn("masquerade", rules)
            self.assertNotIn("flush ruleset", rules)
            self.assertIn('iifname "wlan0" drop', rules)
        with patch.object(net, "address", side_effect=["10.0.0.5/24", "10.0.0.2/24"]), self.assertRaises(NetworkError):
            net.firewall(home, scope)

    def test_setup_uses_private_hostapd_config_and_never_passes_password_to_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            net = LinuxNetwork(directory)
            device = {"name": "wlan0"}
            password = "test-password-with-#-and-spaces "
            with patch("wifi_bridge.network.run") as command, patch.object(net, "connected", return_value=True), patch.object(net, "firewall"):
                net.hotspot(device, "astro-bridge-12abcd", password)
            config = Path(directory, "hostapd.conf")
            text = config.read_text()
            expected = hashlib.pbkdf2_hmac("sha1", password.encode(), b"astro-bridge-12abcd", 4096, 32).hex()
            self.assertEqual(net.hotspot_name(), 'astro-bridge-12abcd')
            self.assertIn("wpa_psk=" + expected, text)
            self.assertIn("wpa=2\n", text)
            self.assertIn("rsn_pairwise=CCMP\n", text)
            self.assertIn("ieee80211w=0\n", text)
            self.assertNotIn(password, text)
            self.assertNotIn(password, str(command.call_args_list))
            for name in ("hostapd.conf", "dnsmasq.conf", "hotspot.json"):
                self.assertEqual(Path(directory, name).stat().st_mode & 0o777, 0o600)
            self.assertEqual(net._hotspot_device(), "wlan0")

    def test_setup_ownership_survives_restart_and_is_released_before_client_association(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "hotspot.json").write_text('{"interface":"wlan0"}')
            net = LinuxNetwork(directory)
            with patch("wifi_bridge.network.run") as command, patch.object(net, "_profile", return_value="wifi-bridge-telescope"):
                net.connect("telescope", {"name": "wlan0", "mac": "02:00:00:00:00:01"}, {"ssid": "Scope"})
            calls = [item.args[0] for item in command.call_args_list]
            self.assertEqual(calls[0], ["systemctl", "stop", "wifi-bridge-ap", "wifi-bridge-dhcp"])
            self.assertLess(calls.index(["nmcli", "device", "set", "wlan0", "managed", "yes"]), len(calls) - 1)
            self.assertFalse(Path(directory, "hotspot.json").exists())
            self.assertIn("wifi-bridge-telescope", calls[-1])

    def test_setup_requires_both_ap_and_dhcp_and_does_not_interrupt_other_radio(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "hotspot.json").write_text('{"interface":"wlan0"}')
            net = LinuxNetwork(directory)
            with patch("wifi_bridge.network.run", return_value="active\nfailed"), patch.object(net, "address", return_value="192.168.77.1/24"):
                self.assertFalse(net.connected("setup", {"name": "wlan0"}))
            with patch("wifi_bridge.network.run"), patch.object(net, "disconnect") as disconnect, patch.object(net, "_profile", return_value="wifi-bridge-home"):
                net.connect("home", {"name": "wlan1", "mac": "02:00:00:00:00:02"}, {"ssid": "Home"})
            disconnect.assert_not_called()

    def test_target_outside_scope_subnet_rejected(self):
        net = LinuxNetwork()
        with patch.object(net, "address", side_effect=["192.168.1.42/24", "10.2.1.2/24"]), self.assertRaises(NetworkError):
            net.firewall({"name": "wlan1"}, {"name": "wlan0"})


class HTTPTests(Fixture):
    def setUp(self):
        super().setUp()
        self.server = WebServer(("127.0.0.1", 0), self.controller)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.cookie, self.csrf = "", ""

    def request(self, method, path, body=None, origin=True, csrf=True, host="127.0.0.1"):
        port = self.server.server_address[1]
        # ARMv6 password hashing can exceed five seconds under concurrent UI load.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self.addCleanup(conn.close)
        headers = {"Host": f"{host}:{port}", "Cookie": self.cookie, "Content-Type": "application/json"}
        if origin:
            headers["Origin"] = f"http://{host}:{port}"
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        raw = response.read()
        result = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
        cookie = response.getheader("Set-Cookie")
        status = response.status
        conn.close()
        return status, result, cookie

    def login(self, password="bootstrap-test-code"):
        status, result, cookie = self.request("POST", "/api/login", {"password": password})
        self.assertEqual(status, 200)
        self.cookie = cookie.split(";")[0]
        self.csrf = result["csrf"]

    def native(self, method, path, body=None, token=""):
        port=self.server.server_port
        conn=http.client.HTTPConnection('127.0.0.1',port,timeout=30)
        headers={'Host':f'127.0.0.1:{port}','Content-Type':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        conn.request(method,'/api/v1'+path,json.dumps(body) if body is not None else None,headers)
        response=conn.getresponse();data=json.loads(response.read());conn.close()
        return response.status,data

    def initial_password_sessions(self, native_first):
        secret='custom-first-save-bootstrap'
        self.controller.bootstrap['password']=secret;self.login(secret)
        code,authorization=self.native('POST','/auth/token',{'password':secret});self.assertEqual(code,200)
        token=authorization['access_token']
        home={'ssid':'FIRST SAVE DEMO','open':True,'mac':self.devices[1]['mac']}
        if native_first:
            self.assertEqual(self.native('PATCH','/home',home,token)[0],200)
        else:
            body=self.controller.status()['config'];body['home']=home
            code,result,_=self.request('POST','/api/config',body)
            self.assertEqual(code,200);self.assertFalse(result['sessions_invalidated'])
        self.assertTrue(password_matches(secret,self.store.config['admin_hash']))
        self.assertNotIn(secret,self.store.path.read_text())
        self.assertEqual(self.request('GET','/api/status')[0],200)
        self.assertEqual(self.native('GET','/status',token=token)[0],200)
        body=self.controller.status()['config'];body['home']['hidden']=True
        body['admin_password']=secret  # Even an explicit unchanged effective password.
        code,result,_=self.request('POST','/api/config',body)
        self.assertEqual(code,200);self.assertFalse(result['sessions_invalidated'])
        self.assertEqual(self.request('GET','/api/status')[0],200)
        self.assertEqual(self.native('GET','/status',token=token)[0],200)
        credentials={'target':'web','current_password':secret,'password':secret}
        if native_first:code,result=self.native('POST','/credentials',credentials,token)
        else:code,result,_=self.request('POST','/api/credentials',credentials)
        self.assertEqual(code,200);self.assertFalse(result['sessions_invalidated'])
        self.assertEqual(self.request('GET','/api/status')[0],200)
        self.assertEqual(self.native('GET','/status',token=token)[0],200)
        credentials['password']='different-new-admin-secret'
        if native_first:code,result=self.native('POST','/credentials',credentials,token)
        else:code,result,_=self.request('POST','/api/credentials',credentials)
        self.assertEqual(code,200);self.assertTrue(result['sessions_invalidated'])
        self.assertEqual(self.request('GET','/api/status')[0],401)
        self.assertEqual(self.native('GET','/status',token=token)[0],401)
        self.assertNotIn(credentials['password'],self.store.path.read_text())

    def test_initial_browser_save_and_unchanged_password_preserve_both_session_kinds(self):
        self.initial_password_sessions(native_first=False)

    def test_initial_native_save_and_unchanged_password_preserve_both_session_kinds(self):
        self.initial_password_sessions(native_first=True)

    def test_event_context_is_non_authorizing_and_rotates_with_browser_session(self):
        self.login();code, first, _ = self.request('GET', '/api/status')
        self.assertEqual(code, 200)
        self.assertEqual(first['event_context'], hashlib.sha256(self.csrf.encode()).hexdigest())
        self.assertEqual(self.request('GET', '/api/status')[1]['event_context'], first['event_context'])
        self.cookie = 'bridge_session=' + first['event_context']
        self.assertEqual(self.request('GET', '/api/status')[0], 401)
        self.login();self.assertNotEqual(self.request('GET', '/api/status')[1]['event_context'], first['event_context'])

    def test_positive_update_safe_hook_is_conservative(self):
        self.assertTrue(self.controller.update_safe())
        self.controller.action_at=1;self.assertFalse(self.controller.update_safe())
        self.controller.action_at=None
        with self.controller.net_lock:self.assertFalse(self.controller.update_safe())
        self.controller.pending_deletions.add('removed');self.assertFalse(self.controller.update_safe())
        self.controller.pending_deletions.clear()
        self.configured();self.assertFalse(self.controller.update_safe())

    def test_update_verification_blocks_mutation_but_keeps_read_and_logout(self):
        self.login()
        from wifi_bridge.ota import atomic
        atomic(self.server.updates.root / 'transaction.json', {'phase': 'verifying'})
        self.assertEqual(self.request('POST', '/api/config', payload(self.devices))[0], 409)
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.request('POST', '/api/config', payload(self.devices), csrf=False)[0], 403)
        self.assertEqual(self.request('GET', '/api/status')[0], 200)
        with patch('wifi_bridge.web.BUILD_ID', 'loaded-code-identity'):
            code, body, _ = self.request('GET', '/api/update-health')
            self.assertEqual(code, 200)
            self.assertEqual(body['build_id'], 'loaded-code-identity')
        self.assertEqual(self.request('POST', '/api/logout', {})[0], 200)

    def test_auth_same_origin_csrf_and_dns_rebinding(self):
        self.assertEqual(self.request("GET", "/api/status")[0], 401)
        self.assertEqual(self.request("GET", "/", host="evil.example")[0], 421)
        self.assertEqual(self.request("POST", "/api/login", {"password": "bootstrap-test-code"}, origin=False)[0], 403)
        self.login()
        self.assertEqual(self.request("GET", "/api/status")[0], 200)
        self.assertEqual(self.request("POST", "/api/config", payload(self.devices), csrf=False)[0], 403)
        self.assertFalse(self.store.path.exists())

    def test_config_persistence_invalidates_session_and_bootstrap(self):
        self.login()
        code,authorization=self.native('POST','/auth/token',{'password':'bootstrap-test-code'})
        self.assertEqual(code,200)
        self.assertEqual(self.request("POST", "/api/config", payload(self.devices))[0], 200)
        self.assertEqual(self.request("GET", "/api/status")[0], 401)
        self.assertEqual(self.native('GET','/status',token=authorization['access_token'])[0],401)
        self.assertEqual(self.request("POST", "/api/login", {"password": "bootstrap-test-code"})[0], 401)
        self.login("local-admin-test-password")
        code, body, _ = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertNotIn("maison-test-pass", json.dumps(body))

    def test_profile_actions_keep_session_and_require_csrf(self):
        self.configured()
        self.login("local-admin-test-password")
        action = {"id": "primary", "action": "disconnect"}
        self.assertEqual(self.request("POST", "/api/telescopes/action", action, csrf=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/telescopes/action", action)[0], 200)
        self.assertEqual(self.request("GET", "/api/status")[0], 200)
        data = public_config(self.store.config)
        data["hotspot"] = {"enabled": True}
        code, result, _ = self.request("POST", "/api/config", data)
        self.assertEqual(code, 200)
        self.assertFalse(result["sessions_invalidated"])
        self.assertEqual(self.request("GET", "/api/status")[0], 200)

    def test_browser_events_require_session_and_reads_are_side_effect_free(self):
        self.assertEqual(self.request("GET", "/api/events")[0], 401)
        self.configured(); self.controller.cycle(0); self.login("local-admin-test-password")
        code, page, _ = self.request("GET", "/api/events")
        self.assertEqual(code, 200)
        self.assertEqual(page["clock_status"], "unverified")
        sequence = self.controller.event_store.sequence
        self.request("GET", "/api/events"); self.request("GET", "/api/status")
        self.assertEqual(self.controller.event_store.sequence, sequence)
        self.assertEqual(self.request("GET", "/api/events?after=bad")[0], 400)
        self.assertEqual(self.request("GET", "/api/events", host="evil.example")[0], 421)
        self.request("POST", "/api/logout", {})
        self.assertEqual(self.request("GET", "/api/events")[0], 401)

    def test_locale_asset_is_public_but_static_allowlist_stays_bounded(self):
        status, body, _ = self.request("GET", "/i18n.js")
        self.assertEqual(status, 200)
        self.assertIn(b"BridgeI18n", body)
        self.assertEqual(self.request("GET", "/i18n.js", host="evil.example")[0], 421)
        self.login()
        for path in ("/static/i18n.js", "/../i18n.js", "/i18n.js/../config.json"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_login_throttling(self):
        for _ in range(5):
            self.assertEqual(self.request("POST", "/api/login", {"password": "wrong"})[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {"password": "bootstrap-test-code"})[0], 429)

    def test_malformed_json_shapes_and_static_paths(self):
        self.login()
        for value in ([], None, "text", 12):
            self.assertEqual(self.request("POST", "/api/config", value)[0], 400)
        self.assertEqual(self.request("GET", "/../../config.json")[0], 404)
        self.assertEqual(self.request("GET", "/")[0], 200)


if __name__ == "__main__":
    unittest.main()
