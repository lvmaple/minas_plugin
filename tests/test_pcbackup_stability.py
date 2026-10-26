"""Isolated regressions for scan integrity, shared history and stale requests."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen


SOURCE = Path(__file__).resolve().parents[1] / "xiaomi-nas-plugin-pcbackup/src/files/httpd.py"
spec = importlib.util.spec_from_file_location("pcbackup_stability_backend", SOURCE)
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class StabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pcbackup-stability-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        backend.DATA_DIR = str(self.root / "etc")
        backend.JOBS_DIR = str(self.root / "etc/jobs")
        backend.JOBS_FILE = str(self.root / "etc/jobs.json")
        backend.HISTORY_FILE = str(self.root / "etc/history.json")
        backend.USER_DATA_ROOT = str(self.root / "pool")
        for name in ("_MANIFEST_CACHE", "_MANIFEST_PATH_CACHE", "_RUN_CURRENT",
                     "_SCAN_STATE", "_RESUME_ID_CACHE"):
            getattr(backend, name).clear()
        self.job = {"id": "stable01", "name": "test", "exclude": [],
                    "destination": str(self.root / "pool/backup"), "pc_path": r"D:\docs"}
        Path(self.job["destination"]).mkdir(parents=True)
        backend.save_jobs({self.job["id"]: self.job})
        self.mtime = 1700000000000
        self.server = None

    def scan(self, files, **extra):
        job = backend.load_jobs()[self.job["id"]]
        code, data = backend.build_plan(job, dict(folder="docs", files=files, **extra))
        self.assertEqual(code, 200, data)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = backend._SCAN_STATE[job["id"]]
            if state["phase"] in ("done", "error"):
                self.assertEqual(state["phase"], "done", state)
                return state["plan"]
            time.sleep(.001)
        self.fail("scan did not settle")

    def start(self, plan):
        job = backend.load_jobs()[self.job["id"]]
        code, data = backend.start_resume(job, {"scan_id": plan["scan_id"],
                                               "source_path": r"D:\docs"})
        self.assertEqual(code, 200, data)
        return data

    def request(self, method, route, body=None, generation=None):
        if self.server is None:
            class QuietHandler(backend.Handler):
                def log_message(self, *args):
                    pass
            self.server = backend.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            self.addCleanup(self.server.server_close)
            self.addCleanup(self.server.shutdown)
        url = "http://127.0.0.1:%d/api/jobs/%s/%s" % (
            self.server.server_port, self.job["id"], route)
        headers = {}
        if isinstance(body, dict):
            if generation is not None:
                body = dict(body, generation=generation)
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        else:
            data = body
            if generation is not None:
                url += ("&" if "?" in url else "?") + "generation=" + generation
        try:
            with urlopen(Request(url, method=method, data=data, headers=headers), timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_corrupt_checkpoint_requires_explicit_replacement(self):
        backend.manifest_meta(self.job, "docs")
        generation = backend._native_generation(self.job)
        path = Path(backend.resume_path(self.job))
        body = {"folder": "docs", "files": [{"p": "new.txt", "s": 5, "m": self.mtime}]}
        for raw in (b"{invalid json", b"[]", b'{"version": 2}', b"\xff"):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                code, out = self.request("POST", "scan", body)
                self.assertEqual(code, 409, out)
                self.assertEqual(out["data"]["reason"], "resume_corrupt")
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(backend._native_generation(self.job), generation)
                self.assertEqual(list(path.parent.glob(path.name + ".corrupt-*")), [])

    def test_explicit_rescan_preserves_corrupt_record_backup_and_partial_data(self):
        backup = Path(self.job["destination"]) / "keep.txt"
        backup.write_bytes(b"hello")
        os.utime(backup, ns=(self.mtime * 1000000, self.mtime * 1000000))
        backend.manifest_meta(self.job, "docs")
        backend.manifest_upsert(self.job, [("keep.txt", {"s": 5, "m": self.mtime})])
        backend.put_chunk(self.job, "pending.txt", 0, b"he")
        path = Path(backend.resume_path(self.job))
        raw = b"{invalid checkpoint"
        path.write_bytes(raw)
        old_generation = backend._native_generation(self.job)
        plan = self.scan([{"p": "keep.txt", "s": 5, "m": self.mtime},
                          {"p": "pending.txt", "s": 5, "m": self.mtime}], replace_resume=True)
        copies = list(path.parent.glob(path.name + ".corrupt-*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), raw)
        self.assertNotEqual(plan["generation"], old_generation)
        self.assertEqual(plan["skipped"], 1)
        self.assertEqual(plan["upload"], [["pending.txt", 5]])
        self.assertEqual(backup.read_bytes(), b"hello")
        self.assertEqual(backend.part_size(self.job, "pending.txt")[1]["size"], 0)
        self.assertEqual((Path(self.job["destination"]) / ("pending.txt" + backend.PART_SUFFIX)).read_bytes(), b"he")
        info = self.start(plan)
        self.assertEqual(info["files"], [["pending.txt", 5, self.mtime]])
        backend.put_chunk(self.job, "pending.txt", 0, b"hello")
        backend.commit_file(self.job, "pending.txt", 5, self.mtime)
        self.assertEqual(backend.finish_run(self.job, {"run_id": info["run_id"]})[1]["status"], "ok")

    def test_corrupt_replacement_waits_for_scan_guards(self):
        backend.manifest_meta(self.job, "docs")
        generation = backend._native_generation(self.job)
        path = Path(backend.resume_path(self.job))
        path.write_bytes(b"{broken")
        files = [{"p": "new.txt", "s": 5, "m": self.mtime}]
        for body, expected in (({"folder": "different", "files": files}, 409),
                               ({"folder": "docs", "files": [{"p": "../bad", "s": 5, "m": 1}]}, 400)):
            with self.subTest(body=body):
                code, _ = self.request("POST", "scan", dict(body, replace_resume=True))
                self.assertEqual(code, expected)
                self.assertEqual(path.read_bytes(), b"{broken")
                self.assertEqual(backend._native_generation(self.job), generation)
                self.assertEqual(list(path.parent.glob(path.name + ".corrupt-*")), [])

        backend.manifest_upsert(self.job, [("old-%d.txt" % n, {"s": 5, "m": self.mtime}) for n in range(11)])
        code, out = self.request("POST", "scan", {"folder": "docs", "files": files, "replace_resume": True})
        self.assertEqual(code, 409)
        self.assertEqual(out["data"]["reason"], "deleted_ratio")
        self.assertEqual(path.read_bytes(), b"{broken")
        self.assertEqual(backend._native_generation(self.job), generation)
        self.assertEqual(list(path.parent.glob(path.name + ".corrupt-*")), [])

    def test_explicit_replacement_does_not_swallow_checkpoint_io_failure(self):
        backend.manifest_meta(self.job, "docs")
        generation = backend._native_generation(self.job)
        path = Path(backend.resume_path(self.job))
        path.write_bytes(b"{broken")
        original = open

        def unreadable(filename, *args, **kwargs):
            if str(filename) == str(path):
                raise PermissionError("checkpoint temporarily unreadable")
            return original(filename, *args, **kwargs)

        with patch.object(backend, "open", side_effect=unreadable, create=True):
            code, _ = self.request("POST", "scan", {
                "folder": "docs", "files": [{"p": "new.txt", "s": 5, "m": self.mtime}], "replace_resume": True})
            self.assertEqual(self.request("PATCH", "", {"pc_path": r"D:\different"})[0], 500)
        self.assertEqual(code, 500)
        self.assertEqual(backend.load_jobs()[self.job["id"]]["pc_path"], self.job["pc_path"])
        self.assertEqual(path.read_bytes(), b"{broken")
        self.assertEqual(backend._native_generation(self.job), generation)
        self.assertEqual(list(path.parent.glob(path.name + ".corrupt-*")), [])

    def test_corrupt_checkpoint_quarantine_failure_can_retry(self):
        backend.manifest_meta(self.job, "docs")
        backend.put_chunk(self.job, "new.txt", 0, b"he")
        path = Path(backend.resume_path(self.job))
        path.write_bytes(b"{broken")
        original = backend.os.replace

        def fail_quarantine(source, target):
            if str(source) == str(path):
                raise OSError("quarantine failed")
            return original(source, target)

        body = {"folder": "docs", "files": [{"p": "new.txt", "s": 5, "m": self.mtime}], "replace_resume": True}
        with patch.object(backend.os, "replace", side_effect=fail_quarantine):
            self.assertEqual(self.request("POST", "scan", body)[0], 500)
        self.assertEqual(path.read_bytes(), b"{broken")
        self.assertEqual((Path(self.job["destination"]) / ("new.txt" + backend.PART_SUFFIX)).read_bytes(), b"he")
        plan = self.scan(body["files"], replace_resume=True)
        self.assertEqual(plan["upload"], [["new.txt", 5]])
        self.assertEqual(len(list(path.parent.glob(path.name + ".corrupt-*"))), 1)

    def test_corrupt_rescan_generation_failure_keeps_original_record(self):
        backend.manifest_meta(self.job, "docs")
        generation = backend._native_generation(self.job)
        path = Path(backend.resume_path(self.job))
        path.write_bytes(b"{broken")
        original = backend._save_json

        def fail_generation(filename, value):
            if str(filename).endswith("native-generation.json"):
                raise OSError("generation write failed")
            return original(filename, value)

        body = {"folder": "docs", "files": [{"p": "new.txt", "s": 5, "m": self.mtime}], "replace_resume": True}
        with patch.object(backend, "_save_json", side_effect=fail_generation):
            self.assertEqual(self.request("POST", "scan", body)[0], 500)
        self.assertEqual(path.read_bytes(), b"{broken")
        self.assertEqual(backend._native_generation(self.job), generation)
        self.assertEqual(list(path.parent.glob(path.name + ".corrupt-*")), [])
        self.assertEqual(self.scan(body["files"], replace_resume=True)["upload"], [["new.txt", 5]])

    def test_corrupt_rescan_thread_start_failure_releases_scan_state_for_retry(self):
        backend.manifest_meta(self.job, "docs")
        path = Path(backend.resume_path(self.job))
        backend.put_chunk(self.job, "new.txt", 0, b"he")
        path.write_bytes(b"{broken")
        body = {"folder": "docs", "files": [{"p": "new.txt", "s": 5, "m": self.mtime}], "replace_resume": True}
        with patch.object(backend.threading.Thread, "start", side_effect=RuntimeError("cannot start scan thread")):
            with self.assertRaisesRegex(RuntimeError, "cannot start scan"):
                backend.build_plan(self.job, body)
        self.assertEqual(backend._SCAN_STATE[self.job["id"]]["phase"], "error")
        self.assertEqual(backend.part_size(self.job, "new.txt")[1]["size"], 0)
        self.assertEqual((Path(self.job["destination"]) / ("new.txt" + backend.PART_SUFFIX)).read_bytes(), b"he")
        copies = list(path.parent.glob(path.name + ".corrupt-*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), b"{broken")
        self.assertEqual(self.scan(body["files"], replace_resume=True)["upload"], [["new.txt", 5]])

    def test_corrupt_rescan_allows_native_source_selection_before_confirmation(self):
        backend.manifest_meta(self.job, "docs")
        generation = backend._native_generation(self.job)
        path = Path(backend.resume_path(self.job))
        backend.put_chunk(self.job, "new.txt", 0, b"he")
        path.write_bytes(b"{broken")
        code, out = self.request("PATCH", "", {"pc_path": r"D:\reselected"})
        self.assertEqual(code, 200, out)
        job = backend.load_jobs()[self.job["id"]]
        self.assertEqual(job["pc_path"], r"D:\reselected")
        self.assertNotEqual(backend._native_generation(job), generation)
        self.assertEqual(path.read_bytes(), b"{broken")
        self.assertEqual(list(path.parent.glob(path.name + ".corrupt-*")), [])
        self.assertEqual((Path(job["destination"]) / ("new.txt" + backend.PART_SUFFIX)).read_bytes(), b"he")
        files = [{"p": "new.txt", "s": 5, "m": self.mtime}]
        self.assertEqual(self.request("POST", "scan", {"folder": "docs", "files": files})[0], 409)
        plan = self.scan(files, replace_resume=True)
        code, info = backend.start_resume(job, {"scan_id": plan["scan_id"], "source_path": r"D:\reselected"})
        self.assertEqual(code, 200, info)
        self.assertEqual(info["source_path"], r"D:\reselected")
        self.assertEqual(backend.part_size(job, "new.txt")[1]["size"], 0)
        self.assertEqual((Path(job["destination"]) / ("new.txt" + backend.PART_SUFFIX)).read_bytes(), b"he")

    def check_replaced_chunk_identity(self, *, source_changed=False, damaged=False, content=b"world"):
        old_plan = self.scan([{"p": "item.txt", "s": 5, "m": self.mtime}])
        self.start(old_plan)
        backend.put_chunk(self.job, "item.txt", 0, b"he")
        part = Path(self.job["destination"]) / ("item.txt" + backend.PART_SUFFIX)
        if damaged:
            Path(backend.resume_path(self.job)).write_bytes(b"{broken")
        if source_changed:
            backend.update_job(self.job["id"], {"pc_path": r"D:\new-source"})
        job = backend.load_jobs()[self.job["id"]]
        mtime = self.mtime if source_changed else self.mtime + 5000
        plan = self.scan([{"p": "item.txt", "s": len(content), "m": mtime}], replace_resume=True)
        code, info = backend.start_resume(job, {"scan_id": plan["scan_id"], "source_path": job["pc_path"]})
        self.assertEqual(code, 200, info)
        self.assertEqual(backend.part_size(job, "item.txt")[1]["size"], 0)
        self.assertEqual(part.read_bytes(), b"he", "a size query must retain old bytes")
        self.assertEqual(backend.commit_file(job, "item.txt", len(content), mtime)[0], 409)
        self.assertEqual(backend.put_chunk(job, "item.txt", 2, content[2:])[0], 409)
        self.assertEqual(part.read_bytes(), b"he")
        self.assertEqual(backend.put_chunk(job, "item.txt", 0, content[:2])[0], 200)
        copies = list((Path(job["destination"]) / backend.MIRROR_DIRNAME / "partial-recovery").glob("*.pcbp.part"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), b"he")
        backend.finish_run(job, {"run_id": info["run_id"], "cancelled": True})
        for name in ("_MANIFEST_CACHE", "_RESUME_ID_CACHE", "_RUN_CURRENT"):
            getattr(backend, name).clear()
        self.assertEqual(backend.resume_status(job)[0], 200)
        self.assertEqual(backend.part_size(job, "item.txt")[1]["size"], 2)
        self.assertEqual(backend.put_chunk(job, "item.txt", 2, content[2:])[0], 200)
        self.assertEqual(backend.commit_file(job, "item.txt", len(content), mtime)[0], 200)
        self.assertEqual(backend.finish_run(job, {"run_id": info["run_id"]})[1]["status"], "ok")
        self.assertEqual((Path(job["destination"]) / "item.txt").read_bytes(), content)

    def test_changed_source_does_not_reuse_old_chunk_prefix(self):
        self.check_replaced_chunk_identity(source_changed=True)

    def test_changed_file_metadata_does_not_reuse_old_chunk_prefix(self):
        self.check_replaced_chunk_identity(content=b"world!")

    def test_damaged_checkpoint_rescan_does_not_mix_old_chunk_with_new_source(self):
        self.check_replaced_chunk_identity(source_changed=True, damaged=True)

    def test_unbound_legacy_chunk_requires_restart_without_deleting_old_bytes(self):
        part = Path(self.job["destination"]) / ("old.txt" + backend.PART_SUFFIX)
        part.write_bytes(b"he")
        self.assertEqual(backend.part_size(self.job, "old.txt")[1]["size"], 0)
        self.assertEqual(backend.put_chunk(self.job, "old.txt", 2, b"rld")[0], 409)
        self.assertEqual(backend.commit_file(self.job, "old.txt", 5, self.mtime)[0], 409)
        self.assertEqual(part.read_bytes(), b"he")
        self.assertEqual(backend.put_chunk(self.job, "old.txt", 0, b"world")[0], 200)
        self.assertEqual(backend.commit_file(self.job, "old.txt", 5, self.mtime)[0], 200)
        self.assertEqual((Path(self.job["destination"]) / "old.txt").read_bytes(), b"world")

    def test_chunk_identity_read_failure_is_retryable_without_losing_progress(self):
        plan = self.scan([{"p": "item.txt", "s": 5, "m": self.mtime}])
        self.start(plan)
        backend.put_chunk(self.job, "item.txt", 0, b"he")
        metadata = next((Path(backend.job_dir(self.job["id"])) / "parts").glob("*.json"))
        saved = metadata.read_bytes()
        original = open

        def fail_metadata(filename, *args, **kwargs):
            if str(filename) == str(metadata):
                raise OSError("part identity temporarily unreadable")
            return original(filename, *args, **kwargs)

        with patch.object(backend, "open", side_effect=fail_metadata, create=True):
            self.assertEqual(self.request("GET", "part?path=item.txt", generation=plan["generation"])[0], 500)
        self.assertEqual(metadata.read_bytes(), saved)
        self.assertEqual(backend.part_size(self.job, "item.txt")[1]["size"], 2)

    def test_chunk_identity_write_failure_does_not_authorize_unrecorded_bytes(self):
        original = backend._save_json

        def fail_metadata(filename, value):
            if Path(filename).parent.name == "parts":
                raise OSError("part identity disk full")
            return original(filename, value)

        with patch.object(backend, "_save_json", side_effect=fail_metadata):
            with self.assertRaisesRegex(OSError, "identity disk full"):
                backend.put_chunk(self.job, "item.txt", 0, b"he")
        self.assertEqual(backend.part_size(self.job, "item.txt")[1]["size"], 0)
        self.assertEqual(backend.put_chunk(self.job, "item.txt", 2, b"rld")[0], 409)
        self.assertEqual(backend.put_chunk(self.job, "item.txt", 0, b"world")[0], 200)
        self.assertEqual(backend.commit_file(self.job, "item.txt", 5, self.mtime)[0], 200)
        self.assertEqual((Path(self.job["destination"]) / "item.txt").read_bytes(), b"world")

    def test_changed_partial_file_is_not_authorized_by_saved_identity(self):
        backend.put_chunk(self.job, "item.txt", 0, b"he")
        part = Path(self.job["destination"]) / ("item.txt" + backend.PART_SUFFIX)
        old_time = part.stat().st_mtime_ns
        part.write_bytes(b"ZZ")
        os.utime(part, ns=(old_time + 1000000000, old_time + 1000000000))
        self.assertEqual(backend.part_size(self.job, "item.txt")[1]["size"], 0)
        self.assertEqual(backend.commit_file(self.job, "item.txt", 2, self.mtime)[0], 409)
        self.assertEqual(backend.put_chunk(self.job, "item.txt", 0, b"world")[0], 200)
        self.assertEqual(backend.commit_file(self.job, "item.txt", 5, self.mtime)[0], 200)
        self.assertEqual((Path(self.job["destination"]) / "item.txt").read_bytes(), b"world")

    def test_old_chunk_quarantine_failure_preserves_bytes_and_can_retry(self):
        backend.put_chunk(self.job, "item.txt", 0, b"he")
        backend._rotate_native_generation(self.job)
        part = Path(self.job["destination"]) / ("item.txt" + backend.PART_SUFFIX)
        original = backend.os.replace

        def fail_move(source, target):
            if str(source) == str(part):
                raise OSError("partial archive move failed")
            return original(source, target)

        with patch.object(backend.os, "replace", side_effect=fail_move):
            with self.assertRaisesRegex(OSError, "archive move failed"):
                backend.put_chunk(self.job, "item.txt", 0, b"world")
        self.assertEqual(part.read_bytes(), b"he")
        self.assertEqual(backend.part_size(self.job, "item.txt")[1]["size"], 0)
        self.assertEqual(backend.put_chunk(self.job, "item.txt", 0, b"world")[0], 200)
        self.assertEqual(backend.commit_file(self.job, "item.txt", 5, self.mtime)[0], 200)
        copies = list((Path(self.job["destination"]) / backend.MIRROR_DIRNAME / "partial-recovery").glob("*.pcbp.part"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), b"he")

    def test_scan_reuploads_truncated_file_and_keeps_checkpoint_pending(self):
        dest = Path(self.job["destination"]) / "same.txt"
        dest.write_bytes(b"hello")
        os.utime(dest, ns=(self.mtime * 1000000, self.mtime * 1000000))
        backend.manifest_meta(self.job, "docs")
        backend.manifest_upsert(self.job, [("same.txt", {"s": 5, "m": self.mtime})])
        with dest.open("r+b") as output:
            output.truncate(1)
        plan = self.scan([{"p": "same.txt", "s": 5, "m": self.mtime}])
        self.assertEqual(plan["upload"], [["same.txt", 5]])
        self.assertEqual(plan["skipped"], 0)
        self.assertEqual(plan["repaired"], 1)
        info = self.start(plan)
        code, receipt = backend.finish_run(self.job, {"run_id": info["run_id"]})
        self.assertEqual(code, 200)
        self.assertEqual(receipt["status"], "partial")
        self.assertTrue(Path(backend.resume_path(self.job)).exists())
        self.assertEqual(dest.read_bytes(), b"h")

    def test_scan_reuploads_changed_disk_mtime_but_allows_one_ms_rounding(self):
        backend.manifest_meta(self.job, "docs")
        for name, delta in (("changed.txt", 10000), ("rounding.txt", 1)):
            dest = Path(self.job["destination"]) / name
            dest.write_bytes(b"hello")
            stamp = (self.mtime + delta) * 1000000
            os.utime(dest, ns=(stamp, stamp))
            backend.manifest_upsert(self.job, [(name, {"s": 5, "m": self.mtime})])
        plan = self.scan([{"p": name, "s": 5, "m": self.mtime}
                          for name in ("changed.txt", "rounding.txt")])
        self.assertEqual(plan["upload"], [["changed.txt", 5]])
        self.assertEqual(plan["skipped"], 1)
        self.assertEqual(plan["repaired"], 1)

    def test_resume_does_not_skip_a_completed_file_changed_on_disk(self):
        plan = self.scan([{"p": "same.txt", "s": 5, "m": self.mtime}])
        info = self.start(plan)
        backend.put_chunk(self.job, "same.txt", 0, b"hello")
        backend.commit_file(self.job, "same.txt", 5, self.mtime)
        dest = Path(self.job["destination"]) / "same.txt"
        dest.write_bytes(b"other")
        stamp = (self.mtime + 10000) * 1000000
        os.utime(dest, ns=(stamp, stamp))
        code, resumed = backend.resume_status(self.job)
        self.assertEqual(code, 200)
        self.assertEqual(resumed["files"], [["same.txt", 5, self.mtime]])
        self.assertEqual(backend.finish_run(self.job, {"run_id": info["run_id"]})[1]["status"], "partial")

    def test_commit_ack_retry_cannot_claim_a_changed_destination(self):
        backend.put_chunk(self.job, "same.txt", 0, b"hello")
        self.assertEqual(backend.commit_file(self.job, "same.txt", 5, self.mtime)[0], 200)
        dest = Path(self.job["destination"]) / "same.txt"
        dest.write_bytes(b"other")
        stamp = (self.mtime + 10000) * 1000000
        os.utime(dest, ns=(stamp, stamp))
        self.assertEqual(backend.commit_file(self.job, "same.txt", 5, self.mtime)[0], 400)

    def test_different_jobs_finish_without_losing_shared_history(self):
        jobs = {str(n): dict(self.job, id=str(n), name="job-%d" % n) for n in range(6)}
        backend.save_jobs(jobs)
        barrier = threading.Barrier(6)
        original = backend.load_history
        original_save = backend._save_json
        write_lock = threading.Lock()

        def slow_read():
            records = original()
            # Widen a legal read/modify/write overlap without waiting inside
            # the critical section for another caller (which must serialize).
            time.sleep(.01)
            return records

        def serialized_replace(path, data):
            if path == backend.HISTORY_FILE:
                with write_lock:
                    return original_save(path, data)
            return original_save(path, data)

        def finish(job):
            barrier.wait(timeout=5)
            return [backend.finish_run(job, {}) for _ in range(4)]

        with patch.object(backend, "load_history", side_effect=slow_read), \
                patch.object(backend, "_save_json", side_effect=serialized_replace):
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(finish, jobs.values()))
        self.assertTrue(all(code == 200 for group in results for code, _ in group))
        history = original()
        self.assertEqual(len(history), 24)
        self.assertEqual({key: sum(h["job_id"] == key for h in history) for key in jobs},
                         {key: 4 for key in jobs})

    def test_old_chunk_request_cannot_write_to_edited_destination(self):
        generation = backend._native_context(self.job)["generation"]
        self.assertEqual(backend.update_job(self.job["id"], {"dest_sub": "new"})[0], 200)
        code, _ = self.request("PUT", "data?path=old.txt&offset=0", b"old", generation)
        self.assertEqual(code, 409)
        self.assertFalse((self.root / "pool/new/old.txt.pcbp.part").exists())

    def test_old_commit_cannot_publish_after_source_edit(self):
        generation = backend._native_context(self.job)["generation"]
        backend.put_chunk(self.job, "old.txt", 0, b"old")
        self.assertEqual(backend.update_job(self.job["id"], {"pc_path": r"D:\new"})[0], 200)
        code, _ = self.request("POST", "commit", {"p": "old.txt", "s": 3, "m": self.mtime}, generation)
        self.assertEqual(code, 409)
        self.assertFalse((Path(self.job["destination"]) / "old.txt").exists())
        self.assertEqual((Path(self.job["destination"]) / "old.txt.pcbp.part").read_bytes(), b"old")

    def test_missing_generation_is_rejected_before_creating_data(self):
        code, _ = self.request("PUT", "data?path=unsafe.txt&offset=0", b"old")
        self.assertEqual(code, 409)
        self.assertFalse((Path(self.job["destination"]) / "unsafe.txt.pcbp.part").exists())

    def test_old_native_prepare_and_directory_batch_cannot_follow_an_edit(self):
        generation = backend._native_context(self.job)["generation"]
        self.assertEqual(backend.update_job(self.job["id"], {"dest_sub": "new"})[0], 200)
        code, _ = self.request("POST", "mkdirs", {"paths": ["stale/a.txt"]}, generation)
        self.assertEqual(code, 409)
        code, _ = self.request("POST", "native-upload", {
            "action": "prepare", "p": "stale/a.txt", "s": 3, "m": self.mtime,
            "source": r"D:\docs\stale\a.txt"}, generation)
        self.assertEqual(code, 409)
        self.assertFalse((self.root / "pool/new/stale").exists())

    def test_chunk_http_retry_restart_resume_and_finish(self):
        plan = self.scan([{"p": "same.txt", "s": 5, "m": self.mtime}])
        generation = plan["generation"]
        code, started = self.request("POST", "resume", {
            "scan_id": plan["scan_id"], "source_path": r"D:\docs"}, generation)
        self.assertEqual(code, 200, started)
        run_id = started["data"]["run_id"]
        for _ in range(2):  # response lost after the first accepted chunk
            self.assertEqual(self.request("PUT", "data?path=same.txt&offset=0", b"he", generation)[0], 200)
        for name in ("_MANIFEST_CACHE", "_SCAN_STATE", "_RESUME_ID_CACHE", "_RUN_CURRENT"):
            getattr(backend, name).clear()
        code, resumed = self.request("GET", "resume")
        self.assertEqual(code, 200)
        self.assertEqual(resumed["data"]["run_id"], run_id)
        self.assertEqual(resumed["data"]["plan"]["generation"], generation)
        self.assertEqual(self.request("GET", "part?path=same.txt", generation=generation)[1]["data"]["size"], 2)
        for _ in range(2):
            self.assertEqual(self.request("PUT", "data?path=same.txt&offset=2", b"llo", generation)[0], 200)
        body = {"p": "same.txt", "s": 5, "m": self.mtime}
        for _ in range(2):
            self.assertEqual(self.request("POST", "commit", body, generation)[0], 200)
        stats = {"run_id": run_id, "uploaded": 1, "uploaded_bytes": 5}
        receipt = self.request("POST", "finish", stats, generation)
        self.assertEqual((receipt[0], receipt[1]["data"]["status"]), (200, "ok"))
        self.assertEqual(self.request("POST", "finish", stats, generation), receipt)
        self.assertEqual(len(backend.load_history()), 1)
        self.assertFalse(Path(backend.resume_path(self.job)).exists())
        self.assertEqual((Path(self.job["destination"]) / "same.txt").read_bytes(), b"hello")

    def test_cancel_keeps_chunk_progress_and_legacy_checkpoint_can_resume(self):
        plan = self.scan([{"p": "same.txt", "s": 5, "m": self.mtime}])
        generation = plan["generation"]
        info = self.start(plan)
        self.assertEqual(self.request("PUT", "data?path=same.txt&offset=0", b"he", generation)[0], 200)
        code, cancelled = self.request("POST", "finish", {
            "run_id": info["run_id"], "cancelled": True}, generation)
        self.assertEqual((code, cancelled["data"]["status"]), (200, "cancelled"))
        record = backend.load_resume(self.job)
        record.pop("generation")  # a pre-fix checkpoint has no generation field
        backend._save_json(backend.resume_path(self.job), record)
        backend._RESUME_ID_CACHE.clear()
        backend._MANIFEST_CACHE.clear()
        code, resumed = self.request("GET", "resume")
        self.assertEqual(code, 200)
        self.assertEqual(resumed["data"]["generation"], generation)
        self.assertEqual(resumed["data"]["files"], [["same.txt", 5, self.mtime]])
        self.assertEqual(self.request("GET", "part?path=same.txt", generation=generation)[1]["data"]["size"], 2)
        self.assertEqual(self.request("PUT", "data?path=same.txt&offset=2", b"llo", generation)[0], 200)
        self.assertEqual(self.request("POST", "commit", {
            "p": "same.txt", "s": 5, "m": self.mtime}, generation)[0], 200)
        self.assertEqual(self.request("POST", "finish", {
            "run_id": info["run_id"], "uploaded": 1}, generation)[1]["data"]["status"], "ok")

    def test_rescan_rejects_every_old_plan_operation(self):
        files = [{"p": "same.txt", "s": 5, "m": self.mtime}]
        plan = self.scan(files)
        generation = plan["generation"]
        info = self.start(plan)
        new = self.scan(files, replace_resume=True)
        self.assertNotEqual(new["generation"], generation)
        operations = [
            ("GET", "part?path=same.txt", None),
            ("PUT", "data?path=same.txt&offset=0", b"stale"),
            ("POST", "commit", {"p": "same.txt", "s": 5, "m": self.mtime}),
            ("POST", "deleted", {"paths": ["same.txt"], "mode": "delete"}),
            ("POST", "finish", {"run_id": info["run_id"]}),
            ("POST", "resume", {"scan_id": plan["scan_id"], "source_path": r"D:\docs"}),
        ]
        for method, route, body in operations:
            with self.subTest(route=route):
                code, result = self.request(method, route, body, generation)
                self.assertEqual(code, 409)
                self.assertEqual(result["data"]["reason"], "transfer_stale")
        self.assertFalse((Path(self.job["destination"]) / "same.txt").exists())

    def test_chunk_identity_reads_large_plan_once_then_uses_cached_identity(self):
        generation = backend._native_generation(self.job)
        backend._save_json(backend.resume_path(self.job), {
            "version": 1, "run_id": "large-run",
            "files": [["deep/path/%05d.bin" % n, 5, self.mtime] for n in range(50000)]})
        with patch.object(backend, "load_resume", wraps=backend.load_resume) as load:
            self.assertEqual(self.request("PUT", "data?path=same.txt&offset=0", b"hello", generation)[0], 200)
            self.assertEqual(load.call_count, 1)
        with patch.object(backend, "load_resume", side_effect=AssertionError("must not reparse the full plan")):
            self.assertEqual(self.request("GET", "part?path=same.txt", generation=generation)[0], 200)
            self.assertEqual(self.request("POST", "commit", {
                "p": "same.txt", "s": 5, "m": self.mtime}, generation)[0], 200)

    def restart_probe(self):
        script = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('restarted', sys.argv[1])
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
b.DATA_DIR = sys.argv[2]; b.JOBS_DIR = b.DATA_DIR + '/jobs'
b.JOBS_FILE = b.DATA_DIR + '/jobs.json'; b.HISTORY_FILE = b.DATA_DIR + '/history.json'
j = b.load_jobs()['stable01']
print(json.dumps({'job': j, 'resume': b.resume_status(j),
                  'generation': b._native_generation(j)}))
"""
        return json.loads(subprocess.check_output(
            [sys.executable, "-X", "utf8", "-c", script, str(SOURCE), backend.DATA_DIR], text=True))

    def legacy_checkpoint(self):
        plan = self.scan([{"p": "same.txt", "s": 5, "m": self.mtime}])
        self.start(plan)
        backend.put_chunk(self.job, "same.txt", 0, b"he")
        record = backend.load_resume(self.job)
        record.pop("generation")
        backend._save_json(backend.resume_path(self.job), record)
        return plan["generation"]

    def test_failed_generation_write_never_publishes_new_destination(self):
        generation = self.legacy_checkpoint()
        with patch.object(backend, "_rotate_native_generation", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                backend.update_job(self.job["id"], {"destination": str(self.root / "new")})
        restarted = self.restart_probe()
        self.assertEqual(restarted["job"]["destination"], self.job["destination"])
        self.assertEqual(restarted["generation"], generation)
        self.assertEqual(restarted["resume"][0], 200)

    def test_failed_config_write_keeps_old_config_but_revokes_old_requests(self):
        generation = self.legacy_checkpoint()
        with patch.object(backend, "save_jobs", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                backend.update_job(self.job["id"], {"pc_path": r"D:\other"})
        restarted = self.restart_probe()
        self.assertEqual(restarted["job"]["pc_path"], self.job["pc_path"])
        self.assertNotEqual(restarted["generation"], generation)
        self.assertEqual(restarted["resume"][0], 409)

    def test_legacy_checkpoint_cannot_gain_new_generation_after_source_edit(self):
        generation = self.legacy_checkpoint()
        self.assertEqual(backend.update_job(self.job["id"], {"pc_path": r"D:\other"})[0], 200)
        restarted = self.restart_probe()
        self.assertNotEqual(restarted["generation"], generation)
        self.assertEqual(restarted["resume"][0], 409)
        self.assertEqual((Path(self.job["destination"]) / "same.txt.pcbp.part").read_bytes(), b"he")

    def test_legacy_record_already_mismatching_source_fails_closed(self):
        self.legacy_checkpoint()
        jobs = backend.load_jobs()
        jobs[self.job["id"]]["pc_path"] = r"D:\other"
        backend.save_jobs(jobs)  # simulate a source edit by an older release
        self.assertEqual(self.restart_probe()["resume"][0], 409)

    def test_valid_legacy_checkpoint_is_bound_once_and_preserves_staged_data(self):
        generation = self.legacy_checkpoint()
        restarted = self.restart_probe()
        self.assertEqual(restarted["resume"][0], 200)
        self.assertEqual(backend.load_resume(self.job).get("generation"), generation)
        self.assertEqual(backend.part_size(self.job, "same.txt")[1]["size"], 2)

    def test_legacy_binding_failure_does_not_publish_source_edit(self):
        self.legacy_checkpoint()
        save = backend._save_json

        def fail_binding(path, value):
            if path == backend.resume_path(self.job):
                raise OSError("checkpoint unavailable")
            return save(path, value)

        with patch.object(backend, "_save_json", side_effect=fail_binding):
            with self.assertRaises(OSError):
                backend.update_job(self.job["id"], {"pc_path": r"D:\other"})
        self.assertEqual(self.restart_probe()["job"]["pc_path"], self.job["pc_path"])

    def test_process_exit_between_fence_and_config_publish_revokes_legacy_plan(self):
        generation = self.legacy_checkpoint()
        script = """
import importlib.util, os, sys
s=importlib.util.spec_from_file_location('crash',sys.argv[1])
b=importlib.util.module_from_spec(s); s.loader.exec_module(b)
b.DATA_DIR=sys.argv[2]; b.JOBS_DIR=b.DATA_DIR+'/jobs'; b.JOBS_FILE=b.DATA_DIR+'/jobs.json'
b.save_jobs=lambda jobs: os._exit(23)
b.update_job('stable01', {'pc_path':r'D:\other'})
"""
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", script,
                                 str(SOURCE), backend.DATA_DIR], check=False)
        self.assertEqual(result.returncode, 23)
        restarted = self.restart_probe()
        self.assertEqual(restarted["job"]["pc_path"], self.job["pc_path"])
        self.assertNotEqual(restarted["generation"], generation)
        self.assertEqual(restarted["resume"][0], 409)


if __name__ == "__main__":
    unittest.main()
