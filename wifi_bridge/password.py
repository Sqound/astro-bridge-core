"""One bridge password for the web interface, the hotspot and the Linux account.

The password itself is never stored. The web interface keeps a salted PBKDF2
hash (config.admin_hash); the hotspot keeps the derived WPA key bound to its
network name (config.hotspot_key = {ssid, psk}); the Linux account keeps its own
crypt hash in the system files. The documented initial password (12345678) is
public: while it is in use, nothing is stored for the hotspot and the key is
derived from bootstrap.json when needed.
"""
import hashlib
import re

DEFAULT = '12345678'
MIN_LENGTH, MAX_LENGTH = 8, 63
# Printable ASCII without space: the same bytes on every phone keyboard, in
# WPA2, in chpasswd and in the web form. No accented letters (some phones encode
# a non-ASCII Wi-Fi password differently).
ALLOWED = re.compile(r'[\x21-\x7e]{8,63}')


class PasswordRuleError(ValueError):
    """The new password does not follow the rule. Never carries the value."""


def check(password):
    """Validate a new bridge password; the message never contains the value."""
    if not isinstance(password, str) or not ALLOWED.fullmatch(password):
        raise PasswordRuleError('The password must have 8 to 63 characters: letters without accents, digits and symbols, without spaces.')
    if password == DEFAULT:
        raise PasswordRuleError('Choose a password other than the default 12345678.')
    return password


def wpa_psk(password, ssid):
    """The WPA2 pre-shared key hostapd uses: PBKDF2-SHA1(password, ssid, 4096, 32)."""
    return hashlib.pbkdf2_hmac('sha1', password.encode(), ssid.encode(), 4096, 32).hex()


def hotspot_key(password, ssid):
    return {'ssid': ssid, 'psk': wpa_psk(password, ssid)}


def valid_key(value):
    return (isinstance(value, dict) and set(value) == {'ssid', 'psk'} and isinstance(value['ssid'], str)
            and 1 <= len(value['ssid'].encode()) <= 32 and isinstance(value['psk'], str)
            and re.fullmatch('[0-9a-f]{64}', value['psk']) is not None)


def hotspot_psk(config, ssid, bootstrap_password):
    """The key the hotspot must use for this network name.

    A personal password is stored only as its key; the stored key names the
    network it belongs to (see config.hotspot_ssid). Without a stored key the
    bridge still uses the initial password from bootstrap.json.
    """
    key = config.get('hotspot_key')
    if valid_key(key):
        if key['ssid'] != ssid:
            raise ValueError('hotspot key belongs to another network name')
        return key['psk']
    return wpa_psk(bootstrap_password, ssid)


def default_in_use(config, bootstrap_password, linux_account=None, web_default=None):
    """One flag: is the public initial password still accepted somewhere?

    web_default may be passed when the (slow) web hash comparison is cached.
    Installations that changed the three passwords separately keep this flag
    until the first change of the single password unites them.
    """
    from .config import password_matches
    if web_default is None:
        encoded = config.get('admin_hash', '')
        web_default = password_matches(DEFAULT, encoded) if encoded else bootstrap_password == DEFAULT
    hotspot_default = not valid_key(config.get('hotspot_key')) and bootstrap_password == DEFAULT
    linux_default = bool(linux_account) and config.get('linux_password_default', bootstrap_password == DEFAULT)
    return bool(web_default or hotspot_default or linux_default)
