"""Bounded finish requests, admission retries and durable restart receipts."""
import importlib.util
import json
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SOURCE = Path(__file__).resolve().parents[1] / "xiaomi-nas-plugin-pcbackup/src/files/httpd.py"
spec = importlib.util.spec_from_file_location("pcbackup_async_backend", SOURCE)
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class FinishAsyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="pcbackup-async-")
        self.addCleanup(temp.cleanup)
        backend.DATA_DIR = temp.name
        backend.JOBS_DIR = temp.name + "/jobs"
        backend.JOBS_FILE = temp.name + "/jobs.json"
        backend.HISTORY_FILE = temp.name + "/history.json"
        for name in ("_MANIFEST_CACHE", "_MANIFEST_PATH_CACHE", "_RUN_CURRENT", "_RESUME_ID_CACHE", "_RESUME_CHECKS"):
            getattr(backend, name).clear()
        self.job = {"id": "async001", "name": "test", "destination": temp.name + "/backup",
                    "exclude": [], "pc_path": r"D:\docs"}
        backend.save_jobs({self.job["id"]: self.job})
        self.stats = {"request_id": uuid.uuid4().hex,
                      "generation": backend._native_generation(self.job), "uploaded": 50000}
        self.addCleanup(self.drain_worker)

    def drain_worker(self):
        deadline = time.monotonic() + 6
        while backend._finish_path(self.job) in backend._FINISH_ACTIVE and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertNotIn(backend._finish_path(self.job), backend._FINISH_ACTIVE)

    def request(self, method, route, body=None):
        if not getattr(self, "server", None):
            class QuietHandler(backend.Handler):
                def log_message(self, *args):
                    pass
            self.server = backend.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            self.addCleanup(self.server.server_close)
            self.addCleanup(self.server.shutdown)
        endpoint = route if route.startswith("/") else "/api/jobs/%s/%s" % (self.job["id"], route)
        url = "http://127.0.0.1:%d%s" % (self.server.server_port, endpoint)
        data = json.dumps(body).encode() if body is not None else None
        req = Request(url, method=method, data=data, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=2) as response:
                return response.status, json.load(response)["data"]
        except HTTPError as error:
            return error.code, json.load(error)

    def checkpoint(self, files=()):
        backend.manifest_meta(self.job, "docs")
        record = {"version": 1, "run_id": uuid.uuid4().hex, "generation": self.stats["generation"],
                  "destination": self.job["destination"], "exclude": [], "folder": "docs",
                  "source_path": self.job["pc_path"], "started": 1, "files": list(files),
                  "total_files": len(files), "deleted": [], "skipped": 0, "repaired": 0,
                  "adopted": 0, "excluded_kept": 3, "del_mode": "del", "prune_excluded": True}
        backend._save_json(backend.resume_path(self.job), record)
        self.stats["run_id"] = record["run_id"]
        return record

    def wait_resume(self, request_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = backend.resume_check(self.job, request_id)
            if result[0] != 200 or result[1]["phase"] == "done":
                return result
            time.sleep(.001)
        self.fail("resume check did not settle")

    def wait(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = backend.finish_async_status(self.job, self.stats["request_id"])
            if result[0] != 200 or result[1]["phase"] == "done":
                return result
            time.sleep(.001)
        self.fail("finish did not settle")

    def test_slow_finish_admission_status_and_duplicate_retry_do_not_wait_for_job_lock(self):
        entered, release = threading.Event(), threading.Event()
        original = backend.finish_run

        def slow(job, stats, **kwargs):
            with backend._job_lock(job["id"]):
                entered.set()
                self.assertTrue(release.wait(5))
                return original(job, stats, **kwargs)

        with patch.object(backend, "finish_run", side_effect=slow) as finish:
            self.assertEqual(backend.finish_async(self.job, self.stats)[0], 200)
            self.assertTrue(entered.wait(5))
            try:
                started = time.monotonic()
                for _ in range(3):
                    self.assertEqual(backend.finish_async(self.job, self.stats)[1]["phase"], "running")
                    self.assertEqual(backend.finish_async_status(self.job, self.stats["request_id"])[1]["phase"], "running")
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(finish.call_count, 1)
                summary = backend.job_summary(self.job)
                self.assertEqual(summary["finish_pending"], self.stats["request_id"])
            finally:
                release.set()
            receipt = self.wait()
        self.assertEqual(receipt[1]["result"]["status"], "ok")
        self.assertEqual(backend.finish_async(self.job, self.stats), receipt)
        self.assertEqual(len(backend.load_history()), 1)

    def test_history_failure_is_retryable_without_duplicate_history(self):
        with patch.object(backend, "append_history", side_effect=OSError("disk full")):
            backend.finish_async(self.job, self.stats)
            self.assertEqual(self.wait()[0], 500)
        backend.finish_async(self.job, self.stats)
        self.assertEqual(self.wait()[1]["result"]["status"], "ok")
        self.assertEqual(len(backend.load_history()), 1)

    def test_history_saved_before_config_failure_is_reconciled_once_on_retry(self):
        with patch.object(backend, "save_jobs", side_effect=OSError("jobs unavailable")):
            backend.finish_async(self.job, self.stats)
            self.assertEqual(self.wait()[0], 500)
        self.assertEqual(len(backend.load_history()), 1)
        backend.finish_async(self.job, self.stats)
        receipt = self.wait()[1]["result"]
        self.assertEqual(receipt["status"], "ok")
        self.assertEqual(backend.load_jobs()[self.job["id"]]["last_stats"], receipt)
        self.assertEqual(len(backend.load_history()), 1)

    def test_receipt_write_failure_is_visible_and_retry_recovers_existing_history(self):
        original = backend._save_json

        def fail_receipt(path, value):
            if path == backend._finish_path(self.job) and value.get("phase") != "running":
                raise OSError("receipt disk full")
            return original(path, value)

        with patch.object(backend, "_save_json", side_effect=fail_receipt):
            backend.finish_async(self.job, self.stats)
            # Observe the first worker's terminal state, without starting any
            # recovery worker by polling an unfinished disk intent.
            deadline = time.monotonic() + 5
            while backend._finish_path(self.job) in backend._FINISH_ACTIVE and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertEqual(backend.finish_async_status(self.job, self.stats["request_id"])[0], 500)
            self.assertEqual(backend.finish_async(self.job, dict(self.stats, request_id=uuid.uuid4().hex))[0], 409)
            self.assertEqual(backend.finish_async_status(self.job, self.stats["request_id"])[0], 500)
        backend.finish_async(self.job, self.stats)
        self.assertEqual(self.wait()[1]["result"]["status"], "ok")
        self.assertEqual(len(backend.load_history()), 1)

    def test_new_process_recovers_persisted_intent_and_lost_result_ack(self):
        # Stop at admission, representing process exit before the worker runs.
        with patch.object(backend.threading.Thread, "start"):
            backend.finish_async(self.job, self.stats)
        backend._FINISH_ACTIVE.pop(backend._finish_path(self.job), None)
        script = """
import importlib.util, json, sys, time
s = importlib.util.spec_from_file_location('restarted', sys.argv[1])
b = importlib.util.module_from_spec(s); s.loader.exec_module(b)
b.DATA_DIR=sys.argv[2]; b.JOBS_DIR=b.DATA_DIR+'/jobs'; b.JOBS_FILE=b.DATA_DIR+'/jobs.json'
b.HISTORY_FILE=b.DATA_DIR+'/history.json'; j=b.load_jobs()['async001']
for n in range(5000):
    code, out = b.finish_async_status(j, sys.argv[3])
    if code != 200 or out['phase'] == 'done':
        print(json.dumps([code, out])); break
    time.sleep(.001)
else: raise RuntimeError('unfinished')
"""
        args = [sys.executable, "-X", "utf8", "-c", script, str(SOURCE),
                backend.DATA_DIR, self.stats["request_id"]]
        first = json.loads(subprocess.check_output(args, text=True))
        second = json.loads(subprocess.check_output(args, text=True))
        self.assertEqual(first, second)
        self.assertEqual(first[1]["result"]["status"], "ok")
        self.assertEqual(len(backend.load_history()), 1)

    def test_same_request_id_cannot_change_stats_and_concurrent_intents_are_rejected(self):
        with patch.object(backend.threading.Thread, "start"):
            backend.finish_async(self.job, self.stats)
            self.assertEqual(backend.finish_async(self.job, dict(self.stats, uploaded=1))[0], 409)
            self.assertEqual(backend.finish_async(self.job, dict(self.stats, request_id=uuid.uuid4().hex))[0], 409)
        backend._FINISH_ACTIVE.pop(backend._finish_path(self.job), None)

    def test_resume_verification_is_polled_without_waiting_for_its_job_lock(self):
        entered, release = threading.Event(), threading.Event()

        def blocked(job):
            with backend._job_lock(job["id"]):
                entered.set()
                self.assertTrue(release.wait(5))
                return 200, {"run_id": "saved", "files": [], "plan": {"previous_uploaded": 50000}}

        with patch.object(backend, "resume_status", side_effect=blocked) as check:
            request_id = uuid.uuid4().hex
            self.assertEqual(backend.resume_check(self.job, request_id)[1]["phase"], "running")
            self.assertTrue(entered.wait(5))
            try:
                started = time.monotonic()
                self.assertEqual(backend.resume_check(self.job, request_id)[1]["phase"], "running")
                self.assertLess(time.monotonic() - started, 1)
            finally:
                release.set()
            for _ in range(5000):
                result = backend.resume_check(self.job, request_id)
                if result[1]["phase"] == "done":
                    break
                time.sleep(.001)
            self.assertEqual(result[1]["result"]["plan"]["previous_uploaded"], 50000)
            self.assertEqual(check.call_count, 1)

    def test_reopened_page_retries_failed_receipt_using_only_persisted_request_id(self):
        original = backend._save_json

        def fail_receipt(path, value):
            if path == backend._finish_path(self.job) and value.get("phase") == "done":
                raise OSError("receipt disk full")
            return original(path, value)

        with patch.object(backend, "_save_json", side_effect=fail_receipt):
            backend.finish_async(self.job, self.stats)
            self.assertEqual(self.wait()[0], 500)
        request_id = backend.job_summary(self.job)["finish_pending"]
        self.assertEqual(request_id, self.stats["request_id"])
        self.assertEqual(self.request("POST", "finish-retry", {"request_id": uuid.uuid4().hex})[0], 404)
        self.assertEqual(self.request("POST", "finish-retry", {"request_id": request_id})[0], 200)
        self.assertEqual(self.wait()[1]["result"]["status"], "ok")
        self.assertEqual(len(backend.load_history()), 1)
        self.assertEqual(backend.job_summary(self.job)["finish_pending"], "")
        self.assertEqual(self.request("POST", "finish-retry", {"request_id": request_id})[1]["phase"], "done")
        self.stats = dict(self.stats, request_id=uuid.uuid4().hex)
        backend.finish_async(self.job, self.stats)
        self.assertEqual(self.wait()[1]["result"]["status"], "ok")
        self.assertEqual(len(backend.load_history()), 2)

    def test_reopened_finish_retry_keeps_original_generation_fence(self):
        self.checkpoint([["partial.txt", 5, 1700000000000]])
        backend.put_chunk(self.job, "partial.txt", 0, b"he")
        with patch.object(backend.threading.Thread, "start"):
            backend.finish_async(self.job, self.stats)
        backend._FINISH_ACTIVE.pop(backend._finish_path(self.job), None)
        self.assertEqual(backend.update_job(self.job["id"], {"pc_path": r"D:\new-source"})[0], 200)
        current = backend.load_jobs()[self.job["id"]]
        self.assertEqual(backend.retry_finish(current, self.stats["request_id"])[0], 200)
        result = self.wait()
        self.assertEqual(result[0], 409)
        self.assertEqual(result[1]["reason"], "transfer_stale")
        self.assertTrue(Path(backend.resume_path(self.job)).exists())
        self.assertEqual(backend.part_size(current, "partial.txt")[1]["size"], 0)
        self.assertEqual((Path(current["destination"]) / ("partial.txt" + backend.PART_SUFFIX)).read_bytes(), b"he")

    def test_transient_resume_failure_retries_same_identity_and_keeps_partial_chunk(self):
        self.checkpoint([["partial.txt", 5, 1700000000000]])
        backend.put_chunk(self.job, "partial.txt", 0, b"he")
        request_id = uuid.uuid4().hex
        with patch.object(backend, "resume_status", side_effect=OSError("transient read")):
            backend.resume_check(self.job, request_id)
            self.assertEqual(self.wait_resume(request_id)[0], 500)
        with patch.object(backend, "resume_status", wraps=backend.resume_status) as actual:
            code, out = self.wait_resume(request_id)
            self.assertEqual(code, 200, out)
            self.assertEqual(out["result"]["files"], [["partial.txt", 5, 1700000000000]])
            self.assertEqual(actual.call_count, 1)
        self.assertEqual(backend.part_size(self.job, "partial.txt")[1]["size"], 2)

    def test_real_checkpoint_open_read_and_decode_failures_retry_without_losing_progress(self):
        record = self.checkpoint([["partial.txt", 5, 1700000000000]])
        backend.put_chunk(self.job, "partial.txt", 0, b"he")
        path = backend.resume_path(self.job)
        saved = Path(path).read_bytes()
        original = open

        class FailedRead:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def read(self):
                raise OSError("injected real file read failure")

        for mode in ("open", "read", "decode"):
            with self.subTest(mode=mode):
                failures = []

                def fail_once(filename, *args, **kwargs):
                    if str(filename) == path and not failures:
                        failures.append(mode)
                        if mode == "open":
                            raise OSError("injected real file open failure")
                        if mode == "read":
                            return FailedRead(original(filename, *args, **kwargs))
                        return io.StringIO("{invalid json")
                    return original(filename, *args, **kwargs)

                request_id = uuid.uuid4().hex
                with patch.object(backend, "open", side_effect=fail_once, create=True):
                    backend.resume_check(self.job, request_id)
                    failed = self.wait_resume(request_id)
                    self.assertEqual(failed[0], 500, failed)
                code, out = self.wait_resume(request_id)
                self.assertEqual(code, 200, out)
                self.assertEqual(out["result"]["run_id"], record["run_id"])
                self.assertEqual(out["result"]["files"], [["partial.txt", 5, 1700000000000]])
                self.assertEqual(failures, [mode])
                self.assertEqual(Path(path).read_bytes(), saved)
                self.assertEqual(backend.part_size(self.job, "partial.txt")[1]["size"], 2)

    def test_only_missing_checkpoint_is_reported_as_no_resume(self):
        request_id = uuid.uuid4().hex
        self.assertEqual(self.wait_resume(request_id)[1]["result"], None)
        for invalid in ("[]", '{"version": 2}'):
            with self.subTest(invalid=invalid):
                path = Path(backend.resume_path(self.job))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(invalid, encoding="utf-8")
                code, out = self.wait_resume(uuid.uuid4().hex)
                self.assertEqual(code, 500, out)
                self.assertEqual(path.read_text(encoding="utf-8"), invalid)

    def test_receipt_recovery_survives_rolling_history_and_a_fresh_process(self):
        record = self.checkpoint([["complete.txt", 5, 1700000000000]])
        backend.put_chunk(self.job, "complete.txt", 0, b"hello")
        self.assertEqual(backend.commit_file(self.job, "complete.txt", 5, 1700000000000)[0], 200)
        original = backend._save_json

        def fail_receipt(path, value):
            if path == backend._finish_path(self.job) and value.get("phase") == "done":
                raise OSError("receipt write failed")
            return original(path, value)

        with patch.object(backend, "_save_json", side_effect=fail_receipt):
            backend.finish_async(self.job, self.stats)
            self.assertEqual(self.wait()[0], 500)
        receipt = backend.load_jobs()[self.job["id"]]["last_stats"]
        self.assertEqual(receipt["status"], "ok")
        self.assertFalse(Path(backend.resume_path(self.job)).exists())
        for n in range(backend.HISTORY_KEEP + 5):
            backend.append_history({"job_id": "other", "run_id": "other-%d" % n, "status": "ok"})
        self.assertFalse(any(h.get("run_id") == record["run_id"] for h in backend.load_history()))
        script = """
import importlib.util, json, sys, time
from unittest.mock import patch
s=importlib.util.spec_from_file_location('restarted',sys.argv[1])
b=importlib.util.module_from_spec(s); s.loader.exec_module(b)
b.DATA_DIR=sys.argv[2]; b.JOBS_DIR=b.DATA_DIR+'/jobs'; b.JOBS_FILE=b.DATA_DIR+'/jobs.json'
b.HISTORY_FILE=b.DATA_DIR+'/history.json'; j=b.load_jobs()['async001']
with patch.object(b,'resume_status',side_effect=AssertionError('must recover without revalidating files')):
    assert b.retry_finish(j,sys.argv[3])[0]==200
    for _ in range(5000):
        code,out=b.finish_async_status(j,sys.argv[3])
        if code!=200: raise RuntimeError(out)
        if out['phase']=='done': break
        time.sleep(.001)
    else: raise RuntimeError('recovery did not settle')
print(json.dumps(out['result']))
"""
        recovered = json.loads(subprocess.check_output(
            [sys.executable, "-X", "utf8", "-c", script, str(SOURCE), backend.DATA_DIR,
             self.stats["request_id"]], text=True, timeout=15))
        self.assertEqual(recovered, receipt)
        self.assertEqual(len(backend.load_history()), backend.HISTORY_KEEP)
        self.assertFalse(any(h.get("run_id") == record["run_id"] for h in backend.load_history()))
        self.assertEqual(backend._load_json(backend._finish_path(self.job), {})["phase"], "done")

    def test_last_stats_recovery_requires_matching_job_and_operation_identity(self):
        self.checkpoint([["pending.txt", 5, 1700000000000]])
        Path(backend.resume_path(self.job)).unlink()
        good = {"job_id": self.job["id"], "job_name": "test", "status": "ok", "ts": 1,
                "run_id": self.stats["run_id"], "request_id": self.stats["request_id"]}
        for change in ({"job_id": "other"}, {"run_id": "other-run"}, {"status": "invalid"},
                       {"status": "partial", "request_id": "other-request"}):
            with self.subTest(change=change):
                backend.save_jobs({self.job["id"]: dict(self.job, last_stats=dict(good, **change))})
                self.assertEqual(backend.finish_run(self.job, self.stats)[0], 409)
        backend.save_jobs({self.job["id"]: dict(self.job, last_stats=good)})
        self.assertEqual(backend.finish_run(self.job, self.stats), (200, good))

    def test_cancelled_and_partial_receipts_survive_history_rollover_without_dropping_checkpoint(self):
        for status in ("cancelled", "partial"):
            with self.subTest(status=status):
                self.checkpoint([["pending.txt", 5, 1700000000000]])
                self.stats.update(request_id=uuid.uuid4().hex, cancelled=status == "cancelled")
                code, canonical = backend.finish_run(self.job, self.stats)
                self.assertEqual((code, canonical["status"]), (200, status))
                saved = Path(backend.resume_path(self.job)).read_bytes()
                for n in range(backend.HISTORY_KEEP + 1):
                    backend.append_history({"job_id": "other", "status": "ok", "ts": n})
                with patch.object(backend, "resume_status", side_effect=AssertionError("must reuse receipt")):
                    self.assertEqual(backend.finish_run(self.job, self.stats), (200, canonical))
                self.assertEqual(Path(backend.resume_path(self.job)).read_bytes(), saved)
                self.assertEqual(len(backend.load_history()), backend.HISTORY_KEEP)

    def test_resume_physical_check_does_not_block_http_job_list(self):
        self.checkpoint([["done.txt", 5, 1700000000000]])
        backend.put_chunk(self.job, "done.txt", 0, b"hello")
        backend.commit_file(self.job, "done.txt", 5, 1700000000000)
        entered, release = threading.Event(), threading.Event()
        original = backend._file_matches

        def gated(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test gate expired")
            return original(*args)

        request_id = uuid.uuid4().hex
        with patch.object(backend, "_file_matches", side_effect=gated):
            self.request("GET", "resume?async=1&request_id=" + request_id)
            self.assertTrue(entered.wait(3))
            try:
                try:
                    code, jobs = self.request("GET", "/api/jobs")
                except (TimeoutError, OSError):
                    self.fail("job list waited for the physical resume verifier")
                self.assertEqual(code, 200)
                self.assertEqual(jobs[0]["finish_pending"], "")
                self.assertEqual(jobs[0]["resume_check_pending"], request_id)
                self.assertTrue(jobs[0]["summary_pending"])
                self.assertTrue(jobs[0]["resume_available"])
            finally:
                release.set()
                result = self.wait_resume(request_id)
        self.assertEqual(result[1]["result"]["plan"]["previous_uploaded"], 1)
        summary = backend.job_summary(self.job)
        self.assertFalse(summary["summary_pending"])
        self.assertEqual(summary["files"], 1)

    def test_job_summary_uses_provisional_counts_when_lock_becomes_busy(self):
        self.checkpoint()
        entered, release = threading.Event(), threading.Event()

        def hold_job_lock():
            with backend._job_lock(self.job["id"]):
                entered.set()
                release.wait(5)

        worker = threading.Thread(target=hold_job_lock)
        worker.start()
        self.assertTrue(entered.wait(3))
        try:
            try:
                code, jobs = self.request("GET", "/api/jobs")
            except (TimeoutError, OSError):
                self.fail("job list blocked without an active coordinator record")
            self.assertEqual(code, 200)
            self.assertTrue(jobs[0]["summary_pending"])
            self.assertEqual(jobs[0]["finish_pending"], "")
            self.assertEqual(jobs[0]["resume_check_pending"], "")
        finally:
            release.set()
            worker.join(3)

    def test_resume_thread_start_failure_does_not_leave_a_running_placeholder(self):
        request_id = uuid.uuid4().hex
        with patch.object(backend.threading.Thread, "start", side_effect=RuntimeError("cannot start new thread")):
            try:
                result = backend.resume_check(self.job, request_id)
            except RuntimeError:
                result = (500, "thread failed")
        self.assertEqual(result[0], 500)
        self.assertNotIn(self.job["id"], backend._RESUME_CHECKS)
        self.assertEqual(self.wait_resume(request_id)[1]["phase"], "done")

    def test_cancel_during_prune_stops_later_deletes_and_keeps_checkpoint_for_resume(self):
        self.checkpoint()
        self.stats["prune_excluded"] = True
        names = ["excluded/%d.tmp" % n for n in range(3)]
        paths = [Path(self.job["destination"]) / name for name in names]
        paths[0].parent.mkdir(parents=True)
        for path in paths:
            path.write_bytes(b"keep until cleaned")
        backend.manifest_exclude(self.job, names)
        entered, release = threading.Event(), threading.Event()
        original = backend.os.remove

        def gated_remove(path):
            original(path)
            if str(path) == str(paths[0]):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test gate expired")

        with patch.object(backend.os, "remove", side_effect=gated_remove):
            backend.finish_async(self.job, self.stats)
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(backend.load_history(), [])
                self.assertEqual(backend.finish_async_status(self.job, self.stats["request_id"], cancel=True)[0], 200)
            finally:
                release.set()
            receipt = self.wait()[1]["result"]
        self.assertEqual(receipt["status"], "cancelled")
        self.assertEqual(receipt["pruned_excluded"], 1)
        self.assertTrue(all(path.exists() for path in paths[1:]))
        self.assertTrue(Path(backend.resume_path(self.job)).exists())
        backend._MANIFEST_CACHE.pop(self.job["id"], None)
        self.assertTrue(set(names[1:]).issubset(backend.load_manifest(self.job)["excluded"]))
        self.stats = dict(self.stats, request_id=uuid.uuid4().hex)
        backend.finish_async(self.job, self.stats)
        self.assertEqual(self.wait()[1]["result"]["status"], "ok")
        self.assertFalse(any(path.exists() for path in paths))
        self.assertFalse(Path(backend.resume_path(self.job)).exists())

    def test_cancel_after_prune_before_result_commit_is_honored(self):
        self.checkpoint()
        self.stats["prune_excluded"] = True
        entered, release = threading.Event(), threading.Event()

        def gated(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test gate expired")
            return 200, {"removed": 0}

        with patch.object(backend, "prune_excluded", side_effect=gated):
            backend.finish_async(self.job, self.stats)
            self.assertTrue(entered.wait(5))
            try:
                backend.finish_async_status(self.job, self.stats["request_id"], cancel=True)
            finally:
                release.set()
            self.assertEqual(self.wait()[1]["result"]["status"], "cancelled")
        self.assertTrue(Path(backend.resume_path(self.job)).exists())

    def test_cancel_after_result_commit_starts_is_declined_and_does_not_rewrite_intent(self):
        self.checkpoint()
        entered, release = threading.Event(), threading.Event()
        original = backend.append_history

        def gated(entry):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test gate expired")
            return original(entry)

        with patch.object(backend, "append_history", side_effect=gated):
            backend.finish_async(self.job, self.stats)
            self.assertTrue(entered.wait(5))
            try:
                code, out = backend.finish_async_status(self.job, self.stats["request_id"], cancel=True)
                self.assertEqual(code, 200)
                self.assertFalse(out.get("cancel_accepted", True))
                self.assertFalse(backend._load_json(backend._finish_path(self.job), {}).get("cancelled", False))
            finally:
                release.set()
            self.assertEqual(self.wait()[1]["result"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
