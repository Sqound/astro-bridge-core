"""Warm isolated crypto process with bounded, deadline-limited private IPC."""
import json
import os
import selectors
import subprocess
import threading
import time


class CryptoWorker:
    def __init__(self, command, env, timeout=12):
        self.command,self.env,self.timeout=command,env,timeout
        self.process=None;self.lock=threading.Lock();self.closed=False

    def terminate(self):
        process=self.process;self.process=None
        if process is not None:
            if process.poll() is None:
                try:process.kill()
                except ProcessLookupError:pass
            process.wait(timeout=2)
            process.stdin.close();process.stdout.close()

    def request(self, value):
        raw=json.dumps(value).encode()+b'\n'
        if len(raw)>32768:raise ValueError('Push request too large')
        with self.lock:
            if self.closed:raise ValueError('Push worker closed')
            try:
                if self.process is None or self.process.poll() is not None:
                    self.terminate()
                    self.process=subprocess.Popen(self.command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,env=self.env,bufsize=0)
                    os.set_blocking(self.process.stdin.fileno(),False)
                    os.set_blocking(self.process.stdout.fileno(),False)
                process=self.process;deadline=time.monotonic()+self.timeout;offset=0;output=bytearray()
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdin,selectors.EVENT_WRITE)
                    selector.register(process.stdout,selectors.EVENT_READ)
                    while True:
                        remaining=deadline-time.monotonic()
                        if remaining<=0:raise ValueError('Push worker deadline')
                        for key,_ in selector.select(remaining):
                            if key.fileobj is process.stdin:
                                offset+=os.write(process.stdin.fileno(),raw[offset:])
                                if offset==len(raw):selector.unregister(process.stdin)
                            else:
                                chunk=os.read(process.stdout.fileno(),min(4097-len(output),4096))
                                if not chunk:raise ValueError('Push worker ended')
                                output.extend(chunk)
                                if len(output)>4096:raise ValueError('Push response too large')
                                if b'\n' in output:
                                    line,extra=output.split(b'\n',1)
                                    if extra or offset!=len(raw):raise ValueError('Push worker protocol')
                                    result=json.loads(line)
                                    if not isinstance(result,dict):raise ValueError('Push worker protocol')
                                    return result
            except (OSError,ValueError,subprocess.SubprocessError):
                self.terminate()
                # Never replay a request whose provider outcome may be unknown.
                raise ValueError('Push worker unavailable') from None

    def close(self):
        with self.lock:
            self.closed=True;self.terminate()
