"""Private atomic AES-256-GCM storage using the platform's system libcrypto."""
import base64
import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import threading

from .rsa import parse

FILES = frozenset(('maker-keys.kek', 'maker-keys.bin'))
UNLOCKS = ('stacking', 'exposures', 'goto', 'temperature', 'battery')


class StorageUnavailable(OSError):
    def __init__(self): super().__init__('STORAGE_UNAVAILABLE')


class GCM:
    def __init__(self):
        try:
            name = ctypes.util.find_library('crypto')
            # The Linux platform declares libcrypto3; minimal musl systems may
            # lack ldconfig/compiler helpers used by find_library. Resolve only
            # this declared soname through the system loader, never vendor paths.
            if not name and sys.platform.startswith('linux'): name = 'libcrypto.so.3'
            if not name: raise StorageUnavailable()
            self.lib = ctypes.CDLL(name)
            ptr, integer = ctypes.c_void_p, ctypes.c_int
            signatures = {'EVP_CIPHER_CTX_new': (ptr, []), 'EVP_CIPHER_CTX_free': (None,[ptr]),
                'EVP_aes_256_gcm': (ptr, []), 'EVP_CIPHER_CTX_ctrl': (integer,[ptr,integer,integer,ptr])}
            for prefix in ('Encrypt','Decrypt'):
                signatures.update({'EVP_'+prefix+'Init_ex':(integer,[ptr,ptr,ptr,ptr,ptr]),
                    'EVP_'+prefix+'Update':(integer,[ptr,ptr,ctypes.POINTER(integer),ptr,integer]),
                    'EVP_'+prefix+'Final_ex':(integer,[ptr,ptr,ctypes.POINTER(integer)])})
            for name, (result, args) in signatures.items():
                fn = getattr(self.lib, name); fn.restype, fn.argtypes = result, args
            if not self.lib.EVP_aes_256_gcm(): raise StorageUnavailable()
            # A real round trip detects providers that expose the symbol but lack GCM.
            key, nonce = bytearray(32), bytearray(12)
            trial = self.crypt(True, key, nonce, b'test', b'probe')
            if self.crypt(False, key, nonce, b'test', trial) != b'probe': raise StorageUnavailable()
        except (OSError, AttributeError): raise StorageUnavailable() from None

    def crypt(self, encrypt, key, nonce, aad, data):
        if len(key) != 32 or len(nonce) != 12 or not encrypt and len(data) < 16: raise StorageUnavailable()
        buffers = []
        def buffer(value):
            value = bytearray(value); buffers.append(value)
            return (ctypes.c_ubyte*len(value)).from_buffer(value)
        lib = self.lib; ctx = lib.EVP_CIPHER_CTX_new()
        if not ctx: raise StorageUnavailable()
        out = bytearray(len(data)+16); written, final = ctypes.c_int(), ctypes.c_int()
        try:
            prefix = 'EVP_'+('Encrypt' if encrypt else 'Decrypt')
            if getattr(lib,prefix+'Init_ex')(ctx,lib.EVP_aes_256_gcm(),None,buffer(key),buffer(nonce)) != 1: raise StorageUnavailable()
            if getattr(lib,prefix+'Update')(ctx,None,ctypes.byref(written),buffer(aad),len(aad)) != 1: raise StorageUnavailable()
            source = data if encrypt else data[:-16]
            if getattr(lib,prefix+'Update')(ctx,(ctypes.c_ubyte*len(out)).from_buffer(out),ctypes.byref(written),buffer(source),len(source)) != 1: raise StorageUnavailable()
            size = written.value
            if not encrypt and lib.EVP_CIPHER_CTX_ctrl(ctx,0x11,16,buffer(data[-16:])) != 1: raise StorageUnavailable()
            if getattr(lib,prefix+'Final_ex')(ctx,ctypes.byref((ctypes.c_ubyte*len(out)).from_buffer(out),size),ctypes.byref(final)) != 1: raise StorageUnavailable()
            size += final.value
            if encrypt:
                tag = bytearray(16); buffers.append(tag)
                if lib.EVP_CIPHER_CTX_ctrl(ctx,0x10,16,(ctypes.c_ubyte*16).from_buffer(tag)) != 1: raise StorageUnavailable()
                return bytearray(out[:size]+tag)
            return bytearray(out[:size])
        finally:
            lib.EVP_CIPHER_CTX_free(ctx)
            out[:] = b'\0'*len(out)
            for value in buffers: value[:] = b'\0'*len(value)


