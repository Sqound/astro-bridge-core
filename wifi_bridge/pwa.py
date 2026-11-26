"""Fixed public shell bytes and deterministic revision; no private state input."""
import gzip
import hashlib
from pathlib import Path
import threading

ASSETS = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/cameras.js': ('cameras.js', 'text/javascript; charset=utf-8'),
    '/session-page.js': ('session-page.js', 'text/javascript; charset=utf-8'),
    '/session-cards.js': ('session-cards.js', 'text/javascript; charset=utf-8'),
    '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
    '/events.js': ('events.js', 'text/javascript; charset=utf-8'),
    '/i18n.js': ('i18n.js', 'text/javascript; charset=utf-8'),
    '/theme.js': ('theme.js', 'text/javascript; charset=utf-8'),
    '/secure.js': ('secure.js', 'text/javascript; charset=utf-8'),
    '/push.js': ('push.js', 'text/javascript; charset=utf-8'),
    '/pwa.js': ('pwa.js', 'text/javascript; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/manifest.webmanifest': ('manifest.webmanifest', 'application/manifest+json'),
    '/icon-192.png': ('icon-192.png', 'image/png'),
    '/icon-512.png': ('icon-512.png', 'image/png'),
    '/apple-touch-icon.png': ('apple-touch-icon.png', 'image/png'),
}
# One language catalog is loaded on demand; the worker caches it when used.
CATALOGS = {f'/i18n-{tag}.js': (f'i18n-{tag}.js', 'text/javascript; charset=utf-8')
            for tag in ('fr', 'ja', 'de', 'es', 'it', 'pt-BR', 'zh-Hans', 'zh-Hant', 'ru')}
SERVED = {**ASSETS, **CATALOGS}
STATIC = Path(__file__).parent / 'static'
COMPRESSIBLE = ('text/', 'application/manifest+json', 'application/json')


def load_shell(folder):
    payload = {url: (folder / name).read_bytes() for url, (name, _) in SERVED.items()}
    worker = (folder / 'sw.js').read_bytes()
    digest = hashlib.sha256()
    for url, data in sorted(payload.items()):
        digest.update(url.encode() + b'\0' + data + b'\0')
    digest.update(worker)
    return payload, worker, digest.hexdigest()


# The running app serves one immutable, coherent snapshot. OTA/rollback restarts
# the process, loading the corresponding bytes once rather than hashing on polls.
PUBLIC, WORKER, REVISION = load_shell(STATIC)


def revision():
    return REVISION


def public_asset(path):
    if path == '/sw.js':
        prefix = ('const REVISION=' + repr(REVISION) + ';\nconst ASSETS=' + repr(sorted(ASSETS))
                  + ';\nconst LAZY=' + repr(sorted(CATALOGS)) + ';\n')
        return prefix.encode() + WORKER, 'text/javascript; charset=utf-8', REVISION
    _, mime = SERVED[path]
    data = PUBLIC[path]
    if path == '/':
        data = data.replace(b'__BRIDGE_SHELL_REVISION__', REVISION.encode())
    return data, mime, REVISION


_compressed = {}
_compress_lock = threading.Lock()


def compressed(path, data, mime):
    """Gzip each fixed shell file once per process, never once per request.

    Prepared files cannot ship as .gz: installed updaters accept only reviewed
    static suffixes. Compressing the immutable bytes once is the equivalent.
    """
    if len(data) < 1024 or not mime.startswith(COMPRESSIBLE):
        return None
    key = (path, hashlib.sha256(data).digest())
    with _compress_lock:
        if key not in _compressed:
            _compressed[key] = gzip.compress(data, compresslevel=9, mtime=0)
        return _compressed[key]
