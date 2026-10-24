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
spec = importlib.util.spec_from_file_location("pcbackup_transfer_backend", SOURCE)
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pcbackup-transfer-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        backend.DATA_DIR = str(self.root / "etc")
        backend.JOBS_DIR = str(self.root / "etc/jobs")
        backend.JOBS_FILE = str(self.root / "etc/jobs.json")
        backend.HISTORY_FILE = str(self.root / "etc/history.json")
        backend.USER_DATA_ROOT = str(self.root / "pool")
        backend._MANIFEST_CACHE.clear()
        backend._MANIFEST_PATH_CACHE.clear()
        backend._RUN_CURRENT.clear()
        backend._SCAN_STATE.clear()
        backend._RESUME_ID_CACHE.clear()
        self.job = {"id": "1234abcd", "name": "test", "destination": str(self.root / "pool/backup")}
        Path(self.job["destination"]).mkdir(parents=True)
        backend.save_jobs({self.job["id"]: self.job})
        self.body = {"p": "new/sub/file.txt", "s": 5, "m": 1700000000000,
                     "source": r"D:\documents\new\sub\file.txt"}

    def prepare(self, body=None):
        code, data = backend.prepare_native_upload(self.job, body or self.body)
        self.assertEqual(code, 200, data)
        return data

    def stage(self, info, data=b"hello", ready=False):
        name = info["ready_name" if ready else "temp_name"]
        p = Path(backend.USER_DATA_ROOT) / ".temp_upload_dir" / name
        p.write_bytes(data)
        return p

    def complete(self, info):
        return backend.complete_native_upload(self.job, {"p": self.body["p"], "token": info["token"]})

    def finalize(self, info, body=None):
        return backend.finalize_native_upload(self.job, {
            "p": (body or self.body)["p"], "token": info["token"]})

    def wait_for_lifecycle_writer(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if backend._lifecycle(self.job["id"]).waiting:
                return
            time.sleep(.001)
        self.fail("lifecycle transition never waited for the active file operation")

    def test_finalize_publishes_with_one_data_sync_and_preserves_mtime(self):
        info = self.prepare()
        self.stage(info, ready=True)
        original = backend.os.fsync
        with patch.object(backend.os, "fsync", wraps=original) as sync:
            self.assertEqual(self.finalize(info)[0], 200)
            self.assertEqual(sync.call_count, 1)
            self.assertEqual(self.finalize(info)[0], 200, "lost acknowledgement must be idempotent")
            self.assertEqual(sync.call_count, 1)
        dest = Path(self.job["destination"]) / self.body["p"]
        self.assertEqual(dest.read_bytes(), b"hello")
        self.assertAlmostEqual(dest.stat().st_mtime * 1000, self.body["m"], delta=1)
        self.assertFalse(Path(str(dest) + backend.PART_SUFFIX).exists())

    def test_finalize_recovers_after_publish_but_before_manifest_append(self):
        info = self.prepare()
        self.stage(info)
        with patch.object(backend, "_run_append", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.finalize(info)
        self.assertNotIn(self.body["p"], backend.load_manifest(self.job)["files"])
        backend._MANIFEST_CACHE.clear()  # model a backend restart
        recovered = self.prepare()
        self.assertEqual(recovered["token"], info["token"])
        self.assertTrue(recovered["uploaded"])
        self.assertEqual(self.finalize(recovered)[0], 200)
        self.assertIn(self.body["p"], backend.load_manifest(self.job)["files"])

    def test_different_native_files_can_sync_in_parallel(self):
        bodies = [dict(self.body, p="parallel/%d.txt" % n) for n in range(6)]
        infos = [self.prepare(b) for b in bodies]
        for info in infos:
            self.stage(info)
        barrier = threading.Barrier(6)
        original = backend.os.fsync

        def concurrent_sync(fd):
            barrier.wait(timeout=5)
            original(fd)

        with patch.object(backend.os, "fsync", side_effect=concurrent_sync):
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda pair: self.finalize(*pair), zip(infos, bodies)))
        self.assertTrue(all(code == 200 for code, _ in results))

    def test_same_file_duplicate_finalize_appends_once(self):
        info = self.prepare()
        self.stage(info)
        with patch.object(backend, "_run_append", wraps=backend._run_append) as append:
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: self.finalize(info), range(6)))
            self.assertEqual(append.call_count, 1)
        self.assertTrue(all(code == 200 for code, _ in results))

    def test_data_sync_failure_keeps_old_destination_and_checkpoint_pending(self):
        info = self.prepare()
        stage = self.stage(info)
        dest = Path(self.job["destination"]) / self.body["p"]
        dest.write_bytes(b"older")
        with patch.object(backend.os, "fsync", side_effect=OSError("I/O error")):
            with self.assertRaises(OSError):
                self.finalize(info)
        self.assertEqual(dest.read_bytes(), b"older")
        self.assertTrue(stage.exists())
        self.assertNotIn(self.body["p"], backend.load_manifest(self.job)["files"])
        self.assertEqual(self.finalize(info)[0], 200)

    def test_restart_after_intent_before_rename_does_not_adopt_old_same_size_file(self):
        info = self.prepare()
        self.stage(info, ready=True)
        dest = Path(self.job["destination"]) / self.body["p"]
        dest.write_bytes(b"older")
        original = backend.os.replace

        def fail_publish(src, target):
            if os.fspath(target) == str(dest):
                raise OSError("rename failed")
            return original(src, target)

        with patch.object(backend.os, "replace", side_effect=fail_publish):
            with self.assertRaises(OSError):
                self.finalize(info)
        backend._MANIFEST_CACHE.clear()
        self.assertEqual(dest.read_bytes(), b"older")
        self.assertEqual(self.finalize(self.prepare())[0], 200)
        self.assertEqual(dest.read_bytes(), b"hello")

    def test_partial_and_superseded_tokens_cannot_publish(self):
        first = self.prepare()
        self.stage(first, b"he")
        self.assertEqual(self.finalize(first)[0], 409)
        second = self.prepare()
        self.assertNotEqual(first["token"], second["token"])
        self.stage(first, b"hello")  # late terminal callback from the cancelled attempt
        self.assertEqual(self.finalize(first)[0], 409)
        self.stage(second, b"newer")
        self.assertEqual(self.finalize(second)[0], 200)
        self.assertEqual((Path(self.job["destination"]) / self.body["p"]).read_bytes(), b"newer")

    def test_generation_and_run_identity_reject_stale_attempts_after_restart(self):
        info = self.prepare()
        self.stage(info, ready=True)
        backend._rotate_native_generation(self.job)
        self.assertEqual(self.finalize(info)[0], 409)
        current = self.prepare()
        self.assertNotEqual(current["token"], info["token"])
        backend._save_json(backend.resume_path(self.job), {"version": 1, "run_id": "next-run"})
        self.assertEqual(self.finalize(current)[0], 409)
        code, _ = backend.prepare_native_upload(self.job, dict(self.body, run_id="old-run"))
        self.assertEqual(code, 409)

    def test_upgrade_recovers_legacy_promoted_part_and_committed_receipt(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                info = self.prepare(dict(self.body, m=self.body["m"] + int(committed)))
                self.stage(info)
                self.assertEqual(self.complete(info)[0], 200)
                if committed:
                    self.assertEqual(backend.commit_file(self.job, self.body["p"], 5, self.body["m"] + 1)[0], 200)
                path = backend._native_record_path(self.job, self.body["p"])
                record = backend._load_json(path, {})
                record["version"] = 1
                record.pop("generation", None)
                record.pop("run_id", None)
                backend._save_json(path, record)
                recovered = self.prepare(dict(self.body, m=self.body["m"] + int(committed)))
                self.assertEqual(recovered["token"], info["token"])
                self.assertTrue(recovered["uploaded"])
                self.assertEqual(self.finalize(recovered)[0], 200)

    def save_checkpoint(self):
        backend.manifest_meta(self.job, "docs")
        backend._save_json(backend.resume_path(self.job), {
            "version": 1, "run_id": "saved-run", "destination": self.job["destination"],
            "exclude": [], "folder": "docs", "source_path": r"D:\documents", "started": 1,
            "total_files": 2, "files": [[self.body["p"], 5, self.body["m"]], ["later.txt", 5, self.body["m"]]],
            "deleted": [], "skipped": 0, "repaired": 0, "adopted": 0, "excluded_kept": 0,
            "del_mode": "del", "prune_excluded": False})

    def save_large_checkpoint(self):
        self.save_checkpoint()
        record = backend.load_resume(self.job)
        record["files"] = [[self.body["p"], 5, self.body["m"]]] + [
            ["performance/" + "nested-directory/" * 8 + "%06d-sample-document.txt" % n,
             5, self.body["m"]] for n in range(49999)]
        record["total_files"] = 50000
        backend._save_json(backend.resume_path(self.job), record)
        return record

    def test_50000_file_checkpoint_is_not_parsed_per_native_context(self):
        self.save_large_checkpoint()
        self.assertGreater(Path(backend.resume_path(self.job)).stat().st_size, 8 * 1024 * 1024)
        with patch.object(backend, "load_resume", wraps=backend.load_resume) as load:
            with ThreadPoolExecutor(max_workers=6) as pool:
                contexts = list(pool.map(lambda _: backend._native_context(self.job), range(6)))
            self.assertTrue(all(c["run_id"] == "saved-run" for c in contexts))
            self.assertLessEqual(load.call_count, 1, "six files must share one cold checkpoint read")
            load.reset_mock()
            for _ in range(100):
                self.assertEqual(backend._native_context(self.job), contexts[0])
            self.assertEqual(load.call_count, 0, "warm file operations must never parse the full plan")

    def test_50000_file_plan_six_workers_publish_and_retry_without_reparsing(self):
        record = self.save_large_checkpoint()
        bodies = [dict(self.body, p=p, s=s, m=m) for p, s, m in record["files"][:36]]

        def upload(body):
            info = self.prepare(body)
            self.stage(info, ready=True)
            first = self.finalize(info, body)
            retry = self.finalize(info, body)  # acknowledgement lost
            self.assertEqual(first[0], 200)
            self.assertEqual(retry, first)
            return info

        with patch.object(backend, "load_resume", wraps=backend.load_resume) as load:
            with ThreadPoolExecutor(max_workers=6) as pool:
                infos = list(pool.map(upload, bodies))
            self.assertEqual(len(infos), 36)
            self.assertEqual(load.call_count, 1, "all prepare/finalize/retry requests share one cold read")
        self.assertEqual(len(backend.load_manifest(self.job)["files"]), 36)
        # Cache contains only a stamp and scalar run ID, never the 50,000 entries.
        cached = backend._RESUME_ID_CACHE[self.job["id"]]
        self.assertEqual(cached[1], "saved-run")
        self.assertLess(len(repr(cached)), 1024)

    def test_50000_file_plan_cancel_and_restart_keep_progress_and_staged_data(self):
        record = self.save_large_checkpoint()
        first = self.prepare()
        self.stage(first)
        self.assertEqual(self.finalize(first)[0], 200)
        ready_body = dict(self.body, p=record["files"][1][0])
        ready = self.prepare(ready_body)
        self.stage(ready, ready=True)
        partial_body = dict(self.body, p=record["files"][2][0])
        partial = self.prepare(partial_body)
        self.stage(partial, b"he")
        self.assertEqual(backend.finish_run(self.job, {"run_id": "saved-run", "cancelled": True})[1]["status"], "cancelled")
        with patch.object(backend, "load_resume", wraps=backend.load_resume) as load:
            self.assertEqual(backend._native_context(self.job)["run_id"], "saved-run")
            self.assertEqual(load.call_count, 0, "cancellation must retain the current identity")
        backend._RESUME_ID_CACHE.clear()
        backend._MANIFEST_CACHE.clear()
        status = backend.resume_status(self.job)[1]
        self.assertEqual(status["plan"]["previous_uploaded"], 1)
        self.assertEqual(len(status["files"]), 49999)
        recovered = self.prepare(ready_body)
        self.assertEqual(recovered["token"], ready["token"])
        self.assertTrue(recovered["uploaded"])
        self.assertEqual(self.finalize(recovered, ready_body)[0], 200)
        new_partial = self.prepare(partial_body)
        self.assertNotEqual(new_partial["token"], partial["token"])
        self.assertEqual(self.finalize(partial, partial_body)[0], 409)
        self.stage(new_partial)
        self.assertEqual(self.finalize(new_partial, partial_body)[0], 200)

    def test_cached_run_id_detects_checkpoint_replacement_and_removal(self):
        record = self.save_large_checkpoint()
        info = self.prepare()
        self.stage(info, ready=True)
        record["run_id"] = "replacement-run"
        backend._save_json(backend.resume_path(self.job), record)
        with patch.object(backend, "load_resume", wraps=backend.load_resume) as load:
            self.assertEqual(self.finalize(info)[0], 409)
            self.assertEqual(load.call_count, 1)
            self.assertEqual(backend._native_context(self.job)["run_id"], "replacement-run")
            self.assertEqual(load.call_count, 1)
        os.remove(backend.resume_path(self.job))
        with patch.object(backend, "load_resume", wraps=backend.load_resume) as load:
            self.assertEqual(backend._native_context(self.job)["run_id"], "")
            self.assertEqual(load.call_count, 0)
        self.assertEqual(self.finalize(info)[0], 409)

    def test_checkpoint_read_failure_is_retried_without_caching_an_empty_run(self):
        self.save_checkpoint()
        with patch.object(backend, "load_resume", return_value=None):
            with self.assertRaises(OSError):
                backend._native_context(self.job)
        self.assertNotIn(self.job["id"], backend._RESUME_ID_CACHE)
        self.assertEqual(backend._native_context(self.job)["run_id"], "saved-run")

    def test_checkpoint_replaced_during_cold_read_is_not_cached(self):
        self.save_checkpoint()
        original = backend.load_resume

        def replace_during_read(job):
            old = original(job)
            backend._save_json(backend.resume_path(job), dict(old, run_id="replacement-run"))
            return old

        with patch.object(backend, "load_resume", side_effect=replace_during_read):
            with self.assertRaises(OSError):
                backend._native_context(self.job)
        self.assertNotIn(self.job["id"], backend._RESUME_ID_CACHE)
        self.assertEqual(backend._native_context(self.job)["run_id"], "replacement-run")

    def test_start_resume_seeds_small_identity_and_successful_finish_invalidates_it(self):
        backend.manifest_meta(self.job, "docs")
        backend._native_context(self.job)  # initialize generation before creating the plan
        backend._SCAN_STATE[self.job["id"]] = {
            "phase": "done", "scan_id": "new-scan", "destination": self.job["destination"], "exclude": [],
            "incoming": {self.body["p"]: (5, self.body["m"])},
            "plan": {"folder": "docs", "total_files": 1, "upload": [[self.body["p"], 5]],
                     "deleted": [], "skipped": 0, "repaired": 0, "adopted": 0, "excluded_kept": 0}}
        body = {"scan_id": "new-scan", "source_path": r"D:\documents"}
        code, started = backend.start_resume(self.job, body)
        self.assertEqual(code, 200)
        self.assertEqual(backend.start_resume(self.job, body)[1]["run_id"], started["run_id"])
        with patch.object(backend, "load_resume", side_effect=AssertionError("new plan must already be cached")):
            self.assertEqual(backend._native_context(self.job)["run_id"], started["run_id"])
        info = self.prepare()
        self.stage(info)
        self.assertEqual(self.finalize(info)[0], 200)
        self.assertEqual(backend.finish_run(self.job, {"run_id": started["run_id"]})[1]["status"], "ok")
        self.assertFalse(Path(backend.resume_path(self.job)).exists())
        self.assertEqual(backend._native_context(self.job)["run_id"], "")
        self.assertEqual(self.finalize(info)[0], 409)

    def test_cancelled_finish_drains_inflight_finalize_and_resume_skips_it(self):
        self.save_checkpoint()
        info = self.prepare()
        self.stage(info, ready=True)
        syncing, release, finishing = threading.Event(), threading.Event(), threading.Event()
        original = backend.os.fsync

        def blocked_sync(fd):
            syncing.set()
            self.assertTrue(release.wait(5))
            original(fd)

        def finish():
            finishing.set()
            return backend.finish_run(self.job, {"run_id": "saved-run", "cancelled": True})

        with patch.object(backend.os, "fsync", side_effect=blocked_sync):
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(self.finalize, info)
                self.assertTrue(syncing.wait(3))
                cancelled = pool.submit(finish)
                self.assertTrue(finishing.wait(3))
                self.wait_for_lifecycle_writer()
                self.assertFalse(cancelled.done())
                release.set()
                self.assertEqual(pending.result(5)[0], 200)
                self.assertEqual(cancelled.result(5)[1]["status"], "cancelled")
        backend._MANIFEST_CACHE.clear()
        status = backend.resume_status(self.job)[1]
        self.assertEqual(status["plan"]["previous_uploaded"], 1)
        self.assertEqual(status["files"], [["later.txt", 5, self.body["m"]]])
        self.assertTrue(Path(backend.resume_path(self.job)).exists())

    def test_edit_delete_and_rescan_wait_for_finalize_then_reject_old_attempt(self):
        # Each lifecycle transition must wait without holding the global job registry lock.
        for action in ("edit", "delete", "scan"):
            with self.subTest(action=action):
                backend.save_jobs({self.job["id"]: self.job})
                info = self.prepare(dict(self.body, m=self.body["m"] + len(action)))
                self.stage(info)
                entered, release, requested = threading.Event(), threading.Event(), threading.Event()
                original = backend.os.fsync

                def blocked(fd):
                    entered.set()
                    self.assertTrue(release.wait(5))
                    original(fd)

                def change():
                    requested.set()
                    if action == "edit":
                        return backend.update_job(self.job["id"], {"pc_path": r"E:\new"})
                    if action == "delete":
                        return backend.delete_job(self.job["id"], False)
                    return backend.build_plan(self.job, {"folder": "docs", "files": [self.body]})

                with patch.object(backend.os, "fsync", side_effect=blocked):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        publishing = pool.submit(self.finalize, info)
                        self.assertTrue(entered.wait(3))
                        changing = pool.submit(change)
                        self.assertTrue(requested.wait(3))
                        self.wait_for_lifecycle_writer()
                        self.assertFalse(changing.done())
                        release.set()
                        self.assertEqual(publishing.result(5)[0], 200)
                        self.assertEqual(changing.result(5)[0], 200)
                for _ in range(100):
                    state = backend._SCAN_STATE.get(self.job["id"], {})
                    if state.get("phase") not in ("queued", "snapshot", "matching", "saving"):
                        break
                    time.sleep(.01)
                self.assertIn(self.finalize(info)[0], (404, 409))

    def test_process_exit_at_publish_boundaries_recovers_from_disk(self):
        self.save_large_checkpoint()
        script = r'''
import importlib.util,json,os,sys
spec=importlib.util.spec_from_file_location("crash_backend",sys.argv[1])
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
job,body,info,point=json.loads(sys.argv[2])
if point == "after-publish":
    b._run_append=lambda *args: os._exit(71)
elif point == "before-publish":
    original=b.os.replace
    def replace(src,dest):
        if dest == os.path.join(job["destination"],body["p"].replace("/",os.sep)):
            os._exit(72)
        return original(src,dest)
    b.os.replace=replace
b.finalize_native_upload(job,{"p":body["p"],"token":info["token"]})
'''
        for point, expected in [("before-publish", 72), ("after-publish", 71)]:
            with self.subTest(point=point):
                body = dict(self.body, p="crash/" + point + ".txt")
                info = self.prepare(body)
                self.stage(info, ready=True)
                env = dict(os.environ, PCBACKUP_DATA=backend.DATA_DIR,
                           PCBACKUP_USER_DATA_ROOT=backend.USER_DATA_ROOT, PLUG_USER="")
                result = subprocess.run([sys.executable, "-c", script, str(SOURCE),
                                         json.dumps([self.job, body, info, point])], env=env, timeout=10,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stderr)
                backend._MANIFEST_CACHE.clear()
                recovered = self.prepare(body)
                self.assertEqual(recovered["token"], info["token"])
                self.assertTrue(recovered["uploaded"])
                self.assertEqual(self.finalize(recovered, body)[0], 200)
                backend._MANIFEST_CACHE.clear()
                self.assertEqual(backend.load_manifest(self.job)["files"][body["p"]]["s"], 5)

    def test_chunk_resume_and_legacy_commit_stay_compatible(self):
        self.assertEqual(backend.put_chunk(self.job, self.body["p"], 0, b"he")[0], 200)
        self.assertEqual(backend.part_size(self.job, self.body["p"])[1]["size"], 2)
        self.assertEqual(backend.put_chunk(self.job, self.body["p"], 2, b"llo")[0], 200)
        self.assertEqual(backend.commit_file(self.job, self.body["p"], 5, self.body["m"])[0], 200)
        self.assertEqual((Path(self.job["destination"]) / self.body["p"]).read_bytes(), b"hello")

    def test_directory_batches_cover_file_2001(self):
        paths = ["old/file%d.txt" % n for n in range(2000)] + ["last/small.txt"]
        for start in range(0, len(paths), 2000):
            batch = paths[start:start + 2000]
            code, result = backend.mkdirs_for_job(self.job, batch)
            self.assertEqual(code, 200)
            self.assertEqual(result["processed"], len(batch))
        self.assertTrue((Path(self.job["destination"]) / "last").is_dir())

    def test_oversized_directory_batch_is_rejected_without_partial_changes(self):
        code, _ = backend.mkdirs_for_job(self.job, ["new/file%d" % n for n in range(2001)])
        self.assertEqual(code, 400)
        self.assertFalse((Path(self.job["destination"]) / "new").exists())

    def test_concurrent_directory_batches_do_not_double_count_a_shared_parent(self):
        target = str(Path(self.job["destination"]) / "shared/sub")
        barrier = threading.Barrier(2)
        observed = set()
        original = os.path.isdir

        def initial_missing(path):
            ident = threading.get_ident()
            if os.fspath(path) == target and ident not in observed:
                observed.add(ident)
                barrier.wait(timeout=3)
                return False
            return original(path)

        with patch.object(backend.os.path, "isdir", side_effect=initial_missing):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: backend.mkdirs_for_job(self.job, ["shared/sub/a.txt"]), range(2)))
        self.assertTrue(Path(target).is_dir())
        self.assertTrue(all(code == 200 and result["processed"] == 1 for code, result in results))
        self.assertEqual(sum(result["created"] for _, result in results), 1)

    def test_invalid_directory_path_does_not_create_earlier_directories(self):
        for invalid in ["../escape", "/absolute", "a\\b", None, 123]:
            with self.subTest(invalid=invalid):
                code, _ = backend.mkdirs_for_job(self.job, ["valid/a", invalid])
                self.assertEqual(code, 400)
                self.assertFalse((Path(self.job["destination"]) / "valid").exists())

    def test_native_prepare_creates_parent_and_persists_transfer(self):
        info = self.prepare()
        self.assertTrue((Path(self.job["destination"]) / "new/sub").is_dir())
        self.assertRegex(info["token"], r"^[a-f0-9]{32}$")
        self.assertRegex(info["temp_name"], r"^pcb_[a-f0-9]{32}\.temptd_\d+$")
        self.assertFalse(info["uploaded"])
        self.assertTrue(list((Path(backend.JOBS_DIR) / self.job["id"] / "native").glob("*.json")))

    def test_complete_recovers_full_put_when_native_move_failed(self):
        info = self.prepare()
        stage = self.stage(info)
        code, data = self.complete(info)
        self.assertEqual(code, 200, data)
        part = Path(self.job["destination"]) / (self.body["p"] + backend.PART_SUFFIX)
        self.assertEqual(part.read_bytes(), b"hello")
        self.assertFalse(stage.exists())
        self.assertEqual(self.complete(info)[0], 200)
        code, _ = backend.commit_file(self.job, self.body["p"], 5, self.body["m"])
        self.assertEqual(code, 200)
        self.assertEqual((Path(self.job["destination"]) / self.body["p"]).read_bytes(), b"hello")
        self.assertEqual(self.complete(info)[0], 200)

    def test_prepare_recovers_ready_file_after_page_closed(self):
        first = self.prepare()
        self.stage(first, ready=True)
        recovered = self.prepare()
        self.assertEqual(recovered["token"], first["token"])
        self.assertTrue(recovered["uploaded"])
        self.assertEqual(self.complete(recovered)[0], 200)

    def test_prepare_reuses_a_promoted_part_after_response_loss(self):
        first = self.prepare()
        self.stage(first, ready=True)
        self.assertEqual(self.complete(first)[0], 200)
        recovered = self.prepare()
        self.assertEqual(recovered["token"], first["token"])
        self.assertTrue(recovered["uploaded"])

    def test_partial_native_data_does_not_replace_existing_part(self):
        info = self.prepare()
        self.stage(info, b"he")
        part = Path(self.job["destination"]) / (self.body["p"] + backend.PART_SUFFIX)
        part.write_bytes(b"older")
        self.assertEqual(self.complete(info)[0], 409)
        self.assertEqual(part.read_bytes(), b"older")
        retry = self.prepare()
        self.assertNotEqual(retry["token"], info["token"])
        self.assertFalse(retry["uploaded"])

    def test_active_upload_is_not_reused_from_full_temp_until_settled(self):
        first = self.prepare()
        self.stage(first)
        retry = self.prepare()
        self.assertNotEqual(retry["token"], first["token"])
        self.assertFalse(retry["uploaded"])
        self.assertEqual(self.complete(first)[0], 409)

    def test_changed_source_metadata_cannot_reuse_old_stage(self):
        first = self.prepare()
        self.stage(first, ready=True)
        for field, value in [("m", self.body["m"] + 1), ("source", r"E:\other\file.txt"), ("s", 6)]:
            second = self.prepare(dict(self.body, **{field: value}))
            self.assertNotEqual(second["token"], first["token"])
            self.assertFalse(second["uploaded"])
            self.assertEqual(self.complete(first)[0], 409)

    def test_native_rejects_invalid_metadata(self):
        for change in [{"p": "../outside"}, {"s": -1}, {"s": True}, {"m": "bad"}, {"source": ""}]:
            code, _ = backend.prepare_native_upload(self.job, dict(self.body, **change))
            self.assertEqual(code, 400)

    def test_native_rejects_unknown_token(self):
        self.prepare()
        for token in ["../bad", "0" * 32, None]:
            code, _ = backend.complete_native_upload(self.job, {"p": self.body["p"], "token": token})
            self.assertIn(code, (400, 409))

    @unittest.skipIf(os.name == "nt", "NAS symlink behavior is tested on Linux")
    def test_native_rejects_symlinked_staging_directory(self):
        outside = self.root / "outside"
        outside.mkdir()
        (Path(backend.USER_DATA_ROOT) / ".temp_upload_dir").symlink_to(outside, target_is_directory=True)
        self.assertEqual(backend.prepare_native_upload(self.job, self.body)[0], 400)

    def test_commit_response_loss_can_be_retried(self):
        backend.put_chunk(self.job, self.body["p"], 0, b"hello")
        self.assertEqual(backend.commit_file(self.job, self.body["p"], 5, self.body["m"])[0], 200)
        self.assertEqual(backend.commit_file(self.job, self.body["p"], 5, self.body["m"])[0], 200)
        self.assertEqual(backend.commit_file(self.job, self.body["p"], 6, self.body["m"])[0], 400)

    def test_http_native_prepare_complete_and_commit(self):
        backend.save_jobs({self.job["id"]: self.job})
        server = backend.ThreadingHTTPServer(("127.0.0.1", 0), backend.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        prefix = "http://127.0.0.1:%d/api/jobs/%s/" % (server.server_port, self.job["id"])

        def post(route, body):
            request = Request(prefix + route, data=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=3) as response:
                return json.load(response)["data"]

        info = post("native-upload", dict(self.body, action="prepare"))
        self.stage(info, ready=True)
        post("native-upload", {"action": "complete", "p": self.body["p"], "token": info["token"]})
        post("commit", self.body)
        self.assertEqual((Path(self.job["destination"]) / self.body["p"]).read_bytes(), b"hello")
        new_body = dict(self.body, p="http/finalize.txt")
        info = post("native-upload", dict(new_body, action="prepare"))
        self.stage(info)
        receipt = post("native-upload", {"action": "finalize", "p": new_body["p"], "token": info["token"]})
        self.assertTrue(receipt["committed"])
        self.assertEqual(post("native-upload", {"action": "finalize", "p": new_body["p"], "token": info["token"]}), receipt)


if __name__ == "__main__":
    unittest.main()
