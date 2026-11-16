"""Private namespace side of the optional Linux service. No host discovery."""
import ipaddress
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import threading
import time
from xml.parsers import expat


def healthy(port):
    """Recognize a property definition without keeping its contents or requesting BLOBs."""
    found = False
    def start(name, attrs):
        nonlocal found
        if name in ('defTextVector', 'defNumberVector', 'defSwitchVector', 'defLightVector', 'defBLOBVector'):
            found = found or bool(attrs.get('device') and attrs.get('name'))

    deadline = time.monotonic() + 0.35
    parser = expat.ParserCreate()
    parser.StartElementHandler = start
    try:
        parser.Parse(b'<properties>')
        with socket.create_connection(('127.0.0.1', port), timeout=0.25) as peer:
            peer.sendall(b'<getProperties version="1.7"/>\n')
            remaining = 8192
            while remaining and not found:
                peer.settimeout(max(0.001, deadline - time.monotonic()))
                block = peer.recv(min(remaining, 1024))
                if not block or time.monotonic() >= deadline:
                    break
                remaining -= len(block)
                parser.Parse(block)
    except (OSError, expat.ExpatError):
        return False
    return found


def forward(peer, port, slots):
    try:
        with peer, socket.create_connection(('127.0.0.1', port), timeout=0.5) as backend:
            peer.settimeout(2)
            backend.settimeout(2)
            readers = {peer: backend, backend: peer}
            while readers:
                readable = select.select(list(readers), [], [], 120)[0]
                if not readable:
                    break
                for source in readable:
                    block = source.recv(32768)
                    if block:
                        readers[source].sendall(block)
                    else:
                        readers.pop(source).shutdown(socket.SHUT_WR)
    except OSError:
        pass
    finally:
        slots.release()


def serve(control, command, port, guard_fd, verify):
    """Run only with inherited authenticated listeners from the platform supervisor."""
    control.settimeout(3)
    def exact(size):
        data = bytearray()
        while len(data) < size:
            part = control.recv(size - len(data))
            if not part:
                raise EOFError
            data.extend(part)
        return bytes(data)

    marker, fds, flags, _ = socket.recv_fds(control, 1, 2)
    listeners = [socket.socket(fileno=fd) for fd in fds]
    child = None
    try:
        size = int.from_bytes(exact(2), 'big')
        if marker != b'L' or not 1 <= size <= 2048:
            return
        rules = json.loads(exact(size))
        if flags & socket.MSG_CTRUNC or not 1 <= len(fds) == len(rules) <= 2:
            return
        access = {s: tuple(ipaddress.IPv4Network(net) for net in nets) for s, nets in zip(listeners, rules)}
        for listener in listeners:
            listener.setblocking(False)
        if not verify() or socket.send_fds(control, [b'P'], [guard_fd]) != 1 or exact(1) != b'G':
            return
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        slots = threading.BoundedSemaphore(8)
        check, last_parent, ready = 0, time.monotonic(), False
        while child.poll() is None and time.monotonic() - last_parent < 3:
            if time.monotonic() >= check:
                if not verify():
                    return
                ready = healthy(port)
                control.sendall(b'1' if ready else b'0')
                check = time.monotonic() + 1
            for sock in select.select([control, *listeners], [], [], 0.25)[0]:
                if sock is control:
                    if not control.recv(16):
                        return
                    last_parent = time.monotonic()
                else:
                    try:
                        peer, address = sock.accept()
                    except (BlockingIOError, ConnectionAbortedError):
                        continue
                    if ready and any(ipaddress.IPv4Address(address[0]) in net for net in access[sock]) and slots.acquire(False):
                        threading.Thread(target=forward, args=(peer, port, slots), daemon=True).start()
                    else:
                        peer.close()
    finally:
        for sock in listeners:
            sock.close()
        if child is not None:
            child.kill()
            child.wait(timeout=1)


def confined(node, port_path, vendor, product, *, sysfs=Path('/sys/bus/usb/devices'), usbfs=Path('/dev/bus/usb')):
    # This worker is the PID namespace init. The pidfd is signalled only once
    # the kernel has killed and waited for every remaining namespace member.
    if os.getpid() != 1 or os.geteuid() == 0 or socket.if_nameindex() != [(1, 'lo')]:
        return False
    attrs = sysfs / port_path
    def number(key, base):
        with (attrs / key).open('rb') as stream:
            data = stream.read(17)
        if len(data) > 16:
            raise ValueError('invalid_usb_attribute')
        return int(data, base)
    return (usbfs / f"{number('busnum', 10):03}" / f"{number('devnum', 10):03}" == node
            and number('idVendor', 16) == vendor and number('idProduct', 16) == product
            and list(usbfs.glob('*/*')) == [node] and node.is_char_device()
            and os.access(node, os.R_OK | os.W_OK))


if __name__ == '__main__':
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    node, port_path, vendor, product = sys.argv[1:]
    with socket.socket(fileno=0) as channel:
        guard = os.pidfd_open(os.getpid())
        try:
            serve(channel, ['/usr/bin/indiserver', '-p', '7624', 'indi_asi_ccd'], 7624, guard,
                  lambda: confined(Path(node), port_path, int(vendor), int(product)))
        finally:
            os.close(guard)
