"""Deterministic disposable RSA fixtures, generated at runtime; never key bytes."""
import base64
import functools
import math
import random

from wifi_bridge.maker_keys.rsa import armour, RSA_OID


def tlv(tag,value):
    length = len(value)
    count = (length.bit_length()+7)//8
    return bytes([tag])+(bytes([length]) if length < 128 else bytes([128+count])+length.to_bytes(count))+value


def integer(value):
    body = value.to_bytes(max(1,(value.bit_length()+7)//8))
    return tlv(2,(b'\0' if body[0]&128 else b'')+body)


def probable_prime(value, rng):
    if any(value % p == 0 for p in (3,5,7,11,13,17,19,23,29,31,37)): return False
    odd, shifts = value-1, 0
    while not odd % 2: odd //= 2; shifts += 1
    for _ in range(32):
        base = rng.randrange(2,value-1)
        power = pow(base,odd,value)
        if power in (1,value-1): continue
        for _ in range(shifts-1):
            power = power*power % value
            if power == value-1: break
        else: return False
    return True


@functools.lru_cache(maxsize=3)
def numbers(seed=71207):
    rng = random.Random(seed)
    def prime():
        while True:
            value = rng.getrandbits(512) | 1 | (3<<510)
            if (value-1)%65537 and probable_prime(value,rng): return value
    p,q = prime(),prime()
    d = pow(65537,-1,math.lcm(p-1,q-1))
    return p*q,65537,d,p,q,d%(p-1),d%(q-1),pow(q,-1,p)


def pem(seed=71207,pkcs8=True):
    der = tlv(48,b''.join(integer(v) for v in (0,*numbers(seed))))
    if pkcs8: der = tlv(48,integer(0)+tlv(48,tlv(6,RSA_OID)+tlv(5,b''))+tlv(4,der))
    label = '' if pkcs8 else 'RSA'
    encoded = base64.b64encode(der)
    return bytearray(armour(label)+b'\n'+b'\n'.join(encoded[i:i+64] for i in range(0,len(encoded),64))+b'\n'+armour(label,True)+b'\n')
