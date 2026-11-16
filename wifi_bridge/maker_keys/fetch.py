"""Owner-triggered HTTPS range extraction; no package/key bytes reach disk."""
import http.client
import re
import ssl
import struct
import threading
import time
from urllib.parse import urljoin, urlsplit
import zlib

from .rsa import armour, parse, Unusable

START = 'https://dl.zwoastro.com/software?app=SeestarAndroid&platform=android&region=China'
HOSTS = frozenset(('dl.zwoastro.com','api.zwoastro.com'))
MEMBER = b'lib/arm64-v8a/libopenssllib.so'
CAP = 16*1024*1024


class FetchError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class Fetcher:
    def __init__(self, *, connection_factory=None, context=None, cancelled=None):
        self.context = context or ssl.create_default_context()
        if not self.context.check_hostname or self.context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError('Verified TLS is required')
        self.factory = connection_factory or http.client.HTTPSConnection
        self.cancelled = cancelled or threading.Event()
        self.journal = []
        self.connection = None
        self.stream = None
        self.lock = threading.Lock()

    def record(self, step, **values):
        if len(self.journal) < 48: self.journal.append(dict(step=step,**values))

    def close(self):
        self.cancelled.set()
        with self.lock: connection,stream = self.connection,self.stream
        if stream:
            try: stream.shutdown(2)
            except OSError: pass
        if connection: connection.close()

    def range(self, start, end, step, total=None):
        if not 0 <= start <= end or end-start+1 > CAP: raise FetchError('cap')
        url, storage = START, None
        for redirects in range(6):
            if self.cancelled.is_set(): raise FetchError('cancelled')
            parts = urlsplit(url)
            if (parts.scheme != 'https' or parts.username or parts.password or parts.fragment
                    or parts.port not in (None,443) or parts.hostname not in HOSTS | ({storage} if storage else set())):
                raise FetchError('redirectRefused')
            connection = self.factory(parts.hostname, timeout=30, context=self.context)
            response = None
            deadline = time.monotonic()+30
            watched = []
            watchdog = threading.Timer(30,self._interrupt,args=(connection,watched)); watchdog.daemon = True
            def active():
                if self.cancelled.is_set(): raise FetchError('cancelled')
                if time.monotonic() >= deadline: raise FetchError('timeout')
            original_connect = connection.connect
            def connected():
                # http.client may lazily create/recreate a socket after close().
                # Never let a late TLS socket turn a cancelled job into a request.
                active()
                try:
                    original_connect()
                    watched.append(connection.sock)
                    with self.lock: self.stream = connection.sock
                    active()
                except BaseException:
                    self._interrupt(connection,watched)
                    raise
            connection.connect = connected
            try:
                with self.lock:
                    active()
                    self.connection = connection
                watchdog.start()
                active()
                connection.request('GET',parts.path+('?' + parts.query if parts.query else ''),headers={
                    'Range':f'bytes={start}-{end}','Accept-Encoding':'identity','Connection':'close'})
                watched.append(connection.sock)
                with self.lock: self.stream = connection.sock
                active()
                response = connection.getresponse()
                active()
                self.record(step,http_status=response.status,bytes=0)
                if response.status in (301,302,303,307,308):
                    if redirects == 5: raise FetchError('tooManyRedirects')
                    location = response.getheader('Location')
                    if not location or len(location) > 8192: raise FetchError('redirectRefused')
                    next_url = urljoin(url,location); next_parts = urlsplit(next_url)
                    if (next_parts.scheme != 'https' or next_parts.username or next_parts.password or next_parts.fragment
                            or next_parts.port not in (None,443) or not next_parts.hostname): raise FetchError('redirectRefused')
                    host = next_parts.hostname
                    if host not in HOSTS:
                        if parts.hostname != 'api.zwoastro.com' or storage not in (None,host): raise FetchError('redirectRefused')
                        # A maker-authenticated redirect grants exactly one storage
                        # hostname for this request; its own redirects cannot add one.
                        storage = host
                    url = next_url
                    continue
                if response.status == 200: raise FetchError('notPartial')
                if response.status != 206: raise FetchError('status')
                value = response.getheader('Content-Range','')
                match = re.fullmatch(r'bytes ([0-9]{1,10})-([0-9]{1,10})/([0-9]{1,10})',value)
                if not match: raise FetchError('range')
                first,last,size = map(int,match.groups())
                if first != start or last != end or not end < size <= 0xffffffff or total is not None and size != total:
                    raise FetchError('range')
                count = end-start+1
                length = response.getheader('Content-Length')
                if (length is not None and length != str(count) or response.getheader('Content-Encoding','identity') != 'identity'
                        or response.getheader('Transfer-Encoding') is not None): raise FetchError('range')
                data = bytearray()
                try:
                    while len(data) < count:
                        if self.cancelled.is_set(): raise FetchError('cancelled')
                        if time.monotonic() >= deadline: raise FetchError('timeout')
                        chunk = response.read1(min(65536,count-len(data)))
                        active()
                        if not chunk: raise FetchError('range')
                        data.extend(chunk)
                    self.record(step,bytes=len(data))
                    return data,size
                except BaseException:
                    data[:] = b'\0'*len(data)
                    raise
            finally:
                watchdog.cancel()
                if response: response.close()
                self._interrupt(connection,watched)
                with self.lock:
                    if self.connection is connection: self.connection = self.stream = None
        raise FetchError('tooManyRedirects')

    @staticmethod
    def _interrupt(connection, watched):
        for stream in list(watched)+[connection.sock]:
            if stream:
                try: stream.shutdown(2)
                except OSError: pass
        connection.close()

    def fetch(self):
        buffers = []
        def read(start,end,step,total=None):
            data,size = self.range(start,end,step,total); buffers.append(data)
            return data,size
        try:
            _,total = read(0,0,'size')
            if total < 22: raise FetchError('zip')
            tail,_ = read(max(0,total-65557),total-1,'tail',total)
            offset = tail.rfind(b'PK\5\6')
            if offset < 0 or offset+22 > len(tail): raise FetchError('zip')
            _,disk,cd_disk,entries_disk,entries,cd_size,cd_offset,comment = struct.unpack_from('<4s4H2LH',tail,offset)
            if offset+22+comment != len(tail) or disk or cd_disk or entries_disk != entries: raise FetchError('zip')
            if (entries == 65535 or cd_size == 0xffffffff or cd_offset == 0xffffffff
                    or offset >= 20 and tail[offset-20:offset-16] == b'PK\6\7'): raise FetchError('ZIP64')
            if not 0 < cd_size <= CAP or not 0 < entries <= 65534: raise FetchError('cap')
            if cd_offset+cd_size > total-len(tail)+offset: raise FetchError('zip')
            directory,_ = read(cd_offset,cd_offset+cd_size-1,'directory',total)
            at, members = 0, []
            for _ in range(entries):
                if at+46 > len(directory) or directory[at:at+4] != b'PK\1\2': raise FetchError('zip')
                header = struct.unpack_from('<4s6H3L5H2L',directory,at)
                flags,method,crc,compressed,size = header[3],header[4],header[7],header[8],header[9]
                nlen,xlen,clen,start_disk,local = header[10],header[11],header[12],header[13],header[16]
                end = at+46+nlen+xlen+clen
                if end > len(directory) or start_disk: raise FetchError('zip')
                if 0xffffffff in (compressed,size,local): raise FetchError('ZIP64')
                extra = memoryview(directory)[at+46+nlen:at+46+nlen+xlen]
                index = 0
                while index < len(extra):
                    if index+4 > len(extra): raise FetchError('zip')
                    kind,length = struct.unpack_from('<HH',extra,index)
                    if kind == 1: raise FetchError('ZIP64')
                    index += 4+length
                    if index > len(extra): raise FetchError('zip')
                if directory[at+46:at+46+nlen] == MEMBER:
                    members.append((flags,method,crc,compressed,size,local))
                at = end
            if at != len(directory) or len(members) != 1: raise FetchError('zip')
            flags,method,crc,compressed,size,local = members[0]
            if method not in (0,8) or flags & ~0x808: raise FetchError('method')
            if not 0 < compressed <= CAP or not 0 < size <= CAP: raise FetchError('cap')
            if local+30 > cd_offset: raise FetchError('zip')
            header,_ = read(local,local+29,'header',total)
            magic,_,local_flags,local_method,_,_,local_crc,local_compressed,local_size,nlen,xlen = struct.unpack('<4s5H3L2H',header)
            if magic != b'PK\3\4' or (local_flags,local_method) != (flags,method): raise FetchError('zip')
            if not flags & 8 and (local_crc,local_compressed,local_size) != (crc,compressed,size): raise FetchError('zip')
            if nlen != len(MEMBER) or xlen > 65535 or local+30+nlen+xlen+compressed > cd_offset: raise FetchError('zip')
            data,_ = read(local+30,local+30+nlen+xlen+compressed-1,'member',total)
            if data[:nlen] != MEMBER: raise FetchError('zip')
            # Local extras cannot introduce ZIP64 when the central record omits it.
            index = nlen
            while index < nlen+xlen:
                if index+4 > nlen+xlen: raise FetchError('zip')
                kind,length = struct.unpack_from('<HH',data,index)
                if kind == 1: raise FetchError('ZIP64')
                index += 4+length
                if index > nlen+xlen: raise FetchError('zip')
            payload = memoryview(data)[nlen+xlen:]
            if method == 0: member = bytearray(payload)
            else:
                inflater = zlib.decompressobj(-15)
                member = bytearray(inflater.decompress(payload,CAP+1)); buffers.append(member)
                if len(member) > CAP or inflater.unconsumed_tail: raise FetchError('cap')
                if not inflater.eof or inflater.unused_data: raise FetchError('zip')
            if method == 0: buffers.append(member)
            if len(member) != size: raise FetchError('cap')
            if zlib.crc32(member) != crc: raise FetchError('CRC')
            self.record('crc',crcVerified=True)
            candidates = {}
            count = 0
            for label in ('RSA',''):
                first,last = armour(label),armour(label,True)
                start = 0
                while True:
                    position = member.find(first,start)
                    if position < 0: break
                    count += 1
                    if count > 16: raise FetchError('twoKeys')
                    end = member.find(last,position+len(first),position+8192)
                    start = position+len(first)
                    if end < 0: continue
                    candidate = bytearray(member[position:end+len(last)]); buffers.append(candidate)
                    try: key = parse(candidate)
                    except Unusable: continue
                    candidates[key.n] = candidate
            self.record('candidates',candidates=len(candidates))
            if not candidates: raise FetchError('noKey')
            if len(candidates) != 1: raise FetchError('twoKeys')
            if self.cancelled.is_set(): raise FetchError('cancelled')
            self.record('complete',verdict='accepted')
            return bytearray(next(iter(candidates.values())))
        except FetchError as error:
            self.record('complete',verdict=error.reason)
            raise
        except (OSError, ValueError, AttributeError, http.client.HTTPException, zlib.error, struct.error):
            reason = 'cancelled' if self.cancelled.is_set() else 'network'
            self.record('complete',verdict=reason)
            raise FetchError(reason) from None
        finally:
            for buffer in buffers: buffer[:] = b'\0'*len(buffer)
