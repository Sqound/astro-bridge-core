"""Real loopback native peer accepts only the three authentication methods."""
import base64
import contextlib
import hashlib
import io
import json
import logging
import socket
import threading
import unittest
import uuid

from helpers.maker_keys import pem
from wifi_bridge.maker_keys.rsa import parse, SHA1_INFO
from wifi_bridge.maker_keys.verify import Handshake


class NativePeerTests(unittest.TestCase):
    def test_exact_handshake_signature_and_redaction(self):
        for verdict in ('verified','refused','unverified'):
            listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(3)
            self.addCleanup(listener.close)
            port=listener.getsockname()[1];methods=[];checks=[]
            challenge=' '+uuid.uuid4().hex+' \u65e5\u672c\n'
            key=pem();rsa=parse(key)
            def peer():
                connection,_=listener.accept();connection.settimeout(3)
                with connection,connection.makefile('rb') as stream:
                    for _ in range(3):
                        line=stream.readline(16385)
                        if not line:break
                        value=json.loads(line);methods.append(value['method'])
                        if value['method']=='get_verify_str':
                            response={'id':value['id'],'result':{'str':challenge}}
                        elif value['method']=='verify_client':
                            checks.append(value['params']['data']==challenge)
                            signature=base64.b64decode(value['params']['sign'],validate=True)
                            encoded=pow(int.from_bytes(signature),rsa.e,rsa.n).to_bytes(128)
                            digest=SHA1_INFO+hashlib.sha1(challenge.encode()).digest()
                            checks.append(encoded==b'\0\1'+b'\xff'*(125-len(digest))+b'\0'+digest)
                            if verdict=='unverified':return
                            response={'id':value['id'],'code':7 if verdict=='refused' else 0}
                        else:response={'id':value['id'],'code':0}
                        connection.sendall(json.dumps(response).encode()+b'\r\n')
            thread=threading.Thread(target=peer,daemon=True);thread.start()
            class MappedSocket:
                def __init__(self,kind):self.stream=socket.socket(socket.AF_INET,kind)
                def __getattr__(self,name):return getattr(self.stream,name)
                def connect(self,address):self.stream.connect(('127.0.0.1',port))
            capture=io.StringIO();handler=logging.StreamHandler(capture);logging.getLogger().addHandler(handler)
            try:
                with contextlib.redirect_stdout(capture),contextlib.redirect_stderr(capture):
                    handshake=Handshake('127.0.0.1/8','test0','127.0.0.2',factory=MappedSocket)
                    result=handshake.run(key)
                    thread.join(3)
            finally:logging.getLogger().removeHandler(handler)
            self.assertEqual(result,verdict);self.assertTrue(all(checks),'Exact signature and unmodified challenge')
            self.assertEqual(methods,['get_verify_str','verify_client']+(['pi_is_verified'] if verdict=='verified' else []))
            self.assertFalse(challenge in capture.getvalue(),'No challenge in logs')
            self.assertFalse(any(line.decode() in capture.getvalue() for line in key.splitlines()),'No PEM line in logs')
