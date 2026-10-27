"""Actual TCP/UDP mount packets in a disposable privileged container only.

Uses private network namespaces and synthetic read-only protocol servers.
No host network interfaces or physical mount commands are used.
"""
from pathlib import Path
import os
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace

from wifi_bridge.mount_discovery import SynscanDiscovery
from wifi_bridge.mounts import MOUNTS, mount_probe
from wifi_bridge.network import LinuxNetwork


def command(*args, check=True):
    result=subprocess.run(args, text=True, capture_output=True)
    if check and result.returncode:raise AssertionError(result.stderr[-2000:])
    return result.stdout.strip()


names=['abm-'+part+'-'+str(os.getpid()) for part in ('home','ap','mount')]
devices=[{'name':name} for name in ('abmh0','abmap0','abms0')]
server=None;observer=None;observer_thread=None
traffic=tempfile.TemporaryDirectory();traffic_log=Path(traffic.name)/'udp-requests.log'
try:
    for index,(namespace,device,local,remote) in enumerate(zip(names,devices,
            ('192.168.42.1/24','192.168.77.1/24','192.168.4.2/24'),
            ('192.168.42.2/24','192.168.77.12/24','192.168.4.1/24'))):
        command('ip','netns','add',namespace);command('ip','-n',namespace,'link','set','lo','up')
        peer='abmp'+str(index)
        command('ip','link','add',device['name'],'type','veth','peer','name',peer)
        command('ip','link','set',peer,'netns',namespace)
        command('ip','addr','add',local,'dev',device['name']);command('ip','link','set',device['name'],'up')
        command('ip','-n',namespace,'addr','add',remote,'dev',peer);command('ip','-n',namespace,'link','set',peer,'up')
    Path('/proc/sys/net/ipv4/ip_forward').write_text('1')
    for namespace,gateway in zip(names[:2],('192.168.42.1','192.168.77.1')):
        command('ip','-n',namespace,'route','add','default','via',gateway)
    server_code='''import socket,threading,sys
def tcp():
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('192.168.4.1',4030));s.listen()
 while True:
  c,a=s.accept()
  with c:
   if c.recv(64)==b':GVP#':c.sendall(b'AM5N#')
def udp():
 s=socket.socket(type=socket.SOCK_DGRAM);s.bind(('192.168.4.1',11880))
 while True:
  data,a=s.recvfrom(64)
  with open(sys.argv[1],'a') as log:log.write(data.hex()+'\\n')
  replies={b':e1\\r':b'=020600\\r',b':e2\\r':b'=020601\\r',b':f1\\r':b'=001\\r',b':j1\\r':b'=000080\\r'}
  if data in replies:s.sendto(replies[data],a)
threading.Thread(target=tcp,daemon=True).start();threading.Thread(target=udp,daemon=True).start()
print('READY',flush=True);threading.Event().wait()
'''
    server=subprocess.Popen(['ip','netns','exec',names[2],'python3','-u','-c',server_code,str(traffic_log)],stdout=subprocess.PIPE)
    assert server.stdout.readline().strip()==b'READY';time.sleep(.2)
    client_code='''import socket,sys
kind,host=sys.argv[1:]
if kind=='zwo_mount':
 with socket.create_connection((host,4030),timeout=.6) as s:
  s.sendall(b':GVP#');assert s.recv(64)==b'AM5N#'
else:
 with socket.socket(type=socket.SOCK_DGRAM) as s:
  s.settimeout(.6);s.sendto(b':e1\\r',(host,11880));data,origin=s.recvfrom(64)
  assert data==b'=020600\\r' and origin==(host,11880)
'''
    with tempfile.TemporaryDirectory() as folder:
        net=LinuxNetwork(folder);net.association_identity=lambda device:'synthetic-'+device['name']
        for kind in MOUNTS:
            net.firewall(home=devices[0],setup=devices[1],telescopes=[{'profile_id':'mount','kind':kind,
                'device':devices[2],'target':'192.168.4.1',kind+'_proxy':True}])
            command('nft','-c','list','table','ip','wifi_bridge')
            for namespace,host in zip(names[:2],('192.168.42.1','192.168.77.1')):
                command('ip','netns','exec',namespace,'python3','-c',client_code,kind,host)
                other='synscan' if kind=='zwo_mount' else 'zwo_mount'
                blocked=subprocess.run(['ip','netns','exec',namespace,'python3','-c',client_code,other,host],capture_output=True)
                assert blocked.returncode, 'The other mount family must not inherit this proxy.'
            identity=mount_probe(devices[2],'192.168.4.2/24','192.168.4.1',kind)
            assert identity['protocol_identity']['family']==kind
            print('PASS: actual '+kind+' NAT from home + hotspot, reply correlation, exact protocol and bound identification',flush=True)
        # Native search reaches a real UDP listener by broadcast. Arbitrary
        # unicast datagrams still use the kernel proxy, bypassing that listener.
        profile={'id':'mount','kind':'synscan','mac':'synthetic-mount','enabled':True,'target_ip':'192.168.4.1'}
        plans=((devices[0]['name'],'192.168.42.1/24'),(devices[1]['name'],'192.168.77.1/24'))
        demands=[]
        def demand(*args,**kwargs):
            demands.append((args,kwargs));return 'REQUEST_ACCEPTED'
        c=SimpleNamespace(demo=False,lock=threading.RLock(),stop=threading.Event(),management_plans=plans,
            last_cycle_ok=True,net_lock=threading.Lock(),
            store=SimpleNamespace(config={'telescopes':[profile],'auto_connect':False}),
            state={'telescopes':[{'id':'mount','synscan_proxy':True,'relay_ready':True}],
                   'devices':[dict(devices[2],mac='synthetic-mount')]},
            operations=SimpleNamespace(demand=demand))
        observer=SynscanDiscovery(c);observer_thread=threading.Thread(target=observer.loop,daemon=True);observer_thread.start()
        deadline=time.monotonic()+3
        while len(observer.sockets)!=2 and time.monotonic()<deadline:time.sleep(.05)
        assert len(observer.sockets)==2
        discovery_client='''import socket,sys,time
host=sys.argv[1]
with socket.socket(type=socket.SOCK_DGRAM) as s:
 s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1);s.settimeout(1.5)
 for query,expected in [(b':e1\\r',b'=020600\\r'),(b':e2\\r',b'=020601\\r')]:
  s.sendto(query,('255.255.255.255',11880));reply,origin=s.recvfrom(64)
  assert reply==expected and origin==(host,11880),(reply,origin)
  time.sleep(.3)
 # A non-version broadcast must not be forwarded, even a read-only status.
 for query in (b':f1\\r',b':J1\\r',b':e1\\rEXTRA'):
  s.sendto(query,('255.255.255.255',11880));s.settimeout(.35)
  try:s.recvfrom(64)
  except TimeoutError:pass
  else:raise AssertionError('Unexpected broadcast reply')
 # The normal unicast proxy preserves native status and position datagrams.
 s.settimeout(1.5)
 for query,expected in [(b':f1\\r',b'=001\\r'),(b':j1\\r',b'=000080\\r')]:
  s.sendto(query,(host,11880));reply,origin=s.recvfrom(64)
  assert reply==expected and origin==(host,11880)
'''
        for namespace,host in zip(names[:2],('192.168.42.1','192.168.77.1')):
            command('ip','netns','exec',namespace,'python3','-c',discovery_client,host)
        assert not demands
        received=traffic_log.read_text().splitlines()
        assert b':J1\r'.hex() not in received and b':e1\rEXTRA'.hex() not in received
        assert received.count(b':f1\r'.hex())==2 and received.count(b':j1\r'.hex())==2,received
        print('PASS: actual SynScan broadcast search on both management interfaces; exact axis replies; non-version broadcasts blocked; native unicast status/position preserved',flush=True)
        net.firewall(home=devices[0],setup=devices[1],isolated=[devices[2]])
        c.store.config.update(auto_connect=True,auto_policy={'synscan_demand':True});profile['enabled']=False;c.state['telescopes']=[]
        pending_client='''import socket
with socket.socket(type=socket.SOCK_DGRAM) as s:
 s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1);s.settimeout(.6)
 s.sendto(b':e1\\r',('255.255.255.255',11880))
 try:s.recvfrom(64)
 except TimeoutError:pass
 else:raise AssertionError('Unselected mount must not advertise an invented reply')
'''
        for namespace in names[:2]:command('ip','netns','exec',namespace,'python3','-c',pending_client)
        assert len(demands)==2 and all(kwargs=={'kind':'synscan','target':None} for args,kwargs in demands),demands
        assert traffic_log.read_text().splitlines()==received
        print('PASS: unselected SynScan broadcast requests guarded selection without forwarding commands or inventing replies',flush=True)
        c.stop.set();observer_thread.join(3);assert not observer_thread.is_alive() and not observer.sockets
        net.firewall(home=devices[0],setup=devices[1],isolated=[devices[2]])
        blocked=subprocess.run(['ip','netns','exec',names[1],'python3','-c',client_code,'synscan','192.168.77.1'],capture_output=True)
        assert blocked.returncode
        print('PASS: relay withdrawal blocks the old hotspot mount endpoint',flush=True)
finally:
    if observer:
        observer.c.stop.set()
        if observer_thread:observer_thread.join(3)
    if server:
        server.terminate();server.wait(timeout=3)
        if server.stdout:server.stdout.close()
    for namespace in names:command('ip','netns','delete',namespace,check=False)
    for device in devices:command('ip','link','delete',device['name'],check=False)
    traffic.cleanup()
