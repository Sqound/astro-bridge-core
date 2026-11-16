"""Synthetic V4L2 only: no host USB/sysfs/video-node reads or real ioctl calls."""
import ctypes as C
import errno
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from wifi_bridge.cameras import v4l2 as V
from wifi_bridge.cameras.alpaca import image_bytes, image_json
from wifi_bridge.cameras.driver import CameraError
from wifi_bridge.cameras.registry import CameraRegistry, load_registry
from wifi_bridge.cameras.service import CameraService
from wifi_bridge.cameras.uvc import UVCCamera, luminance, MAX_STACK

DEVICE = dict(vendor_id=0x1234, product_id=0x5678, device_class=0,
              interface_classes=[14], speed_mbps=480, bus=1, port_path='1-2')


class Mapping(bytearray):
    closed = False

    def close(self):
        self.closed = True


class FakeVideo:
    def __init__(self, formats=(V.GREY,), *, width=2, height=2, stride=None, bits=64):
        self.abi = V.layouts(bits)
        self.formats = formats
        self.width, self.height, self.stride = width, height, stride
        self.calls, self.mappings, self.frames = [], [], []
        self.controls = {V.EXPOSURE_ABSOLUTE: (1, 100, 1000, 1), V.GAIN: (1, 0, 10, 1),
                         V.EXPOSURE_AUTO: (3, 0, 3, 1), V.AUTOGAIN: (2, 0, 1, 1)}
        self.values = {}
        self.fail, self.fail_at, self.counts = None, 1, {}
        self.buffers, self.buffer_length = 2, 64
        self.bad = {}
        self.fd_open = False
        self.closed_fds = 0
        self.now = 0

    def nodes(self, device):
        self.calls.append(('nodes',))
        return ['fake']

    def open(self, node):
        assert not self.fd_open
        self.fd_open = True
        self.calls.append(('open',))
        return 42

    def close(self, fd):
        assert self.fd_open
        self.fd_open = False
        self.closed_fds += 1
        self.calls.append(('close',))

    def map(self, fd, length, offset):
        if self.fail == 'map':
            raise OSError(errno.ENOMEM, 'fake')
        value = Mapping(length)
        self.mappings.append(value)
        return value

    def ioctl(self, fd, name, value):
        assert self.fd_open
        self.calls.append((name, getattr(value, 'index', None), getattr(value, 'id', None), getattr(value, 'value', None)))
        self.counts[name] = self.counts.get(name, 0) + 1
        if self.fail == name and self.counts[name] >= self.fail_at:
            raise OSError(errno.EIO, 'fake')
        if name == 'QUERYCAP':
            value.driver[:8] = b'uvcvideo'
            value.capabilities = 0x80000000
            value.device_caps = self.bad.get('caps', 0x04000001)
        elif name == 'ENUM_FMT':
            if value.index >= len(self.formats):
                raise OSError(errno.EINVAL, 'end')
            value.pixelformat = self.formats[value.index]
        elif name == 'ENUM_FRAMESIZES':
            if value.index:
                raise OSError(errno.EINVAL, 'end')
            value.type = 1
            value.sizes[0:2] = (self.width, self.height)
        elif name == 'S_FMT':
            pix = value.fmt.pix
            if pix.pixelformat in self.bad.get('reject_formats', ()):
                raise OSError(errno.EINVAL, 'unsupported')
            pix.width, pix.height = self.width, self.height
            pix.bytesperline = self.stride or self.width * (1 if pix.pixelformat == V.GREY else 2)
            pix.sizeimage = pix.bytesperline * pix.height
            for key, item in self.bad.get('format', {}).items():
                setattr(pix, key, item)
        elif name == 'G_PARM':
            value.data[2:4] = self.bad.get('period', (1, 10))
        elif name == 'QUERYCTRL':
            if value.id not in self.controls:
                raise OSError(errno.EINVAL, 'unsupported')
            value.type, value.minimum, value.maximum, value.step = self.controls[value.id]
        elif name == 'S_CTRL':
            self.values[value.id] = value.value
        elif name == 'G_CTRL':
            value.value = self.values[value.id] + self.bad.get('control_adjustment', 0)
        elif name == 'REQBUFS':
            if value.count:
                value.count = self.buffers
        elif name == 'QUERYBUF':
            value.length, value.offset = self.buffer_length, value.index * 4096
        elif name == 'DQBUF':
            if not self.frames:
                raise BlockingIOError(errno.EAGAIN, 'wait')
            raw = self.frames.pop(0)
            mapping = self.mappings[-self.buffers]
            mapping[:len(raw)] = raw
            value.index, value.bytesused = 0, len(raw)
            for key, item in self.bad.get('buffer', {}).items():
                setattr(value, key, item)


