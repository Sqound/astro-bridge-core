"""A fake NexStar hand control on a pseudo-terminal: answers the documented frames only."""
import os
import threading

MOVES = (b'P\x02\x10\x24\x03\x00\x00\x00',)


class FakeHandControl:
    def __init__(self, model=11, version=(4, 15), boards=(5, 14), responds=True):
        self.model, self.version, self.boards, self.responds = model, version, boards, responds
        self.master, self.slave = os.openpty()      # the slave stays open: the device never "hangs up"
        self.path = os.ttyname(self.slave)
        self.frames, self.lock, self.buffer = [], threading.Lock(), b''
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        import select
        while not self.done.is_set():
            if not select.select([self.master], [], [], .05)[0]: continue
            try: data = os.read(self.master, 256)
            except OSError: return
            self.buffer += data
            while self.buffer:
                size = {b'K': 2, b'P': 8}.get(self.buffer[:1], 1)
                if len(self.buffer) < size: break
                frame, self.buffer = self.buffer[:size], self.buffer[size:]
                with self.lock: self.frames.append(frame)
                reply = self._reply(frame)
                if reply is not None and self.responds: os.write(self.master, reply)

    def _reply(self, frame):
        if frame[:1] == b'K': return frame[1:2] + b'#'
        if frame == b'V': return bytes(self.version) + b'#'
        if frame == b'm': return bytes([self.model]) + b'#'
        if frame == b'M': return b'#'
        if frame[:1] == b'P':
            return bytes(self.boards) + b'#' if frame[3] == 0xFE else b'#'
        return None

    def sent(self):
        with self.lock: return list(self.frames)

    def close(self):
        self.done.set(); self.thread.join(1)
        for fd in (self.master, self.slave):
            try: os.close(fd)
            except OSError: pass
