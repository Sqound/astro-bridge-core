"""Real native facade over a fixture USB tree and a disposable bound Alpaca port."""
import copy
from pathlib import Path
import sys


def run(flow):
    # Reuse the real HTTP/Alpaca test fixture, never a replay server or host USB.
    # Direct unittest invocation does not add tests/ to the import search path.
    tests_path = str(Path(__file__).resolve().parents[1])
    added_path = tests_path not in sys.path
    if added_path:
        sys.path.insert(0, tests_path)
    try:
        from test_camera_native import CameraNativeHTTPTests
    finally:
        if added_path:
            sys.path.remove(tests_path)
    case = CameraNativeHTTPTests(methodName='runTest')
    try:
        case.setUp()
        case.store.config['telescopes'] = []
        case.controller.protection.clock = lambda: 100.0
        if flow['name'] == 'camera-indi':
            from .indi_fake import DEVICE, FakePlatform
            platform = FakePlatform(port=7624)
            case.service.indi.platform = platform
            case.service.update_settings({'indi': True})
            case.service.reconcile_usb([DEVICE], case.registry)
            platform.wait_ready(case.service)
        else:
            case.attach()
        ident = case.camera()['id']
        result, lease = [], None
        for step in flow['steps']:
            request = copy.deepcopy(step['request'])
            path = request['path'].replace('camera-1', ident)
            headers = {'If-Match':'"'+case.server.api.revision()+'"'} if request['method']=='POST' and path.endswith('/connect') else {}
            body = request['body']
            if body and body.get('lease_id') == 'lease-1':
                body['lease_id'] = lease
            status, actual, _ = case.request(request['method'], path, body, headers)
            if actual.get('lease'):
                lease = actual['lease']['id']
            result.append((status, actual))
        return result
    finally:
        case.doCleanups()


def normalize(body):
    from .native_instrument_flows import normalize as common
    body, patterns = common(body)
    def scrub(value, pointer=''):
        if isinstance(value, dict):
            for key, item in value.items():
                path = pointer + '/' + key
                if key in ('id','instrument_id') and isinstance(item,str) and item.startswith('usb-'):
                    value[key] = 'camera-1'; patterns[path] = '^usb-[0-9a-f]{24}$'
                elif key in ('host','target_ip'):
                    value[key] = '192.0.2.10'; patterns[path] = r'^127\.0\.0\.1$'
                elif key == 'port':
                    value[key] = 11111; patterns[path] = '^[0-9]{4,5}$'
                elif key in ('latest_event_seq','since_seq'):
                    value[key] = 1; patterns[path] = '^[0-9]+$'
                else:
                    scrub(item,path)
        elif isinstance(value,list):
            for index,item in enumerate(value): scrub(item,pointer+'/'+str(index))
    scrub(body)
    return body, patterns
