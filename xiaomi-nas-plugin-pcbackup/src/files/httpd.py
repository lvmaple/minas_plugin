#!/usr/bin/env python3
"""pcbackup — PC → NAS 增量备份插件后端

监听 127.0.0.1:9303，配合前端页面（米家 App / 桌面端）完成：
  1. 前端选好 PC 目录后，把文件清单（路径/大小/修改时间）发来，后端与清单库比对，
     生成增量计划：上传列表 / 跳过数量 / 需要标记删除的列表
  2. 前端按分块 PUT 上传文件内容（支持断点续传 .part 文件）
  3. 上传完成后 commit：原子落盘、保留修改时间、写入清单
  4. PC 上已删除的文件：NAS 端把备份文件改名为 <原名>.del（墓碑标记，不丢数据）

判定"是否变更"：大小 + 修改时间（毫秒），与 rsync 默认快速判定一致。
"""
import fnmatch
import json
import os
import re
import stat
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

try:
    import pwd  # 仅 NAS(Linux) 上可用
except ImportError:
    pwd = None

PORT = int(os.environ.get("PLUG_PORT", "9303"))
_FILES_DIR = os.path.dirname(os.path.abspath(__file__))          # <plugin>/src/files
PLUGIN_ROOT = os.path.dirname(os.path.dirname(_FILES_DIR))       # <plugin>/
DATA_DIR = os.environ.get("PCBACKUP_DATA") or os.path.join(PLUGIN_ROOT, "etc")
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
RESUME_FILENAME = "pending-run.json"

PLUG_USER = os.environ.get("PLUG_USER", "")
# 用户在 App「我的数据」里对应的根目录（任务只需填这下面的子目录）
USER_DATA_ROOT = os.environ.get("PCBACKUP_USER_DATA_ROOT") or (
    "/nas/pool0/%s/data" % PLUG_USER if PLUG_USER else "/nas/pool0/pcbackup")
DEL_SUFFIX = ".del"
PART_SUFFIX = ".pcbp.part"
JSON_BODY_MAX = 128 * 1024 * 1024        # scan 请求可能携带几十万文件元数据
CHUNK_BODY_MAX = 512 * 1024 * 1024       # 单块上限（前端默认 8MiB）
HISTORY_KEEP = 100
TOMBSTONE_SOFT_LIMIT = 10                # 删除数 ≥ 此值且占比 >20% 时需要 force 确认
TOMBSTONE_RATIO = 0.2
ADOPT_TOLERANCE_MS = 2000                # 认领存量文件时的 mtime 容差（毫秒）
DEFAULT_EXCLUDES = (
    "Thumbs.db", "desktop.ini", ".DS_Store", "~$*",
    "node_modules/", ".pnpm-store/", ".venv/", "venv/", ".gradle/",
    "__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
    ".tox/", ".nox/", ".next/", ".nuxt/", ".turbo/",
    "coverage/", "htmlcov/", "test-results/", "playwright-report/",
    "*.pyc", "*.pyo", ".coverage", ".coverage.*", "*.tsbuildinfo",
)

# 备份目标目录只允许落在这些前缀下（插件以 root 运行，必须防误写系统目录）
DEST_ALLOWED_PREFIXES = ("/nas/pool0/", "/home/", "/tmp/", "/mnt/", "/media/", "/srv/")

_JOBS_LOCK = threading.RLock()
_JOB_LOCKS = {}
_UPLOAD_LOCKS = {}


def _job_lock(jid):
    with _JOBS_LOCK:
        return _JOB_LOCKS.setdefault(jid, threading.RLock())


def _upload_lock(key):
    with _JOBS_LOCK:
        return _UPLOAD_LOCKS.setdefault(key, threading.RLock())


# ── 路径安全（核心！插件以 root 运行） ──────────────────────

_SEG_RE = re.compile(r"^[^/\\\x00-\x1f]+$")


def validate_rel(rel):
    """校验前端传来的相对路径：禁止绝对路径、..、反斜杠、控制字符。"""
    if not isinstance(rel, str) or not rel or len(rel) > 1024:
        return False
    if rel.startswith("/") or "\\" in rel:
        return False
    for seg in rel.split("/"):
        if seg in ("", ".", "..") or len(seg) > 255:
            return False
        if not _SEG_RE.match(seg):
            return False
    return True


def safe_join(root, rel):
    """root + rel 拼接，带穿越与符号链接逃逸防护。非法返回 None。"""
    if not validate_rel(rel):
        return None
    root = os.path.realpath(root)
    cur = root
    for seg in rel.split("/"):
        cur = os.path.join(cur, seg)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            return None  # 任何已存在组件是软链 → 拒绝，防止逃逸
    final = os.path.normpath(os.path.join(root, rel))
    if final != root and not final.startswith(root + os.sep):
        return None
    return final


def _canonical_dest(dest):
    """Return a safe destination path, or None if it leaves an allowed data root."""
    if not isinstance(dest, str) or not dest:
        return None
    if os.name == "nt":
        # 仅用于在 Windows 上本地跑测试：允许盘符绝对路径（非盘根）
        return dest if re.match(r"^[A-Za-z]:[\\/].+", dest) else None
    if not os.path.isabs(dest):
        return None
    resolved = os.path.realpath(dest)
    for prefix in DEST_ALLOWED_PREFIXES:
        root = os.path.realpath(prefix.rstrip("/"))
        if root == os.path.sep:
            continue
        try:
            if resolved != root and os.path.commonpath((resolved, root)) == root:
                return resolved
        except ValueError:
            continue
    return None


def validate_dest(dest):
    return _canonical_dest(dest) is not None


def _try_chown(path):
    """服务以 root 运行时，把产物归属还给插件用户（SMB 访问需要）。"""
    if pwd is None or not PLUG_USER:
        return
    try:
        if os.geteuid() == 0:
            pw = pwd.getpwnam(PLUG_USER)
            os.chown(path, pw.pw_uid, pw.pw_gid)
    except Exception:
        pass


# ── 持久化 ──────────────────────────────────────────────────

def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_jobs():
    return _load_json(JOBS_FILE, {})


def save_jobs(jobs):
    _save_json(JOBS_FILE, jobs)


def job_dir(jid):
    return os.path.join(JOBS_DIR, jid)


