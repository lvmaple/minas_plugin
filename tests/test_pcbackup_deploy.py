import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN = Path(__file__).resolve().parents[1] / 'xiaomi-nas-plugin-pcbackup'


class RegistrationTests(unittest.TestCase):
    def test_manual_registration_example_matches_info(self):
        info = json.loads((PLUGIN / 'INFO').read_text(encoding='utf-8'))
        entry = json.loads((PLUGIN / 'deploy/list-entry.json').read_text(encoding='utf-8'))['pcbackup']['info']
        for field in ('version', 'changelog'):
            self.assertEqual(entry[field], info[field], field)

    def register(self, root, info):
        script = (PLUGIN / 'deploy/deploy.sh').read_text(encoding='utf-8')
        block = re.search(r'python3 - "\$LIST_FILE"[^\n]*<<\'PY\'\n(.*?)\nPY', script, re.S)
        self.assertIsNotNone(block)
        info_path = root / 'INFO'
        info_path.write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')
        return subprocess.run([sys.executable, '-X', 'utf8', '-c', block.group(1),
                               str(root / 'plugins.list'), 'pcbackup', '1234', '9303',
                               str(root / 'rollback'), str(info_path)],
                              capture_output=True, text=True, encoding='utf-8')

    def test_registration_and_restore_copy_use_info_without_losing_other_plugins(self):
        with tempfile.TemporaryDirectory(prefix='pcbackup-registration-') as temp:
            root = Path(temp)
            other = {'other': {'info': {'version': '9.0'}}}
            (root / 'plugins.list').write_text(json.dumps(other), encoding='utf-8')
            info = json.loads((PLUGIN / 'INFO').read_text(encoding='utf-8'))
            info.update(version='9.8.7-test', changelog='测试更新说明：恢复与重试')
            out = self.register(root, info)
            self.assertEqual(out.returncode, 0, out.stderr)
            registered = json.loads((root / 'plugins.list').read_text(encoding='utf-8'))
            self.assertEqual(registered['other'], other['other'])
            self.assertEqual(registered['pcbackup']['info']['version'], info['version'])
            self.assertEqual(registered['pcbackup']['info']['changelog'], info['changelog'])
            saved = json.loads((root / 'rollback/list-entry.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['pcbackup'], registered['pcbackup'])

    def test_invalid_info_does_not_overwrite_registration_or_restore_copy(self):
        with tempfile.TemporaryDirectory(prefix='pcbackup-registration-') as temp:
            root = Path(temp)
            original = b'{"other": {"keep": true}}'
            (root / 'plugins.list').write_bytes(original)
            (root / 'rollback').mkdir()
            (root / 'rollback/list-entry.json').write_bytes(original)
            out = self.register(root, {'plugin': 'pcbackup', 'version': ''})
            self.assertNotEqual(out.returncode, 0)
            self.assertEqual((root / 'plugins.list').read_bytes(), original)
            self.assertEqual((root / 'rollback/list-entry.json').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
