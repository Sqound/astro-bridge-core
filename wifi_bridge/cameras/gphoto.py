"""Private gPhoto INDI stdio adapter. No network listener or implicit discovery.

The launcher must confine the child to the previously validated USB node. Sensor
profiles are owner-supplied and frames are withheld unless their FITS metadata
matches the profile. All callbacks run on the camera service worker.
"""
import base64
import binascii
import math
import os
import select
import time
from xml.parsers import expat
from xml.sax.saxutils import escape, quoteattr

from .driver import CameraDriver, CameraError, validate_info, validate_exposure
from .frames import Frame, MAX_PIXELS

MAX_XML = ((MAX_PIXELS * 2 + 2880 * 16 + 2) // 3) * 4 + 1024 * 1024
MAX_HEADER = 2880 * 16


def fits_frame(data, profile):
    """Accept only a bounded primary 2D, unsigned byte or unsigned 16-bit FITS."""
    if not isinstance(data, bytes) or len(data) > MAX_PIXELS * 2 + MAX_HEADER + 2880:
        raise CameraError('capture_failed')
    values, end = {}, None
    for offset in range(0, min(len(data), MAX_HEADER), 80):
        card = data[offset:offset + 80]
        if len(card) != 80:
            break
        try:
            text = card.decode('ascii')
        except UnicodeError:
            raise CameraError('capture_failed') from None
        key = text[:8].strip()
        if key == 'END':
            end = ((offset + 80 + 2879) // 2880) * 2880
            break
        if text[8:10] == '= ':
            if key in values:
                raise CameraError('capture_failed')
            values[key] = text[10:].split('/', 1)[0].strip().strip("'").strip()
    if end is None:
        raise CameraError('capture_failed')
    try:
        width, height = int(values['NAXIS1']), int(values['NAXIS2'])
        bits = int(values['BITPIX'])
        scale = float(values.get('BSCALE', '1'))
        zero = float(values.get('BZERO', '0'))
        if (values['SIMPLE'] != 'T' or int(values['NAXIS']) != 2 or
                (width, height) != (profile['width'], profile['height']) or
                width * height > MAX_PIXELS or bits != profile['bits'] or
                bits not in (8, 16) or scale != 1 or zero != (32768 if bits == 16 else 0)):
            raise ValueError
        pattern = values.get('BAYERPAT', '').upper()
        if (pattern != profile.get('bayer_pattern', '') or
                (pattern and (int(values.get('XBAYROFF', '0')) != 0 or int(values.get('YBAYROFF', '0')) != 0))):
            raise ValueError
        size = width * height * (bits // 8)
        if end + size > len(data) or len(data) != ((end + size + 2879) // 2880) * 2880:
            raise ValueError
        pixels = bytearray(data[end:end + size])
        if bits == 16:
            # FITS stores signed big-endian with BZERO=32768. Convert to
            # unsigned little-endian without allocating a Python int array.
            hi = pixels[::2].translate(bytes.maketrans(bytes(range(256)), bytes(i ^ 0x80 for i in range(256))))
            lo = pixels[1::2]
            pixels[::2], pixels[1::2] = lo, hi
        return Frame(width, height, 'UInt16' if bits == 16 else 'Byte', bytes(pixels), {})
    except (KeyError, ValueError, OverflowError):
        raise CameraError('capture_failed') from None


class _Xml:
    """Incremental, bounded stream parser. Reject DTDs and nested vectors."""
    def __init__(self):
        self.depth = 0
        self.vector = self.element = None
        self.text_size = 0
        self.events = []
        self.unfinished_bytes = 0
        self.parser = expat.ParserCreate()
        self.parser.StartElementHandler = self.start
        self.parser.EndElementHandler = self.end
        self.parser.CharacterDataHandler = self.text
        self.parser.StartDoctypeDeclHandler = self.reject
        self.parser.EntityDeclHandler = self.reject
        self.parser.ExternalEntityRefHandler = self.reject
        self.parser.Parse(b'<stream>', False)
        self.depth = 0
        self.vector = None

    def reject(self, *args):
        raise ValueError('invalid_xml')

    def start(self, tag, attrs):
        self.depth += 1
        if self.depth == 1:
            self.vector = [tag, dict(attrs), {}]
            self.text_size = 0
        elif self.depth == 2:
            self.element = [dict(attrs), []]
        else:
            raise ValueError('invalid_xml')

    def text(self, text):
        if self.depth == 2:
            self.text_size += len(text)
            if self.text_size > MAX_XML:
                raise ValueError('oversized_xml')
            self.element[1].append(text)
        elif text.strip():
            raise ValueError('invalid_xml')

    def end(self, tag):
        if self.depth == 2:
            attrs, chunks = self.element
            name = attrs.get('name')
            if not name or len(name) > 128 or len(self.vector[2]) >= 256 or name in self.vector[2]:
                raise ValueError('invalid_xml')
            self.vector[2][name] = (attrs, ''.join(chunks))
            self.element = None
        elif self.depth == 1:
            self.events.append(self.vector)
            if len(self.events) > 256:
                raise ValueError('too_many_events')
            self.vector = None
        else:
            raise ValueError('invalid_xml')
        self.depth -= 1

    def feed(self, raw):
        self.unfinished_bytes += len(raw)
        if self.unfinished_bytes > MAX_XML:
            raise ValueError('oversized_xml')
        self.parser.Parse(raw, False)
        events, self.events = self.events, []
        if events and self.depth == 0:
            self.unfinished_bytes = 0
        return events


def _write_raw(proc, raw):
    if len(raw) > 16384:
        raise ValueError('oversized_command')
    fd = proc.stdin.fileno()
    view = memoryview(raw)
    deadline = time.monotonic() + .1
    while view:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
            raise TimeoutError('driver_write_timeout')
        try:
            count = os.write(fd, view)
        except BlockingIOError:
            continue
        if count <= 0:
            raise OSError('driver_write_failed')
        view = view[count:]


def _write(proc, tag, device, name, elements):
    attrs = ' device=' + quoteattr(device) + ' name=' + quoteattr(name)
    item_tag = {'newSwitchVector': 'oneSwitch', 'newNumberVector': 'oneNumber'}[tag]
    body = ''.join('<' + item_tag + ' name=' + quoteattr(key) + '>' + escape(str(value)) +
                   '</' + item_tag + '>' for key, value in elements.items())
    _write_raw(proc, ('<' + tag + attrs + '>' + body + '</' + tag + '>\n').encode())

class GPhotoCamera(CameraDriver):
    def __init__(self, *, profile, launch, clock=time.monotonic):
        self.profile, self.launch, self.clock = dict(profile), launch, clock
        pattern = self.profile.get('bayer_pattern', '')
        if pattern not in ('', 'RGGB', 'BGGR', 'GRBG', 'GBRG'):
            raise CameraError('camera_unsupported')
        self.description = validate_info(dict(
            name=profile['name'], sensor_name=profile['name'], width=profile['width'],
            height=profile['height'], pixel_um_x=profile['pixel_um_x'],
            pixel_um_y=profile['pixel_um_y'], bits=profile['bits'],
            max_adu=(1 << profile['bits']) - 1, sensor_type=2 if pattern else 0,
            bayer_offset={'': [0, 0], 'RGGB': [0, 0], 'BGGR': [1, 1],
                          'GRBG': [1, 0], 'GBRG': [0, 1]}[pattern],
            max_bin=1, gain_min=0, gain_max=0, exposure_min=profile['exposure_min'],
            exposure_max=profile['exposure_max'], can_abort=True, cooled=False))
        # Pinned gPhoto converts seconds to uint32 microseconds.
        if profile['bits'] not in (8, 16) or profile['exposure_max'] > 4294:
            raise CameraError('camera_unsupported')
        self.process = None
        self._state = 'idle'
        self._frame = None

    def info(self):
        return dict(self.description)

    def is_ready(self):
        return self._state in ('idle', 'exposing', 'reading', 'download') and self.process is not None

    def handoff_ready(self):
        return self.process is None or self.process.poll() is not None

    def _stop(self):
        proc = self.process
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=.1)
        except TimeoutError:
            return
        except Exception:
            return
        self.process = None

    def open(self):
        if self.process is not None:
            if self.process.poll() is None:
                if self._state == 'error':
                    raise CameraError('busy')
                return
            self._stop()
            if self.process is not None:
                raise CameraError('busy')
        try:
            self.process = self.launch()
            os.set_blocking(self.process.stdout.fileno(), False)
            os.set_blocking(self.process.stdin.fileno(), False)
            self.xml = _Xml()
            self.device = None
            self.definitions = {}
            self.pending = {}
            self.connected = False
            self.busy_seen = False
            self.configured = False
            self._frame = None
            self._state = 'waiting'
            self.deadline = self.clock() + 30
            _write_raw(self.process, b'<getProperties version="1.7"/>\n')
        except Exception:
            self._stop()
            self._state = 'error'
            raise CameraError('camera_driver_unavailable') from None

    def close(self):
        self._stop()
        self._frame = None
        self._state = 'idle' if self.handoff_ready() else 'error'

    def _send(self, tag, name, elements):
        _write(self.process, tag, self.device, name, elements)
        self.pending[name] = dict(elements)

    def _configure(self):
        required = {'CCD_INFO', 'CCD_CAPTURE_FORMAT', 'CCD_TRANSFER_FORMAT',
                    'CCD_COMPRESSION', 'UPLOAD_MODE', 'CCD_EXPOSURE'}
        if not self.connected or not required.issubset(self.definitions) or self.pending:
            return
        capture = self.definitions['CCD_CAPTURE_FORMAT']
        raw = [name for name, (attrs, _) in capture.items()
               if 'raw' in attrs.get('label', '').lower()
               and '+' not in attrs.get('label', '')
               and not any(x in attrs.get('label', '').lower() for x in ('sraw', 'mraw'))]
        if len(raw) != 1:
            raise ValueError('ambiguous_raw_format')
        for name, selected in [('CCD_CAPTURE_FORMAT', raw[0]),
                               ('CCD_TRANSFER_FORMAT', 'FORMAT_FITS'),
                               ('CCD_COMPRESSION', 'INDI_DISABLED'),
                               ('UPLOAD_MODE', 'UPLOAD_CLIENT')]:
            options = self.definitions[name]
            if selected not in options:
                raise ValueError('missing_property')
            self._send('newSwitchVector', name, {key: 'On' if key == selected else 'Off' for key in options})
        p = self.profile
        self._send('newNumberVector', 'CCD_INFO', dict(CCD_MAX_X=p['width'], CCD_MAX_Y=p['height'],
                   CCD_PIXEL_SIZE=p['pixel_um_x'], CCD_PIXEL_SIZE_X=p['pixel_um_x'],
                   CCD_PIXEL_SIZE_Y=p['pixel_um_y']))
        _write_raw(self.process, ('<enableBLOB device=' + quoteattr(self.device) + ' name="CCD1">Also</enableBLOB>\n').encode())
        self.configured = True

    def _event(self, event):
        tag, attrs, elements = event
        device, name, state = attrs.get('device'), attrs.get('name'), attrs.get('state')
        if name == 'CONNECTION' and tag == 'defSwitchVector' and self.device is None:
            if not device or len(device) > 128 or 'CONNECT' not in elements:
                raise ValueError('invalid_connection')
            self.device = device
            self._send('newSwitchVector', 'CONNECTION', {'CONNECT': 'On', 'DISCONNECT': 'Off'})
            return
        if device != self.device:
            return
        if tag.startswith('def') and name:
            self.definitions[name] = elements
        if name in self.pending and tag.startswith('set'):
            if state == 'Alert':
                raise ValueError('indi_alert')
            expected = self.pending[name]
            def matches(k, v):
                if k not in elements:
                    return False
                got = elements[k][1].strip()
                if isinstance(v, (int, float)):
                    try:
                        return math.isfinite(float(got)) and float(got) == float(v)
                    except ValueError:
                        return False
                return got == str(v)
            if state == 'Ok' and all(matches(k, v) for k, v in expected.items()):
                del self.pending[name]
                if name == 'CONNECTION':
                    self.connected = True
        if name == 'CCD_EXPOSURE' and tag == 'setNumberVector':
            if state == 'Alert':
                raise ValueError('exposure_failed')
            if state == 'Busy' and self._state == 'exposing':
                self.busy_seen = True
            if state == 'Ok' and self._state == 'reading' and self._frame is not None:
                self._state = 'download'
                self.deadline = math.inf
        if name == 'CCD1' and tag == 'setBLOBVector':
            if self._state != 'exposing':
                raise ValueError('unexpected_blob')
            if not self.busy_seen or state == 'Alert' or set(elements) != {'CCD1'}:
                raise ValueError('unexpected_blob')
            meta, encoded = elements['CCD1']
            if meta.get('format') != '.fits':
                raise ValueError('invalid_blob')
            raw = base64.b64decode(''.join(encoded.split()), validate=True)
            if int(meta.get('size', '-1')) != len(raw):
                raise ValueError('invalid_blob_size')
            self._frame = fits_frame(raw, self.profile)
            self._state = 'reading'

    def state(self):
        if self.process is None:
            return self._state
        try:
            if self.process.poll() is not None or self.clock() > self.deadline:
                raise ValueError('driver_timeout')
            for _ in range(64):
                ready, _, _ = select.select([self.process.stdout], [], [], 0)
                if not ready:
                    break
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise ValueError('driver_eof')
                for event in self.xml.feed(chunk):
                    self._event(event)
            if self.connected and not getattr(self, 'configured', False):
                self._configure()
            if self._state == 'waiting' and getattr(self, 'configured', False) and not self.pending:
                self._state = 'idle'
                self.deadline = math.inf
        except (OSError, ValueError, TypeError, KeyError, expat.ExpatError, binascii.Error, CameraError):
            self._stop()
            self._frame = None
            self._state = 'error'
        return self._state

    def start_exposure(self, seconds, bin, roi, gain, light):
        self.state()
        if self._state != 'idle' and self._state != 'download':
            raise CameraError('busy')
        values = validate_exposure(self.description, seconds, bin, roi, gain, light)
        if values['roi'] != dict(x=0, y=0, width=self.profile['width'], height=self.profile['height']) or not light:
            raise CameraError('invalid_exposure')
        if self.xml.depth or select.select([self.process.stdout], [], [], 0)[0]:
            raise CameraError('busy')
        self._frame = None
        self.busy_seen = False
        self._state = 'exposing'
        self.deadline = self.clock() + seconds + 120
        try:
            _write(self.process, 'newNumberVector', self.device, 'CCD_EXPOSURE', {'CCD_EXPOSURE_VALUE': seconds})
        except (OSError, ValueError):
            self._stop()
            self._state = 'error'
            raise CameraError('capture_failed') from None

    def abort(self):
        self._stop()
        self._frame = None
        self._state = 'error'

    def read_frame(self):
        if self.state() != 'download' or self._frame is None:
            raise CameraError('not_ready')
        frame, self._frame = self._frame, None
        self._state = 'idle'
        self.deadline = math.inf
        return frame
