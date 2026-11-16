"""Small Linux v6.12 UAPI binding; no host access until explicitly requested.

Layouts encode pointer and timestamp widths independently. Runtime always uses
kernel time64 (including armhf/musl); legacy time32 exists only for ABI tests.
Only little-endian ARM/x86 with asm-generic ioctl numbering is supported.
"""
import ctypes as C
import fcntl
import mmap
import os
from pathlib import Path
import platform
import re
import stat
import sys
from types import SimpleNamespace

from .driver import CameraError
from .usb import MAX_ENTRIES, validate_device

U8, U32, I32 = C.c_uint8, C.c_uint32, C.c_int32
CAPTURE, MMAP = 1, 1
EXPOSURE_AUTO, EXPOSURE_ABSOLUTE = 0x009a0901, 0x009a0902
GAIN, AUTOGAIN = 0x00980913, 0x00980912
MAX_BUFFER_BYTES = 4 * 1024 * 1024
MAX_BUFFERS = 4


def fourcc(value):
    return int.from_bytes(value.encode('ascii'), 'little')


GREY, Y16, YUYV = map(fourcc, ('GREY', 'Y16 ', 'YUYV'))


def structure(name, fields):
    return type(name, (C.LittleEndianStructure,), {'_fields_': fields})


Capability = structure('Capability', [('driver', U8 * 16), ('card', U8 * 32),
    ('bus_info', U8 * 32), ('version', U32), ('capabilities', U32),
    ('device_caps', U32), ('reserved', U32 * 3)])
FormatDescription = structure('FormatDescription', [('index', U32), ('type', U32),
    ('flags', U32), ('description', U8 * 32), ('pixelformat', U32), ('reserved', U32 * 4)])
PixelFormat = structure('PixelFormat', [(name, U32) for name in
    ('width', 'height', 'pixelformat', 'field', 'bytesperline', 'sizeimage',
     'colorspace', 'priv', 'flags', 'ycbcr_enc', 'quantization', 'xfer_func')])
FrameSize = structure('FrameSize', [('index', U32), ('pixel_format', U32),
    ('type', U32), ('sizes', U32 * 6), ('reserved', U32 * 2)])
RequestBuffers = structure('RequestBuffers', [('count', U32), ('type', U32),
    ('memory', U32), ('capabilities', U32), ('flags', U8), ('reserved', U8 * 3)])
QueryControl = structure('QueryControl', [('id', U32), ('type', U32), ('name', U8 * 32),
    ('minimum', I32), ('maximum', I32), ('step', I32), ('default_value', I32),
    ('flags', U32), ('reserved', U32 * 2)])
Control = structure('Control', [('id', U32), ('value', I32)])
StreamParameters = structure('StreamParameters', [('type', U32), ('data', U32 * 50)])


def layouts(pointer_bits, time_bits=64):
    """Derived fixed-width layouts, also constructible on a different host ABI."""
    if (pointer_bits, time_bits) not in ((32, 32), (32, 64), (64, 64)):
        raise ValueError('unsupported_v4l2_abi')
    pointer = U32 if pointer_bits == 32 else C.c_uint64
    stamp = I32 if time_bits == 32 else C.c_int64
    fields = [(name, U32) for name in ('index', 'type', 'bytesused', 'flags', 'field')]
    # ARM EABI aligns time64 to 8 even when pointers/long are 32-bit.
    if time_bits == 64:
        fields.append(('_timestamp_padding', U32))
    fields += [('timestamp', stamp * 2), ('timecode', U8 * 16), ('sequence', U32),
               ('memory', U32), ('offset', pointer), ('length', U32),
               ('reserved2', U32), ('request_fd', I32)]
    if pointer_bits == 64:
        fields.append(('_tail_padding', U32))
    buffer = structure('Buffer', fields)
    class FormatUnion(C.Union):
        _fields_ = [('pix', PixelFormat), ('raw', U8 * 200), ('alignment', pointer)]
    fmt = structure('Format', [('type', U32), ('fmt', FormatUnion)])
    return SimpleNamespace(Buffer=buffer, Format=fmt)