class ABITests(unittest.TestCase):
    def test_fixed_uapi_sizes_and_offsets(self):
        for kind, size in ((V.Capability, 104), (V.FormatDescription, 64), (V.PixelFormat, 48),
                           (V.FrameSize, 44), (V.RequestBuffers, 20), (V.QueryControl, 68),
                           (V.Control, 8), (V.StreamParameters, 204)):
            self.assertEqual(C.sizeof(kind), size)
        for pointer, stamp, size, timestamp, memory, offset, length, fmt_size in (
                (32, 32, 68, 20, 48, 52, 56, 204),
                (32, 64, 80, 24, 60, 64, 68, 204),
                (64, 64, 88, 24, 60, 64, 72, 208)):
            with self.subTest(pointer=pointer, stamp=stamp):
                abi = V.layouts(pointer, stamp)
                self.assertEqual(C.sizeof(abi.Buffer), size)
                self.assertEqual(C.sizeof(abi.Format), fmt_size)
                self.assertEqual(abi.Format.fmt.offset, 4 if pointer == 32 else 8)
                value = abi.Buffer(index=7, type=1, memory=1, offset=4096, length=8192)
                value.timestamp[:] = (123, 456)
                expected = bytearray(size)
                struct.pack_into('<II', expected, 0, 7, 1)
                struct.pack_into('<ii' if stamp == 32 else '<qq', expected, timestamp, 123, 456)
                struct.pack_into('<I', expected, memory, 1)
                struct.pack_into('<I' if pointer == 32 else '<Q', expected, offset, 4096)
                struct.pack_into('<I', expected, length, 8192)
                self.assertEqual(bytes(value), expected)
                self.assertEqual(V.requests(abi)['DQBUF'], {68: 0xc0445611, 80: 0xc0505611, 88: 0xc0585611}[size])
                self.assertEqual(V.requests(abi)['S_FMT'], 0xc0cc5605 if pointer == 32 else 0xc0d05605)
                self.assertEqual(V.requests(abi)['QUERYCAP'], 0x80685600)
                self.assertEqual(V.requests(abi)['STREAMON'], 0x40045612)
        # In particular armhf time64 is neither legacy armhf nor LP64.
        self.assertNotEqual(V.requests(V.layouts(32, 64))['DQBUF'], V.requests(V.layouts(64, 64))['DQBUF'])

    def test_ioctl_uses_mutable_bytes_and_copies_kernel_reply(self):
        io = V.LinuxVideoIO()
        def fake(fd, request, raw, mutate):
            self.assertEqual(fd, 123)
            self.assertEqual(request, 0xc008561b)
            self.assertTrue(mutate)
            self.assertEqual(bytes(raw), struct.pack('<Ii', V.GAIN, 0))
            struct.pack_into('<i', raw, 4, 12)
        value = V.Control(id=V.GAIN)
        with patch.object(V.fcntl, 'ioctl', fake):
            io.ioctl(123, 'G_CTRL', value)
        self.assertEqual(value.value, 12)


