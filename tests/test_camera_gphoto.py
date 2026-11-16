import base64
import io
import os
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from xml.parsers.expat import ExpatError

from wifi_bridge.cameras.driver import CameraError
from wifi_bridge.cameras.gphoto import _Xml, fits_frame, GPhotoCamera

PROFILE = dict(name='Test camera', width=2, height=2, pixel_um_x=4.3, pixel_um_y=4.3,
               bits=16, exposure_min=.001, exposure_max=60, bayer_pattern='RGGB')


def fits(bits=16, pattern='RGGB'):
    cards = ["SIMPLE  =                    T", f"BITPIX  = {bits:20}",
             "NAXIS   =                    2", "NAXIS1  =                    2",
             "NAXIS2  =                    2", "BSCALE  =                    1",
             f"BZERO   = {32768 if bits == 16 else 0:20}", f"BAYERPAT= '{pattern}'", 'END']
    header = ''.join(x.ljust(80) for x in cards).encode().ljust(2880, b' ')
    pixels = bytes.fromhex('800080017fff0000') if bits == 16 else bytes([0, 1, 127, 255])
    return header + pixels.ljust(2880, b'\0')


class GPhotoTests(unittest.TestCase):
    def test_fits_unsigned_pixels_and_shape(self):
        frame = fits_frame(fits(), PROFILE)
        self.assertEqual((frame.width, frame.height, frame.element_type), (2, 2, 'UInt16'))
        self.assertEqual(frame.data, bytes.fromhex('00000100ffff0080'))
        profile = dict(PROFILE, bits=8)
        self.assertEqual(fits_frame(fits(8), profile).data, bytes([0, 1, 127, 255]))

    def test_rejects_mismatched_shape_depth_and_cfa_and_truncation(self):
        for data, profile in [(fits(), dict(PROFILE, width=3)), (fits(), dict(PROFILE, bits=8)),
                              (fits(pattern='BGGR'), PROFILE), (fits()[:-1], PROFILE)]:
            with self.subTest(profile=profile), self.assertRaises(CameraError):
                fits_frame(data, profile)

    def test_xml_fragmentation_and_dtd(self):
        parser = _Xml()
        self.assertEqual(parser.feed(b'<setNumberVector device="GPhoto CCD" name="CCD_EXPOSURE" state="Busy"><oneNumber name="CCD_EXPOSURE_VALUE">'), [])
        event = parser.feed(b'1</oneNumber></setNumberVector>')
        self.assertEqual(event[0][2]['CCD_EXPOSURE_VALUE'][1], '1')
        with self.assertRaises((ValueError, ExpatError)):
            _Xml().feed(b'<!DOCTYPE foo [<!ENTITY x "bad">]><foo>&x;</foo>')

    def test_unexpected_blob_and_roi_fail_closed(self):
        camera = GPhotoCamera(profile=PROFILE, launch=lambda: None)
        camera.pending = {}
        camera.device = 'GPhoto CCD'
        camera._state = 'exposing'
        camera.busy_seen = False
        blob = base64.b64encode(fits()).decode()
        with self.assertRaises(ValueError):
            camera._event(('setBLOBVector', {'device': camera.device, 'name': 'CCD1', 'state': 'Ok'},
                           {'CCD1': ({'format': '.fits', 'size': str(len(fits()))}, blob)}))
        camera._state = 'idle'
        with patch.object(camera, 'state', return_value='idle'):
            with self.assertRaises(CameraError):
                camera.start_exposure(1, 1, dict(x=0, y=0, width=1, height=1), 0, True)

    def test_stale_blob_and_oversized_xml_fail_closed(self):
        camera = GPhotoCamera(profile=PROFILE, launch=lambda: None)
        camera.device, camera.pending, camera._state = 'GPhoto CCD', {}, 'idle'
        with self.assertRaises(ValueError):
            camera._event(('setBLOBVector', {'device': camera.device, 'name': 'CCD1'}, {}))
        with patch('wifi_bridge.cameras.gphoto.MAX_XML', 50):
            parser = _Xml()
            parser.feed(b'<setBLOBVector name="CCD1"><oneBLOB name="CCD1">')
            with self.assertRaises(ValueError):
                parser.feed(b'A' * 51)

    def test_rejects_bad_fits_scaling_and_duplicate_keywords(self):
        data = fits().replace(b'BZERO   =                32768', b'BZERO   =                    0', 1)
        with self.assertRaises(CameraError):
            fits_frame(data, PROFILE)
        data = fits().replace(b'END'.ljust(80), b'NAXIS1  =                    2'.ljust(80) + b'END'.ljust(80), 1)
        with self.assertRaises(CameraError):
            fits_frame(data, PROFILE)

    def test_blocked_driver_input_is_bounded(self):
        from wifi_bridge.cameras.gphoto import _write_raw
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        try:
            while True:
                try:
                    os.write(write_fd, b'x' * 4096)
                except BlockingIOError:
                    break
            with os.fdopen(write_fd, 'wb', buffering=0) as stream:
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    _write_raw(SimpleNamespace(stdin=stream), b'command')
                self.assertLess(time.monotonic() - started, .5)
        finally:
            os.close(read_fd)

    def test_stdio_handshake_exposure_and_reap(self):
        script = r'''
import base64, sys, xml.etree.ElementTree as E
image = base64.b64decode(sys.argv[1])
def out(s):
 print(s, flush=True)
def vec(kind, name, body, state='Idle'):
 out(f'<{kind} device="GPhoto CCD" name="{name}" state="{state}">{body}</{kind}>')
def switch(name, options):
 body = ''.join(f'<defSwitch name="{key}" label="{label}">Off</defSwitch>' for key, label in options)
 vec('defSwitchVector', name, body)
for line in sys.stdin:
 e = E.fromstring(line)
 if e.tag == 'getProperties':
  switch('CONNECTION', [('CONNECT','Connect'),('DISCONNECT','Disconnect')])
 elif e.tag == 'newSwitchVector' and e.attrib['name'] == 'CONNECTION':
  vec('setSwitchVector', 'CONNECTION', '<oneSwitch name="CONNECT">On</oneSwitch><oneSwitch name="DISCONNECT">Off</oneSwitch>', 'Ok')
  switch('CCD_CAPTURE_FORMAT', [('FORMAT_1','RAW')])
  switch('CCD_TRANSFER_FORMAT', [('FORMAT_FITS','FITS'),('FORMAT_NATIVE','Native')])
  switch('CCD_COMPRESSION', [('INDI_DISABLED','Disabled'),('INDI_ENABLED','Enabled')])
  switch('UPLOAD_MODE', [('UPLOAD_CLIENT','Client'),('UPLOAD_LOCAL','Local')])
  vec('defNumberVector','CCD_INFO','')
  vec('defNumberVector','CCD_EXPOSURE','')
 elif e.tag in ('newSwitchVector','newNumberVector') and e.attrib['name'] != 'CCD_EXPOSURE':
  kind = 'setSwitchVector' if e.tag == 'newSwitchVector' else 'setNumberVector'
  vec(kind, e.attrib['name'], ''.join(E.tostring(c, encoding='unicode') for c in e), 'Ok')
 elif e.tag == 'newNumberVector' and e.attrib['name'] == 'CCD_EXPOSURE':
  vec('setNumberVector','CCD_EXPOSURE','<oneNumber name="CCD_EXPOSURE_VALUE">1</oneNumber>', 'Busy')
  vec('setBLOBVector','CCD1', f'<oneBLOB name="CCD1" format=".fits" size="{len(image)}">{base64.b64encode(image).decode()}</oneBLOB>', 'Ok')
  vec('setNumberVector','CCD_EXPOSURE','<oneNumber name="CCD_EXPOSURE_VALUE">0</oneNumber>', 'Ok')
'''
        camera = GPhotoCamera(profile=PROFILE, launch=lambda: subprocess.Popen(
            [sys.executable, '-u', '-c', script, base64.b64encode(fits()).decode()],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL))
        def wait_for(target):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                state = camera.state()
                if state == target:
                    return
                if state == 'error':
                    self.fail('driver entered error')
                time.sleep(.01)
            self.fail('driver timed out: ' + state)
        try:
            camera.open()
            wait_for('idle')
            camera.start_exposure(1, 1, None, 0, True)
            wait_for('download')
            self.assertEqual(camera.read_frame().data, bytes.fromhex('00000100ffff0080'))
        finally:
            camera.close()
        self.assertTrue(camera.handoff_ready())

    def test_realistic_vector_exchange_to_alpaca_frame(self):
        from wifi_bridge.cameras.alpaca import Camera
        camera = GPhotoCamera(profile=PROFILE, launch=lambda: None)
        camera.process = SimpleNamespace(stdin=io.BytesIO())
        write_patch = patch('wifi_bridge.cameras.gphoto._write_raw')
        write_patch.start()
        self.addCleanup(write_patch.stop)
        camera.device = None
        camera.pending = {}
        camera.definitions = {}
        camera.connected = False
        camera.configured = False
        camera._state = 'waiting'
        camera._event(('defSwitchVector', {'device': 'GPhoto CCD', 'name': 'CONNECTION'},
                       {'CONNECT': ({}, 'Off'), 'DISCONNECT': ({}, 'On')}))
        camera._event(('setSwitchVector', {'device': 'GPhoto CCD', 'name': 'CONNECTION', 'state': 'Ok'},
                       {'CONNECT': ({}, 'On'), 'DISCONNECT': ({}, 'Off')}))
        for name, options in {
            'CCD_CAPTURE_FORMAT': {'FORMAT_1': ({'label': 'RAW'}, 'On')},
            'CCD_TRANSFER_FORMAT': {'FORMAT_FITS': ({}, 'On'), 'FORMAT_NATIVE': ({}, 'Off')},
            'CCD_COMPRESSION': {'INDI_ENABLED': ({}, 'Off'), 'INDI_DISABLED': ({}, 'On')},
            'UPLOAD_MODE': {'UPLOAD_CLIENT': ({}, 'On'), 'UPLOAD_LOCAL': ({}, 'Off')},
            'CCD_INFO': {}, 'CCD_EXPOSURE': {},
        }.items():
            camera._event(('defSwitchVector', {'device': 'GPhoto CCD', 'name': name}, options))
        camera._configure()
        for name, expected in list(camera.pending.items()):
            camera._event(('setNumberVector' if name == 'CCD_INFO' else 'setSwitchVector',
                           {'device': camera.device, 'name': name, 'state': 'Ok'},
                           {key: ({}, str(value)) for key, value in expected.items()}))
        self.assertFalse(camera.pending)
        camera._state = 'exposing'
        camera.busy_seen = False
        camera._event(('setNumberVector', {'device': camera.device, 'name': 'CCD_EXPOSURE', 'state': 'Busy'}, {}))
        data = fits()
        camera._event(('setBLOBVector', {'device': camera.device, 'name': 'CCD1', 'state': 'Ok'},
                       {'CCD1': ({'format': '.fits', 'size': str(len(data))}, base64.b64encode(data).decode())}))
        self.assertEqual(camera._state, 'reading')
        camera._event(('setNumberVector', {'device': camera.device, 'name': 'CCD_EXPOSURE', 'state': 'Ok'}, {}))
        self.assertEqual(camera._state, 'download')
        facade = Camera('test', 'gphoto', camera, time.monotonic)
        facade.connected = True
        with patch.object(camera, 'state', side_effect=lambda: camera._state):
            result = facade.call('GET', 'imagearray', {}, 1, '127.0.0.1')
        self.assertEqual(result.data, bytes.fromhex('00000100ffff0080'))

if __name__ == '__main__':
    unittest.main()