def request(number, value_type, direction=3):
    return (direction << 30) | (C.sizeof(value_type) << 16) | (ord('V') << 8) | number


def requests(abi):
    return {name: request(number, kind, direction) for name, number, kind, direction in (
        ('QUERYCAP', 0, Capability, 2), ('ENUM_FMT', 2, FormatDescription, 3),
        ('S_FMT', 5, abi.Format, 3), ('REQBUFS', 8, RequestBuffers, 3),
        ('QUERYBUF', 9, abi.Buffer, 3), ('QBUF', 15, abi.Buffer, 3),
        ('DQBUF', 17, abi.Buffer, 3), ('STREAMON', 18, U32, 1),
        ('STREAMOFF', 19, U32, 1), ('G_PARM', 21, StreamParameters, 3),
        ('G_CTRL', 27, Control, 3), ('S_CTRL', 28, Control, 3),
        ('QUERYCTRL', 36, QueryControl, 3), ('ENUM_FRAMESIZES', 74, FrameSize, 3))}


class LinuxVideoIO:
    """Injected boundary for node resolution, fd/ioctl and mmap operations.

    Resolves only video nodes descended from the already discovered USB port.
    No arbitrary path is accepted from a client or camera table. Descriptor fstat
    and sysfs dev checks prevent opening a substituted regular file/device node.
    """
    def __init__(self, *, sysfs='/sys/class/video4linux', usb_root='/sys/bus/usb/devices', dev_root='/dev'):
        self.sysfs, self.usb_root, self.dev_root = map(Path, (sysfs, usb_root, dev_root))
        self.abi = layouts(C.sizeof(C.c_void_p) * 8)
        self.requests = requests(self.abi)

    def nodes(self, device):
        device = validate_device(device)
        if sys.platform != 'linux' or sys.byteorder != 'little' or platform.machine().lower() not in (
                'x86_64', 'amd64', 'aarch64', 'armv6l', 'armv7l', 'armv8l'):
            raise CameraError('camera_unsupported')
        parent = (self.usb_root / device['port_path']).resolve(strict=True)
        # Match IDs again so a removed/replaced USB device cannot reuse stale metadata.
        for name, expected in (('idVendor', device['vendor_id']), ('idProduct', device['product_id'])):
            with (parent / name).open('rb') as stream:
                raw = stream.read(6)
            if not re.fullmatch(b'[0-9a-fA-F]{4}\n?', raw) or int(raw, 16) != expected:
                raise CameraError('camera_unsupported')
        with os.scandir(self.sysfs) as entries:
            paths = []
            for index, entry in enumerate(entries):
                if index >= MAX_ENTRIES:
                    raise CameraError('camera_unsupported')
                if re.fullmatch(r'video[0-9]{1,3}', entry.name):
                    paths.append(self.sysfs / entry.name)
        nodes = []
        for path in sorted(paths):
            resolved = path.resolve(strict=True)
            if parent not in resolved.parents:
                continue
            with (path / 'dev').open('rb') as stream:
                raw = stream.read(32)
            if not re.fullmatch(b'[0-9]{1,5}:[0-9]{1,5}\n?', raw):
                raise CameraError('camera_unsupported')
            major, minor = map(int, raw.split(b':'))
            nodes.append((self.dev_root / path.name, os.makedev(major, minor)))
            if len(nodes) > 8:
                raise CameraError('camera_unsupported')
        return nodes

    def open(self, node):
        path, expected = node
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISCHR(info.st_mode) or info.st_rdev != expected:
                raise CameraError('camera_unsupported')
        except BaseException:
            os.close(fd)
            raise
        return fd

    def ioctl(self, fd, name, value):
        raw = bytearray(bytes(value))
        fcntl.ioctl(fd, self.requests[name], raw, True)
        C.memmove(C.addressof(value), bytes(raw), C.sizeof(value))

    def map(self, fd, length, offset):
        return mmap.mmap(fd, length, flags=mmap.MAP_SHARED,
                         prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=offset)

    def close(self, fd):
        os.close(fd)
