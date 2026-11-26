"""Bounded Unix socket transport for a disposable ASI SDK process on Linux.

The transport is shared by every manufacturer driver (vendor_process.py).
"""
import json
import socket
import struct
import subprocess
import sys
import time

from .driver import CameraDriver, CameraError, validate_info
from .frames import Frame, MAX_PIXELS

CONTROL_BYTES = 4096
CALL_SECONDS = 4
OPS = ('open', 'close', 'state', 'start_exposure', 'abort', 'read_frame')
ERRORS = frozenset(('camera_driver_unavailable', 'camera_unsupported', 'capture_failed',
                    'invalid_exposure', 'invalid_roi', 'not_open', 'not_ready', 'busy'))


def exact(sock, size, deadline):
    buffer = bytearray(size)
    view = memoryview(buffer)
    while view:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        sock.settimeout(remaining)
        count = sock.recv_into(view)
        if not count:
            raise EOFError
        view = view[count:]
    return bytes(buffer)


def receive(sock, deadline):
    size, = struct.unpack('!I', exact(sock, 4, deadline))
    if size > CONTROL_BYTES:
        raise ValueError
    return json.loads(exact(sock, size, deadline))


def send(sock, value):
    raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > CONTROL_BYTES:
        raise ValueError
    sock.sendall(struct.pack('!I', len(raw)) + raw)


class IsolatedASI(CameraDriver):
    def __init__(self, *, device):
        from .usb import validate_device
        self.device = validate_device(device)
        self.process = self.sock = self.description = None
        self.failed = False

    def _stop(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        proc = self.process
        if proc is not None:
            try:
                proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    return  # Retain ownership until even an uninterruptible child exits.
            self.process = None

    def _call(self, op, *args):
        if self.sock is None:
            raise CameraError('not_open')
        try:
            deadline = time.monotonic() + CALL_SECONDS
            self.sock.settimeout(CALL_SECONDS)
            send(self.sock, [op, args])
            reply = receive(self.sock, deadline)
            if 'error' in reply:
                code = reply['error']
                raise CameraError(code if code in ERRORS else 'capture_failed')
            value = reply['value']
            if op == 'read_frame':
                width, height, kind, meta = value
                if (type(width) is not int or type(height) is not int or min(width, height) < 1
                        or width*height > MAX_PIXELS or kind not in ('Byte', 'UInt16')):
                    raise ValueError
                data = exact(self.sock, width*height*(2 if kind == 'UInt16' else 1), deadline)
                return Frame(width, height, kind, data, meta)
            return value
        except CameraError:
            raise
        except (OSError, EOFError, KeyError, TypeError, ValueError, RecursionError):
            self.failed = True
            self._stop()
            raise CameraError('capture_failed') from None

    def open(self):
        if self.process is not None:
            if self.sock is not None:
                return
            if self.process.poll() is None:
                raise CameraError('capture_failed')
            self.process = None
        self.failed = False
        self.sock, child = socket.socketpair()
        try:
            with child:
                self.process = subprocess.Popen(
                    self.command(child.fileno()),
                    pass_fds=(child.fileno(),), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
            info = validate_info(self._call('open', self.device))
            if self.description is not None and self.description != info:
                raise CameraError('camera_unsupported')
            self.description = info
        except BaseException:
            self._stop()
            raise

    def command(self, descriptor):
        """The child process: one per opened camera, the manufacturer library loaded only there."""
        return [sys.executable, '-m', 'wifi_bridge.cameras.zwo_process', str(descriptor)]

    def close(self):
        try:
            if self.process is not None:
                self._call('close')
        except CameraError:
            pass  # The OS also releases handles when the disposable process exits.
        finally:
            self._stop()
            self.failed = False

    def handoff_ready(self):
        """A timed-out SDK child can still own the USB node after close."""
        return self.process is None or self.process.poll() is not None

    def info(self):
        if self.description is None:
            self.open()
            self.close()
        return validate_info(self.description)

    def start_exposure(self, *args):
        self._call('start_exposure', *args)

    def abort(self):
        self._call('abort')

    def state(self):
        if self.sock is None:
            return 'error' if self.failed else 'idle'
        try:
            return self._call('state')
        except CameraError:
            return 'error'

    def read_frame(self):
        return self._call('read_frame')


def create_driver(device):
    from .zwo_asi import ASIDriver
    from .zwo_platform import declared_path
    return ASIDriver(device, declared_path())


def serve(sock, factory=create_driver, arm_watchdog=lambda seconds: None):
    driver = None
    try:
        while True:
            # EOF on parent exit also wakes an idle helper. Bound each request
            # after the first byte; suppress all native output in the launcher.
            arm_watchdog(0)
            sock.settimeout(None)
            if not sock.recv(1, socket.MSG_PEEK):
                break
            arm_watchdog(2 * CALL_SECONDS)
            op, args = receive(sock, time.monotonic()+CALL_SECONDS)
            if op not in OPS or not isinstance(args, list) or len(args) > 5:
                break
            try:
                if op == 'open':
                    if driver is not None:
                        break
                    driver = factory(*args)
                    driver.open()
                    value = driver.info()
                elif driver is None:
                    break
                else:
                    value = getattr(driver, op)(*args)
                if isinstance(value, Frame):
                    send(sock, {'value': [value.width, value.height, value.element_type, dict(value.metadata)]})
                    sock.sendall(value.data)
                else:
                    send(sock, {'value': value})
            except CameraError as error:
                send(sock, {'error': error.code if error.code in ERRORS else 'camera_unsupported'})
            if op == 'close':
                break
    finally:
        if driver:
            arm_watchdog(2 * CALL_SECONDS)
            driver.close()


if __name__ == '__main__':
    import resource
    import signal
    # Enforce a backstop even when the parent disappears during a stuck native
    # call. Socket EOF releases an idle worker. Core files may contain frames.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    with socket.socket(fileno=int(sys.argv[1])) as channel:
        serve(channel, arm_watchdog=signal.alarm)
