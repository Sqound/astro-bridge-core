"""Download one catalog build from its manufacturer, on the bridge; owner-triggered only.

The same rules as Astro Bridge Setup (tools/astro-bridge-setup/catalog/src/drivers.rs):
HTTPS on port 443 with a verified certificate, no credentials in the address,
the catalog's host first and then only its listed redirect hosts, a size bounded
by the catalog, the SHA-256 checked while the bytes arrive. The file is written
to a private path and never extracted, opened as an archive or executed here:
drivers.install_archive does that, against the same catalog.

Failures carry one plain code the web interface explains:
offline (no route, no name resolution, connection refused or timed out before
any byte), certificate (the manufacturer's site could not be verified, often a
wrong clock), redirect_refused, download_failed (the server answered with an
error), download_interrupted (the transfer stopped), file_changed (size or
fingerprint differ from the catalog), cancelled.
"""
import hashlib
import http.client
import os
import socket
import ssl
from urllib.parse import urljoin, urlsplit

MAX_REDIRECTS = 5
CHUNK = 256 * 1024
USER_AGENT = 'Astro-Bridge'
CODES = ('offline', 'certificate', 'redirect_refused', 'download_failed', 'download_interrupted',
         'file_changed', 'cancelled')


class DownloadError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def allowed_hosts(build):
    return frozenset({urlsplit(build['url']).hostname, *build['redirect_hosts']})


def check_url(url, hosts):
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.username or parts.password or parts.fragment
            or parts.port not in (None, 443) or parts.hostname not in hosts):
        raise DownloadError('redirect_refused')
    return parts


def fetch(build, target, *, factory=None, context=None, progress=None, cancelled=None, timeout=30):
    """Write the archive of a download build to target (created, never replaced) and return its size.

    progress(received, total) is called as bytes arrive. cancelled() returning
    True stops the transfer. target is removed on any failure.
    """
    if build.get('source') != 'download' or not build.get('url') or build.get('size') is None or not build.get('sha256'):
        raise DownloadError('download_failed')
    context = context or ssl.create_default_context()
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise ValueError('Verified TLS is required')
    factory = factory or http.client.HTTPSConnection
    hosts, url, total = allowed_hosts(build), build['url'], build['size']
    try:
        for _ in range(MAX_REDIRECTS + 1):
            parts = check_url(url, hosts)
            path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
            connection = factory(parts.hostname, timeout=timeout, context=context)
            try:
                try:
                    connection.request('GET', path, headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'})
                    response = connection.getresponse()
                except ssl.SSLCertVerificationError:
                    raise DownloadError('certificate') from None
                except (socket.gaierror, ConnectionError, TimeoutError, socket.timeout, OSError):
                    raise DownloadError('offline') from None
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location:
                        raise DownloadError('download_failed')
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise DownloadError('download_failed')
                announced = response.getheader('Content-Length')
                if announced is not None and announced.strip().isdigit() and int(announced) != total:
                    raise DownloadError('file_changed')
                return _receive(response, target, build, progress, cancelled)
            finally:
                connection.close()
        raise DownloadError('redirect_refused')
    except BaseException:
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
        raise


def _receive(response, target, build, progress, cancelled):
    digest, received, total = hashlib.sha256(), 0, build['size']
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'wb') as out:
        while True:
            if cancelled and cancelled():
                raise DownloadError('cancelled')
            try:
                chunk = response.read(min(CHUNK, total + 1 - received))
            except (OSError, http.client.HTTPException):
                raise DownloadError('download_interrupted') from None
            if not chunk:
                break
            received += len(chunk)
            if received > total:
                raise DownloadError('file_changed')
            digest.update(chunk)
            out.write(chunk)
            if progress:
                progress(received, total)
        out.flush()
        os.fsync(out.fileno())
    if received != total:
        raise DownloadError('download_interrupted')
    if digest.hexdigest() != build['sha256']:
        raise DownloadError('file_changed')
    return received
