#!/usr/bin/env python3
"""Reproducible echo-only bench. Run exclusively with a simulator target."""
import argparse
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import socket
import secrets
import socketserver
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wifi_bridge.metrics import percentiles


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class API:
    def __init__(self, origin):
        url = urlsplit(origin)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
                or url.path not in ('', '/') or url.query or url.fragment):
            raise ValueError('Invalid bridge origin')
        self.origin, self.token = origin.rstrip('/'), ''
        self.opener = build_opener(NoRedirect)

    def request(self, path, data=None):
        headers = {'Accept': 'application/json'}
        if self.token: headers['Authorization'] = 'Bearer '+self.token
        if data is not None: headers['Content-Type'] = 'application/json'
        request = Request(self.origin+'/api/v1'+path,
                          json.dumps(data).encode() if data is not None else None, headers)
        with self.opener.open(request, timeout=3) as response:
            raw = response.read(262145)
        if len(raw) > 262144: raise ValueError('Oversized API response')
        return json.loads(raw)


def flow(host, port, transport, duration, rate, size, stop=None):
    samples = deque(maxlen=10000)
    sent = replies = failures = 0
    end = time.monotonic()+duration
    nonce = secrets.token_bytes(8)
    try:
        with socket.socket(type=socket.SOCK_STREAM if transport == 'tcp' else socket.SOCK_DGRAM) as stream:
            stream.settimeout(min(1, duration))
            stream.connect((host, port))
            while time.monotonic() < end and not (stop and stop.is_set()):
                started = time.monotonic(); sent += 1
                payload = (nonce+sent.to_bytes(8, 'big')).ljust(size, b'0')
                try:
                    deadline = min(end, started+1)
                    stream.settimeout(max(.001, deadline-time.monotonic()))
                    stream.sendall(payload)
                    received = b''
                    while len(received) < size:
                        remaining = deadline-time.monotonic()
                        if remaining <= 0: raise TimeoutError
                        stream.settimeout(remaining)
                        part = stream.recv(size-len(received))
                        if not part: raise OSError
                        received += part
                        if transport == 'udp': break
                    if received != payload: raise ValueError
                    samples.append((time.monotonic()-started)*1000); replies += 1
                except (OSError, ValueError):
                    failures += 1
                    # A TCP timeout leaves framing ambiguous; don't invent RTT samples.
                    if transport == 'tcp': break
                remaining = min(end-time.monotonic(), 1/rate-(time.monotonic()-started))
                if remaining > 0: time.sleep(remaining)
    except OSError:
        failures += 1
    return dict(sent=sent, replies=replies, failures=failures,
                loss_percent=round(100*(sent-replies)/sent, 3) if sent else None,
                rtt=dict(samples=len(samples), **percentiles(samples)))


def bench(api, profile, path, transport, flows, duration, rate, size):
    version = api.request('/info')['version']
    inventory = api.request('/endpoints')
    matches = [p for e in inventory['endpoints'] if e['profile_id'] == profile
               for p in e['paths'] if p['path'] == path and p['transport'] == transport and p['ready']]
    if len(matches) != 1: raise ValueError('Choose one ready endpoint path')
    endpoint = matches[0]
    metrics = [api.request('/system/metrics')]
    results = [None]*flows
    stop = threading.Event()
    def worker(index):
        results[index] = flow(endpoint['host'], endpoint['port'], transport, duration, rate, size, stop)
    workers = [threading.Thread(target=worker, args=(i,)) for i in range(flows)]
    started = time.monotonic()
    for worker in workers: worker.start()
    next_sample = started+5
    try:
        while any(worker.is_alive() for worker in workers):
            for worker in workers: worker.join(timeout=.01)
            if time.monotonic() >= next_sample:
                metrics.append(api.request('/system/metrics')); next_sample = time.monotonic()+5
    finally:
        stop.set()
        for worker in workers: worker.join(timeout=2)
    # The cached final sample may predate the final packet; preserve age and sequence.
    metrics.append(api.request('/system/metrics'))
    return dict(schema_version=1, bridge_version=version, simulator_only=True, transport=transport, path=path,
                flows=flows, duration_seconds=duration, rate_per_flow=rate, payload_bytes=size,
                elapsed_seconds=round(time.monotonic()-started, 3), results=results, metrics=metrics)


def metrics_window(api, duration):
    """Sample the existing process cache without generating echo traffic."""
    status = api.request('/system/updates')
    samples = []
    started = time.monotonic()
    try:
        while True:
            reading = api.request('/system/metrics')
            elapsed = time.monotonic() - started
            samples.append(dict(seconds=round(elapsed, 3), sequence=reading['sample_sequence'],
                                age=reading['sample_age_seconds'], rss=reading['process']['rss_bytes'],
                                cpu=reading['process']['cpu_percent']['5s']))
            if elapsed >= duration: break
            # Poll slightly faster than the 5s cache so HTTP latency does not
            # drift past a sample during a long session. The reader deduplicates.
            time.sleep(min(4, duration - elapsed))
    except KeyboardInterrupt:
        pass  # The owner ends a whole-session run after the observed session closes.
    return dict(kind='process', demo=status['demo'], build_id=status['current_build_id'],
                bridge_version=status['current_version'], samples=samples)


def echo_server(transport, host='127.0.0.1', port=0):
    class TCP(socketserver.BaseRequestHandler):
        def handle(self):
            while True:
                data = self.request.recv(16384)
                if not data: break
                self.request.sendall(data)
    class UDP(socketserver.BaseRequestHandler):
        def handle(self):
            data, stream = self.request
            stream.sendto(data, self.client_address)
    class Echo(socketserver.ThreadingTCPServer):
        daemon_threads = True
    return Echo((host, port), TCP) if transport == 'tcp' else socketserver.ThreadingUDPServer((host, port), UDP)


