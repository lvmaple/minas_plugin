"""Finish/recovery checks with 50,000 actual completed files on local storage.

The fixture represents already committed files, not simulated desktop throughput
or a NAS/CFS benchmark. It never connects to the production service.
"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
import unittest
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "xiaomi-nas-plugin-pcbackup/src/files/httpd.py"
spec = importlib.util.spec_from_file_location("pcbackup_finish_scale_backend", SOURCE)
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class FinishScaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        started = time.perf_counter()
        cls.temp = tempfile.TemporaryDirectory(prefix="pcbackup-finish-scale-")
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        backend.DATA_DIR = str(root / "etc")
        backend.JOBS_DIR = str(root / "etc/jobs")
        backend.JOBS_FILE = str(root / "etc/jobs.json")
        backend.HISTORY_FILE = str(root / "etc/history.json")
        backend.USER_DATA_ROOT = str(root / "pool")
        cls.job = {"id": "scale001", "name": "scale", "exclude": [],
                   "destination": str(root / "pool/backup")}
        backend.save_jobs({cls.job["id"]: cls.job})
        cls.mtime = 1700000000000
        cls.files = []
        for group in range(100):
            parent = "group-%03d/" % group + "nested/" * 6
            directory = Path(cls.job["destination"]) / parent
            directory.mkdir(parents=True)
            for number in range(500):
                rel = parent + "%05d.bin" % number
                path = Path(cls.job["destination"]) / rel
                path.write_bytes(b"x")
                os.utime(path, ns=(cls.mtime * 1000000, cls.mtime * 1000000))
                cls.files.append([rel, 1, cls.mtime])
        backend.manifest_meta(cls.job, "docs")
        backend.manifest_upsert(cls.job, [(rel, {"s": size, "m": mtime})
                                        for rel, size, mtime in cls.files])
        print(json.dumps({"fixture_completed_files": len(cls.files),
                          "fixture_seconds": round(time.perf_counter() - started, 3),
                          "storage": "local temporary directory, not NAS"}), flush=True)

    def setUp(self):
        for name in ("_MANIFEST_CACHE", "_MANIFEST_PATH_CACHE", "_RUN_CURRENT", "_RESUME_ID_CACHE",
                     "_RESUME_CHECKS", "_SCAN_STATE"):
            getattr(backend, name).clear()
        Path(backend.HISTORY_FILE).unlink(missing_ok=True)
        backend.save_jobs({self.job["id"]: self.job})
        self.run_id = uuid.uuid4().hex
        self.generation = backend._native_generation(self.job)
        record = {
            "version": 1, "run_id": self.run_id, "generation": self.generation, "scan_id": "scale-scan",
            "started": 1700000000, "source_path": r"D:\docs", "destination": self.job["destination"],
            "exclude": [], "folder": "docs", "total_files": 50000, "files": self.files,
            "deleted": [], "skipped": 0, "repaired": 0, "adopted": 0, "excluded_kept": 0,
            "del_mode": "del", "prune_excluded": False,
        }
        backend._save_json(backend.resume_path(self.job), record)
        self.stats = {"run_id": self.run_id, "uploaded": 50000, "uploaded_bytes": 50000,
                      "failed": 0, "cancelled": False}

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

    def wait_receipt(self, request_id):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            code, out = self.request("GET", "finish-status?request_id=" + request_id)
            self.assertEqual(code, 200, out)
            if out["phase"] == "done":
                return out
            time.sleep(.01)
        self.fail("background finish did not settle")

    def finish(self, **stats):
        return backend.finish_run(self.job, dict(self.stats, **stats),
                                  expected_generation=self.generation)

    def wait_resume(self, request_id):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            code, out = self.request("GET", "resume?async=1&request_id=" + request_id)
            self.assertEqual(code, 200, out)
            if out["phase"] == "done":
                return out["result"]
            time.sleep(.01)
        self.fail("background resume check did not settle")

    def test_50000_resume_verification_keeps_http_job_list_responsive(self):
        other = {"id": "scale002", "name": "other", "exclude": [],
                 "destination": str(Path(self.job["destination"]).parent / "other")}
        backend.save_jobs({self.job["id"]: self.job, other["id"]: other})
        entered, release = threading.Event(), threading.Event()
        original = backend._file_matches

        def gated(*args):
            if not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test gate expired")
            return original(*args)

        request_id = uuid.uuid4().hex
        with patch.object(backend, "_file_matches", side_effect=gated):
            self.assertEqual(self.request("GET", "resume?async=1&request_id=" + request_id)[0], 200)
            self.assertTrue(entered.wait(5))
            try:
                started = time.monotonic()
                for _ in range(3):
                    code, jobs = self.request("GET", "/api/jobs")
                    self.assertEqual(code, 200)
                    self.assertEqual(len(jobs), 2)
                    current = next(j for j in jobs if j["id"] == self.job["id"])
                    self.assertEqual(current["finish_pending"], "")
                    self.assertEqual(current["resume_check_pending"], request_id)
                    self.assertTrue(current["summary_pending"])
                self.assertLess(time.monotonic() - started, 2)
            finally:
                release.set()
                info = self.wait_resume(request_id)
        self.assertEqual(info["plan"]["previous_uploaded"], 50000)
        self.assertEqual(info["files"], [])
        summary = backend.job_summary(self.job)
        self.assertFalse(summary["summary_pending"])
        self.assertEqual(summary["files"], 50000)

    def test_50000_corrupt_rescan_cancel_and_fresh_process_resume_preserve_progress(self):
        path = Path(backend.resume_path(self.job))
        damaged = path.read_bytes()[:-4] + b"!"
        pending = "rescan-pending.txt"
        backend.put_chunk(self.job, pending, 0, b"he")
        path.write_bytes(damaged)
        pending_mtime = self.mtime + 5000
        entries = [{"p": rel, "s": size, "m": mtime} for rel, size, mtime in self.files]
        body = {"folder": "docs", "files": entries + [{"p": pending, "s": 5, "m": pending_mtime}]}
        try:
            self.assertEqual(self.request("PATCH", "", {"pc_path": r"D:\reselected"})[0], 200)
            self.job = backend.load_jobs()[self.job["id"]]
            self.assertEqual(path.read_bytes(), damaged)
            self.assertEqual(list(path.parent.glob(path.name + ".corrupt-*")), [])
            code, out = self.request("POST", "scan", body)
            self.assertEqual(code, 409, out)
            self.assertEqual(out["data"]["reason"], "resume_corrupt")
            self.assertEqual(path.read_bytes(), damaged)
            body["replace_resume"] = True
            self.assertEqual(self.request("POST", "scan", body)[0], 200)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                _, state = self.request("GET", "scan-status")
                if state["phase"] in ("done", "error"):
                    break
                time.sleep(.01)
            self.assertEqual(state["phase"], "done", state)
            plan = state["plan"]
            self.assertEqual(plan["skipped"], 50000)
            self.assertEqual(plan["upload"], [[pending, 5]])
            self.assertNotEqual(plan["generation"], self.generation)
            copies = list(path.parent.glob(path.name + ".corrupt-*"))
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0].read_bytes(), damaged)
            self.assertEqual(backend.part_size(self.job, pending)[1]["size"], 0)
            part = Path(self.job["destination"]) / (pending + backend.PART_SUFFIX)
            self.assertEqual(part.read_bytes(), b"he")
            code, info = self.request("POST", "resume", {"generation": plan["generation"],
                "scan_id": plan["scan_id"], "source_path": self.job["pc_path"]})
            self.assertEqual(code, 200, info)
            self.assertEqual(backend.part_size(self.job, pending)[1]["size"], 0)
            for _ in range(2):  # A lost chunk acknowledgement retries the same prefix.
                self.assertEqual(backend.put_chunk(self.job, pending, 0, b"wo")[0], 200)
            self.assertEqual(backend.part_size(self.job, pending)[1]["size"], 2)
            old_parts = list((Path(self.job["destination"]) / backend.MIRROR_DIRNAME / "partial-recovery").glob("*.pcbp.part"))
            self.assertEqual(len(old_parts), 1)
            self.assertEqual(old_parts[0].read_bytes(), b"he")
            request_id = uuid.uuid4().hex
            self.assertEqual(self.request("POST", "finish", {"async": True,
                "request_id": request_id, "generation": plan["generation"],
                "run_id": info["run_id"], "cancelled": True, "skipped": 50000})[0], 200)
            self.assertEqual(self.wait_receipt(request_id)["result"]["status"], "cancelled")
            self.assertTrue(path.exists())
            script = """
