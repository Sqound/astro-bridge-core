"""Local verified HTTPS chain and synthetic ZIP packages; no maker access."""
import http.client
import contextlib
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import shutil
import ssl
import struct
import subprocess
import tempfile
import threading
import unittest
import zipfile

from wifi_bridge.maker_keys.fetch import Fetcher, FetchError, MEMBER, CAP
from wifi_bridge.maker_keys.rsa import parse
from helpers.maker_keys import pem


def package(payload=None,method=zipfile.ZIP_DEFLATED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream,'w',compression=method) as archive:
        archive.writestr('assets/unrelated',b'Nothing private here')
        archive.writestr(MEMBER.decode(),bytes(pem() if payload is None else payload))
    return bytearray(stream.getvalue())


@unittest.skipUnless(shutil.which('openssl'),'OpenSSL required for disposable test CA')
class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory();root=Path(cls.temporary.name)
        result = subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(root/'tls.key'),'-out',str(root/'tls.crt'),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],capture_output=True)
        if result.returncode: raise unittest.SkipTest('OpenSSL cannot generate test CA')
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(root/'tls.crt',root/'tls.key')
        cls.context=ssl.create_default_context(cafile=str(root/'tls.crt'))
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                owner=self.server.owner
                owner.requests.append((self.path,self.headers.get('Range'),self.headers.get('Accept-Encoding')))
                if self.path.startswith('/software'):
                    target='https://api.zwoastro.com/download?ephemeral=fake-signed-query'
                    if owner.failure=='redirectRefused': target='https://unapproved.invalid/package'
                    self.send_response(302);self.send_header('Location',target);self.end_headers();return
                if self.path.startswith('/download'):
                    target='https://storage.example.invalid/package?ephemeral=fake-signed-query'
                    if owner.failure=='downgrade':target='http://storage.example.invalid/package'
                    if owner.failure=='secondStorage' and sum(r[0].startswith('/download') for r in owner.requests)>1:target='https://second-storage.example.invalid/package'
                    self.send_response(302);self.send_header('Location',target);self.end_headers();return
                if owner.failure in ('tooManyRedirects','secondStorage'):
                    self.send_response(302);self.send_header('Location','https://api.zwoastro.com/download');self.end_headers();return
                if owner.failure in ('notPartial','status'):
                    self.send_response(200 if owner.failure=='notPartial' else 503)
                    self.send_header('Content-Length',str(1300*1024**2));self.end_headers();return
                start,end=map(int,self.headers['Range'][6:].split('-'))
                body=owner.data[start:end+1]
                self.send_response(206)
                self.send_header('Content-Range',f'bytes {start+1 if owner.failure=="range" else start}-{end}/{len(owner.data)}')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                if owner.failure=='blockedBody':
                    owner.headers_ready.set();owner.body_gate.wait(3)
                try: self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError): pass
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.server.socket=context.wrap_socket(cls.server.socket,server_side=True)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temporary.cleanup()

    def setUp(self):
        self.data=package();self.requests=[];self.failure=None;self.server.owner=self
        self.headers_ready=threading.Event();self.body_gate=threading.Event();self.addCleanup(self.body_gate.set)

    def fetcher(self):
        def connection(host,**kwargs): return http.client.HTTPSConnection('localhost',self.server.server_port,**kwargs)
        return Fetcher(connection_factory=connection,context=self.context)

    def fails(self,reason):
        fetcher=self.fetcher()
        with self.assertRaises(FetchError) as caught: fetcher.fetch()
        self.assertEqual(caught.exception.reason,reason)
        self.assertTrue(all(set(row)<={'step','bytes','http_status','crcVerified','candidates','verdict'} for row in fetcher.journal),'Journal allowlist')

    def test_roundtrip_partial_chain_and_redacted_journal(self):
        for method in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED):
            self.data=package(method=method);fetcher=self.fetcher()
            buffers=[];original=fetcher.range
            def tracked(*args,**kwargs):
                result=original(*args,**kwargs);buffers.append(result[0]);return result
            fetcher.range=tracked
            capture=io.StringIO();handler=logging.StreamHandler(capture);logging.getLogger().addHandler(handler)
            try:
                with contextlib.redirect_stdout(capture),contextlib.redirect_stderr(capture):result=fetcher.fetch()
            finally:logging.getLogger().removeHandler(handler)
            self.assertFalse('fake-signed-query' in capture.getvalue(),'No URL query in logs')
            self.assertFalse(any(line.decode() in capture.getvalue() for line in pem().splitlines()),'No PEM line in logs')
            self.assertTrue(buffers and all(not any(buffer) for buffer in buffers),'Range buffers cleared after use')
            self.assertTrue(parse(result).n == parse(pem()).n,'Extracted disposable key')
            self.assertTrue(all(row[2]=='identity' for row in self.requests))
            journal=str(fetcher.journal)
            self.assertFalse('ephemeral' in journal or 'fake-signed-query' in journal,'Queries absent from journal')
            self.assertTrue(any(row.get('crcVerified') for row in fetcher.journal))
            self.assertEqual(sum(row[0].startswith('/software') for row in self.requests)%5,0)

    def test_http_failures(self):
        for failure in ('redirectRefused','tooManyRedirects','notPartial','status'):
            self.failure=failure
            with self.subTest(reason=failure): self.fails(failure)

    def test_redirect_chain_and_range_restrictions(self):
        for failure,reason in (('downgrade','redirectRefused'),('secondStorage','redirectRefused'),('range','range')):
            self.failure=failure;self.requests=[]
            with self.subTest(reason=failure): self.fails(reason)

    def test_zip64_method_cap_crc(self):
        for reason in ('ZIP64','method','cap','CRC'):
            self.data=package();at=self.data.rfind(b'PK\1\2')
            if reason=='ZIP64': struct.pack_into('<L',self.data,at+24,0xffffffff)
            if reason=='method': struct.pack_into('<H',self.data,at+10,99)
            if reason=='cap': struct.pack_into('<L',self.data,at+24,CAP+1)
            if reason=='CRC':
                local=struct.unpack_from('<L',self.data,at+42)[0]
                struct.pack_into('<L',self.data,at+16,123)
                struct.pack_into('<L',self.data,local+14,123)
            with self.subTest(reason=reason): self.fails(reason)

    def test_missing_and_distinct_keys(self):
        for payload,reason in ((b'not a key','noKey'),(pem()+pem(81921),'twoKeys')):
            self.data=package(payload);self.fails(reason)
        self.data=package(pem()+pem(pkcs8=False))
        self.assertTrue(parse(self.fetcher().fetch()).n == parse(pem()).n,'Same RSA key in two formats is one key')

    def test_cancel_interrupts_http10_detached_body_socket(self):
        self.failure='blockedBody';fetcher=self.fetcher();results=[]
        def run():
            try: fetcher.fetch()
            except FetchError as error:results.append(error.reason)
        worker=threading.Thread(target=run,daemon=True);worker.start()
        self.assertTrue(self.headers_ready.wait(2))
        fetcher.close();worker.join(1)
        self.body_gate.set()
        self.assertFalse(worker.is_alive(),'Detached HTTP/1.0 socket is interrupted')
        self.assertEqual(results,['cancelled'])

    def test_tls_verification_and_cancellation(self):
        def mismatch(host,**kwargs):return http.client.HTTPSConnection('127.0.0.1',self.server.server_port,**kwargs)
        with self.assertRaises(FetchError): Fetcher(connection_factory=mismatch,context=self.context).fetch()
        def untrusted(host,**kwargs):return http.client.HTTPSConnection('localhost',self.server.server_port,**kwargs)
        with self.assertRaises(FetchError): Fetcher(connection_factory=untrusted).fetch()
        unsafe=ssl._create_unverified_context()
        with self.assertRaises(ValueError): Fetcher(context=unsafe)
        fetcher=self.fetcher();fetcher.close()
        with self.assertRaises(FetchError) as caught: fetcher.fetch()
        self.assertEqual(caught.exception.reason,'cancelled')