class UVCTests(unittest.TestCase):
    def camera(self, io=None):
        io = io or FakeVideo()
        camera = UVCCamera(device=DEVICE, pixel_um=(3.75, 3.75), io=io, clock=lambda: io.now)
        self.addCleanup(camera.close)
        return camera, io

    def test_inert_construction_probe_closes_and_negotiates_preference(self):
        for formats, bits in (((V.YUYV, V.Y16, V.GREY), 8), ((V.YUYV, V.Y16), 16), ((V.YUYV,), 8)):
            camera, io = self.camera(FakeVideo(formats))
            self.assertFalse(io.calls)
            info = camera.info()
            self.assertEqual(info['bits'], bits)
            self.assertEqual(info['pixel_um_x'], 3.75)
            self.assertFalse(io.fd_open)
            self.assertFalse(any(call[0] in ('REQBUFS', 'STREAMON') for call in io.calls))
            before = len(io.calls)
            self.assertEqual(camera.info(), info)
            self.assertEqual(len(io.calls), before)

    def test_stack_average_roi_manual_controls_and_byte_header(self):
        camera, io = self.camera(FakeVideo(stride=4))
        camera.open()
        camera.start_exposure(0.2, roi=dict(x=1, y=0, width=1, height=2), gain=3)
        io.frames = [bytes([10, 21, 99, 99, 30, 41, 99, 99]), bytes([20, 30, 99, 99, 40, 50, 99, 99])]
        self.assertEqual(camera.state(), 'exposing')
        self.assertEqual(camera.state(), 'download')
        self.assertTrue(all(m.closed for m in io.mappings))
        frame = camera.read_frame()
        self.assertEqual(frame.data, bytes([25, 45]))
        self.assertEqual(frame.element_type, 'Byte')
        self.assertEqual(frame.metadata['stacked_frames'], 2)
        self.assertAlmostEqual(frame.metadata['exposure_seconds'], 0.2)
        self.assertEqual(io.values, {V.EXPOSURE_AUTO: 1, V.EXPOSURE_ABSOLUTE: 1000, V.AUTOGAIN: 0, V.GAIN: 3})
        header = struct.unpack('<11I', next(image_bytes(frame, 1, 2)))
        self.assertEqual(header[5:7], (2, 6))
        self.assertEqual(camera.state(), 'idle')
        self.assertIsNone(camera._sum)

    def test_byte_json_and_binary_have_same_logical_type_and_pixels(self):
        import json
        camera, io = self.camera()
        camera.open()
        camera.start_exposure(.1)
        io.frames = [bytes([1, 2, 3, 4])]
        camera.state()
        frame = camera.read_frame()
        header, *chunks = image_bytes(frame, 1, 2)
        document = json.loads(b''.join(image_json(frame, 1, 2)))
        self.assertEqual(document['Type'], struct.unpack('<11I', header)[5])
        self.assertEqual(list(b''.join(chunks)), [value for row in document['Value'] for value in row])

    def test_format_fallback_and_compressed_only_refusal(self):
        camera, io = self.camera(FakeVideo((V.GREY, V.Y16, V.YUYV)))
        io.bad['reject_formats'] = [V.GREY]
        self.assertEqual(camera.info()['bits'], 16)
        camera, io = self.camera(FakeVideo((V.fourcc('MJPG'),)))
        with self.assertRaises(CameraError):
            camera.info()
        self.assertFalse(io.fd_open)

    def test_stepwise_sizes_and_legacy_enumeration_absence(self):
        for continuous in (None, 2, 3):
            camera, io = self.camera(FakeVideo(width=640, height=480))
            original = io.ioctl
            def sizes(fd, name, value):
                if name != 'ENUM_FRAMESIZES':
                    return original(fd, name, value)
                if continuous is None:
                    raise OSError(errno.ENOTTY, 'legacy')
                value.type = continuous
                value.sizes[:] = (320, 1280, 16, 240, 960, 16)
            io.ioctl = sizes
            self.assertEqual(camera.info()['width'], 640)

    def test_enumeration_flood_is_bounded(self):
        camera, io = self.camera(FakeVideo((V.GREY,) * 64))
        with self.assertRaises(CameraError):
            camera.info()
        self.assertEqual(io.counts['ENUM_FMT'], 64)
        self.assertFalse(io.fd_open)

    def test_late_mapping_failure_releases_earlier_mapping(self):
        camera, io = self.camera()
        original = io.map
        def mapping(fd, length, offset):
            if offset:
                raise OSError(errno.ENOMEM, 'fake')
            return original(fd, length, offset)
        io.map = mapping
        camera.open()
        with self.assertRaises(CameraError):
            camera.start_exposure(.1)
        self.assertEqual(len(io.mappings), 1)
        self.assertTrue(io.mappings[0].closed)

    def test_control_quantization_gain_step_and_total_deadline(self):
        camera, io = self.camera()
        io.controls[V.GAIN] = (1, 0, 10, 2)
        io.controls[V.EXPOSURE_ABSOLUTE] = (1, 100, 1000, 100)
        camera.open()
        with self.assertRaises(CameraError):
            camera.start_exposure(.055, gain=3)
        camera.start_exposure(.056, gain=2)
        self.assertEqual(io.values[V.EXPOSURE_ABSOLUTE], 600)
        camera.abort()
        camera.start_exposure(.3)
        io.frames = [bytes(4)] * 3
        camera._total_deadline = .05
        io.now = .05
        self.assertEqual(camera.state(), 'error')
        self.assertTrue(all(mapping.closed for mapping in io.mappings))

    def test_mmap_uses_returned_offset_length_and_shared_access(self):
        io = V.LinuxVideoIO()
        with patch.object(V.mmap, 'mmap') as mapped:
            io.map(123, 8192, 4096)
        mapped.assert_called_once_with(123, 8192, flags=V.mmap.MAP_SHARED,
                                      prot=V.mmap.PROT_READ | V.mmap.PROT_WRITE, offset=4096)

    def test_y16_little_endian_average_and_yuyv_luma(self):
        for pixel_format, raws, expected in (
                (V.Y16, [struct.pack('<4H', 1000, 65535, 2, 3), struct.pack('<4H', 2000, 65533, 4, 5)],
                 struct.pack('<4H', 1500, 65534, 3, 4)),
                (V.YUYV, [bytes([10, 200, 20, 201, 30, 202, 40, 203])] * 2, bytes([10, 20, 30, 40]))):
            camera, io = self.camera(FakeVideo((pixel_format,)))
            camera.open()
            camera.start_exposure(.2)
            io.frames = raws
            camera.state()
            self.assertEqual(camera.state(), 'download')
            self.assertEqual(camera.read_frame().data, expected)

    def test_no_controls_uses_reported_frame_period_and_never_invents_dark(self):
        camera, io = self.camera()
        io.controls.clear()
        camera.open()
        self.assertEqual(camera.info()['exposure_min'], .1)
        self.assertEqual(camera.info()['gain_max'], 0)
        for kwargs in ({'gain': 1}, {'light': 'false'}, {'bin': 2}):
            with self.assertRaises(CameraError):
                camera.start_exposure(.1, **kwargs)
        self.assertFalse(io.mappings)
        camera.start_exposure(.2)
        self.assertFalse(any(call[0] == 'S_CTRL' for call in io.calls))
        camera.abort()
        frames = []
        for light in (True, False):
            camera.start_exposure(.1, light=light)
            io.frames = [bytes([12, 23, 34, 45])]
            self.assertEqual(camera.state(), 'download')
            frame = camera.read_frame()
            self.assertTrue(frame.metadata['light'])
            frames.append(frame.data)
        self.assertEqual(frames, [bytes([12, 23, 34, 45])] * 2)

    def test_nonblocking_timeout_abort_close_and_reopen(self):
        camera, io = self.camera()
        camera.open()
        camera.start_exposure(.2)
        self.assertEqual(camera.state(), 'exposing')
        with self.assertRaisesRegex(CameraError, 'busy'):
            camera.start_exposure(.1)
        io.now = 2
        self.assertEqual(camera.state(), 'error')
        self.assertTrue(all(m.closed for m in io.mappings))
        self.assertIsNone(camera._sum)
        camera.start_exposure(.1)
        camera.abort()
        self.assertEqual(camera.state(), 'idle')
        camera.close()
        camera.close()
        self.assertFalse(io.fd_open)
        camera.open()
        self.assertTrue(io.fd_open)

    def test_invalid_capabilities_format_and_period_fail_closed(self):
        for change in ({'caps': 1}, {'format': {'pixelformat': V.fourcc('MJPG')}},
                       {'format': {'bytesperline': 1}}, {'format': {'sizeimage': 999999999}},
                       {'format': {'width': 99999}}, {'format': {'field': 4}}, {'period': (0, 10)}):
            camera, io = self.camera()
            io.bad = change
            with self.assertRaisesRegex(CameraError, 'camera_unsupported'):
                camera.open()
            self.assertFalse(io.fd_open)

    def test_open_rejects_changed_published_geometry(self):
        camera, io = self.camera()
        camera.info()
        io.width = 4
        with self.assertRaisesRegex(CameraError, 'camera_unsupported'):
            camera.open()
        self.assertFalse(io.fd_open)

    def test_bounds_and_failures_release_all_resources(self):
        for failure in ('REQBUFS', 'QUERYBUF', 'QBUF', 'map', 'STREAMON', 'S_CTRL', 'G_CTRL'):
            camera, io = self.camera()
            camera.open()
            io.fail = failure
            with self.assertRaises(CameraError):
                camera.start_exposure(.1)
            self.assertTrue(all(m.closed for m in io.mappings))
            self.assertIsNone(camera._sum)
            camera.close()
            self.assertFalse(io.fd_open)
        for buffers, length in ((0, 64), (5, 64), (2, V.MAX_BUFFER_BYTES + 1), (2, 1)):
            camera, io = self.camera()
            camera.open()
            io.buffers, io.buffer_length = buffers, length
            with self.assertRaises(CameraError):
                camera.start_exposure(.1)
            self.assertTrue(all(m.closed for m in io.mappings))
        camera, io = self.camera()
        camera.open()
        with self.assertRaises(CameraError):
            camera.start_exposure(MAX_STACK)
        self.assertFalse(io.mappings)

    def test_invalid_dequeue_and_disconnect_discard_partial_average(self):
        for bad in ({'index': 99}, {'bytesused': 65}, {'bytesused': 1}, {'flags': 0x40}, {'memory': 2}, {'type': 9}):
            camera, io = self.camera()
            camera.open()
            camera.start_exposure(.2)
            io.frames = [bytes(4)]
            io.bad['buffer'] = bad
            self.assertEqual(camera.state(), 'error')
            self.assertIsNone(camera._sum)
            self.assertTrue(all(m.closed for m in io.mappings))
        camera, io = self.camera()
        camera.open()
        camera.start_exposure(.1)
        io.fail = 'DQBUF'
        self.assertEqual(camera.state(), 'error')

    def test_streamoff_failure_closes_fd_before_unmapping(self):
        camera, io = self.camera()
        camera.open()
        camera.start_exposure(.1)
        io.fail = 'STREAMOFF'
        camera.abort()
        self.assertFalse(io.fd_open)
        self.assertTrue(all(m.closed for m in io.mappings))
        self.assertEqual(camera.state(), 'error')

    def test_late_ioctl_result_and_render_allocation_failure_are_discarded(self):
        camera, io = self.camera()
        camera.open()
        camera.start_exposure(.1)
        io.frames = [bytes(4)]
        original = io.ioctl
        def late(fd, name, value):
            original(fd, name, value)
            if name == 'DQBUF':
                io.now = 10
        io.ioctl = late
        self.assertEqual(camera.state(), 'error')
        self.assertIsNone(camera._sum)
        io.ioctl = original
        camera.start_exposure(.1)
        io.frames = [bytes(4)]
        self.assertEqual(camera.state(), 'download')
        with patch.object(camera, '_frame', side_effect=MemoryError), self.assertRaises(CameraError):
            camera.read_frame()
        self.assertIsNone(camera._sum)
        self.assertEqual(camera.state(), 'error')

    def test_luminance_rejects_short_rows_odd_yuyv_and_strips_padding(self):
        self.assertEqual(list(luminance(bytes([1, 2, 9, 3, 4, 9]), 2, 2, 3, V.GREY)), [1, 2, 3, 4])
        for args in ((b'\0', 2, 2, 2, V.GREY), (bytes(8), 3, 1, 6, V.YUYV),
                     (bytes(8), 2, 2, 1, V.GREY), (bytes(8), 2, 2, 4, 0)):
            with self.assertRaises(CameraError):
                luminance(*args)

    def test_service_factory_uses_real_driver_with_fake_io_and_truthful_discovery(self):
        io = FakeVideo()
        registry = CameraRegistry(list(load_registry().descriptors()),
                                  {'uvc': lambda **kw: UVCCamera(**kw, pixel_um=(3.75, 3.75), io=io, clock=lambda: io.now)})
        service = CameraService()
        self.addCleanup(service.close)
        service.reconcile_usb([DEVICE], registry)
        self.assertEqual(service.inventory()[0]['discovery']['state'], 'ready')
        self.assertFalse(io.fd_open)
        service.execute(0, 'PUT', 'connected', {'connected': 'true'}, 1, '127.0.0.1')
        self.assertEqual(service.execute(0, 'GET', 'pixelsizex', {}, 1, '127.0.0.1'), 3.75)
        service.execute(0, 'PUT', 'startexposure', {'duration': '.1', 'light': 'true'}, 1, '127.0.0.1')
        service.reconcile_usb([], registry)
        self.assertFalse(io.fd_open)
        self.assertTrue(all(m.closed for m in io.mappings))
        io.bad = {'caps': 0}
        service.reconcile_usb([DEVICE], registry)
        self.assertEqual(service.inventory()[0]['discovery']['state'], 'unsupported')
        self.assertIsNone(service.inventory()[0]['alpaca'])

    def test_unknown_physical_pitch_never_probes_or_advertises_ready(self):
        io = FakeVideo()
        camera = UVCCamera(device=DEVICE, io=io)
        with self.assertRaisesRegex(CameraError, 'camera_unsupported'):
            camera.info()
        self.assertFalse(io.calls)
        for pitch in ((0, 1), (True, 1), (1, float('nan')), (1,), '3.75'):
            with self.assertRaises(CameraError):
                UVCCamera(device=DEVICE, pixel_um=pitch, io=io)
        service = CameraService()
        self.addCleanup(service.close)
        service.reconcile_usb([DEVICE], load_registry())
        self.assertEqual(service.inventory()[0]['discovery']['state'], 'unsupported')
        self.assertEqual(service.configured_devices(), [])


