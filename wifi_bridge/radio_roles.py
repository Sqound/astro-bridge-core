"""Persistent radio purposes, separate from transient associations and services."""
import re

PURPOSES = ('auto', 'home', 'hotspot', 'telescope')


def interface_roles(config):
    result = dict(config.get('interface_roles', {}))
    for purpose, mac in (('home', config.get('home', {}).get('mac')),
                         ('hotspot', config.get('hotspot', {}).get('mac'))):
        if mac: result.setdefault(mac, purpose)
    return result


def role_allows(config, mac, purpose):
    return interface_roles(config).get(mac, 'auto') in ('auto', purpose)


def normalize_roles(data, old, devices, home, hotspot):
    from .config import Invalid
    previous = interface_roles(old)
    roles = data.get('interface_roles', previous)
    if not isinstance(roles, dict) or len(roles) > 64:
        raise Invalid('Invalid Wi-Fi adapter roles.')
    roles = dict(roles)
    available = {d['mac'] for d in devices} | set(previous)
    for mac, purpose in roles.items():
        if (not isinstance(mac, str) or not re.fullmatch(r'(?:[a-f0-9]{2}:){5}[a-f0-9]{2}', mac)
                or mac not in available or not isinstance(purpose, str) or purpose not in PURPOSES):
            raise Invalid('Choose a known Wi-Fi adapter and a valid role.')
    for purpose in ('home', 'hotspot'):
        if list(roles.values()).count(purpose) > 1:
            raise Invalid('Reserve only one adapter for private Wi-Fi and one for the hotspot.')
    changed = roles != previous
    home = dict(home); hotspot = dict(hotspot)
    if changed:
        selected = next((mac for mac, purpose in roles.items() if purpose == 'home'), '')
        if selected and home: home['mac'] = selected
        selected = next((mac for mac, purpose in roles.items() if purpose == 'hotspot'), '')
        if selected:
            hotspot['mac'] = selected
            if previous.get(selected) != 'hotspot': hotspot['enabled'] = True
        elif any(purpose == 'hotspot' for purpose in previous.values()):
            hotspot['mac'] = ''
    else:
        # Existing network-specific controls are explicit assignments as well.
        for purpose, network, before in (('home', home, old.get('home', {})),
                                          ('hotspot', hotspot, old.get('hotspot', {}))):
            mac = network.get('mac', '')
            if mac != before.get('mac', ''):
                if mac and roles.get(mac, 'auto') not in ('auto', purpose):
                    raise Invalid('This Wi-Fi adapter is reserved for another role. Change its role in Wi-Fi adapters first.')
                for previous_mac in [key for key, value in roles.items() if value == purpose]:
                    roles[previous_mac] = 'auto'
                if mac: roles[mac] = purpose
    policy = {'interface_roles': roles}
    for purpose, network in (('home', home), ('hotspot', hotspot)):
        mac = network.get('mac', '')
        if mac and not role_allows(policy, mac, purpose):
            raise Invalid('This Wi-Fi adapter is reserved for another role. Change its role in Wi-Fi adapters first.')
        if purpose == 'hotspot' and mac:
            device = next((d for d in devices if d['mac'] == mac), None)
            if device is not None and not device.get('ap', True):
                raise Invalid('This Wi-Fi adapter does not support hotspot mode.')
    return roles, home, hotspot
