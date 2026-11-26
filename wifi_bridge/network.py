"""Linux networking: NetworkManager clients, hostapd setup AP and owned nft table."""
import ipaddress
import os
import json
from pathlib import Path
import re
import socket
import subprocess
import time
import uuid

from .config import atomic_json
from .init_system import using_openrc, openrc_command
from .mounts import mount_probe
from .plugins import relay_plugins, proxy_flag, plugin



# Hubs built into the board itself (Raspberry Pi 4: VL805 USB 2.0 hub; Pi 3: LAN9514 / LAN7515),
# recognized only next to the root hub. The Pi 5 and the Zero W have none.
BOARD_HUBS = {('2109', '3431'), ('0424', '9514'), ('0424', '2514')}
USB_DEVICE = re.compile(r'[0-9]+-[0-9]+(?:\.[0-9]+)*')


def behind_usb_hub(path):
    """True when a USB Wi-Fi adapter is plugged through a hub other than the board's own.

    Reads the USB topology only (sysfs). The adapter is the first USB device above
    its network interface; any hub above it counts, except a known board hub
    attached at the first two levels.
    """
    path = Path(path)
    adapter = None
    for parent in [path, *path.parents]:
        if not USB_DEVICE.fullmatch(parent.name):
            continue
        if adapter is None:
            adapter = parent
            continue
        try:
            klass = (parent / 'bDeviceClass').read_text().strip()
            ids = ((parent / 'idVendor').read_text().strip().lower(), (parent / 'idProduct').read_text().strip().lower())
        except OSError:
            continue
        if klass != '09':
            continue
        depth = parent.name.split('-', 1)[1].count('.') + 1
        if ids in BOARD_HUBS and depth <= 2:
            continue
        return True
    return False

class NetworkError(RuntimeError):
    # NetworkManager's activation failure code, when a connection attempt reported one.
    activation = None
    # The step of joining an instrument's Wi-Fi that failed (see link_failure_stage).
    stage = None


def link_failure_stage(activation=None, state=None, reason=None):
    """Name the step that failed when an instrument's Wi-Fi could not be joined.

    activation is the code NetworkManager gave with the failure; state and reason
    are the interface's own, read while an attempt that outlasted its wait is still
    pending. Returns NETWORK_NOT_FOUND (the network is not heard), NOT_ASSOCIATED
    (heard, but the instrument did not accept the link), ADDRESS_NOT_OBTAINED
    (linked, but the instrument gave no address), or None when nothing says which.
    """
    codes = {value for value in (activation, reason) if type(value) is int}
    if 53 in codes: return 'NETWORK_NOT_FOUND'                       # ssid-not-found
    if codes & {5, 15, 16, 17}: return 'ADDRESS_NOT_OBTAINED'        # ip-config-unavailable, dhcp-*
    if state in (70, 80): return 'ADDRESS_NOT_OBTAINED'              # still waiting for its address
    if codes & {8, 9, 10, 11}: return 'NOT_ASSOCIATED'               # supplicant-*
    if state in (40, 50, 60): return 'NOT_ASSOCIATED'                # still preparing or associating
    return None


# Instruments whose own access point always uses one address plan: kind -> its address.
# Only for these may the bridge take an address itself when none is given.
FIXED_ADDRESS_PLANS = {'synscan': '192.168.4.1'}
# Hosts tried, in order, far from the low addresses such access points hand out.
FALLBACK_HOSTS = (250, 249, 248)
KEEP_SHARING = object()


def leading_code(value):
    match = re.match(r'^(\d{1,3})(?:\s|$)', value or '')
    return int(match[1]) if match else None


class NetworkAuthenticationError(NetworkError):
    def __init__(self):
        super().__init__('Wi-Fi authentication failed. Check the saved password.')


class NetworkIsolationError(NetworkError):
    def __init__(self, reason='ISOLATION_UNAVAILABLE'):
        self.reason = reason
        super().__init__('Isolated connection unavailable. Check namespace support, Wi-Fi driver and host tools.')


