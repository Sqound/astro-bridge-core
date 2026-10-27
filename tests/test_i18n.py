"""Canonical backend display messages and deterministic public catalog boundary."""
import ast
import importlib.util
import json
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]

class CatalogSourceTests(unittest.TestCase):
    def test_all_explicit_public_backend_errors_have_english_catalog_keys(self):
        english=json.loads((ROOT/'locales/en.json').read_text())
        for path in (ROOT/'wifi_bridge').glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                values=[]
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Name):
                    index=2 if node.func.id=='ApiError' else 0 if node.func.id=='Invalid' else None
                    if index is not None and len(node.args)>index:values.append(node.args[index])
                if isinstance(node,ast.Dict):
                    values.extend(value for key,value in zip(node.keys,node.values) if isinstance(key,ast.Constant) and key.value=='error')
                for value in values:
                    if not isinstance(value,ast.Constant) or not isinstance(value.value,str):continue
                    with self.subTest(path=path.name,message=value.value):
                        self.assertIn(value.value,english)
                        self.assertEqual(english[value.value],value.value)
                        self.assertNotRegex(value.value,r'(?i)\b(?:inconnue|invalide|mot de passe|réseau|requête)\b')

    def test_generated_asset_matches_sources_and_all_ten_locales_are_complete(self):
        spec=importlib.util.spec_from_file_location('catalog_build',ROOT/'tools/build-i18n.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertEqual(module.compile_catalogs(),(ROOT/'wifi_bridge/static/i18n.js').read_text())
        en=json.loads((ROOT/'locales/en.json').read_text());fr=json.loads((ROOT/'locales/fr.json').read_text())
        self.assertEqual(set(en),set(fr));self.assertGreater(len(en),600)
        # Every shipped locale has its own complete catalog; no automatic fill.
        for tag,_,_ in module.LOCALES:
            values=json.loads((ROOT/'locales'/f'{tag}.json').read_text())
            self.assertEqual(set(values),set(en),tag)
