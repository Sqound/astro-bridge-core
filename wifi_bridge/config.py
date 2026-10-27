"""Validated configuration; secrets stay in root-only files, never in API replies."""
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
from .mounts import PROFILE_KINDS
from .plugins import demand_plugins, demand_flag


class Invalid(ValueError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        tmp.unlink(missing_ok=True)


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 260_000)
    return salt + ":" + digest.hex()


def password_matches(password, encoded):
    return hmac.compare_digest(password_hash(password, encoded.split(":")[0]), encoded)


CONNECTION_MODES = ('private', 'hotspot_isolated', 'hotspot_shared')


DEMAND_FLAGS = tuple(demand_flag(p['id']) for p in demand_plugins())
AUTO_POLICY_DEFAULTS = {'quiet_seconds':120, 'cooldown_seconds':60,'force_first_profile':False,
                        **dict.fromkeys(DEMAND_FLAGS,True)}
LEGACY_AUTO_POLICY_DEFAULTS = dict(AUTO_POLICY_DEFAULTS, **dict.fromkeys(DEMAND_FLAGS,False))


def auto_policy(config):
    # Missing flags in existing installations remain opt-outs. Only a new config
    # includes all supported types; its master switch still starts disabled.
    return dict(LEGACY_AUTO_POLICY_DEFAULTS, **config.get('auto_policy', {}))


def validated_auto_policy(data, old):
    value=data.get('auto_policy', {})
    if not isinstance(value,dict) or set(value)-set(AUTO_POLICY_DEFAULTS):raise Invalid('Unknown automatic connection policy fields.')
    policy=auto_policy(old);policy.update(value)
    for field in ('quiet_seconds','cooldown_seconds'):
        if type(policy[field]) is not int or not 30<=policy[field]<=3600:raise Invalid('Quiet/cooldown must be integer seconds from30 to3600.')
    for field in ('force_first_profile',*DEMAND_FLAGS):
        if type(policy[field]) is not bool:raise Invalid('Force and manufacturer demand policies must be booleans.')
    if 'acknowledge_interruption' in data and type(data['acknowledge_interruption']) is not bool:raise Invalid('Interruption acknowledgement must be a boolean.')
    if any(policy[field] and not auto_policy(old)[field] for field in ('force_first_profile',*DEMAND_FLAGS)) and data.get('acknowledge_interruption') is not True:
        raise Invalid('Explicitly acknowledge possible automatic connection interruption before enabling force or manufacturer demand.')
    return policy


def default_config(mac):
    suffix = mac.replace(":", "")[-6:].lower()
    return {"hostname": "astro-bridge-" + suffix, "country": "JP", "home": {},
            "telescope": {}, "telescope_ip": "10.0.0.1", "admin_hash": "", "schema_version": 2, "telescopes": [],
            "hotspot": {"enabled": False}, "connection_mode": "private", "hotspot_private_lan": False, "auto_connect": False, "auto_policy": dict(AUTO_POLICY_DEFAULTS)}


def reserved_hotspot_mac(config):
    hotspot = config.get('hotspot', {})
    return hotspot.get('mac', '') if hotspot.get('enabled', False) or config.get('connection_mode', 'private') != 'private' else ''


def hotspot_ssid(config, mac):
    """One SSID for hostapd and status; the suffix identifies the AP radio."""
    explicit = config.get('hotspot', {}).get('ssid', '')
    if explicit:
        return explicit
    suffix = mac.replace(':', '')[-6:].lower()
    name = config['hostname']
    # The factory hostname already contains a radio suffix. Do not duplicate it.
    if re.fullmatch(r'astro-bridge-[0-9a-f]{6}', name):
        name = 'astro-bridge'
    if name.endswith('-' + suffix):
        name = name[:-7]
    return name.encode()[:25].decode('utf-8', errors='ignore') + '-' + suffix


def profiles(config):
    """Normalize legacy single-telescope settings without changing credentials."""
    if "telescopes" in config and config["telescopes"]:
        return config["telescopes"]
    if config.get("telescope"):
        return [dict(config["telescope"], id="primary", name="Telescope", kind="vespera",
                     target_ip=config.get("telescope_ip", "10.0.0.1"), enabled=True)]
    return []


def network_intent(profile):
    if profile is None: return None
    return {key: profile.get(key) for key in ('id', 'name', 'ssid', 'password', 'mac', 'bssid', 'hidden', 'target_ip', 'kind', 'enabled')}


def role_for(profile):
    return "telescope" if profile["id"] == "primary" else "telescope-" + profile["id"]


