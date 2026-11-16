"""Alpaca Camera V3 protocol for attached-camera drivers, without network I/O."""
from datetime import datetime, timezone
import json
import math
import re
import struct
import uuid

from .driver import CameraError, validate_info

# Every Camera V3 and pre-Platform-7 common member in the official Device API.
READ = frozenset('bayeroffsetx bayeroffsety binx biny camerastate cameraxsize cameraysize canabortexposure canasymmetricbin canfastreadout cangetcoolerpower canpulseguide cansetccdtemperature canstopexposure ccdtemperature cooleron coolerpower electronsperadu exposuremax exposuremin exposureresolution fastreadout fullwellcapacity gain gainmax gainmin gains hasshutter heatsinktemperature imagearray imagearrayvariant imageready ispulseguiding lastexposureduration lastexposurestarttime maxadu maxbinx maxbiny numx numy offset offsetmax offsetmin offsets percentcompleted pixelsizex pixelsizey readoutmode readoutmodes sensorname sensortype setccdtemperature startx starty subexposureduration connected description driverinfo driverversion interfaceversion name supportedactions'.split())
WRITE = frozenset('binx biny cooleron fastreadout gain numx numy offset readoutmode setccdtemperature startx starty subexposureduration abortexposure pulseguide startexposure stopexposure connected action commandblind commandbool commandstring'.split())
MESSAGES = {
    0x400: 'This camera does not support this feature.',
    0x401: 'Invalid camera parameter.',
    0x402: 'No completed exposure is available.',
    0x407: 'Connect the camera first.',
    0x40B: 'camera in use by another client',
    0x500: 'Use ImageBytes for images larger than 1 Mpx.',
    0x501: 'Camera service is unavailable.',
}


class AlpacaError(Exception):
    def __init__(self, number):
        self.number = number
        super().__init__(MESSAGES[number])


def number(value, minimum=0, maximum=4294967295):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,10}', value):
        raise AlpacaError(0x401)
    result = int(value)
    if not minimum <= result <= maximum:
        raise AlpacaError(0x401)
    return result


def boolean(value):
    if not isinstance(value, str) or value.lower() not in ('true', 'false'):
        raise AlpacaError(0x401)
    return value.lower() == 'true'