@contextmanager
def demo_bridge(transport):
    """Disposable DemoNetwork HTTP server, real loopback relay and echo target."""
    from wifi_bridge.config import Store
    from wifi_bridge.controller import Controller
    from wifi_bridge.instrument_relay import InstrumentRelay
    from wifi_bridge.network import DemoNetwork
    from wifi_bridge.web import WebServer
    target = echo_server(transport)
    threading.Thread(target=target.serve_forever, daemon=True).start()
    relay = InstrumentRelay('127.0.0.1', 0, transport, '127.0.0.1', target.server_address[1],
                            lambda kind: socket.socket(type=kind)).start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            net = DemoNetwork()
            store = Store(directory, net.devices()[0]['mac'])
            controller = Controller(store, net, {'password': secrets.token_urlsafe(32)}, demo=True)
            controller.update(devices=net.devices())
            net.isolated_endpoints([], [], [])
            net.isolated.relays[('simulator', '127.0.0.1', relay.port, transport)] = relay
            server = WebServer(('127.0.0.1', 0), controller)
            # Use only a throwaway in-memory session in this disposable fixture.
            api = API('http://127.0.0.1:'+str(server.server_port))
            api.token = secrets.token_urlsafe(32)
            server.sessions[api.token] = dict(kind='api', scopes=['admin'], expires=time.monotonic()+3660,
                                            transport='http', tls_identity=None, csrf='')
            original = server.api.read
            def read(resource, *args, **kwargs):
                if resource == 'endpoints':
                    return {'endpoints': [{'profile_id': 'simulator', 'paths': [dict(path='home',
                        transport=transport, host='127.0.0.1', port=relay.port, ready=True)]}]}
                return original(resource, *args, **kwargs)
            server.api.read = read
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try: yield api
            finally: server.shutdown(); server.server_close()
    finally:
        relay.close(); target.shutdown(); target.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bridge')
    parser.add_argument('--metrics-only', action='store_true', help='Sample process resources without an endpoint or echo traffic')
    parser.add_argument('--serve-simulator', action='store_true', help='Run a standalone echo target on dedicated simulator equipment')
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--listen-port', type=int, default=4030)
    parser.add_argument('--profile', default='simulator')
    parser.add_argument('--path', choices=('home', 'hotspot'), default='home')
    parser.add_argument('--transport', choices=('tcp', 'udp'), default='tcp')
    parser.add_argument('--flows', type=int, default=1)
    parser.add_argument('--duration', type=float, default=65)
    parser.add_argument('--rate', type=int, default=10)
    parser.add_argument('--size', type=int, default=256)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--simulator-target', action='store_true', help='Confirm the selected profile connects only to an echo simulator')
    parser.add_argument('--output-dir', type=Path, default=Path('release-artifacts'))
    args = parser.parse_args(argv)
    if (not 1 <= args.flows <= 32 or not .1 <= args.duration <= (42000 if args.metrics_only else 3600) or not 1 <= args.rate <= 1000
            or not 16 <= args.size <= 16384): parser.error('Bench input outside bounds')
    if args.metrics_only and (args.simulator_target or args.dry_run or args.serve_simulator):
        parser.error('Metrics-only requires a management origin and no simulator')
    if not args.serve_simulator and not args.dry_run and (not args.bridge or not (args.metrics_only or args.simulator_target)):
        parser.error('--bridge and either --metrics-only or --simulator-target are required')
    def run(api):
        if args.metrics_only: return metrics_window(api, args.duration)
        return bench(api, 'simulator' if args.dry_run else args.profile, args.path, args.transport,
                     args.flows, args.duration, args.rate, args.size)
    try:
        if args.serve_simulator:
            import ipaddress
            address = ipaddress.IPv4Address(args.listen)
            if address.is_multicast or not 1 <= args.listen_port <= 65535:
                parser.error('Invalid simulator listener')
            target = echo_server(args.transport, str(address), args.listen_port)
            try:
                print('Echo simulator running; press Ctrl-C to stop.', flush=True)
                target.serve_forever()
            except KeyboardInterrupt: pass
            finally: target.server_close()
            return 0
        if args.dry_run:
            if args.path != 'home': parser.error('Dry-run provides the synthetic home path only')
            with demo_bridge(args.transport) as api: result = run(api)
        else:
            api = API(args.bridge)
            api.token = api.request('/auth/token', {'password': getpass.getpass('Administrator password: ')})['access_token']
            try:
                result = run(api)
                if not args.metrics_only:
                    status = api.request('/system/updates')
                    result.update(build_id=status['current_build_id'], demo=status['demo'])
            finally:
                # Revoke the temporary token without persisting authentication material.
                request = Request(api.origin+'/api/v1/auth/token', method='DELETE', headers={'Authorization': 'Bearer '+api.token})
                try:
                    with api.opener.open(request, timeout=3): pass
                finally: api.token = ''
        if not args.metrics_only:
            result['measurement_source'] = 'loopback-demo' if args.dry_run else 'owner-simulator'
        args.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        output = args.output_dir/('load-bench-'+stamp+'.json')
        with open(output, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump(result, stream, indent=2, allow_nan=False); stream.write('\n')
        print(output)
        return 0
    except (OSError, ValueError, KeyError):
        print('Bench failed; no request payload or authentication data recorded.', file=sys.stderr)
        return 1


if __name__ == '__main__': raise SystemExit(main())
