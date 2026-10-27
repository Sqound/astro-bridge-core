"""Real nft counter attribution in disposable Linux namespaces; no hardware claim."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from wifi_bridge.activity import ForwardActivity


def cmd(*args, check=True):
    return subprocess.run(args,check=check,text=True,capture_output=True).stdout.strip()

suffix=str(os.getpid());names=['abc-client-'+suffix,'abc-scope-'+suffix]
server=None
try:
    for name in names:
        cmd('ip','netns','add',name);cmd('ip','-n',name,'link','set','lo','up')
    for iface,peer,ns,local,remote in [('abc0','abcp0',names[0],'192.168.42.1/24','192.168.42.2/24'),('abc1','abcp1',names[1],'10.0.0.2/24','10.0.0.1/24')]:
        cmd('ip','link','add',iface,'type','veth','peer','name',peer);cmd('ip','link','set',peer,'netns',ns)
        cmd('ip','addr','add',local,'dev',iface);cmd('ip','link','set',iface,'up')
        cmd('ip','-n',ns,'addr','add',remote,'dev',peer);cmd('ip','-n',ns,'link','set',peer,'up')
    cmd('ip','-n',names[0],'route','add','10.0.0.0/24','via','192.168.42.1')
    cmd('ip','-n',names[1],'route','add','192.168.42.0/24','via','10.0.0.2')
    Path('/proc/sys/net/ipv4/ip_forward').write_text('1')
    with tempfile.TemporaryDirectory() as state:
        activity=ForwardActivity(state)
        mark=activity.attach('one','synthetic-association','10.0.0.1','10.0.0.2','192.168.42.1','192.168.42.0/24',False,'abc0','abc1',kind='vespera')
        activity.configure([],lambda script:subprocess.run(['nft','-f','-'],input=script,text=True,capture_output=True,check=True))
        assert not activity.catalog['coverage_lost']
        server_code='''import socket,threading,time
for port in (8083,8082,4700,4800,4801,4804,4554,4555,4999):
 def serve(port=port):
  s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('10.0.0.1',port));s.listen()
  while True:
   c,_=s.accept();request=c.recv(100);c.sendall(b'x'*(65536 if port in (8082,4555) or request==b'bulk' else 128));c.close()
 threading.Thread(target=serve,daemon=True).start()
time.sleep(60)
'''
        server=subprocess.Popen(['ip','netns','exec',names[1],'python3','-c',server_code])
        time.sleep(.3)
        def read():
            data=json.loads(cmd('nft','-j','list','table','ip','wifi_bridge_activity'))
            return {c['name']:c['bytes'] for row in data['nftables'] if (c:=row.get('counter'))}
        def probe(port,bulk=False):
            expected=65536 if port in (8082,4555) or bulk else 128
            request="bulk" if bulk else "status"
            cmd('ip','netns','exec',names[0],'python3','-c',"import socket; s=socket.create_connection(('10.0.0.1',"+str(port)+"),timeout=2);s.sendall("+repr(request.encode())+");data=b''\nwhile chunk:=s.recv(65536):data+=chunk\nassert len(data)=="+str(expected))
            time.sleep(.1)
        probe(8083);first=read()
        raw=lambda value:sum(value['ab_'+str(mark)+'_'+d] for d in ('out','in'))
        control=lambda value:sum(value['ab_'+str(mark)+'_control_'+d] for d in ('out','in'))
        assert 0<control(first)<=raw(first),first
        probe(8082);second=read()
        assert raw(second)-raw(first)>65536 and control(second)==control(first),(first,second)
        handle,named=activity.counter_snapshot()
        assert handle>0 and {name:pair[1] for name,pair in named.items()}==second
        activity.configure([],lambda script:subprocess.run(['nft','-f','-'],input=script,text=True,capture_output=True,check=True))
        assert read()==second,'Unchanged rule plan must retain cumulative counters'
        activity.attach('one','synthetic-association','10.0.0.1','10.0.0.2','192.168.42.1','192.168.42.0/24',False,'abc0','abc1',kind='seestar')
        activity.configure([],lambda script:subprocess.run(['nft','-f','-'],input=script,text=True,capture_output=True,check=True))
        # Same target IP, different instrument family: Singularity probes closed
        # Vespera ports while Seestar owns the radio. No data may bypass activity.
        server.terminate();server.wait(timeout=3)
        server=subprocess.Popen(['ip','netns','exec',names[1],'python3','-c',server_code.replace('8083,8082,','')])
        time.sleep(.3)
        for port in (8082,8083):
            previous=read()
            cmd('ip','netns','exec',names[0],'python3','-c',
                'import socket; s=socket.socket();s.settimeout(2);assert s.connect_ex(("10.0.0.1",'+str(port)+'))!=0;s.close()')
            current=read()
            assert raw(current)>raw(previous) and raw(current)-raw(previous)==control(current)-control(previous),(port,previous,current)
        raw_syn='''import socket,struct,sys
port,offset,extra,payload=map(int,sys.argv[1:])
def checksum(data):
 if len(data)%2:data+=b'\\0'
 value=sum(struct.unpack('!'+str(len(data)//2)+'H',data));value=(value>>16)+(value&65535);value+=(value>>16)
 return (~value)&65535
src=socket.inet_aton('192.168.42.2');dst=socket.inet_aton('10.0.0.1')
tcp=struct.pack('!HHIIBBHHH',51000+offset+extra+payload,port,1,0,offset<<4,2,4096,0,0)+b'\\1'*(offset*4-20)+b'x'*payload
check=checksum(src+dst+struct.pack('!BBH',0,6,len(tcp))+tcp);tcp=tcp[:16]+struct.pack('!H',check)+tcp[18:]
ip=struct.pack('!BBHHHBBH4s4s',0x45+extra//4,0,20+extra+len(tcp),1,0,64,6,0,src,dst)+b'\\1'*extra
ip=ip[:10]+struct.pack('!H',checksum(ip))+ip[12:]
s=socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_RAW);s.sendto(ip+tcp,('10.0.0.1',0));s.close()
'''
        for offset,extra,payload,background in ((5,0,0,True),(11,0,0,True),(15,0,0,True),(11,0,1,False),(5,4,0,False)):
            previous=read()
            cmd('ip','netns','exec',names[0],'python3','-c',raw_syn,'8083',str(offset),str(extra),str(payload))
            time.sleep(.1);current=read();delta=raw(current)-raw(previous);classified=control(current)-control(previous)
            assert delta>0 and (delta==classified if background else delta>classified),(offset,extra,payload,previous,current)
        print('PASS: real same-target Vespera SYN/reset retries are bounded background; SYN payload and IPv4 options stay protected',flush=True)
        server.terminate();server.wait(timeout=3)
        server=subprocess.Popen(['ip','netns','exec',names[1],'python3','-c',server_code])
        time.sleep(.3);previous=read();probe(8082);current=read()
        assert raw(current)-raw(previous)>65536 and control(current)-control(previous)<512,(previous,current)
        print('PASS: successful foreign-port connection and content still prevent a quiet interval',flush=True)
        previous=read()
        for port in (4700,4800,4801,4804):
            probe(port);current=read()
            assert 0<control(current)-control(previous)<=raw(current)-raw(previous),(port,previous,current)
            previous=current
        for port in (4554,4555,4999):
            probe(port);current=read()
            assert raw(current)>raw(previous) and control(current)==control(previous),(port,previous,current)
            previous=current
        probe(4800,bulk=True);current=read()
        assert raw(current)-raw(previous)>65536 and control(current)-control(previous)>65536
        print('PASS: Seestar small native-service channels counted; preview/unknown ports excluded; large service transfer retained',flush=True)
        print('PASS: actual nft TCP status counters separate from mixed content, preserve cumulative counters; first-install coverage remains unknown',flush=True)
finally:
    if server:server.terminate();server.wait(timeout=3)
    for name in names:cmd('ip','netns','delete',name,check=False)
    for iface in ('abc0','abc1'):cmd('ip','link','delete',iface,check=False)
    cmd('nft','delete','table','ip','wifi_bridge_activity',check=False)
