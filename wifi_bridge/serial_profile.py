"""Serial device profiles: data, not code. A profile owns the serial settings,
the read-only identification frames and the stop sequence sent when a client is
lost. The relay executes it and never carries a manufacturer command itself."""
from functools import lru_cache
import re

from .serial_profile_data import PROFILES

BAUDS = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)
FORMAT = re.compile(r'[78][NEO][12]')
MAX_FRAMES = 16
MAX_FRAME_BYTES = 32


def _frame(entry, extra=()):
    if not isinstance(entry, dict) or set(entry) - {'id', 'hex'} - set(extra) or not {'id', 'hex'} <= set(entry):
        raise ValueError('Invalid serial profile frame.')
    if not isinstance(entry['id'], str) or not re.fullmatch(r'[a-z0-9_]{1,48}', entry['id']):
        raise ValueError('Invalid serial profile frame.')
    try: data = bytes.fromhex(entry['hex'])
    except (TypeError, ValueError): raise ValueError('Invalid serial profile frame.') from None
    if not 1 <= len(data) <= MAX_FRAME_BYTES: raise ValueError('Invalid serial profile frame.')
    return dict(entry, data=data)


def _sequence(value, extra=()):
    if value is None: return None
    if not isinstance(value, dict) or not 0 < value.get('frame_timeout_seconds', 0) <= 5: raise ValueError('Invalid serial profile sequence.')
    terminator = bytes.fromhex(value.get('reply_terminator', ''))
    frames = value.get('frames')
    if len(terminator) != 1 or not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
        raise ValueError('Invalid serial profile sequence.')
    return {'terminator': terminator[0], 'timeout': float(value['frame_timeout_seconds']),
            'frames': [_frame(f, extra) for f in frames], 'parser': value.get('parser')}


def validate_profile(value):
    if not isinstance(value, dict) or value.get('schema_version') != 1 or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', str(value.get('id'))):
        raise ValueError('Invalid serial profile.')
    serial = value.get('serial', {})
    if serial.get('baud') not in BAUDS or not FORMAT.fullmatch(str(serial.get('format'))) or serial.get('flow_control') != 'none':
        raise ValueError('Invalid serial profile settings.')
    port = value.get('default_tcp_port')
    if type(port) is not int or not 1024 <= port <= 65535: raise ValueError('Invalid serial profile port.')
    return dict(value, stop=_sequence(value.get('stop_on_loss')), identify_sequence=_sequence(value.get('identify'), ('reply_length',)),
                models={int(k): v for k, v in value.get('models', {}).items()})


@lru_cache(maxsize=1)
def load_profiles():
    profiles = {}
    for entry in PROFILES:
        value = validate_profile(entry)
        if value['id'] in profiles: raise ValueError('Duplicate serial profile.')
        profiles[value['id']] = value
    return profiles


def profile(ident):
    return load_profiles().get(ident)


def public_profile(value):
    return {key: value[key] for key in ('id', 'name', 'protocol', 'app_preset', 'serial', 'default_tcp_port')} | {
        'stop_on_loss': bool(value['stop']), 'identification': bool(value['identify_sequence'])}


def parse_nexstar(replies, profile_models):
    """Interpret the documented replies of the identification frames.

    Echo `K x` returns `x#`; `V` two version bytes; `m` one model byte; the
    pass-through version request two bytes per motor board. Nothing here is a
    serial number: the documented protocol has no per-unit identifier.
    """
    result = {'responding': False, 'hand_control_version': None, 'model_code': None, 'model_name': None, 'board_versions': {}}
    if replies.get('echo') != b'x':
        return result
    version = replies.get('version')
    if version is not None and len(version) == 2 and 1 <= version[0] <= 9:
        result['responding'] = True
        result['hand_control_version'] = '%d.%d' % (version[0], version[1])
    model = replies.get('model')
    if model is not None and len(model) == 1:
        result['model_code'] = model[0]
        result['model_name'] = profile_models.get(model[0], 'Celestron')
    for axis in ('azimuth', 'altitude'):
        board = replies.get(axis + '_board_version')
        if board is not None and len(board) == 2: result['board_versions'][axis] = '%d.%d' % (board[0], board[1])
    return result


PARSERS = {'nexstar_v1': parse_nexstar}