class CancellationRaceTests(unittest.TestCase):
    def test_cancel_during_factory_and_socket_replacement_never_sends_request(self):
        for phase in ('factory','connect'):
            entered=threading.Event();release=threading.Event();outcome=[]
            class Socket:
                def __init__(self):self.closed=False
                def shutdown(self,*args):self.closed=True
            class Connection:
                def __init__(self):self.sock=None;self.sockets=[];self.requests=0;self.closes=0
                def connect(self):
                    self.sock=Socket();self.sockets.append(self.sock)
                    entered.set();release.wait(3)
                    # Model a TLS wrapper or reconnect replacing the cancelled socket.
                    self.sock=Socket();self.sockets.append(self.sock)
                def request(self,*args,**kwargs):self.connect();self.requests+=1
                def getresponse(self):raise AssertionError('No HTTP response after cancellation')
                def close(self):
                    self.closes+=1
                    if self.sock:self.sock.shutdown(2)
                    self.sock=None
            connection=Connection()
            def factory(*args,**kwargs):
                if phase=='factory':entered.set();release.wait(3)
                return connection
            fetcher=Fetcher(connection_factory=factory)
            def run():
                try:fetcher.fetch()
                except FetchError as error:outcome.append(error.reason)
            worker=threading.Thread(target=run,daemon=True);worker.start()
            self.assertTrue(entered.wait(2));fetcher.close();release.set();worker.join(2)
            self.assertFalse(worker.is_alive(),'Cancelled setup terminates')
            self.assertEqual(outcome,['cancelled']);self.assertEqual(connection.requests,0)
            self.assertGreater(connection.closes,0)
            self.assertTrue(all(stream.closed for stream in connection.sockets),'Late replacement sockets closed')