MIRROR_DIRNAME = ".pcbackup"     # 清单镜像目录（放在备份数据根下，防固件升级清插件目录）
RUNS_DIRNAME = "runs"            # 追加式运行日志：一次任务执行一个文件，只追加不重写
RUN_KEEP_DONE = 5                # 压实后保留最近 N 段已折叠日志作审计

_MANIFEST_CACHE = {}             # jid -> {"m": manifest}（内存态 = 主档+运行日志折叠结果）
_RUN_CURRENT = {}                # jid -> 当前运行日志文件名（压实后重置，下次写入新建）
_MANIFEST_PATH_CACHE = {}        # jid -> (etc_path, mirror_path) 只算一次


def manifest_paths(job):
    key = job["id"]
    c = _MANIFEST_PATH_CACHE.get(key)
    if c is None:
        etc_path = os.path.join(job_dir(key), "manifest.json")
        mirror_path = safe_join(job["destination"], MIRROR_DIRNAME + "/manifest.json")
        c = (etc_path, mirror_path)
        _MANIFEST_PATH_CACHE[key] = c
    return c


def runs_dir(job):
    return os.path.join(job_dir(job["id"]), RUNS_DIRNAME)


def _run_list_all(job):
    try:
        return os.listdir(runs_dir(job))
    except FileNotFoundError:
        return []


def _run_names(job):
    """全部运行日志（含已折叠的 .done），按时间序。
    折叠是幂等的，全量重放可最大化恢复：主档/镜像全丢时账本仍能从日志重建。"""
    names = [n for n in _run_list_all(job)
             if n.endswith(".jsonl") or n.endswith(".done")]
    return sorted(names)


def _run_append(job, lines):
    """追加式写运行日志：每行一个 JSON 操作，O(1) 落盘，绝不全量重写账本。
    必须在 _job_lock 内调用。丢几行无害：文件本体已落盘，认领机制自愈。"""
    if not lines:
        return
    name = _RUN_CURRENT.get(job["id"])
    if name is None:
        name = "run-%d.jsonl" % time.time_ns()
        _RUN_CURRENT[job["id"]] = name
    d = runs_dir(job)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "a", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False, separators=(",", ":")) + "\n")


def _apply_line(m, ln):
    """把一行日志操作应用到内存态。写侧（manifest_* 助手）与放侧（_fold）共用，
    操作语义单点维护。行格式：
    {"op":"up","p":rel,"s","m","t"[,"adopted"]} / {"op":"del","p"}
    {"op":"excl","p"} / {"op":"excl_clear"} / {"op":"meta","folder"}"""
    op = ln.get("op")
    if op == "meta":
        m["folder"] = ln.get("folder", m.get("folder", ""))
        return
    if op == "excl_clear":
        m["excluded"] = []
        return
    p = ln.get("p", "")
    if not p:
        return
    files = m["files"]
    excl = m["excluded"]
    if op == "up":
        ent = {"s": ln.get("s", 0), "m": ln.get("m", 0), "t": ln.get("t", 0)}
        if ln.get("adopted"):
            ent["adopted"] = True
        files[p] = ent
        if p in excl:
            excl.remove(p)
    elif op == "del":
        files.pop(p, None)
    elif op == "excl":
        files.pop(p, None)
        if p not in excl:
            excl.append(p)


def _fold(m, path):
    """把一段运行日志叠到内存态 m 上（幂等：后写覆盖先写；容忍半行/坏行）。"""
    try:
        f = open(path, "r", encoding="utf-8")
    except OSError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ln = json.loads(line)
            except ValueError:
                continue
            _apply_line(m, ln)


def _manifest_write_disk(job, m):
    """紧凑格式全量落盘：dump 一次、主档与镜像各写一份。仅压实/重置时调用。"""
    text = json.dumps(m, ensure_ascii=False, separators=(",", ":"))
    etc_path, mirror_path = manifest_paths(job)
    os.makedirs(job_dir(job["id"]), exist_ok=True)
    tmp = etc_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, etc_path)
    if mirror_path:
        try:
            os.makedirs(os.path.dirname(mirror_path), exist_ok=True)
            tmp2 = mirror_path + ".tmp"
            with open(tmp2, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp2, mirror_path)
            _try_chown(os.path.dirname(mirror_path))
        except OSError:
            pass


def load_manifest(job):
    """账本 = 主档（上次压实结果）+ 未折叠运行日志，折叠为内存 dict。
    返回缓存里的同一个 dict；改动请走 manifest_* 助手（追加日志行）。"""
    with _job_lock(job["id"]):
        c = _MANIFEST_CACHE.get(job["id"])
        if c is not None:
            return c["m"]
        etc_path, mirror_path = manifest_paths(job)
        m = _load_json(etc_path, None)
        # 带 folder/excluded 的空账本是合法状态（目录切换 reset 的结果），必须信任主档；
        # 只有主档缺失/损坏/真全空（folder、excluded 也空）才回退镜像，
        # 否则陈旧镜像会复活旧目录条目或丢掉 excluded 列表
        if (m is None or (not m.get("files") and not m.get("folder")
                          and not m.get("excluded"))) and mirror_path:
            m2 = _load_json(mirror_path, None)
            if m2 and m2.get("files"):
                m = m2
        if m is None:
            m = {"version": 1, "folder": "", "files": {}}
        m.setdefault("files", {})
        m.setdefault("folder", "")
        if not isinstance(m.get("excluded"), list):
            m["excluded"] = []
        for name in _run_names(job):
            _fold(m, os.path.join(runs_dir(job), name))
        _MANIFEST_CACHE[job["id"]] = {"m": m}
        return m


def manifest_upsert(job, entries):
    """批量入账 [(rel, entry)]：追加 up 行（含从 excluded 认回）。"""
    with _job_lock(job["id"]):
        m = load_manifest(job)
        lines = []
        for rel, ent in entries:
            ln = {"op": "up", "p": rel,
                  "s": ent.get("s", 0), "m": ent.get("m", 0), "t": ent.get("t", 0)}
            if ent.get("adopted"):
                ln["adopted"] = True
            _apply_line(m, ln)
            lines.append(ln)
        _run_append(job, lines)


def manifest_del(job, rels):
    """删除记账（墓碑行）：从 files 移除。"""
    with _job_lock(job["id"]):
        m = load_manifest(job)
        lines = []
        for rel in rels:
            ln = {"op": "del", "p": rel}
            _apply_line(m, ln)
            lines.append(ln)
        _run_append(job, lines)