import importlib.util,json,sys,time,uuid
s=importlib.util.spec_from_file_location('restarted',sys.argv[1])
b=importlib.util.module_from_spec(s);s.loader.exec_module(b)
b.DATA_DIR=sys.argv[2];b.JOBS_DIR=b.DATA_DIR+'/jobs';b.JOBS_FILE=b.DATA_DIR+'/jobs.json'
b.HISTORY_FILE=b.DATA_DIR+'/history.json';j=b.load_jobs()['scale001'];rid=uuid.uuid4().hex
for _ in range(5000):
    code,out=b.resume_check(j,rid)
    assert code==200,out
    if out['phase']=='done':break
    time.sleep(.001)
else:raise RuntimeError('resume did not settle')
info=out['result'];assert info['plan']['skipped']==50000
assert info['source_path']==sys.argv[3]
assert info['files']==[['rescan-pending.txt',5,1700000005000]]
assert b.part_size(j,'rescan-pending.txt')[1]['size']==2
assert b.put_chunk(j,'rescan-pending.txt',2,b'rld',expected_generation=info['generation'])[0]==200
assert b.commit_file(j,'rescan-pending.txt',5,1700000005000,expected_generation=info['generation'])[0]==200
stats={'run_id':info['run_id'],'generation':info['generation'],'request_id':uuid.uuid4().hex,'uploaded':1,'skipped':50000}
assert b.finish_async(j,stats)[0]==200
for _ in range(5000):
    code,out=b.finish_async_status(j,stats['request_id']);assert code==200,out
    if out['phase']=='done':break
    time.sleep(.001)
