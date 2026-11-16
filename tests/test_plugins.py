"""Declarative manifests drive runtime behavior; community fixtures do not touch hardware."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_bridge import Fixture
from wifi_bridge.api import MobileAPI
from wifi_bridge.plugins import (all_plugins, plugin, proxy_flag, public_registry, registry,
                                 require_config_plugins, ssid_plugin, validate_manifest, validate_registry)

ROOT=Path(__file__).resolve().parents[1]


class RegistryTests(unittest.TestCase):
    def test_sources_bundle_and_editor_schema_are_reproducible_and_public_metadata_is_copied(self):
        subprocess.run([sys.executable,str(ROOT/'tools/build-plugins.py'),'--check'],check=True,capture_output=True)
        self.assertEqual(set(registry()),{'generic','vespera','dwarf','seestar','zwo_mount','synscan'})
        public=public_registry();public['plugins'][0]['name']='Changed'
        self.assertNotEqual(all_plugins()[0]['name'],'Changed')
        self.assertEqual(proxy_flag('vespera'),'legacy_ports')

    def test_rejects_executable_fields_unknown_adapters_and_unbounded_or_unsafe_metadata(self):
        base=copy.deepcopy(plugin('synscan'))
        changes=[{'schema_version':2},{'exec':'anything'},{'id':'../bad'},{'relay':{'transport':'udp','ports':[True]}},
                 {'relay':{'transport':'udp','ports':[11880,11880]}},{'identification':'python:module'},
                 {'default_address':True},{'default_address':'8.8.8.8'},
                 {'activity':'dwarf_metadata_v1'},{'discovery':{'engine':'synscan_version','response':'made up'}},
                 {'references':[{'title':'Bad','url':'javascript:alert(1)'}]}]
        for change in changes:
            with self.subTest(change=change),self.assertRaises((ValueError,TypeError)):
                validate_manifest(dict(base,**change))

    def test_optional_observation_metadata_and_legacy_defaults(self):
        base=copy.deepcopy(plugin('generic'));base.pop('telemetry')
        self.assertEqual(validate_manifest(base)['telemetry'],{'adapters':[],'default_enabled':[]})
        for telemetry in ({'adapters':[],'default_enabled':[]},
                          {'adapters':['link_v1'],'default_enabled':['link_v1']}):
            self.assertEqual(validate_manifest(dict(base,telemetry=telemetry))['telemetry'],telemetry)
        seestar=validate_manifest(plugin('seestar'))
        self.assertEqual(seestar['telemetry']['adapters'][-1],{'id':'seestar_events_v1','requires':'maker_key'})
        self.assertEqual(seestar['maker_key']['sources'],['maker_package','import'])
        self.assertEqual(plugin('vespera')['telemetry']['default_enabled'],['vespera_status_v1'])
        for ident in ('generic','dwarf','zwo_mount','synscan'):
            self.assertEqual({k:v for k,v in plugin(ident)['telemetry'].items() if k!='limitation'},{'adapters':['link_v1'] + ([] if ident == 'dwarf' else ['alpaca_observer_v1']), 'default_enabled':[]})
            self.assertNotIn('maker_key',plugin(ident))

    def test_rejects_invalid_observation_metadata(self):
        base=copy.deepcopy(plugin('generic'))
        invalid=[None, [], {}, {'adapters':['unknown'],'default_enabled':[]},
                 {'adapters':['link_v1','link_v1'],'default_enabled':[]},
                 {'adapters':[{}],'default_enabled':[]},
                 {'adapters':[True],'default_enabled':[]},
                 {'adapters':['link_v1'],'default_enabled':['vespera_status_v1']},
                 {'adapters':['link_v1'],'default_enabled':['link_v1','link_v1']},
                 {'adapters':['link_v1'],'default_enabled':True},
                 {'adapters':['link_v1'],'default_enabled':[],'exec':'forbidden'},
                 {'adapters':['seestar_events_v1'],'default_enabled':[]}]
        for telemetry in invalid:
            with self.subTest(telemetry=telemetry),self.assertRaises(ValueError):
                validate_manifest(dict(base,telemetry=telemetry))
        seestar=copy.deepcopy(plugin('seestar'))
        for change in ({'telemetry':{'adapters':[{'id':'link_v1','requires':'maker_key'}],'default_enabled':[]}},
                       {'telemetry':{'adapters':[{'id':'seestar_events_v1','requires':'other'}],'default_enabled':[]}},
                       {'telemetry':{'adapters':[{'id':'seestar_events_v1','requires':'maker_key'}],'default_enabled':['seestar_events_v1']}}):
            with self.subTest(change=change),self.assertRaises(ValueError):validate_manifest(dict(seestar,**change))
        del seestar['maker_key']
        with self.assertRaises(ValueError):validate_manifest(seestar)

    def test_rejects_invalid_maker_key_metadata(self):
        base=copy.deepcopy(plugin('seestar'))
        for key in (None, [], {}, dict(base['maker_key'],algorithm='other'),
                    dict(base['maker_key'],family='other'),dict(base['maker_key'],sources=['import']),
                    dict(base['maker_key'],sources=['maker_package','import','url']),
                    dict(base['maker_key'],payload='forbidden')):
            with self.subTest(key=key),self.assertRaises(ValueError):validate_manifest(dict(base,maker_key=key))
        with self.assertRaises(ValueError):validate_manifest(dict(plugin('generic'),maker_key=base['maker_key']))

    def test_registry_preserves_legacy_ids_and_rejects_conflicting_discovery(self):
        rows=copy.deepcopy(list(all_plugins()))
        with self.assertRaises(ValueError):validate_registry(rows+rows[:1])
        with self.assertRaises(ValueError):validate_registry(rows[1:])
        extra=copy.deepcopy(plugin('zwo_mount'));extra.update(id='another_mount',name='Another mount',wifi=[{'prefix':'Other_','suffix':'any','min_suffix':1}])
        with self.assertRaises(ValueError):validate_registry(rows+[extra])
        extra['relay']['ports']=[12345];extra['wifi'][0]['prefix']='SynScan_special_'
        # Declared roles must name relayed ports; move the declaration with the port.
        with self.assertRaises(ValueError):validate_registry(rows+[dict(extra,wifi=[{'prefix':'Other_','suffix':'any','min_suffix':1}])])
        extra['port_roles']=[{'port':12345,'role':'control','protocol':'lx200'}]
        with self.assertRaises(ValueError):validate_registry(rows+[extra])
        extra['wifi'][0]['prefix']='Other_';self.assertIn('another_mount',validate_registry(rows+[extra]))

    def test_port_roles_are_bounded_declarations_of_relayed_ports(self):
        from wifi_bridge.plugins import port_role
        self.assertEqual(port_role('seestar',4700),('control','json_rpc'))
        self.assertEqual(port_role('seestar',4554),('preview','rtsp'))
        self.assertEqual(port_role('dwarf',554),('preview','rtsp'))
        self.assertEqual(port_role('vespera',8083),('stream','socketio'))
        self.assertEqual(port_role('generic',80),('other','unknown'))
        base=copy.deepcopy(plugin('vespera'))
        for roles in ([], [{'port':9,'role':'control','protocol':'http'}], [{'port':8082,'role':'admin','protocol':'http'}],
                      [{'port':8082,'role':'rest','protocol':'ftp'}], [{'port':8082,'role':'rest','protocol':'http','extra':1}],
                      [{'port':8082,'role':'rest','protocol':'http'}]*2, 'rest'):
            with self.subTest(roles=roles), self.assertRaises(ValueError):
                validate_manifest(dict(base,port_roles=roles))
        with self.assertRaises(ValueError):validate_manifest(dict(copy.deepcopy(plugin('generic')),port_roles=[{'port':80,'role':'rest','protocol':'http'}]))

    def test_saved_profile_or_policy_cannot_be_orphaned_by_plugin_removal(self):
        require_config_plugins({'telescopes':[{'kind':'synscan'}]})
        for config in ({'telescopes':[{'kind':'removed'}]},{'auto_policy':{'removed_demand':False}}):
            with self.assertRaises(ValueError):require_config_plugins(config)

    def test_release_packaging_refuses_source_manifest_changes_without_rebuilding_catalog(self):
        spec=importlib.util.spec_from_file_location('plugin_release',ROOT/'tools/package-release.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);shutil.copytree(ROOT/'plugins',root/'plugins')
            app=root/'wifi_bridge';(app/'static').mkdir(parents=True)
            (app/'__init__.py').write_text('__version__ = "0.6.33"\n')
            catalog=public_registry();catalog['plugins'][0]['description']='Stale generated description'
            (app/'static/plugins.json').write_text(json.dumps(catalog))
            with self.assertRaisesRegex(ValueError,'catalog is stale'):module.package(root=root)

    def test_wifi_hints_remain_hints_and_respect_utf8_limit(self):
        self.assertEqual(ssid_plugin('s50 pro_TEST')['id'],'seestar')
        self.assertEqual(ssid_plugin('SynScan_WiFi_AB12')['id'],'synscan')
        self.assertEqual(ssid_plugin('AMH_'+'A'*29)['id'],'generic')

    def test_community_manifest_adds_type_proxy_candidate_policy_and_operation_without_core_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);shutil.copytree(ROOT/'wifi_bridge',root/'wifi_bridge',ignore=shutil.ignore_patterns('__pycache__'))
            rows=copy.deepcopy(list(all_plugins()));extra=copy.deepcopy(plugin('zwo_mount'))
            extra.update(id='community_mount',name='Community mount',description='Synthetic community fixture.',
                         default_address='192.168.5.1',identification='none',
                         relay={'transport':'tcp','ports':[12345]},wifi=[{'prefix':'Community_','min_suffix':1,'suffix':'any'}],
                         port_roles=[{'port':12345,'role':'control','protocol':'lx200'}],
                         validation={'level':'simulation','models':[],'checks':[],'limitations':['physical_pending']})
            validate_registry(rows+[extra]);rows.append(extra)
            (root/'wifi_bridge/static/plugins.json').write_text(json.dumps({'schema_version':1,'plugins':rows}))
            (root/'scripts').mkdir()
            for name in ('generate_openapi.py', 'native_client_openapi.py'):
                shutil.copyfile(ROOT/'scripts'/name, root/'scripts'/name)
            subprocess.run([sys.executable,str(root/'scripts/generate_openapi.py')],cwd=root,check=True,capture_output=True)
            contract=json.loads((root/'wifi_bridge/static/openapi.json').read_text())['components']['schemas']
            self.assertIn('community_mount',contract['TelescopeCreate']['properties']['kind']['enum'])
            self.assertIn('community_mount_proxy',contract['Connection']['properties'])
            self.assertIn('community_mount_demand',contract['AutoPolicy']['properties'])
            script='''import tempfile,time
from wifi_bridge.config import Store,auto_policy
from wifi_bridge.controller import Controller
from wifi_bridge.network import DemoNetwork
from wifi_bridge.api import MobileAPI
from wifi_bridge.candidates import scan_candidate
net=DemoNetwork(3);devices=net.devices()
with tempfile.TemporaryDirectory() as folder:
 store=Store(folder,devices[0]['mac']);c=Controller(store,net,{'password':'synthetic-bootstrap'},demo=True);c.update(devices=devices);api=MobileAPI(c)
 api.update('home',{'ssid':'DemoHome','open':True,'mac':devices[1]['mac']});c.cycle(0)
 api.update('settings',{'auto_connect':True})
 api.update('telescopes',{'id':'community','kind':'community_mount','name':'Synthetic mount','target_ip':'192.168.5.1','ssid':'Telescope-DEMO','password':'synthetic-pass','auto_eligible':True},create=True)
 assert auto_policy(store.config)['community_mount_demand'] is True
 assert auto_policy({})['community_mount_demand'] is False
 assert scan_candidate({'ssid':'Community_DEMO','bssid':'02:00:00:11:22:33','frequency':2412,'signal':80,'security':'WPA2'},0)['suggested']['kind']=='community_mount'
 assert any(p['id']=='community_mount' for p in api.read('plugins')['plugins'])
 result=c.operations.demand(c.management_plan,'192.168.1.9',kind='community_mount')
 assert result=='REQUEST_ACCEPTED',result
 c.operations.thread.join(10);op=c.operations.read(c.operations.last)
 assert op['state']=='succeeded',op
 assert op['cause']=='PLUGIN_CONNECTION'
 assert op['endpoint']['proxy']['ports']==[12345] and op['endpoint']['proxy']['transport']=='tcp'
 assert c.state['telescopes'][0]['community_mount_proxy'] is True
 from unittest.mock import patch
 import sys
 sys.path.insert(0,sys.argv[1])
 from test_vespera_demand import packet
 c.demo=False
 with patch.object(c.operations,'demand',return_value='READY_CONNECTION_PRESERVED') as demand:
  c.plugin_adapters['community_mount'].receive(packet(port=12345,target='192.168.1.42'),0,c.management_plan)
  assert demand.call_args.kwargs['kind']=='community_mount' and demand.call_args.kwargs['target'] is None
print('PASS: one additional manifest drives candidate, profile, proxy, policy and automatic operation')
'''
            result=subprocess.run([sys.executable,'-c',script,str(ROOT/'tests')],cwd=root,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr+result.stdout)


class PluginAPITests(Fixture):
    def test_catalog_is_redacted_and_reading_does_not_modify_settings(self):
        self.configured();before=copy.deepcopy(self.store.config)
        catalog=MobileAPI(self.controller).read('plugins')
        self.assertEqual(len(catalog['plugins']),6);self.assertEqual(catalog['schema_version'],1)
        self.assertEqual(self.store.config,before)
        self.assertNotIn('vespera-test-pass',json.dumps(catalog));self.assertNotIn('Vespera-ABC',json.dumps(catalog))
        self.assertEqual(next(p for p in catalog['plugins'] if p['id']=='dwarf')['discovery']['engine'],'dwarf_ping')