def manifest_exclude(job, rels):
    """排除规则命中：从 files 移到 excluded（盘上文件保留，可一键彻底删除）。"""
    with _job_lock(job["id"]):
        m = load_manifest(job)
        lines = []
        for rel in rels:
            ln = {"op": "excl", "p": rel}
            _apply_line(m, ln)
            lines.append(ln)
        _run_append(job, lines)


def manifest_excl_clear(job):
    """彻底删除完成：清空 excluded 列表。"""
    with _job_lock(job["id"]):
        m = load_manifest(job)
        ln = {"op": "excl_clear"}
        _apply_line(m, ln)
        _run_append(job, [ln])


def manifest_meta(job, folder):
    """绑定备份目录名（meta 行，折叠时保持一致）。"""
    with _job_lock(job["id"]):
        m = load_manifest(job)
        if m.get("folder") != folder:
            ln = {"op": "meta", "folder": folder}
            _apply_line(m, ln)
            _run_append(job, [ln])


def manifest_reset(job, folder):
    """目录切换：清空账本、作废全部运行日志，从零重建。返回新账本。
    顺序约束：先作废日志、再原子写主档，最后才换内存缓存——中途失败时旧账本仍在盘上，
    残存日志也不会被重放到错误的主档上。"""
    with _job_lock(job["id"]):
        m = {"version": 1, "folder": folder, "files": {}, "excluded": []}
        _RUN_CURRENT.pop(job["id"], None)
        for name in _run_list_all(job):
            p = os.path.join(runs_dir(job), name)
            try:
                os.remove(p)
            except OSError:
                try:
                    os.rename(p, p + ".discarded")   # 删不掉就移出折叠范围，防重放
                except OSError:
                    pass
        _manifest_write_disk(job, m)   # 失败则抛出：内存缓存未动，状态保持一致
        _MANIFEST_CACHE[job["id"]] = {"m": m}
        return m


def compact_manifest(job):
    """使用前压实（合并点定在比对前）：把折叠后的内存态写成紧凑主档+镜像，
    并把已折叠的运行日志转 .done（保留最近 RUN_KEEP_DONE 段作审计）。
    临时文件+原子替换成功才改名日志；中途崩溃可安全重入（折叠幂等）。"""
    with _job_lock(job["id"]):
        m = load_manifest(job)
        _manifest_write_disk(job, m)
        _RUN_CURRENT.pop(job["id"], None)
        d = runs_dir(job)
        try:
            names = os.listdir(d)
        except FileNotFoundError:
            return
        for name in names:
            if name.endswith(".jsonl"):
                try:
                    os.rename(os.path.join(d, name), os.path.join(d, name[:-6] + ".done"))
                except OSError:
                    pass
        dones = sorted(n for n in os.listdir(d) if n.endswith(".done"))
        for name in dones[:-RUN_KEEP_DONE]:
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass


def load_history():
    return _load_json(HISTORY_FILE, [])


def append_history(entry):
    h = load_history()
    h.append(entry)
    _save_json(HISTORY_FILE, h[-HISTORY_KEEP:])


def resume_path(job):
    return os.path.join(job_dir(job["id"]), RESUME_FILENAME)


def load_resume(job):
    record = _load_json(resume_path(job), None)
    return record if isinstance(record, dict) and record.get("version") == 1 else None


def resume_status(job):
    """Only inspect files in the saved upload plan; never rescan the PC tree."""
    with _job_lock(job["id"]):
        record = load_resume(job)
        if record is None:
            return 200, None
        if (record.get("destination") != job["destination"]
                or record.get("exclude") != job.get("exclude", [])):
            return 409, {"reason": "resume_stale", "message": "任务目标或排除规则已变化，请重新扫描"}
        snapshot = load_manifest(job)
        if snapshot.get("folder") != record.get("folder"):
            return 409, {"reason": "resume_stale", "message": "任务已切换电脑目录，请重新扫描"}
        manifest = snapshot["files"]
        pending, completed, completed_bytes = [], 0, 0
        for rel, size, mtime in record["files"]:
            entry = manifest.get(rel)
            matches = bool(entry and entry.get("s") == size
                           and abs(entry.get("m", 0) - mtime) <= 1)
            if matches:
                destf = safe_join(job["destination"], rel)
                try:
                    matches = bool(destf and os.path.isfile(destf)
                                   and os.path.getsize(destf) == size)
                except OSError:
                    matches = False
            if matches:
                completed += 1
                completed_bytes += size
            else:
                pending.append([rel, size, mtime])
        deleted = record["deleted"]
        pending_deleted = [rel for rel in deleted if rel in manifest]
        plan = {
            "folder": record["folder"], "total_files": record["total_files"],
            "upload": [[rel, size] for rel, size, _ in pending],
            "upload_bytes": sum(size for _, size, _ in pending),
            "skipped": record["skipped"], "repaired": record["repaired"],
            "adopted": record["adopted"], "deleted": pending_deleted,
            "excluded_kept": record["excluded_kept"],
            "previous_uploaded": completed, "previous_bytes": completed_bytes,
            "previous_deleted": len(deleted) - len(pending_deleted),
        }
        return 200, {
            "run_id": record["run_id"], "source_path": record["source_path"],
            "started": record["started"], "del_mode": record["del_mode"],
            "prune_excluded": record["prune_excluded"], "files": pending,
            "plan": plan,
        }


def start_resume(job, body):
    source_path = _clean_pc_path(body.get("source_path"))
    if not source_path or not re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", source_path):
        return 400, "续传需要有效的 PC 绝对路径"
    with _job_lock(job["id"]):
        st = _SCAN_STATE.get(job["id"])
        if (not st or st.get("phase") != "done"
                or body.get("scan_id") != st.get("scan_id")):
            return 409, "增量计划已失效，请重新扫描"
        if (st.get("destination") != job["destination"]
                or st.get("exclude") != job.get("exclude", [])):
            return 409, "任务目标或排除规则已变化，请重新扫描"
        existing = load_resume(job)
        if existing:
            if (existing.get("scan_id") == st["scan_id"]
                    and existing.get("source_path") == source_path
                    and existing.get("del_mode") == ("delete" if body.get("del_mode") == "delete" else "del")
                    and existing.get("prune_excluded") == bool(body.get("prune_excluded"))):
                return resume_status(job)
            if not body.get("replace"):
                return 409, "已有未完成备份，请先选择继续或重新扫描"
        plan = st["plan"]
        incoming = st["incoming"]
        record = {
            "version": 1, "run_id": uuid.uuid4().hex, "scan_id": st["scan_id"],
            "started": int(time.time()),
            "source_path": source_path, "destination": job["destination"],
            "exclude": job.get("exclude", []), "folder": plan["folder"],
            "total_files": plan["total_files"],
            "files": [[rel, size, incoming[rel][1]] for rel, size in plan["upload"]],
            "deleted": plan["deleted"], "skipped": plan["skipped"],
            "repaired": plan["repaired"], "adopted": plan["adopted"],
            "excluded_kept": plan["excluded_kept"],
            "del_mode": "delete" if body.get("del_mode") == "delete" else "del",
            "prune_excluded": bool(body.get("prune_excluded")),
        }
        _save_json(resume_path(job), record)
        return resume_status(job)