else:raise RuntimeError('finish did not settle')
print(json.dumps(out['result']))
"""
            receipt = json.loads(subprocess.check_output(
                [sys.executable, "-X", "utf8", "-c", script, str(SOURCE), backend.DATA_DIR, self.job["pc_path"]],
                text=True, timeout=30))
            self.assertEqual((receipt["status"], receipt["uploaded"], receipt["skipped"]), ("ok", 1, 50000))
            self.assertFalse(path.exists())
            self.assertEqual((Path(self.job["destination"]) / pending).read_bytes(), b"world")
        finally:
            backend._MANIFEST_CACHE.clear()
            backend.manifest_del(self.job, [pending])
            (Path(self.job["destination"]) / pending).unlink(missing_ok=True)
            (Path(self.job["destination"]) / (pending + backend.PART_SUFFIX)).unlink(missing_ok=True)

    def test_50000_completed_files_finish_once_and_lost_ack_retry_is_idempotent(self):
        started = time.perf_counter()
        with patch.object(backend, "resume_status", wraps=backend.resume_status) as status:
            code, receipt = self.finish()
            elapsed = time.perf_counter() - started
            self.assertEqual((code, receipt["status"]), (200, "ok"))
            self.assertEqual(status.call_count, 1)
            self.assertEqual(self.finish(), (code, receipt))
            self.assertEqual(status.call_count, 1, "lost finish ACK must not rescan 50,000 files")
        self.assertFalse(Path(backend.resume_path(self.job)).exists())
        self.assertNotIn(self.job["id"], backend._RESUME_ID_CACHE)
        self.assertEqual(len(backend.load_history()), 1)
        print(json.dumps({"completed_files": 50000, "finish_seconds": round(elapsed, 3),
                          "lost_ack_revalidated": False, "storage": "local, not NAS"}), flush=True)

    def test_50000_completed_files_cancel_restart_resume_and_finish(self):
        paths = [Path(self.job["destination"]) / ("excluded-%d.tmp" % n) for n in range(3)]
        for path in paths:
            path.write_bytes(b"excluded data")
        backend.manifest_exclude(self.job, [path.name for path in paths])
        entered, release = threading.Event(), threading.Event()
        original = backend.os.remove

        def gated_remove(path):
            original(path)
            if str(path) == str(paths[0]):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test gate expired")

        request_id = uuid.uuid4().hex
        body = dict(self.stats, generation=self.generation, request_id=request_id,
                    prune_excluded=True, **{"async": True})
        with patch.object(backend.os, "remove", side_effect=gated_remove):
            self.assertEqual(self.request("POST", "finish", body)[0], 200)
            self.assertTrue(entered.wait(120))
            try:
                self.assertEqual(backend.load_history(), [])
                code, out = self.request("POST", "finish-cancel", {"request_id": request_id})
                self.assertEqual(code, 200)
                self.assertTrue(out["cancel_accepted"])
            finally:
                release.set()
            receipt = self.wait_receipt(request_id)["result"]
            self.assertEqual(receipt["status"], "cancelled")
            self.assertEqual(receipt["pruned_excluded"], 1)
        self.assertTrue(all(path.exists() for path in paths[1:]))
        self.assertTrue(Path(backend.resume_path(self.job)).exists())
        backend._MANIFEST_CACHE.clear()
        backend._RESUME_ID_CACHE.clear()
        code, info = backend.resume_status(self.job)
        self.assertEqual(code, 200)
        self.assertEqual(info["run_id"], self.run_id)
        self.assertEqual(info["plan"]["previous_uploaded"], 50000)
        self.assertEqual(info["files"], [])
        self.assertEqual(self.finish(prune_excluded=True)[1]["status"], "ok")
        self.assertFalse(any(path.exists() for path in paths))
        self.assertFalse(Path(backend.resume_path(self.job)).exists())
        self.assertEqual([h["status"] for h in backend.load_history()], ["cancelled", "ok"])

    def test_50000_completed_files_history_failure_retains_checkpoint_for_retry(self):
        original = backend._save_json

        def disk_full(path, value):
            if path == backend.HISTORY_FILE:
                raise OSError("injected history disk full")
            return original(path, value)

        with patch.object(backend, "_save_json", side_effect=disk_full):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.finish()
        self.assertTrue(Path(backend.resume_path(self.job)).exists())
        self.assertEqual(backend.load_history(), [])
        self.assertEqual(self.finish()[1]["status"], "ok")
        self.assertEqual(len(backend.load_history()), 1)

    def test_one_missing_file_blocks_success_after_50000_files_were_recorded(self):
        rel = self.files[-1][0]
        target = Path(self.job["destination"]) / rel
        target.unlink()
        try:
            self.assertEqual(self.finish()[1]["status"], "partial")
            self.assertTrue(Path(backend.resume_path(self.job)).exists())
            code, info = backend.resume_status(self.job)
            self.assertEqual(code, 200)
            self.assertEqual(info["files"], [[rel, 1, self.mtime]])
            self.assertEqual(info["plan"]["previous_uploaded"], 49999)
        finally:
            target.write_bytes(b"x")
            os.utime(target, ns=(self.mtime * 1000000, self.mtime * 1000000))

    def test_50000_async_http_finish_and_lost_ack_retry_use_one_physical_check(self):
        request_id = uuid.uuid4().hex
        body = dict(self.stats, generation=self.generation, request_id=request_id, **{"async": True})
        entered, release = threading.Event(), threading.Event()
        original = backend.resume_status
        save_json = backend._save_json

        def fail_receipt(path, value):
            if path == backend._finish_path(self.job) and value.get("phase") == "done":
                raise OSError("receipt disk full")
            return save_json(path, value)

        def gated(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test gate expired")
            return original(*args, **kwargs)

        with patch.object(backend, "resume_status", side_effect=gated) as check:
            with patch.object(backend, "_save_json", side_effect=fail_receipt):
                self.assertEqual(self.request("POST", "finish", body)[1]["phase"], "running")
                self.assertTrue(entered.wait(5))
                try:
                    started = time.monotonic()
                    for _ in range(3):
                        self.assertEqual(self.request("POST", "finish", body)[1]["phase"], "running")
                        self.assertEqual(self.request("GET", "finish-status?request_id=" + request_id)[1]["phase"], "running")
                    self.assertLess(time.monotonic() - started, 2)
                finally:
                    release.set()
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    code, _ = self.request("GET", "finish-status?request_id=" + request_id)
                    if code == 500:
                        break
                    time.sleep(.01)
                else:
                    self.fail("injected receipt failure was not visible")
            self.assertEqual(backend.job_summary(self.job)["finish_pending"], request_id)
            canonical = backend.load_jobs()[self.job["id"]]["last_stats"]
            self.assertFalse(Path(backend.resume_path(self.job)).exists())
            for n in range(backend.HISTORY_KEEP + 5):
                backend.append_history({"job_id": "other", "run_id": "other-%d" % n, "status": "ok"})
            self.assertFalse(any(h.get("run_id") == self.run_id for h in backend.load_history()))
            script = """
