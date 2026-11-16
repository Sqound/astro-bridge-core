"""Runtime-generated secrets only; assertions never render secret operands."""
import base64
import ctypes.util
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from wifi_bridge.maker_keys.rsa import parse, parse_der, Unusable, SHA1_INFO, armour
from wifi_bridge.maker_keys.store import KeyStore, StorageUnavailable, GCM
from helpers.maker_keys import pem, integer, tlv, numbers


class RSATests(unittest.TestCase):
    def test_pkcs1_pkcs8_blinding_and_exact_utf8(self):
        for pkcs8 in (False,True):
            key = parse(pem(pkcs8=pkcs8))
            challenge = ' runtime challenge \u65e5\u672c\n'
            first, second = key.sign(challenge), key.sign(challenge)
            self.assertTrue(first == second,'Deterministic RSA result')
            encoded = pow(int.from_bytes(base64.b64decode(first)),key.e,key.n).to_bytes(128)
            digest = SHA1_INFO+hashlib.sha1(challenge.encode()).digest()
            self.assertTrue(encoded == b'\0\1'+b'\xff'*(125-len(digest))+b'\0'+digest,'Exact challenge encoding')
            self.assertFalse(first == key.sign(challenge.strip()),'Whitespace preserved')

    def test_invalid_keys_and_der_bounds(self):
        values = [b'',b'invalid',b'x'*8193,pem()+b'junk',pem().replace(b'\n',b'\x00',1)]
        for value in values:
            with self.assertRaises(Unusable): parse(value)
        wrong=pem();wrong_der=base64.b64decode(b''.join(wrong.splitlines()[1:-1]))
        from wifi_bridge.maker_keys.rsa import RSA_OID
        with self.assertRaises(Unusable):parse_der(wrong_der.replace(RSA_OID,RSA_OID[:-1]+b'\2'),pkcs8=True)
        n,e,d,p,q,dp,dq,qi = numbers()
        for parts in ((1,n,e,d,p,q,dp,dq,qi),(0,n+2,e,d,p,q,dp,dq,qi),(0,n,e,d,p,q,dp+1,dq,qi)):
            with self.assertRaises(Unusable): parse_der(tlv(48,b''.join(integer(v) for v in parts)))
        for value in (b'\x30\x80',b'\x30\x81\x01\0',b'\x30\x82\0\xff',tlv(48,integer(0))+b'\0'):
            with self.assertRaises(Unusable): parse_der(value)

    @unittest.skipUnless(shutil.which('openssl'),'OpenSSL unavailable')
    def test_openssl_signature(self):
        key = pem(); signature = base64.b64decode(parse(key).sign('disposable cross check'))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = subprocess.run(['openssl','pkey','-pubout'],input=key,capture_output=True)
            self.assertTrue(result.returncode == 0,'Public key conversion')
            (root/'public.pem').write_bytes(result.stdout)
            (root/'signature').write_bytes(signature)
            result = subprocess.run(['openssl','dgst','-sha1','-verify',str(root/'public.pem'),'-signature',str(root/'signature')],input=b'disposable cross check',capture_output=True)
            self.assertTrue(result.returncode == 0,'OpenSSL verifies signature')

    def test_no_committed_private_armour(self):
        root = Path(__file__).resolve().parents[1]
        # Scan source files including new untracked files, excluding runtime and Git.
        start = b'-----'+b'BEGIN '+b'(?:[A-Z0-9]+ )?'+b'PRIVATE'+b' KEY-----'
        import re
        from wifi_bridge.privacy import private_path
        names=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=root).split(b'\0')
        for name in names:
            if not name:continue
            path=root/name.decode()
            if path.is_file() and not path.is_symlink():
                self.assertFalse(private_path(path.relative_to(root)),'Secret runtime file forbidden in source tree')
                self.assertFalse(re.search(start,path.read_bytes()),'Private armour forbidden in '+str(path.relative_to(root)))



