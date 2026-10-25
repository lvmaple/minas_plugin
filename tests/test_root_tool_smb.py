"""Offline SMB regressions. No certificates, sockets, NAS, or root payloads are used."""
import copy
import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import shlex
import shutil
import subprocess
import unittest
from unittest.mock import mock_open, patch
from urllib.parse import unquote


SCRIPT = (Path(__file__).resolve().parents[1] /
          "xiaomi-nas-root-tool-1.0.9.462/xiaomi-nas-root.py")
spec = importlib.util.spec_from_file_location("minas_root_tool", SCRIPT)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)
USER = "u10001"


def account(id, username="original-user", status="1"):
    return {"id": str(id), "status": status, "username": username,
            "password": "original-password", "share_dir": ["/original/share"]}


class SetupComplete(Exception):
    """Stop main immediately after SMB setup, before key/payload generation."""


class OfflineNas:
    """Protocol fixture based on firmware NasSamba.lua's two-slot allocation.

    Deliberately allows add to overwrite the selected occupied slot, just as the
    firmware does. The client must prevent that and preserve existing records.
    """
    json_body = tool.Nas.json_body

    def __init__(self, accounts=()):
        self.uid = "10001"
        self.sn = "nas.10001.test.0"
        self.ip = "192.0.2.1"
        self.accounts = {str(a["id"]): copy.deepcopy(a) for a in accounts}
        self.calls = []
        self.webdav_calls = []
        self.foreign_names = []
        self.responses = {}
        self.add_code = 0
        self.account_reads = 0
        self.on_read = None
        self.persist_add = True
        self.persist_set = True

    def reply(self, data):
        if isinstance(data, bytes):
            return data
        return b"HTTP/1.1 200 OK\r\n\r\n" + json.dumps(data).encode()

    def duplicate(self, username, id):
        return (any(a["username"].upper() == username.upper() and existing_id != id
                    for existing_id, a in self.accounts.items()) or
                username.upper() in [name.upper() for name in self.foreign_names])

    def https(self, path, body=None, timeout=15):
        operation = path.rsplit("/", 1)[-1]
        self.calls.append((operation, copy.deepcopy(body)))
        selected = "1" if "1" not in self.accounts else "2"
        if operation == "get_pool_info":
            result = {"code": 0, "data": {"webDAV": {
                "username": USER, "password": "webdav-password", "port": 5000}}}
        elif operation == "get_samba_account":
            self.account_reads += 1
            if self.on_read:
                self.on_read(self)
            result = {"code": 0, "data": {"account": list(self.accounts.values())}}
        elif operation == "judge_samba_account":
            result = {"code": 2509 if self.duplicate(body["username"], selected) else 0}
        elif operation == "add_samba_account":
            code = 2509 if self.duplicate(body["username"], selected) else self.add_code
            if self.persist_add and code in (0, 2502):
                self.accounts[selected] = {"id": selected, "status": "1", **copy.deepcopy(body)}
            result = {"code": code}
            if code == 0:
                result["data"] = {"id": int(selected)}
        elif operation == "set_samba_account":
            id = str(body["id"])
            if self.duplicate(body["username"], id):
                result = {"code": 2509}
            elif id not in self.accounts:
                result = {"code": 2511}
            else:
                if self.persist_set:
                    self.accounts[id].update(copy.deepcopy(body))
                result = {"code": 0}
        else:
            raise AssertionError("Unexpected API: " + operation)
        return self.reply(self.responses.get(operation, result))

    def webdav(self, method, path, body=None, auth=None, timeout=20):
        self.webdav_calls.append((method, path))
        return "HTTP/1.1 201 Created\r\n\r\n"

    @property
    def writes(self):
        return [(op, body) for op, body in self.calls
                if op in ("add_samba_account", "set_samba_account", "del_samba_account")]