import importlib.util, json, sys, time
from unittest.mock import patch
s=importlib.util.spec_from_file_location('restarted',sys.argv[1])
b=importlib.util.module_from_spec(s); s.loader.exec_module(b)
b.DATA_DIR=sys.argv[2]; b.JOBS_DIR=b.DATA_DIR+'/jobs'; b.JOBS_FILE=b.DATA_DIR+'/jobs.json'
b.HISTORY_FILE=b.DATA_DIR+'/history.json'; j=b.load_jobs()['scale001']
with patch.object(b,'resume_status',side_effect=AssertionError('completed files must not be revalidated')):
    assert b.retry_finish(j,sys.argv[3])[0]==200
    for _ in range(5000):
        code,out=b.finish_async_status(j,sys.argv[3])
        if code!=200: raise RuntimeError(out)
        if out['phase']=='done': break
        time.sleep(.001)
    else: raise RuntimeError('receipt recovery timed out')
print(json.dumps(out['result']))
"""
            recovered = json.loads(subprocess.check_output(
                [sys.executable, "-X", "utf8", "-c", script, str(SOURCE), backend.DATA_DIR,
                 request_id], text=True, timeout=15))
            self.assertEqual(recovered, canonical)
            # A reopened page has only the saved request ID, not original stats.
            self.assertEqual(self.request("POST", "finish-retry", {"request_id": request_id})[0], 200)
            receipt = self.wait_receipt(request_id)
            self.assertEqual(receipt["result"], canonical)
            self.assertEqual(self.request("POST", "finish", body)[1], receipt)
            self.assertEqual(check.call_count, 1)
        self.assertEqual(len(backend.load_history()), backend.HISTORY_KEEP)
        self.assertFalse(any(h.get("run_id") == self.run_id for h in backend.load_history()))
        self.assertFalse(Path(backend.resume_path(self.job)).exists())

    def test_50000_cancel_during_async_verification_then_new_process_resumes_without_upload(self):
        request_id = uuid.uuid4().hex
        body = dict(self.stats, generation=self.generation, request_id=request_id, **{"async": True})
        entered, release = threading.Event(), threading.Event()
        original = backend._file_matches

        def gated(*args):
            if not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test gate expired")
            return original(*args)

        with patch.object(backend, "_file_matches", side_effect=gated):
            self.request("POST", "finish", body)
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(self.request("POST", "finish-cancel", {"request_id": request_id})[1]["phase"], "running")
            finally:
                release.set()
            self.assertEqual(self.wait_receipt(request_id)["result"]["status"], "cancelled")
        self.assertTrue(Path(backend.resume_path(self.job)).exists())
        script = """
