"""Local shell replay and HTTP fixtures; never connect to a NAS or deploy."""
import base64
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


SOURCE = Path(__file__).resolve().parents[1] / 'deploy-plugins.py'
spec = importlib.util.spec_from_file_location('deploy_socat_test_backend', SOURCE)
deploy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = deploy
spec.loader.exec_module(deploy)
BASH = (['wsl', '--exec', 'bash'] if shutil.which('wsl') else None) if os.name == 'nt' else (
    ['bash'] if shutil.which('bash') else None)


@unittest.skipUnless(BASH, 'local shell replay needs Bash or WSL')
class SocatCommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='socat-wire-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.counter = 0

    def replay(self, platform, script, expected, chunked):
        client = deploy.SocatClient.__new__(deploy.SocatClient)
        client.connection = SimpleNamespace(host='nas.example.test', shell_port=2323)
        with patch.object(deploy, 'os', SimpleNamespace(name=platform)), \
                patch.object(deploy.shutil, 'which', return_value='fixture-wsl'), \
                patch.object(deploy.subprocess, 'run', return_value=SimpleNamespace(stdout='')) as run:
            client.run_command(script)
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ['wsl', 'bash', '-c'] if platform == 'nt' else ['bash', '-c', args[-1]])
        formatter, separator, receiver = args[-1].rpartition(' | timeout ')
        self.assertTrue(separator)
        self.assertIn('nc nas.example.test 2323', receiver)
        rendered = subprocess.run(BASH + ['-c', formatter], capture_output=True, timeout=5)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        wire = rendered.stdout
        self.assertNotIn(b'nc nas.example.test', wire)
        chunks = re.findall(rb'echo -n ([A-Za-z0-9+/=]+) >> /tmp/dp.b64', wire)
        if chunked:
            self.assertTrue(chunks)
            self.assertTrue(all(len(chunk) <= 56 for chunk in chunks))
            self.assertEqual(base64.b64decode(b''.join(chunks)), script.replace('\r\n', '\n').encode())
        else:
            self.assertFalse(chunks)

        # Only replay the delivered shell commands in this test's own directory.
        self.counter += 1
        folder = self.root / str(self.counter)
        folder.mkdir()
        native = folder.as_posix()
        linux = '/mnt/' + folder.drive[0].lower() + native[2:] if os.name == 'nt' else native
        for name in ('dp.b64', 'dp2.sh', 'dp.sh'):
            wire = wire.replace(('/tmp/' + name).encode(), shlex.quote(linux + '/' + name).encode())
        out = subprocess.run(BASH, input=wire, capture_output=True, timeout=8)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stderr, b'')
        self.assertEqual(out.stdout, expected.encode())

    def test_windows_short_script_lf_and_crlf(self):
        script = "if true; then\n  printf 'nc-fixture r\\n'\nfi\n"
        for newline in ('\n', '\r\n'):
            with self.subTest(newline=repr(newline)):
                self.replay('nt', script.replace('\n', newline), 'nc-fixture r\n', False)

    def test_windows_long_script_preserves_body_and_normalizes_crlf(self):
        script = 'set -eu\n' + '# fixture filler\n' * 20 + "if true; then\n  printf '%s\\n' 'r $literal 中文'\nfi\n"
        for newline in ('\n', '\r\n'):
            with self.subTest(newline=repr(newline)):
                self.replay('nt', script.replace('\n', newline), 'r $literal 中文\n', True)

    def test_posix_short_and_long_scripts_lf_and_crlf(self):
        short = "if true; then\n  printf 'nc-fixture r\\n'\nfi\n"
        for filler in ('', '# fixture filler\n' * 20):
            for newline in ('\n', '\r\n'):
                with self.subTest(long=bool(filler), newline=repr(newline)):
                    self.replay('posix', (filler + short).replace('\n', newline), 'nc-fixture r\n', True)


class FragmentedReader(io.RawIOBase):
    def __init__(self, data, fragment):
        self.data, self.offset, self.fragment = data, 0, fragment

    def readable(self):
        return True

    def readinto(self, target):
        chunk = self.data[self.offset:self.offset + min(len(target), self.fragment)]
        target[:len(chunk)] = chunk
        self.offset += len(chunk)
        return len(chunk)


class ReplySocket:
    def __init__(self, data, fragment=7):
        self.data, self.offset, self.fragment = data, 0, fragment
        self.closed = False
        self.sent = b''

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def settimeout(self, timeout):
        pass

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        chunk = self.data[self.offset:self.offset + min(size, self.fragment)]
        self.offset += len(chunk)
        return chunk

    def makefile(self, *args, **kwargs):
        return io.BufferedReader(FragmentedReader(self.data, self.fragment))