class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_roundtrip_modes_metadata_and_refused(self):
        store = KeyStore(self.root)
        if not store.available: self.skipTest('System GCM unavailable')
        key = pem(); store.put('seestar',key,'import')
        self.assertTrue(store.read('seestar') == key,'Encrypted key round trip')
        for path in (self.root/'secrets').iterdir():
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertFalse(bytes(key) in path.read_bytes(),'No plaintext on disk')
        public = store.status()
        self.assertEqual(set(public),{'family','state','source','updated_seq','unlocks'})
        store.verdict('seestar','verified')
        restored = KeyStore(self.root)
        self.assertEqual(restored.status()['state'],'verified')
        self.assertTrue(restored.read('seestar') == key,'Restart key round trip')
        restored.verdict('seestar','refused')
        self.assertIsNone(restored.read('seestar'))
        self.assertEqual(KeyStore(self.root).status()['state'],'refused')
        self.assertGreater(restored.status()['updated_seq'],public['updated_seq'])

    def test_tamper_and_unavailable_never_write_plaintext(self):
        store = KeyStore(self.root)
        if not store.available: self.skipTest('System GCM unavailable')
        store.put('seestar',pem(),'import')
        path = self.root/'secrets'/'maker-keys.bin'
        record = json.loads(path.read_bytes())
        data = bytearray(base64.b64decode(record['keys']['seestar']['ciphertext'])); data[-1] ^= 1
        record['keys']['seestar']['ciphertext'] = base64.b64encode(data).decode()
        path.write_text(json.dumps(record))
        self.assertEqual(KeyStore(self.root).status()['state'],'storage_unavailable')
        empty = self.root/'unavailable'
        with patch('ctypes.util.find_library',return_value=None), patch('ctypes.CDLL',side_effect=OSError('unavailable')): unavailable = KeyStore(empty)
        self.assertEqual(unavailable.status()['state'],'storage_unavailable')
        with self.assertRaises(StorageUnavailable): unavailable.put('seestar',pem(),'import')
        self.assertFalse(empty.exists())

    def test_linux_declared_soname_fallback_and_missing_provider(self):
        import ctypes
        name=ctypes.util.find_library('crypto')
        if not name:self.skipTest('Host libcrypto unavailable')
        load=ctypes.CDLL;requested=[]
        def system_library(value):requested.append(value);return load(name)
        with patch('wifi_bridge.maker_keys.store.sys.platform','linux'), patch('ctypes.util.find_library',return_value=None), patch('ctypes.CDLL',side_effect=system_library):
            crypto=GCM()
        self.assertEqual(requested,['libcrypto.so.3'])
        with patch('ctypes.CDLL',return_value=object()):
            with self.assertRaises(StorageUnavailable):GCM()

    def test_delete_absent_remains_absent_after_restart(self):
        store=KeyStore(self.root)
        store.verdict('seestar','absent')
        self.assertEqual(KeyStore(self.root).status()['state'],'absent')

    def test_private_fifo_symlink_and_wrong_shapes_fail_closed(self):
        import os
        directory=self.root/'secrets';directory.mkdir(mode=0o700)
        path=directory/'maker-keys.bin'
        for value in ([],{'sequence':0,'keys':[]},{'sequence':1,'keys':{'seestar':[]}}):
            path.write_text(json.dumps(value));path.chmod(0o600)
            self.assertEqual(KeyStore(self.root).status()['state'],'storage_unavailable')
        path.unlink();os.mkfifo(path,0o600)
        self.assertEqual(KeyStore(self.root).status()['state'],'storage_unavailable')
        path.unlink();other=self.root/'other';other.write_bytes(b'opaque');other.chmod(0o600);path.symlink_to(other)
        self.assertEqual(KeyStore(self.root).status()['state'],'storage_unavailable')

    def test_aad_and_kek_modes(self):
        crypto = GCM()
        data = crypto.crypt(True,bytearray(32),bytearray(12),b'family-one',b'private')
        with self.assertRaises(StorageUnavailable): crypto.crypt(False,bytearray(32),bytearray(12),b'family-two',data)
        store = KeyStore(self.root);store.put('seestar',pem(),'import')
        (self.root/'secrets'/'maker-keys.kek').chmod(0o644)
        self.assertEqual(KeyStore(self.root).status()['state'],'storage_unavailable')