def public_config(config):
    def redact(network):
        return {k: v for k, v in network.items() if k != "password"} | {
            "has_password": bool(network.get("password"))}
    return {"hostname": config["hostname"], "country": config["country"],
            "telescope_ip": config.get("telescope_ip", "10.0.0.1"),
            "configured": bool(config["home"]),
            "admin_configured": bool(config["admin_hash"]),
            "home": redact(config["home"]) if config["home"] else {}, "telescope": redact(config.get("telescope", {})),
            "telescopes": [redact(p) | {"auto_eligible": p.get("auto_eligible", False), "owner_locked": p.get("owner_locked", False), "auto_replaceable":p.get("auto_replaceable",False)} for p in profiles(config)],
            "connection_mode": config.get("connection_mode", "private"), "hotspot_private_lan": config.get("hotspot_private_lan", False),
            "hotspot": config.get("hotspot", {"enabled": False}), "auto_connect": config.get("auto_connect", False), "auto_policy":auto_policy(config)}


def text(value, maximum=128):
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Invalid("Invalid or oversized text.")
    return value


def private_ipv4(value):
    try:
        if not isinstance(value, str):
            raise ValueError()
        address = ipaddress.IPv4Address(value)
        if not address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
            raise ValueError()
        return str(address)
    except (ValueError, TypeError):
        raise Invalid("The telescope address must be a private IPv4 address.") from None


