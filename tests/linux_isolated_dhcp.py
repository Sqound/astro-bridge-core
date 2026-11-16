"""Actual BusyBox DHCP and lease-hook test; disposable Linux container only."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from wifi_bridge.isolated import namespace_firewall, private_text
from wifi_bridge.network import run

if os.environ.get('AB_DISPOSABLE_NETWORK_TEST') != '1' or os.geteuid() != 0:
    raise SystemExit('A disposable Linux container is required.')


def cmd(*args, check=True): return run(list(args), timeout=5, check=check)


def wait_for(predicate):
    deadline = time.monotonic()+8
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.1)
    raise AssertionError('DHCP state did not converge')


client, server = 'ab-dhcp-client', 'ab-dhcp-server'
children = []
try:
    for name in (client, server):
        cmd('ip','netns','add',name); cmd('ip','-n',name,'link','set','lo','up')
    cmd('ip','link','add','ab-dhcp0','type','veth','peer','name','ab-dhcp1')
    cmd('ip','link','set','ab-dhcp0','netns',client);cmd('ip','link','set','ab-dhcp1','netns',server)
    cmd('ip','-n',client,'link','set','ab-dhcp0','up')
    cmd('ip','-n',server,'address','add','192.168.4.1/24','dev','ab-dhcp1')
    cmd('ip','-n',server,'link','set','ab-dhcp1','up')
    run(['ip','netns','exec',client,'nft','-f','-'],input=namespace_firewall('192.168.4.1','udp',11880))
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        lease = folder/'lease.json'; hook = folder/'hook'
        private_text(hook,'#!'+sys.executable+'\nfrom wifi_bridge.isolated_dhcp import main\nmain()\n',mode=0o700)
        dns = subprocess.Popen(['ip','netns','exec',server,'dnsmasq','--keep-in-foreground','--port=0',
            '--no-resolv','--no-hosts','--bind-interfaces','--interface=ab-dhcp1',
            '--dhcp-range=192.168.4.20,192.168.4.25,255.255.255.0,1m','--dhcp-option=3,192.168.4.1',
            '--dhcp-option=6,8.8.8.8','--dhcp-option=121,10.0.0.0/8,192.168.4.1',
            '--dhcp-leasefile='+str(folder/'server.leases')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        children.append(dns);time.sleep(.3);assert dns.poll() is None
        environment = dict(os.environ,AB_INTERFACE='ab-dhcp0',AB_TARGET='192.168.4.1',AB_LEASE_FILE=str(lease))
        dhcp = subprocess.Popen(['ip','netns','exec',client,'busybox','udhcpc','-f','-n','-t','4','-T','1',
            '-i','ab-dhcp0','-s',str(hook)],env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        children.append(dhcp)
        wait_for(lease.exists)
        first = json.loads(lease.read_text())
        assert first['address'].startswith('192.168.4.') and first['address'].endswith('/24'),first
        routes = json.loads(cmd('ip','-n',client,'-j','-4','route','show'))
        assert len(routes)==1 and routes[0]['dst']=='192.168.4.0/24',routes
        assert lease.stat().st_mode & 0o777 == 0o600
        dhcp.send_signal(signal.SIGUSR1)
        wait_for(lambda: lease.exists() and json.loads(lease.read_text())['expires']>first['expires'])
        dhcp.send_signal(signal.SIGUSR2)
        wait_for(lambda: not lease.exists())
        addresses = json.loads(cmd('ip','-n',client,'-j','-4','address','show','dev','ab-dhcp0'))
        assert not any(row.get('addr_info') for row in addresses),addresses
        print('PASS actual DHCP lease, namespace firewall, renewal, release, private file and ignored gateway/DNS/classless routes',flush=True)
finally:
    for child in reversed(children):
        child.terminate()
        try: child.wait(timeout=2)
        except subprocess.TimeoutExpired: child.kill();child.wait()
    for name in (client,server):cmd('ip','netns','delete',name,check=False)
