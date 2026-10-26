import importlib.util
import json
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "deploy-plugins.py"
spec = importlib.util.spec_from_file_location("deploy_plugins", SCRIPT)
deploy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = deploy
spec.loader.exec_module(deploy)


class DeployPluginsTests(unittest.TestCase):
    def test_discovers_only_plugins_with_nas_entrypoint(self):
        plugins = deploy.discover_plugins()
        self.assertEqual([p.plugin_id for p in plugins],
                         ["dockerctl", "motrix", "pcbackup", "sysmon"])
        self.assertEqual(next(p.script for p in plugins if p.plugin_id == "sysmon"),
                         "deploy/deploy2.sh")
        self.assertTrue(next(p.pass_host for p in plugins if p.plugin_id == "motrix"))

    def test_packages_each_plugin_with_posix_paths_and_entrypoint(self):
        with tempfile.TemporaryDirectory() as temp:
            for plugin in deploy.discover_plugins():
                archive = Path(temp) / f"{plugin.plugin_id}.zip"
                deploy.package_plugin(plugin, archive)
                with zipfile.ZipFile(archive) as packed:
                    names = packed.namelist()
                self.assertIn("INFO", names)
                self.assertIn(plugin.script, names)
                self.assertTrue(all("\\" not in name and not name.startswith("/")
                                    for name in names))
                self.assertFalse(any("__pycache__" in name or name.endswith(".pyc")
                                     for name in names))

    def test_menu_accepts_none_all_and_multiple_plugins(self):
        plugins = deploy.discover_plugins()
        self.assertEqual(deploy.parse_selection("0", plugins), [])
        self.assertEqual(deploy.parse_selection("none", plugins), [])
        self.assertEqual(deploy.parse_selection("a", plugins), plugins)
        self.assertEqual([p.plugin_id for p in deploy.parse_selection("1,sysmon,1", plugins)],
                         ["dockerctl", "sysmon"])
        with self.assertRaises(ValueError):
            deploy.parse_selection("99", plugins)

    def test_config_is_read_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            source = {"host": "nas.local", "uid": "123", "ssh_key": ""}
            path.write_text(json.dumps(source), encoding="utf-8")
            self.assertEqual(deploy.read_config(path), source)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), source)

    def test_config_supplies_connection_without_prompt(self):
        args = Namespace(host=None, uid=None, ssh_key=None, user=None,
                         port=None, transport=None, channel=None,
                         shell_port=None, cert_dir=None)
        config = {"host": "nas.local", "uid": "123", "ssh_key": "",
                  "user": "root", "port": 2222, "transport": "native"}
        with patch("builtins.input") as prompt:
            connection = deploy.make_connection(args, config)
        prompt.assert_not_called()
        self.assertEqual(connection, deploy.Connection(
            "nas.local", "123", "root", 2222, None, "native"))

    def test_none_option_checks_ssh_but_never_uploads(self):
        with patch.object(deploy.SshClient, "check_connection") as check, \
             patch.object(deploy, "deploy_plugin") as install:
            result = deploy.main(["--host", "nas.local", "--uid", "123",
                                  "--transport", "native", "--ssh-key=",
                                  "--channel", "ssh",
                                  "--select", "none"])
        self.assertEqual(result, 0)
        check.assert_called_once()
        install.assert_not_called()

    def test_fix_text_crlf_only_touches_text_suffixes(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "p.zip"
            png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4  # 含 0x0D 的二进制
            script = b"#!/bin/sh\r\nset -e\r\n"
            info = b'{\r\n  "plugin": "x"\r\n}\r\n'
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("ui/icon.png", png)
                z.writestr("deploy/deploy.sh", script)
                z.writestr("INFO", info)
            changed = deploy.fix_text_crlf(archive)
            self.assertEqual(changed, 2)
            with zipfile.ZipFile(archive) as z:
                self.assertEqual(z.read("ui/icon.png"), png)          # 二进制原样
                self.assertEqual(z.read("deploy/deploy.sh"), b"#!/bin/sh\nset -e\n")
                self.assertNotIn(b"\r", z.read("INFO"))

    def test_make_client_picks_socat_or_ssh(self):
        ssh_conn = deploy.Connection("nas", "1", "root", 22, None, "native", "ssh")
        socat_conn = deploy.Connection("nas", "1", "root", 22, None, "native", "socat")
        with patch.object(deploy, 'SshClient') as ssh, patch.object(deploy, 'SocatClient') as socat:
            self.assertIs(deploy.make_client(ssh_conn), ssh.return_value)
            ssh.assert_called_once_with(ssh_conn)
            socat.assert_not_called()
            self.assertIs(deploy.make_client(socat_conn), socat.return_value)
            socat.assert_called_once_with(socat_conn)


class SocatTlsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='deploy-tls-')
        self.addCleanup(temp.cleanup)
        self.cert_dir = Path(temp.name)
        for name in ('123_test_cert.pem', '123_test_private_key.pem', 'ca_chain.pem'):
            (self.cert_dir / name).touch()
        self.peer = {'subject': ((('commonName', 'nas.123.server.2'),),)}
        self.handshakes = []
        owner = self

        class Context:
            def __init__(self, *args):
                self.verify_mode = ssl.CERT_REQUIRED
                self.check_hostname = True
                self.load_cert_chain = MagicMock()

            def wrap_socket(self, raw, server_hostname):
                owner.handshakes.append((self.verify_mode, self.check_hostname, server_hostname))
                if owner.tls_error:
                    raise owner.tls_error
                stream = MagicMock()
                stream.__enter__.return_value = stream
                stream.getpeercert.side_effect = lambda binary_form=False: b'test-peer' if binary_form else owner.peer
                return stream

        self.tls_error = None
        self.verified = Context()
        for target, kwargs in (
            ('find_cert_dir', {'return_value': self.cert_dir}),
            ('socket.create_connection', {'return_value': MagicMock()}),
            ('ssl.SSLContext', {'side_effect': Context}),
            ('ssl.create_default_context', {'return_value': self.verified}),
            ('ssl._ssl._test_decode_cert', {'return_value': self.peer}),
        ):
            mock = patch('deploy_plugins.' + target, **kwargs)
            value = mock.start()
            self.addCleanup(mock.stop)
            if target == 'ssl.create_default_context':
                self.context_factory = value
            if target == 'socket.create_connection':
                self.connect = value
        self.connection = deploy.Connection('nas.example.test', '123', 'root', 22, None, 'native', 'socat')

    def test_probe_verifies_chain_and_formal_connections_verify_hostname(self):
        client = deploy.SocatClient(self.connection)
        self.assertEqual(client.server_cn, 'nas.123.server.2')
        self.assertTrue(self.handshakes)
        self.assertTrue(all(mode == ssl.CERT_REQUIRED and not hostname for mode, hostname, _ in self.handshakes))
        self.assertTrue(client.ctx.check_hostname)
        self.assertEqual(client.ctx.verify_mode, ssl.CERT_REQUIRED)
        self.context_factory.assert_called_once_with(cafile=str(self.cert_dir / 'ca_chain.pem'))
        self.verified.load_cert_chain.assert_called_once_with(
            str(self.cert_dir / '123_test_cert.pem'), str(self.cert_dir / '123_test_private_key.pem'))

    def test_untrusted_peer_is_rejected_without_local_certificate_fallback(self):
        self.tls_error = ssl.SSLCertVerificationError('untrusted test peer')
        with self.assertRaises(ValueError) as caught:
            deploy.SocatClient(self.connection)
        self.assertIs(caught.exception.__cause__, self.tls_error)
        self.assertTrue(self.verified.check_hostname)

    def test_unreachable_probe_reports_failure_instead_of_silently_selecting_a_cn(self):
        error = OSError('test connection refused')
        self.connect.side_effect = error
        with self.assertRaises(ValueError) as caught:
            deploy.SocatClient(self.connection)
        self.assertIs(caught.exception.__cause__, error)


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL generates temporary test certificates')
class SocatTlsIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='deploy-real-tls-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())

        def openssl(*args):
            subprocess.run(['openssl', *args], cwd=self.root, check=True,
                           capture_output=True, timeout=15)

        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                '-subj', '/CN=Deployment-Test-CA', '-keyout', 'ca.key', '-out', 'ca_chain.pem')
        for name, cn, usage in [('server', 'nas.123.server.2', 'serverAuth'),
                                ('123_test', 'nas.123.client.1', 'clientAuth')]:
            openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=' + cn,
                    '-keyout', name + '_private_key.pem', '-out', name + '.csr')
            ext = self.root / (name + '.ext')
            ext.write_text('extendedKeyUsage=' + usage + '\nsubjectAltName=DNS:' + cn + '\n')
            openssl('x509', '-req', '-in', name + '.csr', '-CA', 'ca_chain.pem', '-CAkey', 'ca.key',
                    '-CAcreateserial', '-days', '1', '-extfile', ext.name, '-out', name + '_cert.pem')

    def server(self, trusted=True):
        cert, key = self.root / 'server_cert.pem', self.root / 'server_private_key.pem'
        if not trusted:
            cert, key = self.root / 'untrusted.pem', self.root / 'untrusted.key'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                            '-subj', '/CN=nas.123.server.2', '-keyout', str(key), '-out', str(cert)],
                           check=True, capture_output=True, timeout=15)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        context.load_verify_locations(cafile=str(self.root / 'ca_chain.pem'))
        context.verify_mode = ssl.CERT_REQUIRED
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(.1)
        address = listener.getsockname()
        stop = threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    raw, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    raw.settimeout(2)
                    with context.wrap_socket(raw, server_side=True) as stream:
                        stream.sendall(b'ok')
                except (ssl.SSLError, OSError):
                    raw.close()

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()

        def cleanup():
            stop.set()
            listener.close()
            worker.join(3)

        self.addCleanup(cleanup)
        return address

    def test_rotated_server_name_works_with_real_ca_and_mutual_tls(self):
        address = self.server()
        connect = socket.create_connection
        connection = deploy.Connection('nas.example.test', '123', 'root', 22, None, 'native', 'socat')
        with patch.object(deploy, 'find_cert_dir', return_value=self.root), \
                patch.object(deploy.socket, 'create_connection', side_effect=lambda *a, **kw: connect(address, timeout=3)):
            client = deploy.SocatClient(connection)
        self.assertEqual(client.server_cn, 'nas.123.server.2')
        self.assertTrue(client.ctx.check_hostname)
        with connect(address, timeout=3) as raw:
            with client.ctx.wrap_socket(raw, server_hostname=client.server_cn) as stream:
                self.assertEqual(stream.recv(2), b'ok')

    def test_real_untrusted_server_is_rejected_during_probe(self):
        address = self.server(trusted=False)
        connect = socket.create_connection
        connection = deploy.Connection('nas.example.test', '123', 'root', 22, None, 'native', 'socat')
        with patch.object(deploy, 'find_cert_dir', return_value=self.root), \
                patch.object(deploy.socket, 'create_connection', side_effect=lambda *a, **kw: connect(address, timeout=3)):
            with self.assertRaises(ValueError) as caught:
                deploy.SocatClient(connection)
        self.assertIsInstance(caught.exception.__cause__, ssl.SSLCertVerificationError)


if __name__ == "__main__":
    unittest.main()