def run(args, timeout=20, check=True, input=None):
    try:
        def execute(command):
            return subprocess.run(command, input=input, text=True, capture_output=True,
                timeout=timeout, check=False, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
        result = openrc_command(args, execute) if args[0] == "systemctl" and using_openrc() else execute(args)
    except (OSError, subprocess.TimeoutExpired):
        raise NetworkError("System command unavailable or timed out: " + args[0]) from None
    if check and result.returncode:
        # stderr may contain SSIDs or credentials; do not propagate it.
        if (args[0] == 'nmcli' and 'connection' in args and 'up' in args
                and re.search(r'^Error: Connection activation failed: \(7\)(?:\s|$)',
                              getattr(result, 'stderr', '') or '', re.M)):
            raise NetworkAuthenticationError()
        error = NetworkError("System command failed: " + args[0])
        if args[0] == 'nmcli' and 'connection' in args and 'up' in args:
            # Only the numeric code is kept: the text may name a network.
            code = re.search(r'^Error: Connection activation failed: \((\d{1,3})\)(?:\s|$)',
                             getattr(result, 'stderr', '') or '', re.M)
            if code: error.activation = int(code[1])
        raise error
    # Preserve SSID whitespace; trim only the command's final line terminator.
    return result.stdout.rstrip("\n")


def split_nm(line):
    parts, current, escaped = [], "", False
    for char in line:
        if escaped:
            current += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return parts


def radio_bands(capabilities):
    # Recent iw versions render frequencies as 5180.0 MHz, older ones as 5180 MHz.
    frequencies = [int(f) for f in re.findall(r"\*\s+(\d{4})(?:\.\d+)? MHz", capabilities)]
    return ["2.4", "5"] if any(4900 <= f < 5900 for f in frequencies) else ["2.4"]


def keyvalue(value):
    # GLib keyfile string escaping, including leading/trailing spaces.
    return str(value).replace("\\", "\\\\").replace(" ", "\\s").replace("\n", "\\n").replace("\r", "\\r")


def keyfile(sections):
    return "\n".join("[" + section + "]\n" + "\n".join(
        key + "=" + keyvalue(value) for key, value in entries.items()) + "\n"
        for section, entries in sections.items())


def hotspot_route_targets(targets, blocked_networks=(), blocked_addresses=()):
    """Bounded host routes only; never advertise management/local destinations."""
    private = tuple(ipaddress.IPv4Network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    blocked = [ipaddress.IPv4Network('192.168.77.0/24'), *blocked_networks]
    excluded = set(blocked_addresses)
    accepted = set()
    for value in targets:
        try:
            address = ipaddress.IPv4Address(value)
        except (ValueError, TypeError):
            continue
        if (not any(address in network for network in private) or address in excluded
                or any(address in network for network in blocked)):
            continue
        accepted.add(address)
        if len(accepted) > 24:
            raise NetworkError('Too many hotspot host routes.')
    return [str(address) for address in sorted(accepted)]


def networkmanager_process_identity(proc=Path('/proc')):
    """Find one real root-owned daemon, including OpenRC foreground operation.

    PID files are optional on both supported systems; supervise-daemon's PID is
    not NetworkManager's identity. A bounded incomplete/ambiguous view is unknown.
    The optional root is only for local functional tests, never an API input.
    """
    def read(path, limit):
        with path.open(encoding='utf-8') as stream:
            value = stream.read(limit+1)
        if len(value) > limit: raise ValueError('Process metadata exceeds limit.')
        return value

    def start(path, pid):
        head, fields = read(path/'stat', 4096).rsplit(')', 1)
        fields = fields.split()
        if (head != pid+' (NetworkManager' or len(fields) < 20
                or fields[0] in ('Z','X','x') or not re.fullmatch(r'[0-9]{1,20}', fields[19])):
            raise ValueError('Invalid daemon process identity.')
        return fields[19]

    candidates = []
    try:
        with os.scandir(proc) as entries:
            for count, entry in enumerate(entries):
                if count >= 4096: return None
                pid = entry.name
                if not re.fullmatch(r'[1-9][0-9]{0,9}', pid): continue
                path = proc/pid
                try: comm = read(path/'comm', 64).strip()
                except (OSError, UnicodeError): continue  # Unrelated processes can exit.
                if comm != 'NetworkManager': continue
                before = start(path, pid)
                if os.readlink(path/'exe') not in ('/usr/sbin/NetworkManager','/sbin/NetworkManager'):
                    return None
                status = read(path/'status', 16384)
                if not re.search(r'^Uid:\s+0\s+0\s+0\s+0\s*$', status, re.M): return None
                if start(path, pid) != before: return None
                candidates.append(pid+':'+before)
                if len(candidates) > 1: return None
        return candidates[0] if len(candidates) == 1 else None
    except (OSError, ValueError, UnicodeError):
        return None


def networkmanager_owned_process_identity(pid, proc=Path('/proc')):
    """Validate only the daemon PID returned by the system bus's unique owner."""
    if not isinstance(pid,str) or not re.fullmatch(r'[1-9][0-9]{0,9}',pid):return None
    path=proc/pid
    def read(name,limit):
        with (path/name).open(encoding='utf-8') as stream:value=stream.read(limit+1)
        if len(value)>limit:raise ValueError('Process metadata exceeds limit.')
        return value
    def started():
        head,parts=read('stat',4096).rsplit(')',1);parts=parts.split()
        if (head!=pid+' (NetworkManager' or len(parts)<20 or parts[0] in ('Z','X','x')
                or not re.fullmatch(r'[0-9]{1,20}',parts[19])):raise ValueError('Invalid daemon identity.')
        return parts[19]
    try:
        before=started()
        if read('comm',64).strip()!='NetworkManager':return None
        if os.readlink(path/'exe') not in ('/usr/sbin/NetworkManager','/sbin/NetworkManager'):return None
        if not re.search(r'^Uid:\s+0\s+0\s+0\s+0\s*$',read('status',16384),re.M):return None
        return pid+':'+before if started()==before else None
    except (OSError,ValueError,UnicodeError):return None


class LinuxNetwork:
    def __init__(self, state_dir="/var/lib/wifi-bridge"):
        self.state_dir = Path(state_dir)
        self.last_firewall = None
        self.country_code = "JP"
        self.telemetry = None
        self.sharing_active = None
        self.link_cache = {}
        self.isolated = None
        # Owner switch (auto_policy.address_fallback), and the roles whose address the bridge took itself.
        self.address_fallback = True
        self.announce_check = 0
        self.fallback_roles = set()
        # What radio_idle last read per interface (NetworkManager state, link), for the service log.
        self.radio_observations = {}
        # Set by the controller: true once a restart is requested or the service is stopping.
        self.halting = lambda: False

    def _isolation(self):
        if self.isolated is None:
            from .isolated import IsolatedRadios
            self.isolated = IsolatedRadios(self.state_dir, run)
        return self.isolated

    def isolated_endpoints(self, entries, rows, paths):
        if self.isolated or any(p.get('isolated') for p in entries):
            self._isolation().sync(entries, rows, paths)

    def close(self):
        if self.isolated: self.isolated.close()

    def devices(self):
        devices = []
        for entry in sorted(Path("/sys/class/net").iterdir()):
            if not (entry / "wireless").exists() and not (entry / "phy80211").exists():
                continue
            name = entry.name
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", name):
                continue
            mac = run(["nmcli", "-g", "GENERAL.PERM-HWADDR", "device", "show", name], check=False)
            if not re.fullmatch(r"(?:[A-Fa-f0-9]{2}:){5}[A-Fa-f0-9]{2}", mac):
                mac = (entry / "address").read_text().strip()
            usb = "/usb" in str((entry / "device").resolve())
            hub = usb and behind_usb_hub((entry / "device").resolve())
            bands = ["2.4"]
            capabilities = ""
            phy_path = entry / "phy80211" / "name"
            if phy_path.exists():
                capabilities = run(["iw", "phy", phy_path.read_text().strip(), "info"], check=False)
                bands = radio_bands(capabilities)
            devices.append({"name": name, "mac": mac.lower(), "usb": usb,
                            "bands": bands, "ap": bool(re.search(r"^\s*\* AP\s*$", capabilities, re.M)),
                            "label": name + (" · USB" if usb else " · built-in"),
                            **({"usb_hub": True} if hub else {})})
        if self.isolated or (self.state_dir/'isolated').exists():
            owned = self._isolation().present_devices()
            devices = [d for d in devices if d['mac'] not in {r['mac'] for r in owned}] + owned
        return sorted(devices, key=lambda d: (d["usb"], d["name"]))

    def scan(self, name, rescan=True, timeout=None):
        # NetworkManager handles AP-mode scans when supported. Onboard scans during
        # hotspot operation may be unavailable; USB results can still fill both SSIDs.
        result = run(["nmcli", "-t", "-f", "SSID,BSSID,SIGNAL,FREQ,SECURITY", "device", "wifi",
                      "list", "ifname", name, "--rescan", "yes" if rescan else "no"], timeout=timeout or (25 if rescan else 3))
        networks = []
        for line in result.splitlines():
            fields = split_nm(line)
            if len(fields) == 5 and fields[0]:
                networks.append(dict(ssid=fields[0], bssid=fields[1],
                                     signal=int(fields[2] or 0), frequency=int(fields[3].split()[0]),
                                     security=fields[4]))
        return sorted(networks, key=lambda n: -n["signal"])

    def scan_station(self, device, home, role="home"):
        if self.isolated and self.isolated.record_for(device): return False
        # A scan request on our associated station never changes its connection.
        info=run(["iw","dev",device['name'],"info"],timeout=2,check=False)
        return (self.connected(role,device,home) and self._hotspot_device()!=device['name']
                and re.findall(r"^\s*type\s+(\S+)\s*$",info,re.M)==['managed'])

    def cached_scan(self, name):return self.scan(name,rescan=False)

    def radio_idle(self, device):
        if self.isolated and self.isolated.record_for(device): return False
        # Fail closed for unmanaged, unknown, externally associated and AP devices.
        state = run(["nmcli", "-g", "GENERAL.STATE", "device", "show", device["name"]], timeout=2, check=False)
        link = run(["iw", "dev", device["name"], "link"], timeout=2, check=False)
        info = run(["iw", "dev", device["name"], "info"], timeout=2, check=False)
        self.radio_observations[device["name"]] = {
            "state": state.split(" ", 1)[0] if re.fullmatch(r"\d{1,3}", state.split(" ", 1)[0]) else "unknown",
            "link": "not connected" if link.strip() == "Not connected." else "connected" if link.lstrip().startswith("Connected") else "unknown"}
        return (state.split(" ", 1)[0] == "30" and link.strip() == "Not connected."
                and re.findall(r"^\s*type\s+(\S+)\s*$", info, re.M) == ['managed']
                and self._hotspot_device() != device["name"])

    def release_stale_hotspot(self, devices):
        try:
            return self._release_stale_hotspot(devices)
        except (NetworkError, OSError, ValueError, KeyError):
            return False

    def _release_stale_hotspot(self, devices):
        """Release only our stopped AP's ownership after proving its radio idle.

        No services, links or associations are changed here. Unknown observations,
        a running service or an absent adapter retain ownership for safe recovery.
        The one exception is the radio the bridge itself released from
        NetworkManager to start this AP: once both AP services are confirmed
        stopped and the radio still has the recorded MAC address, it is handed
        back to NetworkManager exactly as a normal AP stop would. Without this,
        an AP that stopped on its own left that radio unmanaged and blocked every
        later hotspot start until a reboot.
        The controller calls this while holding its network lock.
        """
        path = self.state_dir / 'hotspot.json'
        if not path.exists(): return False
        before = path.read_bytes()
        device = next((d for d in devices if d['name'] == self._hotspot_device()), None)
        if device is None: return False
        services = run(['systemctl', 'is-active', 'wifi-bridge-ap', 'wifi-bridge-dhcp'], timeout=5, check=False)
        if services.splitlines() != ['inactive', 'inactive']: return False
        state = run(['nmcli', '-g', 'GENERAL.STATE', 'device', 'show', device['name']], timeout=2)
        recorded = json.loads(before).get('mac')
        if (state.split(' ', 1)[0] == '10' and isinstance(recorded, str) and recorded
                and recorded.lower() == str(device.get('mac', '')).lower()):
            if path.read_bytes() != before: return False
            run(['ip', 'address', 'del', '192.168.77.1/24', 'dev', device['name']], timeout=3, check=False)
            run(['nmcli', 'device', 'set', device['name'], 'managed', 'yes'], timeout=5)
            path.unlink()
            return True
        link = run(['iw', 'dev', device['name'], 'link'], timeout=2)
        info = run(['iw', 'dev', device['name'], 'info'], timeout=2)
        if (state.split(' ', 1)[0] != '30' or link.strip() != 'Not connected.'
                or re.findall(r'^\s*type\s+(\S+)\s*$', info, re.M) != ['managed']): return False
        if path.read_bytes() != before: return False
        path.unlink()
        return True

    def discovery_associated(self, device, profile):
        if self.isolated and self.isolated.record_for(device):
            from .config import role_for
            return self.isolated.connected(role_for(profile), device, profile)
        from .config import role_for
        current = run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", device["name"]], timeout=2, check=False)
        link = run(["iw", "dev", device["name"], "link"], timeout=2, check=False)
        return (current == "wifi-bridge-" + role_for(profile)
                and any(line.lstrip() == "SSID: " + profile['ssid'] for line in link.splitlines()))

    def link_observation(self, device, profile):
        if self.isolated and self.isolated.record_for(device):
            return self.isolated.link_observation(device, profile)
        from .config import role_for
        current = run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", device["name"]], timeout=2, check=False)
        link = run(["iw", "dev", device["name"], "link"], timeout=2, check=False)
        match = re.search(r"^Connected to ([a-fA-F0-9:]{17})\b", link, re.M)
        if (current != 'wifi-bridge-' + role_for(profile) or not match
                or not any(line.lstrip() == 'SSID: ' + profile['ssid'] for line in link.splitlines())): return None
        return {'bssid': match.group(1).lower()}

    def discover_seestar(self, device, profile):
        from .candidates import seestar_probe
        return seestar_probe(device, self.address(device, timeout=2), profile['target_ip'])

    def discover_mount(self, device, profile):
        if self.isolated and self.isolated.record_for(device):
            from .isolated import in_namespace
            record = self.isolated.record_for(device)
            with in_namespace('/run/netns/'+record['namespace']):
                return mount_probe(device, self.address(device, timeout=2), profile['target_ip'], profile['kind'])
        return mount_probe(device, self.address(device, timeout=2), profile['target_ip'], profile['kind'])

    def _profile(self, role, sections):
        name = "wifi-bridge-" + role
        path = Path("/etc/NetworkManager/system-connections") / (name + ".nmconnection")
        import os
        payload = keyfile(sections)
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        run(["nmcli", "connection", "load", str(path)])
        return name

    def _base(self, role, name, mac, ssid):
        return {"connection": {"id": "wifi-bridge-" + role,
                                "uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, "wifi-bridge/" + role)),
                                "type": "wifi", "interface-name": name, "autoconnect": "false"},
                "wifi": {"mode": "infrastructure", "ssid": ssid, "mac-address": mac,
                         "cloned-mac-address": "permanent", "powersave": 2},
                "ipv4": {"method": "auto", "route-metric": 100 if role == "home" else 600,
                         "never-default": "false" if role == "home" else "true",
                         "ignore-auto-dns": "false" if role == "home" else "true"},
                "ipv6": {"method": "disabled"}}

    def connect(self, role, device, config):
        if config.get('isolated'):
            return self._isolation().connect(role, device, config)
        if self.isolated and self.isolated.record_for(device):
            self.isolated.release(self.isolated.record_for(device)['role'])
        if self._hotspot_device() == device["name"]:
            self.disconnect("setup")
        sections = self._base(role, device["name"], device["mac"], config["ssid"])
        if config.get("bssid"):
            sections["wifi"]["bssid"] = config["bssid"]
        sections["wifi"]["hidden"] = str(config.get("hidden", False)).lower()
        if config.get("password"):
            sections["wifi-security"] = {"key-mgmt": "wpa-psk", "psk": config["password"]}
        if role.startswith('telescope'):
            sections['connection']['auth-retries'] = 1
        name = self._profile(role, sections)
        self.fallback_roles.discard(role)
        # An instrument's own DHCP server can take 30 s or more after the link (AZ-GTiX: 27 to 31 s
        # measured), so its wait covers NetworkManager's 45 s address timeout plus the association.
        wait = 65 if role.startswith('telescope') else 35
        try:
            run(["nmcli", "--wait", str(wait), "connection", "up", "id", name,
                 "ifname", device["name"]], timeout=wait + 5)
        except NetworkError as error:
            rejected = isinstance(error, NetworkAuthenticationError)
            if not rejected:
                reason = state = ''
                try:
                    reason = run(['nmcli', '-g', 'GENERAL.REASON', 'device', 'show', device['name']], timeout=2, check=False)
                    rejected = bool(re.match(r'^7(?:\s|$)', reason))
                    # Read before our own cancellation below: an attempt that outlasted
                    # its wait is still at the step where it is stuck.
                    if not rejected:
                        state = run(['nmcli', '-g', 'GENERAL.STATE', 'device', 'show', device['name']], timeout=2, check=False)
                except NetworkError:
                    pass
                if not rejected:
                    error.stage = link_failure_stage(error.activation, leading_code(state), leading_code(reason))
            # nmcli's wait timeout does not cancel NetworkManager's activation.
            # Cancel only our own role, after retaining its failure reason.
            try: self.disconnect(role)
            except NetworkError: pass
            if rejected: raise NetworkAuthenticationError() from None
            if error.stage == 'ADDRESS_NOT_OBTAINED' and self._fallback_address(role, device, config, sections):
                return
            raise

    def _fallback_address(self, role, device, config, sections):
        """Take an address ourselves on an instrument network whose plan is fixed and known.

        Only after the instrument accepted the link and gave no address, only
        for a kind listed in FIXED_ADDRESS_PLANS at its usual address, and only
        while the owner switch is on. NetworkManager probes the address before
        using it (dad-timeout) and refuses one that answers; the instrument
        must then answer a ping. Nothing else is sent to the instrument.
        """
        target = config.get('target_ip')
        if (not self.address_fallback or not target or not role.startswith('telescope')
                or FIXED_ADDRESS_PLANS.get(config.get('kind')) != target): return False
        network = ipaddress.IPv4Network(target + '/24', strict=False)
        for host in FALLBACK_HOSTS:
            # A restart or a stop ends the search before the next blocking command.
            if self.halting(): return False
            address = network.network_address + host
            if address == ipaddress.IPv4Address(target): continue
            sections['ipv4'] = {'method': 'manual', 'address1': str(address) + '/24', 'dad-timeout': 2000,
                                'route-metric': 600, 'never-default': 'true', 'ignore-auto-dns': 'true'}
            name = self._profile(role, sections)
            try:
                run(['nmcli', '--wait', '20', 'connection', 'up', 'id', name, 'ifname', device['name']], timeout=25)
                run(['ping', '-c', '2', '-W', '2', '-I', device['name'], target], timeout=8)
            except NetworkError as error:
                try: self.disconnect(role)
                except NetworkError: pass
                # An address that answers is refused by the probe (ip-address-duplicate): try the next one.
                # Anything else, a silent instrument included, ends here.
                if error.activation != 64: return False
                continue
            self.fallback_roles.add(role)
            print('wifi-bridge: ' + role + ' gave no address; the bridge took a free one of its fixed network', flush=True)
            return True
        return False

    def known_telescope_roles(self):
        active = run(["nmcli", "-g", "NAME", "connection", "show"])
        return {name[len("wifi-bridge-"):] for name in active.splitlines()
                if re.fullmatch(r"wifi-bridge-telescope(?:-[a-z0-9][a-z0-9-]{0,31})?", name)} | ({r['role'] for r in self.isolated.records.values()} if self.isolated else set())

    def remove_profile(self, role):
        run(["nmcli", "--wait", "5", "connection", "delete", "id", "wifi-bridge-" + role],
            timeout=10, check=False)

    def hotspot_owner(self):
        """Interface recorded as owning the bridge AP, or '' when none is recorded."""
        try:
            return self._hotspot_device()
        except (OSError, ValueError, KeyError, TypeError):
            return ''

    def _hotspot_device(self):
        path = self.state_dir / "hotspot.json"
        if not path.exists():
            return ""
        return json.loads(path.read_text())["interface"]

    def disconnect(self, role):
        if self.isolated and any(r['role'] == role for r in self.isolated.records.values()):
            return self.isolated.release(role)
        if role == "setup":
            name = self._hotspot_device()
            if not name:
                return
            run(["systemctl", "stop", "wifi-bridge-ap", "wifi-bridge-dhcp"])
            run(["ip", "address", "del", "192.168.77.1/24", "dev", name], check=False)
            run(["nmcli", "device", "set", name, "managed", "yes"], check=False)
            (self.state_dir / "hotspot.json").unlink(missing_ok=True)
            return
        run(["nmcli", "--wait", "5", "connection", "down", "id", "wifi-bridge-" + role],
            timeout=10, check=False)

    def connected(self, role, device, config=None):
        if not device:
            return False
        if device.get('kind') == 'ethernet':
            actual = self.wired_home()
            return role == 'home' and actual is not None and all(actual[k] == device[k] for k in ('name', 'mac', 'address'))
        if self.isolated and self.isolated.record_for(device):
            return self.isolated.connected(role, device, config)
        if role == "setup":
            if self._hotspot_device() != device["name"]:
                return False
            try:
                active = run(["systemctl", "is-active", "wifi-bridge-ap", "wifi-bridge-dhcp"], timeout=3, check=False)
                if active.splitlines() != ["active", "active"] or self.address(device, timeout=3) != "192.168.77.1/24":
                    return False
                info = run(["iw", "dev", device["name"], "info"], timeout=2, check=False)
                if re.findall(r'^\s*type\s+(\S+)\s*$', info, re.M) != ['AP']:
                    return False
                status = run(['hostapd_cli', '-p', '/run/wifi-bridge-hostapd', '-i', device['name'], 'status'], timeout=2, check=False)
                return re.findall(r'^state=(.*)$', status, re.M) == ['ENABLED']
            except NetworkError:
                return False
        current = run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", device["name"]], check=False)
        if current != "wifi-bridge-" + role or not self.address(device):
            return False
        if config is not None:
            link = run(["iw", "dev", device["name"], "link"], check=False)
            from .link_observations import remember_link
            remember_link(self.link_cache, device["name"], link)
            ssid = next((line.lstrip()[6:] for line in link.splitlines() if line.lstrip().startswith("SSID: ")), None)
            if ssid != config["ssid"]:
                return False
        return True

    def address(self, device, timeout=20):
        if self.isolated and self.isolated.record_for(device): return self.isolated.address(device)
        raw = run(["ip", "-j", "-4", "address", "show", "dev", device["name"]], timeout=timeout)
        if device.get('kind') == 'ethernet':
            addresses = [addr['local'] + '/' + str(addr['prefixlen'])
                         for item in json.loads(raw or '[]') for addr in item.get('addr_info', [])
                         if addr.get('scope') == 'global']
            if addresses != [device.get('address')]:
                raise NetworkError('No confirmed management forwarding path.')
            return addresses[0]
        for item in json.loads(raw or "[]"):
            for addr in item.get("addr_info", []):
                if addr.get("scope") == "global":
                    return addr["local"] + "/" + str(addr["prefixlen"])
        return ""

    def _private_text(self, filename, text):
        path = self.state_dir / filename
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def hotspot_dhcp(self, device, targets=(), *, restart=True, sharing=KEEP_SHARING):
        """Publish checked DHCP-only changes without touching AP or telescope links.

        A caller that only updates routes keeps the Internet sharing in force: writing
        it away here would make the next firewall pass restart DHCP again, in a loop.
        """
        if sharing is KEEP_SHARING:
            sharing = self.sharing_active
        name = device['name']
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', name):
            raise NetworkError('Invalid hotspot interface.')
        routes = [part for target in hotspot_route_targets(targets) for part in (target+'/32', '192.168.77.1')]
        # RFC3442 clients ignore option 3 when option 121 exists: preserve its default here.
        routes += ['0.0.0.0/0', '192.168.77.1']
        dns = (['port=53', 'listen-address=192.168.77.1', 'except-interface=lo', 'no-resolv', 'no-hosts',
                'domain-needed', 'bogus-priv'] + ['server='+ip+'@'+sharing['uplink'] for ip in sharing['dns']]) if sharing else ['port=0']
        text = '\n'.join(dns + [ 'interface='+name, 'bind-dynamic', 'user=root',
            'dhcp-range=192.168.77.10,192.168.77.100,255.255.255.0,1h',
            'dhcp-option=3,192.168.77.1', 'dhcp-option=6'+(',192.168.77.1' if sharing else ''), 'dhcp-authoritative',
            'dhcp-option=121,'+','.join(routes),
            'dhcp-leasefile='+str(self.state_dir/'hotspot.leases'), ''])
        path = self.state_dir/'dnsmasq.conf'
        previous = path.read_text() if path.exists() else None
        if previous == text:
            return False
        staged = self.state_dir/'dnsmasq.conf.pending'
        self._private_text(staged.name, text)
        try:
            run(['/usr/sbin/dnsmasq', '--test', '--conf-file='+str(staged)], timeout=5)
            os.replace(staged, path)
            if restart:
                try:
                    # SIGHUP does not reread dnsmasq.conf. Restart DHCP alone.
                    run(['systemctl', 'restart', 'wifi-bridge-dhcp'])
                except NetworkError:
                    if previous is not None:
                        self._private_text(path.name, previous)
                        run(['systemctl', 'restart', 'wifi-bridge-dhcp'], check=False)
                    else:
                        path.unlink(missing_ok=True)
                    raise
        finally:
            staged.unlink(missing_ok=True)
        return True

    def hotspot_name(self):
        try:
            lines = (self.state_dir / 'hostapd.conf').read_text().splitlines()
            return bytes.fromhex(next(line[6:] for line in lines if line.startswith('ssid2='))).decode()
        except (OSError, ValueError, StopIteration):
            raise NetworkError('The current hotspot name could not be read.') from None

    def _wait_nm_unmanaged(self, name):
        # A successful set request is not proof NetworkManager has released the radio.
        for attempt in range(8):
            state = run(['nmcli', '-g', 'GENERAL.STATE', 'device', 'show', name], timeout=1, check=False)
            if state.split(' ', 1)[0] == '10':
                return
            if attempt < 7:
                time.sleep(.25)
        raise NetworkError('NetworkManager did not release the hotspot interface.')

    def _wait_station_manager_gone(self, name):
        """Give the station manager a moment to really let go of a released radio.

        NetworkManager reports a radio unmanaged before wpa_supplicant has
        dropped it, and dropping a radio puts it back in station mode. An
        access point started in between is refused its keys, or starts and
        then silently loses its radio. On radios with a separate peer-to-peer
        device (the onboard Broadcom radio), that device exists exactly as
        long as wpa_supplicant holds the radio: wait for it to go, a few
        seconds at most, then start anyway. The checks after the start still
        decide.
        """
        marker = re.compile(r'^\s*Interface\s+p2p-dev-' + re.escape(name) + r'\s*$', re.M)
        for attempt in range(16):
            try: listing = run(['iw', 'dev'], timeout=2, check=False)
            except NetworkError: return
            if not isinstance(listing, str) or not marker.search(listing): return
            if attempt < 15: time.sleep(.25)

    def _wait_hotspot_ready(self, device):
        for attempt in range(8):
            if self.connected('setup', device):
                return
            if attempt < 7:
                time.sleep(.25)
        raise NetworkError('The hotspot or its DHCP server did not start.')

    # Seconds during which a newly started AP must keep its radio in AP mode.
    AP_SETTLE_SECONDS = 8

    def _left_ap_mode(self, name):
        # Only an explicit other interface type counts; an unavailable
        # observation is left to the reconcile loop.
        info = run(['iw', 'dev', name, 'info'], timeout=2, check=False)
        if not isinstance(info, str):
            return False
        types = re.findall(r'^\s*type\s+(\S+)\s*$', info, re.M)
        return bool(types) and types != ['AP']

    def _wait_hotspot_stable(self, device):
        """Keep the AP only if its radio stays in AP mode after the start.

        Releasing a radio from NetworkManager also makes wpa_supplicant drop it,
        asynchronously, and dropping a radio restores station mode. When that
        happens after hostapd started, hostapd keeps running and reports its AP
        enabled while nothing is broadcast. Watch the radio for a few seconds and
        restart the AP service once; a second loss is a start failure.
        """
        for restart in (True, False):
            for _ in range(int(self.AP_SETTLE_SECONDS / .25)):
                time.sleep(.25)
                if self._left_ap_mode(device['name']):
                    break
            else:
                return
            if not restart:
                raise NetworkError('The hotspot radio left access-point mode after starting.')
            run(['systemctl', 'restart', 'wifi-bridge-ap'])
            self._wait_hotspot_ready(device)

    AP_LOG = Path('/var/log/wifi-bridge-ap.log')

    def ap_diagnosis(self, name):
        """Why an access point did not take, in one line without names or passwords.

        Radio type (AP or managed), whether the station manager still holds the radio
        (its peer-to-peer device exists), NetworkManager's device state, hostapd's own
        state, what hostapd logged since its last start, and Broadcom driver errors.
        """
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', name or ''):
            return 'radio unknown'
        parts = []
        def read(command):
            try:
                value = run(command, timeout=2, check=False)
                return value if isinstance(value, str) else ''
            except NetworkError:
                return ''
        types = re.findall(r'^\s*type\s+(\S+)\s*$', read(['iw', 'dev', name, 'info']), re.M)
        parts.append('radio type ' + (types[0] if types else 'unknown'))
        listing = read(['iw', 'dev'])
        parts.append('station manager ' + ('still holds it' if re.search(r'^\s*Interface\s+p2p-dev-' + re.escape(name) + r'\s*$', listing, re.M)
                                            else 'released it' if listing else 'unknown'))
        state = read(['nmcli', '-g', 'GENERAL.STATE', 'device', 'show', name]).split(' ', 1)[0]
        parts.append('NetworkManager state ' + (state if state.isdigit() else 'unknown'))
        hostapd = re.findall(r'^state=([A-Z_]+)$', read(['hostapd_cli', '-p', '/run/wifi-bridge-hostapd', '-i', name, 'status']), re.M)
        parts.append('hostapd ' + (hostapd[0] if hostapd else 'not answering'))
        try:
            with self.AP_LOG.open('rb') as stream:
                stream.seek(0, 2); stream.seek(max(0, stream.tell() - 16384))
                text = stream.read().decode('utf-8', 'replace')
            text = text[text.rfind('Configuration file:'):] if 'Configuration file:' in text else text
            seen = []
            for label, pattern in (('AP-ENABLED', r'AP-ENABLED'), ('AP-DISABLED', r'AP-DISABLED'),
                                   ('keys refused', r'key not allowed'), ('channel refused', r'[Cc]ould not set channel'),
                                   ('terminated', r'CTRL-EVENT-TERMINATING')):
                count = len(re.findall(pattern, text))
                if count: seen.append(label + ' ' + str(count))
            last = re.findall(r'interface state ([A-Z_]+)->([A-Z_]+)', text)
            if last: seen.append('last state ' + last[-1][0] + '->' + last[-1][1])
            parts.append('hostapd log since its start: ' + (', '.join(seen) if seen else 'nothing notable'))
        except OSError:
            parts.append('hostapd log unreadable')
        driver = read(['dmesg'])
        errors = len(re.findall(r'brcmf_\w+.*fail', driver[-65536:]))
        if errors: parts.append('driver errors ' + str(errors))
        return name + ': ' + ', '.join(parts)

    def hotspot(self, device, ssid, psk, targets=()):
        # psk is the derived 64-hex WPA key (password.py): the bridge stores and passes
        # only that. A passphrase given by a direct caller is derived here, in memory.
        # NetworkManager/wpa_supplicant AP mode rejected valid WPA2 clients on the
        # physical Zero W. A dedicated hostapd WPA2/CCMP AP completed the handshake.
        # A stale or unverified owner may still have clients. Reconciliation may
        # release it only after independently proving that both services stopped.
        self.last_ap_diagnosis = None
        owned = self._hotspot_device()
        if owned and not self.connected('setup', {'name': owned}):
            raise NetworkError('The previous hotspot owner is not safely released.')
        self.disconnect("setup")
        name = device["name"]
        if not re.fullmatch(r"[0-9a-f]{64}", psk or ""):
            from .password import wpa_psk
            psk = wpa_psk(psk, ssid)
        self._private_text("hostapd.conf", "\n".join([
            "interface=" + name, "driver=nl80211", "ssid2=" + ssid.encode().hex(),
            "country_code=" + self.country_code, "hw_mode=g", "channel=6",
            "ieee80211n=1", "wmm_enabled=1", "auth_algs=1", "wpa=2",
            "wpa_key_mgmt=WPA-PSK", "rsn_pairwise=CCMP", "ieee80211w=0",
            "wpa_psk=" + psk, "ctrl_interface=/run/wifi-bridge-hostapd", ""]))
        self.hotspot_dhcp(device, targets, restart=False)
        # Persist ownership before changing the radio so partial failures can be
        # recovered after an application restart. Client credentials never enter argv.
        atomic_json(self.state_dir / "hotspot.json", {"interface": name, **({"mac": device["mac"]} if device.get("mac") else {})})
        try:
            run(["nmcli", "--wait", "5", "connection", "down", "id", "wifi-bridge-setup"], check=False)
            run(["nmcli", "device", "set", name, "managed", "no"])
            self._wait_nm_unmanaged(name)
            self._wait_station_manager_gone(name)
            run(["ip", "address", "replace", "192.168.77.1/24", "dev", name])
            run(["ip", "link", "set", name, "up"])
            run(["systemctl", "restart", "wifi-bridge-ap", "wifi-bridge-dhcp"])
            self._wait_hotspot_ready(device)
            self._wait_hotspot_stable(device)
        except NetworkError:
            # What the radio looked like when it failed, before the teardown changes it.
            self.last_ap_diagnosis = self.ap_diagnosis(name)
            # Release our partial AP before the controller tries another radio.
            # If cleanup itself fails, retain ownership and abort safely.
            self.disconnect("setup")
            raise

    def ap_vacant(self, device):
        """Current raw station observation, including unauthenticated stations."""
        try:
            if not self.connected('setup', device): return False
            raw = run(['iw','dev',device['name'],'station','dump'], timeout=3)
            # Only an actually empty successful kernel reply proves vacancy.
            return not raw.strip()
        except NetworkError:
            return False

    def hotspot_clients(self, device):
        """Return associated stations, enriched only by current matching DHCP leases."""
        try:
            stations = run(["iw", "dev", device["name"], "station", "dump"], timeout=3)
        except NetworkError:
            return {"clients": [], "clients_available": False}
        leases = {}
        try:
            for line in (self.state_dir / "hotspot.leases").read_text().splitlines():
                parts = line.split()
                if len(parts) < 4:
                    continue
                expires, mac, address, hostname = parts[:4]
                try:
                    if int(expires) != 0 and int(expires) <= time.time():
                        continue
                    if ipaddress.ip_address(address) not in ipaddress.ip_network("192.168.77.0/24"):
                        continue
                except ValueError:
                    continue
                leases[mac.lower()] = {"ip": address, "hostname": "" if hostname == "*" else hostname[:253]}
        except (OSError, UnicodeError):
            pass
        clients = []
        for match in re.finditer(r"^Station ([0-9a-fA-F:]{17})\b(.*?)(?=^Station |\Z)", stations, re.M | re.S):
            mac = match[1].lower()
            if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
                continue
            if re.search(r"^\s*(?:authorized|associated):\s*no\s*$", match[2], re.M):
                continue
            clients.append({"mac": mac, **leases.get(mac, {"ip": "", "hostname": ""})})
        return {"clients": clients, "clients_available": True}

    def identity(self, hostname, devices, settings=None, runtime=None):
        from .runtime import platform
        deployment = platform()
        if deployment not in ("raspberry", "live-usb"):
            if deployment != 'native':
                return False
            # The Pi 5 images are intentionally still classified native; their
            # root-owned image marker and the device-tree model gate discovery.
            from .cameras.discovery import pi5_appliance
            if not pi5_appliance():
                return False
        from .announce import update
        changed = update(Path('/etc/avahi'), hostname, devices, settings, runtime, run)
        return self.keep_announcing() or changed

    def keep_announcing(self, now=None):
        """Start the announcement daemon again when it is not running.

        Its files are written only when they change, so a daemon that stopped,
        or that no runlevel starts, would otherwise stay silent with correct
        files. Checked twice a minute at most; True when it had to be started.
        """
        now = time.monotonic() if now is None else now
        if now < self.announce_check: return False
        self.announce_check = now + 30
        try:
            run(['avahi-daemon', '--check'], timeout=3)
            return False
        except NetworkError:
            # Never a reason to fail a reconcile: tried again at the next check.
            try: run(['systemctl', 'restart', 'avahi-daemon'])
            except NetworkError: return False
            return True

    def country(self, country):
        self.country_code = country
        run(["iw", "reg", "set", country])
        run(["rfkill", "unblock", "wifi"])
        run(["nmcli", "radio", "wifi", "on"])

    def probe(self, ip, device):
        # Read-only TCP handshake; never sends telescope commands.
        try:
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device["name"].encode() + b"\0")
                sock.settimeout(2)
                sock.connect((ip, 8082))
            return True
        except OSError:
            return False

    def wired_home(self):
        from .wired import home
        try:
            return home(run, split_nm)
        except (OSError, ValueError, NetworkError):
            return None

    def management_paths(self, home=None, setup=None):
        return [{'role':role,'device':device,'address':self.address(device)}
                for role,device in (('home',home),('ap',setup)) if device]

    def sharing_uplink(self, home, setup, private_lan=False):
        """Only actual owned home/default/DNS observations can admit AP forwarding."""
        if not setup:return None,'HOTSPOT_UNAVAILABLE'
        if not home:return None,'UPLINK_UNAVAILABLE'
        try:
            for device in (home,setup):
                if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}',device['name']):raise ValueError()
            lan=ipaddress.IPv4Interface(self.address(home,timeout=2))
            ap=ipaddress.IPv4Interface(self.address(setup,timeout=2))
            if home['name']==setup['name'] or str(ap)!='192.168.77.1/24' or lan.network.overlaps(ap.network):
                return None,'SUBNET_CONFLICT'
            raw=run(['nmcli','-t','-f','GENERAL.CONNECTION,IP4.GATEWAY,IP4.DNS','device','show',home['name']],timeout=3)
            if len(raw)>4096:raise ValueError()
            if re.findall(r'^GENERAL\.CONNECTION:(.*)$',raw,re.M)!=['wifi-bridge-wired' if home.get('kind') == 'ethernet' else 'wifi-bridge-home']:raise ValueError()
            gateways=re.findall(r'^IP4\.GATEWAY:(.*)$',raw,re.M)
            if len(gateways)!=1:raise ValueError()
            gateway=ipaddress.IPv4Address(gateways[0])
            if gateway not in lan.network or gateway in (lan.ip,lan.network.network_address,lan.network.broadcast_address):raise ValueError()
            routes=json.loads(run(['ip','-j','-4','route','show','default'],timeout=2) or '[]')
            if (not isinstance(routes,list) or len(routes)!=1 or routes[0].get('dst')!='default'
                    or routes[0].get('gateway')!=str(gateway) or routes[0].get('dev')!=home['name']):raise ValueError()
            resolvers=re.findall(r'^IP4\.DNS(?:\[[0-9]+\])?:(.*)$',raw,re.M)
            if not 1<=len(resolvers)<=3:return None,'DNS_UNAVAILABLE'
            dns=[]
            for value in resolvers:
                address=ipaddress.IPv4Address(value)
                if (address.is_unspecified or address.is_multicast or address.is_loopback or address.is_link_local
                        or address in ap.network or address in (lan.ip,lan.network.network_address,lan.network.broadcast_address)):
                    return None,'DNS_UNAVAILABLE'
                if str(address) not in dns:dns.append(str(address))
            return {'uplink':home['name'],'network':str(lan.network),'gateway':str(gateway),
                    'dns':tuple(dns),'private_lan':bool(private_lan)},'UPLINK_CONFIGURED'
        except (OSError,ValueError,TypeError,KeyError,NetworkError):return None,'UPLINK_UNAVAILABLE'

    def withdraw_sharing(self):
        """Atomic removal of sharing permissions, including after process restart.

        Separate owned chains leave telescope rules/counters/conntrack untouched.
        Older tables lack these chains and cannot have this sharing capability.
        """
        script=('flush chain ip wifi_bridge sharing_forward\nflush chain ip wifi_bridge sharing_srcnat\n'
                'flush chain ip wifi_bridge dns_input\n'
                'add rule ip wifi_bridge dns_input ip daddr 192.168.77.1 udp dport 53 drop\n'
                'add rule ip wifi_bridge dns_input ip daddr 192.168.77.1 tcp dport 53 drop\n')
        try:run(['nft','-f','-'],input=script)
        except NetworkError:
            if self.sharing_active:raise
        self.sharing_active=None;self.last_firewall=None

    def firewall(self, home=None, telescope=None, target="10.0.0.1", setup=None, telescopes=None, isolated=None, management=None, sharing=False, private_lan=False, hotspot_targets=None):
        try:
            return self._firewall(home,telescope,target,setup,telescopes,isolated,management,sharing,private_lan,hotspot_targets)
        except Exception:
            self.withdraw_sharing()
            raise

    def _firewall(self, home=None, telescope=None, target="10.0.0.1", setup=None, telescopes=None, isolated=None, management=None, sharing=False, private_lan=False, hotspot_targets=None):
        """One atomic owned-table plan for up to two isolated management ingresses."""
        entries = telescopes if telescopes is not None else ([{'device':telescope,'target':target,'legacy_ports':True}] if telescope else [])
        owned_ports=set()
        for spec in relay_plugins():
            flag=proxy_flag(spec['id']);owners=[e for e in entries if e.get(flag)]
            if len(owners)>1:raise NetworkError('Native proxy ports conflict.')
            # Historical Vespera/Seestar fixtures omitted kind; retain that old
            # internal shorthand, while new adapters require their declared kind.
            if any(e.get('kind',spec['id'] if spec['id'] in ('vespera','seestar','dwarf') else None)!=spec['id'] for e in owners):raise NetworkError('Invalid proxy type.')
            if owners:
                ports={(spec['relay']['transport'],p) for p in spec['relay']['ports']}
                if owned_ports&ports:raise NetworkError('Native proxy ports conflict.')
                owned_ports|=ports
        paths = self.management_paths(home,setup) if management is None else management
        if len(paths)>2: raise NetworkError('Too many management interfaces.')
        rules, drops, dstnat, srcnat, discovery_output = [], [], [], [], []
        udp_relays = []
        occupied = []
        for path in paths:
            interface = ipaddress.IPv4Interface(path['address'])
            if any(interface.network.overlaps(n) for n in occupied): raise NetworkError('Management subnets overlap.')
            occupied.append(interface.network)
        for entry in entries:
            scope=entry['device'];scope_ip=ipaddress.IPv4Interface(self.address(scope));address=ipaddress.IPv4Address(entry['target'])
            if any(scope_ip.network.overlaps(n) for n in occupied):raise NetworkError('Management and telescope subnets overlap.')
            if address not in scope_ip.network or address in (scope_ip.ip,scope_ip.network.network_address,scope_ip.network.broadcast_address):raise NetworkError('Invalid telescope address.')
            occupied.append(scope_ip.network)
            t=scope['name']
            for path in paths:
                ingress=ipaddress.IPv4Interface(path['address']);h=path['device']['name']
                source=f'ip saddr {ingress.network} '
                rules += [f'iifname "{h}" oifname "{t}" {source}ip daddr {address} accept',
                          # A reply leaves by the route to the client, which may be another
                          # interface on the same network (cable and Wi-Fi together).
                          f'iifname "{t}" ip daddr {ingress.network} ct state established,related accept']
                selected=next((spec for spec in relay_plugins() if entry.get(proxy_flag(spec['id']))),None)
                if selected:
                    relay=selected['relay'];ports=relay['ports']
                    port_set='{ '+', '.join(map(str,ports))+' }' if len(ports)>1 else str(ports[0])
                    dstnat.append(f'iifname "{h}" {source}ip daddr {ingress.ip} {relay["transport"]} dport {port_set} dnat ip to {address}')
                    if relay['transport']=='udp':udp_relays += [(str(ingress.ip),port) for port in ports]
                if selected and selected['discovery']['engine']=='synscan_version':
                    # A local broadcast reply from Pi:11880 must not create a
                    # reverse conntrack flow that captures this client's later
                    # unicast commands before DNAT. Forwarded mount traffic does
                    # not traverse OUTPUT and remains tracked/protected normally.
                    discovery_output.append(f'oifname "{h}" ip saddr {ingress.ip} ip daddr {ingress.network} udp sport 11880 notrack')
            if paths:srcnat.append(f'oifname "{t}" ip daddr {address} masquerade')
            drops += [f'iifname "{t}" drop',f'oifname "{t}" drop']
        uplink,reason=self.sharing_uplink(home,setup,private_lan) if sharing else (None,'DISABLED')
        # A resolver routed through an instrument must never be treated as an uplink.
        if uplink and any(ipaddress.ip_address(ip) in subnet for ip in uplink['dns'] for subnet in occupied[len(paths):]):
            uplink,reason=None,'DNS_UNAVAILABLE'
        if uplink and any(ipaddress.ip_network(uplink['network']).overlaps(n) for n in occupied[len(paths):]):
            uplink,reason=None,'SUBNET_CONFLICT'
        for device in [p['device'] for p in paths]+list(isolated or [])+([setup] if setup else []):
            drops += [f'iifname "{device["name"]}" drop',f'oifname "{device["name"]}" drop']
        first=paths[0] if paths else None
        self.configure_activity(entries if paths else [],first['device'] if first else None,ipaddress.ip_interface(first['address']) if first else None,management=paths)
        def script_for(access):
            permits=[];extra_nat=[];dns_rules=[]
            if setup:
                # Even a stale dnsmasq must not serve non-AP clients or isolated modes.
                if access:
                    dns_rules += [f'iifname "{setup["name"]}" ip saddr 192.168.77.0/24 ip daddr 192.168.77.1 udp dport 53 accept',
                                  f'iifname "{setup["name"]}" ip saddr 192.168.77.0/24 ip daddr 192.168.77.1 tcp dport 53 accept']
                dns_rules += ['ip daddr 192.168.77.1 udp dport 53 drop','ip daddr 192.168.77.1 tcp dport 53 drop']
            if not setup:dns_rules=['ip daddr 192.168.77.1 udp dport 53 drop','ip daddr 192.168.77.1 tcp dport 53 drop']
            if access:
                ap=setup['name'];h=home['name'];prefix=f'iifname "{ap}" oifname "{h}" ip saddr 192.168.77.0/24 '
                denied=['0.0.0.0/8','127.0.0.0/8','169.254.0.0/16','224.0.0.0/4','240.0.0.0/4','192.168.77.0/24']
                denied += [str(n) for n in occupied[len(paths):]]
                permits += [prefix+'ip daddr { '+', '.join(dict.fromkeys(denied))+' } drop',
                            prefix+f'ip daddr {ipaddress.ip_network(access["network"]).broadcast_address} drop']
                reverse=f'iifname "{h}" oifname "{ap}" ip daddr 192.168.77.0/24 '
                permits.append(reverse+'ip saddr { '+', '.join(dict.fromkeys(denied+[str(ipaddress.ip_network(access["network"]).broadcast_address)]))+' } drop')
                if access['private_lan']:
                    permits += [prefix+'ip daddr '+access['network']+' ct state new,established,related accept',
                                reverse+'ip saddr '+access['network']+' ct state established,related accept']
                # Internet-only excludes directly attached home even if it uses public addressing.
                restricted=['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','100.64.0.0/10',access['network']]
                permits += [prefix+'ip daddr { '+', '.join(dict.fromkeys(restricted))+' } drop',
                            prefix+'ct state new,established,related accept',
                            reverse+'ip saddr { '+', '.join(dict.fromkeys(restricted))+' } drop',
                            reverse+'ct state established,related accept']
                extra_nat=[f'iifname "{ap}" oifname "{h}" ip saddr 192.168.77.0/24 masquerade']
            nat=('chain dstnat { type nat hook prerouting priority dstnat; policy accept;\n'+'\n'.join(dstnat)+
                 '\n}\nchain sharing_srcnat {\n'+'\n'.join(extra_nat)+'\n}\n'
                 'chain srcnat { type nat hook postrouting priority srcnat; policy accept;\n'+
                 '\n'.join(dict.fromkeys(srcnat))+ '\njump sharing_srcnat\n}')
            input_chain='chain dns_input { type filter hook input priority -10; policy accept;\n'+'\n'.join(dns_rules)+'\n}\n'
            output_chain=('chain synscan_discovery_output { type filter hook output priority raw; policy accept;\n'+
                          '\n'.join(discovery_output)+'\n}\n') if discovery_output else ''
            return ('delete table ip wifi_bridge\ntable ip wifi_bridge {\n'+input_chain+output_chain+
                    'chain sharing_forward {\n'+'\n'.join(permits)+'\n}\n'+
                    'chain forward { type filter hook forward priority -10; policy accept;\n'+
                    '\n'.join(rules+['jump sharing_forward']+list(dict.fromkeys(drops)))+'\n}\n'+nat+'\n}\n')
        def publish(access):
            script=script_for(access)
            if script!=self.last_firewall:
                if self.last_firewall is None:run(['nft','add','table','ip','wifi_bridge'],check=False)
                run(['nft','-f','-'],input=script);self.last_firewall=script
                if self.telemetry:self.telemetry.replaced()
                self.forget_unrelayed_udp(udp_relays)
        # Withdraw old sharing BEFORE DNS/service changes. Telescope plans stay in every transaction.
        if uplink!=self.sharing_active:
            publish(None);self.sharing_active=None
        if setup and (hotspot_targets is not None or sharing):
            try:self.hotspot_dhcp(setup,hotspot_targets or (),sharing=uplink)
            except (OSError,ValueError,NetworkError):
                publish(None);self.sharing_active=None
                return {'available':False,'reason':'DNS_UNAVAILABLE'}
        publish(uplink);self.sharing_active=uplink
        return {'available':bool(uplink),'reason':reason}

    def forget_unrelayed_udp(self, relays):
        """Let a client that wrote to a relay port before the relay existed be relayed.

        Address translation is decided once per flow. A datagram that reached a
        relay port while its rule was absent (the instrument was not linked yet,
        or its link had dropped) leaves a flow delivered to the bridge itself,
        and every later datagram from the same client port follows it for as
        long as the client keeps writing. Only such untranslated flows to the
        bridge's own address and relay port are forgotten; relayed ones answer
        from the instrument's address and are left alone.
        """
        for address,port in dict.fromkeys(relays):
            try:run(['conntrack','-D','-p','udp','--orig-dst',address,'--orig-port-dst',str(port),'--reply-src',address],timeout=3,check=False)
            except NetworkError:pass

    def association_identity(self, device):
        """Active-object + NM process identity separates same-IP reconnections."""
        try:
            manager=networkmanager_process_identity()
            if not manager:return None
            command=['nmcli','-g','GENERAL.CON-PATH','device','show',device['name']]
            active=run(command,timeout=2)
            if not re.fullmatch('/org/freedesktop/NetworkManager/ActiveConnection/[0-9]+',active):return None
            link=run(['iw','dev',device['name'],'link'],timeout=2)
            from .link_observations import remember_link
            remember_link(self.link_cache, device['name'], link)
            match=re.search(r'^Connected to ([0-9a-fA-F:]{17})\b',link)
            if not match or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}',match[1]):return None
            if run(command,timeout=2)!=active or networkmanager_process_identity()!=manager:return None
            return manager+':'+active+':'+match[1].lower()
        except (OSError,IndexError,NetworkError):return None

    def fast_association_identity(self, device, timeout=1):
        """Same token via existing DBus CLI, with a strict total collection budget."""
        from .activity import bounded_read
        deadline=time.monotonic()+timeout
        def observe(args):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ValueError('Observation deadline.')
            return bounded_read(args,limit=4096,timeout=remaining)
        def object_path(raw,prefix):
            paths=re.findall(r'\bobject path "([^"]+)"',raw)
            if len(paths)!=1 or not re.fullmatch(re.escape(prefix)+r'/[0-9]+',paths[0]):raise ValueError('Unknown object.')
            return paths[0]
        bus=['dbus-send','--system','--print-reply','--dest=org.freedesktop.DBus','/org/freedesktop/DBus']
        owner_command=bus+['org.freedesktop.DBus.GetNameOwner','string:org.freedesktop.NetworkManager']
        def owner():
            values=re.findall(r'^\s*string "(:[0-9]{1,10}\.[0-9]{1,10})"\s*$',observe(owner_command),re.M)
            if len(values)!=1:raise ValueError('Unknown bus owner.')
            return values[0]
        try:
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}',device['name']):return None
            unique=owner()
            values=re.findall(r'^\s*uint32 ([1-9][0-9]{0,9})\s*$',observe(bus+
                ['org.freedesktop.DBus.GetConnectionUnixProcessID','string:'+unique]),re.M)
            if len(values)!=1:return None
            manager=networkmanager_owned_process_identity(values[0])
            if not manager:return None
            base=['dbus-send','--system','--print-reply','--dest='+unique]
            device_path=object_path(observe(base+['/org/freedesktop/NetworkManager',
                'org.freedesktop.NetworkManager.GetDeviceByIpIface','string:'+device['name']]),'/org/freedesktop/NetworkManager/Devices')
            command=base+[device_path,'org.freedesktop.DBus.Properties.Get',
                'string:org.freedesktop.NetworkManager.Device','string:ActiveConnection']
            active=object_path(observe(command),'/org/freedesktop/NetworkManager/ActiveConnection')
            link=observe(['iw','dev',device['name'],'link'])
            match=re.search(r'^Connected to ((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})\b',link)
            if not match or object_path(observe(command),'/org/freedesktop/NetworkManager/ActiveConnection')!=active:return None
            if networkmanager_owned_process_identity(values[0])!=manager or owner()!=unique or time.monotonic()>=deadline:return None
            return manager+':'+active+':'+match[1].lower()
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):return None

    def configure_activity(self, entries, home, home_ip, management=None):
        if not self.telemetry and not any(e.get('profile_id') for e in entries):return
        try:
            if self.telemetry is None:
                from .activity import ForwardActivity
                self.telemetry=ForwardActivity(self.state_dir)
            paths=management if management is not None else ([{'role':'home','device':home,'address':str(home_ip)}] if home else [])
            prepared=[]
            for entry in entries:
                if not entry.get('profile_id'):continue
                scope=entry['device'];address=ipaddress.IPv4Interface(self.address(scope));token=self.association_identity(scope)
                for path in paths:
                    ingress=ipaddress.ip_interface(path['address'])
                    prepared.append((entry['profile_id'],token,str(entry['target']),str(address.ip),str(ingress.ip),str(ingress.network),
                        any(entry.get(proxy_flag(p['id'])) for p in relay_plugins()),path['device']['name'],scope['name'],entry.get('kind')))
            identity=tuple(sorted(prepared,key=repr))
            # Revalidate real association identities and addresses every time.
            # An identical verified path still has its old installed rules; do
            # not publish a fictitious transition or rewrite its private catalog.
            if (self.telemetry.configured and not self.telemetry.catalog_error and not self.telemetry.catalog.get('coverage_lost')
                    and all(row[1] for row in prepared) and self.telemetry.plan_identity==identity):return
            self.telemetry.plan()
            for row in prepared:self.telemetry.attach(*row[:-1],kind=row[-1])
            self.telemetry.configure(entries,lambda script:run(['nft','-f','-'],input=script))
            if self.telemetry.configured and not self.telemetry.catalog.get('coverage_lost') and not self.telemetry.catalog_error:
                self.telemetry.plan_identity=identity
        except (OSError,ValueError,NetworkError):
            if self.telemetry:
                self.telemetry.plan();self.telemetry.catalog['coverage_lost']=True

    def sample_activity(self, *, force=False, policy=None):
        if self.isolated and policy: self.isolated.policy = dict(policy)
        if self.telemetry:
            policy=policy or {'quiet_seconds':120,'cooldown_seconds':60}
            self.telemetry.sample(force=force,quiet_seconds=policy['quiet_seconds'],cooldown_seconds=policy['cooldown_seconds'])

    def selection_activity(self, profile, device, home):
        """Only a current association catalog entry may supply idle evidence."""
        unknown = {'state':'unknown','reason':'ASSOCIATION_NOT_MEASURED','sample_age_seconds':16}
        if self.isolated and self.isolated.record_for(device):
            return self.isolated.activity().get(profile['id'], unknown)
        if not self.telemetry: return unknown
        token = self.association_identity(device)
        if not token: return unknown
        scope=ipaddress.ip_interface(self.address(device))
        signature=self.telemetry.instrument_signature(token,profile['target_ip'],str(scope.ip),device['name'])
        mark=self.telemetry.current.get(profile['id']);record=self.telemetry.catalog['records'].get(str(mark),{})
        if record.get('instrument_signature')!=signature:return unknown
        return self.activity_status()['profiles'].get(profile['id'], unknown)

    def link_observations(self, rows):
        """Private normalized cached evidence; collector never executes commands."""
        result = {}
        for ident, row in rows.items():
            radio = self.link_cache.get(row.get('interface'), {})
            result[ident] = {'radio': dict(radio) if time.monotonic()-radio.get('observed', -100) <= 30 else {}}
        if self.telemetry:
            activity = self.telemetry.status()['profiles']
            for ident, value in list(self.telemetry.values.items()):
                if ident not in rows: continue
                if (activity.get(ident, {}).get('sample_age_seconds', 16) > 15
                        or value.get('generation') != self.telemetry.generation): continue
                mark = self.telemetry.current.get(ident)
                record = self.telemetry.catalog['records'].get(str(mark), {})
                radio = self.link_cache.get(record.get('device'), {})
                result[ident] = {'activity': dict(activity.get(ident, {}), **{
                    key:value[key] for key in ('bytes_to_instrument','bytes_from_instrument') if key in value}),
                    'generation': value.get('group'),
                    'radio': dict(radio) if time.monotonic()-radio.get('observed', -100) <= 30 else {}}
        if self.isolated:
            for ident, value in self.isolated.activity().items():
                if ident not in rows: continue
                record = next((r for r in list(self.isolated.records.values()) if r['profile_id'] == ident), {})
                radio = record.get('link_observation', {})
                result[ident] = {'activity': value, 'generation': record.get('intent'),
                    'radio': dict(radio) if time.monotonic()-radio.get('observed', -100) <= 30 else {}}
        return result

    def activity_status(self):
        result = self.telemetry.status() if self.telemetry else {'supported':False,'profiles':{},'reason':'COLLECTOR_NOT_READY','idle_selection_implemented':True}
        if self.isolated:
            result['profiles'].update(self.isolated.activity())
            if self.isolated.activity(): result['supported'] = True
        return result

    def linux_password(self, password):
        """Change the Linux account of this image; return an undo callable or None."""
        from .runtime import pi5_appliance
        if pi5_appliance():
            # Pi 5 image: root, through stdin only; astro-ssh restarts with the new state.
            from .linux_account import change_root_password
            return change_root_password(password, lambda args, input=None: run(args, timeout=40, input=input))
        # The one-shot helper can only change bridge; neither account nor command
        # comes from HTTP input. PAM receives the secret through stdin.
        self._private_text("linux-password.pending", password)
        try:
            run(["systemctl", "start", "wifi-bridge-password"], timeout=40)
        finally:
            (self.state_dir / "linux-password.pending").unlink(missing_ok=True)

    def reboot(self):
        run(["systemctl", "reboot"])


