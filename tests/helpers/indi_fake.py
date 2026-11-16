"""Disposable loopback deployment of a Python fake indiserver and real relay."""
from pathlib import Path
import os
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from wifi_bridge.cameras.indi_worker import serve
from wifi_bridge.cameras.listener import ManagementAccess

DEVICE = dict(port_path='1-2', bus=1, vendor_id=0x03c3, product_id=0x120a,
              device_class=0, interface_classes=[], speed_mbps=480)
PATH = ManagementAccess('127.0.0.1', '127.0.0.0/8', [], 'lo')


class FakePlatform:
    def __init__(self, port=0):
        self.bind_port = port
        self.port = None
        self.starts = []

    def listen(self, access):
        assert access.address == '127.0.0.1'
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((access.address, self.bind_port))
        listener.listen(8)
        self.port = listener.getsockname()[1]
        return listener

    def launch(self, driver, device, channel):
        assert driver == 'indi_asi_ccd' and device == DEVICE
        with socket.socket() as ephemeral:
            ephemeral.bind(('127.0.0.1', 0))
            port = ephemeral.getsockname()[1]
        child = subprocess.Popen([sys.executable, __file__, 'worker', str(port)],
                                 stdin=channel, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.starts.append(child)
        return child

    def wait_ready(self, service):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            service.sync_indi((PATH,))
            if service.indi_status()['state'] == 'ready':
                return
            time.sleep(0.03)
        raise AssertionError('fake indiserver did not become healthy')


def fake_server(port):
    with socket.socket() as server:
        server.bind(('127.0.0.1', port))
        server.listen(8)
        while True:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(1)
                if conn.recv(128).strip() == b'<getProperties version="1.7"/>':
                    conn.sendall(b'<defTextVector device="Test camera" name="DRIVER_INFO"/>\n')


if __name__ == '__main__':
    if sys.argv[1] == 'worker':
        port = int(sys.argv[2])
        with socket.socket(fileno=0) as channel:
            read, write = os.pipe()
            try:
                serve(channel, [sys.executable, __file__, 'indiserver', '-p', str(port), 'indi_asi_ccd'],
                      port, read, lambda: True)
            finally:
                os.close(read)
                os.close(write)
    elif sys.argv[1:3] == ['indiserver', '-p'] and sys.argv[4:] == ['indi_asi_ccd']:
        fake_server(int(sys.argv[3]))
