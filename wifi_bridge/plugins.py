"""Versioned, bounded instrument manifests; curated adapters own all execution.

Source manifests live in plugins/*.json. The generator bundles them into one
static JSON asset supported by existing app-only OTA installers. No runtime code,
expressions, shell commands or nft fragments are loaded from manifests.
"""
from functools import lru_cache
import copy
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
CORE_IDS = ('generic', 'vespera', 'dwarf', 'seestar', 'zwo_mount', 'synscan')
IDENTIFIERS = ('none', 'seestar_scan_iscope', 'zwo_model', 'synscan_version')
DISCOVERY = ('none', 'tcp_syn', 'seestar_scan_iscope', 'dwarf_ping', 'synscan_version')
ACTIVITY_PORTS = {'protect_all': (), 'vespera_metadata_v1': (8083,),
                  'dwarf_metadata_v1': (9900,), 'seestar_metadata_v1': (4700,4800,4801,4804)}
CHECKS = ('wifi', 'identity', 'native_app', 'preview', 'switching', 'private_proxy', 'hotspot_proxy')
LIMITATIONS = ('physical_pending', 'autonomous_pending', 'guiding_pending', 'first_discovery_unavailable', 'images_pending', 'switching_pending')


def text(value, limit=256):
    if not isinstance(value, str) or not value or len(value)>limit or any(ord(c)<32 for c in value):
        raise ValueError('Invalid plugin text.')
    return value


def fields(value, required, optional=()):
    if not isinstance(value,dict) or set(value)-set(required)-set(optional) or not set(required)<=set(value):
        raise ValueError('Unknown or missing plugin fields.')


def validate_manifest(value):
    fields(value, ('schema_version','id','version','name','description','category','wifi','default_address',
                   'relay','identification','discovery','activity','readiness','validation','references'), ('onboarding',))
    if type(value['schema_version']) is not int or value['schema_version']!=SCHEMA_VERSION:raise ValueError('Unsupported plugin schema.')
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,31}',text(value['id'],32)):raise ValueError('Invalid plugin ID.')
    if not re.fullmatch(r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}',text(value['version'],16)):raise ValueError('Invalid plugin version.')
    text(value['name'],64);text(value['description'],300)
    if value['category'] not in ('generic','telescope','mount'):raise ValueError('Invalid plugin category.')
    if not isinstance(value['wifi'],list) or len(value['wifi'])>16:raise ValueError('Invalid Wi-Fi hints.')
    for hint in value['wifi']:
        fields(hint,('prefix','min_suffix','suffix'))
        if len(text(hint['prefix'],32).encode())>32 or type(hint['min_suffix']) is not int or hint['min_suffix'] not in (0,1):raise ValueError('Invalid Wi-Fi prefix.')
        if hint['suffix'] not in ('any','alnum-hyphen','alnum-hyphen-underscore'):raise ValueError('Invalid Wi-Fi suffix matcher.')
    if value['category']=='mount' and value['activity']!='protect_all':raise ValueError('Mount traffic protection cannot be weakened by a manifest.')
    if value['activity']!='protect_all' and value['id'] not in ('vespera','dwarf','seestar'):raise ValueError('New families require conservative traffic protection.')
    if value['default_address'] is not None:
        if not isinstance(value['default_address'],str):raise ValueError('Invalid default address type.')
        address=ipaddress.IPv4Address(value['default_address'])
        if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:raise ValueError('Invalid default address.')
    rules=value.get('onboarding',[])
    if not isinstance(rules,list) or len(rules)>16:raise ValueError('Invalid onboarding defaults.')
    for rule in rules:
        fields(rule,('prefix',),('target_ip','password'))
        if not isinstance(rule['prefix'],str) or len(rule['prefix'].encode())>32:raise ValueError('Invalid onboarding prefix.')
        if 'target_ip' in rule:
            if not isinstance(rule['target_ip'],str):raise ValueError('Invalid onboarding address type.')
            address=ipaddress.IPv4Address(rule['target_ip'])
            if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:raise ValueError('Invalid onboarding address.')
        if 'password' in rule:
            password=rule['password']
            if not isinstance(password,str) or not 8<=len(password.encode())<=63 or any(ord(c)<32 for c in password):raise ValueError('Invalid factory password.')
    relay=value['relay']
    if relay is not None:
        fields(relay,('transport','ports'))
        if relay['transport'] not in ('tcp','udp') or not isinstance(relay['ports'],list) or not 1<=len(relay['ports'])<=16:raise ValueError('Invalid plugin relay.')
        if any(type(p) is not int or not 1<=p<=65535 for p in relay['ports']) or len(set(relay['ports']))!=len(relay['ports']):raise ValueError('Invalid relay ports.')
    if value['identification'] not in IDENTIFIERS or value['activity'] not in ACTIVITY_PORTS:raise ValueError('Unknown plugin adapter.')
    discovery=value['discovery'];fields(discovery,('engine',),('target_mode',))
    if discovery['engine'] not in DISCOVERY:raise ValueError('Unknown discovery adapter.')
    if discovery['engine']=='tcp_syn':
        if not relay or relay['transport']!='tcp' or len(relay['ports'])>4 or discovery.get('target_mode') not in ('original','proxy_or_original'):raise ValueError('Invalid TCP demand adapter.')
    elif 'target_mode' in discovery:raise ValueError('Unexpected demand option.')
    expected={'synscan_version':('udp',[11880]),'seestar_scan_iscope':('tcp',[4700,4800,4801,4804,4554,4555]),'dwarf_ping':('tcp',[9900,8082,554])}
    if discovery['engine'] in expected:
        transport,ports=expected[discovery['engine']]
        if relay!={'transport':transport,'ports':ports}:raise ValueError('Relay does not match discovery adapter.')
    if value['identification']=='synscan_version' and relay!={'transport':'udp','ports':[11880]}:raise ValueError('Invalid SynScan identification transport.')
    if value['identification']=='zwo_model' and (not relay or relay['transport']!='tcp' or len(relay['ports'])!=1):raise ValueError('Invalid ZWO identification transport.')
    if value['readiness'] not in ('association','vespera_tcp') or value['readiness']=='vespera_tcp' and (not relay or relay['transport']!='tcp' or 8082 not in relay['ports']):raise ValueError('Unknown readiness adapter.')
    validation=value['validation'];fields(validation,('level','models','checks','limitations'))
    if validation['level'] not in ('unverified','simulation','partial_hardware'):raise ValueError('Invalid validation level.')
    if not isinstance(validation['models'],list) or len(validation['models'])>16:raise ValueError('Invalid validation models.')
    for model in validation['models']:text(model,64)
    if validation['level']=='partial_hardware' and not validation['models']:raise ValueError('Hardware evidence requires an exact model.')
    for key,allowed in (('checks',CHECKS),('limitations',LIMITATIONS)):
        if not isinstance(validation[key],list) or len(validation[key])>len(allowed) or any(v not in allowed for v in validation[key]) or len(set(validation[key]))!=len(validation[key]):raise ValueError('Invalid validation scope.')
    if not isinstance(value['references'],list) or not 1<=len(value['references'])<=8:raise ValueError('Plugin references required.')
    for reference in value['references']:
        fields(reference,('title','url'));text(reference['title'],120);url=urlsplit(text(reference['url'],512))
        if url.scheme!='https' or not url.hostname or url.username or url.password:raise ValueError('Invalid plugin reference.')
    return copy.deepcopy(value)


