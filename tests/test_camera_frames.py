"""Image ordering and bounded frame representation, independent of HTTP/USB."""
from array import array
from dataclasses import FrozenInstanceError
import unittest

from wifi_bridge.cameras.frames import Frame, column_major, benchmark_transpose


class FrameTests(unittest.TestCase):
    def test_asymmetric_uint16_image_has_distinct_corners_and_height_fastest(self):
        # Row-major 3x2: corners are 1, 513, 1024, 65535; values cross byte bounds.
        data = b'\x01\x00\x02\x01\x01\x02\x00\x04\xff\x0f\xff\xff'
        frame = Frame(3, 2, 'UInt16', data, {})
        self.assertEqual(column_major(frame), array('H', [1, 1024, 258, 4095, 513, 65535]))
        self.assertEqual(frame.data, data)
        for x in range(3):
            for y in range(2):
                self.assertEqual(column_major(frame)[x * 2 + y], int.from_bytes(data[(y * 3 + x) * 2:(y * 3 + x + 1) * 2], 'little'))

    def test_byte_and_single_axis_images(self):
        self.assertEqual(list(column_major(Frame(2, 3, 'Byte', bytes([1, 2, 3, 4, 5, 6]), {}))), [1, 3, 5, 2, 4, 6])
        for width, height in ((1, 3), (3, 1)):
            self.assertEqual(list(column_major(Frame(width, height, 'Byte', b'\x01\x02\x03', {}))), [1, 2, 3])

    def test_rejects_unbounded_or_inconsistent_frames_and_private_metadata(self):
        for args in ((0, 1, 'Byte', b'', {}), (True, 1, 'Byte', b'x', {}),
                     (16384, 16384, 'Byte', b'', {}), (1, 1, 'float', b'x', {}),
                     (1, 1, 'UInt16', b'x', {}), (1, 1, 'Byte', bytearray(b'x'), {}),
                     (1, 1, 'Byte', b'x', {'raw': 'forbidden'}),
                     (1, 1, 'Byte', b'x', {'gain': float('nan')}),
                     (1, 1, 'Byte', b'x', {'gain': []}),
                     (1, 1, 'Byte', b'x', {'seed': 1 << 64})):
            with self.subTest(args=args[:3]), self.assertRaises(ValueError):
                Frame(*args)

    def test_metadata_is_detached_and_immutable(self):
        metadata = {'gain': 1}
        frame = Frame(1, 1, 'Byte', b'x', metadata)
        metadata['gain'] = 2
        self.assertEqual(frame.metadata['gain'], 1)
        with self.assertRaises(TypeError):
            frame.metadata['gain'] = 3
        with self.assertRaises(FrozenInstanceError):
            frame.width = 2

    def test_benchmark_reports_real_bounded_measurements(self):
        result = benchmark_transpose(1)
        self.assertEqual((result['width'], result['height'], result['iterations']), (1280, 960, 1))
        self.assertGreater(result['median_ms'], 0)
        self.assertEqual(result['min_ms'], result['max_ms'])
        for invalid in (0, 21, True, 1.5):
            with self.assertRaises(ValueError):
                benchmark_transpose(invalid)
