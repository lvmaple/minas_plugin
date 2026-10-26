"""Run real restore/control scripts against a temporary NAS filesystem, never a NAS."""
import configparser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
DEPLOY = {'pcbackup': 'deploy.sh', 'dockerctl': 'deploy.sh', 'sysmon': 'deploy2.sh'}


def heredoc(source, tag):
    return re.search(r"<<'" + tag + r"'\n(.*?)\n" + tag + r"(?:\n|$)", source, re.S).group(1)


@unittest.skipIf(os.name == 'nt', 'real shell fixtures require Linux or WSL')
class PluginPersistenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='plugin-persistence-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()))
        for name in ('etc/systemd/system', 'etc/nginx/conf.d/luci', 'data/plugin/www/icon',
                     'home/u1234/plugin', 'run/lock', 'tmp', 'bin'):
            (self.root / name).mkdir(parents=True)
        self.log = self.root / 'commands'
        self.list_file = self.root / 'data/plugin/u1234.list'
        self.list_file.write_text('{"official": {"keep": true}}')
        self.nginx = self.root / 'etc/nginx/conf.d/luci/plugincenter.conf'
        self.nginx.write_text('server {\n location /plugin { }\n}\n')
        self.stub('systemctl', '''printf '%s\\n' "$*" >> "$TEST_COMMANDS"
if [ "${TEST_RESTORING:-0}" = 1 ]; then
  case " $* " in
    *" start "*|*" restart "*) case " $* " in *" --no-block "*) ;; *) exit 9;; esac;;
  esac
fi
exit 0''')
        for command in ('chown', 'nginx', 'sleep'):
            self.stub(command, 'exit 0')
        self.stub('python3', 'exec "' + sys.executable + '" "$@"')
        docker = self.root / 'data/docker/docker'
        docker.parent.mkdir()
        docker.write_text('#!/bin/sh\nexit 0\n')
        docker.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.root / 'bin') + os.pathsep + os.environ['PATH'],
                        TEST_COMMANDS=str(self.log))
        self.scripts = {}
        for plugin in (*DEPLOY, 'motrix'):
            self.prepare_plugin(plugin)

    def rewrite(self, source):
        # One pass prevents rewriting the /tmp prefix inside replacement paths.
        return re.sub(r'(?<![\w./-])/(data|home|etc|run|tmp)/', lambda m: str(self.root) + m.group(), source)

    def stub(self, command, body):
        path = self.root / 'bin' / command
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def home(self, plugin='pcbackup'):
        return self.root / 'home/u1234/plugin' / plugin

    def backup(self, plugin='pcbackup'):
        return self.root / 'data/backup/plugins' / plugin

    @property
    def state(self):
        return self.backup() / 'state'

    def prepare_plugin(self, plugin):
        package = REPO / ('xiaomi-nas-plugin-' + plugin)
        home = self.home(plugin)
        for name in ('src/ui', 'src/files', 'scripts', 'etc'):
            (home / name).mkdir(parents=True)
        (home / 'INFO').write_text('{}')
        (home / 'src/ui/icon.png').write_bytes(b'icon')
        for name in ('src/files/server.sh', 'src/ui/' + plugin + '.cgi', 'scripts/skopeo-container'):
            (home / name).write_text('#!/bin/sh\nexit 0\n')
        control = package / 'scripts/control'
        (home / 'scripts/control').write_text(self.rewrite(control.read_text(encoding='utf-8')))
        backup = self.backup(plugin)
        backup.mkdir(parents=True)
        (backup / 'list-entry.json').write_text(json.dumps({plugin: {'restored': True}}))
        if plugin in DEPLOY:
            source = (package / 'deploy' / DEPLOY[plugin]).read_text(encoding='utf-8')
            # Install through the real deployment stage, including any generated control script.
            staging = self.root / 'packages' / plugin
            staging.mkdir(parents=True)
            shutil.copyfile(home / 'INFO', staging / 'INFO')
            shutil.copytree(home / 'scripts', staging / 'scripts')
            content = staging if plugin == 'sysmon' else staging / 'src'
            for directory in ('files', 'ui'):
                shutil.copytree(home / 'src' / directory, content / directory)
            stage = source[source.index('echo "==> [2/8]'):source.index('echo "==> [3/8]')]
            env = dict(self.env, SRC_ROOT=str(staging), HOME_DIR=str(home), PLUGIN=plugin, USER_NAME='u1234')
            out = subprocess.run(['sh', '-e'], input=self.rewrite(stage), env=env,
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            restore = heredoc(source, 'RST').replace('__DEPLOY_UID__', '1234').replace('__NAS_UID__', '1234')
            block = re.search(r'cat > "\$RESTORE_UNIT" <<UNIT\n.*?\nUNIT', source, re.S).group()
            unit = self.root / 'etc/systemd/system' / (plugin + '-restore.service')
            env = dict(self.env, RESTORE_UNIT=str(unit), PLUGIN=plugin, BACKUP_DIR=str(backup))
            out = subprocess.run(['sh'], input=block, env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
        else:
            restore = (package / 'deploy/restore.sh').read_text(encoding='utf-8')
            # Exercise the real installer too; Docker and nginx are local command stubs.
            installer = (package / 'deploy/install-web-proxy.sh').read_text(encoding='utf-8')
            (backup / 'install-web-proxy.sh').write_text(self.rewrite(installer))
            shutil.copyfile(package / 'deploy/motrix-nginx.conf.tmpl', backup / 'motrix-nginx.conf.tmpl')
            (backup / 'ws-ticket').write_text('0' * 64)
            state = self.root / 'data/motrix-server/data'
            state.mkdir(parents=True)
            (state / 'operator-token').write_text('fixture-token')
            (home / 'src/files/web_proxy.py').write_text('# fixture\n')
        script = backup / 'restore.sh'
        script.write_text(self.rewrite(restore))
        self.scripts[plugin] = script

    def control(self, action='postinstall', **extra):
        env = dict(self.env, PLUG_HOME_DIR=str(self.home()), PLUG_SRC_DIR=str(self.home() / 'src'),
                   PLUG_USER='u1234', **extra)
        return subprocess.run(['sh', str(self.home() / 'scripts/control'), action],
                              env=env, capture_output=True, text=True, timeout=15)

    def restore(self, plugin):
        return subprocess.run(self.restore_command(plugin), env=dict(self.env, TEST_RESTORING='1'),
                              capture_output=True, text=True, timeout=15)

    def restore_command(self, plugin):
        return ['sh', str(self.scripts[plugin])] + (['1234', '192.0.2.10'] if plugin == 'motrix' else [])

    def snapshot(self):
        source = (REPO / 'xiaomi-nas-plugin-pcbackup/deploy/deploy.sh').read_text(encoding='utf-8')
        block = re.search(r'mkdir -p "\$\{BACKUP_DIR\}"\nrm -rf "\$\{BACKUP_DIR\}/\$\{PLUGIN\}"\ncp -a[^\n]+', source).group()
        out = subprocess.run(['sh', '-eu'], input=block,
                             env=dict(self.env, HOME_DIR=str(self.home()), BACKUP_DIR=str(self.backup()), PLUGIN='pcbackup'),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)

    def write_legacy(self):
        legacy = self.home() / 'etc'
        (legacy / 'jobs/old').mkdir(parents=True, exist_ok=True)
        (legacy / 'jobs.json').write_text('{"old": {"id": "old"}}')
        (legacy / 'jobs/old/pending-run.json').write_text('{"run_id":"before-upgrade"}')
        return legacy

    def test_boot_restore_queues_start_without_waiting_for_its_own_success(self):
        for plugin in DEPLOY:
            with self.subTest(plugin=plugin):
                self.log.write_text('')
                out = self.restore(plugin)
                self.assertEqual(out.returncode, 0, out.stderr)
                commands = self.log.read_text().splitlines()
                synchronous = [c for c in commands if c.split()[0] in ('start', 'restart')]
                self.assertEqual(synchronous, [], 'restore waited for an ordered-after service')
                self.assertIn('--no-block start ' + plugin + '.service', commands)
                unit = configparser.ConfigParser()
                unit.read(self.root / 'etc/systemd/system' / (plugin + '-restore.service'))
                self.assertEqual(unit['Unit']['Before'], plugin + '.service')

    def test_deployment_installs_the_packaged_control_without_a_stale_generated_copy(self):
        for plugin in DEPLOY:
            with self.subTest(plugin=plugin):
                packaged = (REPO / ('xiaomi-nas-plugin-' + plugin) / 'scripts/control').read_text(encoding='utf-8')
                self.assertEqual((self.home(plugin) / 'scripts/control').read_text(), self.rewrite(packaged))

    def test_first_upgrade_migrates_tasks_and_checkpoints_before_switching_unit(self):
        legacy = self.write_legacy()
        original = (legacy / 'jobs/old/pending-run.json').read_bytes()
        out = self.control()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue((self.state / 'jobs.json').is_file(), 'runtime state still lives only in plugin home')
        self.assertEqual((self.state / 'jobs/old/pending-run.json').read_bytes(), original)
        self.assertEqual((legacy / 'jobs/old/pending-run.json').read_bytes(), original)
        unit = self.root / 'etc/systemd/system/pcbackup.service'
        self.assertIn('Environment=PCBACKUP_DATA=' + str(self.state), unit.read_text())

    def test_enable_can_migrate_legacy_state_without_an_earlier_postinstall(self):
        self.write_legacy()
        unit = self.root / 'etc/systemd/system/pcbackup.service'
        unit.write_text('# old service\n')
        out = self.control('enable')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue((self.state / 'jobs.json').is_file(), 'enable started with empty state')
        self.assertIn('Environment=PCBACKUP_DATA=' + str(self.state), unit.read_text())

    def test_migration_failure_preserves_source_and_can_be_retried(self):
        legacy = self.write_legacy()
        hook = self.root / 'copy-failure'
        hook.mkdir()
        (hook / 'sitecustomize.py').write_text('''import pathlib, shutil
def fail_copy(source, destination, *args, **kwargs):
    pathlib.Path(destination, 'partial.json').write_text('partial-copy')
    raise OSError('simulated copy failure')
shutil.copytree = fail_copy
''')
        out = self.control(PYTHONPATH=str(hook))
        self.assertNotEqual(out.returncode, 0, 'migration failure was ignored')
        self.assertTrue((legacy / 'jobs/old/pending-run.json').is_file())
        self.assertFalse(self.state.exists(), 'partial state was published')
        self.assertEqual(list(self.backup().glob('.state-migrate-*')), [])
        self.assertEqual(self.control().returncode, 0)
        self.assertTrue((self.state / 'jobs/old/pending-run.json').is_file())

    def test_old_writer_is_stopped_before_migration_reads_its_checkpoint(self):
        legacy = self.write_legacy()
        unit = self.root / 'etc/systemd/system/pcbackup.service'
        unit.write_text('# old service\n')
        self.env['TEST_LEGACY_CHECKPOINT'] = str(legacy / 'jobs/old/pending-run.json')
        self.stub('systemctl', '''printf '%s\\n' "$*" >> "$TEST_COMMANDS"
if [ "$1" = stop ]; then
  printf '%s' '{"run_id":"flushed-on-stop"}' > "$TEST_LEGACY_CHECKPOINT"
fi
exit 0''')
        out = self.control()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads((self.state / 'jobs/old/pending-run.json').read_text()),
                         {'run_id': 'flushed-on-stop'})
        self.assertEqual(self.log.read_text().splitlines()[0], 'stop pcbackup.service')

    def test_existing_persistent_state_is_not_overwritten_by_old_home_snapshot(self):
        self.write_legacy()
        self.state.mkdir()
        (self.state / 'jobs.json').write_text('{"new": {"id":"new"}}')
        out = self.control()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads((self.state / 'jobs.json').read_text()), {'new': {'id': 'new'}})
        unit = self.root / 'etc/systemd/system/pcbackup.service'
        self.assertIn('Environment=PCBACKUP_DATA=' + str(self.state), unit.read_text())

    def test_boot_after_directory_loss_keeps_latest_tasks_and_50000_file_checkpoint(self):
        self.write_legacy()
        out = self.control()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(self.state.is_dir(), 'persistent state directory missing')
        self.snapshot()
        source = REPO / 'xiaomi-nas-plugin-pcbackup/src/files/httpd.py'
        spec = importlib.util.spec_from_file_location('persistence_backend', source)
        backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        backend.DATA_DIR = str(self.state)
        backend.JOBS_DIR = str(self.state / 'jobs')
        backend.JOBS_FILE = str(self.state / 'jobs.json')
        backend.HISTORY_FILE = str(self.state / 'history.json')
        job_record = {'id': 'old', 'name': 'old', 'exclude': [], 'pc_path': r'D:\docs',
                      'destination': str(self.root / 'data/user-backup')}
        Path(job_record['destination']).mkdir()
        backend.save_jobs({'old': job_record, 'new': {'id': 'new'}})
        backend.manifest_meta(job_record, 'docs')
        backend.compact_manifest(job_record)
        checkpoint = {
            'version': 1, 'run_id': 'after-deploy', 'generation': backend._native_generation(job_record),
            'source_kind': 'native', 'source_path': r'D:\docs', 'destination': job_record['destination'],
            'exclude': [], 'folder': 'docs', 'started': 1700000000, 'total_files': 50000,
            'files': [[str(i) + '.bin', 1, 1700000000000] for i in range(50000)],
            'deleted': [], 'skipped': 0, 'repaired': 0, 'adopted': 0, 'excluded_kept': 0,
            'del_mode': 'del', 'prune_excluded': False,
        }
        job = self.state / 'jobs/old'
        backend._save_json(backend.resume_path(job_record), checkpoint)
        (job / 'finish-request.json').write_text('{"request_id":"finish-after-deploy"}')
        for name in ('parts', 'native', 'runs'):
            (job / name).mkdir(exist_ok=True)
            (job / name / 'one.json').write_text('{"saved":"after-deploy"}')
        (self.state / 'history.json').write_text('[{"run_id":"latest"}]')
        before = {str(p.relative_to(self.state)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.state.rglob('*') if p.is_file()}
        backup_data = self.root / 'data/user-backup.txt'
        backup_data.write_bytes(b'actual-backup-must-not-change')
        shutil.rmtree(self.home())  # Only the temporary fixture's plugin directory.
        for _ in range(2):
            out = self.restore('pcbackup')
            self.assertEqual(out.returncode, 0, out.stderr)
        after = {str(p.relative_to(self.state)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.state.rglob('*') if p.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(backup_data.read_bytes(), b'actual-backup-must-not-change')
        self.assertTrue((self.home() / 'INFO').is_file())
        self.assertEqual(self.nginx.read_text().count('# === pcbackup (auto-added) ==='), 1)
        # A fresh backend must accept the preserved plan, not merely find copied JSON files.
        check = '''import importlib.util, sys
spec = importlib.util.spec_from_file_location('restarted_backend', sys.argv[1])
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)
jobs = backend.load_jobs()
assert set(jobs) == {'old', 'new'}
code, reply = backend.resume_status(jobs['old'])
assert code == 200, reply
assert reply['run_id'] == 'after-deploy'
assert len(reply['files']) == 50000
'''
        out = subprocess.run([sys.executable, '-c', check, str(source)],
                             env=dict(self.env, PCBACKUP_DATA=str(self.state)),
                             capture_output=True, text=True, timeout=15)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_four_plugins_restoring_together_keep_all_entries_and_routes(self):
        hook = self.root / 'hooks'
        hook.mkdir()
        (hook / 'sitecustomize.py').write_text('''import json, time
original_load = json.load
def slow_load(stream, *args, **kwargs):
    value = original_load(stream, *args, **kwargs)
    if str(getattr(stream, 'name', '')).endswith('u1234.list'):
        time.sleep(.4)
    return value
json.load = slow_load
''')
        env = dict(self.env, TEST_RESTORING='1', PYTHONPATH=str(hook))
        processes = [(plugin, subprocess.Popen(self.restore_command(plugin), env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)) for plugin in self.scripts]
        try:
            for plugin, proc in processes:
                out, err = proc.communicate(timeout=20)
                self.assertEqual(proc.returncode, 0, plugin + '\n' + err)
        finally:
            for _, proc in processes:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        self.assertEqual(set(json.loads(self.list_file.read_text())), {'official', *self.scripts})
        nginx = self.nginx.read_text()
        for plugin in DEPLOY:
            self.assertEqual(nginx.count('# === ' + plugin + ' (auto-added) ==='), 1)
        self.assertTrue((self.root / 'etc/nginx/conf.d/luci/motrix.conf').is_file())

    def test_failed_restore_releases_shared_lock_and_is_retryable(self):
        (self.backup() / 'list-entry.json').write_text('{invalid-json')
        self.assertNotEqual(self.restore('pcbackup').returncode, 0)
        out = self.restore('sysmon')
        self.assertEqual(out.returncode, 0, out.stderr)
        (self.backup() / 'list-entry.json').write_text('{"pcbackup":{"retried":true}}')
        out = self.restore('pcbackup')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(set(json.loads(self.list_file.read_text())), {'official', 'sysmon', 'pcbackup'})


if __name__ == '__main__':
    unittest.main()