class NodeResolutionTests(unittest.TestCase):
    def test_only_descendant_nodes_with_matching_ids_and_bounded_dev_attribute(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            usb, video = root / 'usb', root / 'video'
            usb.mkdir()
            video.mkdir()
            parent = usb / DEVICE['port_path']
            parent.mkdir()
            (parent / 'idVendor').write_text('1234\n')
            (parent / 'idProduct').write_text('5678\n')
            node = parent / 'interface' / 'video0'
            node.mkdir(parents=True)
            (node / 'dev').write_text('81:0\n')
            (video / 'video0').symlink_to(node)
            unrelated = root / 'other'
            unrelated.mkdir()
            (video / 'video1').symlink_to(unrelated)
            io = V.LinuxVideoIO(sysfs=video, usb_root=usb, dev_root=root / 'dev')
            with patch.object(V.sys, 'platform', 'linux'), patch.object(V.platform, 'machine', return_value='armv7l'):
                self.assertEqual(io.nodes(DEVICE), [(root / 'dev' / 'video0', os.makedev(81, 0))])
                (parent / 'idProduct').write_text('0001\n')
                with self.assertRaises(CameraError):
                    io.nodes(DEVICE)

    def test_fd_is_nonblocking_nofollow_and_character_identity_checked(self):
        io = V.LinuxVideoIO()
        with patch.object(V.os, 'open', return_value=123) as opened, \
             patch.object(V.os, 'fstat') as info, patch.object(V.os, 'close') as closed:
            info.return_value.st_mode = 0o100600  # Regular file: never ioctl it.
            with self.assertRaises(CameraError):
                io.open(('/fake/video0', 123))
            flags = opened.call_args.args[1]
            self.assertTrue(flags & os.O_NONBLOCK)
            self.assertTrue(flags & os.O_NOFOLLOW)
            closed.assert_called_once_with(123)