def duration(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise AlpacaError(0x401) from None
    if not math.isfinite(result) or result < 0:
        raise AlpacaError(0x401)
    return result


def envelope(client, server, value=None, error=0, include_value=True):
    result = {'ClientTransactionID': client, 'ServerTransactionID': server,
              'ErrorNumber': error, 'ErrorMessage': MESSAGES.get(error, '')}
    if include_value and not error:
        result['Value'] = value
    return result


def image_bytes(frame, client, server):
    """44-byte header then bounded column-major chunks; no converted frame copy."""
    transmission = 8 if frame.element_type == 'UInt16' else 6
    yield struct.pack('<11I', 1, 0, client, server, 44, 2, transmission, 2,
                      frame.width, frame.height, 0)
    size = 2 if frame.element_type == 'UInt16' else 1
    source = memoryview(frame.data)
    chunk = bytearray()
    for x in range(frame.width):
        for y in range(frame.height):
            offset = (y * frame.width + x) * size
            chunk.extend(source[offset:offset + size])
            if len(chunk) >= 16384:
                yield bytes(chunk)
                chunk.clear()
    if chunk:
        yield bytes(chunk)


def image_json(frame, client, server):
    """Incremental JSON with the same x,y ordering as ImageBytes."""
    if frame.width * frame.height > 1_000_000:
        raise AlpacaError(0x500)
    prefix = envelope(client, server, include_value=False)
    prefix.update(Type=2, Rank=2)
    yield json.dumps(prefix, separators=(',', ':')).encode()[:-1] + b',"Value":['
    size = 2 if frame.element_type == 'UInt16' else 1
    for x in range(frame.width):
        yield b'[' if x == 0 else b',['
        chunk = bytearray()
        for y in range(frame.height):
            offset = (y * frame.width + x) * size
            value = frame.data[offset] if size == 1 else struct.unpack_from('<H', frame.data, offset)[0]
            chunk.extend(((',' if y else '') + str(value)).encode())
            if len(chunk) >= 16384:
                yield bytes(chunk)
                chunk.clear()
        yield bytes(chunk) + b']'
    yield b']}'


class Camera:
    """Called only on the service worker. Owner identity includes source address."""
    def __init__(self, ident, driver_name, driver, clock, default_bin=1):
        self.ident, self.driver_name, self.driver, self.clock = ident, driver_name, driver, clock
        self.info = validate_info(driver.info())
        self.connected = False
        self.owner = None
        self.idle_since = None
        self.refused = None
        self.generation = uuid.uuid4().hex
        self.frame = None
        self.last_seconds = 0.0
        self.last_start = None
        heavy = self.info['width'] * self.info['height'] * (2 if self.info['bits'] > 8 else 1) > 3 * 1024 * 1024
        self.default_bin = 2 if heavy and default_bin == 2 and self.info['max_bin'] >= 2 else 1
        self.values = dict(binx=self.default_bin, biny=self.default_bin, startx=0, starty=0,
                           numx=self.info['width'] // self.default_bin, numy=self.info['height'] // self.default_bin, gain=self.info['gain_min'])

    def state(self):
        state = self.driver.state() if self.connected else 'idle'
        if state == 'error':
            # A failed acquisition or lost device invalidates cached readout.
            self.frame = self.last_start = None
        # Completed downloads are idle for lease purposes, even before retrieval.
        if state in ('waiting', 'exposing', 'reading'):
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = self.clock()
        if self.owner and self.idle_since is not None and self.clock() - self.idle_since >= 120:
            self.owner = None
        return state

    def check_owner(self, client, address):
        self.state()
        if self.owner and (self.owner['client_id'], self.owner['address']) != (client, address):
            self.refused = {'client_id': client, 'address': address, 'since': self.clock()}
            raise AlpacaError(0x40B)

    def release(self):
        if self.connected:
            self.driver.close()
        self.connected, self.owner, self.frame = False, None, None
        self.refused = None
        self.generation = uuid.uuid4().hex
        self.last_start = None
        self.idle_since = None

    def call(self, method, member, params, client, address):
        if member not in (READ if method == 'GET' else WRITE):
            raise AlpacaError(0x400)
        state = self.state()
        common = {'name': self.info['name'], 'description': 'Attached camera',
                  'driverinfo': 'Astro Bridge camera service', 'driverversion': '1.0',
                  'interfaceversion': 3, 'supportedactions': [], 'connected': (self.connected and
                      (self.driver_name != 'gphoto' or self.driver.is_ready()))}
        if method == 'GET' and member in common:
            return common[member]
        if method == 'PUT':
            self.check_owner(client, address)
            if member == 'connected':
                if boolean(params.get(member)):
                    self.driver.open()
                    self.connected = True
                else:
                    self.release()
                return None
        if not self.connected:
            raise AlpacaError(0x407)
        if self.driver_name == 'gphoto' and member in ('gain', 'gainmin', 'gainmax', 'gains'):
            raise AlpacaError(0x400)
        if method == 'PUT':
            if member == 'startexposure':
                if state in ('waiting', 'exposing', 'reading'):
                    raise AlpacaError(0x40B)
                values = self.values
                if values['binx'] != values['biny']:
                    raise AlpacaError(0x401)
                binning = values['binx']
                roi = dict(x=values['startx'] * binning, y=values['starty'] * binning,
                           width=values['numx'] * binning, height=values['numy'] * binning)
                seconds = duration(params.get('duration'))
                boolean(params.get('light'))  # Shutterless V3 cameras ignore Light.
                # The current driver contract reports HasShutter=False. V3
                # requires ignoring Light, including for the simulator facade.
                self.driver.start_exposure(seconds, binning, roi, values['gain'], True)
                self.frame = None
                self.last_seconds = seconds
                self.last_start = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
                if not self.owner:
                    self.generation = uuid.uuid4().hex
                    self.refused = None
                    self.owner = {'client_id': client, 'address': address, 'since': self.clock()}
                self.idle_since = None
                return None
            if member == 'abortexposure' and self.info['can_abort'] and self.driver_name != 'gphoto':
                self.driver.abort()
                self.last_start = None
                self.frame = None
                self.idle_since = self.clock()
                return None
            if member in self.values:
                if state in ('waiting', 'exposing', 'reading'):
                    raise AlpacaError(0x40B)
                maximum = (self.info['max_bin'] if member.startswith('bin') else
                           self.info['gain_max'] if member == 'gain' else
                           self.info['width' if member.endswith('x') else 'height'])
                minimum = self.info['gain_min'] if member == 'gain' else 1 if member.startswith(('bin', 'num')) else 0
                self.values[member] = number(params.get(member), minimum, maximum)
                return None
            if member == 'readoutmode' and number(params.get(member)) == 0:
                return None
            raise AlpacaError(0x400)
        if member in self.values:
            return self.values[member]
        if member == 'imagearray':
            if state == 'download' and self.frame is None:
                self.frame = self.driver.read_frame()
            if self.frame is None:
                raise AlpacaError(0x402)
            return self.frame
        if member in ('lastexposureduration', 'lastexposurestarttime'):
            if self.last_start is None or state in ('waiting', 'exposing', 'reading'):
                raise AlpacaError(0x402)
            return self.last_seconds if member == 'lastexposureduration' else self.last_start
        if member in ('bayeroffsetx', 'bayeroffsety') and self.info['sensor_type'] == 0:
            raise AlpacaError(0x400)
        properties = {
            'bayeroffsetx': self.info['bayer_offset'][0], 'bayeroffsety': self.info['bayer_offset'][1],
            'camerastate': {'idle': 0, 'waiting': 1, 'exposing': 2, 'reading': 3, 'download': 4, 'error': 5}[state],
            'cameraxsize': self.info['width'], 'cameraysize': self.info['height'],
            'canabortexposure': self.info['can_abort'] and self.driver_name != 'gphoto', 'canasymmetricbin': False,
            'canfastreadout': False, 'cangetcoolerpower': False, 'canpulseguide': False,
            'cansetccdtemperature': False, 'canstopexposure': False,
            'exposuremax': self.info['exposure_max'], 'exposuremin': self.info['exposure_min'],
            'gainmax': self.info['gain_max'], 'gainmin': self.info['gain_min'],
            'imageready': self.frame is not None or state == 'download', 'ispulseguiding': False,
            'maxadu': self.info['max_adu'], 'maxbinx': self.info['max_bin'], 'maxbiny': self.info['max_bin'],
            'pixelsizex': self.info['pixel_um_x'], 'pixelsizey': self.info['pixel_um_y'],
            'readoutmode': 0, 'readoutmodes': ['Default'], 'sensorname': self.info['sensor_name'],
            'sensortype': self.info['sensor_type'], 'hasshutter': False,
        }
        if member in properties:
            return properties[member]
        raise AlpacaError(0x400)
