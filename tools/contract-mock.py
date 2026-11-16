#!/usr/bin/env python3
"""Strict, loopback-only native contract replay. No appliance or network backend."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import threading

ROOT = Path(__file__).resolve().parents[1] / 'contracts/native-client/v1'
MAX_BODY = 32768


def scenarios():
    return [json.loads(path.read_text()) for path in sorted(ROOT.glob('*.json')) if path.name not in ('STATUS.json', 'openapi-snapshot.json')]


class ReplayServer(HTTPServer):
    def __init__(self, address, flows):
        self.steps = [step for flow in flows for step in flow['steps']]
        self.index = 0
        self.lock = threading.Lock()
        super().__init__(address, ReplayHandler)


class ReplayHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log request paths, credentials or bodies.

    def reply(self, status, body, headers=None):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        for name, value in (headers or {}).items():
            if name.lower() not in ('content-length', 'content-type', 'cache-control'):
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def replay(self):
        self.connection.settimeout(5)
        with self.server.lock:
            if self.server.index >= len(self.server.steps):
                self.reply(599, {'error': {'code': 'SCENARIO_COMPLETE', 'message': 'No steps remain.'}, 'diff': ['unexpected request']})
                return
            expected = self.server.steps[self.server.index]
            request = expected['request']
            diff = []
            if self.command != request['method']:
                diff.append('method differs')
            if self.path != request['path']:
                diff.append('path or query differs')
            for key, value in request['headers'].items():
                if self.headers.get(key) != value:
                    diff.append('header ' + key + ' differs')
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 <= size <= MAX_BODY or self.headers.get('Transfer-Encoding'):
                    raise ValueError()
                raw = self.rfile.read(size)
                body = json.loads(raw) if raw else None
                if body != request['body']:
                    diff.append('JSON body differs')
            except (ValueError, UnicodeDecodeError, TimeoutError):
                diff.append('invalid or oversized JSON body')
            if diff:
                # Report locations, never actual or expected credential material.
                self.reply(599, {'error': {'code': 'SCENARIO_MISMATCH', 'message': 'Request does not match the next step.'}, 'step': self.server.index, 'diff': diff})
                return
            self.server.index += 1
            response = expected['response']
            self.reply(response['status'], response['body'], response['headers'])

    do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choices = parser.add_mutually_exclusive_group(required=True)
    choices.add_argument('--scenario')
    choices.add_argument('--all', action='store_true')
    choices.add_argument('--list', action='store_true')
    parser.add_argument('--port', type=int, default=8798)
    args = parser.parse_args()
    flows = scenarios()
    if args.list:
        for flow in flows:
            print(flow['name'])
        return
    if not 0 <= args.port <= 65535:
        parser.error('port must be between 0 and 65535')
    if args.scenario:
        flows = [flow for flow in flows if flow['name'] == args.scenario]
        if not flows:
            parser.error('unknown scenario; use --list')
    with ReplayServer(('127.0.0.1', args.port), flows) as server:
        print(json.dumps({'port': server.server_port, 'steps': len(server.steps)}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