class DemoNetwork:
    """Explicit simulation: never runs a command or touches host networking."""
    def __init__(self, radios=2):
        self.radios = radios
        self.active = set()
        self.associations = {}
        self.instrument_addresses = {}
        self.ap_device = ""
        self.links = {}
        self.isolated = None

    def isolated_endpoints(self, entries, rows, paths):
        from .isolated import IsolatedRadios
        if self.isolated is None:
            self.isolated = IsolatedRadios.__new__(IsolatedRadios)
            self.isolated.relays = {}; self.isolated.counters = {}; self.isolated.records = {}; self.isolated.ping_at = {}
            self.isolated.udp_owners = {}
            self.isolated.policy = {'quiet_seconds':120, 'cooldown_seconds':60}
        self.isolated.sync(entries, rows, paths, demo=True)

    def devices(self):
        return ([{"name": "wlan0", "mac": "b8:27:eb:12:ab:cd", "usb": False, "bands": ["2.4"], "label": "wlan0 · built-in"},
                {"name": "wlan1", "mac": "00:11:22:33:44:55", "usb": True, "bands": ["2.4", "5"], "label": "wlan1 · USB"}] +
                [{"name": "wlan" + str(i), "mac": "02:00:00:00:00:" + f"{i:02x}",
                  "usb": True, "bands": ["2.4", "5"], "label": "wlan" + str(i) + " · USB"}
                 for i in range(2, self.radios)])

    def scan_station(self, device, home, role="home"):return self.connected(role,device,home) and self.ap_device!=device['name']

    def cached_scan(self, name):return self.scan(name)

    def radio_idle(self, device):
        return device['mac'] not in self.associations.values() and self.ap_device != device['name']

    def link_observation(self, device, profile):
        from .config import role_for
        value = self.links.get(device['mac'])
        return {'bssid': value['bssid']} if value and value['ssid'] == profile['ssid'] and self.connected(role_for(profile), device) else None

    def discovery_associated(self, device, profile):
        from .config import role_for
        return self.connected(role_for(profile), device, profile)

    def discover_seestar(self, device, profile):
        # Demo never fabricates manufacturer identity or network replies.
        return None

    def discover_mount(self, device, profile):
        return None

    def scan(self, name, rescan=True, timeout=None):
        return [{"ssid": "Telescope-DEMO", "bssid": "02:00:00:11:22:33", "signal": 92, "frequency": 2412, "security": "WPA2"},
                {"ssid": "Maison", "bssid": "02:00:00:44:55:66", "signal": 81, "frequency": 5180, "security": "WPA2"}]

    def connect(self, role, device, config):
        if config["ssid"] == "ECHEC":
            raise NetworkError("Simulation: connection refused.")
        for other, mac in list(self.associations.items()):
            if mac == device["mac"]:
                self.disconnect(other)
        self.associations[role] = device["mac"]
        self.links[device['mac']] = {'ssid': config['ssid'], 'bssid': config.get('bssid') or '02:00:00:11:22:33'}
        self.active.add(role)
        if config.get("target_ip"):
            target = ipaddress.IPv4Address(config["target_ip"])
            subnet = ipaddress.IPv4Network(str(target) + "/24", strict=False)
            client = next(ip for ip in subnet.hosts() if ip != target)
            self.instrument_addresses[device["mac"]] = str(client) + "/24"

    def connected(self, role, device, config=None):
        return bool(device) and role in self.active and self.associations.get(role) == device["mac"]

    def known_telescope_roles(self):
        return {r for r in self.active if r.startswith("telescope")}

    def remove_profile(self, role):
        self.disconnect(role)

    def disconnect(self, role):
        self.active.discard(role)
        mac = self.associations.pop(role, None)
        if mac not in self.associations.values():
            self.instrument_addresses.pop(mac, None)
            self.links.pop(mac, None)

    def address(self, device, timeout=20):
        if self.connected('setup',device):return '192.168.77.1/24'
        if device["mac"] in self.instrument_addresses:
            return self.instrument_addresses[device["mac"]]
        if device["name"] == "wlan1":
            return "192.168.1.42/24"
        if device["name"] == "wlan0":
            return "10.0.0.2/24"
        return "192.168." + str(int(device["name"][4:]) + 2) + ".2/24"

    def hotspot_name(self):
        return self.ap_ssid

    def release_stale_hotspot(self, devices):
        return False

    def hotspot(self, device, ssid, psk, targets=()):
        self.disconnect("setup")
        self.connect("setup", device, {"ssid": ssid})
        self.ap_ssid = ssid

    def hotspot_dhcp(self, device, targets=(), *, restart=True): pass

    def ap_vacant(self, device):
        return False  # Demo's displayed synthetic phone remains connected.

    def management_paths(self, home=None, setup=None):
        return [{'role':role,'device':device,'address':self.address(device)} for role,device in (('home',home),('ap',setup)) if device]

    def hotspot_clients(self, device):
        return {"clients": [{"mac": "02:00:00:77:00:01", "ip": "192.168.77.12", "hostname": "Demo phone"}], "clients_available": True}

    def identity(self, *args, **kwargs): return False
    def country(self, *args): pass
    def firewall(self, *args, **kwargs): pass
    def probe(self, *args): return True
    def linux_password(self, password): pass
    def reboot(self): pass