def finish_run(job, stats):
    """Keep an unfinished checkpoint; remove it only after every planned item settles."""
    with _job_lock(job["id"]):
        run_id = stats.get("run_id")
        record = load_resume(job) if run_id else None
        if run_id:
            previous = next((h for h in reversed(load_history())
                             if h.get("run_id") == run_id and h.get("status") == "ok"), None)
            if previous:
                if record and record.get("run_id") == run_id:
                    os.remove(resume_path(job))
                return 200, previous
            if not record or record.get("run_id") != run_id:
                return 409, "续传记录已变化，请刷新任务"
            code, status = resume_status(job)
            if code != 200:
                return code, status
            remaining = bool(status["plan"]["upload"] or status["plan"]["deleted"])
        else:
            remaining = False
        cancelled = bool(stats.get("cancelled"))
        failed = int(stats.get("failed", 0))
        complete = not (remaining or cancelled or failed)
        pruned = 0
        if stats.get("prune_excluded") and (not run_id or complete):
            code, result = prune_excluded(job)
            pruned = result.get("removed", 0) if code == 200 else 0
        entry = {
            "job_id": job["id"], "job_name": job["name"],
            "ts": int(time.time()), "run_id": run_id,
            "uploaded": int(stats.get("uploaded", 0)),
            "uploaded_bytes": int(stats.get("uploaded_bytes", 0)),
            "skipped": int(stats.get("skipped", 0)),
            "adopted": int(stats.get("adopted", 0)),
            "deleted": int(stats.get("deleted", 0)),
            "failed": failed, "elapsed_ms": int(stats.get("elapsed_ms", 0)),
            "cancelled": cancelled,
            "status": "cancelled" if cancelled else ("ok" if complete else "partial"),
            "pruned_excluded": pruned,
        }
        append_history(entry)
        with _JOBS_LOCK:
            jobs = load_jobs()
            if job["id"] in jobs:
                jobs[job["id"]]["last_run"] = entry["ts"]
                jobs[job["id"]]["last_stats"] = entry
                save_jobs(jobs)
        if run_id and complete:
            os.remove(resume_path(job))
        return 200, entry


# ── 备份逻辑 ────────────────────────────────────────────────

def _disk_snapshot(dest_root, progress=None):
    """一次遍历备份目录，返回 {相对路径: (大小, mtime毫秒)}。
    扫描比对用：避免对每个文件做 realpath + 逐级 lstat + isfile 的系统调用风暴。
    跳过清单镜像目录与传输中的 .part 文件；不跟随符号链接。
    progress 传入 dict 时实时更新 seen 计数（供前端轮询进度）。"""
    root = os.path.realpath(dest_root)
    snap = {}
    seen = 0
    if not os.path.isdir(root):
        return snap
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != MIRROR_DIRNAME]
        relbase = os.path.relpath(dirpath, root)
        for name in filenames:
            if name.endswith(PART_SUFFIX):
                continue
            rel = name if relbase == "." else relbase.replace(os.sep, "/") + "/" + name
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            snap[rel] = (st.st_size, int(st.st_mtime * 1000))
            seen += 1
            if progress is not None and (seen & 16383) == 0:
                progress["seen"] = seen
    if progress is not None:
        progress["seen"] = seen
    return snap


def _match_excludes(rel, patterns):
    """rel 路径是否命中排除规则（与前端 isExcluded 同语义）。"""
    segs = rel.split("/")
    base = segs[-1]
    dirs = segs[:-1]
    for p in patterns:
        p = (p or "").strip()
        if not p:
            continue
        if p.endswith("/"):
            if p[:-1] in dirs:
                return True
        elif fnmatch.fnmatch(base, p) or fnmatch.fnmatch(rel, p):
            return True
    return False


# ── 异步扫描（大目录比对可能耗时几十秒，必须可轮询进度） ──

_SCAN_STATE = {}      # jid -> {"phase","seen","total","plan","error","started"}


def build_plan_gates(job, body):
    """同步段：解析上报清单 + folder/ratio 门禁（不碰磁盘，秒回）。
    返回 (incoming, manifest, err, folder, prune)，err 为 (code, data) 或 None。"""
    folder = body.get("folder", "")
    files = body.get("files", [])
    confirm = bool(body.get("confirm", False))
    force = bool(body.get("force", False))
    prune = bool(body.get("prune_excluded", False))
    if not isinstance(files, list) or not files:
        return None, None, (400, "文件清单为空（请确认选择了有效目录）"), folder, False

    incoming = {}
    for f in files:
        rel = f.get("p", "")
        if not validate_rel(rel):
            return None, None, (400, "包含非法路径: %r" % rel[:120]), folder, False
        try:
            incoming[rel] = (int(f["s"]), int(f["m"]))
        except (KeyError, TypeError, ValueError):
            return None, None, (400, "文件元数据不完整: %r" % rel[:120]), folder, False

    manifest = load_manifest(job)
    prev_folder = manifest.get("folder", "")
    folder_switch = bool(prev_folder) and prev_folder != folder
    if folder_switch and not confirm:
        return None, None, (409, {
            "reason": "folder_mismatch",
            "message": "上次备份的目录是「%s」，本次是「%s」，确认切换吗？"
                       "切换后将从零重建清单（旧备份文件保留，不会被标记删除）" % (prev_folder, folder),
        }), folder, False
    if folder_switch:
        manifest = manifest_reset(job, folder)

    # 排除规则（新增/修改）会覆盖已备份的文件：这些条目从清单中剔除，
    # 不算"已删除"、不做 .del 标记，盘上已备份的文件原样保留
    excl = job.get("exclude") or []
    if excl and manifest["files"]:
        dropped = [rel for rel in list(manifest["files"]) if _match_excludes(rel, excl)]
        if dropped:
            # 记录被排除的路径：盘上文件保留，用户可在界面上一键彻底删除
            manifest_exclude(job, dropped)

    deleted = sorted(rel for rel in manifest["files"] if rel not in incoming)
    mtotal = len(manifest["files"])
    if (deleted and not force and len(deleted) >= TOMBSTONE_SOFT_LIMIT
            and len(deleted) > TOMBSTONE_RATIO * max(mtotal, 1)):
        return None, None, (409, {
            "reason": "deleted_ratio",
            "message": "本次将标记删除 %d / %d 个文件（超过 20%%）。若确认目录选择无误，请勾选强制执行。" % (len(deleted), mtotal),
        }), folder, False
    return incoming, manifest, None, folder, prune


