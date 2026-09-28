import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


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
                         port=None, transport=None)
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
                                  "--select", "none"])
        self.assertEqual(result, 0)
        check.assert_called_once()
        install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