def validate_registry(values):
    if not isinstance(values,list) or not 6<=len(values)<=32:raise ValueError('Invalid plugin registry size.')
    result={}
    listeners=set();tcp_ports=set();prefixes=[]
    for value in values:
        plugin=validate_manifest(value);ident=plugin['id']
        if ident in result:raise ValueError('Duplicate plugin ID.')
        engine=plugin['discovery']['engine']
        if engine=='tcp_syn':
            ports=set(plugin['relay']['ports'])
            if ports&tcp_ports:raise ValueError('Ambiguous TCP discovery ports.')
            tcp_ports|=ports
        elif engine!='none':
            if engine in listeners:raise ValueError('A native discovery protocol belongs to one plugin; extend that plugin for new models.')
            listeners.add(engine)
        for hint in plugin['wifi']:
            prefix=hint['prefix'].lower()
            if any(other!=ident and (prefix.startswith(old) or old.startswith(prefix)) for other,old in prefixes):raise ValueError('Ambiguous Wi-Fi family hints.')
            prefixes.append((ident,prefix))
        result[ident]=plugin
    if not set(CORE_IDS)<=set(result):raise ValueError('Existing saved profile types must remain available.')
    expected={'generic':'none','vespera':'tcp_syn','dwarf':'dwarf_ping','seestar':'seestar_scan_iscope','zwo_mount':'tcp_syn','synscan':'synscan_version'}
    if any(result[key]['discovery']['engine']!=engine for key,engine in expected.items()):raise ValueError('Existing protocol adapter IDs must remain compatible.')
    return result


@lru_cache(maxsize=1)
def registry():
    path=Path(__file__).parent/'static'/'plugins.json'
    if path.stat().st_size>256*1024:raise ValueError('Oversized plugin catalog.')
    value=json.loads(path.read_text())
    fields(value,('schema_version','plugins'))
    if type(value['schema_version']) is not int or value['schema_version']!=SCHEMA_VERSION:raise ValueError('Unsupported registry schema.')
    return validate_registry(value['plugins'])