def _scan_worker(job, folder, incoming, manifest, prune=False):
    st = _SCAN_STATE[job["id"]]
    try:
        st["phase"] = "snapshot"
        compact_manifest(job)   # 使用前压实：合并点在比对前（崩溃/关窗口留下的日志在此收编）
        snapshot = _disk_snapshot(job["destination"], st)
        st["phase"] = "matching"

        upload, skipped, repaired, adopted = [], 0, 0, 0
        adopted_entries = []
        mfiles = manifest["files"]
        for rel, (s, m) in incoming.items():
            me = mfiles.get(rel)
            disk = snapshot.get(rel)
            # mtime 允许 ±1ms：Windows/浏览器/Node 对同一时间戳的毫秒表示
            # 会差 1（截断 vs 四舍五入 vs 浮点噪声），严格相等会把约 1/4
            # 文件误判为变更（踩过：12711 个假变更 + 单文件永久误判）
            unchanged = bool(me) and me.get("s") == s and abs(me.get("m", 0) - m) <= 1
            if unchanged and disk is not None:
                skipped += 1
            elif (not me) and disk is not None:
                # 认领：NAS 上已有同名且大小+修改时间一致的文件（别的途径同步来的），
                # 直接登记进清单库，不再重传
                ds, dm = disk
                if ds == s and abs(dm - m) <= ADOPT_TOLERANCE_MS:
                    adopted += 1
                    ent = {"s": s, "m": m, "t": int(time.time()), "adopted": True}
                    mfiles[rel] = ent
                    adopted_entries.append((rel, ent))
                else:
                    upload.append([rel, s])
            else:
                if unchanged and disk is None:
                    repaired += 1  # 清单说有、盘上没有（被手动删/损坏）→ 补传
                upload.append([rel, s])

        upload.sort(key=lambda u: u[0])   # u = [rel, size]，按路径排序
        st["phase"] = "saving"
        if adopted_entries:
            manifest_upsert(job, adopted_entries)   # 批量追加运行日志行
        manifest_meta(job, folder)
        st["plan"] = {
            "scan_id": st["scan_id"],
            "folder": folder,
            "total_files": len(incoming),
            "upload": upload,           # 紧凑二元组 [rel, size]，响应体积减半
            "upload_bytes": sum(u[1] for u in upload),
            "skipped": skipped,
            "repaired": repaired,
            "adopted": adopted,
            "deleted": sorted(rel for rel in manifest["files"] if rel not in incoming),
            "excluded_kept": len(manifest.get("excluded") or []),
            "prune_excluded": prune,
        }
        st["phase"] = "done"
    except Exception as e:
        st["phase"] = "error"
        st["error"] = "%s: %s" % (type(e).__name__, e)[:300]


def build_plan(job, body):
    """同步入口：门禁通过后启动后台比对线程。
    busy 检查必须先于门禁的破坏性副作用（reset/排除剔除），
    否则并发扫描会先清账本再吃 409。"""
    with _job_lock(job["id"]):
        cur = _SCAN_STATE.get(job["id"])
        if cur and cur.get("phase") not in ("done", "error"):
            return 409, {"reason": "scan_busy", "message": "该任务正在比对中，请稍候"}
        old_run = load_resume(job)
        if old_run and not body.get("replace_resume"):
            return 409, {"reason": "resume_pending", "message": "有未完成备份，请选择继续或重新扫描"}
        incoming, manifest, err, folder, prune = build_plan_gates(job, body)
        if err is not None:
            return err
        if old_run:
            os.remove(resume_path(job))
        _SCAN_STATE[job["id"]] = {
            "phase": "queued", "seen": 0, "total": len(incoming),
            "plan": None, "error": None, "started": time.time(),
            "scan_id": uuid.uuid4().hex, "incoming": incoming,
            "destination": job["destination"], "exclude": list(job.get("exclude", [])),
        }
        threading.Thread(target=_scan_worker, args=(job, folder, incoming, manifest, prune), daemon=True).start()
    return 200, {"started": True, "total": len(incoming)}


def put_chunk(job, rel, offset, body):
    """分块写入 .part 文件（幂等：offset <= 当前大小时先截断回 offset 再写，
    网络中断重试同一块不会错位；offset > 当前大小说明客户端状态过期，拒绝）。"""
    destf = safe_join(job["destination"], rel)
    if destf is None:
        return 400, "非法路径"
    part = destf + PART_SUFFIX
    with _upload_lock(part):
        parent = os.path.dirname(destf)
        if not os.path.isdir(parent):
            _mkdir_chowned(parent)
        cur = os.path.getsize(part) if os.path.isfile(part) else 0
        if offset > cur:
            return 409, {"reason": "offset_mismatch", "part_size": cur}
        mode = "r+b" if os.path.exists(part) else "wb"
        with open(part, mode) as f:
            f.truncate(offset)
            f.seek(offset)
            f.write(body)
    return 200, {"path": rel, "part_size": offset + len(body)}


def part_size(job, rel):
    destf = safe_join(job["destination"], rel)
    if destf is None:
        return 400, "非法路径"
    part = destf + PART_SUFFIX
    return 200, {"size": os.path.getsize(part) if os.path.isfile(part) else 0}


