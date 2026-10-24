import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
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
        backend.USER_DATA_ROOT = str(self.root / "pool")
        backend._MANIFEST_CACHE.clear()
        backend._MANIFEST_PATH_CACHE.clear()
        backend._RUN_CURRENT.clear()
        self.job = {"id": "1234abcd", "name": "test", "destination": str(self.root / "pool/backup")}
        Path(self.job["destination"]).mkdir(parents=True)
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


if __name__ == "__main__":
    unittest.main()
