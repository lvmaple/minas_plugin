"""Isolated backend benchmark, with a real 50,000-entry checkpoint and six workers.

No production service or backup task is used. Data arrival is simulated by writing
the native staging file; this measures backend overhead, not desktop upload speed.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch


DEFAULT_SOURCE = Path(__file__).resolve().parents[1] / "xiaomi-nas-plugin-pcbackup/src/files/httpd.py"


def measure(backend, count, lookups, files):
    with tempfile.TemporaryDirectory(prefix="pcbackup-context-bench-") as tmp:
        root = Path(tmp)
        backend.DATA_DIR = str(root / "etc")
        backend.JOBS_DIR = str(root / "etc/jobs")
        backend.JOBS_FILE = str(root / "etc/jobs.json")
        backend.USER_DATA_ROOT = str(root / "pool")
        backend._MANIFEST_CACHE.clear()
        backend._MANIFEST_PATH_CACHE.clear()
        backend._RUN_CURRENT.clear()
        if hasattr(backend, "_RESUME_ID_CACHE"):
            backend._RESUME_ID_CACHE.clear()
        job = {"id": "bench001", "name": "isolated", "destination": str(root / "pool/backup")}
        Path(job["destination"]).mkdir(parents=True)
        backend.save_jobs({job["id"]: job})
        entries = [["benchmark/" + "nested-directory/" * 8 + "%06d-document.bin" % n, 1024, 1700000000000]
                   for n in range(count)]
        run_id = "benchmark-run"
        backend._save_json(backend.resume_path(job), {"version": 1, "run_id": run_id, "files": entries})
        size = Path(backend.resume_path(job)).stat().st_size
        stages = {}
        with patch.object(backend, "load_resume", wraps=backend.load_resume) as load:
            wall, cpu = time.perf_counter(), time.process_time()
            assert backend._native_context(job)["run_id"] == "benchmark-run"
            stages["cold"] = {"wall_ms": (time.perf_counter() - wall) * 1000,
                              "cpu_ms": (time.process_time() - cpu) * 1000, "full_plan_reads": load.call_count}
            load.reset_mock()
            barrier = threading.Barrier(6)

            def worker(_):
                barrier.wait(timeout=10)
                for _ in range(lookups // 6):
                    assert backend._native_context(job)["run_id"] == "benchmark-run"

            wall, cpu = time.perf_counter(), time.process_time()
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(worker, range(6)))
            stages["warm"] = {"calls": lookups, "wall_ms": (time.perf_counter() - wall) * 1000,
                              "cpu_ms": (time.process_time() - cpu) * 1000, "full_plan_reads": load.call_count}
            load.reset_mock()

            def upload(n):
                # All paths and metadata belong to the saved plan.
                rel, size, mtime = entries[n]
                body = {"p": rel, "s": size, "m": mtime,
                        "source": "D:\\benchmark\\%06d.bin" % n, "run_id": run_id}
                code, prepared = backend.prepare_native_upload(job, body)
                assert code == 200, prepared
                stage = root / "pool/.temp_upload_dir" / prepared["ready_name"]
                stage.write_bytes(b"x" * 1024)
                code, result = backend.finalize_native_upload(job, {"p": rel, "token": prepared["token"]})
                assert code == 200 and result["committed"], result

            # The full file pipeline is measured only with distinct paths.
            if count >= files:
                wall, cpu = time.perf_counter(), time.process_time()
                with ThreadPoolExecutor(max_workers=6) as pool:
                    list(pool.map(upload, range(files)))
                seconds = time.perf_counter() - wall
                stages["pipeline"] = {"files": files, "seconds": seconds, "files_per_second": files / seconds,
                                      "cpu_ms": (time.process_time() - cpu) * 1000, "full_plan_reads": load.call_count}
                # A normal incremental modification must not create one useless
                # retirement file per already-completed native upload.
                entries = [[p, s, m + 1000] for p, s, m in entries]
                run_id = "benchmark-modified"
                record = {"version": 1, "run_id": run_id, "files": entries}
                backend._save_json(backend.resume_path(job), record)
                with backend._job_lock(job["id"]):
                    if hasattr(backend, "_remember_resume_id"):
                        backend._remember_resume_id(job, record)
                load.reset_mock()
                retired_writes = []
                original_save = backend._save_json
                def track_save(path, value):
                    if "native-retired" in str(path):
                        retired_writes.append(path)
                    return original_save(path, value)
                wall, cpu = time.perf_counter(), time.process_time()
                with patch.object(backend, "_save_json", side_effect=track_save):
                    with ThreadPoolExecutor(max_workers=6) as pool:
                        list(pool.map(upload, range(files)))
                stages["modified_pipeline"] = {"files": files, "seconds": time.perf_counter() - wall,
                    "cpu_ms": (time.process_time() - cpu) * 1000, "full_plan_reads": load.call_count,
                    "retirement_json_writes": len(retired_writes)}
        return {"checkpoint_entries": count, "checkpoint_bytes": size, "workers": 6, "stages": stages}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--lookups", type=int, default=24)
    parser.add_argument("--files", type=int, default=24)
    parser.add_argument("--require-fast-path", action="store_true")
    args = parser.parse_args()
    assert args.lookups > 0 and args.lookups % 6 == 0 and 0 < args.files <= 50000
    spec = importlib.util.spec_from_file_location("context_bench_backend", args.source)
    backend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend)
    results = [measure(backend, count, args.lookups, args.files) for count in (2, 50000)]
    if args.require_fast_path:
        for result in results:
            assert result["stages"]["cold"]["full_plan_reads"] == 1
            assert result["stages"]["warm"]["full_plan_reads"] == 0
        assert results[1]["stages"]["pipeline"]["full_plan_reads"] == 0
        assert results[1]["stages"]["modified_pipeline"]["full_plan_reads"] == 0
        assert results[1]["stages"]["modified_pipeline"]["retirement_json_writes"] == 0
    print(json.dumps({"version": backend.PLUGIN_VERSION, "source": str(args.source), "isolated": True,
                      "simulated_data_arrival": True, "results": results}, indent=2))


if __name__ == "__main__":
    main()
