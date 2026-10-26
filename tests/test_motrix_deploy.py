"""Deployment regressions use copied assets and an isolated shell fixture only."""
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


PLUGIN = Path(__file__).resolve().parents[1] / 'xiaomi-nas-plugin-motrix'


class MotrixPatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='motrix-patch-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.ui = self.root / 'ui'
        shutil.copytree(PLUGIN / 'src/ui', self.ui)

    def snapshot(self):
        return {str(p.relative_to(self.ui)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.ui.rglob('*') if p.is_file()}

    def run_patch(self, ticket='0' * 64):
        return subprocess.run([sys.executable, str(PLUGIN / 'deploy/patch-web-assets.py'),
                               str(self.ui), '1234', ticket], capture_output=True, text=True,
                              encoding='utf-8', timeout=20)

    def test_repeat_patch_fails_without_changing_patched_assets(self):
        first = self.run_patch()
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.snapshot()
        second = self.run_patch()
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_counter_validation_failure_leaves_all_input_files_unchanged(self):
        marker = 'globalThis.location?.origin??``'
        for p in self.ui.glob('assets/*.js'):
            content = p.read_text(encoding='utf-8')
            if marker in content:
                p.write_text(content.replace(marker, '`test-invalid-origin`', 1), encoding='utf-8')
                break
        else:
            self.fail('real upstream marker was not found')
        before = self.snapshot()
        out = self.run_patch()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('API origins', out.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_missing_upstream_chunk_does_not_modify_other_assets(self):
        (self.ui / 'assets/window-chrome-CZCTzY6z.js').unlink()
        before = self.snapshot()
        self.assertNotEqual(self.run_patch().returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_normal_redeploy_overlays_original_assets_and_reuses_chunk_name(self):
        first = self.run_patch()
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.snapshot()
        shutil.copytree(PLUGIN / 'src/ui', self.ui, dirs_exist_ok=True)
        second = self.run_patch()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_fresh_overlay_can_change_ticket_without_double_prefixes(self):
        self.assertEqual(self.run_patch().returncode, 0)
        shutil.copytree(PLUGIN / 'src/ui', self.ui, dirs_exist_ok=True)
        out = self.run_patch('1' * 64)
        self.assertEqual(out.returncode, 0, out.stderr)
        html = (self.ui / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('/plugin/1234/motrix/plugin/1234/motrix/', html)
        assets = '\n'.join(p.read_text(encoding='utf-8') for p in self.ui.glob('assets/*.js')
                           if not p.name.startswith('window-chrome-motrix-'))
        self.assertIn('/api/' + '1' * 64, assets)


@unittest.skipIf(os.name == 'nt', 'shell fixtures run on Linux or WSL')
class MotrixRestoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='motrix-restore-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.backup = self.root / 'backup'
        self.backup.mkdir()
        plugin_home = self.root / 'plugin'
        (plugin_home / 'src/ui').mkdir(parents=True)
        (plugin_home / 'INFO').write_text('{}')
        (plugin_home / 'src/ui/icon.png').write_bytes(b'test-icon')
        (self.backup / 'list-entry.json').write_text(json.dumps({'motrix': {'test': True}}))
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.stub('chown', 'exit 0')
        self.stub('docker', 'exit 0')
        self.stub('hostname', 'printf "%s\\n" "$TEST_HOST_IPS"')
        self.stub('ip', 'case "$1" in route) printf "%s\\n" "$TEST_ROUTE";; *) printf "%s\\n" "$TEST_ADDR";; esac')
        installer = self.backup / 'install-web-proxy.sh'
        installer.write_text('#!/bin/sh\nprintf "%s|%s\\n" "$1" "$2" > "$TEST_PROXY_LOG"\nexit "${TEST_PROXY_EXIT:-0}"\n')
        self.proxy_log = self.root / 'proxy-call'
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        TEST_ROUTE='', TEST_HOST_IPS='', TEST_ADDR='', TEST_PROXY_LOG=str(self.proxy_log))
        source = (PLUGIN / 'deploy/restore.sh').read_text()
        source = source.replace('/data/backup/plugins/motrix', str(self.backup))
        source = source.replace('HOME_DIR="/home/${USER_NAME}/plugin/motrix"', 'HOME_DIR="' + str(plugin_home) + '"')
        source = source.replace('/data/plugin', str(self.root / 'data/plugin'))
        source = source.replace('/data/docker/docker', str(self.bin / 'docker'))
        source = source.replace('/run/lock', str(self.root / 'run/lock'))
        self.script = self.root / 'restore.sh'
        self.script.write_text(source)

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def restore(self, explicit=None):
        return subprocess.run(['sh', str(self.script), '1234'] + ([explicit] if explicit else []),
                              env=self.env, capture_output=True, text=True, timeout=5)

    def test_explicit_ip_does_not_require_a_default_route(self):
        out = self.restore('192.0.2.10')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.proxy_log.read_text().strip(), '1234|192.0.2.10')

    def test_default_route_address_is_used(self):
        self.env['TEST_ROUTE'] = '1.1.1.1 via 192.0.2.1 dev eth0 src 192.0.2.20'
        out = self.restore()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.proxy_log.read_text().strip(), '1234|192.0.2.20')

    def test_missing_route_uses_local_ipv4_address(self):
        self.env['TEST_HOST_IPS'] = '2001:db8::10 192.0.2.30'
        out = self.restore()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(self.proxy_log.exists(), 'proxy restoration was silently skipped')
        self.assertEqual(self.proxy_log.read_text().strip(), '1234|192.0.2.30')

    def test_missing_hostname_address_uses_interface_address(self):
        self.env['TEST_ADDR'] = '    inet 192.0.2.40/24 brd 192.0.2.255 scope global eth0'
        out = self.restore()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(self.proxy_log.exists(), 'proxy restoration was silently skipped')
        self.assertEqual(self.proxy_log.read_text().strip(), '1234|192.0.2.40')

    def test_no_address_is_an_explicit_failure_and_can_be_retried(self):
        out = self.restore()
        self.assertNotEqual(out.returncode, 0, 'missing IP was reported as successful recovery')
        self.assertTrue(out.stderr.strip())
        self.assertFalse(self.proxy_log.exists())
        self.env['TEST_ROUTE'] = '1.1.1.1 dev eth0 src 192.0.2.50'
        self.assertEqual(self.restore().returncode, 0)
        self.assertEqual(self.proxy_log.read_text().strip(), '1234|192.0.2.50')

    def test_proxy_install_failure_is_not_hidden(self):
        self.env['TEST_PROXY_EXIT'] = '7'
        self.assertEqual(self.restore('192.0.2.60').returncode, 7)

    def test_generated_restore_service_retries_failures(self):
        source = (PLUGIN / 'deploy/deploy.sh').read_text()
        block = re.search(r'cat > /etc/systemd/system/motrix-restore.service <<UNIT\n.*?\nUNIT', source, re.S)
        self.assertIsNotNone(block)
        unit = self.root / 'restore.service'
        command = block.group().replace('/etc/systemd/system/motrix-restore.service', str(unit))
        env = dict(self.env, UID_NUM='1234', BACKUP_DIR=str(self.backup))
        out = subprocess.run(['sh'], input=command, env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        config = configparser.ConfigParser(interpolation=None)
        config.read(unit)
        self.assertEqual(config['Service'].get('Restart'), 'on-failure')
        self.assertEqual(config['Service'].get('RestartSec'), '5')


if __name__ == '__main__':
    unittest.main()
