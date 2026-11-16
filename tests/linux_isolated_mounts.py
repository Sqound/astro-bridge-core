"""Actual overlapping-IP transport tests. Disposable Linux container ONLY.

No physical radios, Wi-Fi association, mount motion, tracking or guiding commands.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from wifi_bridge.config import atomic_json
from wifi_bridge.isolated import IsolatedRadios, namespace_firewall, namespace_name
from wifi_bridge.network import run

if os.environ.get('AB_DISPOSABLE_NETWORK_TEST') != '1' or os.geteuid() != 0:
    raise SystemExit('Run only in a disposable --network none privileged Linux container.')


def cmd(*args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if check and result.returncode: raise AssertionError(result.stderr[-1000:])
    return result.stdout.strip()


def client(namespace, host, port, transport, expected, timeout=2):
    code = '''import socket,sys
host,port,transport,expected=sys.argv[1:];port=int(port)
with socket.socket(type=socket.SOCK_STREAM if transport=='tcp' else socket.SOCK_DGRAM) as s:
 s.settimeout(2)
 if transport=='tcp':
  s.connect((host,port));s.sendall(b':GV');s.sendall(b'P#:GVP#');s.shutdown(socket.SHUT_WR)
  result=b''
  while True:
   data=s.recv(4096)
   if not data:break
   result+=data
 else:
  s.sendto(b':e1\\r',(host,port));result,origin=s.recvfrom(100)
  assert origin==(host,port),origin
 assert result==bytes.fromhex(expected),repr(result)
'''
    return cmd('ip', 'netns', 'exec', namespace, 'python3', '-c', code, host, str(port), transport, expected.hex())


names = ['abi-'+part for part in ('home', 'ap', 'zwo', 'syn', 'zwo-target', 'syn-target')]
servers = []
manager = None
rows = []
try:
    cmd('ip', 'link', 'set', 'lo', 'up')
    for name in names:
        cmd('ip', 'netns', 'add', name); cmd('ip', '-n', name, 'link', 'set', 'lo', 'up')
    paths = []
    for index, (role, ns, address, peer_address) in enumerate((
            ('home', names[0], '192.168.42.1/24', '192.168.42.2/24'),
            ('ap', names[1], '192.168.77.1/24', '192.168.77.12/24'))):
        iface = 'ab-in'+str(index); peer = 'ab-cli'+str(index)
        cmd('ip', 'link', 'add', iface, 'type', 'veth', 'peer', 'name', peer)
        cmd('ip', 'link', 'set', peer, 'netns', ns)
        cmd('ip', 'addr', 'add', address, 'dev', iface); cmd('ip', 'link', 'set', iface, 'up')
        cmd('ip', '-n', ns, 'addr', 'add', peer_address, 'dev', peer); cmd('ip', '-n', ns, 'link', 'set', peer, 'up')
        # No client route to the real instrument subnet is installed.
        paths.append({'role': role, 'address': address, 'device': {'name': iface}})
    profiles = []
    with tempfile.TemporaryDirectory() as directory:
        manager = IsolatedRadios(directory, run)
        for index, kind in enumerate(('zwo_mount', 'synscan')):
            ns, remote = names[index+2], names[index+4]
            iface, peer = 'ab-radio'+str(index), 'ab-scope'+str(index)
            cmd('ip', 'link', 'add', iface, 'type', 'veth', 'peer', 'name', peer)
            cmd('ip', 'link', 'set', iface, 'netns', ns); cmd('ip', 'link', 'set', peer, 'netns', remote)
            cmd('ip', '-n', ns, 'address', 'add', '192.168.4.2/24', 'dev', iface)
            cmd('ip', '-n', ns, 'link', 'set', iface, 'up')
            cmd('ip', '-n', remote, 'address', 'add', '192.168.4.1/24', 'dev', peer)
            cmd('ip', '-n', remote, 'link', 'set', peer, 'up')
            mac = cmd('ip', '-n', ns, '-j', 'link', 'show', iface)
            device = {'name': iface, 'mac': json.loads(mac)[0]['address'], 'usb': True, 'label': kind}
            transport, port = ('tcp', 4030) if index == 0 else ('udp', 11880)
            profile = {'id': kind, 'name': kind, 'kind': kind, 'mac': device['mac'], 'enabled': True,
                       'isolated': True, 'target_ip': '192.168.4.1', 'client_port': 14030 if index == 0 else 21180}
            profiles.append(profile)
            # Test namespaces intentionally use synthetic names, not claimed Wi-Fi PHYs.
            record = {'namespace': ns, 'device': device, 'role': 'telescope-'+kind, 'profile_id': kind,
                      'target': '192.168.4.1', 'processes': [], 'boot_id': manager.boot_id, 'created': True}
            manager.records[device['mac']] = record
            nsfolder = Path(directory)/'isolated'/ns; nsfolder.mkdir()
            atomic_json(nsfolder/'lease.json', {'address': '192.168.4.2/24', 'expires': time.monotonic()+600})
            manager.command(record, ['nft', '-f', '-'], input=namespace_firewall('192.168.4.1', transport, port))
            # Both targets expose BOTH services with different fingerprints. A cross
            # namespace error therefore gives an unmistakable wrong-device response.
            code = r'''import socket,threading,sys
name=sys.argv[1].encode()
def tcp():
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('192.168.4.1',4030));s.listen()
 while True:
  c,_=s.accept()
  def request(c):
   with c:
    data=b''
    while True:
     part=c.recv(100)
     if not part:break
     data+=part
    if data==b':GVP#:GVP#':c.sendall(name+b'#'+name+b'#')
  threading.Thread(target=request,args=(c,),daemon=True).start()
def udp():
 s=socket.socket(type=socket.SOCK_DGRAM);s.bind(('192.168.4.1',11880))
 while True:
  data,a=s.recvfrom(65535)
  if data==b':e1\r':s.sendto(b'=020600\r' if name==b'SYN' else b'WRONG',a)
  else:s.sendto(name+data,a)
threading.Thread(target=tcp,daemon=True).start();threading.Thread(target=udp,daemon=True).start()
print('READY',flush=True);threading.Event().wait()
'''
            server = subprocess.Popen(['ip', 'netns', 'exec', remote, 'python3', '-u', '-c', code, 'AM5N' if index == 0 else 'SYN'], stdout=subprocess.PIPE)
            servers.append(server); assert server.stdout.readline().strip() == b'READY'
            rows.append({'id': kind, 'connected': True, 'relay_ready': True, 'state': 'online'})
        manager.sync(profiles, rows, paths)
        assert all(r['client_endpoint']['ready'] for r in rows), rows
        for ns, host in ((names[0], '192.168.42.1'), (names[1], '192.168.77.1')):
            client(ns, host, 14030, 'tcp', b'AM5N#AM5N#')
            manager.release_owner('synscan')
            client(ns, host, 21180, 'udp', b'=020600\r')
        print('PASS same target/client IPv4 in two independent namespaces; real LAN and hotspot TCP/UDP replies without client routes', flush=True)
        # One UDP controller across home/AP; refusal cannot disturb its replies.
        manager.release_owner('synscan')
        owner_code = r'''import socket,time
with socket.socket(type=socket.SOCK_DGRAM) as s:
 s.settimeout(2)
 for index in range(20):
  s.sendto(b'owner',('192.168.42.1',21180));data,origin=s.recvfrom(100)
  assert data==b'SYNowner' and origin==('192.168.42.1',21180),(data,origin)
  if index==0:print('OWNED',flush=True)
  time.sleep(.05)
'''
        owner_worker = subprocess.Popen(['ip','netns','exec',names[0],'python3','-u','-c',owner_code],
                                        stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        assert owner_worker.stdout.readline().strip() == b'OWNED'
        blocked_code = r'''import socket
with socket.socket(type=socket.SOCK_DGRAM) as s:
 s.settimeout(.3);s.sendto(b'refused',('192.168.77.1',21180))
 try:s.recvfrom(100)
 except socket.timeout:pass
 else:raise AssertionError('Foreign UDP client received a reply')
'''
        blocked = subprocess.run(['ip','netns','exec',names[1],'python3','-c',blocked_code],capture_output=True,timeout=3)
        assert blocked.returncode == 0, blocked.stderr
        _, error = owner_worker.communicate(timeout=5); assert owner_worker.returncode == 0, error
        assert manager.owner_status('synscan')['refused_clients'] == 1
        manager.release_owner('synscan')
        client(names[1], '192.168.77.1', 21180, 'udp', b'=020600\r')
        print('PASS UDP owner survives foreign client on other path; explicit release allows takeover', flush=True)
        # Concurrent TCP sessions retain their independent upstream sockets.
        workers = []
        for index in range(6):
            code = r'''import socket,sys
host=sys.argv[1]
for _ in range(15):
 with socket.create_connection((host,14030),timeout=2) as s:
  s.sendall(b':GVP#:GVP#');s.shutdown(socket.SHUT_WR);data=b''
  while True:
   part=s.recv(100)
   if not part:break
   data+=part
  assert data==b'AM5N#AM5N#',data
'''
            workers.append(subprocess.Popen(['ip','netns','exec',names[index%2],'python3','-c',code,
                                            '192.168.42.1' if index%2==0 else '192.168.77.1',str(index)],stdout=subprocess.PIPE,stderr=subprocess.PIPE))
        for worker in workers:
            _, error = worker.communicate(timeout=30); assert worker.returncode == 0, error
        print('PASS simultaneous TCP reconnects/pipelining with six independent clients, no wrong-device replies', flush=True)
        # Root namespace has no instrument route; prohibited service is filtered
        # even when a caller deliberately creates a socket in the wrong namespace.
        for profile in profiles:
            record = manager.records[profile['mac']]
            forbidden = 'udp' if profile['kind']=='zwo_mount' else 'tcp'
            code = "import socket; s=socket.socket(type="+('socket.SOCK_DGRAM' if forbidden=='udp' else 'socket.SOCK_STREAM')+");s.settimeout(.3);"
            code += "s.sendto(b':e1\\r',('192.168.4.1',11880));s.recvfrom(100)" if forbidden=='udp' else "s.connect(('192.168.4.1',4030))"
            result = subprocess.run(['ip','netns','exec',record['namespace'],'python3','-c',code],capture_output=True)
            assert result.returncode != 0, 'Namespace allowed an undeclared service'
            routes = cmd('ip','-n',record['namespace'],'-j','route','show')
            assert not any(r.get('dst')=='default' for r in json.loads(routes))
        assert not any(r.get('dst')=='192.168.4.0/24' for r in json.loads(cmd('ip','-j','route','show')))
        print('PASS no root instrument routes, no namespace default route, undeclared TCP/UDP services blocked', flush=True)
        # Stop one transport and assert the other's listener and socket survive.
        survivor = next(relay for key, relay in manager.relays.items() if key[0]=='synscan' and key[1]=='192.168.42.1')
        profiles[0]['enabled'] = False; rows[0].update(connected=False, state='saved')
        ownership_before = manager.owner_status('synscan')
        manager.sync(profiles, rows, paths)
        assert manager.owner_status('synscan') == ownership_before
        assert survivor is next(relay for key, relay in manager.relays.items() if key[0]=='synscan' and key[1]=='192.168.42.1')
        manager.release_owner('synscan')
        client(names[0], '192.168.42.1', 21180, 'udp', b'=020600\r')
        profiles[0]['enabled'] = True; rows[0].update(connected=True, state='online')
        manager.sync(profiles, rows, paths)
        client(names[0], '192.168.42.1', 14030, 'tcp', b'AM5N#AM5N#')
        assert manager.counters['synscan'].snapshot()['bytes'] > 0
        print('PASS profile disable/re-enable preserves other live transport and endpoint allocation', flush=True)
        print(json.dumps({'result':'PASS','simulated_instruments':2,'real_target_ip':'192.168.4.1',
                          'endpoints':[r['client_endpoint'] for r in rows],
                          'physical_wifi_validated':False,'metrics':[c.snapshot() for c in manager.counters.values()]}), flush=True)
finally:
    if manager:
        for ident in list(manager.counters): manager.close_profile(ident)
    for server in servers:
        server.terminate()
        try: server.wait(timeout=2)
        except subprocess.TimeoutExpired: server.kill(); server.wait()
    for name in names: cmd('ip','netns','delete',name,check=False)