def commit_file(job, rel, size, mtime):
    """.part → 正式文件：原子替换、保留修改时间、记清单。"""
    destf = safe_join(job["destination"], rel)
    if destf is None:
        return 400, "非法路径"
    part = destf + PART_SUFFIX
    with _upload_lock(part):
        if not os.path.isfile(part):
            return 400, "分块数据不存在，请重新上传该文件"
        actual = os.path.getsize(part)
        if size is not None and actual != size:
            return 409, {"reason": "size_mismatch", "part_size": actual}
        with open(part, "r+b") as f:
            os.fsync(f.fileno())
        os.replace(part, destf)
        if mtime:
            try:
                os.utime(destf, (mtime / 1000.0, mtime / 1000.0))
            except (OSError, ValueError):
                pass
        _try_chown(destf)
        manifest_upsert(job, [(rel, {"s": size if size is not None else actual,
                                     "m": mtime if mtime else int(time.time() * 1000),
                                     "t": int(time.time())})])
    return 200, {"path": rel, "size": actual}


def tombstone(job, paths, mode="del"):
    """处理 PC 端已删除的文件。
    mode="del"（默认）：改名为 <原名>.del，数据保留；已有 .del 则覆盖为最新版本。
    mode="delete"：直接删除 NAS 上的备份文件（不可恢复）。"""
    results = []
    renamed = []
    popped = []
    seen = set()
    with _job_lock(job["id"]):
        m = load_manifest(job)
        for rel in paths:
            if not validate_rel(rel):
                results.append({"p": rel, "status": "invalid"})
                continue
            if rel in seen or rel not in m["files"]:
                results.append({"p": rel, "status": "ignored"})
                continue
            seen.add(rel)
            destf = safe_join(job["destination"], rel)
            if destf and os.path.isfile(destf):
                if mode == "delete":
                    os.remove(destf)
                    results.append({"p": rel, "status": "deleted"})
                else:
                    os.replace(destf, destf + DEL_SUFFIX)
                    results.append({"p": rel, "status": "renamed"})
                renamed.append(rel)
            elif destf and os.path.isfile(destf + DEL_SUFFIX):
                results.append({"p": rel, "status": "already"})
            else:
                results.append({"p": rel, "status": "missing"})
            popped.append(rel)
        if popped:
            manifest_del(job, popped)   # 记账出口（墓碑行）
    # 只清理受影响路径上变空的目录（不再全树遍历）
    _prune_empty_dirs(job["destination"], renamed)
    return 200, results


def prune_excluded(job):
    """彻底删除因排除规则移出管理的备份文件（用户在界面显式触发，不可恢复）。"""
    with _job_lock(job["id"]):
        m = load_manifest(job)
        paths = list(m.get("excluded") or [])
        removed = 0
        for rel in paths:
            destf = safe_join(job["destination"], rel)
            if destf and os.path.isfile(destf):
                try:
                    os.remove(destf)
                    removed += 1
                except OSError:
                    pass
        manifest_excl_clear(job)
    _prune_empty_dirs(job["destination"], paths)
    return 200, {"removed": removed, "total": len(paths)}


def _prune_empty_dirs(dest_root, rel_paths):
    """删除 rel_paths 各级父目录中已变空的目录（深度优先，不触碰根目录）。"""
    root = os.path.realpath(dest_root)
    if not os.path.isdir(root) or not rel_paths:
        return
    dirs = set()
    for rel in rel_paths:
        d = os.path.dirname(rel)
        while d:
            dirs.add(d)
            d = os.path.dirname(d)
    for d in sorted(dirs, key=len, reverse=True):
        p = os.path.join(root, d.replace("/", os.sep))
        try:
            if os.path.isdir(p) and not os.listdir(p):
                os.rmdir(p)
        except OSError:
            pass


def browse(job, sub):
    base = os.path.realpath(job["destination"]) if not sub else safe_join(job["destination"], sub)
    if base is None or not os.path.isdir(base):
        return 400, "目录不存在"
    entries = []
    with os.scandir(base) as it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
                entries.append({
                    "name": e.name,
                    "dir": e.is_dir(follow_symlinks=False),
                    "size": 0 if e.is_dir(follow_symlinks=False) else st.st_size,
                    "mtime": int(st.st_mtime),
                    "del": e.name.endswith(DEL_SUFFIX),
                })
            except OSError:
                continue
    entries = [e for e in entries if e["name"] != MIRROR_DIRNAME and not e["name"].endswith(PART_SUFFIX)]
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return 200, entries


def _resolve_dest(body):
    """任务目标目录：优先用「我的数据」下的相对子目录（dest_sub），
    兼容旧版绝对路径 destination。返回 (绝对路径, 子目录或空串, 错误信息)。"""
    dest_sub = str(body.get("dest_sub", "")).strip().replace("\\", "/").strip("/")
    if dest_sub:
        if not validate_rel(dest_sub):
            return None, "", "子目录路径不合法（不能含 .. 等）"
        dest = USER_DATA_ROOT.rstrip("/") + "/" + dest_sub
        resolved = _canonical_dest(dest)
        if resolved is None:
            return None, "", "解析出的目录不在允许范围内"
        return resolved, dest_sub, ""
    dest = str(body.get("destination", "")).strip().replace("\\", "/")
    if not dest:
        return None, "", "请填写「我的数据」下的子目录"
    resolved = _canonical_dest(dest)
    if resolved is None:
        return None, "", "目录必须位于 %s 下" % "、".join(DEST_ALLOWED_PREFIXES)
    return resolved, "", ""


def _mkdir_chowned(dest):
    """创建目录（含父级），并把新建的目录归还给插件用户。"""
    if os.path.isdir(dest):
        return
    missing = []
    p = dest
    while not os.path.exists(p) and p not in ("/",):
        missing.append(p)
        p = os.path.dirname(p)
    os.makedirs(dest, exist_ok=True)
    for d in missing:
        _try_chown(d)


# ── 任务管理 ────────────────────────────────────────────────

def _clean_pc_path(v):
    """PC 侧绝对路径（仅记录给前端做原生读取，服务端绝不触碰）。
    放宽校验：去空白、限长、禁控制字符；空串表示清除。"""
    s = str(v or "").strip()
    if not s:
        return ""
    if len(s) > 1024 or re.search(r"[\x00-\x1f]", s):
        return None
    return s