def private_read(path, maximum):
    descriptor = os.open(path, os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > maximum: raise StorageUnavailable()
        with os.fdopen(descriptor, 'rb', closefd=False) as stream: return bytearray(stream.read(maximum+1))
    finally: os.close(descriptor)


def atomic(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix='.'+path.name+'-', dir=path.parent)
    try:
        os.fchmod(descriptor,0o600)
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,path)
        directory = os.open(path.parent,os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


class KeyStore:
    def __init__(self, state, *, crypto_factory=GCM):
        self.directory = Path(state)/'secrets'
        self.lock = threading.RLock()
        self.record = {'sequence':0, 'keys':{}}
        self.crypto = None
        self.kek = bytearray()
        try:
            self.crypto = crypto_factory()
            if self.directory.is_symlink(): raise StorageUnavailable()
            if (self.directory/'maker-keys.bin').exists():
                if (self.directory/'maker-keys.kek').exists():
                    self.kek = private_read(self.directory/'maker-keys.kek',32)
                    if len(self.kek) != 32: raise StorageUnavailable()
                raw = private_read(self.directory/'maker-keys.bin',65536)
                try: self.record = json.loads(raw)
                finally: raw[:] = b'\0'*len(raw)
                if not isinstance(self.record,dict) or not isinstance(self.record.get('keys'),dict) or set(self.record) != {'sequence','keys'} or type(self.record['sequence']) is not int or not 0 <= self.record['sequence'] <= 2**63-1 or set(self.record['keys'])-{'seestar'}: raise StorageUnavailable()
                for family, row in self.record['keys'].items():
                    if not isinstance(row,dict) or row.get('state') not in ('unverified','verified','refused','absent') or row['source'] not in ('import','maker_package',None): raise StorageUnavailable()
                    if type(row.get('updated_seq')) is not int or not 0 < row['updated_seq'] <= self.record['sequence']: raise StorageUnavailable()
                    if row['state'] in ('unverified','verified'):
                        if len(self.kek) != 32: raise StorageUnavailable()
                        plain = self.read(family)
                        try: parse(plain)
                        finally: plain[:] = b'\0'*len(plain)
            elif (self.directory/'maker-keys.kek').exists():
                self.kek = private_read(self.directory/'maker-keys.kek',32)
                if len(self.kek) != 32: raise StorageUnavailable()
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            self.crypto = None
            self.kek[:] = b'\0'*len(self.kek)
            self.record = {'sequence':0, 'keys':{}}

    @property
    def available(self): return self.crypto is not None

    def status(self, family='seestar'):
        with self.lock:
            row = self.record['keys'].get(family,{})
            return {'family':family,'state':row.get('state','absent') if self.available else 'storage_unavailable',
                    'source':row.get('source'), 'updated_seq':row.get('updated_seq',0),'unlocks':list(UNLOCKS)}

    def read(self, family):
        with self.lock:
            if not self.available: raise StorageUnavailable()
            row = self.record['keys'].get(family,{})
            if row.get('state') not in ('unverified','verified'): return None
            try:
                data = base64.b64decode(row['ciphertext'],validate=True)
                return self.crypto.crypt(False,self.kek,data[:12],('astro-bridge-maker-key-v1|'+family).encode(),data[12:])
            except (ValueError, KeyError): raise StorageUnavailable() from None

    def put(self, family, pem, source):
        parse(pem)
        with self.lock:
            if not self.available: raise StorageUnavailable()
            try:
                self.prepare()
                if not self.kek:
                    self.kek = bytearray(secrets.token_bytes(32))
                    atomic(self.directory/'maker-keys.kek',self.kek)
                nonce = secrets.token_bytes(12)
                data = self.crypto.crypt(True,self.kek,nonce,('astro-bridge-maker-key-v1|'+family).encode(),pem)
                self.change(family, {'state':'unverified','source':source,'ciphertext':base64.b64encode(nonce+data).decode()})
            except OSError:
                self.crypto = None
                raise StorageUnavailable() from None

    def prepare(self):
        if self.directory.is_symlink(): raise StorageUnavailable()
        self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid(): raise StorageUnavailable()
        self.directory.chmod(0o700)

    def change(self, family, row):
        if not self.available: raise StorageUnavailable()
        if self.record['sequence'] >= 2**63-1: raise StorageUnavailable()
        seq = self.record['sequence']+1
        row = dict(row,updated_seq=seq)
        record = {'sequence':seq,'keys':dict(self.record['keys'],**{family:row})}
        try:
            self.prepare()
            atomic(self.directory/'maker-keys.bin',json.dumps(record,separators=(',',':')).encode())
        except OSError:
            self.crypto = None
            raise StorageUnavailable() from None
        self.record = record

    def verdict(self, family, state):
        with self.lock:
            if state not in ('verified','refused','absent'): raise ValueError('Invalid key state')
            row = self.record['keys'].get(family,{})
            self.change(family, dict(row,state=state) if state == 'verified' else {'state':state,'source':None})

    def close(self):
        with self.lock:
            self.kek[:] = b'\0'*len(self.kek)
            self.crypto = None