import importlib.util, io, json, sys, time, uuid
from unittest.mock import patch
s=importlib.util.spec_from_file_location('restarted',sys.argv[1])
b=importlib.util.module_from_spec(s); s.loader.exec_module(b)
b.DATA_DIR=sys.argv[2]; b.JOBS_DIR=b.DATA_DIR+'/jobs'; b.JOBS_FILE=b.DATA_DIR+'/jobs.json'
b.HISTORY_FILE=b.DATA_DIR+'/history.json'; j=b.load_jobs()['scale001']; rid=uuid.uuid4().hex
with patch.object(b.threading.Thread,'start',side_effect=RuntimeError('cannot start new thread')):
    code,_=b.resume_check(j,rid)
    assert code==500 and j['id'] not in b._RESUME_CHECKS
original_open=open; checkpoint=b.resume_path(j)
with original_open(checkpoint,'rb') as stream: saved_checkpoint=stream.read()
class FailedRead:
    def __init__(self,stream): self.stream=stream
    def __enter__(self): return self
    def __exit__(self,*args): self.stream.close()
    def read(self): raise OSError('real checkpoint read failed')
for failure in ('open','read','decode'):
    fired=[]
    def fail_once(path,*args,**kwargs):
        if str(path)==checkpoint and not fired:
            fired.append(failure)
            if failure=='open': raise OSError('real checkpoint open failed')
            if failure=='read': return FailedRead(original_open(path,*args,**kwargs))
            return io.StringIO('{invalid json')
        return original_open(path,*args,**kwargs)
    with patch.object(b,'open',side_effect=fail_once,create=True):
        b.resume_check(j,rid)
        for _ in range(5000):
            code,_=b.resume_check(j,rid)
            if code==500: break
            time.sleep(.001)
        else: raise RuntimeError('real checkpoint failure was hidden: '+failure)
    assert fired==[failure]
    with original_open(checkpoint,'rb') as stream: assert stream.read()==saved_checkpoint