def create_job(body):
    name = str(body.get("name", "")).strip()
    exclude = body.get("exclude")
    if exclude is None:
        exclude = list(DEFAULT_EXCLUDES)
    if not name:
        return 400, "请填写任务名称"
    dest, dest_sub, err = _resolve_dest(body)
    if err:
        return 400, err
    pc_path = _clean_pc_path(body.get("pc_path"))
    if pc_path is None:
        return 400, "pc_path 非法"
    purge_allowed = not os.path.lexists(dest)
    try:
        _mkdir_chowned(dest)
    except OSError as e:
        return 400, "创建目录失败: %s" % e
    jid = uuid.uuid4().hex[:8]
    with _JOBS_LOCK:
        jobs = load_jobs()
        jobs[jid] = {
            "id": jid, "name": name, "destination": dest, "dest_sub": dest_sub,
            "purge_allowed": purge_allowed,
            "exclude": exclude, "folder": "", "pc_path": pc_path,
            "created": int(time.time()), "last_run": 0, "last_stats": {},
        }
        save_jobs(jobs)
    os.makedirs(job_dir(jid), exist_ok=True)
    _try_chown(job_dir(jid))
    return 200, {"id": jid}


def update_job(jid, body):
    with _JOBS_LOCK:
        jobs = load_jobs()
        if jid not in jobs:
            return 404, "任务不存在"
        job = jobs[jid]
        if "name" in body and str(body["name"]).strip():
            job["name"] = str(body["name"]).strip()
        if "dest_sub" in body or "destination" in body:
            dest, dest_sub, err = _resolve_dest(body)
            if err:
                return 400, err
            if dest != job["destination"]:
                job["purge_allowed"] = not os.path.lexists(dest)
            try:
                _mkdir_chowned(dest)
            except OSError as e:
                return 400, "创建目录失败: %s" % e
            job["destination"] = dest
            job["dest_sub"] = dest_sub
        if "exclude" in body:
            job["exclude"] = [str(x) for x in (body["exclude"] or [])]
        if "pc_path" in body:
            pc_path = _clean_pc_path(body.get("pc_path"))
            if pc_path is None:
                return 400, "pc_path 非法"
            job["pc_path"] = pc_path
        save_jobs(jobs)
    return 200, "ok"


def delete_job(jid, purge):
    with _JOBS_LOCK:
        jobs = load_jobs()
        job = jobs.get(jid)
        if job is None:
            return 404, "任务不存在"
        if purge and not job.get("purge_allowed", False):
            return 400, "该备份目录并非由此任务新建，不能自动删除；任务未删除"
        purge_dest = _canonical_dest(job["destination"]) if purge else None
        if purge and purge_dest is None:
            return 400, "备份目录不在允许范围内，任务未删除"
        if purge:
            for other_id, other in jobs.items():
                if other_id == jid:
                    continue
                other_dest = _canonical_dest(other.get("destination"))
                if other_dest is None:
                    continue
                try:
                    if os.path.commonpath((purge_dest, other_dest)) == purge_dest:
                        return 409, "该目录仍包含其他备份任务，任务未删除"
                except ValueError:
                    continue
        jobs.pop(jid)
        save_jobs(jobs)
    with _job_lock(jid):
        _MANIFEST_CACHE.pop(jid, None)
        _RUN_CURRENT.pop(jid, None)
    import shutil
    shutil.rmtree(job_dir(jid), ignore_errors=True)
    if purge:
        shutil.rmtree(purge_dest, ignore_errors=True)
        return 200, "已删除任务及其备份数据"
    return 200, "已删除任务（备份数据保留在 %s）" % job["destination"]


def job_summary(job):
    m = load_manifest(job)
    files = m.get("files", {})
    total_bytes = sum(v.get("s", 0) for v in files.values())
    return {
        "id": job["id"], "name": job["name"], "destination": job["destination"],
        "dest_sub": job.get("dest_sub", ""),
        "exclude": job.get("exclude", []), "folder": m.get("folder", ""),
        "pc_path": job.get("pc_path", ""),
        "files": len(files), "bytes": total_bytes,
        "last_run": job.get("last_run", 0), "last_stats": job.get("last_stats", {}),
        "resume_available": load_resume(job) is not None,
        "created": job.get("created", 0),
    }


