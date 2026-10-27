"""Regression coverage for independent radios, saved profiles and AP arbitration."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

from test_bridge import Fixture, payload
from wifi_bridge.config import Invalid, Store, profiles, validate_config
from wifi_bridge.network import LinuxNetwork, NetworkError


class MultiTests(Fixture):
    def multi(self):
        self.configured()
        c = copy.deepcopy(self.store.config)
        c["telescopes"] = profiles(c)
        c["telescopes"].append(dict(c["telescopes"][0], id="second", name="Second", ssid="Other", enabled=False))
        return c

    def test_switch_profiles_keeps_home_and_secrets(self):
        c = self.multi()
        self.store.save(validate_config(c, self.store.config, self.devices))
        self.controller.cycle(0)
        with patch.object(self.net, "disconnect", wraps=self.net.disconnect) as disconnect:
            self.controller.telescope_action({"id": "second", "action": "connect"})
            self.controller.cycle(5)
        self.assertNotIn("home", [call.args[0] for call in disconnect.call_args_list])
        self.assertTrue(self.controller.status()["home_ip"])
        self.assertIn("telescope-second", self.net.active)
        self.assertNotIn("telescope", self.net.active)
        self.assertEqual([p["enabled"] for p in profiles(self.store.config)], [False, True])
        self.assertNotIn("vespera-test-pass", json.dumps(self.controller.status()))
        restarted = Store(self.tmp.name, "unused")
        self.assertEqual([p["enabled"] for p in profiles(restarted.config)], [False, True])

    def test_optional_ap_waits_for_free_radio(self):
        c = self.multi()
        c["hotspot"] = {"enabled": True}
        self.store.save(validate_config(c, self.store.config, self.devices))
        self.controller.cycle(0)
        self.assertTrue(self.controller.status()["telescope_connected"])
        self.assertEqual(self.controller.status()["hotspot"]["reason"], "no_free_radio")
        self.controller.telescope_action({"id": "primary", "action": "disconnect"})
        self.controller.cycle(5)
        self.controller.cycle(6)  # The prior owned instrument is now actually disconnected.
        self.assertTrue(self.controller.status()["hotspot"]["active"])
        self.assertTrue(self.controller.status()["home_ip"])
        from wifi_bridge.api import MobileAPI,ApiError
        with self.assertRaises(ApiError):self.controller.telescope_action({"id":"second","action":"connect"})
        MobileAPI(self.controller).update("hotspot",{"enabled":False})
        self.controller.cycle(10)
        self.controller.telescope_action({"id":"second","action":"connect"})
        self.controller.cycle(15)
        self.assertFalse(self.controller.status()["hotspot"]["active"])
        self.assertTrue(self.controller.status()["telescope_connected"])

    def test_missing_instrument_adapter_preserves_home_and_saved_profile(self):
        c = self.multi()
        self.store.save(validate_config(c, self.store.config, self.devices))
        self.controller.cycle(0)
        with patch.object(self.net, "devices", return_value=self.devices[1:]):
            self.controller.cycle(5)
        self.assertTrue(self.controller.status()["home_ip"])
        self.assertEqual(self.controller.status()["telescopes"][0]["state"], "missing")
        self.controller.telescope_action({"id": "primary", "action": "disconnect"})
        self.assertEqual(len(profiles(self.store.config)), 2)

    def test_home_only_configuration_and_recovery(self):
        c = self.multi()
        c["telescopes"] = []
        c["hotspot"] = {"enabled": True}
        self.store.save(validate_config(c, self.store.config, self.devices))
        self.controller.cycle(0)
        self.assertTrue(self.controller.status()["home_ip"])
        self.assertTrue(self.controller.status()["hotspot"]["active"])
        self.assertEqual(profiles(self.store.config), [])

    def test_multiple_enabled_on_same_radio_rejected(self):
        c = self.multi()
        c["telescopes"][1]["enabled"] = True
        with self.assertRaises(Invalid):
            validate_config(c, self.store.config, self.devices)

    def test_multiple_radios_distinct_subnets_and_spare_ap(self):
        c = self.multi()
        extra = {"name": "wlan2", "mac": "02:00:00:00:00:02", "usb": True, "bands": ["2.4"], "label": "USB 2"}
        spare = dict(extra, name="wlan3", mac="02:00:00:00:00:03")
        devices = self.devices + [extra, spare]
        c["telescopes"][1].update(mac=extra["mac"], target_ip="192.168.4.1", enabled=True, kind="generic")
        c["hotspot"] = {"enabled": True}
        self.store.save(validate_config(c, self.store.config, devices))
        original = self.net.address
        with patch.object(self.net, "devices", return_value=devices), patch.object(self.net, "address", side_effect=lambda d,timeout=20: "192.168.4.2/24" if d["name"] == "wlan2" else original(d)):
            self.controller.cycle(0)
        status = self.controller.status()
        self.assertEqual(sum(p["relay_ready"] for p in status["telescopes"]), 2)
        self.assertEqual(status["hotspot"]["interface"], "wlan3")
        self.assertTrue(status["home_ip"])

    def test_subnet_conflict_blocks_second_and_preserves_first(self):
        c = self.multi()
        extra = {"name": "wlan2", "mac": "02:00:00:00:00:02", "usb": True, "bands": ["2.4"], "label": "USB 2"}
        devices = self.devices + [extra]
        c["telescopes"][1].update(mac=extra["mac"], enabled=True)
        self.store.save(validate_config(c, self.store.config, devices))
        with patch.object(self.net, "devices", return_value=devices), patch.object(self.net, "address", side_effect=lambda d: "192.168.1.42/24" if d["name"] == "wlan1" else "10.0.0.2/24"):
            self.controller.cycle(0)
            with patch.object(self.net, "connect", wraps=self.net.connect) as connect:
                self.controller.cycle(20)
                connect.assert_not_called()
        first, second = self.controller.status()["telescopes"]
        self.assertTrue(first["relay_ready"])
        self.assertEqual(second["state"], "conflict")
        self.assertNotIn("telescope-second", self.net.active)

    def test_legacy_migration_backup_retains_original_credentials(self):
        self.configured()
        old = copy.deepcopy(self.store.config)
        old.pop("schema_version");old.pop("telescopes");old.pop("hotspot")
        self.store.save(old)
        c = copy.deepcopy(old);c["telescopes"] = profiles(old)
        self.store.save(validate_config(c, old, self.devices))
        backup = Path(self.tmp.name, "config.v1.backup.json")
        self.assertEqual(json.loads(backup.read_text()), old)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(profiles(self.store.config)[0]["password"], old["telescope"]["password"])

    def test_passwords_follow_profile_id_not_position(self):
        c = self.multi()
        c["telescopes"][1]["password"] = "different-secret"
        self.store.save(validate_config(c, self.store.config, self.devices))
        c["telescopes"].reverse()
        for p in c["telescopes"]:
            p["password"] = ""
        result = validate_config(c, self.store.config, self.devices)
        self.assertEqual(result["telescopes"][0]["password"], "different-secret")
        self.assertEqual(result["telescopes"][1]["password"], "vespera-test-pass")

    def test_public_payload_preserves_existing_open_networks(self):
        from wifi_bridge.config import public_config
        self.configured(lambda d: d["telescope"].update(password="", open=True))
        c = public_config(self.store.config)
        c["telescopes"].append(dict(c["telescopes"][0], id="spare", name="Spare", enabled=False, open=True))
        result = validate_config(c, self.store.config, self.devices)
        self.assertEqual(result["telescopes"][0]["password"], "")

    def test_recovery_uses_spare_radio_before_suspending_instrument(self):
        self.configured(lambda d: d["home"].update(ssid="ECHEC"))
        extra = {"name": "wlan2", "mac": "02:00:00:00:00:02", "usb": True, "ap": True}
        with patch.object(self.net, "devices", return_value=self.devices + [extra]):
            self.controller.cycle(0)
        self.assertEqual(self.controller.status()["hotspot"]["interface"], "wlan2")

    def test_unsupported_ap_does_not_report_hotspot_active(self):
        with patch.object(self.net, "devices", return_value=[dict(d, ap=False) for d in self.devices]):
            self.controller.cycle(0)
        self.assertFalse(self.controller.status()["hotspot"]["active"])
        self.assertEqual(self.controller.status()["mode"], "no_management")
        self.assertEqual(self.controller.status()["hotspot"]["reason"], "no_free_radio")

    def test_home_subnet_reserved_for_ap_keeps_home_without_starting_ap(self):
        c = self.multi();c["hotspot"] = {"enabled": True}
        self.store.save(validate_config(c, self.store.config, self.devices))
        with patch.object(self.net, "address", side_effect=lambda d: "192.168.77.42/24" if d["usb"] else "10.0.0.2/24"):
            self.controller.cycle(0)
        self.assertEqual(self.controller.status()["hotspot"]["reason"], "subnet_conflict")
        self.assertTrue(self.controller.status()["home_ip"])
        self.assertTrue(self.controller.status()["relay_ready"])

    def test_restart_cleans_orphaned_networkmanager_profile(self):
        self.configured();self.net.active.add("telescope-orphan")
        with patch.object(self.net, "remove_profile", wraps=self.net.remove_profile) as remove:
            self.controller.cycle(0)
        remove.assert_called_once_with("telescope-orphan")
        self.assertTrue(self.controller.status()["relay_ready"])

    def test_new_settings_apply_without_reboot(self):
        c = self.multi()
        self.assertEqual(self.controller.save(c)["reboot_in"], 0)
        self.assertIsNone(self.controller.action_at)

    def test_delete_removes_nm_profile_after_disconnect(self):
        self.configured();self.controller.cycle(0)
        self.controller.telescope_action({"id": "primary", "action": "delete"})
        with patch.object(self.net, "remove_profile", wraps=self.net.remove_profile) as remove:
            self.controller.cycle(5)
        remove.assert_called_once_with("telescope")
        self.assertTrue(self.controller.status()["home_ip"])
        self.assertEqual(profiles(self.store.config), [])


class MultiFirewallTests(Fixture):
    def test_all_accepts_precede_scope_drops_and_single_legacy_relay(self):
        net = LinuxNetwork()
        addresses = {"home": "192.168.1.2/24", "a": "10.0.0.2/24", "b": "192.168.4.2/24"}
        entries = [{"device": {"name": "a"}, "target": "10.0.0.1", "legacy_ports": True},
                   {"device": {"name": "b"}, "target": "192.168.4.1", "legacy_ports": False}]
        with patch.object(net, "address", side_effect=lambda d: addresses[d["name"]]), patch("wifi_bridge.network.run") as run:
            net.firewall(home={"name": "home"}, telescopes=entries)
        rules = run.call_args.kwargs["input"]
        self.assertLess(rules.index('iifname "home" oifname "b" ip saddr 192.168.1.0/24 ip daddr'), rules.index('iifname "a" drop'))
        self.assertEqual(rules.count("dnat ip to"), 1)
        self.assertEqual(rules.count("masquerade"), 2)
        entries[1]["target"] = "8.8.8.8"
        with patch.object(net, "address", side_effect=lambda d: addresses[d["name"]]), patch("wifi_bridge.network.run") as run, self.assertRaises(NetworkError):
            net.firewall(home={"name": "home"}, telescopes=entries)
        # Invalid plans may only withdraw the separately owned sharing chains;
        # they must not publish a new instrument table or touch associations.
        for call in run.call_args_list:
            self.assertEqual(call.args[0],['nft','-f','-'])
            self.assertNotIn('delete table',call.kwargs['input'])
            self.assertIn('flush chain ip wifi_bridge sharing_forward',call.kwargs['input'])
