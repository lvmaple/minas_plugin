"""Normal incremental backups and recovery through the actual service contract."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen


SOURCE = Path(__file__).resolve().parents[1] / "xiaomi-nas-plugin-pcbackup/src/files/httpd.py"
spec = importlib.util.spec_from_file_location("pcbackup_workflow_backend", SOURCE)
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="pcbackup-workflow-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        backend.DATA_DIR = str(self.root / "etc")
        backend.JOBS_DIR = str(self.root / "etc/jobs")
        backend.JOBS_FILE = str(self.root / "etc/jobs.json")
        backend.HISTORY_FILE = str(self.root / "etc/history.json")
        backend.USER_DATA_ROOT = str(self.root / "pool")
        for name in ("_MANIFEST_CACHE", "_MANIFEST_PATH_CACHE", "_RUN_CURRENT", "_RESUME_ID_CACHE", "_SCAN_STATE"):
            getattr(backend, name).clear()
        self.job = {"id": "1234abcd", "name": "workflow", "exclude": [], "pc_path": r"D:\docs",
                    "destination": str(self.root / "pool/backup")}
        Path(self.job["destination"]).mkdir(parents=True)
        backend.save_jobs({self.job["id"]: self.job})
        backend.manifest_meta(self.job, "docs")
        self.mtime = 1700000000000

    def seed(self, rel, data=b"hello"):
        path = Path(self.job["destination"]) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, ns=(self.mtime * 1000000, self.mtime * 1000000))
        backend.manifest_upsert(self.job, [(rel, {"s": len(data), "m": self.mtime})])
        return path

    def scan(self, files, **options):
        code, out = backend.build_plan(self.job, dict({"folder": "docs", "files": files}, **options))
        self.assertEqual(code, 200, out)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = backend._SCAN_STATE[self.job["id"]]
            if state["phase"] in ("done", "error"):
                self.assertEqual(state["phase"], "done", state.get("error"))
                return state["plan"]
            time.sleep(.001)
        self.fail("scan did not complete")

    def checkpoint(self, plan, browser=False, prune=False):
        code, info = backend.start_resume(self.job, {
            "scan_id": plan["scan_id"], "generation": plan["generation"],
            "source_path": "" if browser else self.job["pc_path"],
            "source_kind": "browser" if browser else "native",
            "del_mode": "del", "prune_excluded": prune})
        self.assertEqual(code, 200, info)
        return info

    def request(self, route, body=None, method="POST"):
        if not getattr(self, "server", None):
            class QuietHandler(backend.Handler):
                def log_message(self, *args):
                    pass
            self.server = backend.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            self.addCleanup(self.server.server_close)
            self.addCleanup(self.server.shutdown)
        req = Request("http://127.0.0.1:%d/api/jobs/%s/%s" % (
            self.server.server_port, self.job["id"], route), method=method,
            data=(body if isinstance(body, bytes) else json.dumps(body).encode()) if body is not None else None,
            headers={"Content-Type": "application/octet-stream" if isinstance(body, bytes) else "application/json"})
        try:
            with urlopen(req, timeout=3) as stream:
                return stream.status, json.load(stream)["data"]
        except HTTPError as error:
            return error.code, json.load(error)

    def native(self, rel, data=b"hello", mtime=None, finish=True):
        body = {"p": rel, "s": len(data), "m": mtime or self.mtime,
                "source": self.job["pc_path"] + "\\" + rel.replace("/", "\\")}
        code, info = backend.prepare_native_upload(self.job, body)
        self.assertEqual(code, 200, info)
        if finish:
            (Path(backend.USER_DATA_ROOT) / ".temp_upload_dir" / info["ready_name"]).write_bytes(data)
            self.assertEqual(backend.finalize_native_upload(self.job, {"p": rel, "token": info["token"]})[0], 200)
        return info

    def stats(self, info, **kwargs):
        return dict({"run_id": info["run_id"], "generation": info["generation"],
                     "request_id": uuid.uuid4().hex, "failed": 0}, **kwargs)

    def test_successfully_read_empty_source_marks_last_file_deleted(self):
        path = self.seed("last.txt")
        plan = self.scan([])
        self.assertEqual(plan["deleted"], ["last.txt"])
        info = self.checkpoint(plan)
        self.assertEqual(backend.tombstone(self.job, plan["deleted"], "del")[0], 200)
        self.assertEqual(backend.finish_run(self.job, self.stats(info))[1]["status"], "ok")
        self.assertFalse(path.exists())
        self.assertEqual(Path(str(path) + ".del").read_bytes(), b"hello")

    def test_all_source_files_excluded_can_still_be_cleaned(self):
        path = self.seed("old.tmp")
        self.job["exclude"] = ["*.tmp"]
        backend.save_jobs({self.job["id"]: self.job})
        plan = self.scan([])
        self.assertEqual(plan["deleted"], [])
        self.assertEqual(plan["excluded_kept"], 1)
        info = self.checkpoint(plan, prune=True)
        self.assertEqual(backend.finish_run(self.job, self.stats(info, prune_excluded=True))[1]["status"], "ok")
        self.assertFalse(path.exists())

    def test_browser_cancel_reopen_preserves_plan_and_partial_bytes(self):
        plan = self.scan([{"p": "a.txt", "s": 5, "m": self.mtime}])
        info = self.checkpoint(plan, browser=True)
        backend.put_chunk(self.job, "a.txt", 0, b"he", expected_generation=plan["generation"])
        self.assertEqual(backend.finish_run(self.job, self.stats(info, cancelled=True))[1]["status"], "cancelled")
        backend._RESUME_ID_CACHE.clear()
        code, reopened = backend.resume_status(self.job)
        self.assertEqual(code, 200)
        self.assertEqual(reopened["source_kind"], "browser")
        self.assertEqual(reopened["run_id"], info["run_id"])
        self.assertEqual(backend.part_size(self.job, "a.txt")[1]["size"], 2)
        backend.put_chunk(self.job, "a.txt", 2, b"llo", expected_generation=plan["generation"])
        self.assertEqual(backend.commit_file(self.job, "a.txt", 5, self.mtime)[0], 200)
        self.assertEqual(backend.finish_run(self.job, self.stats(info))[1]["status"], "ok")
        self.assertEqual((Path(self.job["destination"]) / "a.txt").read_bytes(), b"hello")

    def test_directory_to_file_preserves_old_backup_and_retries_preparation(self):
        self.seed("foo/bar.txt", b"old backup")
        plan = self.scan([{"p": "foo", "s": 5, "m": self.mtime + 1000}])
        info = self.checkpoint(plan)
        backend.tombstone(self.job, plan["deleted"], "del")
        args = {"run_id": info["run_id"], "generation": plan["generation"]}
        code, out = self.request("prepare-paths", args)
        self.assertEqual(code, 200, out)
        archive = plan["type_archives"]["foo"]
        self.assertEqual((Path(self.job["destination"]) / archive / "bar.txt.del").read_bytes(), b"old backup")
        self.native("foo", b"world", self.mtime + 1000)
        self.assertEqual(self.request("prepare-paths", args)[0], 200, "a lost ACK must be safe to retry")
        self.assertEqual(backend.finish_run(self.job, self.stats(info))[1]["status"], "ok")
        self.assertEqual((Path(self.job["destination"]) / "foo").read_bytes(), b"world")

    def test_type_archive_preserves_existing_del_and_excluded_cleanup(self):
        self.seed("foo/bar.txt")
        self.seed("foo/ignored.tmp")
        backend.manifest_exclude(self.job, ["foo/ignored.tmp"])
        existing = Path(self.job["destination"]) / "foo.del"
        existing.write_bytes(b"previous retained backup")
        plan = self.scan([{"p": "foo", "s": 5, "m": self.mtime + 1000}])
        info = self.checkpoint(plan, prune=True)
        backend.tombstone(self.job, plan["deleted"], "del")
        args = {"run_id": info["run_id"], "generation": plan["generation"]}
        code, out = self.request("prepare-paths", args)
        self.assertEqual(code, 200, out)
        archive = plan["type_archives"]["foo"]
        self.assertEqual(backend.load_manifest(self.job)["excluded"], [archive + "/ignored.tmp"])
        self.native("foo", b"world", self.mtime + 1000)
        self.assertEqual(backend.finish_run(self.job, self.stats(info, prune_excluded=True))[1]["status"], "ok")
        self.assertEqual(existing.read_bytes(), b"previous retained backup")
        self.assertFalse((Path(self.job["destination"]) / archive / "ignored.tmp").exists())

    def test_cleanup_failure_retains_retry_paths_and_never_reports_success(self):
        self.seed("kept.txt")
        target = self.seed("old.tmp")
        backend.manifest_exclude(self.job, ["old.tmp"])
        plan = self.scan([{"p": "kept.txt", "s": 5, "m": self.mtime}])
        info = self.checkpoint(plan, prune=True)
        stats = self.stats(info, prune_excluded=True)
        original = backend.os.remove
        def fail(path):
            if os.fspath(path) == str(target):
                raise OSError("temporarily unable to delete")
            return original(path)
        with patch.object(backend.os, "remove", side_effect=fail):
            with self.assertRaises(OSError):
                backend.finish_run(self.job, stats)
        self.assertEqual(backend.load_manifest(self.job)["excluded"], ["old.tmp"])
        self.assertTrue(Path(backend.resume_path(self.job)).exists())
        self.assertEqual(backend.finish_run(self.job, stats)[1]["status"], "ok")
        self.assertFalse(target.exists())
        self.assertEqual(len(backend.load_history()), 1)

    def test_normal_native_modification_does_not_write_empty_retirement_records(self):
        self.native("a.txt")
        original = backend._save_json
        writes = []
        def track(path, value):
            writes.append(os.fspath(path))
            return original(path, value)
        with patch.object(backend, "_save_json", side_effect=track):
            self.native("a.txt", mtime=self.mtime + 1000, finish=False)
        self.assertFalse(any("native-retired" in path for path in writes), writes)
        self.assertEqual(len(writes), 1)

    def test_late_source_patch_cannot_override_a_newer_selection(self):
        original = self.job["pc_path"]
        self.assertEqual(backend.update_job(self.job["id"], {
            "pc_path": r"E:\docs", "expected_pc_path": original})[0], 200)
        code, out = backend.update_job(self.job["id"], {
            "pc_path": r"F:\docs", "expected_pc_path": original})
        self.assertEqual(code, 409)
        self.assertEqual(out["reason"], "source_changed")
        self.assertEqual(backend.load_jobs()[self.job["id"]]["pc_path"], r"E:\docs")

    def test_empty_source_still_uses_the_existing_mass_deletion_confirmation(self):
        for n in range(10):
            self.seed("%d.txt" % n)
        code, out = backend.build_plan(self.job, {"folder": "docs", "files": []})
        self.assertEqual(code, 409)
        self.assertEqual(out["reason"], "deleted_ratio")
        self.assertEqual(len(self.scan([], force=True)["deleted"]), 10)

    def test_type_preparation_recovers_a_rename_before_journal_failure(self):
        self.seed("foo/bar.txt")
        self.seed("foo/ignored.tmp")
        backend.manifest_exclude(self.job, ["foo/ignored.tmp"])
        plan = self.scan([{"p": "foo", "s": 5, "m": self.mtime + 1000}])
        info = self.checkpoint(plan)
        backend.tombstone(self.job, plan["deleted"], "del")
        args = {"run_id": info["run_id"], "generation": plan["generation"]}
        original = backend._run_append
        def fail(job, lines):
            if any(line["op"] == "excl_move" for line in lines):
                raise OSError("temporary journal failure")
            return original(job, lines)
        with patch.object(backend, "_run_append", side_effect=fail):
            self.assertEqual(self.request("prepare-paths", args)[0], 500)
        self.assertEqual(backend.load_manifest(self.job)["excluded"], ["foo/ignored.tmp"])
        self.assertEqual(self.request("prepare-paths", args)[0], 200)
        archived = plan["type_archives"]["foo"] + "/ignored.tmp"
        backend._MANIFEST_CACHE.clear()
        self.assertEqual(backend.load_manifest(self.job)["excluded"], [archived])
        self.assertEqual((Path(self.job["destination"]) / archived).read_bytes(), b"hello")

    def test_browser_http_scan_cancel_reopen_retry_commit_and_finish(self):
        code, out = self.request("scan", {"folder": "docs", "files": [{"p": "a.txt", "s": 5, "m": self.mtime}]})
        self.assertEqual(code, 200, out)
        def poll(route):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                code, result = self.request(route, method="GET")
                self.assertEqual(code, 200, result)
                if result["phase"] == "done":
                    return result
                time.sleep(.001)
            self.fail("operation did not settle: " + route)
        plan = poll("scan-status")["plan"]
        code, info = self.request("resume", {"generation": plan["generation"], "scan_id": plan["scan_id"],
                                            "source_kind": "browser", "source_path": "", "del_mode": "del"})
        self.assertEqual(code, 200, info)
        suffix = "&generation=" + plan["generation"]
        self.assertEqual(self.request("data?path=a.txt&offset=0" + suffix, b"he", "PUT")[0], 200)
        cancelled = self.stats(info, cancelled=True, **{"async": True})
        self.assertEqual(self.request("finish", cancelled)[0], 200)
        self.assertEqual(poll("finish-status?request_id=" + cancelled["request_id"])["result"]["status"], "cancelled")
        reopened = poll("resume?async=1&request_id=" + uuid.uuid4().hex)["result"]
        self.assertEqual(reopened["run_id"], info["run_id"])
        self.assertEqual(self.request("part?path=a.txt" + suffix, method="GET")[1]["size"], 2)
        for _ in range(2):
            self.assertEqual(self.request("data?path=a.txt&offset=2" + suffix, b"llo", "PUT")[0], 200)
        self.assertEqual(self.request("commit", {"p": "a.txt", "s": 5, "m": self.mtime, "generation": plan["generation"]})[0], 200)
        completed = self.stats(info, uploaded=1, **{"async": True})
        self.assertEqual(self.request("finish", completed)[0], 200)
        self.assertEqual(poll("finish-status?request_id=" + completed["request_id"])["result"]["status"], "ok")
        with urlopen("http://127.0.0.1:%d/api/jobs/%s/download?path=a.txt" % (self.server.server_port, self.job["id"]), timeout=2) as stream:
            self.assertEqual(stream.read(), b"hello")


if __name__ == "__main__":
    unittest.main()