# ── HTTP ────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "pcbackup/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stdout.write("[pcbackup] %s %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()

    # -- 响应工具 --

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data=None):
        self._json({"code": 0, "data": data if data is not None else {}})

    def _err(self, status, message):
        if isinstance(message, dict):
            self._json({"code": status, "message": message.get("message", ""), "data": message}, status)
        else:
            self._json({"code": status, "message": str(message)}, status)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > JSON_BODY_MAX:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > CHUNK_BODY_MAX:
            raise ValueError("请求体过大")
        return self.rfile.read(length) if length else b""

    def _query(self):
        if getattr(self, "_parsed_request_target", None) != self.path:
            parsed = urlparse(self.path)
            self._qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            self._request_path = parsed.path
            self._parsed_request_target = self.path
        return self._qs

    def _path(self):
        self._query()
        return self._request_path

    def _job(self, jid):
        jobs = load_jobs()
        if jid not in jobs:
            self._err(404, "任务不存在")
            return None
        return jobs[jid]

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- 路由 --

    def do_GET(self):
        try:
            path = self._path().rstrip("/")
            q = self._query()
            parts = [unquote(p) for p in path.split("/") if p]

            if path == "/api/health":
                self._ok({"status": "running", "version": "1.2.0", "plugin": "pcbackup"})

            elif path == "/api/config":
                self._ok({"data_root": USER_DATA_ROOT, "del_suffix": DEL_SUFFIX,
                          "version": "1.2.0"})

            elif path == "/api/jobs":
                self._ok([job_summary(j) for j in load_jobs().values()])

            elif len(parts) == 4 and parts[1] == "jobs" and parts[3] == "manifest":
                job = self._job(parts[2]) or None
                if job:
                    self._ok(load_manifest(job))   # 折叠态即最新账本（含未合并运行日志）

            elif len(parts) == 4 and parts[1] == "jobs" and parts[3] == "scan-status":
                job = self._job(parts[2]) or None
                if job:
                    st = _SCAN_STATE.get(job["id"])
                    if not st:
                        self._err(404, "没有进行中的扫描")
                    else:
                        out = {"phase": st.get("phase"), "seen": st.get("seen", 0),
                               "total": st.get("total", 0)}
                        if st.get("phase") == "done":
                            out["plan"] = st.get("plan")
                        if st.get("phase") == "error":
                            out["error"] = st.get("error")
                        self._ok(out)

            elif len(parts) == 4 and parts[1] == "jobs" and parts[3] == "resume":
                job = self._job(parts[2]) or None
                if job:
                    code, data = resume_status(job)
                    (self._ok if code == 200 else lambda d: self._err(code, d))(data)

            elif len(parts) == 4 and parts[1] == "jobs" and parts[3] == "part":
                job = self._job(parts[2]) or None
                if job:
                    if "path" not in q:
                        self._err(400, "缺少 path 参数")
                    else:
                        code, data = part_size(job, q["path"])
                        (self._ok if code == 200 else lambda d: self._err(code, d))(data)

            elif len(parts) == 4 and parts[1] == "jobs" and parts[3] == "history":
                job = self._job(parts[2]) or None
                if job:
                    self._ok([h for h in load_history() if h.get("job_id") == job["id"]][-30:])

            elif len(parts) == 4 and parts[1] == "jobs" and parts[3] == "browse":
                job = self._job(parts[2]) or None
                if job:
                    code, data = browse(job, q.get("sub", ""))
                    (self._ok if code == 200 else lambda d: self._err(code, d))(data)

            elif len(parts) == 4 and parts[1] == "jobs" and parts[3] == "download":
                job = self._job(parts[2]) or None
                if job:
                    self._send_download(job, q.get("path", ""))

            else:
                self._err(404, "not found")
        except BrokenPipeError:
            pass
        except (ValueError, KeyError) as e:
            self._err(400, "请求格式错误: %s" % e)
        except Exception as e:
            self._err(500, "服务器错误: %s" % e)

    def _send_download(self, job, rel):
        destf = safe_join(job["destination"], rel)
        if not destf or not os.path.isfile(destf):
            self._err(404, "文件不存在")
            return
        size = os.path.getsize(destf)
        fname = os.path.basename(destf)
        from urllib.parse import quote
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''%s" % quote(fname))
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(destf, "rb") as f:
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_POST(self):
        try:
            path = self._path().rstrip("/")
            parts = [unquote(p) for p in path.split("/") if p]

            if path == "/api/jobs":
                code, data = create_job(self._read_json())
                (self._ok if code == 200 else lambda d: self._err(code, d))(data)

            elif len(parts) == 4 and parts[1] == "jobs" and parts[3] == "scan":
                job = self._job(parts[2]) or None
                if job:
                    body = self._read_json()
                    code, data = build_plan(job, body)
                    (self._ok if code == 200 else lambda d: self._err(code, d))(data)

            elif len(parts) == 4 and parts[1] == "jobs" and parts[3] == "resume":
                job = self._job(parts[2]) or None
                if job:
                    code, data = start_resume(job, self._read_json())
                    (self._ok if code == 200 else lambda d: self._err(code, d))(data)

            elif len(parts) == 4 and parts[1] == "jobs" and parts[3] == "commit":
                job = self._job(parts[2]) or None
                if job:
                    body = self._read_json()
                    code, data = commit_file(job, body.get("p", ""),
                                             body.get("s"), body.get("m"))
                    (self._ok if code == 200 else lambda d: self._err(code, d))(data)

            elif len(parts) == 4 and parts[1] == "jobs" and parts[3] == "prune-excluded":
                job = self._job(parts[2]) or None
                if job:
                    code, data = prune_excluded(job)
                    (self._ok if code == 200 else lambda d: self._err(code, d))(data)

            elif len(parts) == 4 and parts[1] == "jobs" and parts[3] == "deleted":
                job = self._job(parts[2]) or None
                if job:
                    body = self._read_json()
                    code, data = tombstone(job, body.get("paths", []),
                                           "delete" if body.get("mode") == "delete" else "del")
                    (self._ok if code == 200 else lambda d: self._err(code, d))(data)

            elif len(parts) == 4 and parts[1] == "jobs" and parts[3] == "finish":
                job = self._job(parts[2]) or None
                if job:
                    code, data = finish_run(job, self._read_json())
                    (self._ok if code == 200 else lambda d: self._err(code, d))(data)

            else:
                self._err(404, "not found")
        except (ValueError, KeyError) as e:
            self._err(400, "请求格式错误: %s" % e)
        except Exception as e:
            self._err(500, "服务器错误: %s" % e)

    def do_PUT(self):
        try:
            path = self._path().rstrip("/")
            parts = [unquote(p) for p in path.split("/") if p]
            if len(parts) == 4 and parts[1] == "jobs" and parts[3] == "data":
                job = self._job(parts[2]) or None
                if job:
                    q = self._query()
                    try:
                        offset = int(q.get("offset", "0"))
                    except ValueError:
                        self._err(400, "offset 非法")
                        return
                    body = self._read_body()
                    code, data = put_chunk(job, q.get("path", ""), offset, body)
                    (self._ok if code == 200 else lambda d: self._err(code, d))(data)
            else:
                self._err(404, "not found")
        except (ValueError, KeyError) as e:
            self._err(400, "请求格式错误: %s" % e)
        except Exception as e:
            self._err(500, "服务器错误: %s" % e)

    def do_PATCH(self):
        try:
            path = self._path().rstrip("/")
            parts = [unquote(p) for p in path.split("/") if p]
            if len(parts) == 3 and parts[1] == "jobs":
                code, data = update_job(parts[2], self._read_json())
                (self._ok if code == 200 else lambda d: self._err(code, d))(data)
            else:
                self._err(404, "not found")
        except (ValueError, KeyError) as e:
            self._err(400, "请求格式错误: %s" % e)
        except Exception as e:
            self._err(500, "服务器错误: %s" % e)

    def do_DELETE(self):
        try:
            path = self._path().rstrip("/")
            parts = [unquote(p) for p in path.split("/") if p]
            q = self._query()
            if len(parts) == 3 and parts[1] == "jobs":
                code, data = delete_job(parts[2], purge=q.get("purge") == "1")
                (self._ok if code == 200 else lambda d: self._err(code, d))(data)
            else:
                self._err(404, "not found")
        except (ValueError, KeyError) as e:
            self._err(400, "请求格式错误: %s" % e)
        except Exception as e:
            self._err(500, "服务器错误: %s" % e)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(JOBS_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("[pcbackup] listening on 127.0.0.1:%d  data=%s" % (PORT, DATA_DIR))
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