class SocatResponseTests(unittest.TestCase):
    def setUp(self):
        self.client = deploy.SocatClient.__new__(deploy.SocatClient)
        self.client.connection = SimpleNamespace(host='nas.example.test')
        self.client.server_cn = 'nas.fixture.test'
        self.client.ctx = SimpleNamespace(wrap_socket=lambda raw, **kwargs: raw)
        self.client._webdav_auth = None
        self.body = json.dumps({'data': {'webDAV': {'username': 'fixture用户', 'password': 'fixture密码'}}},
                               ensure_ascii=False).encode()
        self.auth = 'Basic ' + base64.b64encode('fixture用户:fixture密码'.encode()).decode()

    def plain(self, status=200, length=True):
        headers = ('Content-Length: %d\r\n' % len(self.body)).encode() if length else b''
        return ('HTTP/1.1 %d Fixture\r\n' % status).encode() + headers + b'Connection: close\r\n\r\n' + self.body

    def chunked(self, chunks, extensions=False):
        payload = b''
        for chunk in chunks:
            header = '%x%s\r\n' % (len(chunk), ';fixture=yes' if extensions else '')
            payload += header.encode() + chunk + b'\r\n'
        payload += b'0\r\nX-Fixture: done\r\n\r\n' if extensions else b'0\r\n\r\n'
        return b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n' + payload

    def verify_response(self, data):
        reply = ReplySocket(data)
        with patch.object(deploy.socket, 'create_connection', return_value=reply) as connect:
            try:
                auth = self.client._webdav_password()
            except RuntimeError as error:
                self.fail('valid HTTP fixture was not decoded: %s' % error)
            self.assertEqual(auth, self.auth)
            self.assertEqual(self.client._webdav_password(), self.auth)
            connect.assert_called_once()
        self.assertTrue(reply.closed)
        self.assertIn(b'POST /cgi-bin/luci/filemgr/get_pool_info HTTP/1.1', reply.sent)

    def test_content_length_json_and_cached_credentials(self):
        self.verify_response(self.plain())

    def test_connection_close_delimited_json(self):
        self.verify_response(self.plain(length=False))

    def test_one_http_chunk(self):
        self.verify_response(self.chunked([self.body]))

    def test_multiple_chunks_with_a_utf8_character_split(self):
        split = self.body.index('用户'.encode()) + 1
        self.verify_response(self.chunked([self.body[:split], self.body[split:]]))

    def test_many_chunks_with_extensions_and_trailer(self):
        self.verify_response(self.chunked([self.body[i:i + 7] for i in range(0, len(self.body), 7)], True))

    def test_http_failure_does_not_cache_credentials_and_can_retry(self):
        bad, good = ReplySocket(self.plain(status=503)), ReplySocket(self.plain())
        with patch.object(deploy.socket, 'create_connection', side_effect=[bad, good]):
            with self.assertRaises(RuntimeError):
                self.client._webdav_password()
            self.assertIsNone(self.client._webdav_auth)
            self.assertTrue(bad.closed)
            self.assertEqual(self.client._webdav_password(), self.auth)

    def test_truncated_chunked_response_is_not_cached(self):
        reply = ReplySocket(self.chunked([self.body])[:-5])
        with patch.object(deploy.socket, 'create_connection', return_value=reply):
            with self.assertRaises(RuntimeError):
                self.client._webdav_password()
        self.assertIsNone(self.client._webdav_auth)
        self.assertTrue(reply.closed)


class SocatStagingTests(unittest.TestCase):
    def setUp(self):
        self.plugin = SimpleNamespace(plugin_id='motrix', name='fixture', script='deploy/deploy.sh', pass_host=True)
        self.connection = SimpleNamespace(uid='1234', host='nas.example.test')
        self.client = MagicMock()
        self.client.run_command.return_value = 'Motrix ready'

    def test_user_ownership_and_directory_mode_are_set_before_upload(self):
        with redirect_stdout(io.StringIO()):
            deploy._deploy_via_socat(self.plugin, self.connection, self.client, Path('fixture.zip'))
        setup = self.client.run_command.call_args_list[0].args[0]
        self.assertIn('mkdir -p /home/u1234/pool0/data/.deploy_staging-motrix && ', setup)
        self.assertIn('chown u1234:u1234 /home/u1234/pool0/data/.deploy_staging-motrix && ', setup)
        self.assertIn('chmod 755 /home/u1234/pool0/data/.deploy_staging-motrix', setup)
        self.assertEqual([call[0] for call in self.client.mock_calls[:2]], ['run_command', 'upload'])
        self.client.upload.assert_called_once_with(Path('fixture.zip'), '/pool0/data/.deploy_staging-motrix/motrix.zip')

    def test_setup_failure_stops_before_upload(self):
        self.client.run_command.side_effect = RuntimeError('fixture permission failure')
        with self.assertRaises(RuntimeError):
            deploy._deploy_via_socat(self.plugin, self.connection, self.client, Path('fixture.zip'))
        self.client.upload.assert_not_called()


if __name__ == '__main__':
    unittest.main()
