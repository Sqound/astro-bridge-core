"""Actual simultaneous instrument traffic in isolated Linux network namespaces."""
import os
from pathlib import Path
import subprocess
import time
import tempfile
import json

from wifi_bridge.network import LinuxNetwork


def command(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True).stdout.strip()


suffix = str(os.getpid())
clients = ["wbm-home-" + suffix, "wbm-a-" + suffix, "wbm-b-" + suffix, "wbm-ap-" + suffix]
devices = [{"name": name} for name in ("wbmh0", "wbma0", "wbmb0", "wbmap0")]
servers = []
holds = []
state = tempfile.TemporaryDirectory()
try:
    for ns, device, index, addresses in zip(clients, devices, range(4),
            [("192.168.42.1/24", "192.168.42.2/24"), ("10.0.0.2/24", "10.0.0.1/24"), ("192.168.4.2/24", "192.168.4.1/24"), ("192.168.77.1/24", "192.168.77.12/24")]):
        command("ip", "netns", "add", ns)
        command("ip", "-n", ns, "link", "set", "lo", "up")
        peer = "wbmp" + str(index)
        command("ip", "link", "add", device["name"], "type", "veth", "peer", "name", peer)
        command("ip", "link", "set", peer, "netns", ns)
        command("ip", "addr", "add", addresses[0], "dev", device["name"])
        command("ip", "link", "set", device["name"], "up")
        command("ip", "-n", ns, "addr", "add", addresses[1], "dev", peer)
        command("ip", "-n", ns, "link", "set", peer, "up")
    Path("/proc/sys/net/ipv4/ip_forward").write_text("1")
    net = LinuxNetwork(state.name)
    net.association_identity = lambda device: "namespace-owned-"+device["name"]
    net.firewall(home=devices[0], telescopes=[
        {"profile_id":"one", "device": devices[1], "target": "10.0.0.1", "legacy_ports": True},
        {"profile_id":"two", "device": devices[2], "target": "192.168.4.1", "legacy_ports": False}])
    command("ip", "-n", clients[0], "route", "add", "10.0.0.0/24", "via", "192.168.42.1")
    command("ip", "-n", clients[0], "route", "add", "192.168.4.0/24", "via", "192.168.42.1")
    server_code = '''import socket,sys,threading
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind((sys.argv[1],8082));s.listen()
def serve(c):
 try:
  data=c.recv(1024);c.sendall(sys.argv[1].encode()+b":"+data)
  if data==b"hold":c.recv(1)
 finally:c.close()
while True:
 c,a=s.accept();threading.Thread(target=serve,args=(c,),daemon=True).start()
'''
    for ns, target in zip(clients[1:], ("10.0.0.1", "192.168.4.1")):
        servers.append(subprocess.Popen(["ip", "netns", "exec", ns, "python3", "-c", server_code, target]))
    client_code = '''import socket
from concurrent.futures import ThreadPoolExecutor
def probe(pair):
 host,expected=pair
 with socket.create_connection((host,8082),timeout=2) as s:
  s.sendall(b"test");assert s.recv(1024)==expected.encode()+b":test"
with ThreadPoolExecutor(max_workers=3) as pool:
 list(pool.map(probe,[("10.0.0.1","10.0.0.1"),("192.168.4.1","192.168.4.1"),("192.168.42.1","10.0.0.1")]))
'''
    for _ in range(20):
        result = subprocess.run(["ip", "netns", "exec", clients[0], "python3", "-c", client_code], capture_output=True)
        if not result.returncode:
            break
        time.sleep(.1)
    else:
        raise AssertionError("Concurrent routed instruments and legacy DNAT did not return the expected payloads")
    print("PASS: two instruments routed concurrently; legacy DNAT reaches only its selected target", flush=True)
    def measure():
        net.telemetry.next_sample=0;net.sample_activity()
        return net.activity_status()['profiles']
    first=measure();assert first['one']['packets']>0 and first['two']['packets']>0,first
    hold_code='import socket,sys; s=socket.create_connection((sys.argv[1],8082),timeout=2);s.sendall(b"hold");s.recv(1024);print("READY",flush=True);sys.stdin.read()'
    def hold(address, namespace=None):
        child=subprocess.Popen(['ip','netns','exec',namespace or clients[0],'python3','-u','-c',hold_code,address],stdin=subprocess.PIPE,stdout=subprocess.PIPE)
        holds.append(child);assert child.stdout.readline().strip()==b'READY';return child
    hold('192.168.42.1');hold('192.168.4.1');time.sleep(.3)
    active=measure();assert active['one']['tcp_established']>=1 and active['two']['tcp_established']>=1,active
    assert all(v['state']!='quiet' for v in active.values()) # First installation coverage remains honestly unknown.
    before={k:v['packets'] for k,v in active.items()}
    assert net.probe('10.0.0.1',devices[1])
    time.sleep(.3)
    probes=measure();assert {k:v['packets'] for k,v in probes.items()}==before,probes
    command('ip','netns','exec',clients[0],'python3','-c','import socket;s=socket.socket(type=socket.SOCK_DGRAM);s.sendto(b"generic discovery",("10.0.0.1",4720))')
    assert {k:v['packets'] for k,v in measure().items()}==before
    print('PASS: owned routed/DNAT+SNAT TCP sessions counted; LOCAL bridge probes and generic UDP excluded',flush=True)
    # Add an independent AP ingress while old home TCP remains completely silent.
    entries=[{'profile_id':'one','device':devices[1],'target':'10.0.0.1','legacy_ports':True},
             {'profile_id':'two','device':devices[2],'target':'192.168.4.1','legacy_ports':False}]
    command('ip','-n',clients[3],'route','add','10.0.0.0/24','via','192.168.77.1')
    command('ip','-n',clients[3],'route','add','192.168.4.0/24','via','192.168.77.1')
    command('ip','-n',clients[3],'route','add','192.168.42.0/24','via','192.168.77.1')
    command('ip','-n',clients[0],'route','add','192.168.77.0/24','via','192.168.42.1')
    net.firewall(home=devices[0],setup=devices[3],telescopes=entries)
    ap_code=client_code.replace('192.168.42.1','192.168.77.1')
    command('ip','netns','exec',clients[3],'python3','-c',ap_code)
    hold('192.168.77.1',clients[3]);time.sleep(.2)
    paths=measure();assert paths['one']['tcp_established']>=2,paths
    assert not any(r['gap_latched'] for r in net.telemetry.catalog['records'].values())
    highwater=net.telemetry.catalog['next']
    net.firewall(setup=devices[3],telescopes=entries)
    command('ip','netns','exec',clients[3],'python3','-c',ap_code)
    paths=measure();assert paths['one']['tcp_established']>=2,paths
    assert paths['one']['reason']!='STALE_ASSOCIATION_SESSIONS',paths
    net.firewall(home=devices[0],setup=devices[3],telescopes=entries)
    assert net.telemetry.catalog['next']==highwater
    assert measure()['one']['tcp_established']>=2
    servers.append(subprocess.Popen(['ip','netns','exec',clients[0],'python3','-c',server_code,'192.168.42.2']))
    time.sleep(.2)
    lateral=subprocess.run(['ip','netns','exec',clients[3],'python3','-c','import socket;socket.create_connection(("192.168.42.2",8082),timeout=.5)'],capture_output=True)
    assert lateral.returncode!=0,'AP must not forward to home clients'
    print('PASS: AP-only/dual routed and DNAT paths; old home silent TCP survives AP addition/home withdrawal; no lateral AP-home forwarding',flush=True)
    # DHCP can install a preferred conflicting route even if a logical plan omits home.
    command('ip','route','del','10.0.0.0/24','dev',devices[1]['name'])
    command('ip','route','add','10.0.0.0/24','dev',devices[1]['name'],'metric','600')
    command('ip','addr','add','10.0.0.100/24','dev',devices[0]['name'],'noprefixroute')
    command('ip','route','add','10.0.0.0/24','dev',devices[0]['name'],'metric','100')
    assert devices[0]['name'] in command('ip','route','get','10.0.0.1')
    fib_blocked=subprocess.run(['ip','netns','exec',clients[3],'python3','-c','import socket;socket.create_connection(("10.0.0.1",8082),timeout=.5)'],capture_output=True)
    assert fib_blocked.returncode!=0,'A logical omitted ingress cannot override an actual conflicting kernel route'
    command('ip','route','del','10.0.0.0/24','dev',devices[0]['name'],'metric','100')
    command('ip','addr','del','10.0.0.100/24','dev',devices[0]['name'])
    command('ip','netns','exec',clients[3],'python3','-c',ap_code)
    assert measure()['one']['tcp_established']>=2
    print('PASS: conflicting home metric100 beats instrument600; AP forwarding cannot be called ready until routing conflict is resolved',flush=True)
    # A new association generation with unchanged interface/IP cannot inherit the
    # old silent established session as a new known-zero connection.
    net.association_identity=lambda device:'new-active-object-'+device['name']
    entries=[{'profile_id':'one','device':devices[1],'target':'10.0.0.1','legacy_ports':True},
             {'profile_id':'two','device':devices[2],'target':'192.168.4.1','legacy_ports':False}]
    net.firewall(home=devices[0],telescopes=entries)
    assert measure()['one']['reason']=='STALE_ASSOCIATION_SESSIONS'
    # A FOREIGN nonzero mark stays untouched. Its gap counter outlives main-table
    # replacement and an app restart even when that foreign session goes silent.
    command('nft','add','table','ip','activity_test')
    command('nft','add','chain','ip','activity_test','pre','{ type filter hook prerouting priority -150; policy accept; }')
    command('nft','add','rule','ip','activity_test','pre','iifname',devices[0]['name'],'ip','daddr','10.0.0.1','tcp','dport','8082','ct','state','new','ct','mark','set','0x12345678')
    hold('10.0.0.1');time.sleep(.2)
    assert measure()['one']['reason']=='MARK_COLLISION'
    foreign=command('/usr/sbin/conntrack','-L','-f','ipv4','--mark','0x12345678','-p','tcp','--state','ESTABLISHED')
    assert 'mark=305419896' in foreign
    net.last_firewall=None;net.firewall(home=devices[0],telescopes=entries)
    assert measure()['one']['reason']=='MARK_COLLISION'
    net=LinuxNetwork(state.name);net.association_identity=lambda device:'new-active-object-'+device['name']
    net.firewall(home=devices[0],telescopes=entries);assert measure()['one']['reason']=='MARK_COLLISION'
    print('PASS: foreign ct marks preserved; unclassified gap persists across main-table reset/app restart',flush=True)
    from wifi_bridge.activity import PREFIX
    unknown=PREFIX+60000
    command('nft','add','rule','ip','activity_test','pre','iifname',devices[0]['name'],'ip','daddr','192.168.4.1','tcp','dport','8082','ct','state','new','ct','mark','set',str(unknown))
    hold('192.168.4.1');time.sleep(.2);assert measure()['two']['state']=='unknown'
    assert str(unknown) not in net.telemetry.catalog['records']
    command('nft','delete','table','ip','wifi_bridge_activity');assert measure()['two']['state']=='unknown'
    net.telemetry.configured=False;net.last_firewall=None;net.firewall(home=devices[0],telescopes=entries)
    assert measure()['two']['state']=='unknown';assert net.telemetry.catalog['coverage_lost']
    # Exercise the advertised bounded maximum, including retained classifiers.
    for index in range(88-len(net.telemetry.catalog['records'])):
        net.telemetry.attach('one','max-shape-association','10.0.0.1','10.0.0.2','192.168.42.'+str(index+3),'192.168.42.0/24',True,devices[0]['name'],devices[1]['name'],kind='vespera')
    net.telemetry.configure(entries,lambda script:subprocess.run(['nft','-f','-'],input=script,text=True,capture_output=True,check=True))
    data=command('nft','-a','-j','list','table','ip','wifi_bridge_activity')
    assert len(data.encode())<1048576;assert net.telemetry.path.stat().st_size<65536
    assert len(net.telemetry.catalog['records'])==88;assert isinstance(measure()['one']['packets'],int)
    print('PASS: 88-generation maximum collected within bounded nft/catalog sizes: '+str(len(data.encode()))+'/'+str(net.telemetry.path.stat().st_size)+' bytes',flush=True)
    print('PASS: same-prefix unknown marks never adopted; activity-table loss latches coverage unknown',flush=True)
    net.firewall(isolated=devices[1:])
    blocked = subprocess.run(["ip", "netns", "exec", clients[0], "python3", "-c", client_code], capture_output=True)
    assert blocked.returncode != 0, "Unconfigured instrument interfaces must not forward packets"
    print("PASS: removing relays keeps instrument radios isolated", flush=True)
finally:
    for child in holds:
        if child.stdin:child.stdin.close()
        try:child.wait(timeout=3)
        except subprocess.TimeoutExpired:child.kill();child.wait()
        if child.stdout:child.stdout.close()
    state.cleanup()
    for server in servers:
        server.terminate();server.wait(timeout=3)
    for ns in clients:
        command("ip", "netns", "delete", ns, check=False)
    for device in devices:
        command("ip", "link", "delete", device["name"], check=False)
