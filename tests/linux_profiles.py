"""Parse real NetworkManager keyfiles offline without altering any network."""
import subprocess
from wifi_bridge.network import LinuxNetwork, keyfile

net = LinuxNetwork()
for role in ("home", "telescope"):
    profile = net._base(role, "wlan0", "02:11:22:33:44:55", " café: test\\ ")
    profile["wifi-security"] = {"key-mgmt": "wpa-psk", "psk": " fictif\\ 2026 "}
    result = subprocess.run(["nmcli", "--offline", "connection", "modify", "connection.id", "parsed-profile"],
                            input=keyfile(profile), capture_output=True, text=True)
    assert result.returncode == 0, role + ": " + result.stderr
    assert "parsed-profile" in result.stdout
    print("PASS: profile " + role + " accepted by NetworkManager, SSID and PSK containing whitespace/backslashes")
