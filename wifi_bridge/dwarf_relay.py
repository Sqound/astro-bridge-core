"""Offline DWARF codec retained for regression analysis; no network execution path."""
import ipaddress
import re

from .dwarf_demand import _varint

DWARF_PORTS = (9900, 8082, 554)
TOP = {1:0,2:0,3:2,4:0,5:2,6:2,7:2,8:2,9:2,10:2,11:2,12:0,13:2,14:2}
AP = {1:2,2:0,3:2,4:2,5:2,6:2,7:2,8:2}
STA = {1:2,2:2,3:2,4:0,5:2,6:2}


class EchoRejected(ValueError):
    def __init__(self, reason='ECHO_INVALID'):
        self.reason = reason
        super().__init__('DWARF discovery reply rejected.')


def fields(raw, schema, limit):
    if not isinstance(raw, bytes) or len(raw)>limit: raise EchoRejected()
    result = {}; offset = 0
    try:
        while offset<len(raw):
            tag,offset=_varint(raw,offset);number=tag>>3;wire=tag&7
            if number not in schema or number in result or wire!=schema[number]:raise EchoRejected()
            if wire==0:value,offset=_varint(raw,offset)
            else:
                size,offset=_varint(raw,offset)
                if size>limit or offset+size>len(raw):raise EchoRejected()
                value=raw[offset:offset+size];offset+=size
            result[number]=value
        return result
    except ValueError:raise EchoRejected() from None


def varint(value):
    out=bytearray()
    while value>=128:out.append((value&127)|128);value>>=7
    out.append(value);return bytes(out)


def encode(values, schema):
    return b''.join(varint((number<<3)|schema[number])+
                   (varint(value) if schema[number]==0 else varint(len(value))+value)
                   for number,value in sorted(values.items()))


def relay_ping_timestamp(data):
    """Bounded opaque filters are relayed only to a ready owner, never allocation."""
    if not isinstance(data,bytes) or not 2<=len(data)<=512 or data[:2]!=b'\x08\x01':return None
    seen={1};last=1;offset=2;stamp=None;magic=False;counts={4:0,5:0}
    try:
        while offset<len(data):
            tag,offset=_varint(data,offset);number=tag>>3;wire=tag&7
            if number not in (2,3,4,5) or number<last or number in seen and number in (2,3):return None
            if wire!=(0 if number==2 else 2):return None
            seen.add(number);last=number
            if number==2:
                stamp,offset=_varint(data,offset)
                if not 0<stamp<=2**63-1:return None
            else:
                size,offset=_varint(data,offset)
                if offset+size>len(data):return None
                if number==3:magic=size==4 and data[offset:offset+size]==b'txtl'
                else:
                    counts[number]+=1
                    if size>64 or counts[number]>8:return None
                offset+=size
        return stamp if magic and {1,2,3}<=seen else None
    except ValueError:return None


def sanitize_echo(raw, stamp, target, endpoint):
    """Keep genuine known manufacturer fields, remove credentials and virtualize IPv4."""
    value=fields(raw,TOP,4096)
    if value.get(1)!=2 or value.get(3)!=b'txtl' or value.get(4)!=stamp:raise EchoRejected()
    # Absolute URLs could bypass the selected proxy. The actual mini uses an opaque numeric version in this field, despite its
    # community schema name. Preserve it without interpreting it as a transport URI.
    if value.get(11) not in (b'ws', b'ws://') and not re.fullmatch(rb'[0-9]{1,2}\.[0-9]{1,2}',value.get(11,b'')):raise EchoRejected('UNSUPPORTED_SCHEME')
    model=fields(value.get(6,b''),{1:0,2:0},128)
    if not model or 13 not in value or 14 not in value:raise EchoRejected()
    ap=fields(value[13],AP,1024);sta=fields(value[14],STA,1024)
    try:
        target=ipaddress.IPv4Address(target);endpoint=ipaddress.IPv4Address(endpoint)
        if endpoint.is_unspecified or endpoint.is_multicast or endpoint.is_loopback:raise EchoRejected()
        found=False
        for mapping,key,ipv6 in ((ap,7,8),(sta,5,6)):
            address=mapping.get(key,b'')
            if len(address) not in (0,4) or len(mapping.get(ipv6,b'')) not in (0,16):raise EchoRejected()
            found=found or address==target.packed
            # IPv6 has no corresponding owned forwarding/proxy contract.
            mapping.pop(ipv6,None)
            # Do not turn an absent/unconfigured network into a connected STA.
            if len(address)==4 and address!=b'\0'*4:mapping[key]=endpoint.packed
        if not found:raise EchoRejected()
    except (ValueError,TypeError):raise EchoRejected() from None
    value.pop(9,None);ap.pop(6,None);sta.pop(3,None)
    value[13]=encode(ap,AP);value[14]=encode(sta,STA)
    result=encode(value,TOP)
    if len(result)>4096:raise EchoRejected()
    return result
