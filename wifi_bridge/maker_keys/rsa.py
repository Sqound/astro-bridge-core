"""Strict, bounded RSA import and blinded PKCS#1 v1.5 SHA-1 signing."""
import base64
import binascii
import hashlib
import math
import re
import secrets
from dataclasses import dataclass

MAX_PEM = 8192
RSA_OID = bytes.fromhex('2a864886f70d010101')
SHA1_INFO = bytes.fromhex('3021300906052b0e03021a05000414')


class Unusable(ValueError):
    def __init__(self): super().__init__('KEY_UNUSABLE')


class DER:
    def __init__(self, data): self.data, self.at = memoryview(data), 0

    def item(self, tag):
        if self.at + 2 > len(self.data) or self.data[self.at] != tag: raise Unusable()
        size = self.data[self.at+1]; self.at += 2
        if size & 128:
            count = size & 127
            if not 1 <= count <= 2 or self.at+count > len(self.data) or not self.data[self.at]: raise Unusable()
            size = int.from_bytes(self.data[self.at:self.at+count]); self.at += count
            if size < 128 or count == 2 and size < 256: raise Unusable()
        if self.at+size > len(self.data): raise Unusable()
        value = self.data[self.at:self.at+size]; self.at += size
        return value

    def integer(self):
        value = self.item(2)
        if not value or value[0] & 128 or len(value) > 514 or len(value) > 1 and value[0] == 0 and value[1] < 128: raise Unusable()
        return int.from_bytes(value)

    def end(self):
        if self.at != len(self.data): raise Unusable()


@dataclass(repr=False, frozen=True)
class RSAKey:
    n: int
    e: int
    d: int
    p: int
    q: int
    dp: int
    dq: int
    qi: int

    def sign(self, challenge):
        if not isinstance(challenge, str) or len(challenge.encode('utf-8')) > 4096: raise Unusable()
        size = (self.n.bit_length()+7)//8
        digest = SHA1_INFO + hashlib.sha1(challenge.encode('utf-8')).digest()
        encoded = b'\0\1' + b'\xff'*(size-len(digest)-3) + b'\0' + digest
        message = int.from_bytes(encoded)
        for _ in range(128):
            blind = secrets.randbelow(self.n-3)+2
            if math.gcd(blind, self.n) == 1: break
        else: raise Unusable()
        masked = message*pow(blind, self.e, self.n) % self.n
        left, right = pow(masked, self.dp, self.p), pow(masked, self.dq, self.q)
        signature = (right + self.q*((left-right)*self.qi % self.p))*pow(blind, -1, self.n) % self.n
        # Detect invalid CRT parameters and faulted computations before returning.
        if pow(signature, self.e, self.n) != message: raise Unusable()
        return base64.b64encode(signature.to_bytes(size)).decode('ascii')


def parse_der(data, pkcs8=False):
    top = DER(data); body = DER(top.item(48)); top.end()
    if body.integer() != 0: raise Unusable()
    if pkcs8:
        algorithm = DER(body.item(48))
        if bytes(algorithm.item(6)) != RSA_OID: raise Unusable()
        if algorithm.at < len(algorithm.data):
            if len(algorithm.item(5)): raise Unusable()
        algorithm.end()
        inner = body.item(4); body.end()
        return parse_der(inner)
    key = RSAKey(*(body.integer() for _ in range(8))); body.end()
    if (not 1024 <= key.n.bit_length() <= 4096 or key.e < 3 or key.e % 2 == 0 or key.e >= key.n
            or key.p < 3 or key.q < 3 or key.p % 2 == 0 or key.q % 2 == 0 or key.p == key.q
            or key.p*key.q != key.n or not 1 < key.d < key.n
            or key.e*key.d % math.lcm(key.p-1, key.q-1) != 1
            or key.dp != key.d % (key.p-1) or key.dq != key.d % (key.q-1)
            or not 0 < key.qi < key.p or key.qi*key.q % key.p != 1): raise Unusable()
    # A deterministic public round trip catches malformed factors without a
    # primality generator in the import path. Signing always verifies its result.
    if pow(pow(42, key.d, key.n), key.e, key.n) != 42: raise Unusable()
    return key


def armour(label, end=False):
    return ('-----' + ('END ' if end else 'BEGIN ') + label + ' PRIVATE' + ' KEY-----').encode('ascii') if label else (
        '-----' + ('END ' if end else 'BEGIN ') + 'PRIVATE' + ' KEY-----').encode('ascii')


def parse(pem):
    if isinstance(pem, str):
        try: pem = pem.encode('ascii')
        except UnicodeError: raise Unusable() from None
    if not isinstance(pem, (bytes, bytearray)) or not 1 <= len(pem) <= MAX_PEM: raise Unusable()
    stripped = pem.strip()
    for label in ('RSA', ''):
        first, last = armour(label), armour(label, True)
        if stripped.startswith(first) and stripped.endswith(last):
            body = stripped[len(first):-len(last)]
            if not re.fullmatch(rb'[A-Za-z0-9+/=\r\n\t ]+', body): raise Unusable()
            der = bytearray()
            try:
                der.extend(base64.b64decode(re.sub(rb'\s+', b'', body), validate=True))
                return parse_der(der, pkcs8=not label)
            except (ValueError, TypeError, binascii.Error, OverflowError): raise Unusable() from None
            finally: der[:] = b'\0'*len(der)
    raise Unusable()
