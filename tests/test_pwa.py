"""Public snapshot revisions are content-based, coherent and independent of state."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest

from wifi_bridge import pwa


class PublicShellTests(unittest.TestCase):
    def test_every_public_byte_participates_and_order_is_deterministic(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for _,(name,_) in pwa.ASSETS.items():shutil.copy2(pwa.STATIC/name,root/name)
            shutil.copy2(pwa.STATIC/'sw.js',root/'sw.js')
            before=pwa.load_shell(root)[2]
            self.assertEqual(pwa.load_shell(root)[2],before)
            for name in [item[0] for item in pwa.ASSETS.values()]+['sw.js']:
                path=root/name;original=path.read_bytes();path.write_bytes(original+b'changed')
                self.assertNotEqual(pwa.load_shell(root)[2],before,name)
                path.write_bytes(original)
            self.assertEqual(pwa.load_shell(root)[2],before)

    def test_running_snapshot_stays_coherent_until_next_process_load(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'static').mkdir()
            shutil.copy2(Path(pwa.__file__),root/'pwa.py')
            for _,(name,_) in pwa.ASSETS.items():shutil.copy2(pwa.STATIC/name,root/'static'/name)
            shutil.copy2(pwa.STATIC/'sw.js',root/'static/sw.js')
            def load():
                spec=importlib.util.spec_from_file_location('temporary_pwa',root/'pwa.py')
                module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                return module
            old=load();asset=old.public_asset('/style.css');old_revision=old.revision()
            with (root/'static/style.css').open('ab') as stream:stream.write(b'/* next process */')
            self.assertEqual(old.public_asset('/style.css'),asset)
            self.assertEqual(old.revision(),old_revision)
            new=load();self.assertNotEqual(new.revision(),old_revision)
            self.assertEqual(new.public_asset('/')[2],new.public_asset('/sw.js')[2])
            self.assertIn(new.revision().encode(),new.public_asset('/')[0])
            self.assertIn(new.revision().encode(),new.public_asset('/sw.js')[0])

    def test_no_private_resource_can_be_served_as_shell_asset(self):
        for path in ['/api/status','/api/login','/api/events','/../config.json','/config.json']:
            with self.assertRaises(KeyError):pwa.public_asset(path)
