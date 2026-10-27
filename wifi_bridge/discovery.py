"""Relay Seestar discovery to the selected LAN port proxy, without logging secrets."""
import ipaddress
import json
import socket
import time


SEESTAR_PORTS = (4700, 4800, 4801, 4804, 4554, 4555)


def scan_request(data):
    if not 0 < len(data) <= 4096:
        return None
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(value, dict) or value.get("method") != "scan_iscope":
        return None
    ident = value.get('id')
    if not (ident is None or type(ident) is int and len(str(ident)) <= 128
            or isinstance(ident, str) and len(ident) <= 128 and not any(ord(c)<32 for c in ident)): return None
    return value


class SeestarDiscovery:
    def __init__(self, controller):
        self.controller = controller

    def loop(self):
        from .network import NetworkError
        listeners={};next_request=0
        try:
            while not self.controller.stop.is_set():
                with self.controller.lock:plans=self.controller.management_plans[:2]
                for plan in list(listeners):
                    if plan not in plans:listeners.pop(plan).close()
                for plan in plans:
                    if plan in listeners:continue
                    listener=None
                    try:
                        listener=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
                        listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                        listener.setsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,plan[0].encode()+b"\0")
                        listener.bind(("",4720));listener.settimeout(.1);listeners[plan]=listener
                    except OSError:
                        if listener:listener.close()
                if not listeners:self.controller.stop.wait(.5);continue
                for plan,listener in list(listeners.items()):
                    try:
                        data,client=listener.recvfrom(4097);request=scan_request(data)
                        interface=ipaddress.ip_interface(plan[1]);source=ipaddress.ip_address(client[0])
                        if (not request or source not in interface.network or source in (interface.ip,interface.network.network_address,interface.network.broadcast_address)
                                or time.monotonic()<next_request):continue
                        next_request=time.monotonic()+.1
                        with self.controller.lock:
                            proxy=next((p for p in self.controller.discovery_plans if p[:2]==plan),None)
                        if not proxy:
                            self.controller.operations.demand(plan,client[0]);continue
                        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as upstream:
                            upstream.setsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,proxy[2].encode()+b"\0")
                            upstream.settimeout(.75);upstream.sendto(data,(proxy[3],4720));reply,origin=upstream.recvfrom(16385)
                        response=json.loads(reply)
                        if (origin!=(proxy[3],4720) or len(reply)>16384 or not isinstance(response,dict)
                                or response.get('method')!='scan_iscope' or response.get('id')!=request.get('id')):continue
                        with self.controller.lock:
                            if plan not in self.controller.management_plans or proxy not in self.controller.discovery_plans:continue
                        listener.sendto(reply,client)
                    except (OSError,ValueError,UnicodeError,NetworkError):continue
        finally:
            for listener in listeners.values():listener.close()
