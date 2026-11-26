"""Shared pieces of the manufacturer camera adapters other than ZWO.

Each adapter (player_one.py, qhyccd.py, atik.py, moravian.py) binds only the
functions whose C signature is written in that manufacturer's official SDK
header, and runs only inside the disposable child process of one opened camera
(vendor_process.py). None of them is verified on hardware: each declares
VERIFIED_ON_HARDWARE = False, as the driver catalog does.

What the official documentation does not establish is not guessed: the adapter
refuses it with a plain error (camera_unsupported, invalid_roi) instead.
"""
import ctypes as C
import sys
import threading

from .driver import CameraError

# Bayer offsets of the colour patterns, as the ZWO adapter publishes them:
# the (x, y) position of the red pixel in the 2 x 2 cell.
OFFSETS = {'RG': (0, 0), 'BG': (1, 1), 'GR': (1, 0), 'GB': (0, 1)}
MAX_CAMERAS = 16


def bind(library, signatures):
    """Declare each function's result and argument types; a missing symbol means another library."""
    try:
        for name, (restype, argtypes) in signatures.items():
            function = getattr(library, name)
            function.argtypes, function.restype = list(argtypes), restype
    except AttributeError:
        raise CameraError('camera_driver_unavailable') from None
    return library


def single_usb(inventory, vendors, device, product=None):
    """The device is the only USB device of this manufacturer (and model, when given) on the bridge.

    None of these SDKs names the USB port of a camera. Like the ZWO adapter, a
    camera is served only when nothing else could be confused with it.
    """
    rows = inventory.scan()
    if rows is None:
        raise CameraError('camera_unsupported')
    matches = [row for row in rows if row['vendor_id'] in vendors
               and (product is None or row['product_id'] == product)]
    if len(matches) != 1 or matches[0] != device:
        raise CameraError('camera_unsupported')


def text(raw):
    """A bounded printable name from a C string buffer."""
    value = bytes(raw).split(b'\0', 1)[0].decode('ascii', errors='replace').strip()
    value = ''.join(c for c in value if ord(c) >= 32)[:64]
    return value


def native_u16(data, offset=0):
    return int.from_bytes(bytes(data[offset:offset + 2]), sys.byteorder)


def u16_bytes(value):
    return int(value).to_bytes(2, sys.byteorder)


def flip_rows(data, width, height, size):
    """Rows stored bottom-up become top-down, the order of a Frame."""
    line = width * size
    return b''.join(data[row * line:(row + 1) * line] for row in range(height - 1, -1, -1))


class NativeJob:
    """One blocking manufacturer call sequence on a thread of the child process.

    The request loop of the child keeps answering state requests while the
    camera exposes or reads out. Nothing else calls the library meanwhile.
    """
    def __init__(self, work):
        self.result = self.error = None
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(work,), name='camera-native', daemon=True)

    def _run(self, work):
        try:
            self.result = work()
        except BaseException as error:  # Reported as a failed capture, never raised in the thread.
            self.error = error
        finally:
            self.finished.set()

    def start(self):
        self.thread.start()
        return self

    def done(self):
        return self.finished.is_set()

    def alive(self):
        return self.thread.is_alive()


def pointer_args(*values):
    return [C.byref(value) for value in values]