def validate_network(network, previous, available):
    if not isinstance(network, dict):
        raise Invalid("Invalid network.")
    ssid = text(network.get("ssid", ""), 32)
    if not 1 <= len(ssid.encode()) <= 32:
        raise Invalid("The SSID must contain 1 to 32 bytes.")
    mac = text(network.get("mac", ""), 17).lower()
    if mac not in available:
        raise Invalid("Select an available adapter or keep this profile assignment.")
    supplied = network.get("password")
    retained = supplied in (None, "") and previous.get("ssid") == ssid
    password = previous.get("password", "") if retained else text(supplied or "", 64)
    existing_open = retained and previous.get("password") == "" and "open" not in network
    if network.get("open") is True or existing_open:
        password = ""
    elif not (8 <= len(password.encode()) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", password)):
        raise Invalid("Wi-Fi password must contain 8 to 63 bytes, or select an open network.")
    bssid = text(network.get("bssid", ""), 17).lower()
    if bssid and not re.fullmatch(r"(?:[a-f0-9]{2}:){5}[a-f0-9]{2}", bssid):
        raise Invalid("Invalid BSSID.")
    return {"ssid": ssid, "password": password, "mac": mac, "bssid": bssid,
            "hidden": network.get("hidden") is True}


def validate_config(data, old, devices):
    if not isinstance(data, dict):
        raise Invalid("Invalid configuration.")
    hostname = text(data.get("hostname", ""), 63).lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname):
        raise Invalid("The hostname must contain letters, digits or hyphens, without .local.")
    country = text(data.get("country", "JP"), 2).upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise Invalid("Country must be a two-letter code, for example JP or FR.")
    present = {d["mac"].lower() for d in devices}
    legacy = "telescopes" not in data
    home_allowed = present | ({old["home"]["mac"]} if old.get("home") and not legacy else set())
    requested_home = data.get("home", {})
    # Only a genuinely absent initial network is optional. Never interpret an
    # empty/malformed edit as permission to forget an existing home or secret.
    home = {} if isinstance(requested_home, dict) and not requested_home and not old.get("home") else validate_network(requested_home, old.get("home", {}), home_allowed)
    entries = data.get("telescopes")
    if legacy:
        entries = [dict(data.get("telescope", {}), id="primary", name="Telescope", kind="vespera",
                        target_ip=data.get("telescope_ip", "10.0.0.1"), enabled=True)] if isinstance(data.get("telescope"), dict) else None
    if not isinstance(entries, list) or len(entries) > 24:
        raise Invalid("Invalid list. At most 24 telescope profiles are supported.")
    hotspot = data.get("hotspot", old.get("hotspot", {"enabled": False}))
    if not isinstance(hotspot, dict) or type(hotspot.get("enabled", False)) is not bool:
        raise Invalid("Invalid hotspot option.")
    hotspot_name = text(hotspot.get('ssid', ''), 32)
    if len(hotspot_name.encode()) > 32 or hotspot_name and not hotspot_name.strip():
        raise Invalid("The hotspot name must contain 1 to 32 bytes, or be empty for the automatic name.")
    hotspot_mac = text(hotspot.get('mac', old.get('hotspot', {}).get('mac', '')), 17).lower()
    if hotspot_mac:
        device = next((d for d in devices if d['mac'].lower() == hotspot_mac), None)
        if not re.fullmatch(r'(?:[a-f0-9]{2}:){5}[a-f0-9]{2}', hotspot_mac) or (
                device is None and hotspot_mac != old.get('hotspot', {}).get('mac')):
            raise Invalid("Select an available hotspot adapter or keep its current assignment.")
        if device is not None and not device.get('ap', True):
            raise Invalid("This Wi-Fi adapter does not support hotspot mode.")
        if home and hotspot_mac == home['mac']:
            raise Invalid("The hotspot and private network require separate adapters.")
    mode = data.get('connection_mode', old.get('connection_mode', 'private'))
    private_lan = data.get('hotspot_private_lan', old.get('hotspot_private_lan', False))
    if not isinstance(mode, str) or mode not in CONNECTION_MODES or type(private_lan) is not bool:
        raise Invalid("Invalid configuration.")
    previous = {p["id"]: p for p in profiles(old)}
    normalized, ids, active_macs = [], set(), set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise Invalid("Invalid profile.")
        ident = text(entry.get("id", ""), 32)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", ident) or ident in ids:
            raise Invalid("Invalid or duplicate profile ID.")
        ids.add(ident)
        name = text(entry.get("name", ""), 64).strip()
        kind = entry.get("kind", "generic")
        if not name or kind not in PROFILE_KINDS:
            raise Invalid("Invalid telescope name or type.")
        prior = previous.get(ident, {})
        # An inactive saved instrument does not need a radio assignment.
        allowed = present | ({prior["mac"]} if prior and not legacy else set())
        if entry.get("enabled", False) is False:
            allowed.add("")
        network = validate_network(entry, prior, allowed)
        if home and network["mac"] == home["mac"]:
            raise Invalid("The telescope and private network require separate adapters.")
        enabled = entry.get("enabled", False)
        if type(enabled) is not bool:
            raise Invalid("Invalid enabled value.")
        if enabled and network["mac"] in active_macs:
            raise Invalid("Only one active connection is allowed per adapter. Use Connect to switch telescopes.")
        if enabled:
            active_macs.add(network["mac"])
        from .candidates import validate_binding
        binding = entry.get('physical_binding', prior.get('physical_binding'))
        if binding != prior.get('physical_binding'):
            raise Invalid('Use the binding endpoint with the current configuration revision to change physical identity.')
        binding = validate_binding(binding)
        owner_locked = entry.get('owner_locked', prior.get('owner_locked', False))
        if type(owner_locked) is not bool or owner_locked != prior.get('owner_locked', False):
            raise Invalid('Change owner protection through its dedicated endpoint.')
        replaceable=entry.get('auto_replaceable',prior.get('auto_replaceable',False))
        if type(replaceable) is not bool:raise Invalid('Automatic replacement consent must be a boolean.')
        eligible = entry.get('auto_eligible', prior.get('auto_eligible', False))
        if type(eligible) is not bool: raise Invalid('Automatic eligibility must be a boolean.')
        normalized.append(dict(network, auto_replaceable=replaceable, owner_locked=owner_locked, auto_eligible=eligible, id=ident, name=name, kind=kind,
                               target_ip=private_ipv4(entry.get("target_ip")), enabled=enabled,
                               **({'physical_binding': binding} if binding is not None else {})))
    if hotspot_mac and (hotspot.get('enabled', False) or mode != 'private') and hotspot_mac in active_macs:
        raise Invalid("The hotspot and active telescopes require separate adapters.")
    encoded = old["admin_hash"]
    admin = data.get("admin_password", "")
    if not isinstance(admin, str) or admin and not 8 <= len(admin) <= 128:
        raise Invalid("Administrator password must contain 8 to 128 characters.")
    if admin:
        encoded = password_hash(admin)
    if not encoded:
        raise Invalid("Choose an administrator password of at least 8 characters.")
    automatic = data.get('auto_connect', old.get('auto_connect', False))
    if type(automatic) is not bool: raise Invalid('Auto-connect must be a boolean.')
    primary = next((p for p in normalized if p["id"] == "primary"), normalized[0] if normalized else {})
    return {"schema_version": 2, "connection_mode": mode, "hotspot_private_lan": private_lan, "auto_connect": automatic, "auto_policy":validated_auto_policy(data,old), "hostname": hostname, "country": country, "home": home,
            "admin_hash": encoded, "telescopes": normalized,
            **{key: old[key] for key in ("hotspot_password", "linux_password_default") if key in old},
            "telescope": {k: v for k, v in primary.items() if k in ("ssid", "mac", "password", "bssid", "hidden")},
            "telescope_ip": primary.get("target_ip", "10.0.0.1"),
            "hotspot": {"enabled": hotspot.get("enabled", False), **({'ssid': hotspot_name} if 'ssid' in hotspot else {}),
                        **({'mac': hotspot_mac} if 'mac' in hotspot or 'mac' in old.get('hotspot', {}) else {})}}


class Store:
    def __init__(self, directory, mac):
        self.directory = Path(directory)
        self.path = self.directory / "config.json"
        self.config = json.loads(self.path.read_text()) if self.path.exists() else default_config(mac)

    def save(self, config):
        if self.path.exists() and self.config.get("schema_version", 1) < 2:
            backup = self.directory / "config.v1.backup.json"
            if not backup.exists():
                atomic_json(backup, self.config)
        atomic_json(self.path, config)
        self.config = config
