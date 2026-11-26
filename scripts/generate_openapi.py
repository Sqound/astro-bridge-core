"""Generate the shipped OpenAPI 3.1 contract using only the standard library."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wifi_bridge import __version__
from wifi_bridge.plugins import profile_kinds, relay_plugins, demand_plugins, proxy_flag, manifest_schema, all_plugins
from wifi_bridge.events import TYPES, CAUSES, REASONS


def ref(name):
    return {'$ref': '#/components/schemas/' + name}


def obj(properties, required=(), **extra):
    result = dict(type='object', properties=properties, **extra)
    if required:
        result['required'] = list(required)
    return result


string = {'type': 'string'}
boolean = {'type': 'boolean'}
mac = {'type': 'string', 'pattern': '^(?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2}$'}
ident = {'type': 'string', 'pattern': '^[a-z0-9][a-z0-9-]{0,31}$'}
network = {'ssid': {'type': 'string', 'minLength': 1, 'maxLength': 32, 'description': '1–32 UTF-8 bytes.'},
           'mac': mac, 'password': dict(string, writeOnly=True), 'open': boolean,
           'bssid': string, 'hidden': boolean}
telescope = dict(network, mac={'type': 'string', 'pattern': '^(?:|(?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2})$', 'description': 'Empty or omitted for an inactive unassigned profile.'}, name={'type': 'string', 'minLength': 1, 'maxLength': 64},
                 kind={'type': 'string', 'enum': list(profile_kinds()), 'default': 'generic'},
                 target_ip={'type': 'string', 'format': 'ipv4', 'description': 'Private instrument IPv4, not the Pi address.'},
                 enabled=dict(boolean, default=False), isolated=dict(boolean,default=False,description='Opt-in isolated transport (ZWO and SynScan mounts, Vespera telescopes); one radio per instrument, so several instruments may share one target address.'),
                 client_ports={'type':'array','readOnly':True,'items':{'type':'integer','minimum':10000,'maximum':49151},'maxItems':2,'description':'Isolated Vespera: client ports for 8082 (HTTP) and 8083 (Socket.IO), assigned and persisted by the bridge.'},
                 client_port={'type':'integer','minimum':10000,'maximum':49151,'description':'Stable dedicated TCP/UDP port. Allocated on save when isolated is true; excludes reserved management ports.'}, client_connect=dict(boolean,default=True), auto_replaceable=dict(boolean,default=False,description='Victim participation, also requires auto_eligible; never overrides a lease or owner lock.'), auto_eligible=dict(boolean,default=False,description='Explicit automatic allocation consent, separate from desired association.'))
public_network = {k: v for k, v in network.items() if k not in ('password', 'open')}
public_network['has_password'] = dict(boolean, readOnly=True)
meta = {'api_version': {'type': 'string', 'const': '1'}, 'config_revision': string, 'operation_id': {'type': ['string', 'null']}}
connection = obj({'id': ident, 'state': string, 'message': string, 'connected': boolean,
                  'reason': string, 'retry_blocked': boolean,
                  'service': ref('ServiceReadiness'), 'relay_ready': boolean, 'target_ip': string, 'client_ip': string,
                  'interface': string, 'subnet': string, 'legacy_ports': boolean, 'seestar_proxy': boolean, 'dwarf_proxy':boolean, 'zwo_mount_proxy':boolean, 'synscan_proxy':boolean, 'native_proxy_reason':{'type':['string','null']},
                  'client_endpoint':ref('IsolatedClientEndpoint'), 'transport_metrics':ref('IsolatedTransportMetrics'),
                  'management_paths':{'type':'array','maxItems':2,'items':obj({'role':{'enum':['home','ap']},'host':string,'subnet':string,'forwarding_ready':boolean})}})
schemas = {
    'Error': obj({'error': obj({'code': string, 'message': string}, ['code', 'message'])}, ['error']),
    'TokenRequest': obj({'password': dict(string, writeOnly=True, maxLength=128)}, ['password'], additionalProperties=False),
    'Token': obj({'access_token': dict(string, description='Opaque, expires after 12 hours or a service restart.'),
                  'token_type': {'type': 'string', 'const': 'Bearer'}, 'expires_in': {'type': 'integer'}, 'api_version': string}, ['access_token', 'token_type', 'expires_in']),
    'Empty': obj({}, additionalProperties=False),
    'UpdateStatus': obj({'state': string, 'current_version': string, 'version': string,
                         'available': boolean, 'automatic': boolean, 'release_id': {'type': 'integer'},
                         'checked_at': {'type': 'number'}, 'message': string, 'demo': boolean,
                         'current_build_id': {'type': ['string', 'null']}, 'supported': boolean,
                         'maintenance': boolean, 'rollback_service_ready': boolean}),
    'UpdateAction': {'oneOf': [obj({'action': {'const': 'check'}}, ['action'], additionalProperties=False),
                              obj({'action': {'const': 'preferences'}, 'automatic': boolean}, ['action','automatic'], additionalProperties=False),
                              obj({'action': {'const': 'install'}, 'release_id': {'type': 'integer'}}, ['action','release_id'], additionalProperties=False)]},
    'ConnectRequest': obj({'mac': dict(mac, description='Optional destination radio; moves the saved profile and replaces its occupant.'),
                           'expected_mac': dict(telescope['mac'], description='Optional precondition on the current assigned radio.')}, additionalProperties=False),
    'HomePatch': obj(network, additionalProperties=False),
    'TelescopeCreate': obj(dict(telescope, id=ident), ['name', 'ssid', 'target_ip'], additionalProperties=False),
    'TelescopePatch': obj(telescope, additionalProperties=False),
    'ServiceReadiness': obj({'state': {'type': 'string', 'enum': ['ready', 'not_answering', 'not_probed', 'unknown']},
        'checked_at_seq': {'type': ['integer', 'null'], 'minimum': 1},
        'method': {'type': ['string', 'null'], 'enum': ['tcp_connect', 'udp_identity', 'vespera_tcp', None]},
        'failures': {'type': 'integer', 'minimum': 0, 'maximum': 2**31-1}},
        ['state', 'checked_at_seq', 'method', 'failures'], additionalProperties=False),
    'Connection': connection,
    'Telescope': obj(dict({k:v for k,v in telescope.items() if k not in ('password','open')},
                          id=ident, has_password=boolean, client_endpoint={'oneOf':[ref('IsolatedClientEndpoint'),{'type':'null'}]}, connection=ref('Connection'))),
    'TelescopeList': obj(dict(meta, telescopes={'type': 'array', 'items': ref('Telescope')})),
    'TelescopeEnvelope': obj(dict(meta, telescope=ref('Telescope'))),
    'HomeEnvelope': obj(dict(meta, home=obj(dict(public_network, connection=obj({'connected': boolean, 'client_ip': string}))))),
    'HotspotClient': obj({'mac': mac, 'ip': string, 'hostname': string}),
    'HotspotConnection': obj({'active': boolean, 'reason': string, 'interface': string, 'mac': mac, 'ip': string,
                              'clients_available': boolean, 'clients': {'type': 'array', 'items': ref('HotspotClient')}}),
    'HotspotPatch': obj({'enabled': boolean, 'mac': {'type':'string', 'pattern':'^(?:|(?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2})$', 'description':'Permanent MAC of the selected AP-capable adapter; empty selects automatically. Recovery may use another free adapter if private Wi-Fi is unavailable.'}, 'ssid': dict(string, maxLength=32, description='Custom hotspot name (1–32 UTF-8 bytes); empty uses the device name and AP MAC suffix.'),
                         'current_password': dict(string, writeOnly=True, maxLength=128, description='Current bridge password: required to give the hotspot a new name once a personal password is set (409 PASSWORD_REQUIRED otherwise), because only its derived key is stored.')}, additionalProperties=False),
    'HotspotEnvelope': obj(dict(meta, hotspot=obj({'enabled': boolean, 'mac': string, 'ssid': string, 'configured_ssid': string,
                         'connection': ref('HotspotConnection')}))),
    'SettingsPatch': obj({'auto_connect':dict(boolean,default=False), 'hostname': {'type': 'string', 'maxLength': 63}, 'country': {'type': 'string', 'pattern': '^[A-Za-z]{2}$'}}, additionalProperties=False),
    'Defaults': obj({'password': dict(boolean, description='One bridge password: true while the initial password 12345678 is accepted anywhere (web, hotspot or Linux account). web, linux and hotspot repeat it.'),
                     'web': boolean, 'linux': boolean, 'hotspot': boolean}),
    'SettingsEnvelope': obj(dict(meta, settings=ref('SettingsPatch'), default_credentials=ref('Defaults'))),
    'Interface': obj({'name': string, 'mac': mac, 'usb': boolean,
                      'usb_hub': dict(boolean, description='Present and true when this USB Wi-Fi adapter is plugged through a USB hub other than the board own (read from the USB topology). Plug it directly into a port of the board when it has enough ports.'),
                      'bands': {'type': 'array', 'items': string},
                      'ap': boolean, 'label': string, 'available_for_telescope': boolean, 'can_assign_telescope': boolean,
                      'assignments': {'type': 'array', 'items': obj({'role': string, 'id': ident})}}),
    'Interfaces': obj(dict(meta, interfaces={'type': 'array', 'items': ref('Interface')})),
    'Networks': obj({'api_version': string, 'networks': {'type': 'array', 'items': obj({'ssid': string, 'bssid': string,
                    'signal': {'type':'integer'}, 'frequency': {'type':'integer'}, 'security': string})}}),
    'PublicConfig': obj({'hostname': string, 'country': string, 'configured': boolean, 'admin_configured': boolean,
                         'auto_connect': boolean, 'home': obj(public_network), 'hotspot': ref('HotspotPatch'),
                         'telescopes': {'type': 'array', 'items': ref('Telescope')}}),
    'ConfigEnvelope': obj(dict(meta, config=ref('PublicConfig'))),
    'Status': obj(dict(meta, mode=string, message=string, home_ip=string, setup_ssid=string, url=string,
                       management=ref('Management'), config=ref('PublicConfig'), telescopes={'type':'array', 'items':ref('Connection')},
                       devices={'type':'array','items':ref('Interface')}, hotspot=ref('HotspotConnection'),
                       default_credentials=ref('Defaults'), connection_operation={'anyOf':[ref('ConnectionOperation'),{'type':'null'}]}, connection_protection=ref('Protection'), connection_activity=ref('Activity'), last_error=string)),
    'Action': obj(dict(meta, ok=boolean, status_url=string)),
    'OK': obj({'ok': boolean}),
    'Credentials': obj({'target': {'type':'string','enum':['all','web','hotspot','linux'],'default':'all','description':'One bridge password: every target changes the web interface, the hotspot and the Linux account together. linux is refused where the bridge has no Linux account.'},
                        'current_password': dict(string, writeOnly=True, maxLength=128),
                        'password': dict(string, writeOnly=True, minLength=8, maxLength=63, pattern='^[!-~]{8,63}$', description='Printable ASCII without spaces; not 12345678.')},
                       ['current_password','password'], additionalProperties=False),
    'CredentialResult': obj({'ok':boolean, 'sessions_invalidated':boolean, 'hotspot_reconnect':boolean}),
    'Reboot': obj({'ok':boolean, 'reboot_in':{'type':'integer'}}),
    'Info': obj({'product':{'type':'string','const':'astro-bridge'}, 'version':string, 'api_version':string,
                 'authentication':string, 'token_expires_in':{'type':'integer'}, 'capabilities':{'type':'array','items':string}, 'openapi_url':string})
}
schemas['Management'] = obj({'mode':{'enum':['none','home_only','ap_only','dual']},'reason':string,
    'paths':{'type':'array','maxItems':2,'items':obj({'role':{'enum':['home','ap']},'kind':{'type':'string','enum':['wifi','ethernet'],'description':'Optional link medium. Absence on older bridges does not imply Ethernet.'},'interface':string,'ip':string,'subnet':string})},
    'home_retry':obj({'reason':string,'remaining_seconds':{'type':'integer','minimum':0}})})
event_fields = {
    'sequence': {'type': 'integer', 'minimum': 1},
    'boot_id': {'type': 'string', 'pattern': '^[0-9a-f]{32}$'},
    'timestamp': {'type': 'string', 'format': 'date-time', 'description': 'Best-effort device time; use sequence for ordering.'},
    'clock_status': {'type': 'string', 'enum': ['unverified', 'synchronized']},
    'operation_id': string, 'profile_id': ident, 'profile_name': dict(string, maxLength=64),
    'radio_id': {'type': ['string', 'null'], 'description': 'Opaque identifier for this service run, never a MAC address.'},
    'type': {'type': 'string', 'enum': sorted(TYPES)}, 'cause': {'type': 'string', 'enum': sorted(CAUSES)},
    'reason': {'type': 'string', 'enum': sorted(REASONS)},
    'outcome': {'type': 'string', 'enum': ['accepted', 'in_progress', 'associated', 'waiting', 'ready', 'disconnected', 'failed', 'deferred', 'not_answering', 'unknown']},
    'associated': boolean, 'ready': boolean,
}
schemas['ConnectionEvent'] = obj(event_fields, event_fields.keys(), additionalProperties=False)
schemas['ConnectionEvent']['properties']['interfaces'] = {'type': 'array', 'maxItems': 2, 'uniqueItems': True, 'items': {'enum': ['home', 'hotspot']}, 'description': 'Management roles only; present on announcement_updated.'}
schemas['ConnectionEvent']['properties']['utc_corrected'] = dict(string, format='date-time', description='Optional estimate; sequence remains the ordering authority.')
schemas['SystemTime'] = obj({'clock_status': event_fields['clock_status'],
    'system_utc': dict(string, format='date-time'), 'corrected_utc': dict(string, format='date-time'),
    'offset_ms': {'type': 'integer'}, 'source': {'enum': ['client_hint', 'ntp', 'lower_bound']},
    'hint_age_seconds': {'type': ['integer', 'null'], 'minimum': 0},
    'last_sync': obj({'kind': {'enum': ['network', 'client_hint', 'never'], 'description': 'network: chrony measured network time; client_hint: only an approximate time from a client; never: no setting recorded.'},
                      'age_seconds': {'type': ['integer', 'null'], 'minimum': 0, 'description': 'Monotonic seconds since that setting during this run; null when it was not seen since the bridge started.'},
                      'this_run': boolean,
                      'last_network_sync_utc': {'type': ['string', 'null'], 'format': 'date-time', 'description': 'Last network time setting recorded, possibly before a restart. A date to show, never a basis for a duration.'}},
                     ['kind', 'age_seconds', 'this_run', 'last_network_sync_utc'], additionalProperties=False)},
    ['clock_status', 'system_utc', 'corrected_utc', 'offset_ms', 'source', 'hint_age_seconds', 'last_sync'], additionalProperties=False)
schemas['TimeHint'] = obj({'utc_iso': dict(string, format='date-time', maxLength=40),
    'uncertainty_ms': {'type': 'integer', 'minimum': 0, 'maximum': 86400000}},
    ['utc_iso', 'uncertainty_ms'], additionalProperties=False)
schemas['EventPage'] = obj({
    'api_version': string, 'boot_id': event_fields['boot_id'],
    'oldest_sequence': {'type': 'integer'}, 'latest_sequence': {'type': 'integer'},
    'next_cursor': {'type': 'integer'}, 'has_more': boolean, 'gap': boolean,
    'reset_reason': {'type': ['string', 'null'], 'enum': ['BOOT_CHANGED', 'RETENTION_GAP', 'CURSOR_AHEAD', 'HISTORY_UNAVAILABLE', None]},
    'persistent': boolean, 'history_available': boolean, 'clock_status': event_fields['clock_status'],
    'events': {'type': 'array', 'maxItems': 100, 'items': ref('ConnectionEvent')},
}, ['boot_id', 'oldest_sequence', 'latest_sequence', 'next_cursor', 'has_more', 'gap', 'events'])
https_identity = obj({'configured': boolean, 'enabled': boolean,
    'state': {'type': 'string', 'enum': ['unconfigured','valid','invalid','hostname_mismatch','not_yet_valid','ca_expired','leaf_expired']},
    'hostname': {'type': ['string','null']}, 'fingerprint_sha256': {'type': ['string','null'], 'pattern': '^[0-9a-f]{64}$'},
    'ca_expires': {'type': ['string','null'], 'format': 'date-time'}, 'leaf_expires': {'type': ['string','null'], 'format': 'date-time'},
    'issuance_clock_evidence': {'type': ['string','null'], 'enum': [None,'chrony-synchronized','owner-confirmed-current-utc']},
    'current_clock': {'type': 'string', 'enum': ['synchronized','unverified'], 'description': 'Observation at the last diagnostic job, never continuing sync proof.'}})
schemas['HTTPSStatus'] = obj({'api_version': string, 'supported': boolean, 'reason': {'type': ['string','null']},
    'diagnostics_stale': boolean, 'diagnostics_age_seconds': {'type': ['integer','null'], 'minimum': 0},
    'identity': {'oneOf': [https_identity, {'type': 'null'}]},
    'listener': obj({'ready': boolean, 'url': {'type': ['string','null']}, 'error': {'type': ['string','null']}}),
    'operation': {'oneOf': [obj({'id': string, 'action': {'type': 'string','enum': ['setup','renew','enable','disable','reset','refresh']},
       'state': {'type': 'string','enum': ['running','succeeded','failed']}, 'code': {'type': ['string','null']}}), {'type': 'null'}]}})
https_requests = []
for action in ['setup','renew','enable','disable','reset','refresh']:
    fields = {'action': {'type': 'string', 'const': action}}
    required = ['action']
    if action != 'refresh': fields['current_password'] = dict(string, writeOnly=True, maxLength=128); required.append('current_password')
    if action in ('setup','renew'):
        fields['owner_confirmed_utc'] = {'type': 'integer','minimum':1577836800,'maximum':4102444800,
            'description':'Optional one-operation owner assertion; must match RAM UTC within120s. Never sets the clock or proves NTP.'}
    if action == 'reset': fields['confirm_reset'] = {'type':'boolean','const':True}; required.append('confirm_reset')
    https_requests.append(obj(fields, required, additionalProperties=False))
schemas['HTTPSAction'] = {'oneOf': https_requests}

# Physical metadata is read-only through profile CRUD; changes use revision-checked binding.
physical_alias = obj({'ssid':network['ssid'], 'bssid':mac, 'source':{'enum':['owner-confirmed','wifi-observed']},
                      'observed_at':{'type':['integer','null'],'description':'Best-effort device epoch; not synchronized-clock proof.'},
                      'frequency':{'type':'integer','minimum':2300,'maximum':7125,'description':'Optional server-observed last-seen MHz hint, not permanent capability.'}},
                     ['ssid','bssid','source','observed_at'],additionalProperties=False)
schemas['PhysicalBinding'] = obj({'version':{'const':1},'aliases':{'type':'array','minItems':1,'maxItems':8,'items':physical_alias},
    'reported_identity':{'type':'null','description':'Stable manufacturer identity decoder not supported.'},
    'client_observation':{'type':'null','description':'No client-MAC observation claimed.'}},['version','aliases','reported_identity','client_observation'],additionalProperties=False)
schemas['Telescope']['properties']['physical_binding'] = dict(ref('PhysicalBinding'), readOnly=True)
schemas['DiscoveryRequest'] = obj({'mode':{'enum':['wifi','reachable']}},['mode'],additionalProperties=False)
schemas['DiscoveryCandidate'] = obj({'observation_id':{'type':'string','pattern':'^[a-f0-9]{32}$'},'scan_source':{'enum':['fresh','connected_station','cached']},'candidate_id':string,'mode':{'enum':['wifi','reachable']},'ssid':string,'bssid':mac,
    'frequency':{'type':'integer'},'signal':{'type':'integer','minimum':0,'maximum':100},'security':string,
    'observed_at':{'type':'integer'},'clock_reliable':{'const':False},'source':{'enum':['wifi-scan','seestar-scan-iscope']+[p['id']+'-read-only-query' for p in all_plugins() if p['identification'] in ('zwo_model','synscan_version')]},
    'suggested':obj({'kind':telescope['kind'],'name':string,'target_ip':{'type':['string','null']},'password':{'type':'string','description':'Documented manufacturer factory password only; never stored owner credentials.'},'source':{'const':'ssid-hint-and-defaults'}}),
    'address':{'type':'string','format':'ipv4'},'profile_id':ident,'reported_identity':{'type':'null'},'protocol_identity':obj({'family':{'enum':['zwo_mount','synscan']},'model':{'type':['string','null']},'board_version_hex':string,'authentication':{'const':'unverified'}}),'identity_reason':string})
schemas['Status']['properties']['nearby_telescopes']=obj({'candidates':{'type':'array','maxItems':64,'items':obj({'observation_id':{'type':'string','pattern':'^[a-f0-9]{32}$'},'ssid':string,'kind':telescope['kind'],'signal':{'type':'integer','minimum':0,'maximum':100}})},'scan_age_seconds':{'type':['integer','null']},'interval_seconds':{'const':45},'reason':string})
schemas['DiscoveryPage'] = obj({'api_version':string,'candidates':{'type':'array','maxItems':64,'items':ref('DiscoveryCandidate')},
    'scan_source':{'type':['string','null'],'enum':['fresh','connected_station','cached',None]},'reason':{'enum':['OBSERVED','NO_SPARE_RADIO','NO_CANDIDATES','NO_SUPPORTED_REACHABLE_DEVICE']},'expires_in':{'const':120},
    'physical_binding':obj({'supported':{'const':True},'verification':{'const':'observed-or-owner-confirmed'}}),
    'automatic_selection':obj({'supported':{'const':True},'reason':{'const':'EXPLICIT_PROFILE_ENSURE_ONLY'},'consent_required':{'const':True}})})
correction={'append_aliases':boolean,'confirm_replace':boolean,'conflicting_profile_ids':{'type':'array','maxItems':24,'items':ident}}
schemas['BindingRequest'] = {'oneOf':[obj({'remove_bssid':mac},['remove_bssid'],additionalProperties=False),
    obj(dict(correction,candidate_id=string),['candidate_id'],additionalProperties=False),
    obj(dict(correction,owner_confirmed={'const':True},aliases={'type':'array','minItems':1,'maxItems':8,
        'items':obj({'ssid':network['ssid'],'bssid':mac},['ssid','bssid'],additionalProperties=False)}),['owner_confirmed','aliases'],additionalProperties=False)]}
schemas['UdpEndpointOwner'] = obj({'address':dict(string, maxLength=21, description='Controller IPv4:port.'),
    'since_seq':{'type':'integer','minimum':1,'maximum':2**53-1}}, ['address','since_seq'], additionalProperties=False)
ownership = {'owner':{'oneOf':[ref('UdpEndpointOwner'),{'type':'null'}]},
    'refused_clients':{'type':'integer','minimum':0,'maximum':2**53-1},
    'owner_idle_seconds':{'type':'number','exclusiveMinimum':0,'maximum':3600,'default':20}}
schemas['EndpointOwnerRelease'] = obj(dict(meta, **ownership, ok=boolean, id=ident),
    ['api_version','config_revision','ok','id','owner','refused_clients','owner_idle_seconds'])
endpoint_path = {'path': {'enum': ['home', 'hotspot']}, 'host': dict(string, format='ipv4'),
    'port': {'type': 'integer', 'minimum': 1, 'maximum': 65535}, 'transport': {'enum': ['tcp', 'udp']},
    'service': {'enum': ['lx200', 'synscan', 'seestar_native', 'vespera_http', 'vespera_socketio', 'dwarf_ws', 'direct_route', 'dwarf_http', 'dwarf_rtsp']},
    'ready': boolean, 'service_state': schemas['ServiceReadiness']['properties']['state'],
    'requires_client_route': boolean}
schemas['EndpointPath'] = obj(dict(endpoint_path, owner=ownership['owner'],
    refused_clients=dict(ownership['refused_clients'], description='Isolated UDP only: shared per-profile refused datagram count, repeated on home/hotspot paths.')), endpoint_path.keys(), additionalProperties=False)
from wifi_bridge.plugins import PORT_ROLES, PORT_PROTOCOLS
port_purpose = {'role': {'enum': list(PORT_ROLES), 'description': 'Declared purpose of this port (contract 1.1). Unknown future values must be treated as other.'},
                'protocol': {'enum': list(PORT_PROTOCOLS), 'description': 'Declared application protocol of this port (contract 1.1).'}}
schemas['EndpointPath']['properties'].update(port_purpose)
schemas['ProfileEndpoints'] = obj({'profile_id': ident, 'kind': telescope['kind'],
    'target': obj({'ip': dict(string, format='ipv4'), 'ports': {'type': 'array', 'maxItems': 16,
        'items': {'type': 'integer', 'minimum': 1, 'maximum': 65535}}}, ['ip', 'ports'], additionalProperties=False),
    'paths': {'type': 'array', 'maxItems': 64, 'items': ref('EndpointPath')}},
    ['profile_id', 'kind', 'target', 'paths'], additionalProperties=False)
schemas['EndpointInventory'] = obj({'api_version': meta['api_version'], 'config_revision': string,
    'boot_id': event_fields['boot_id'], 'endpoints': {'type': 'array', 'maxItems': 24, 'items': ref('ProfileEndpoints')}},
    ['api_version', 'config_revision', 'boot_id', 'endpoints'], additionalProperties=False)
schemas['IsolatedClientEndpoint'] = obj({'host':{'type':['string','null']},'port':{'type':'integer'},
    'transport':{'enum':['tcp','udp']},'protocol':{'enum':['lx200','synscan','vespera_http','vespera_socketio']},'ready':boolean,'reason':string,
    'service':string,'target_port':{'type':'integer','description':'Instrument port this client port relays to.'},
    **ownership,'simulated':boolean,'alternatives':{'type':'array','maxItems':2,'description':'One entry per management path for the first relayed port.','items':obj({'role':{'enum':['home','ap']},
        'host':string,'port':{'type':'integer'},'transport':string,'protocol':string,'service':string,'target_port':{'type':'integer'},'ready':boolean,'reason':string,**ownership})}})
schemas['IsolatedClientEndpoint']['properties']['services'] = {'type':'array','maxItems':4,
    'description':'Multi-port isolated instruments (Vespera 8082 HTTP and 8083 Socket.IO): every relayed port on every management path.',
    'items':schemas['IsolatedClientEndpoint']['properties']['alternatives']['items']}
schemas['IsolatedTransportMetrics'] = obj({'bytes_to_instrument':{'type':'integer'},'bytes_from_instrument':{'type':'integer'},
    'bytes':{'type':'integer'},'tcp_established':{'type':'integer'},'relay_errors':{'type':'integer'},
    'last_exchange':{'type':['string','null']},'ping':obj({'method':{'const':'icmp_echo'},'samples':{'type':'integer'},
        'replies':{'type':'integer'},'median_ms':{'type':['number','null']},'loss_percent':{'type':['number','null']},
        'window_seconds':{'const':600},'available':boolean,'reason':{'type':['string','null']}})})
schemas['ConnectionEndpoint'] = obj({'profile_id':ident,'target_ip':{'type':'string','format':'ipv4'},'subnet':string,
    'client_endpoint':ref('IsolatedClientEndpoint'),'bridge_instrument_ip':string,'native_proxy_reason':{'type':['string','null']},'management_endpoints':{'type':'array','maxItems':2,'items':obj({'role':{'enum':['home','ap']},'host':string,'subnet':string,'proxy':{'oneOf':[{'type':'null'},obj({'host':string,'ports':{'type':'array','items':{'type':'integer'}},'protocol':{'enum':[p['id'] for p in relay_plugins()]},'transport':{'enum':['tcp','udp']}})]}})},'proxy':{'oneOf':[{'type':'null'},obj({'host':string,'ports':{'type':'array','items':{'type':'integer'}},'protocol':{'enum':[p['id'] for p in relay_plugins()]},'transport':{'enum':['tcp','udp']}})]},
    'direct_route':obj({'routability':{'const':'unknown'},'route_may_be_required':{'const':True}}),
    'manufacturer_identity':{'const':'unknown'},'maker_app_acceptance':{'const':'unverified'},'service_readiness':{'enum':['checked','not_probed']}})
schemas['ConnectionOperation'] = obj({'api_version':string,'id':string,'boot_id':string,'profile_id':ident,
    'cause':{'enum':['AUTOMATIC','SEESTAR_DISCOVERY','VESPERA_CONNECTION','DWARF_DISCOVERY','ZWO_MOUNT_CONNECTION','SYNSCAN_DISCOVERY']},'state':{'enum':['running','succeeded','failed']},'phase':string,'reason':string,'age_seconds':{'type':'integer'},
    'endpoint':{'oneOf':[ref('ConnectionEndpoint'),{'type':'null'}]},'endpoint_current':boolean,
    'physical_selection':{'oneOf':[{'type':'null'},obj({'bssid':mac,'frequency':{'type':'integer'},'verification':{'const':'wifi-observed-not-manufacturer-verified'}})]},
    'restoration':obj({'state':{'enum':['not_needed','in_progress','restored','failed','superseded']},'reason':{'type':['string','null']}}),'status_url':string,'failure_stage':{'enum':['NETWORK_NOT_FOUND','NOT_ASSOCIATED','ADDRESS_NOT_OBTAINED']},'displaced':obj({'profile_id':ident,'reason':{'enum':['QUIET_REPLACEMENT','FORCED_INTERRUPTION','UNREACHABLE','STANDBY','FORCED']}})})
schemas['EnsureResult'] = {'oneOf':[ref('ConnectionOperation'),obj({'state':{'const':'ready'},'profile_id':ident,'endpoint':ref('ConnectionEndpoint'),'api_version':string})]}
schemas['InterfaceRoles'] = {'type':'object','maxProperties':64,'propertyNames':mac,
    'additionalProperties':{'enum':['auto','home','hotspot','telescope']},
    'description':'Persistent purposes by permanent MAC. One home and one hotspot reservation at most. Actual revision required to change roles; stopped services retain their reservation.'}
for name in ('SettingsPatch','PublicConfig'):
    schemas[name]['properties']['interface_roles']=ref('InterfaceRoles')
schemas['Interface']['properties']['configured_role']={'enum':['auto','home','hotspot','telescope']}
schemas['PublicConfig']['properties']['auto_connect']=dict(boolean,default=False)
schemas['Telescope']['properties']['owner_locked']=dict(boolean,readOnly=True,default=False)
schemas['Lease'] = obj({'id':dict(string,pattern='^[0-9a-f]{32}$'),'profile_id':ident,'version':{'type':'integer','minimum':1},
    'remaining_seconds':{'type':'integer','minimum':0,'maximum':900},'owned':boolean,'config_revision':string})
schemas['Protection'] = obj({'api_version':string,'leases':{'type':'array','maxItems':32,'items':ref('Lease')},
    'owner_locks':{'type':'array','maxItems':24,'items':ident},'restart_boundary':{'const':'RAM_LEASES_LOST_OWNER_LOCKS_PERSIST'}})
schemas['LeaseCreate'] = obj({'ttl_seconds':{'type':'integer','minimum':30,'maximum':900,'default':120}},additionalProperties=False)
schemas['LeaseRenew'] = obj({'version':{'type':'integer','minimum':1},'ttl_seconds':{'type':'integer','minimum':30,'maximum':900,'default':120}},['version'],additionalProperties=False)
schemas['LeaseRelease'] = obj({'version':{'type':'integer','minimum':1}},['version'],additionalProperties=False)
schemas['OwnerRelease'] = obj({'version':{'type':'integer','minimum':1},'confirm_release':{'const':True},'current_password':dict(string,writeOnly=True,maxLength=128)},['version','confirm_release','current_password'],additionalProperties=False)
schemas['OwnerLock'] = obj({'locked':boolean,'current_password':dict(string,writeOnly=True,maxLength=128)},['locked','current_password'],additionalProperties=False)
schemas['ActivityProfile'] = obj({'state':{'enum':['unsupported','unknown','active','quiet']},'reason':string,
    'packets':{'type':['integer','null']},'bytes':{'type':['integer','null']},'tcp_established':{'type':['integer','null']},
    'generation':{'type':'integer'},'sample_age_seconds':{'type':'integer'},'quiet_seconds':{'type':['integer','null']},
    'last_activity_age_seconds':{'type':'integer'},'quiet_required_seconds':{'type':'integer','minimum':30,'maximum':3600},'cooldown_seconds':{'type':'integer','minimum':30,'maximum':3600},'cooldown_remaining':{'type':'integer'}})
schemas['Activity'] = obj({'api_version':string,'supported':boolean,'reason':{'type':['string','null']},
    'profiles':{'type':'object','maxProperties':24,'additionalProperties':ref('ActivityProfile')},'idle_selection_implemented':{'const':True}})
schemas['AutoPolicy'] = obj({'quiet_seconds':{'type':'integer','minimum':30,'maximum':3600,'default':120},
    'cooldown_seconds':{'type':'integer','minimum':30,'maximum':3600,'default':60},
    'protect_running_operations':dict(boolean,default=False),'force_first_profile':dict(boolean,default=False),'seestar_demand':dict(boolean,default=True),'vespera_demand':dict(boolean,default=True),'dwarf_demand':dict(boolean,default=True),'zwo_mount_demand':dict(boolean,default=True),'synscan_demand':dict(boolean,default=True)},additionalProperties=False)
schemas['SettingsPatch']['properties'].update(auto_policy=ref('AutoPolicy'),acknowledge_interruption=dict(boolean,writeOnly=True,description='Required true when enabling running-operation protection, force or manufacturer demand; actual revision required for every policy change.'))
schemas['PublicConfig']['properties']['auto_policy']=ref('AutoPolicy')
schemas['AutoPolicy']['properties']['address_fallback']=dict(boolean,default=True,description='Owner switch, absent until set: when an instrument whose address plan is fixed and known (a SynScan mount: 192.168.4.1/24) accepts the Wi-Fi link but gives no address, the bridge takes a free address of that network itself, after checking that nobody answers for it and that the instrument answers. The instrument entry then says address_fallback.')
schemas['AutoPolicy']['properties']['night_reservations']=dict(boolean,default=True,description='Owner switch: paired apps may hold night reservations (reserve_seconds). False answers 403 RESERVATIONS_DISABLED.')
station_fields={'name':{'type':'string','minLength':1,'maxLength':64},'kind':{'enum':['seestar','dwarf']},
    'target_ip':dict(string,format='ipv4',description='Instrument address on the bridge network (hotspot DHCP lease or private LAN).'),
    'client_connect':dict(boolean,default=True)}
schemas['Station']=obj(dict(station_fields,id=ident,access={'const':'station'},
    reachable={'type':['boolean','null'],'description':'Last TCP handshake result; null before the first check.'},
    rtt_ms={'type':['number','null']}),['id','name','kind','access','target_ip','client_connect'])
schemas['StationCreate']=obj(dict(station_fields,id=ident),['name','kind','target_ip'],additionalProperties=False)
schemas['StationPatch']=obj(dict(station_fields),additionalProperties=False)
schemas['StationEnvelope']=obj(dict(meta,station=ref('Station')))
schemas['StationList']=obj(dict(meta,stations={'type':'array','items':ref('Station'),'maxItems':16}))
schemas['StationDeleted']=obj({'api_version':string,'deleted':boolean,'id':ident})
schemas['PublicConfig']['properties']['stations']={'type':'array','items':ref('Station'),'maxItems':16}
# Serial mounts (not Wi-Fi): a serial adapter relayed as one TCP access point per mount. Management additions, not contract changes.
serial_fields={'name':{'type':'string','minLength':1,'maxLength':64},'profile':{'enum':['nexstar']},
    'device':dict(string,description='Stable udev link of the adapter: /dev/serial/by-id/... (adapter with a serial number) or /dev/serial/by-path/... (USB socket). Never a bare /dev/ttyUSBn.'),
    'tcp_port':{'type':'integer','minimum':2000,'maximum':2099,'description':'Assigned from 2000 upward when omitted; kept afterwards.'},
    'baud':{'enum':[1200,2400,4800,9600,19200,38400,57600,115200]},'format':{'type':'string','pattern':'^[78][NEO][12]$'},
    'idle_timeout_seconds':{'type':'integer','description':'0 disables; otherwise 5 to 3600. The idle client is dropped and the stop sequence is sent.'}}
serial_identity=obj({'responding':boolean,'hand_control_version':{'type':['string','null']},'model_code':{'type':['integer','null']},'model_name':{'type':['string','null']},
    'board_versions':{'type':'object'},'read_at':string,'source':{'const':'bridge_read'}})
schemas['SerialMount']=obj(dict(serial_fields,id=ident,kind={'const':'mount'},link_type={'const':'serial_usb'},
    declared_identity={'type':'object'},state={'enum':['starting','idle','client','identifying','stopping','adapter_missing','error','stopped','demo']},
    reason={'type':['string','null']},adapter_present={'type':['boolean','null']},
    client={'type':['object','null'],'description':'The single client holding the link.'},
    refused_clients={'type':'integer'},last_refused={'type':['object','null']},stops_sent={'type':'integer'},last_stop={'type':['object','null']},stop_pending={'type':'boolean'},
    identification={'enum':['identified','not_identified','unknown']},identity=dict(serial_identity,type=['object','null']),identity_mismatch=boolean,
    client_setup=obj({'port':{'type':'integer'},'transport':{'const':'tcp'},'host':{'type':'null'},'preset':string}),journal={'type':'array','items':{'type':'object'}}),
    ['id','name','profile','device','tcp_port','link_type','state'])
schemas['SerialMountCreate']=obj(dict(serial_fields,id=ident,declared_identity={'type':'object'}),['device'],additionalProperties=False)
schemas['SerialMountPatch']=obj({k:v for k,v in serial_fields.items() if k in ('name','tcp_port','baud','format','idle_timeout_seconds')},additionalProperties=False)
schemas['SerialMountEnvelope']=obj(dict(meta,serial_mount=ref('SerialMount')))
schemas['SerialMountList']=obj(dict(meta,serial_mounts={'type':'array','items':ref('SerialMount'),'maxItems':8}))
schemas['SerialMountDeleted']=obj({'api_version':string,'deleted':boolean,'id':ident})
schemas['SerialDevice']=obj({'node':string,'stable_path':{'type':['string','null']},'stable_kind':{'enum':['by-id','by-path',None]},'by_path':{'type':['string','null']},
    'vendor_id':{'type':['string','null']},'product_id':{'type':['string','null']},'manufacturer':{'type':['string','null']},'product':{'type':['string','null']},
    'serial_number':{'type':['string','null']},'unique_by_id':boolean,'link_type':{'const':'serial_usb'},'can_add':boolean,
    'identification':{'enum':['identified','not_identified','unknown']},'identity':dict(serial_identity,type=['object','null']),
    'proposal':obj(dict(serial_fields,declared_identity={'type':'object'}))})
schemas['SerialDeviceList']=obj(dict(meta,serial_devices={'type':'array','items':ref('SerialDevice')}))
schemas['SerialIdentifyRequest']=obj({'device':string},['device'],additionalProperties=False)
schemas['SerialIdentity']=obj(dict(meta,identity=dict(serial_identity,type=['object','null'])))
schemas['SerialProfileList']=obj(dict(meta,serial_profiles={'type':'array','items':{'type':'object'}}))
schemas['DeclaredDevice']=obj({'id':ident,'name':string,'family':string,'kind':{'enum':['mount','smart_telescope','camera','other']},'link_type':{'enum':['wifi','serial_usb','usb']},
    'access':string,'state':{'enum':['online','offline','unknown']},'client_connected':{'type':['boolean','null']},
    'endpoint':{'type':['object','null'],'description':'host is null: use the bridge address of the network the app reached.'},'detail':{'type':'object'}},
    ['id','name','kind','link_type','state'])
schemas['DeviceList']=obj(dict(meta,devices={'type':'array','items':ref('DeclaredDevice')},detected={'type':'array','items':ref('SerialDevice')}))
schemas['PublicConfig']['properties']['serial_mounts']={'type':'array','items':ref('SerialMount'),'maxItems':8}
schemas['ConnectionPauses'] = obj({'recovery_required':boolean,'reason':{'type':['string','null']},
    'profiles':{'type':'object','maxProperties':24,'additionalProperties':string}})
schemas['PauseRecovery'] = obj({'confirm_recovery':{'const':True},'current_password':dict(string,writeOnly=True,maxLength=128)},['confirm_recovery','current_password'],additionalProperties=False)
schemas['Status']['properties'].update(connection_pauses=ref('ConnectionPauses'),maker_demand=obj({'reason':string,'age_seconds':{'type':'integer'},'next_attempt_seconds':{'type':'integer'},'protocol':{'const':'seestar-scan-iscope'},'manufacturer_acceptance':{'const':'unverified'}}))
schemas['Status']['properties']['maker_demands'] = obj({'seestar':schemas['Status']['properties']['maker_demand'], 'vespera':obj({'supported':boolean,'observer':string,'reason':string,'age_seconds':{'type':'integer'},'next_attempt_seconds':{'type':'integer'},'protocol':{'const':'vespera-tcp-syn'},'manufacturer_acceptance':{'const':'unverified'}})})
schemas['Status']['properties']['maker_demands']['properties']['dwarf'] = obj({'passive_only':{'const':True},'replies_disabled':{'const':True},'supported':boolean,'observer':string,'reason':string,'age_seconds':{'type':'integer'},'next_attempt_seconds':{'type':'integer'},'protocol':{'const':'dwarf-udp-ping'},'manufacturer_acceptance':{'const':'unverified'},'maker_app_acceptance':{'const':'unverified'},'discovery_relay':obj({'passive_only':{'const':True},'replies_disabled':{'const':True},'supported':{'const':False},'last_result':{'const':'REPLIES_DISABLED'},'last_result_age_seconds':{'type':['integer','null']},'proxy_available':boolean,'relay_plan_available':{'const':False},'maker_app_acceptance':{'const':'unverified'}})})
for p in demand_plugins():
    kind=p['id']
    if kind in ('seestar','vespera','dwarf'):continue
    protocol={'zwo_mount':'zwo-mount-tcp-syn','synscan':'synscan-version-query'}.get(kind,p['discovery']['engine'])
    schemas['Status']['properties']['maker_demands']['properties'][kind]=obj({'supported':boolean,'observer':string,'reason':string,'age_seconds':{'type':'integer'},'next_attempt_seconds':{'type':'integer'},'protocol':{'const':protocol},'manufacturer_acceptance':{'const':'unverified'}})
# Explicit access policy, never inferred from an existing dual-management setup.
access_mode={'type':'string','enum':['private','hotspot_isolated','hotspot_shared'],'default':'private'}
for schema in ('SettingsPatch','PublicConfig'):
    schemas[schema]['properties'].update(connection_mode=access_mode,hotspot_private_lan=dict(boolean,default=False))
schemas['NetworkAccess']=obj({'mode':access_mode,'configured':boolean,'available':boolean,
    'reason':string,'private_lan_allowed':boolean,'internet_verified':{'const':False}})
schemas['Status']['properties']['network_access']=ref('NetworkAccess')
schemas['Status']['properties']['ssh']=obj({'account':{'type':['string','null'],'enum':['root','bridge',None],'description':'Linux account protected by the bridge password.'},
                                            'password_login':{'type':['boolean','null'],'description':'What SSH accepts now, written by the image rule; null when this host has no such rule.'}},['account','password_login'])
schemas['SettingsEnvelope']['properties']['network_access']=ref('NetworkAccess')
schemas['ActivityProfile']['properties'].update(
    consultation={'enum':['unknown','background_control','mount_control','probable_content','none','unclassified']},raw_active={'type':['boolean','null']},interval_seconds={'type':['number','null']},
    interval_packets={'type':['integer','null']},interval_bytes={'type':['integer','null']},
    classifier=string,calibrated=boolean)
schemas['PluginManifest']=manifest_schema()
schemas['PluginCatalog']=obj(dict(meta,schema_version={'const':1},plugins={'type':'array','items':ref('PluginManifest'),'maxItems':32}))
for p in relay_plugins():schemas['Connection']['properties'][proxy_flag(p['id'])]=boolean
for p in demand_plugins():schemas['AutoPolicy']['properties'][p['id']+'_demand']=dict(boolean,default=True)
nullable_number = {'type': ['number', 'null'], 'minimum': 0}
nullable_integer = {'type': ['integer', 'null'], 'minimum': 0}
latency = obj({'samples': {'type': 'integer', 'minimum': 0}, 'p50_ms': nullable_number,
               'p95_ms': nullable_number}, ('samples', 'p50_ms', 'p95_ms'), additionalProperties=False)
relay_metrics = obj({'profile_id': {'anyOf': [ident, {'type': 'null'}]}, 'host': string, 'port': {'type': 'integer'},
    'service': {'enum': ['instrument', 'seestar_discovery']}, 'transport': {'enum': ['tcp', 'udp']},
    **{name: {'type': 'integer', 'minimum': 0, 'maximum': 2**53-1} for name in
       ('bytes_to_instrument', 'bytes_from_instrument', 'datagrams', 'drops')},
    'forwarding_latency': latency}, ('profile_id', 'host', 'port', 'transport', 'service',
    'bytes_to_instrument', 'bytes_from_instrument', 'datagrams', 'drops', 'forwarding_latency'), additionalProperties=False)
schemas['SystemMetrics'] = obj({'api_version': {'const': '1'}, 'sample_sequence': {'type': 'integer'},
    'sample_interval_seconds': {'const': 5}, 'sample_age_seconds': {'type': 'number', 'minimum': 0},
    'process': obj({'rss_bytes': nullable_integer, 'threads': nullable_integer,
        'cpu_percent': obj({'5s': nullable_number, '60s': nullable_number}, ('5s', '60s'), additionalProperties=False)},
        ('rss_bytes', 'threads', 'cpu_percent'), additionalProperties=False),
    'system': obj({'load_average': {'anyOf': [{'type': 'null'}, {'type': 'array', 'items': {'type': 'number', 'minimum': 0}, 'minItems': 3, 'maxItems': 3}]},
        'memory_free_bytes': nullable_integer, 'memory_available_bytes': nullable_integer},
        ('load_average', 'memory_free_bytes', 'memory_available_bytes'), additionalProperties=False),
    'http_loop_lag': latency, 'relays': {'type': 'array', 'items': relay_metrics, 'maxItems': 50},
    'relay_coverage': {'const': 'userspace_only'}}, ('api_version', 'sample_sequence',
    'sample_interval_seconds', 'sample_age_seconds', 'process', 'system', 'http_loop_lag', 'relays', 'relay_coverage'), additionalProperties=False)
paths = {}
revision = {'name':'If-Match','in':'header','required':False,'schema':string,
            'description':'Optional quoted config_revision from a previous read. A stale revision returns 412 without saving.'}
id_param = {'name':'id','in':'path','required':True,'schema':ident}


def add(path, method, operation, summary, response, request=None, status=200, public=False, conditional=False):
    value = {'operationId':operation,'summary':summary,'responses':{
        str(status): {'description':'Success','content':{'application/json':{'schema':ref(response)}}},
        **{str(code): {'description':description,'content':{'application/json':{'schema':ref('Error')}}}
           for code,description in [(400,'Invalid request'),(401,'Authentication required'),(403,'Origin refused'),
                                    (404,'Resource not found'),(409,'Conflict or network busy'),(412,'Stale revision'),(429,'Rate limited')]}}}
    if public:
        value['security'] = []
    if request:
        value['requestBody'] = {'required': True,'content':{'application/json':{'schema':ref(request)}}}
    params = ([id_param] if '{id}' in path else []) + ([revision] if conditional else [])
    if params:
        value['parameters'] = params
    paths.setdefault('/api/v1' + path, {})[method] = value


add('/endpoints','get','getEndpoints','Read current instrument paths (read or administrator scope)','EndpointInventory')
add('/endpoints/{id}','get','getProfileEndpoints','Read current paths for one saved profile (read or administrator scope)','EndpointInventory')
add('/endpoints/{id}/release-owner','post','releaseEndpointOwner','Release the instrument UDP controller (administrator scope)',
    'EndpointOwnerRelease','Empty',conditional=True)
release_owner = paths['/api/v1/endpoints/{id}/release-owner']['post']
release_owner['x-required-scopes'] = ['admin']
release_owner['parameters'][-1] = dict(revision, required=True, description='Actual current quoted config_revision; wildcard refused.')
release_owner['responses']['428'] = {'description':'Actual current configuration revision required','content':{'application/json':{'schema':ref('Error')}}}
add('/info','get','getInfo','Identify the bridge and its capabilities','Info',public=True)
add('/openapi.json','get','getOpenAPI','Download the API contract','OK',public=True)
paths['/api/v1/openapi.json']['get']['responses']['200']['content']['application/json']['schema']={'type':'object'}
add('/auth/token','post','createToken','Sign in using the current web administrator password','Token','TokenRequest',public=True)
add('/auth/token','delete','revokeToken','Revoke only this mobile session','OK')
add('/auth/revoke-all','post','revokeAllTokens','Revoke all API tokens and browser sessions','OK','Empty')
for path, name, response in [('/status','Status','Status'),('/config','Config','ConfigEnvelope'),
                             ('/interfaces','Interfaces','Interfaces'),('/home','Home','HomeEnvelope'),
                             ('/hotspot','Hotspot','HotspotEnvelope'),('/settings','Settings','SettingsEnvelope'),
                             ('/telescopes','Telescopes','TelescopeList'),('/plugins','Plugins','PluginCatalog')]:
    add(path,'get','get'+name,'Read '+name.lower(),response)
for path, name, response, request in [('/home','Home','HomeEnvelope','HomePatch'),('/hotspot','Hotspot','HotspotEnvelope','HotspotPatch'),
                                     ('/settings','Settings','SettingsEnvelope','SettingsPatch')]:
    add(path,'patch','update'+name,'Partially update '+name.lower(),response,request,conditional=True)
add('/events','get','getConnectionEvents','Replay confirmed connection events; requests do not imply readiness','EventPage')
paths['/api/v1/events']['get']['parameters'] = [
    {'name': 'after', 'in': 'query', 'required': False, 'schema': {'type': 'integer', 'minimum': 0, 'maximum': 2**63-1}, 'description': 'Last processed sequence. Omit for the latest bounded activity snapshot.'},
    {'name': 'boot_id', 'in': 'query', 'required': False, 'schema': event_fields['boot_id'], 'description': 'Previous service epoch; a changed boot resets replay explicitly.'},
    {'name': 'limit', 'in': 'query', 'required': False, 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}},
]
add('/wifi/networks','get','scanWiFi','Scan a detected radio; retry 409 when busy','Networks')
paths['/api/v1/wifi/networks']['get']['parameters']=[{'name':'interface','in':'query','required':True,'schema':string,'description':'Current interface name from GET /interfaces, for example wlan0.'}]
add('/telescopes','post','createTelescope','Save an instrument profile; inactive by default','TelescopeEnvelope','TelescopeCreate',status=201,conditional=True)
add('/telescopes/{id}','get','getTelescope','Read saved preferences and live connection state','TelescopeEnvelope')
add('/telescopes/{id}','patch','updateTelescope','Edit one profile without replacing other profiles','TelescopeEnvelope','TelescopePatch',conditional=True)
add('/telescopes/{id}','delete','deleteTelescope','Delete profile and queue connection cleanup','Action',conditional=True)
add('/stations','get','getStations','Station instruments joined to the bridge network, with last reachability','StationList')
add('/stations','post','createStation','Declare a station instrument (no radio, no Wi-Fi credentials)','StationEnvelope','StationCreate',status=201,conditional=True)
add('/stations/{id}','get','getStation','Read one station instrument','StationEnvelope')
add('/stations/{id}','patch','updateStation','Edit a station instrument','StationEnvelope','StationPatch',conditional=True)
add('/stations/{id}','delete','deleteStation','Remove a station instrument without reservations','StationDeleted',conditional=True)
for action in ('connect','disconnect'):
    add('/telescopes/{id}/'+action,'post',action+'Telescope',action.title()+' this instrument; poll its connection state','Action','ConnectRequest' if action == 'connect' else 'Empty',status=202,conditional=True)
add('/discovery/{observation}','get','getDiscoveredWifi','Read a retained Wi-Fi observation and current plugin defaults for the add form','DiscoveryCandidate')
paths['/api/v1/discovery/{observation}']['get']['parameters']=[{'name':'observation','in':'path','required':True,'schema':{'type':'string','pattern':'^[a-f0-9]{32}$'}}]
add('/discovery','post','discoverCandidates','Discover without interrupting management or telescope connections','DiscoveryPage','DiscoveryRequest')
add('/telescopes/{id}/binding','get','getBinding','Read saved profile and separate physical binding','TelescopeEnvelope')
add('/telescopes/{id}/binding','post','bindIdentity','Bind or explicitly correct physical aliases; connection unchanged','TelescopeEnvelope','BindingRequest',conditional=True)
add('/telescopes/{id}/binding','delete','unlinkIdentity','Unlink physical identity; Wi-Fi selector and profile ID unchanged','TelescopeEnvelope',conditional=True)
for method in ('post','delete'):
    endpoint=paths['/api/v1/telescopes/{id}/binding'][method]
    endpoint['parameters']=[id_param,dict(revision,required=True,description='Actual quoted current config_revision. Wildcard is refused; missing returns 428, stale 412.')]
    endpoint['responses']['428']={'description':'Actual configuration revision required','content':{'application/json':{'schema':ref('Error')}}}
add('/telescopes/{id}/ensure','post','ensureTelescope','Read ready connection or request free/consenting quiet/advanced replacement with actual outcomes','EnsureResult','Empty',status=202,conditional=True)
endpoint=paths['/api/v1/telescopes/{id}/ensure']['post']
endpoint['responses']['200']={'description':'Already ready; no mutation or consent required','content':{'application/json':{'schema':ref('EnsureResult')}}}
endpoint['parameters']=[id_param,dict(revision,description='Actual current revision required for a new allocation; wildcard refused. Same pending operation coalesces with original/current revision.')]
endpoint['responses']['428']={'description':'Actual current configuration revision required','content':{'application/json':{'schema':ref('Error')}}}
add('/protection','get','getProtection','Read reservations/owner locks with ownership relative to this session','Protection')
add('/activity','get','getActivity','Read cached bounded forwarded-path observations, never proof an exposure ended','Activity')
for path,method,operation,summary,response,request,conditional in [
    ('/telescopes/{id}/leases','post','reserveConnection','Reserve an already ready connection','Lease','LeaseCreate',True),
    ('/leases/{id}','patch','renewReservation','Renew this session reservation with exact record version','Lease','LeaseRenew',False),
    ('/leases/{id}','delete','releaseReservation','Release this session reservation with exact record version','OK','LeaseRelease',False),
    ('/leases/{id}/release','post','ownerReleaseReservation','Explicit owner release, reauthentication and current version required','OK','OwnerRelease',True),
    ('/telescopes/{id}/owner-lock','patch','setOwnerLock','Persist explicit owner lock after reauthentication','OK','OwnerLock',True)]:
    add(path,method,operation,summary,response,request,conditional=conditional)
    if path.startswith('/leases/'):
        paths['/api/v1'+path][method]['parameters']=[dict(id_param,schema=dict(string,pattern='^[0-9a-f]{32}$'))]+([dict(revision,required=True)] if conditional else [])
    if conditional:
        paths['/api/v1'+path][method]['parameters']=[dict(parameter,required=True,description='Actual current revision; wildcard refused.') if parameter['name']=='If-Match' else parameter for parameter in paths['/api/v1'+path][method]['parameters']]
        paths['/api/v1'+path][method]['responses']['428']={'description':'Actual current configuration revision required','content':{'application/json':{'schema':ref('Error')}}}
add('/system/connection-pauses/recover','post','recoverConnectionPauses','Reauthenticate and rebuild paused holds for every current instrument; never reconnects or releases protection','ConnectionPauses','PauseRecovery',conditional=True)
add('/operations/{id}','get','getConnectionOperation','Read bounded service-run operation status; no mutation replay','ConnectionOperation')
paths['/api/v1/operations/{id}']['get']['parameters']=[dict(id_param,schema=string)]
paths['/api/v1/operations/{id}']['get']['responses']['410']={'description':'Operation expired or service run changed','content':{'application/json':{'schema':ref('Error')}}}
add('/credentials','post','changePassword','Change the single bridge password (web interface, hotspot and Linux account)','CredentialResult','Credentials')
add('/system/https','get','getHTTPS','Read cached certificate diagnostics and actual listener readiness','HTTPSStatus')
add('/system/https','post','manageHTTPS','Queue one bounded certificate operation; accepted is not ready','HTTPSStatus','HTTPSAction',status=202)
add('/system/https/certificate','get','getPublicCA','Download the authenticated public CA certificate only','OK')
paths['/api/v1/system/https/certificate']['get']['responses']['200']['content'] = {'application/x-pem-file': {'schema': {'type':'string','format':'binary'}}}
add('/system/updates','get','getUpdates','Read application release/update status','UpdateStatus')
add('/system/updates','post','updateSystem','Check releases, set automatic policy or install the checked release','Action','UpdateAction',status=202)
add('/system/reboot','post','reboot','Schedule a reboot; association may delay shutdown','Reboot','Empty',status=202)
add('/system/metrics','get','getSystemMetrics','Read cached local load measurements (administrator scope)','SystemMetrics')
add('/system/time','get','getSystemTime','Read system UTC and best-effort correction without changing the clock','SystemTime')
add('/system/time-hint','post','hintSystemTime','Supply an unverified UTC estimate (read scope); synchronized clocks ignore it','SystemTime','TimeHint')
for path, method in [('/system/time','get'), ('/system/time-hint','post')]:
    paths['/api/v1' + path][method].update(tags=['native-client'], **{'x-contract-status': 'implemented'})
add('/devices','get','getDevices','Every declared device with the nature of its link (wifi, serial_usb, usb) and its state, plus detected undeclared serial adapters','DeviceList')
add('/serial-profiles','get','getSerialProfiles','Serial device profiles the bridge can relay','SerialProfileList')
add('/serial-devices','get','getSerialDevices','Serial adapters seen but not declared, with what the bridge knows and a ready proposal','SerialDeviceList')
add('/serial-devices/identify','post','identifySerialDevice','Read-only identification of a detected adapter (never moves or sets anything)','SerialIdentity','SerialIdentifyRequest')
add('/serial-mounts','get','getSerialMounts','Declared serial mounts with state, relay port and the single client','SerialMountList')
add('/serial-mounts','post','createSerialMount','Declare a serial mount: nothing is relayed before this call','SerialMountEnvelope','SerialMountCreate',status=201,conditional=True)
add('/serial-mounts/{id}','get','getSerialMount','Read one serial mount','SerialMountEnvelope')
add('/serial-mounts/{id}','patch','updateSerialMount','Rename a serial mount or change its settings (settings are refused while a client holds the link)','SerialMountEnvelope','SerialMountPatch',conditional=True)
add('/serial-mounts/{id}','delete','deleteSerialMount','Remove a serial mount (409 LINK_HELD while a client holds the link)','SerialMountDeleted',conditional=True)
add('/serial-mounts/{id}/identify','post','identifySerialMount','Read-only identification, only while no client holds the link (409 LINK_HELD otherwise)','SerialIdentity','Empty')
from native_client_openapi import extend
extend(schemas, paths)
from wifi_bridge.api import MobileAPI
for path, methods in paths.items():
    for method, entry in methods.items():
        scope = MobileAPI.required_scope(method.upper(), path.removeprefix('/api/v1'))
        entry['x-required-scopes'] = [] if scope is None else [scope]
        if scope is not None:
            entry['responses'].setdefault('403', {'description': 'Insufficient scope', 'content': {'application/json': {'schema': ref('Error')}}})

spec={'openapi':'3.1.0','info':{'title':'Astro Bridge Local API','version':__version__,
      'description':'Native mobile API. HTTP recovery remains available; optional device-local HTTPS requires supported diskless OS helpers and explicit client CA trust. Tokens bind to actual transport. Passwords and private keys are never returned. Accepted operations are asynchronous.'},
      'servers':[{'url':scheme+'://{bridgeHost}','variables':{'bridgeHost':{'default':'astro-bridge-12abcd.local'}}} for scheme in ('http','https')],
      'security':[{'BearerAuth':[]}], 'paths':paths,
      'components':{'securitySchemes':{'BearerAuth':{'type':'http','scheme':'bearer','bearerFormat':'opaque'}},'schemas':schemas}}
Path(__file__).resolve().parents[1].joinpath('wifi_bridge/static/openapi.json').write_text(json.dumps(spec,indent=2,ensure_ascii=False)+'\n')
