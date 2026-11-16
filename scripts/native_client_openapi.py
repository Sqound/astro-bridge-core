"""Native client v1 additions, deliberately separate from runtime capability claims."""


def extend(schemas, paths):
    def ref(name):
        return {'$ref': '#/components/schemas/' + name}

    def obj(properties, required=None):
        return {'type': 'object', 'properties': properties,
                'required': list(properties if required is None else required),
                'additionalProperties': False}

    def arr(items, maximum=100):
        return {'type': 'array', 'items': items, 'maxItems': maximum}

    def enum(*values):
        return {'type': 'string', 'enum': list(values)}

    text = {'type': 'string', 'maxLength': 128}
    ident = {'type': 'string', 'pattern': '^[a-z0-9][a-z0-9-]{0,31}$'}
    boolean = {'type': 'boolean'}
    integer = {'type': 'integer', 'minimum': 0, 'maximum': 2**63-1}
    nullable_text = {'type': ['string', 'null'], 'maxLength': 128}
    scopes = dict(arr(enum('read', 'connect', 'telemetry', 'camera', 'admin'), 5), minItems=1, uniqueItems=True)
    caps = arr(enum('pairing', 'instruments', 'connect', 'events-wait', 'telemetry',
                    'alpaca-camera', 'maker-keys'), 7)
    device_id = {'type': 'string', 'pattern': '^[0-9a-f]{16}$'}
    boot_id = {'type': 'string', 'pattern': '^[0-9a-f]{32}$'}
    duration = {'type': 'integer', 'minimum': 60, 'maximum': 3600, 'default': 900}
    request_path = enum('home', 'hotspot', 'other')
    schemas['NativeInfo'] = obj({
        'product': enum('astro-bridge'), 'version': text, 'api_version': enum('1'),
        'contract_version': {'type': 'integer', 'const': 1}, 'device_id': device_id,
        'name': {'type': 'string', 'maxLength': 63}, 'capabilities': caps,
        'request_path': request_path,
        'pairing': obj({'available': boolean, 'method': enum('admin_password')}),
    })
    schemas['NativeInfo']['properties'].update({name: schemas['Info']['properties'][name] for name in ('authentication', 'token_expires_in', 'openapi_url')})
    schemas['NativeInfo']['properties']['legacy_capabilities'] = arr(text, 100)
    schemas['NativeClientCreate'] = obj({'name': {'type': 'string', 'minLength': 1, 'maxLength': 48}, 'scopes': scopes})
    schemas['NativeClientCreated'] = obj({'client_id': ident, 'client_secret': text, 'scopes': scopes, 'device_id': device_id})
    schemas['NativeClientLogin'] = obj({'client_id': ident, 'client_secret': text})
    schemas['NativeClientToken'] = obj({'access_token': text, 'token_type': enum('Bearer'), 'expires_in': {'type': 'integer', 'const': 43200}, 'scopes': scopes})
    schemas['NativeInstrument'] = obj({
        'id': ident, 'kind': enum('smart_telescope', 'mount', 'camera', 'other'),
        'family': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,47}$'}, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 64},
        'link': enum('connected', 'connecting', 'available', 'saved', 'disabled'),
        'service': enum('ready', 'not_answering', 'not_probed', 'unknown'),
        'since_seq': integer, 'reason': nullable_text, 'client_connect': boolean,
        'blockers': arr(text, 24),
        'lease': obj({'held': boolean, 'mine': boolean, 'expires_in': {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 3600}}),
        'capabilities': obj({'telemetry': arr({'type': 'string', 'pattern': '^[a-z][a-z0-9_.-]{0,63}$'}, 16), 'maker_key': {'type': ['string', 'null'], 'enum': ['absent', 'unverified', 'verified', None]}}),
    })
    # Contract 1.1: driver protocol family and model, null when unknown.
    from wifi_bridge.plugins import INSTRUMENT_PROTOCOLS
    schemas['NativeInstrument']['properties']['protocol'] = {'type': ['string', 'null'], 'enum': list(INSTRUMENT_PROTOCOLS) + ['alpaca', 'indi', None],
        'description': 'Protocol family to choose a driver from; null when the family declares none. Treat unknown values as unsupported.'}
    schemas['NativeInstrument']['properties']['model'] = {'type': ['string', 'null'], 'maxLength': 64,
        'description': 'Instrument model when known (verified identity or attached camera driver), otherwise null. Never the owner-chosen name.'}
    from wifi_bridge.identity import SOURCES as IDENTITY_SOURCES
    schemas['NativeInstrument']['properties']['verified_identity'] = {'oneOf': [{'type': 'null'}, obj({
        'model': {'type': ['string', 'null'], 'maxLength': 32},
        'hardware_id': {'type': ['string', 'null'], 'pattern': '^[0-9a-f]{16}$',
                        'description': 'Installation-local keyed digest of a manufacturer-reported identifier; never the identifier itself.'}})],
        'description': 'Identity read during this service run by a read-only manufacturer reply on the current link; null when unverified.'}
    schemas['NativeInstrument']['properties']['identity_source'] = {'type': 'string', 'enum': list(IDENTITY_SOURCES),
        'description': 'Which read-only reply verified the identity; unverified when none applies to the current link.'}
    schemas['NativeInstruments'] = obj({'config_revision': text, 'boot_id': boot_id, 'latest_event_seq': integer, 'instruments': arr(ref('NativeInstrument'), 24)})
    schemas['NativeInstruments']['properties'].update({'next_cursor': nullable_text, 'has_more': boolean})
    schemas['NativeAddress'] = obj({'host': text, 'ports': arr(obj({'port': {'type': 'integer', 'minimum': 1, 'maximum': 65535}, 'transport': enum('tcp', 'udp'), 'service': enum('lx200', 'synscan', 'seestar_native', 'vespera_http', 'vespera_socketio', 'dwarf_ws', 'direct_route')}), 32)})
    port_fields = schemas['NativeAddress']['properties']['ports']['items']['properties']
    port_fields['service']['enum'].extend(('alpaca_camera', 'indi_camera'))
    port_fields['device_number'] = {'type':'integer','minimum':0,'maximum':7}
    # Contract 1.1: optional declared purpose and application protocol per port.
    from wifi_bridge.plugins import PORT_ROLES, PORT_PROTOCOLS
    port_fields['service']['enum'].extend(('dwarf_http', 'dwarf_rtsp'))
    port_fields['role'] = {'type': 'string', 'enum': list(PORT_ROLES), 'description': 'Declared purpose: control, preview, imaging, rest, stream or other. Treat unknown values as other.'}
    port_fields['protocol'] = {'type': 'string', 'enum': list(PORT_PROTOCOLS), 'description': 'Declared application protocol carried by this port.'}
    schemas['NativeEndpoint'] = obj({'instrument_id': ident, 'request_path': request_path,
        'mode': enum('relay', 'proxy', 'routed', 'direct'), 'primary': ref('NativeAddress'),
        'alternatives': arr(ref('NativeAddress'), 2), 'requires_client_route': boolean, 'target_ip': text})
    schemas['NativeLease'] = obj({'id': text, 'expires_in': {'type': 'integer', 'minimum': 0, 'maximum': 3600}})
    schemas['NativeConnectRequest'] = obj({'lease_seconds': duration, 'allow_replace': arr(ident, 24), 'wait_seconds': {'type': 'integer', 'minimum': 0, 'maximum': 25, 'default': 0}}, [])
    schemas['NativeReady'] = obj({'state': enum('ready'), 'instrument_id': ident, 'lease': ref('NativeLease'), 'endpoint': ref('NativeEndpoint')})
    schemas['NativeConnecting'] = obj({'state': enum('connecting'), 'operation': obj({'id': text, 'status_url': text}), 'lease': ref('NativeLease')})
    # Preserve the existing operation lifecycle rather than introduce ready/running synonyms.
    schemas['NativeOperation'] = obj({'id': {'type': 'string', 'pattern': '^[0-9a-f]{32}\\.[0-9a-f]{32}$'}, 'boot_id': boot_id, 'profile_id': ident,
        'state': enum('running', 'succeeded', 'failed'), 'phase': text, 'reason': text,
        'endpoint': {'oneOf': [ref('NativeEndpoint'), {'type': 'null'}]}, 'endpoint_current': boolean, 'status_url': text})
    schemas['NativeOperation']['properties']['progress'] = {'oneOf': [{'type': 'null'}, obj({
        'phase': enum('leaving', 'associating', 'verifying', 'ready', 'restoring', 'failed'),
        'eta_seconds': {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 120}})],
        'description': 'Contract 1.1 switch progress: a countdown estimate from typical phase budgets, bounded by the operation deadline; null eta after failure.'}
    # Contract 1.1 night reservation: optional longer horizon, 1.0 bounds unchanged.
    reserve = {'type': 'integer', 'minimum': 60, 'maximum': 50400,
               'description': 'Night reservation horizon in seconds (at most 14 hours), counted by the bridge clock.'}
    schemas['NativeConnectRequest']['properties']['reserve_seconds'] = reserve
    schemas['NativeLease']['properties']['reservation_expires_in'] = {'type': 'integer', 'minimum': 0, 'maximum': 50400,
        'description': 'Present on a night reservation: its true remaining seconds. expires_in stays capped at 3600.'}
    schemas['NativeInstrument']['properties']['lease']['properties']['reservation_expires_in'] = {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 50400}
    schemas['NativeInstrument']['properties']['lease']['required'].append('reservation_expires_in')
    schemas['NativeRenew'] = obj({'lease_id': text, 'lease_seconds': duration})
    schemas['NativeRenew']['properties']['reserve_seconds'] = reserve
    schemas['NativeRelease'] = obj({'lease_id': text})
    schemas['NativeDisconnect'] = obj({'lease_id': text}, [])
    schemas['NativeError'] = obj({'error': obj({'code': text, 'message': {'type': 'string', 'maxLength': 1024},
        'reason': text, 'replaceable': arr(obj({'instrument_id': ident, 'name': text}), 24)}, ['code', 'message'])})
    schemas['NativeError']['properties']['error']['properties'].update({
        'free_radios': {'type': 'integer', 'minimum': 0, 'maximum': 16}, 'simultaneous': boolean})
    schemas['NativeInstrument']['properties']['access'] = {'type': 'string', 'enum': ['direct', 'station', 'attached'],
        'description': 'direct = the bridge joins the instrument Wi-Fi with a radio; station (contract 1.2) = the instrument joined the bridge network (hotspot or private LAN) and uses no radio; attached = USB camera.'}
    # Contract 1.2 station instruments: unreachable link, direct endpoints.
    schemas['NativeInstrument']['properties']['link']['enum'].append('unreachable')
    schemas['NativeInstrument']['properties']['link']['description'] = 'unreachable (contract 1.2): a station instrument that does not answer on the bridge network; treat unknown values as saved.'
    schemas['NativeInfo']['properties']['management_endpoints'] = {'type': 'array', 'maxItems': 8,
        'description': 'Contract 1.2: the bridge web interface on each confirmed management path; empty when unknown. Open url as given; never build it.',
        'items': obj({'url': {'type': 'string', 'maxLength': 256, 'pattern': '^https?://'}, 'path': enum('home', 'hotspot'), 'tls': boolean})}
    schemas['NativeInstrument']['properties']['current_operation'] = {'type': ['object', 'null'], 'additionalProperties': False,
        'required': ['state', 'kind', 'since'], 'properties': {
            'state': {'type': 'string', 'enum': ['idle', 'pending', 'running', 'paused', 'completed', 'failed', 'stopped', 'unknown']},
            'kind': {'type': ['string', 'null'], 'enum': [None, 'initialization', 'observation', 'capture', 'stacking', 'mosaic',
                                                          'calibration', 'pointing', 'focusing']},
            'since': {'type': 'string', 'maxLength': 64}},
        'description': 'Contract 1.2: read-only operation evidence used by protect_running_operations; null when the instrument reports none. kind is set only while running; since is the bridge UTC time this state was first observed.'}
    schemas['NativeInstrument']['properties']['simultaneous'] = {'type': 'boolean',
        'description': 'Contract 1.1: true when it is connected or connecting it needs no replacement (a free radio exists). False means connecting it disconnects another instrument; adding a Wi-Fi adapter avoids that.'}
    schemas['NativeError']['properties']['error']['properties']['retry_after'] = {'type': 'integer', 'minimum': 1, 'maximum': 3600,
        'description': 'Contract 1.1: seconds before retrying; repeats the Retry-After header of every 429.'}
    schemas['NativeInfo']['properties'].update({
        'contract_minor': {'type': 'integer', 'minimum': 0, 'description': 'Minor version of contract 1 (1 since 1.1, 2 since 1.2).'},
        'free_radios': {'type': 'integer', 'minimum': 0, 'maximum': 16, 'description': 'Instrument-capable radios currently unused: a connect needing a radio will not replace anything while this is above zero.'},
        'hotspot': boolean, 'tls_fingerprint': {'type': ['string', 'null'], 'pattern': '^[0-9a-f]{64}$',
            'description': 'SHA-256 of the bridge CA certificate while local HTTPS is ready; a trust-on-first-use pin, verified again over TLS.'}})
    event_base = {'sequence': integer, 'boot_id': boot_id, 'timestamp': text,
                  'clock_status': enum('unverified', 'synchronized'), 'instrument_id': ident}
    event_data = {
        'camera.detected': {}, 'camera.removed': {}, 'camera.power_unstable': {},
        'instrument.state': {'link': schemas['NativeInstrument']['properties']['link'], 'service': schemas['NativeInstrument']['properties']['service']},
        'instrument.lost': {'reason': text}, 'instrument.restored': {},
        'instrument.moved': {'from_radio_id': text, 'to_radio_id': text},
        'instrument.endpoint_changed': {'endpoint': {'oneOf': [ref('NativeEndpoint'), {'type': 'null'}]}},
        'lease.expiring': {'lease_id': text, 'expires_in': integer},
        'lease.ended': {'lease_id': text, 'reason': text},
        'telemetry.session_started': {'session_id': text},
        'telemetry.session_ended': {'session_id': text},
        'telemetry.available': {'session_id': text, 'latest_sample_seq': integer},
        'telemetry.adapter_refused': {'adapter_id': enum('seestar_events_v1')},
        'telemetry.adapter_yielded': {'adapter_id': enum('vespera_status_v1','seestar_events_v1'), 'retry_after': {'type':'integer', 'const':600}},
        # Contract 1.1: connection (switch) progress and its completion notice.
        'instrument.switch_progress': {'operation': {'type': 'string', 'pattern': '^[0-9a-f]{32}\\.[0-9a-f]{32}$'},
            'phase': enum('leaving', 'associating', 'verifying', 'restoring'), 'eta_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 120}},
        'instrument.switch_completed': {'operation': {'type': 'string', 'pattern': '^[0-9a-f]{32}\\.[0-9a-f]{32}$'},
            'outcome': enum('ready', 'failed'), 'reason': text},
    }
    schemas['NativeEvent'] = {'oneOf': [obj(dict(event_base, type=enum(name), **fields)) for name, fields in event_data.items()]}
    for event in schemas['NativeEvent']['oneOf']:
        event['properties']['utc_corrected'] = dict(text, format='date-time')
    page = dict(schemas['EventPage']['properties'])
    page['events'] = arr({'oneOf': [ref('ConnectionEvent'), ref('NativeEvent')]})
    schemas['NativeEvents'] = obj(page, schemas['EventPage']['required'])
    schemas['TelemetrySample'] = obj({'seq': integer, 'session_id': text, 'boot_id': boot_id,
        'mono_ms': integer, 'utc': text, 'utc_corrected': nullable_text,
        'metric': {'type': 'string', 'maxLength': 64},
        'value': {'type': ['number', 'string', 'boolean'], 'maxLength': 64}})
    schemas['TelemetryEvent'] = obj({'seq': integer, 'session_id': text, 'boot_id': boot_id,
        'type': enum('session.started', 'session.ended'), 'reason': nullable_text})
    schemas['NativeTelemetryPage'] = obj({'next_cursor': integer,
        'has_more': boolean, 'gap': boolean, 'reset_reason': page['reset_reason'],
        'samples': arr(ref('TelemetrySample'), 500), 'events': arr(ref('TelemetryEvent'), 500)})
    schemas['NativeTelemetryPage']['properties']['boot_id'] = dict(boot_id,
        description='Contract 1.2: boot domain of next_cursor; send it back as the boot_id query parameter. Absent from earlier servers.')
    header = {'id': text, 'profile_id': ident, 'kind': text, 'started_seq': integer,
        'ended_seq': dict(integer, type=['integer','null']), 'clock_status': enum('unverified','synchronized'),
        'adapters': arr(text,16), 'started': text, 'ended': nullable_text}
    schemas['TelemetrySession'] = obj(header)
    schemas['TelemetrySessions'] = obj({'sessions': arr(ref('TelemetrySession'),100), 'next_cursor': integer, 'has_more': boolean})
    schemas['TelemetrySummary'] = obj(dict(header, frames_accepted={'type':'number'}, frames_rejected={'type':'number'},
        reject_reasons={'type':'object','maxProperties':128,'additionalProperties':{'type':'number'}}, pointing_failures=integer,
        gaps=arr(obj({'from_seq':integer,'to_seq':integer,'reason':enum('RETENTION_GAP')}),100),
        readers=arr(obj({'client_name':{'type':'string','maxLength':48},'from_seq':integer,'to_seq':integer}),16)))
    schemas['TelemetryAnalysis'] = obj({'session_id':text,
        'sample_count':dict(integer,type=['integer','null']), 'bucket_count':{'type':'integer','const':240},
        'from_mono_ms':integer, 'to_mono_ms':integer, 'limited':boolean, 'history_available':boolean,
        'points':arr(obj({'metric':enum('stack.frames_accepted','stack.frames_rejected'),
            'from_seq':integer,'to_seq':integer,'low':{'type':'number'},'high':{'type':'number'},
            'mono_ms':integer,'utc':text}),480),
        'gaps':arr(obj({'from_seq':integer,'to_seq':integer,'reason':enum('RETENTION_GAP')}),100),
        'reader_coverage':obj({'unknown_before_seq':dict(integer,type=['integer','null']),
            'fetched':arr(obj({'from_seq':integer,'to_seq':integer}),128)}),
        'exposure':obj({'observed_seconds':{'type':['number','null']},'observed_frames':{'type':'number'},
            'complete':boolean,'latest_seconds':{'type':['number','null']}})})
    schemas['TelemetryStatus'] = obj({'enabled':boolean, 'persistent':boolean,
        'retention':obj({'nights':{'type':'integer','minimum':1,'maximum':365}, 'max_bytes':integer}),
        'used_bytes':integer,'sessions_count':integer,'clock_status':enum('unverified','synchronized'),
        'boot_id':boot_id,'latest_seq':integer,'config_revision':text,
        'profiles':{'type':'object','maxProperties':24,'additionalProperties':obj({'adapters':dict(arr(text,16),uniqueItems=True),
            'alpaca_interval_seconds':{'type':'integer','minimum':10,'maximum':120}}, ['adapters'])}})
    counter = {'type':'integer','minimum':0,'maximum':2**53}
    schemas['TelemetryAdapterStatus'] = obj({
        'adapter_id':text,'adapter_state':enum('waiting','connecting','listening','yielded','refused','unavailable'),
        'retry_after':integer,'unavailable_reason':{'type':['string','null'],'enum':[None,'owner_disabled','demo','key_unavailable','disconnected','isolated_priority']},
        'event_counts_minute':obj({name:counter for name in ('PiStatus','Stack','Exposure','AutoGoto','AutoFocus','unknown')}),
        'handshakes':obj({name:counter for name in ('verified','refused','unverified')}),
        'connection_closes':counter,'connected_seconds':integer,'last_connection_seconds':integer},
        ['adapter_id','adapter_state','retry_after','unavailable_reason'])
    schemas['TelemetryStatus']['properties']['adapter_status'] = {'type':'object','maxProperties':24,'additionalProperties':arr(ref('TelemetryAdapterStatus'),16)}
    schemas['TelemetryCardReading'] = obj({'value':{'type':['number','string','boolean']},
        'utc':text,'since':text,'cadence_seconds':{'type':'number','exclusiveMinimum':0,'maximum':120},
        'adapter_id':text,'age_seconds':{'type':'number','minimum':0},'stale':boolean})
    card_recent = obj({'id':text,'started':text,'ended':nullable_text,'clock_status':enum('unverified','synchronized'),
                       'frames_accepted':{'type':['number','null']},'frames_rejected':{'type':['number','null']}})
    card_target = obj({'target':{'type':'string','maxLength':64},'frames_accepted':{'type':['number','null']},'frames_rejected':{'type':['number','null']}})
    schemas['TelemetryCard'] = obj({'readings':{'type':'object','maxProperties':32,'additionalProperties':ref('TelemetryCardReading')},
        'recent':arr(card_recent,3),'tonight':arr(card_target,8),'history_partial':boolean})
    schemas['TelemetryStatus']['properties']['card_data'] = {'type':'object','maxProperties':24,'additionalProperties':ref('TelemetryCard')}
    schemas['TelemetrySettings'] = obj({'retention_nights':{'type':'integer','minimum':1,'maximum':365},
        'max_bytes':{'type':'integer','minimum':4*1024**2,'maximum':512*1024**2},
        'profiles':{'type':'object','maxProperties':24,'additionalProperties':obj({'adapters':dict(arr(text,16),uniqueItems=True),
            'alpaca_interval_seconds':{'type':'integer','minimum':10,'maximum':120}}, ['adapters'])}}, [])
    schemas['TelemetryDeleted'] = obj({'ok':boolean,'config_revision':text})
    schemas['TelemetryExport'] = obj({'session':ref('TelemetrySummary'),
        'metrics_dictionary':{'type':'object','additionalProperties':text},
        'samples':{'type':'array','items':ref('TelemetrySample')}, 'events':{'type':'array','items':ref('TelemetryEvent')}})

    def response(schema, description='Success'):
        return {'description': description, 'content': {'application/json': {'schema': ref(schema)}}}

    def add(path, method, operation, result, request=None, status=200):
        entry = {'operationId': operation, 'tags': ['native-client'], 'x-contract-status': 'planned',
                 'responses': {str(status): response(result), **{str(code): response('NativeError', 'Contract error') for code in (400, 401, 403, 404, 409, 410, 412, 428, 429)}}}
        if '{id}' in path:
            entry['parameters'] = [{'name': 'id', 'in': 'path', 'required': True, 'schema': ident}]
        if request:
            entry['requestBody'] = {'required': True, 'content': {'application/json': {'schema': ref(request)}}}
        paths['/api/v1' + path] = {method: entry}
        entry['x-required-scopes'] = ['telemetry' if path.startswith('/telemetry/') else 'connect' if method == 'post' else 'read']
        return entry

    schemas['ConnectionOperation']['required'] = ['id', 'boot_id', 'profile_id', 'state', 'phase', 'reason', 'cause']
    for path, schema in [('/info', 'NativeInfo'), ('/events', 'NativeEvents'), ('/operations/{id}', 'NativeOperation')]:
        entry = paths['/api/v1' + path]['get']
        entry.update(tags=['native-client'], **{'x-contract-status': 'planned', 'x-existing-route-implemented': True})
        old = entry['responses']['200']['content']['application/json']['schema']
        entry['responses']['200']['content']['application/json']['schema'] = {'anyOf': [old, ref(schema)]} if path != '/events' else ref(schema)
    paths['/api/v1/info']['get']['x-contract-status'] = 'implemented'
    paths['/api/v1/info']['get']['responses']['200']['content']['application/json']['schema'] = ref('NativeInfo')
    schemas['NativeClientLogin']['x-contract-status'] = 'implemented'
    schemas['NativeClientToken']['x-contract-status'] = 'implemented'
    token = paths['/api/v1/auth/token']['post']
    token.update(tags=['native-client'], **{'x-contract-status': 'implemented', 'x-client-credentials-status': 'implemented'})
    token['requestBody']['content']['application/json']['schema'] = {'oneOf': [ref('TokenRequest'), ref('NativeClientLogin')]}
    # Legacy Token has permissive fields: anyOf preserves its compatibility with scoped tokens.
    token['responses']['200']['content']['application/json']['schema'] = {'anyOf': [ref('Token'), ref('NativeClientToken')]}
    paired = add('/auth/clients', 'post', 'pairNativeClient', 'NativeClientCreated', 'NativeClientCreate', 201)
    paired.update({'x-required-scopes': ['admin'], 'x-contract-status': 'implemented'})
    schemas['NativeClientSummary'] = obj({'client_id': ident, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 48},
        'scopes': scopes, 'created_seq': {'type': 'integer', 'minimum': 1},
        'last_used_utc_best_effort': {'type': ['string', 'null']}})
    schemas['NativeClientList'] = obj({'clients': arr(ref('NativeClientSummary'), 16), 'config_revision': text})
    listed = {'operationId': 'listPairedClients', 'tags': ['native-client'], 'x-contract-status': 'implemented',
              'responses': {'200': response('NativeClientList'), **{str(code): response('NativeError') for code in (401, 403)}},
              'x-required-scopes': ['admin']}
    paths['/api/v1/auth/clients']['get'] = listed
    removed = add('/auth/clients/{id}', 'delete', 'revokePairedClient', 'OK')
    removed.update({'x-required-scopes': ['admin'], 'x-contract-status': 'implemented'})
    removed['parameters'].append({'name': 'If-Match', 'in': 'header', 'required': True,
                                  'schema': {'type': 'string'}, 'description': 'Actual current config_revision from the client list; wildcard refused.'})
    listed_instruments = add('/instruments', 'get', 'listNativeInstruments', 'NativeInstruments')
    listed_instruments['parameters'] = [{'name':'after', 'in':'query', 'required':False,
        'schema':{'type':'string','minLength':46,'maxLength':46,'pattern':'^[A-Za-z0-9_-]{46}$'},
        'description':'Opaque next_cursor from the previous page. CATALOG_CHANGED requires a fresh first page.'}]
    add('/instruments/{id}', 'get', 'getNativeInstrument', 'NativeInstrument')
    connect = add('/instruments/{id}/connect', 'post', 'connectNativeInstrument', 'NativeReady', 'NativeConnectRequest')
    connect['responses']['202'] = response('NativeConnecting')
    connect['parameters'].append({'name': 'If-Match', 'in': 'header', 'required': True, 'schema': {'type': 'string', 'pattern': '^"[^"\\r\\n]+"$'}})
    add('/instruments/{id}/lease', 'post', 'renewNativeLease', 'NativeLease', 'NativeRenew')
    add('/instruments/{id}/release', 'post', 'releaseNativeLease', 'OK', 'NativeRelease')
    add('/instruments/{id}/disconnect', 'post', 'disconnectNativeInstrument', 'OK', 'NativeDisconnect')
    add('/instruments/{id}/endpoint', 'get', 'getNativeEndpoint', 'NativeEndpoint')
    for path, methods in paths.items():
        if path.startswith('/api/v1/instruments') or path == '/api/v1/operations/{id}':
            for entry in methods.values(): entry['x-contract-status'] = 'implemented'
    paths['/api/v1/events']['get']['x-contract-status'] = 'implemented'
    paths['/api/v1/events']['get']['parameters'].append({'name': 'wait', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0, 'maximum': 25, 'default': 0}, 'x-contract-status': 'implemented'})
    telemetry = add('/telemetry/samples', 'get', 'getTelemetrySamples', 'NativeTelemetryPage')
    telemetry['parameters'] = [dict(p) for p in paths['/api/v1/events']['get']['parameters'] if p['name'] != 'wait']
    for parameter in telemetry['parameters']:
        if parameter['name'] == 'limit':
            parameter['schema'] = {'type': 'integer', 'minimum': 1, 'maximum': 500}
    telemetry['parameters'].append({'name': 'session_id', 'in': 'query', 'required': True, 'schema': text})

    telemetry['x-contract-status'] = 'implemented'
    def resource(path, method, operation, result, request=None):
        existing = paths.get('/api/v1' + path, {}).copy()
        entry = add(path, method, operation, result, request)
        paths['/api/v1' + path].update(existing)
        entry['x-contract-status'] = 'implemented'
        return entry
    resource('/telemetry', 'get', 'getTelemetry', 'TelemetryStatus')
    sessions = resource('/telemetry/sessions', 'get', 'getTelemetrySessions', 'TelemetrySessions')
    sessions['parameters'] = [{'name':name,'in':'query','schema':schema} for name,schema in (
        ('profile_id',ident),('after',integer),('limit',{'type':'integer','minimum':1,'maximum':100}))]
    resource('/telemetry/sessions/{id}', 'get', 'getTelemetrySession', 'TelemetrySummary')
    resource('/telemetry/sessions/{id}/analysis', 'get', 'analyzeTelemetrySession', 'TelemetryAnalysis')
    exported = resource('/telemetry/sessions/{id}/export', 'get', 'exportTelemetrySession', 'TelemetryExport')
    exported['parameters'].append({'name':'format','in':'query','schema':enum('csv','json')})
    exported['responses']['200']['content']['text/csv'] = {'schema':{'type':'string'}}
    exported['responses']['200']['headers'] = {'Content-Disposition':{'schema':{'type':'string'},'description':'Neutral kind-date attachment filename.'}}
    for path,method,operation,result,request in [('/telemetry/settings','patch','patchTelemetrySettings','TelemetryStatus','TelemetrySettings'),
            ('/telemetry/sessions','delete','deleteTelemetrySessions','TelemetryDeleted',None),
            ('/telemetry/sessions/{id}','delete','deleteTelemetrySession','TelemetryDeleted',None)]:
        entry = resource(path,method,operation,result,request)
        entry.setdefault('parameters',[]).append({'name':'If-Match','in':'header','required':True,'schema':{'type':'string'}})

    camera_port = {'type':'integer','minimum':1024,'maximum':65535}
    camera_settings = {'indi':boolean, 'serve':boolean, 'discover':boolean, 'port':camera_port,
        'allowed_subnets':dict(arr({'type':'string','minLength':9,'maxLength':18,
            'pattern':r'^(?:[0-9]{1,3}\.){3}[0-9]{1,3}/(?:[1-9]|[12][0-9]|3[0-2])$'},16),uniqueItems=True)}
    schemas['NativeCameraSettingsValue'] = obj(camera_settings, ['serve','port','allowed_subnets'])
    schemas['NativeCameraSettingsPatch'] = dict(obj(camera_settings, []), minProperties=1)
    indi = obj({'port': {'type':'integer','const':7624}, 'state':enum('off','waiting','unavailable','ready')})
    schemas['NativeCameraSettings'] = obj({'settings':ref('NativeCameraSettingsValue'),'config_revision':text,'indi':indi},
                                          ['settings','config_revision'])
    camera_fields = {
        'id':{'type':'string','pattern':'^[a-z][a-z0-9_-]{0,63}$'},
        'name':{'type':'string','minLength':1,'maxLength':64},
        'driver':{'type':'string','pattern':'^[a-z][a-z0-9_]{0,31}$'},
        'state':{'type':'string','minLength':1,'maxLength':32,
                 'description':'Current driver state (idle, waiting, exposing, reading, download, error). Clients preserve unknown additive values; future discovery lifecycle is separate.'},
        'alpaca':{'oneOf':[{'type':'null'}, obj({'port':camera_port,
                    'device_number':{'type':'integer','minimum':0,'maximum':7}})],
                  'description':'Null unless the Alpaca listener is bound on this request ingress.'},
        'last_image':obj({'bytes':{'type':'integer','minimum':0,'maximum':33554432},
                          'seconds':{'type':'number','minimum':0,'maximum':86400}}),
        'owner':obj({'client_id':{'type':'integer','minimum':0,'maximum':4294967295},
                     'address':{'type':'string','format':'ipv4'},
                     'since':{'type':'number','minimum':0,'description':'Monotonic seconds, not UTC.'},
                     'generation':{'type':'string','pattern':'^[0-9a-f]{32}$'}, 'dhcp_name':{'type':'string','maxLength':253}}, ['client_id','address','since']),
    }
    schemas['NativeCamera'] = obj(camera_fields, [key for key in camera_fields if key != 'owner'])
    schemas['NativeCamera']['properties'].update({
        'instrument_id': ident, 'indi': indi,
        'sensor':obj({'width':{'type':'integer','minimum':1,'maximum':16384},'height':{'type':'integer','minimum':1,'maximum':16384},'bits':{'type':'integer','minimum':1,'maximum':16},'max_bin':{'type':'integer','minimum':1,'maximum':16}}),
        'defaults':obj({'bin':{'type':'integer','minimum':1,'maximum':16},'gain':{'type':'integer','minimum':0,'maximum':65535}}),
        'current':obj({'bin':{'type':'integer','minimum':1,'maximum':16},'exposure_seconds':{'type':'number','minimum':0,'maximum':86400}}),
        'refused':obj({'client_id':{'type':'integer','minimum':0,'maximum':4294967295},'address':{'type':'string','format':'ipv4'},'since':{'type':'number','minimum':0}}),
        'discovery': obj({'state':enum('detected','preparing','ready','driver_missing','unsupported'),
                          'power_unstable':boolean,'disconnects':{'type':'integer','minimum':0,'maximum':1201}}),
        'usb': obj({'vendor_id':{'type':'integer','minimum':0,'maximum':65535},
                    'product_id':{'type':'integer','minimum':0,'maximum':65535},
                    'device_class':{'type':'integer','minimum':0,'maximum':255},
                    'interface_classes':dict(arr({'type':'integer','minimum':0,'maximum':255},256),uniqueItems=True),
                    'speed_mbps':{'type':'number','exclusiveMinimum':0,'maximum':100000},
                    'bus':{'type':'integer','minimum':1,'maximum':999},
                    'port_path':{'type':'string','maxLength':35,'pattern':r'^[1-9][0-9]{0,2}-[1-9][0-9]{0,2}(?:\.[1-9][0-9]{0,2}){0,7}$'}}),
    })
    schemas['CameraOwnerRelease'] = obj({'confirm_release':{'const':True},'owner_generation':{'type':'string','pattern':'^[0-9a-f]{32}$'}})
    schemas['CameraDemoPlug'] = obj({'plugged':boolean})
    schemas['NativeCameras'] = arr(ref('NativeCamera'), 8)
    for path,method,operation,result,request,scope in [
        ('/cameras','get','listCameras','NativeCameras',None,'camera'),
        ('/cameras/settings','get','getCameraSettings','NativeCameraSettings',None,'admin'),
        ('/cameras/settings','patch','patchCameraSettings','NativeCameraSettings','NativeCameraSettingsPatch','admin'),
        ('/cameras/{id}/release-owner','post','releaseCameraOwner','OK','CameraOwnerRelease','admin'),
        ('/cameras/demo','post','plugDemoCamera','OK','CameraDemoPlug','admin')]:
        entry = resource(path,method,operation,result,request)
        entry['x-required-scopes'] = [scope]
        entry['responses']['503'] = response('NativeError')
        if path.startswith('/cameras') and method in ('patch','post'):
            entry['parameters'] = [{'name':'If-Match','in':'header','required':True,
                'schema':{'type':'string'},'description':'Actual current quoted config_revision; wildcard refused.'}]
            if '{id}' in path:
                entry['parameters'].append({'name':'id','in':'path','required':True,'schema':camera_fields['id']})

    schemas['NativeInstrument']['properties']['capabilities']['properties']['maker_key']['enum'] += ['refused','storage_unavailable']
    key_state = enum('absent','unverified','verified','refused','storage_unavailable')
    schemas['MakerKey'] = obj({'family':enum('seestar'),'state':key_state,
        'source':{'type':['string','null'],'enum':['maker_package','import',None]},
        'updated_seq':integer,'unlocks':arr(enum('stacking','exposures','goto','temperature','battery'),5)})
    schemas['MakerKeys'] = obj({'keys':arr(ref('MakerKey'),1),'config_revision':text})
    schemas['MakerKeyImport'] = obj({'pem':{'type':'string','minLength':1,'maxLength':8192,'writeOnly':True}})
    schemas['MakerKeyStep'] = obj({'step':text,'bytes':integer,'http_status':{'type':'integer','minimum':100,'maximum':599},
        'crcVerified':boolean,'candidates':{'type':'integer','minimum':0,'maximum':16},'verdict':text}, ['step'])
    schemas['MakerKeyOperation'] = obj({'id':{'type':'string','pattern':'^[0-9a-f]{32}$'},
        'state':enum('running','succeeded','failed'),'verdict':nullable_text,'steps':arr(ref('MakerKeyStep'),48),'status_url':text})
    schemas['MakerKeyOperationResult'] = obj({'operation':ref('MakerKeyOperation'),'config_revision':text})
    schemas['MakerKeyFetch'] = obj({})
    for path,method,operation,result,request in [
        ('/maker-keys','get','listMakerKeys','MakerKeys',None),
        ('/maker-keys/{family}','put','importMakerKey','MakerKeys','MakerKeyImport'),
        ('/maker-keys/{family}','delete','deleteMakerKey','MakerKeys',None),
        ('/maker-keys/{family}/fetch','post','fetchMakerKey','MakerKeyOperationResult','MakerKeyFetch'),
        ('/maker-keys/operations/{id}','get','getMakerKeyOperation','MakerKeyOperationResult',None)]:
        entry = resource(path,method,operation,result,request)
        entry['x-required-scopes'] = ['admin']
        entry['responses']['422'] = response('NativeError')
        entry['responses']['503'] = response('NativeError')
        if method == 'post': entry['responses']['202'] = entry['responses'].pop('200')
        if '{id}' in path: entry['parameters'][0]['schema'] = {'type':'string','pattern':'^[0-9a-f]{32}$'}
        if '{family}' in path:
            entry['parameters'] = [{'name':'family','in':'path','required':True,'schema':enum('seestar')}]
        if method == 'delete':
            entry['parameters'] += [{'name':'If-Match','in':'header','required':True,
                'schema':{'type':'string'},'description':'Actual current quoted config_revision; wildcard refused.'}]
