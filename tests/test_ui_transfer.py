"""First page over a slow hotspot: compressed, one language at a time, cached when versioned."""
import gzip
import http.client
import re
import threading

from wifi_bridge.pwa import REVISION
from wifi_bridge.web import WebServer
from test_bridge import Fixture

# The 1.1.0 first page was 2.04 MB in 25 files, ten languages at once and uncompressed.
FIRST_PAGE_BUDGET = 400 * 1024


class FirstPageTransferTests(Fixture):
    def setUp(self):
        super().setUp()
        self.server = WebServer(("127.0.0.1", 0), self.controller)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path, headers=None):
        port = self.server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self.addCleanup(conn.close)
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
        response = conn.getresponse()
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), response.read()

    def first_page(self, locale=None):
        status, headers, body = self.get("/", {"Accept-Encoding": "gzip, deflate, br"})
        self.assertEqual((status, headers["content-encoding"]), (200, "gzip"))
        page = gzip.decompress(body).decode()
        paths = re.findall(r'(?:src|href)="(/[a-z0-9.-]+\.(?:js|css|png|webmanifest)(?:\?v=[0-9a-f]{64})?)"', page.split("</head>")[0])
        if locale:
            paths.append(f"/i18n-{locale}.js?v={REVISION}")
        total, results = len(body), {}
        for path in paths:
            status, headers, body = self.get(path, {"Accept-Encoding": "gzip"})
            self.assertEqual(status, 200, path)
            total += len(body)
            results[path] = headers
        return total, results

    def test_first_page_fits_the_budget_in_english_and_with_one_other_language(self):
        for locale in (None, "ru"):
            with self.subTest(locale=locale):
                total, results = self.first_page(locale)
                self.assertLess(total, FIRST_PAGE_BUDGET)
                for path, headers in results.items():
                    if path.endswith(".js") or path.endswith(".css"):
                        self.assertEqual(headers.get("content-encoding"), "gzip", path)
                        self.assertIn("?v=" + REVISION, path)
                        self.assertEqual(headers["cache-control"], "public, max-age=31536000, immutable", path)

    def test_unversioned_shell_revalidates_and_other_languages_are_not_loaded(self):
        status, headers, _ = self.get("/app.js")
        self.assertEqual((status, headers["cache-control"]), (200, "no-cache"))
        self.assertNotIn("content-encoding", headers)
        status, again, body = self.get("/app.js", {"If-None-Match": headers["etag"]})
        self.assertEqual((status, body), (304, b""))
        self.assertEqual(again["x-bridge-shell"], REVISION)
        # A stale or forged version never earns a long cache lifetime.
        self.assertEqual(self.get("/app.js?v=" + "0" * 64)[1]["cache-control"], "no-cache")
        self.assertEqual(self.get("/sw.js?v=" + REVISION)[1]["cache-control"], "no-cache")
        _, _, english = self.get("/i18n.js")
        self.assertNotIn("Пароль".encode(), english)
        self.assertNotEqual(self.get("/i18n-xx.js")[0], 200)