def plugin(ident):return registry()[ident]
def all_plugins():return tuple(registry().values())
def profile_kinds():return tuple(registry())
def relay_plugins():return tuple(p for p in all_plugins() if p['relay'])
def demand_plugins():return tuple(p for p in all_plugins() if p['discovery']['engine']!='none')
def proxy_flag(ident):return 'legacy_ports' if ident=='vespera' else ident+'_proxy'
def demand_flag(ident):return ident+'_demand'
def public_registry():return {'schema_version':SCHEMA_VERSION,'plugins':copy.deepcopy(list(all_plugins()))}


def ssid_plugin(ssid):
    if not isinstance(ssid,str) or len(ssid.encode())>32:return plugin('generic')
    lowered=ssid.lower()
    for p in all_plugins():
        for hint in p['wifi']:
            prefix=hint['prefix'].lower()
            if not lowered.startswith(prefix):continue
            suffix=ssid[len(prefix):]
            if len(suffix)<hint['min_suffix']:continue
            allowed='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-'
            if hint['suffix']=='alnum-hyphen-underscore':allowed+='_'
            if hint['suffix']!='any' and any(c not in allowed for c in suffix):continue
            return p
    return plugin('generic')


def manifest_schema():
    """Editor/API schema; the stdlib validator also checks adapter invariants."""
    def obj(properties):return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
    string={'type':'string','minLength':1,'maxLength':256}
    array=lambda item,maximum:{'type':'array','items':item,'maxItems':maximum}
    result=obj({'schema_version':{'const':1},'id':{'type':'string','pattern':'^[a-z][a-z0-9_]{0,31}$'},
        'version':{'type':'string','pattern':'^[0-9]{1,3}\\.[0-9]{1,3}\\.[0-9]{1,3}$'},'name':dict(string,maxLength=64),'description':dict(string,maxLength=300),
        'category':{'enum':['generic','telescope','mount']},
        'wifi':array(obj({'prefix':dict(string,maxLength=32),'min_suffix':{'enum':[0,1]},'suffix':{'enum':['any','alnum-hyphen','alnum-hyphen-underscore']}}),16),
        'default_address':{'type':['string','null'],'format':'ipv4'},
        'relay':{'oneOf':[{'type':'null'},obj({'transport':{'enum':['tcp','udp']},'ports':dict(array({'type':'integer','minimum':1,'maximum':65535},16),minItems=1,uniqueItems=True)})]},
        'identification':{'enum':list(IDENTIFIERS)},
        'discovery':{'oneOf':[obj({'engine':{'enum':[e for e in DISCOVERY if e!='tcp_syn']}}),obj({'engine':{'const':'tcp_syn'},'target_mode':{'enum':['original','proxy_or_original']}})]},
        'activity':{'enum':list(ACTIVITY_PORTS)},'readiness':{'enum':['association','vespera_tcp']},
        'validation':obj({'level':{'enum':['unverified','simulation','partial_hardware']},'models':array(dict(string,maxLength=64),16),
                          'checks':dict(array({'enum':list(CHECKS)},len(CHECKS)),uniqueItems=True),
                          'limitations':dict(array({'enum':list(LIMITATIONS)},len(LIMITATIONS)),uniqueItems=True)}),
        'references':dict(array(obj({'title':dict(string,maxLength=120),'url':dict(string,maxLength=512,format='uri',pattern='^https://')}),8),minItems=1)})
    result['properties']['onboarding']=array({'type':'object','properties':{'prefix':{'type':'string','maxLength':32},'target_ip':{'type':'string','format':'ipv4'},'password':{'type':'string','minLength':8,'maxLength':63}},'required':['prefix'],'additionalProperties':False},16)
    return result


def onboarding_defaults(ssid):
    spec=ssid_plugin(ssid);result={'kind':spec['id'],'name':spec['name'] if spec['id']!='generic' else '', 'target_ip':spec['default_address']}
    for rule in sorted(spec.get('onboarding',[]),key=lambda r:-len(r['prefix'])):
        if ssid.lower().startswith(rule['prefix'].lower()):
            result.update({k:v for k,v in rule.items() if k!='prefix'});break
    return result


def require_config_plugins(config):
    kinds={entry.get('kind','generic') for entry in config.get('telescopes',[])}
    if kinds-set(profile_kinds()):raise ValueError('This update is missing a plugin used by a saved profile.')
    fields={'quiet_seconds','cooldown_seconds','force_first_profile'}|{demand_flag(p['id']) for p in demand_plugins()}
    if set(config.get('auto_policy',{}))-fields:raise ValueError('This update is missing a saved plugin policy.')