for _ in range(120000):
    code, out=b.resume_check(j,rid)
    if code!=200: raise RuntimeError(out)
    if out['phase']=='done': break
    time.sleep(.001)
else: raise RuntimeError('resume timed out')
info=out['result']
assert info['files']==[] and info['plan']['previous_uploaded']==50000
stats={'run_id':info['run_id'],'uploaded':50000,'request_id':uuid.uuid4().hex,
       'generation':info['generation']}
b.finish_async(j,stats)
for _ in range(120000):
    code,out=b.finish_async_status(j,stats['request_id'])
    if code!=200: raise RuntimeError(out)
    if out['phase']=='done': break
    time.sleep(.001)
else: raise RuntimeError('finish timed out')
print(json.dumps({'completed':info['plan']['previous_uploaded'],'pending':len(info['files']),
                  'status':out['result']['status']}))
"""
        result = json.loads(subprocess.check_output(
            [sys.executable, "-X", "utf8", "-c", script, str(SOURCE), backend.DATA_DIR],
            text=True, timeout=240))
        self.assertEqual(result, {"completed": 50000, "pending": 0, "status": "ok"})
        self.assertEqual([h["status"] for h in backend.load_history()], ["cancelled", "ok"])


    def test_50000_browser_checkpoint_cancel_reopens_in_a_new_process_without_reupload(self):
        record = backend.load_resume(self.job)
        rel = "browser/pending.txt"
        def restore_fixture():
            backend._MANIFEST_CACHE.clear()
            backend._RUN_CURRENT.clear()
            backend.manifest_del(self.job, [rel])
            (Path(self.job["destination"]) / rel).unlink(missing_ok=True)
            (Path(self.job["destination"]) / (rel + backend.PART_SUFFIX)).unlink(missing_ok=True)
        self.addCleanup(restore_fixture)
        record.update(source_kind="browser", source_path="", files=self.files + [[rel, 5, self.mtime]])
        backend._save_json(backend.resume_path(self.job), record)
        backend._RESUME_ID_CACHE.clear()
        self.assertEqual(backend.put_chunk(self.job, rel, 0, b"he")[0], 200)
        self.assertEqual(self.finish(cancelled=True)[1]["status"], "cancelled")
        script = """
import importlib.util,json,sys,os,uuid
spec=importlib.util.spec_from_file_location('browser_restart',sys.argv[1])
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
b.DATA_DIR=sys.argv[2];b.JOBS_DIR=os.path.join(b.DATA_DIR,'jobs')
b.JOBS_FILE=os.path.join(b.DATA_DIR,'jobs.json');b.HISTORY_FILE=os.path.join(b.DATA_DIR,'history.json')
j=b.load_jobs()['scale001'];code,info=b.resume_status(j)
assert code==200 and info['source_kind']=='browser'
assert info['plan']['previous_uploaded']==50000 and len(info['files'])==1
rel,size,mtime=info['files'][0]
assert b.part_size(j,rel)[1]['size']==2
assert b.put_chunk(j,rel,2,b'llo')[0]==200
assert b.put_chunk(j,rel,2,b'llo')[0]==200
assert b.commit_file(j,rel,size,mtime)[0]==200
stats={'run_id':info['run_id'],'generation':info['generation'],'request_id':uuid.uuid4().hex,'uploaded':50001}
code,out=b.finish_run(j,stats,expected_generation=info['generation'])
assert code==200 and out['status']=='ok'
with open(os.path.join(j['destination'],rel),'rb') as stream: assert stream.read()==b'hello'
print(json.dumps({'previous_uploaded':50000,'resumed_offset':2,'status':out['status']}))
"""
        result = json.loads(subprocess.check_output(
            [sys.executable, "-X", "utf8", "-c", script, str(SOURCE), backend.DATA_DIR],
            text=True, timeout=240))
        self.assertEqual(result, {"previous_uploaded": 50000, "resumed_offset": 2, "status": "ok"})
        self.assertEqual([h["status"] for h in backend.load_history()], ["cancelled", "ok"])


if __name__ == "__main__":
    unittest.main()
