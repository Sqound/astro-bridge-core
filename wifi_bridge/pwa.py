"""Fixed public shell bytes and deterministic revision; no private state input."""
import hashlib
from pathlib import Path

ASSETS = {
    '/': ('index.html', 'text/html; charset=utf-8'),
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
STATIC = Path(__file__).parent / 'static'


def load_shell(folder):
    payload = {url: (folder / name).read_bytes() for url, (name, _) in ASSETS.items()}
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
        prefix = 'const REVISION=' + repr(REVISION) + ';\nconst ASSETS=' + repr(sorted(ASSETS)) + ';\n'
        return prefix.encode() + WORKER, 'text/javascript; charset=utf-8', REVISION
    _, mime = ASSETS[path]
    data = PUBLIC[path]
    if path == '/':
        data = data.replace(b'__BRIDGE_SHELL_REVISION__', REVISION.encode())
    return data, mime, REVISION
