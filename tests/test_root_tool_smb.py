"""Offline SMB regressions. No certificates, sockets, NAS, or root payloads are used."""
import copy
import hashlib
import importlib.util
import io
import json
import os
import re
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
             patch.object(tool, "_cleanup_via_shell") as cleanup, \
             patch.object(tool.time, "sleep"), \
             patch.object(tool.socket, "create_connection", side_effect=AssertionError("Network forbidden")), \
             patch.object(tool.subprocess, "run", side_effect=AssertionError("Subprocess forbidden")), \
             patch("sys.argv", [str(SCRIPT), nas.ip, *args]), redirect_stdout(output):
            self.last_cleanup = cleanup
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
                     patch.object(tool, "_smb_read_file", return_value=None), \
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
                self.assertIn("rm -f /etc/ld.so.preload", command)
                self.assertIn("uci delete sambauser.u10001_" + expected_id, command)
                other_id = "2" if expected_id == "1" else "1"
                self.assertNotIn("uci delete sambauser.u10001_" + other_id, command)
                self.assert_preserved(nas, originals)

    def test_main_with_existing_preload_backs_up_and_cleans_with_restore(self):
        nas = OfflineNas()
        with patch.object(tool, "Nas", return_value=nas), \
             patch.object(tool, "find_cert_dir", return_value="offline-certs"), \
             patch.object(tool, "ssh_key_path", return_value="offline-key"), \
             patch.object(tool, "build_trigger_so"), \
             patch.object(tool.os.path, "getsize", return_value=0), \
             patch.object(tool, "_smb_read_file",
                          side_effect=[b"/vendor/preload.so\n", b"/vendor/preload.so\n"]) as read, \
             patch.object(tool, "_smb_write_file") as write, \
             patch.object(tool, "tcp_open", return_value=True), \
             patch.object(tool, "_cleanup_via_shell") as cleanup, \
             patch.object(tool.socket, "create_connection", side_effect=AssertionError("Network forbidden")), \
             patch.object(tool.subprocess, "run", side_effect=AssertionError("Subprocess forbidden")), \
             patch("builtins.open", mock_open(read_data="offline-public-key")), \
             patch("sys.argv", [str(SCRIPT), nas.ip]), redirect_stdout(io.StringIO()):
            tool.main()
        self.assertEqual(read.call_count, 2)
        self.assertEqual(write.call_count, 4)
        names = [call.args[1] for call in write.call_args_list]
        self.assertTrue(names[0].startswith("ld.so.preload.bak-"), names)
        self.assertEqual(names[1:], [".rootpwn.sh", "pwn.so", "ld.so.preload"])
        self.assertEqual(write.call_args_list[0].args[2], b"/vendor/preload.so\n")
        payload = write.call_args_list[1].args[2].decode()
        self.assertTrue(payload.startswith("#!/bin/sh\nif [ -f /etc/%s ]" % names[0]), payload[:120])
        self.assertIn("mv -f /etc/%s /etc/ld.so.preload" % names[0], payload)
        command = cleanup.call_args.args[1]
        self.assertIn("mv -f /etc/%s /etc/ld.so.preload" % names[0], command)
        self.assertNotIn("rm -f /etc/ld.so.preload /etc/pwn.so", command)

    def test_main_aborts_without_arming_when_preload_state_unknown(self):
        nas = OfflineNas()
        with patch.object(tool, "Nas", return_value=nas), \
             patch.object(tool, "find_cert_dir", return_value="offline-certs"), \
             patch.object(tool, "ssh_key_path", return_value="offline-key"), \
             patch.object(tool, "build_trigger_so"), \
             patch.object(tool.os.path, "getsize", return_value=0), \
             patch.object(tool, "_smb_read_file", side_effect=OSError("share auth failed")), \
             patch.object(tool, "_smb_write_file") as write, \
             patch.object(tool, "_cleanup_via_shell") as cleanup, \
             patch.object(tool.socket, "create_connection", side_effect=AssertionError("Network forbidden")), \
             patch.object(tool.subprocess, "run", side_effect=AssertionError("Subprocess forbidden")), \
             patch("builtins.open", mock_open(read_data="offline-public-key")), \
             patch("sys.argv", [str(SCRIPT), nas.ip]), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                tool.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(write.call_count, 0)
        cleanup.assert_called_once()
        self.assertNotIn("ld.so.preload", cleanup.call_args.args[1])

    def test_setup_failure_after_add_attempts_cleanup_and_discloses_password(self):
        nas = OfflineNas()
        nas.add_code = 2512
        ready, output = self.run_setup(nas)
        self.assertFalse(ready, output)
        self.last_cleanup.assert_called_once()
        command = self.last_cleanup.call_args.args[1]
        self.assertIn("uci delete sambauser.u10001_1", command)
        self.assertNotIn("uci delete sambauser.u10001_2", command)
        # 步骤②失败清理绝不触碰 /etc 下的文件（本阶段 payload 从未写入）
        self.assertNotIn("ld.so.preload", command)
        self.assertNotIn("pwn.so", command)
        match = re.search(r"口令[^:]*: (Rp![A-Za-z0-9_-]+)", output)
        self.assertIsNotNone(match, output)
        password = match.group(1)
        self.assertIn(hashlib.sha256(password.encode()).hexdigest(), command)
        self.assertNotIn(password, command)

    def test_setup_failure_after_verified_add_uses_readback_slot(self):
        nas = OfflineNas()
        nas.persist_set = False
        ready, output = self.run_setup(nas)
        self.assertFalse(ready, output)
        self.last_cleanup.assert_called_once()
        command = self.last_cleanup.call_args.args[1]
        self.assertIn("uci delete sambauser.u10001_1", command)
        match = re.search(r"口令[^:]*: (Rp![A-Za-z0-9_-]+)", output)
        self.assertIn(hashlib.sha256(match.group(1).encode()).hexdigest(), command)

    def test_staging_only_failure_cleans_directory_without_account_operations(self):
        original = account(1)
        nas = OfflineNas([original])

        def change_after_snapshot(nas):
            if nas.account_reads == 2:
                nas.accounts["1"]["share_dir"] = ["/user/changed/share"]

        nas.on_read = change_after_snapshot
        ready, output = self.run_setup(nas)
        self.assertFalse(ready, output)
        self.last_cleanup.assert_called_once()
        command = self.last_cleanup.call_args.args[1]
        self.assertTrue(command.startswith("rm -rf "), command)
        self.assertIn("/home/u10001/pool0/data/", command)
        self.assertNotIn("uci ", command)
        self.assertIn("CLEAN_OK", command)

    def test_pre_add_failures_do_not_invoke_cleanup(self):
        originals = [account(1, "first-user"), account(2, "second-user")]
        nas = OfflineNas(originals)
        self.assertFalse(self.run_setup(nas)[0])
        self.last_cleanup.assert_not_called()


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

    def cleanup_command(self, id, preload="keep", bak=None):
        return tool._disarm_command("10001", "unique-staging", id, "temporary-password",
                                    preload, bak)

    def sandbox(self, command, values, preload_override=None):
        """在本地 POSIX shell 沙箱执行真实生成的清理命令。uci get 从 values 取值，
        其余 uci/rm/sed/nas/systemctl 打桩，sed 指向临时 passwd/shadow 夹具。"""
        cases = "\n".join("%s) printf '%%s\\n' %s ;;" % (shlex.quote(key), shlex.quote(value))
                          for key, value in values.items())
        stubs = ""
        if preload_override is not None:
            # restore 模式：cat/mv/rm 打桩（/etc 路径不能真动，结果记入 ops）
            stubs = ("cat() { printf '%%s' %s; }\n"
                     "mv() { printf 'mv %%s\\n' \"$*\" >> \"$diag_root/ops\"; }\n"
                     % shlex.quote(preload_override))
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
: > "$diag_root/ops"
''' + stubs + '''
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
rm() { printf '%s\\n' "$*" >> "$diag_root/ops"; :; }
sed() { command sed -i "$2" "$diag_root/passwd" "$diag_root/shadow"; }
nas() { :; }
systemctl() { :; }
''' + command + '''
printf '\\nDELETES\\n'
command cat "$diag_root/deletes"
printf 'PASSWD\\n'
command cat "$diag_root/passwd" 2>/dev/null || true
printf 'SHADOW\\n'
command cat "$diag_root/shadow" 2>/dev/null || true
printf 'OPS\\n'
command cat "$diag_root/ops"
'''
        # Bytes retain LF on Windows; text-mode stdin would translate it to CRLF.
        result = subprocess.run(self.shell, input=script.encode("utf-8"),
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result.stdout.decode("utf-8")

    def run_cleanup(self, id, changes=None, preload="keep"):
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
        command = self.cleanup_command(id, preload=preload,
                                       bak="ld.so.preload.bak-1a2b3c4d" if preload == "restore" else None)
        return self.sandbox(command, values,
                            preload_override="/vendor/orig.so" if preload == "restore" else None)

    def run_setup_cleanup(self, changes=None):
        section = "u10001_1"
        values = {
            "sambauser." + section + ".id": "1",
            "sambauser." + section + ".user": USER,
            "sambauser." + section + ".name": USER,
            "sambauser." + section + ".passwd": "temporary-password",
        }
        if changes:
            values.update(changes)
        return self.sandbox(tool._disarm_setup(
            "10001", "unique-staging", "1", "temporary-password"), values)

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

    def test_disarm_preload_modes(self):
        keep = self.cleanup_command("1")
        self.assertNotIn("ld.so.preload", keep)
        self.assertIn("rm -f /etc/pwn.so /etc/.rootpwn.sh /etc/.rootpwn.log", keep)
        remove = self.cleanup_command("1", preload="remove")
        self.assertIn("rm -f /etc/ld.so.preload /etc/pwn.so /etc/.rootpwn.sh /etc/.rootpwn.log",
                      remove)
        restore = self.cleanup_command("1", preload="restore", bak="ld.so.preload.bak-1a2b3c4d")
        self.assertIn("mv -f /etc/ld.so.preload.bak-1a2b3c4d /etc/ld.so.preload", restore)
        self.assertIn("= '/etc/pwn.so' ]", restore)
        with self.assertRaises(ValueError):
            tool._disarm_command("10001", "unique-staging", "1", "temporary-password", "restore")

    def test_restore_disarm_keeps_restored_original_preload(self):
        # 备份已被消费（[ -f bak ] 为假）且当前内容是原厂 preload：不允许 rm
        output = self.run_cleanup("1", preload="restore")
        self.assertIn("CLEAN_OK", output)
        ops = output.split("OPS\n")[1]
        self.assertNotIn("mv ", ops)
        for line in ops.splitlines():
            self.assertNotEqual(line, "-f /etc/ld.so.preload", ops)
        self.assertIn("-f /etc/pwn.so /etc/.rootpwn.sh /etc/.rootpwn.log", ops)

    def test_restore_disarm_removes_preload_when_still_armed(self):
        # 备份不存在且当前内容仍是本工具布防标记：删除布防
        section = "u10001_1"
        values = {
            "sambauser." + section + ".id": "1",
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
        output = self.sandbox(self.cleanup_command("1", preload="restore",
                                                   bak="ld.so.preload.bak-1a2b3c4d"),
                              values, preload_override="/etc/pwn.so")
        self.assertIn("CLEAN_OK", output)
        ops = output.split("OPS\n")[1]
        self.assertIn("-f /etc/ld.so.preload", ops.splitlines())

    def test_setup_disarm_anchored_on_sambauser_only(self):
        # sambashare path/name 状态未知（set 未确认）：不核对也要能清（CLEAN_OK）
        output = self.run_setup_cleanup()
        self.assertIn("CLEAN_OK", output)
        deletes = output.split("DELETES\n")[1].split("PASSWD\n")[0].splitlines()
        self.assertEqual(deletes, ["sambashare.u10001_1", "sambauser.u10001_1"])
        records = output.split("PASSWD\n")[1]
        self.assertNotIn("\nsambau10001:", "\n" + records)
        self.assertIn("\noriginal-user:", "\n" + records)

    def test_setup_disarm_skips_foreign_or_missing_records(self):
        for changes in ({"sambauser.u10001_1.passwd": "user-changed-password"},
                        {"sambauser.u10001_1.user": "u20002"},
                        {"sambauser.u10001_1.id": "2"},
                        {"sambauser.u10001_1.name": "other-name"}):
            with self.subTest(changes=changes):
                output = self.run_setup_cleanup(changes)
                self.assertIn("SMB_CLEAN_SKIP", output)
                self.assertNotIn("CLEAN_OK", output)
                self.assertEqual(output.split("DELETES\n")[1].split("PASSWD\n")[0], "")
                self.assertEqual(sum(line.startswith("sambau10001:")
                                     for line in output.splitlines()), 2)

    def test_setup_disarm_never_touches_etc_files(self):
        command = tool._disarm_setup("10001", "unique-staging", "1", "temporary-password")
        self.assertNotIn("ld.so.preload", command)
        self.assertNotIn("pwn.so", command)
        self.assertNotIn(".rootpwn", command)
        self.assertTrue(command.startswith("rm -rf "))


class PayloadAndReadHelperTests(unittest.TestCase):
    def test_payload_without_backup_removes_preload(self):
        script = tool._payload_script("ssh-ed25519 AAAA minas-root", 2323, None)
        self.assertEqual(script.splitlines()[1], "rm -f /etc/ld.so.preload")
        self.assertIn("ssh-ed25519 AAAA minas-root", script)
        self.assertIn("TCP-LISTEN:2323", script)

    def test_payload_with_backup_restores_it_before_anything_else(self):
        script = tool._payload_script("ssh-ed25519 AAAA minas-root", 2323,
                                      "ld.so.preload.bak-1a2b3c4d")
        lines = script.splitlines()
        self.assertTrue(lines[1].startswith("if [ -f /etc/ld.so.preload.bak-1a2b3c4d ]"), lines[1])
        self.assertIn("mv -f /etc/ld.so.preload.bak-1a2b3c4d /etc/ld.so.preload", lines[1])
        self.assertIn("\"$(cat /etc/ld.so.preload 2>/dev/null)\" = '/etc/pwn.so'", lines[1])
        self.assertNotEqual(lines[1], "rm -f /etc/ld.so.preload")

    def test_smb_read_file_missing_maps_to_none_when_authenticated(self):
        with patch.object(tool.os, "name", "nt"), \
             patch.object(tool.subprocess, "run") as run, \
             patch("builtins.open", side_effect=FileNotFoundError):
            run.return_value.returncode = 0
            self.assertIsNone(tool._smb_read_file(r"\\nas\etc-1", "ld.so.preload", "pw"))
        self.assertEqual(run.call_count, 2)

    def test_smb_read_file_returns_bytes_on_direct_hit(self):
        with patch.object(tool.os, "name", "nt"), \
             patch.object(tool.subprocess, "run", side_effect=AssertionError("mount unexpected")), \
             patch("builtins.open", mock_open(read_data=b"/vendor/preload.so\n")):
            self.assertEqual(tool._smb_read_file(r"\\nas\etc-1", "ld.so.preload", "pw"),
                             b"/vendor/preload.so\n")

    def test_smb_read_file_rejects_unverified_absence(self):
        with patch.object(tool.os, "name", "nt"), \
             patch.object(tool.subprocess, "run") as run, \
             patch("builtins.open", side_effect=FileNotFoundError):
            run.return_value.returncode = 1219  # 挂载失败：不能把读失败当成文件不存在
            with self.assertRaises(OSError):
                tool._smb_read_file(r"\\nas\etc-1", "ld.so.preload", "pw")


if __name__ == "__main__":
    unittest.main()
