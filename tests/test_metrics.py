"""Synthetic proc parsing, bounded measurements and disposable echo load."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from wifi_bridge.metrics import ForwardingMetrics, SystemMetrics, proc_snapshot
from helpers.contract_schema import validate


class MetricsTests(unittest.TestCase):
    def fixture(self, root, ticks=300):
        (root/'self').mkdir(exist_ok=True)
        fields = ['0']*22
        fields[0] = 'S'; fields[11] = str(ticks); fields[12] = '100'; fields[17] = '7'; fields[21] = '12'
        (root/'self/stat').write_text('123 (worker (test)) '+ ' '.join(fields))
        (root/'loadavg').write_text('0.25 1.50 2.0 1/20 123')
        (root/'meminfo').write_text('MemFree: 123 kB\nMemAvailable: 456 kB\n')

    def test_fixture_proc_numeric_fields_and_malformed_or_absent_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.fixture(root)
            data = proc_snapshot(root, ticks=100, page_size=4096)
            self.assertEqual(data, dict(rss_bytes=49152, threads=7, cpu_seconds=4,
                load_average=[.25, 1.5, 2], memory_free_bytes=123*1024, memory_available_bytes=456*1024))
            (root/'self/stat').write_text('malformed')
            (root/'loadavg').write_text('nan inf -1')
            (root/'meminfo').write_text('MemFree: -3 kB\nMemAvailable: wrong kB')
            self.assertTrue(all(value is None for value in proc_snapshot(root).values()))
            self.fixture(root, ticks=10**200)
            self.assertIsNone(proc_snapshot(root)['cpu_seconds'])
            (root/'loadavg').write_text('0'*65537)
            self.assertIsNone(proc_snapshot(root)['load_average'])
            self.assertTrue(all(value is None for value in proc_snapshot(root/'missing').values()))

    def test_cache_five_and_sixty_second_windows_lag_and_clock_reset(self):
        now = [0]
        calls = []
        def reader():
            calls.append(now[0])
            return dict(rss_bytes=100, threads=3, cpu_seconds=now[0]/2,
                        load_average=[0, 0, 0], memory_free_bytes=1, memory_available_bytes=2)
        collector = SystemMetrics(clock=lambda: now[0], reader=reader)
        first = collector.read()
        self.assertIsNone(first['process']['cpu_percent']['5s'])
        now[0] = 4.9
        self.assertEqual(collector.read()['sample_sequence'], 1)
        self.assertEqual(calls, [0])
        for stamp in range(5, 66, 5):
            now[0] = stamp
            collector.tick(stamp-.125)
        result = collector.read()
        self.assertEqual(result['process']['cpu_percent'], {'5s': 50, '60s': 50})
        self.assertEqual(result['http_loop_lag']['p95_ms'], 125)
        self.assertEqual(len(calls), 14)
        schema = json.loads(Path('wifi_bridge/static/openapi.json').read_text())
        validate(result, {'$ref': '#/components/schemas/SystemMetrics'}, schema)
        result['process']['threads'] = 999
        self.assertEqual(collector.read()['process']['threads'], 3)

    def test_relay_measurements_are_bounded_and_separate_directions(self):
        counters = ForwardingMetrics()
        from unittest.mock import patch
        with patch('wifi_bridge.metrics.time.monotonic', return_value=3):
            for _ in range(300): counters.forward(10, True, 2.999, datagram=True)
            counters.forward(2, False, 2.998, datagram=True)
        counters.drop()
        value = counters.snapshot()
        self.assertEqual(value['bytes_to_instrument'], 3000)
        self.assertEqual(value['bytes_from_instrument'], 2)
        self.assertEqual(value['datagrams'], 301); self.assertEqual(value['drops'], 1)
        self.assertEqual(value['forwarding_latency']['samples'], 256)
        self.assertEqual(value['forwarding_latency']['p50_ms'], 1)

    def test_bench_dry_run_uses_real_loopback_tcp_udp_and_demo_http(self):
        spec = importlib.util.spec_from_file_location('load_bench', Path('tools/load-bench.py'))
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        for transport in ('tcp', 'udp'):
            with self.subTest(transport=transport), module.demo_bridge(transport) as api:
                result = module.bench(api, 'simulator', 'home', transport, 1, .15, 20, 32)
                self.assertGreater(result['results'][0]['replies'], 0)
                self.assertEqual(result['results'][0]['failures'], 0)
                self.assertTrue(result['simulator_only'])
                self.assertEqual(result['metrics'][0]['relays'][0]['transport'], transport)
                wire = json.dumps(result)
                for field in ('Authorization', 'access_token', 'password', 'csrf', 'payload"'):
                    self.assertNotIn(field, wire)
        for transport in ('tcp', 'udp'):
            target = module.echo_server(transport)
            import threading
            threading.Thread(target=target.serve_forever, daemon=True).start()
            try:
                direct = module.flow('127.0.0.1', target.server_address[1], transport, .1, 20, 32)
                self.assertGreater(direct['replies'], 0)
                self.assertEqual(direct['failures'], 0)
            finally: target.shutdown(); target.server_close()
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(module.main(['--dry-run', '--duration', '.1', '--output-dir', directory]), 0)
            files = list(Path(directory).glob('load-bench-*.json'))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    def test_dry_run_token_covers_the_maximum_bench_duration(self):
        import time
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('load_bench', Path('tools/load-bench.py'))
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with module.demo_bridge('tcp') as api:
            later = time.monotonic()+3601
            with patch('wifi_bridge.web.time.monotonic', return_value=later):
                self.assertEqual(api.request('/system/metrics')['api_version'], '1')

    def test_single_byte_tcp_fragments_keep_timestamp_metadata_bounded(self):
        from wifi_bridge.metrics import IngressTimings
        from unittest.mock import patch
        timings = IngressTimings(); counters = ForwardingMetrics()
        for index in range(65536): timings.receive(1, index/100000)
        self.assertEqual(len(timings.pending), 64)
        self.assertEqual(sum(part[0] for part in timings.pending), 65536)
        with patch('wifi_bridge.metrics.time.monotonic', return_value=1):
            timings.forward(32000, counters, True)
            timings.forward(33536, counters, True)
        self.assertFalse(timings.pending)
        self.assertEqual(counters.snapshot()['bytes_to_instrument'], 65536)
        self.assertLessEqual(counters.snapshot()['forwarding_latency']['samples'], 256)

    def test_seestar_discovery_counts_only_numbers_without_storing_reply(self):
        import threading
        from unittest.mock import MagicMock, patch
        from wifi_bridge.discovery import SeestarDiscovery
        plan = ('home0', '192.168.1.2/24')
        controller = MagicMock()
        controller.lock = threading.RLock(); controller.stop = threading.Event()
        controller.management_plans = (plan,)
        controller.discovery_plans = (plan+('scope0', '10.0.0.1'),)
        request = b'{"method":"scan_iscope","id":7}'
        reply = b'{"method":"scan_iscope","id":7,"private":"discard"}'
        listener = MagicMock(); upstream = MagicMock()
        upstream.__enter__.return_value = upstream
        upstream.sendto.return_value = len(request)
        upstream.recvfrom.return_value = (reply, ('10.0.0.1', 4720))
        listener.sendto.return_value = len(reply)
        calls = [0]
        def receive(*args):
            calls[0] += 1
            if calls[0] > 1:
                controller.stop.set(); raise OSError
            return request, ('192.168.1.9', 12000)
        listener.recvfrom.side_effect = receive
        counters = ForwardingMetrics()
        with patch('wifi_bridge.discovery.socket.socket', side_effect=[listener, upstream]), patch(
                'wifi_bridge.discovery.ForwardingMetrics', return_value=counters):
            SeestarDiscovery(controller).loop()
        result = counters.snapshot()
        self.assertEqual(result['bytes_to_instrument'], len(request))
        self.assertEqual(result['bytes_from_instrument'], len(reply))
        self.assertEqual(result['datagrams'], 2)
        self.assertEqual(result['drops'], 0)
        self.assertNotIn('private', json.dumps(result))

    def test_http_loop_timer_records_delayed_service_callback(self):
        from wifi_bridge.web import WebServer
        from unittest.mock import Mock, patch
        server = WebServer.__new__(WebServer)
        server.owns_secure_access = True
        server.api = Mock()
        server.controller = Mock()
        server.metrics_deadline = 10
        server.metrics_poll_interval = .5
        with patch('wifi_bridge.web.time.monotonic', return_value=10.2): server.service_actions()
        server.api.metrics.tick.assert_called_once_with(10, 10.2)
        server.controller.telemetry_sessions.tick.assert_called_once_with()
        self.assertEqual(server.metrics_deadline, 10.7)
