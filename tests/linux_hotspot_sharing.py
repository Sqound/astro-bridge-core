"""Real DNS/NAT/direction checks in a NEW disposable --network none container.

Run explicitly as root. Refuses a non-container or any pre-existing network link
other than loopback. No physical interfaces, external packets or existing services.
"""
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from wifi_bridge.activity import ForwardActivity
from wifi_bridge.network import LinuxNetwork
import wifi_bridge.network as network


def cmd(*args):
    return subprocess.run(args,check=True,text=True,capture_output=True,timeout=8).stdout.strip()


def main():
    assert os.geteuid()==0 and Path('/.dockerenv').exists(),'Disposable Docker container required.'
    assert {row['ifname'] for row in json.loads(cmd('ip','-j','link','show'))}=={'lo'},'Network must be isolated and unused.'
    cmd('ip','link','set','lo','up')
    prefix='abs'+str(os.getpid());names=[prefix+x for x in ('c','r','t')];processes=[];dns=[None]
    interfaces=[('absap','abspc',names[0],'192.168.77.1/24','192.168.77.10/24'),
                ('abshome','abspr',names[1],'192.168.12.2/24','192.168.12.1/24'),
                ('abstel','abspt',names[2],'10.0.0.2/24','10.0.0.1/24')]
    try:
        for name in names:cmd('ip','netns','add',name);cmd('ip','-n',name,'link','set','lo','up')
        for iface,peer,ns,local,remote in interfaces:
            cmd('ip','link','add',iface,'type','veth','peer','name',peer)
            cmd('ip','link','set',peer,'netns',ns);cmd('ip','addr','add',local,'dev',iface);cmd('ip','link','set',iface,'up')
            cmd('ip','-n',ns,'addr','add',remote,'dev',peer);cmd('ip','-n',ns,'link','set',peer,'up')
        cmd('ip','-n',names[0],'route','add','default','via','192.168.77.1')
        cmd('ip','route','add','default','via','192.168.12.1','dev','abshome')
        cmd('ip','-n',names[1],'addr','add','8.8.8.8/32','dev','lo')  # Synthetic public endpoint, never external.
        cmd('ip','-n',names[1],'route','add','192.168.77.0/24','via','192.168.12.2')
        Path('/proc/sys/net/ipv4/ip_forward').write_text('1')
        server_code='''import socket,threading,time
for host,port in HOSTS:
 def serve(host=host,port=port):
  s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind((host,port));s.listen()
  while True:
   c,peer=s.accept()
   def echo(c=c,peer=peer):
    try:
     while c.recv(1024):c.sendall(peer[0].encode()+b'\\n')
    except OSError:pass
    finally:c.close()
   threading.Thread(target=echo,daemon=True).start()
 threading.Thread(target=serve,daemon=True).start()
time.sleep(120)
'''
        for ns,hosts in [(names[1],[('8.8.8.8',8088),('192.168.12.1',8088)]),
                         (names[2],[('10.0.0.1',8082)]),(names[0],[('192.168.77.10',8088)])]:
            processes.append(subprocess.Popen(['ip','netns','exec',ns,'python3','-c',server_code.replace('HOSTS',repr(hosts))],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
        processes.append(subprocess.Popen(['ip','netns','exec',names[1],'dnsmasq','--keep-in-foreground','--port=53','--bind-interfaces','--listen-address=192.168.12.1','--no-resolv','--no-hosts','--address=/fixture.test/8.8.8.8','--user=root'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
        time.sleep(.3)
        def client(ns,host,port=8088,allowed=True,source=None):
            code='''import socket
s=socket.socket();s.settimeout(.6)
SOURCE
try:
 s.connect((HOST,PORT));s.sendall(b'fixture');reply=s.recv(128);assert reply;print(reply.decode().strip())
except (OSError,AssertionError):print('BLOCKED')
finally:s.close()
'''.replace('SOURCE',f's.bind(({source!r},0))' if source else '').replace('HOST',repr(host)).replace('PORT',str(port))
            result=cmd('ip','netns','exec',ns,'python3','-c',code)
            assert (result!='BLOCKED')==allowed,(host,allowed,result)
            return result
        def dns_query(ns,allowed=True):
            code='''import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(.6)
q=b'\\x12\\x34\\x01\\x00\\x00\\x01'+b'\\x00'*6+b'\\x07fixture\\x04test\\x00\\x00\\x01\\x00\\x01'
try:
 s.sendto(q,('192.168.77.1',53));r=s.recv(1024);print('ANSWER' if r[-4:]==b'\\x08'*4 else 'INVALID')
except OSError:print('BLOCKED')
finally:s.close()
'''
            result=cmd('ip','netns','exec',ns,'python3','-c',code);assert result==('ANSWER' if allowed else 'BLOCKED'),result
        with tempfile.TemporaryDirectory() as state:
            net=LinuxNetwork(state);original_run=network.run
            activity=ForwardActivity(state)
            mark=activity.attach('scope','fixture-association','10.0.0.1','10.0.0.2','192.168.77.1','192.168.77.0/24',True,'absap','abstel',kind='vespera')
            assert mark
            activity.configure([],lambda script:subprocess.run(['nft','-f','-'],input=script,text=True,capture_output=True,check=True))
            net.telemetry=activity
            def run(args,**kwargs):
                if args[0]=='nmcli':return 'GENERAL.CONNECTION:wifi-bridge-home\nIP4.GATEWAY:192.168.12.1\nIP4.DNS[1]:192.168.12.1'
                if args==['systemctl','restart','wifi-bridge-dhcp']:
                    if dns[0]:dns[0].terminate();dns[0].wait(timeout=3)
                    dns[0]=subprocess.Popen(['dnsmasq','--keep-in-foreground','--conf-file='+str(Path(state,'dnsmasq.conf'))],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    time.sleep(.1);assert dns[0].poll() is None;return ''
                return original_run(args,**kwargs)
            home={'name':'abshome'};ap={'name':'absap'};scope={'name':'abstel'}
            row={'profile_id':'scope','kind':'vespera','device':scope,'target':'10.0.0.1','legacy_ports':True}
            def apply(sharing=True,private=False):
                return net.firewall(home=home,setup=ap,telescopes=[row],sharing=sharing,private_lan=private,hotspot_targets=['10.0.0.1'])
            def counters():
                rows=json.loads(cmd('nft','-j','list','table','ip','wifi_bridge_activity'))['nftables']
                return {v['name']:(v['packets'],v['bytes']) for r in rows if (v:=r.get('counter'))}
            def held(host,port):
                code='''import socket,sys
s=socket.create_connection((HOST,PORT),timeout=1);s.settimeout(.6);print('READY',flush=True)
for line in sys.stdin:
 try:s.sendall(b'fixture');print('OK' if s.recv(128) else 'BLOCKED',flush=True)
 except OSError:print('BLOCKED',flush=True)
s.close()
'''.replace('HOST',repr(host)).replace('PORT',str(port))
                p=subprocess.Popen(['ip','netns','exec',names[0],'python3','-u','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
                processes.append(p);assert p.stdout.readline().strip()=='READY';return p
            def tick(p,answer='OK'):
                p.stdin.write('tick\n');p.stdin.flush();assert p.stdout.readline().strip()==answer
            with patch.object(network,'run',side_effect=run),patch.object(net,'configure_activity'):
                status=apply();assert status['available'],status;dns_query(names[0]);dns_query(names[1],False)
                assert client(names[0],'8.8.8.8')=='192.168.12.2'
                client(names[0],'192.168.12.1',allowed=False);client(names[1],'192.168.77.10',allowed=False)
                print('PASS_PUBLIC_NAT_DNS_AP_ONLY_PRIVATE_AND_REVERSE_BLOCKED')
                cmd('ip','-n',names[0],'addr','add','192.168.12.99/32','dev','abspc')
                client(names[0],'8.8.8.8',allowed=False,source='192.168.12.99')
                before=counters();client(names[0],'8.8.8.8');assert counters()==before
                print('PASS_SPOOF_REJECTED_INTERNET_NOT_TELESCOPE_ACTIVITY')
                instrument=held('192.168.77.1',8082);tick(instrument)
                status=apply(private=True);assert status['available'],status;private=held('192.168.12.1',8088);tick(private);tick(instrument)
                status=apply(private=False);assert status['available'],status;tick(private,'BLOCKED');tick(instrument)
                raw=cmd('conntrack','-L','-f','ipv4','--mark',str(mark),'-p','tcp','--state','ESTABLISHED','-o','extended')
                assert 'ESTABLISHED' in raw
                print('PASS_PRIVATE_PERMISSION_REMOVAL_PRESERVES_TELESCOPE_SESSION_MARK')
                assert not apply(sharing=False)['available'];client(names[0],'8.8.8.8',allowed=False);tick(instrument);dns_query(names[0],False)
                status=apply();assert status['available'],status;cmd('ip','route','del','default','via','192.168.12.1','dev','abshome')
                assert not apply()['available'];client(names[0],'8.8.8.8',allowed=False);tick(instrument)
                print('PASS_ISOLATED_AND_UPLINK_LOSS_KEEP_AP_TELESCOPE')
    finally:
        for p in [dns[0],*processes]:
            if p and p.poll() is None:p.terminate()
        for p in [dns[0],*processes]:
            if p:
                try:p.wait(timeout=3)
                except subprocess.TimeoutExpired:p.kill();p.wait()
        for name in names:subprocess.run(['ip','netns','del',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


if __name__=='__main__':main()
