"""Explicit loopback-only camera-service demo for external conformance tools.

This uses the production Alpaca listener and worker with one synthetic camera.
It neither discovers hardware nor starts the bridge's network controller. The
single stdout JSON object is machine-readable endpoint metadata, not a payload
log. Stop with SIGINT or SIGTERM; all camera state is disposable and in memory.
"""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wifi_bridge.cameras.listener import AlpacaServer, ManagementAccess
from wifi_bridge.cameras.service import CameraService
from wifi_bridge.cameras.simulator import SimulatedCamera


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true', required=True)
    parser.add_argument('--port', type=int, default=11112, metavar='PORT')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('invalid_port')
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    service = CameraService()
    server = worker = None
    try:
        service.add('simulation', 'simulator', SimulatedCamera(width=320, height=240))
        access = ManagementAccess('127.0.0.1', '127.0.0.0/8', ['localhost'])
        server = AlpacaServer(access, service, port=args.port)
        worker = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': 0.05}, daemon=True)
        worker.start()
        service.set_listening(server.server_port, ['127.0.0.1'])
        print(json.dumps({'demo': True, 'camera':
              f'http://127.0.0.1:{server.server_port}/api/v1/camera/0'}), flush=True)
        stop.wait()
    finally:
        service.set_listening(None)
        if server:
            if worker:
                server.shutdown()
                worker.join(timeout=1)
            server.server_close()
        service.close()


if __name__ == '__main__':
    main()