class RootToolSmbTests(unittest.TestCase):
    def run_setup(self, nas, *args):
        output = io.StringIO()
        with patch.object(tool, "Nas", return_value=nas), \
             patch.object(tool, "find_cert_dir", return_value="offline-certs"), \
             patch.object(tool, "ssh_key_path", side_effect=SetupComplete), \
             patch.object(tool.time, "sleep"), \
             patch.object(tool.socket, "create_connection", side_effect=AssertionError("Network forbidden")), \
             patch.object(tool.subprocess, "run", side_effect=AssertionError("Subprocess forbidden")), \
             patch("sys.argv", [str(SCRIPT), nas.ip, *args]), redirect_stdout(output):
            try:
                tool.main()
            except SetupComplete:
                return True, output.getvalue()
            except SystemExit as exc:
                self.assertEqual(exc.code, 1)
                return False, output.getvalue()
        self.fail("Main unexpectedly reached the root payload")

    def assert_preserved(self, nas, originals):
        for original in originals:
            self.assertEqual(nas.accounts[original["id"]], original)

    def test_empty_configuration_uses_actual_id_one(self):
        nas = OfflineNas()
        ready, output = self.run_setup(nas)
        self.assertTrue(ready, output)
        self.assertEqual(nas.accounts["1"]["share_dir"], ["/etc"])
        self.assertEqual([body["id"] for op, body in nas.writes if op == "set_samba_account"], ["1"])
        self.assertIn("sambashare.u10001_1", unquote(nas.webdav_calls[0][1]))

    def test_existing_other_account_is_preserved_when_creating_id_two(self):
        original = account(1, status="0")
        nas = OfflineNas([original])
        ready, output = self.run_setup(nas)
        self.assertTrue(ready, output)
        self.assert_preserved(nas, [original])
        self.assertEqual(nas.accounts["2"]["share_dir"], ["/etc"])
        self.assertGreaterEqual(nas.account_reads, 3)

    def test_existing_slot_two_is_preserved_when_slot_one_is_free(self):
        original = account(2)
        nas = OfflineNas([original])
        ready, output = self.run_setup(nas)
        self.assertTrue(ready, output)
        self.assert_preserved(nas, [original])
        self.assertEqual(nas.accounts["1"]["share_dir"], ["/etc"])

    def test_existing_same_username_at_either_id_stops_before_writes(self):
        for id in (1, 2):
            with self.subTest(id=id):
                originals = [account(id, USER)]
                nas = OfflineNas(originals)
                ready, output = self.run_setup(nas)
                self.assertFalse(ready, output)
                self.assertEqual(nas.writes, [])
                self.assertEqual(nas.webdav_calls, [])
                self.assert_preserved(nas, originals)

    def test_username_collision_is_case_insensitive(self):
        original = account(1, USER.upper())
        nas = OfflineNas([original])
        self.assertFalse(self.run_setup(nas)[0])
        self.assertEqual(nas.writes, [])
        self.assert_preserved(nas, [original])

    def test_foreign_username_collision_stops_before_webdav(self):
        nas = OfflineNas()
        nas.foreign_names = [USER]
        nas.responses["add_samba_account"] = {"code": 2509}
        nas.add_code = 2509
        self.assertFalse(self.run_setup(nas)[0])
        self.assertEqual(nas.writes, [])
        self.assertEqual(nas.webdav_calls, [])

    def test_full_slots_are_not_overwritten(self):
        originals = [account(1, "first-user"), account(2, "second-user")]
        nas = OfflineNas(originals)
        self.assertFalse(self.run_setup(nas)[0])
        self.assertEqual(nas.writes, [])
        self.assertEqual(nas.webdav_calls, [])
        self.assert_preserved(nas, originals)

    def test_failed_or_malformed_account_listing_prevents_writes(self):
        for response in ({"code": 2511}, {"code": 0}, {"data": {"account": []}},
                         {"code": 0, "data": {"account": {}}},
                         b"HTTP/1.1 502 Bad Gateway\r\n\r\nnot-json"):
            with self.subTest(response=response):
                nas = OfflineNas()
                nas.responses["get_samba_account"] = response
                self.assertFalse(self.run_setup(nas)[0])
                self.assertEqual(nas.writes, [])

    def test_add_failure_never_falls_back_to_setting_an_account(self):
        for code in (2509, 2512, 2504):
            with self.subTest(code=code):
                nas = OfflineNas()
                nas.add_code = code
                self.assertFalse(self.run_setup(nas)[0])
                self.assertEqual([op for op, _ in nas.writes], ["add_samba_account"])

    def test_restart_failure_only_repairs_verified_created_id(self):
        nas = OfflineNas()
        nas.add_code = 2502
        ready, output = self.run_setup(nas)
        self.assertTrue(ready, output)
        self.assertEqual(nas.accounts["1"]["share_dir"], ["/etc"])
        self.assertEqual([body["id"] for op, body in nas.writes if op == "set_samba_account"], ["1"])

    def test_missing_add_code_is_not_success_and_does_not_leak_response(self):
        nas = OfflineNas()
        nas.responses["add_samba_account"] = {"raw": "sensitive-server-data"}
        ready, output = self.run_setup(nas)
        self.assertFalse(ready, output)
        self.assertEqual([op for op, _ in nas.writes], ["add_samba_account"])
        self.assertNotIn("sensitive-server-data", output)

    def test_returned_id_mismatch_prevents_set(self):
        nas = OfflineNas()
        nas.responses["add_samba_account"] = {"code": 0, "data": {"id": 2}}
        self.assertFalse(self.run_setup(nas)[0])
        self.assertEqual([op for op, _ in nas.writes], ["add_samba_account"])

    def test_missing_add_id_is_recovered_from_verified_readback(self):
        nas = OfflineNas()
        nas.responses["add_samba_account"] = {"code": 0}
        ready, output = self.run_setup(nas)
        self.assertTrue(ready, output)
        self.assertEqual(nas.accounts["1"]["share_dir"], ["/etc"])

    def test_no_created_account_in_readback_prevents_set(self):
        nas = OfflineNas()
        nas.persist_add = False
        self.assertFalse(self.run_setup(nas)[0])
        self.assertEqual([op for op, _ in nas.writes], ["add_samba_account"])

    def test_changed_original_configuration_aborts_before_add(self):
        original = account(1)
        nas = OfflineNas([original])

        def change_after_snapshot(nas):
            if nas.account_reads == 2:
                nas.accounts["1"]["share_dir"] = ["/user/changed/share"]

        nas.on_read = change_after_snapshot
        self.assertFalse(self.run_setup(nas)[0])
        self.assertEqual(nas.writes, [])
        self.assertEqual(nas.accounts["1"]["share_dir"], ["/user/changed/share"])

    def test_set_response_and_readback_must_both_confirm_success(self):
        for response, persist in (({"raw": "sensitive-server-data"}, True), ({"code": 0}, False)):
            with self.subTest(response=response, persist=persist):
                nas = OfflineNas()
                nas.responses["set_samba_account"] = response
                nas.persist_set = persist
                ready, output = self.run_setup(nas)
                self.assertFalse(ready, output)
                self.assertNotIn("sensitive-server-data", output)

    def test_uid_override_must_match_authenticated_user(self):
        nas = OfflineNas()
        self.assertFalse(self.run_setup(nas, "--uid", "20002")[0])
        self.assertEqual(nas.writes, [])
        self.assertEqual(nas.webdav_calls, [])

    def test_main_passes_actual_id_to_cleanup_and_uses_repaired_share_name(self):
        for originals, expected_id in (([], "1"), ([account(1)], "2")):
            with self.subTest(id=expected_id):
                nas = OfflineNas(originals)
                with patch.object(tool, "Nas", return_value=nas), \
                     patch.object(tool, "find_cert_dir", return_value="offline-certs"), \
                     patch.object(tool, "ssh_key_path", return_value="offline-key"), \
                     patch.object(tool, "build_trigger_so"), \
                     patch.object(tool.os.path, "getsize", return_value=0), \
                     patch.object(tool, "_smb_write_file") as write, \
                     patch.object(tool, "tcp_open", return_value=True), \
                     patch.object(tool, "_cleanup_via_shell") as cleanup, \
                     patch.object(tool.socket, "create_connection", side_effect=AssertionError("Network forbidden")), \
                     patch.object(tool.subprocess, "run", side_effect=AssertionError("Subprocess forbidden")), \
                     patch("builtins.open", mock_open(read_data="offline-public-key")), \
                     patch("sys.argv", [str(SCRIPT), nas.ip]), redirect_stdout(io.StringIO()):
                    tool.main()
                self.assertEqual(write.call_count, 3)
                self.assertTrue(all(call.args[0] == r"\\192.0.2.1\etc-10001"
                                    for call in write.call_args_list))
                cleanup.assert_called_once()
                command = cleanup.call_args.args[1]
                self.assertIn("uci delete sambauser.u10001_" + expected_id, command)
                other_id = "2" if expected_id == "1" else "1"
                self.assertNotIn("uci delete sambauser.u10001_" + other_id, command)
                self.assert_preserved(nas, originals)


class RootToolCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shell = shutil.which("sh")
        if shell:
            cls.shell = [shell]
        elif os.name == "nt" and shutil.which("wsl"):
            cls.shell = [shutil.which("wsl"), "--exec", "/bin/sh"]
        else:
            raise unittest.SkipTest("POSIX shell needed for offline cleanup sandbox")

    def cleanup_command(self, id):
        return tool._disarm_command("10001", "unique-staging", id, "temporary-password")

    def run_cleanup(self, id, changes=None):
        section = "u10001_" + id
        values = {
            "sambauser." + section + ".id": id,
            "sambauser." + section + ".user": USER,
            "sambauser." + section + ".name": USER,
            "sambauser." + section + ".passwd": "temporary-password",
            "sambauser." + section + ".dirs": "/etc",
            "sambauser." + section + ".status": "1",
            "sambashare." + section + ".name": "etc-10001",
            "sambashare." + section + ".path": "/etc",
            "sambashare." + section + ".users": "sambau10001",
            "sambashare." + section + ".user_force": "root",
        }
        if changes:
            values.update(changes)
        cases = "\n".join("%s) printf '%%s\\n' %s ;;" % (shlex.quote(key), shlex.quote(value))
                          for key, value in values.items())
        # Execute the REAL generated shell command. All NAS/system mutations are
        # functions; sed is redirected to disposable passwd/shadow fixtures.
        script = '''
set -eu
diag_root=$(mktemp -d /tmp/minas-smb-cleanup.XXXXXXXX)
trap 'command rm -rf -- "$diag_root"' EXIT
printf '%s\\n' 'sambau10001:x:1700:1700::/tmp:/bin/false' \\
  'sambau100010:x:1701:1701::/tmp:/bin/false' \\
  'prefixsambau10001:x:1702:1702::/tmp:/bin/false' \\
  'original-user:x:1703:1703::/tmp:/bin/false' > "$diag_root/passwd"
cp "$diag_root/passwd" "$diag_root/shadow"
: > "$diag_root/deletes"
uci() {
  if [ "$1" = '-q' ]; then shift; fi
  case "$1" in
    get) case "$2" in
''' + cases + '''
      *) return 1 ;;
    esac ;;
    delete) printf '%s\\n' "$2" >> "$diag_root/deletes" ;;
    commit) : ;;
    *) return 1 ;;
  esac
}
rm() { :; }
sed() { command sed -i "$2" "$diag_root/passwd" "$diag_root/shadow"; }
nas() { :; }
systemctl() { :; }
''' + self.cleanup_command(id) + '''
printf '\\nDELETES\\n'
cat "$diag_root/deletes"
printf 'PASSWD\\n'
cat "$diag_root/passwd"
printf 'SHADOW\\n'
cat "$diag_root/shadow"
'''
        # Bytes retain LF on Windows; text-mode stdin would translate it to CRLF.
        result = subprocess.run(self.shell, input=script.encode("utf-8"),
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result.stdout.decode("utf-8")

    def test_cleanup_only_deletes_actual_created_slot_and_exact_system_username(self):
        for id in ("1", "2"):
            with self.subTest(id=id):
                output = self.run_cleanup(id)
                self.assertIn("CLEAN_OK", output)
                deletes = output.split("DELETES\n")[1].split("PASSWD\n")[0].splitlines()
                self.assertEqual(deletes, ["sambashare.u10001_" + id, "sambauser.u10001_" + id])
                records = output.split("PASSWD\n")[1]
                self.assertNotIn("\nsambau10001:", "\n" + records)
                self.assertEqual(records.count("sambau100010:"), 2)
                self.assertEqual(records.count("prefixsambau10001:"), 2)
                self.assertEqual(records.count("original-user:"), 2)
                self.assertNotIn("temporary-password", self.cleanup_command(id))

    def test_cleanup_skips_records_replaced_or_changed_after_setup(self):
        for field, value in (("sambauser.u10001_1.passwd", "user-changed-password"),
                             ("sambauser.u10001_1.name", "original-user"),
                             ("sambauser.u10001_1.dirs", "/original/share"),
                             ("sambauser.u10001_1.status", "0"),
                             ("sambauser.u10001_1.id", "2"),
                             ("sambauser.u10001_1.user", "u20002"),
                             ("sambashare.u10001_1.path", "/original/share"),
                             ("sambashare.u10001_1.name", "original-share"),
                             ("sambashare.u10001_1.users", "sambau10001 original-user"),
                             ("sambashare.u10001_1.user_force", "original-user")):
            with self.subTest(field=field):
                output = self.run_cleanup("1", {field: value})
                self.assertIn("SMB_CLEAN_SKIP", output)
                self.assertNotIn("CLEAN_OK", output)
                self.assertEqual(output.split("DELETES\n")[1].split("PASSWD\n")[0], "")
                self.assertEqual(sum(line.startswith("sambau10001:")
                                     for line in output.splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
