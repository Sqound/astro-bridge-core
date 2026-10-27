"""Real Linux packet test; run only inside an isolated privileged container.

The bridge is this network namespace. Two veth pairs connect private netns
representing a home client and a telescope. Never uses a host network device.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from wifi_bridge.network import LinuxNetwork, NetworkError


def command(*args, check=True):
    result = subprocess.run(args, text=True, capture_output=True, check=check)
    return result.stdout.strip()


client, scope = "wb-client-" + str(os.getpid()), "wb-scope-" + str(os.getpid())
home, telescope = {"name": "wbhome0"}, {"name": "wbscope0"}
server = None
try:
    for namespace in (client, scope):
        command("ip", "netns", "add", namespace)
        command("ip", "-n", namespace, "link", "set", "lo", "up")
    for bridge_if, peer, namespace, bridge_ip, peer_ip in (
        (home["name"], "wbhome1", client, "192.168.42.1/24", "192.168.42.2/24"),
        (telescope["name"], "wbscope1", scope, "10.0.0.2/24", "10.0.0.1/24"),
    ):
        command("ip", "link", "add", bridge_if, "type", "veth", "peer", "name", peer)
        command("ip", "link", "set", peer, "netns", namespace)
        command("ip", "addr", "add", bridge_ip, "dev", bridge_if)
        command("ip", "link", "set", bridge_if, "up")
        command("ip", "-n", namespace, "addr", "add", peer_ip, "dev", peer)
        command("ip", "-n", namespace, "link", "set", peer, "up")
    Path("/proc/sys/net/ipv4/ip_forward").write_text("1")
    network = LinuxNetwork()
    network.firewall(home=home, telescope=telescope)
    command("nft", "-c", "list", "table", "ip", "wifi_bridge")
    server_code = '''import socket
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(("10.0.0.1",8082));s.listen()
while True:
 c,a=s.accept();data=c.recv(1024);c.sendall(b"VESPERA:"+data);c.close()
'''
    server = subprocess.Popen(["ip", "netns", "exec", scope, "python3", "-u", "-c", server_code])
    client_code = '''import socket,sys
with socket.create_connection((sys.argv[1],8082),timeout=2) as s:
 s.sendall(b"bridge-test");assert s.recv(1024)==b"VESPERA:bridge-test"
'''
    for attempt in range(20):
        result = subprocess.run(["ip", "netns", "exec", client, "python3", "-c", client_code,
                                 "192.168.42.1"], capture_output=True)
        if result.returncode == 0:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("TCP 8082 DNAT relay did not respond")
    print("PASS: TCP forwarding through bridge home IP to telescope, with return SNAT", flush=True)
    command("ip", "-n", client, "route", "add", "10.0.0.0/24", "via", "192.168.42.1")
    command("ip", "netns", "exec", client, "python3", "-c", client_code, "10.0.0.1")
    print("PASS: direct routed access to 10.0.0.1", flush=True)
    network.firewall(setup=telescope)
    blocked = subprocess.run(["ip", "netns", "exec", client, "python3", "-c", client_code,
                              "10.0.0.1"], capture_output=True)
    assert blocked.returncode != 0, "Hotspot must be isolated from forwarding"
    print("PASS: hotspot mode blocks packet forwarding", flush=True)
    # Table replacement is idempotent and never flushes other application rules.
    command("nft", "add", "table", "ip", "foreign_table")
    network.firewall(home=home, telescope=telescope)
    command("nft", "list", "table", "ip", "foreign_table")
    print("PASS: unrelated network rules are preserved", flush=True)
finally:
    if server:
        server.terminate()
        server.wait(timeout=3)
    for namespace in (client, scope):
        command("ip", "netns", "delete", namespace, check=False)
    for interface in (home["name"], telescope["name"]):
        command("ip", "link", "delete", interface, check=False)
