"""Strict passive DWARF Ping demand; no discovery replies or device metadata."""
import ctypes
import ipaddress
import socket

from .vespera_demand import VesperaDemand


def ping_filter(broadcast):
    code=[];labels={}
    def stmt(op,k):code.append((op,None,None,k))
    def jump(op,k,yes=None,no=None):code.append((op,yes,no,k))
    stmt(0x28,12);jump(0x15,0x0800,no='reject')
    stmt(0x30,14);stmt(0x54,0xf0);jump(0x15,0x40,no='reject')
    stmt(0x30,23);jump(0x15,17,no='reject')
    stmt(0x28,20);jump(0x45,0x3fff,yes='reject')
    stmt(0x20,30);jump(0x15,0xffffffff,yes='broadcast');jump(0x15,broadcast,no='reject')
    labels['broadcast']=len(code)
    stmt(0x30,14);stmt(0x54,15);jump(0x35,5,no='reject')
    stmt(0xb1,14);stmt(0x48,16);jump(0x15,9900,no='reject')
    stmt(0x48,18);jump(0x35,10,no='reject');jump(0x25,520,yes='reject')
    stmt(0x48,22);jump(0x15,0x0801,no='reject')  # Reject Echo before any payload delivery.
    stmt(0x87,0);stmt(0x04,22+512);stmt(0x16,0)
    labels['reject']=len(code);stmt(0x06,0)
    return tuple((op,labels[yes]-i-1 if yes else 0,labels[no]-i-1 if no else 0,k)
                 for i,(op,yes,no,k) in enumerate(code))


def filtered_socket(plan):
    if not hasattr(socket,'AF_PACKET'):raise OSError('Passive demand unavailable.')
    broadcast=int(ipaddress.IPv4Interface(plan[1]).network.broadcast_address)
    class Instruction(ctypes.Structure):
        _fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint32)]
    class Program(ctypes.Structure):
        _fields_=[('len',ctypes.c_ushort),('filter',ctypes.POINTER(Instruction))]
    instructions=ping_filter(broadcast)
    array=(Instruction*len(instructions))(*(Instruction(*row) for row in instructions))
    program=Program(len(instructions),array)
    stream=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,0)
    try:
        stream.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,16384)
        stream.setsockopt(socket.SOL_SOCKET,26,bytes(program))
        stream.bind((plan[0],0x0800));stream.setblocking(False);return stream
    except (OSError,ValueError):stream.close();raise


def _varint(data,offset):
    start=offset;value=0
    for shift in range(0,70,7):
        if offset>=len(data):raise ValueError('Malformed Ping.')
        byte=data[offset];offset+=1;value|=(byte&127)<<shift
        if byte<128:
            if value>2**64-1 or offset-start!=max(1,(value.bit_length()+6)//7):raise ValueError('Malformed Ping.')
            return value,offset
    raise ValueError('Malformed Ping.')


def ping_timestamp(data):
    """Canonical generic Ping only; filtered/unknown requests cannot select a device."""
    if not isinstance(data,bytes) or not 2<=len(data)<=512 or data[:2]!=b'\x08\x01':return None
    fields={1};last=1;offset=2;stamp=None;magic=False;empty_filters={4:0,5:0}
    try:
        while offset<len(data):
            tag,offset=_varint(data,offset);field=tag>>3;wire=tag&7
            if field not in (2,3,4,5) or field<last or field in fields and field in (2,3):return None
            if wire!=(0 if field==2 else 2):return None
            fields.add(field);last=field
            if field==2:
                stamp,offset=_varint(data,offset)
                if not 0<stamp<=2**63-1:return None
            else:
                size,offset=_varint(data,offset)
                if offset+size>len(data):return None
                if field==3:magic=size==4 and data[offset:offset+size]==b'txtl'
                else:
                    # The observed HOME request is unfiltered; inclusion/exclusion
                    # semantics of nonempty vocals/mutes are deliberately unsupported.
                    empty_filters[field]+=1
                    if size or empty_filters[field]>8:return None
                offset+=size
        return stamp if magic and {1,2,3}<=fields else None
    except ValueError:return None


def ping_request(frame,packet_type,plan,bridge_addresses=()):
    if packet_type not in (0,1,2) or len(frame)<42 or frame[12:14]!=b'\x08\x00':return None
    ip=frame[14:];ihl=(ip[0]&15)*4;total=int.from_bytes(ip[2:4],'big')
    if ip[0]>>4!=4 or ihl<20 or len(ip)<ihl+8 or ip[9]!=17 or int.from_bytes(ip[6:8],'big')&0x3fff:return None
    try:
        address=ipaddress.IPv4Interface(plan[1]);source=ipaddress.IPv4Address(ip[12:16]);destination=ipaddress.IPv4Address(ip[16:20])
        if (source not in address.network or source in (address.ip,address.network.network_address,address.network.broadcast_address)
                or source in bridge_addresses or source.is_multicast or source.is_unspecified):return None
        if destination not in (address.network.broadcast_address,ipaddress.IPv4Address('255.255.255.255')):return None
        udp=ip[ihl:];length=int.from_bytes(udp[4:6],'big')
        if int.from_bytes(udp[2:4],'big')!=9900 or not 8<=length<=520 or total<ihl+length or len(udp)<length:return None
        stamp=ping_timestamp(bytes(udp[8:length]))
        if stamp is None:return None
        return str(source),bytes(ip[12:16]+udp[:2])+stamp.to_bytes(8,'big')
    except (ValueError,TypeError,IndexError):return None


class DwarfDemand(VesperaDemand):
    kind='dwarf'
    read_size=594

    def open_socket(self,plan):return filtered_socket(plan)

    def status(self):
        with self.c.lock:
            # Keep the old diagnostic object for readers, without a reply service.
            return dict(super().status(), passive_only=True, replies_disabled=True,
                        discovery_relay={'supported':False, 'last_result':'REPLIES_DISABLED',
                            'last_result_age_seconds':None, 'passive_only':True, 'replies_disabled':True,
                            'proxy_available':bool(self.c.last_cycle_ok and any(
                                r.get('relay_ready') and r.get('dwarf_proxy') for r in self.c.state['telescopes'])),
                            'relay_plan_available':False, 'maker_app_acceptance':'unverified'},
                        maker_app_acceptance='unverified')

    def receive(self,frame,packet_type,plan):
        with self.c.lock:
            if plan not in self.plans():return
            bridge={ipaddress.IPv4Interface(p[1]).ip for p in self.c.management_plans[:2]}
        parsed=ping_request(frame,packet_type,plan,bridge)
        if not parsed:return
        source,signature=parsed
        self.offer(plan,source,signature,seen_seconds=5)
