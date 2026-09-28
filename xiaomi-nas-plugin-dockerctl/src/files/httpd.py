#!/usr/bin/env python3
"""Docker Console — Xiaomi NAS plugin backend. Full-featured."""

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, urlsplit, parse_qs, unquote

PORT = int(os.environ.get("PLUG_PORT", "9302"))
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
DOCKER_BIN = os.environ.get("DOCKER_BIN", "/data/docker/docker")
SKOPEO_BIN = os.environ.get("SKOPEO_BIN", "skopeo")
TIMEOUT = 12
COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "/data/backup/plugins/dockerctl/compose")


def run_docker(args, timeout=TIMEOUT):
    cmd = [DOCKER_BIN] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except FileNotFoundError:
        return 1, "", f"docker binary not found: {DOCKER_BIN}"
    except Exception as e:
        return 1, "", str(e)


def run_docker_input(args, data, timeout=30):
    """Like run_docker but feeds `data` to stdin (used for `docker login --password-stdin`)."""
    cmd = [DOCKER_BIN] + args
    try:
        r = subprocess.run(cmd, input=data, capture_output=True, text=True, errors="replace", timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except FileNotFoundError:
        return 1, "", f"docker binary not found: {DOCKER_BIN}"
    except Exception as e:
        return 1, "", str(e)


def list_registry_auths():
    """Registry server names the docker engine has credentials for (never the credentials)."""
    cfg_path = os.path.join(os.path.expanduser("~"), ".docker", "config.json")
    names = set()
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        names.update((cfg.get("auths") or {}).keys())
        names.update((cfg.get("credHelpers") or {}).keys())
    except Exception:
        pass
    return sorted(names)


def safe_registry(host):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,253}(:\d{1,5})?", host or ""))


# ---- operation log (ring buffer + on-disk, so failures are never lost) ----
OPS_LOG_PATH = "/tmp/dockerctl/ops.log"
OPS_LOG = {"lock": threading.Lock(), "items": []}


def log_op(kind, target, ok, detail=""):
    entry = {"ts": time.time(), "kind": kind, "target": target,
             "ok": bool(ok), "detail": (detail or "").strip()[-500:]}
    with OPS_LOG["lock"]:
        OPS_LOG["items"] = (OPS_LOG["items"] + [entry])[-200:]
    try:
        os.makedirs(os.path.dirname(OPS_LOG_PATH), exist_ok=True)
        with open(OPS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_ops_log():
    try:
        with open(OPS_LOG_PATH, encoding="utf-8") as f:
            lines = f.readlines()[-200:]
        for ln in lines:
            try:
                OPS_LOG["items"].append(json.loads(ln))
            except Exception:
                pass
    except Exception:
        pass


# Docker's image Created time is the build time, not the local pull time.
PULL_TIMES_PATH = "/data/backup/plugins/dockerctl/pull-times.json"
PULL_TIMES = {"lock": threading.Lock(), "items": {}}


def _image_ref(ref):
    return ref if ":" in ref.rsplit("/", 1)[-1] or "@" in ref else ref + ":latest"


def _save_pull_times():
    os.makedirs(os.path.dirname(PULL_TIMES_PATH), exist_ok=True)
    temp_path = PULL_TIMES_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(PULL_TIMES["items"], f, ensure_ascii=False)
    os.replace(temp_path, PULL_TIMES_PATH)


def _load_pull_times():
    try:
        with open(PULL_TIMES_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            PULL_TIMES["items"] = {k: v for k, v in saved.items()
                                   if isinstance(k, str) and isinstance(v, dict)}
        return
    except (OSError, ValueError):
        pass

    # Recover successful pulls already in this plugin's operation log once.
    recent = {}
    for entry in OPS_LOG["items"]:
        if not isinstance(entry, dict):
            continue
        ref = entry.get("target", "")
        if entry.get("kind") == "拉取镜像" and entry.get("ok"):
            recent[_image_ref(ref)] = entry.get("ts", 0)
        elif entry.get("kind") == "删除镜像" and entry.get("ok"):
            recent.pop(_image_ref(ref), None)
    for ref, pulled_at in recent.items():
        code, out, _ = run_docker(["image", "inspect", "--format", "{{.Id}}", ref], timeout=10)
        if code == 0 and out.strip():
            PULL_TIMES["items"][ref] = {"id": out.strip(), "at": pulled_at}
    if PULL_TIMES["items"]:
        try:
            _save_pull_times()
        except OSError:
            pass


def _record_pull_time(image, tag_as, pulled_at):
    code, out, _ = run_docker(["image", "inspect", "--format", "{{.Id}}", image], timeout=10)
    if code or not out.strip():
        return
    with PULL_TIMES["lock"]:
        for ref in (image, tag_as):
            if ref:
                PULL_TIMES["items"][_image_ref(ref)] = {"id": out.strip(), "at": pulled_at}
        try:
            _save_pull_times()
        except OSError:
            pass


def _forget_pull_time(ref):
    with PULL_TIMES["lock"]:
        if PULL_TIMES["items"].pop(_image_ref(ref), None) is not None:
            try:
                _save_pull_times()
            except OSError:
                pass


def _attach_pull_times(items):
    with PULL_TIMES["lock"]:
        saved = dict(PULL_TIMES["items"])
    for item in items:
        ref = (item.get("Repository") or "") + ":" + (item.get("Tag") or "")
        entry = saved.get(ref) or {}
        at = entry.get("at", 0) if entry.get("id") == item.get("ID") else 0
        item["PulledAt"] = at if isinstance(at, (int, float)) and at > 0 else 0
    return items


# ---- background pull task (async, with rolling progress frames) ----
PULL_TASK = {"lock": threading.Lock(), "task": None}


def _pull_snapshot():
    with PULL_TASK["lock"]:
        t = PULL_TASK["task"]
        if not t:
            return None
        snap = dict(t)
        snap.pop("proc", None)
        snap.pop("helper_container", None)
        snap["frames"] = list(t["frames"])
        return snap


def proxy_error(proxy):
    """Validate a single-use HTTP(S) proxy URL before passing it to skopeo."""
    if not isinstance(proxy, str) or len(proxy) > 2048 or any(c.isspace() or ord(c) < 32 for c in proxy):
        return "代理地址不合法"
    try:
        parsed = urlsplit(proxy)
        port = parsed.port
    except ValueError:
        return "代理地址不合法"
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or
            not port or "@" in parsed.netloc or
            parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        return "请输入不含账号密码的 HTTP(S) 代理地址，如 http://192.168.1.2:7890"
    return ""


def _stream_pull(args, task_id, timeout=1800, binary=None, env=None):
    """Run a pull command with output streamed into the rolling frame buffer."""
    cmd = [binary or DOCKER_BIN] + args
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", bufsize=1, env=env)
    except Exception as e:
        return 1, "", str(e)
    with PULL_TASK["lock"]:
        t = PULL_TASK["task"]
        if t and t["id"] == task_id:
            t["proc"] = p
    collected = []
    deadline = time.time() + timeout
    try:
        for raw in p.stdout:
            if time.time() > deadline:
                p.kill()
                return 1, "\n".join(collected), "timeout"
            for frag in re.split(r"[\r\n]+", raw):
                frag = frag.strip()
                if frag:
                    collected.append(frag)
                    with PULL_TASK["lock"]:
                        t = PULL_TASK["task"]
                        if t and t["id"] == task_id:
                            t["frames"] = (t["frames"] + [frag])[-12:]
                            t["updated"] = time.time()
        p.wait(timeout=30)
    except Exception as e:
        try:
            p.kill()
        except Exception:
            pass
        return 1, "\n".join(collected), str(e)
    return p.returncode, "\n".join(collected), ""


def _run_pull_task(image, tag_as, task_id, proxy="", skopeo_bin=""):
    if proxy:
        env = os.environ.copy()
        env.update({"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
                    "http_proxy": proxy, "https_proxy": proxy,
                    "NO_PROXY": "", "no_proxy": ""})
        helper_container = ("dockerctl-skopeo-" + task_id
                            if os.path.basename(skopeo_bin) == "skopeo-container" else "")
        if helper_container:
            env["DOCKERCTL_CONTAINER_NAME"] = helper_container
        code, out, err = _stream_pull(
            ["copy", "docker://" + image, "docker-daemon:" + image],
            task_id, binary=skopeo_bin, env=env)
        if code and helper_container:
            # The docker CLI can exit before its helper container; stop it too.
            run_docker(["stop", "-t", "1", helper_container], timeout=10)
    else:
        code, out, err = _stream_pull(["pull", image], task_id)
    if code == 0 and tag_as and tag_as != image:
        code2, out2, err2 = run_docker(["tag", image, tag_as], timeout=10)
        out += "\n" + out2
        if code2:
            code, err = code2, (err + err2)
    finalized = False
    if code != 0 and not err:
        # docker 的报错混在输出流末尾（如 "failed to register layer: ..."），取末尾几行
        err = "\n".join([l for l in (out or "").splitlines() if l.strip()][-3:])
    with PULL_TASK["lock"]:
        active = (PULL_TASK["task"] is not None and
                  PULL_TASK["task"]["id"] == task_id and
                  PULL_TASK["task"]["status"] == "running")
    if code == 0 and active:
        _record_pull_time(image, tag_as, time.time())
    with PULL_TASK["lock"]:
        t = PULL_TASK["task"]
        if t and t["id"] == task_id and t["status"] == "running":
            t["status"] = "success" if code == 0 else "error"
            t["error"] = err if code else ""
            t["output"] = out[-4000:]
            t["ended"] = time.time()
            finalized = True
    if finalized:
        log_op("拉取镜像", image, code == 0, err if code else "")


def safe_name(name):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}", name or ""))


def safe_id(v):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-:@/]{0,255}", v or ""))


def safe_addr(v):
    """Loose check for subnet/gateway strings (IPv4/IPv6 CIDR)."""
    return bool(re.fullmatch(r"[0-9A-Fa-f.:/]{3,64}", v or ""))


def _to_port(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        n = v
    elif isinstance(v, str) and v.isdigit():
        n = int(v)
    else:
        return None
    return n if 1 <= n <= 65535 else None


def build_run_args(body):
    """Build argv for `docker run` from a deploy form payload.

    Returns (args, error_message); args is None on error.
    """
    image = (body.get("image") or "").strip()
    if not image:
        return None, "镜像名不能为空"
    if not safe_id(image):
        return None, "镜像名不合法"

    name = (body.get("name") or "").strip()
    if name and not safe_name(name):
        return None, "容器名不合法"

    args = ["run", "-d"]
    if name:
        args += ["--name", name]

    # ports: [{"host": 8080, "container": 80, "protocol": "tcp"}]
    for p in body.get("ports") or []:
        raw_h, raw_c = p.get("host"), p.get("container")
        if raw_h is None and raw_c is None:
            continue
        h, c = _to_port(raw_h), _to_port(raw_c)
        if h is None or c is None:
            return None, "端口须为 1-65535 整数"
        proto = "udp" if p.get("protocol") == "udp" else "tcp"
        args += ["-p", f"{h}:{c}/{proto}"]

    # volumes: [{"host": "/path", "container": "/path", "readonly": false}]
    for v in body.get("volumes") or []:
        h, c = v.get("host"), v.get("container")
        if h and c:
            ro = ":ro" if v.get("readonly") else ""
            args += ["-v", f"{h}:{c}{ro}"]

    # env: [{"key": "FOO", "value": "bar"}]
    for e in body.get("env") or []:
        k, val = e.get("key"), e.get("value")
        if k:
            args += ["-e", f"{k}={val if val is not None else ''}"]

    # restart policy
    rp = (body.get("restart") or "").strip()
    if rp in ("no", "on-failure", "always", "unless-stopped"):
        args += ["--restart", rp]

    # network
    net = (body.get("network") or "").strip()
    if net and safe_name(net):
        args += ["--net", net]

    # extra raw flags
    extra = (body.get("extra") or "").strip()
    if extra:
        try:
            args += shlex.split(extra)
        except ValueError:
            pass

    args.append(image)
    # command override
    cmd = (body.get("command") or "").strip()
    if cmd:
        try:
            args += shlex.split(cmd)
        except ValueError:
            args += cmd.split()
    return args, None


def parse_json_list(output):
    items = []
    for line in output.splitlines():
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except Exception:
                pass
    return items


class Handler(BaseHTTPRequestHandler):
    timeout = 30  # per-connection socket timeout (slowloris guard)

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 1_000_000:
            length = 1_000_000
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _audit(self, method, path):
        print(f"[audit] {time.strftime('%Y-%m-%d %H:%M:%S')} {self.client_address[0]} {method} {path}", flush=True)

    # ===================== GET =====================
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = parse_qs(u.query)

        if path in ("/", "/api/health"):
            self._json({"ok": True, "service": "dockerctl", "ts": int(time.time())})
            return

        # --- system info ---
        if path == "/api/info":
            self._json(self._sys_info())
            return

        if path == "/api/overview":
            self._json(self._overview())
            return

        # --- containers ---
        if path == "/api/containers":
            show_all = q.get("all", ["0"])[0] != "0"
            args = ["ps", "--no-trunc", "--format", "{{json .}}"]
            if show_all:
                args.insert(1, "-a")
            code, out, err = run_docker(args, timeout=15)
            self._json({"items": parse_json_list(out), "error": err if code else ""})
            return

        m = re.fullmatch(r"/api/containers/([^/]+)/logs", path)
        if m:
            cid = unquote(m.group(1))
            if not safe_name(cid):
                self._json({"error": "invalid id"}, 400); return
            tail = q.get("tail", ["200"])[0]
            if not tail.isdigit():
                tail = "200"
            code, out, err = run_docker(["logs", "--tail", tail, cid], timeout=20)
            self._json({"logs": out, "error": err if code else ""})
            return

        m = re.fullmatch(r"/api/containers/([^/]+)/stats", path)
        if m:
            cid = unquote(m.group(1))
            if not safe_name(cid):
                self._json({"error": "invalid id"}, 400); return
            code, out, err = run_docker(["stats", "--no-stream", "--format", "{{json .}}", cid], timeout=12)
            st = {}
            if code == 0 and out.strip():
                try:
                    st = json.loads(out.strip().splitlines()[0])
                except Exception:
                    pass
            self._json({"stats": st, "error": err if code else ""})
            return

        m = re.fullmatch(r"/api/containers/([^/]+)", path)
        if m:
            cid = unquote(m.group(1))
            if not safe_name(cid):
                self._json({"error": "invalid id"}, 400); return
            code, out, err = run_docker(["inspect", cid], timeout=10)
            detail = {}
            if code == 0 and out.strip():
                try:
                    detail = json.loads(out)[0] if out.strip().startswith("[") else json.loads(out)
                except Exception:
                    pass
            self._json({"detail": detail, "error": err if code else ""})
            return

        # --- images ---
        if path == "/api/images":
            code, out, err = run_docker(["images", "--no-trunc", "--format", "{{json .}}"], timeout=15)
            self._json({"items": _attach_pull_times(parse_json_list(out)), "error": err if code else ""})
            return

        if path == "/api/images/pull":
            self._json({"error": "use POST"}, 405); return

        if path == "/api/images/pull/status":
            self._json({"task": _pull_snapshot()})
            return

        m = re.fullmatch(r"/api/images/([^/]+)", path)
        if m:
            img = unquote(m.group(1))
            if not safe_id(img):
                self._json({"error": "invalid image"}, 400); return
            code, out, err = run_docker(["inspect", img], timeout=10)
            detail = {}
            if code == 0 and out.strip():
                try:
                    detail = json.loads(out)[0] if out.strip().startswith("[") else json.loads(out)
                except Exception:
                    pass
            self._json({"detail": detail, "error": err if code else ""})
            return

        # --- volumes ---
        if path == "/api/volumes":
            code, out, err = run_docker(["volume", "ls", "--format", "{{json .}}"], timeout=10)
            self._json({"items": parse_json_list(out), "error": err if code else ""})
            return

        m = re.fullmatch(r"/api/volumes/([^/]+)", path)
        if m:
            vol = unquote(m.group(1))
            if not safe_name(vol):
                self._json({"error": "invalid volume"}, 400); return
            code, out, err = run_docker(["volume", "inspect", vol], timeout=8)
            detail = {}
            if code == 0 and out.strip():
                try:
                    detail = json.loads(out)[0]
                except Exception:
                    pass
            self._json({"detail": detail, "error": err if code else ""})
            return

        # --- networks ---
        if path == "/api/networks":
            code, out, err = run_docker(["network", "ls", "--format", "{{json .}}"], timeout=10)
            self._json({"items": parse_json_list(out), "error": err if code else ""})
            return

        m = re.fullmatch(r"/api/networks/([^/]+)", path)
        if m:
            net = unquote(m.group(1))
            if not safe_id(net):
                self._json({"error": "invalid network"}, 400); return
            code, out, err = run_docker(["network", "inspect", net], timeout=8)
            detail = {}
            if code == 0 and out.strip():
                try:
                    detail = json.loads(out)[0]
                except Exception:
                    pass
            self._json({"detail": detail, "error": err if code else ""})
            return

        # --- compose list ---
        if path == "/api/compose":
            items = self._list_compose()
            self._json({"items": items})
            return

        # --- registry auth list (server names only) ---
        if path == "/api/registry/auth":
            self._json({"registries": list_registry_auths()})
            return

        # --- operation log ---
        if path == "/api/logs":
            try:
                n = int(q.get("tail", ["200"])[0])
            except Exception:
                n = 200
            n = max(1, min(n, 500))
            with OPS_LOG["lock"]:
                items = OPS_LOG["items"][-n:]
            self._json({"items": items})
            return

        self._json({"error": "not found"}, 404)

    # ===================== POST =====================
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        self._audit("POST", path)
        body = self._read_body()

        # --- container lifecycle ---
        m = re.fullmatch(r"/api/containers/([^/]+)/(start|stop|restart|pause|unpause|kill|remove)", path)
        if m:
            cid, action = unquote(m.group(1)), m.group(2)
            if not safe_name(cid):
                self._json({"error": "invalid id"}, 400); return
            if action == "remove":
                args = ["rm"]
                if body.get("force"):
                    args.append("-f")
                args.append(cid)
            else:
                args = [action, cid]
            code, out, err = run_docker(args, timeout=20)
            log_op("容器操作:" + action, cid, code == 0, err if code else "")
            self._json({"ok": code == 0, "output": out, "error": err if code else ""})
            return

        # --- create & run container ---
        if path == "/api/deploy/run":
            self._deploy_run(body)
            return

        # --- compose up ---
        if path == "/api/deploy/compose":
            self._deploy_compose(body)
            return

        # --- clear operation log ---
        if path == "/api/logs/clear":
            with OPS_LOG["lock"]:
                OPS_LOG["items"] = []
            try:
                open(OPS_LOG_PATH, "w").close()
            except Exception:
                pass
            self._json({"ok": True})
            return

        # --- image pull (async task with progress) ---
        if path == "/api/images/pull":
            img = body.get("image", "").strip()
            tag_as = (body.get("tag_as") or "").strip()
            proxy = body.get("proxy") or ""
            if not img or not safe_id(img):
                self._json({"error": "invalid image name"}, 400); return
            if tag_as and not safe_id(tag_as):
                self._json({"error": "invalid tag name"}, 400); return
            if proxy:
                error = proxy_error(proxy)
                if error:
                    self._json({"error": error}, 400); return
                skopeo_bin = shutil.which(SKOPEO_BIN)
                if not skopeo_bin:
                    self._json({"error": "单次代理拉取需要在 NAS 安装 skopeo（或设置 SKOPEO_BIN）"}, 400); return
            else:
                skopeo_bin = ""
            with PULL_TASK["lock"]:
                t = PULL_TASK["task"]
                running = bool(t and t["status"] == "running")
                if running:
                    snap = dict(t)
                    snap.pop("proc", None)
                    snap.pop("helper_container", None)
                    snap["frames"] = list(t["frames"])
                    if t["image"] == img:
                        # 同镜像重复提交：幂等返回，前端直接打开进度
                        self._json({"ok": True, "already": True, "task": snap}); return
                    self._json({"ok": False, "task": snap,
                                "error": "已有拉取任务进行中: %s（同一时间只能拉一个，可在进度条上取消）" % t["image"]}, 409); return
                task = {"id": f"{int(time.time() * 1000)}", "image": img, "tag_as": tag_as,
                        "using_proxy": bool(proxy),
                        "status": "running", "frames": [], "error": "", "output": "",
                        "started": time.time(), "updated": time.time(), "ended": 0.0}
                if proxy and os.path.basename(skopeo_bin) == "skopeo-container":
                    task["helper_container"] = "dockerctl-skopeo-" + task["id"]
                PULL_TASK["task"] = task
            threading.Thread(target=_run_pull_task,
                             args=(img, tag_as, task["id"], proxy, skopeo_bin), daemon=True).start()
            self._json({"ok": True, "task": _pull_snapshot()})
            return

        # --- cancel running pull ---
        if path == "/api/images/pull/cancel":
            with PULL_TASK["lock"]:
                t = PULL_TASK["task"]
                if not t or t["status"] != "running":
                    self._json({"ok": False, "error": "没有进行中的拉取"}); return
                proc = t.get("proc")
                t["status"] = "error"
                t["error"] = "已手动取消"
                t["ended"] = time.time()
                img_name = t["image"]
                helper_container = t.get("helper_container")
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            if helper_container:
                run_docker(["stop", "-t", "1", helper_container], timeout=10)
            log_op("取消拉取", img_name, True, "")
            self._json({"ok": True})
            return

        # --- registry login / logout ---
        if path == "/api/registry/login":
            server = (body.get("server") or "").strip()
            user = (body.get("username") or "").strip()
            pw = body.get("password") or ""
            if not server or not safe_registry(server):
                self._json({"error": "invalid registry"}, 400); return
            if not user or "\n" in user or "\r" in user:
                self._json({"error": "invalid username"}, 400); return
            if not pw:
                self._json({"error": "password required"}, 400); return
            code, out, err = run_docker_input(
                ["login", "-u", user, "--password-stdin", server], pw + "\n", timeout=30)
            log_op("仓库登录", server, code == 0, err if code else "")
            self._json({"ok": code == 0, "output": out, "error": err if code else ""})
            return

        if path == "/api/registry/logout":
            server = (body.get("server") or "").strip()
            if not server or not safe_registry(server):
                self._json({"error": "invalid registry"}, 400); return
            code, out, err = run_docker(["logout", server], timeout=15)
            log_op("仓库退出", server, code == 0, err if code else "")
            self._json({"ok": code == 0, "output": out, "error": err if code else ""})
            return

        # --- image remove ---
        m = re.fullmatch(r"/api/images/([^/]+)/remove", path)
        if m:
            img = unquote(m.group(1))
            if not safe_id(img):
                self._json({"error": "invalid image"}, 400); return
            force = bool(body.get("force"))
            args = ["rmi"] + (["-f"] if force else []) + [img]
            code, out, err = run_docker(args, timeout=20)
            if code == 0:
                _forget_pull_time(img)
            log_op("删除镜像", img, code == 0, err if code else "")
            self._json({"ok": code == 0, "output": out, "error": err if code else ""})
            return

        # --- volume create / remove ---
        if path == "/api/volumes":
            name = body.get("name", "").strip()
            if not name or not safe_name(name):
                self._json({"error": "invalid volume name"}, 400); return
            code, out, err = run_docker(["volume", "create", name], timeout=8)
            log_op("创建存储卷", name, code == 0, err if code else "")
            self._json({"ok": code == 0, "output": out, "error": err if code else ""})
            return

        m = re.fullmatch(r"/api/volumes/([^/]+)/remove", path)
        if m:
            vol = unquote(m.group(1))
            if not safe_name(vol):
                self._json({"error": "invalid volume"}, 400); return
            code, out, err = run_docker(["volume", "rm", vol], timeout=8)
            log_op("删除存储卷", vol, code == 0, err if code else "")
            self._json({"ok": code == 0, "output": out, "error": err if code else ""})
            return

        # --- network create / remove ---
        if path == "/api/networks":
            name = body.get("name", "").strip()
            driver = body.get("driver", "bridge").strip()
            if not name or not safe_name(name):
                self._json({"error": "invalid network name"}, 400); return
            if driver not in ("bridge", "host", "overlay", "macvlan", "none"):
                driver = "bridge"
            args = ["network", "create", "--driver", driver]
            subnet = (body.get("subnet") or "").strip()
            gateway = (body.get("gateway") or "").strip()
            parent = (body.get("parent") or "").strip()
            if subnet:
                if not safe_addr(subnet):
                    self._json({"error": "invalid subnet"}, 400); return
                args += ["--subnet", subnet]
            if gateway:
                if not safe_addr(gateway):
                    self._json({"error": "invalid gateway"}, 400); return
                args += ["--gateway", gateway]
            if parent:
                if not safe_name(parent):
                    self._json({"error": "invalid parent interface"}, 400); return
                args += ["-o", f"parent={parent}"]
            args.append(name)
            code, out, err = run_docker(args, timeout=10)
            log_op("创建网络", name, code == 0, err if code else "")
            self._json({"ok": code == 0, "output": out, "error": err if code else ""})
            return

        m = re.fullmatch(r"/api/networks/([^/]+)/remove", path)
        if m:
            net = unquote(m.group(1))
            if not safe_id(net):
                self._json({"error": "invalid network"}, 400); return
            code, out, err = run_docker(["network", "rm", net], timeout=10)
            log_op("删除网络", net, code == 0, err if code else "")
            self._json({"ok": code == 0, "output": out, "error": err if code else ""})
            return

        # --- compose down / remove ---
        m = re.fullmatch(r"/api/compose/([^/]+)/(down|remove)", path)
        if m:
            proj, action = unquote(m.group(1)), m.group(2)
            if not safe_name(proj):
                self._json({"error": "invalid project"}, 400); return
            proj_dir = os.path.join(COMPOSE_DIR, proj)
            yml_path = os.path.join(proj_dir, "docker-compose.yml")
            if not os.path.isfile(yml_path):
                self._json({"error": "project not found"}, 404); return
            if action == "down":
                code, out, err = run_docker(["compose", "-p", proj, "-f", yml_path, "down"], timeout=60)
            else:
                # remove project dir only if down succeeds
                code, out, err = run_docker(["compose", "-p", proj, "-f", yml_path, "down", "--remove-orphans"], timeout=60)
                if code == 0:
                    shutil.rmtree(proj_dir, ignore_errors=True)
            log_op("Compose " + action, proj, code == 0, err if code else "")
            self._json({"ok": code == 0, "output": out, "error": err if code else ""})
            return

        self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        # No CORS headers: the UI is same-origin (served via the platform's
        # CGI/nginx), so cross-origin access is intentionally not allowed.
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()

    # ===================== helpers =====================
    def _sys_info(self):
        code, out, err = run_docker(["version", "--format", "{{json .}}"], timeout=8)
        ver = {}
        if code == 0 and out.strip():
            try:
                ver = json.loads(out)
            except Exception:
                pass
        code2, out2, _ = run_docker(["info", "--format", "{{json .}}"], timeout=10)
        info = {}
        if code2 == 0 and out2.strip():
            try:
                info = json.loads(out2)
            except Exception:
                pass
        return {"docker": ver, "info": info, "docker_bin": DOCKER_BIN}

    def _overview(self):
        code, out, err = run_docker(["ps", "-a", "--format", "{{.State}}"], timeout=12)
        states = {"running": 0, "exited": 0, "paused": 0, "created": 0, "restarting": 0, "other": 0}
        total = 0
        if code == 0:
            for line in out.splitlines():
                s = line.strip().lower()
                if not s:
                    continue
                total += 1
                if s in states:
                    states[s] += 1
                else:
                    states["other"] += 1
        code2, out2, _ = run_docker(["images", "-q"], timeout=10)
        images = len([x for x in out2.splitlines() if x.strip()]) if code2 == 0 else 0
        code3, out3, _ = run_docker(["volume", "ls", "-q"], timeout=8)
        volumes = len([x for x in out3.splitlines() if x.strip()]) if code3 == 0 else 0
        code4, out4, _ = run_docker(["network", "ls", "-q"], timeout=8)
        networks = len([x for x in out4.splitlines() if x.strip()]) if code4 == 0 else 0
        running = states.get("running", 0)
        return {
            "containers_total": total,
            "containers_running": running,
            "images": images,
            "volumes": volumes,
            "networks": networks,
            "states": states,
            "error": err if code else "",
        }

    def _deploy_run(self, body):
        """Run `docker run` from form fields (args built by build_run_args)."""
        args, err = build_run_args(body)
        if err:
            self._json({"error": err}, 400); return
        code, out, err = run_docker(args, timeout=60)
        log_op("部署容器", (body.get("name") or body.get("image") or "").strip(),
               code == 0, err if code else "")
        self._json({"ok": code == 0, "container_id": out.strip(), "output": out, "error": err if code else ""})

    def _deploy_compose(self, body):
        """Write compose YAML and run `docker compose up -d`."""
        name = (body.get("name") or "").strip() or f"stack-{int(time.time())}"
        if not safe_name(name):
            self._json({"error": "项目名不合法"}, 400); return
        yaml_text = body.get("yaml") or body.get("compose") or ""
        if not yaml_text.strip():
            self._json({"error": "compose YAML 不能为空"}, 400); return

        os.makedirs(COMPOSE_DIR, exist_ok=True)
        proj_dir = os.path.join(COMPOSE_DIR, name)
        os.makedirs(proj_dir, exist_ok=True)
        yml_path = os.path.join(proj_dir, "docker-compose.yml")
        with open(yml_path, "w", encoding="utf-8") as f:
            f.write(yaml_text)

        # docker compose up
        code, out, err = run_docker(["compose", "-p", name, "-f", yml_path, "up", "-d"], timeout=120)
        if code != 0:
            # fallback: standalone docker-compose binary
            try:
                r = subprocess.run(
                    ["docker-compose", "-p", name, "-f", yml_path, "up", "-d"],
                    capture_output=True, text=True, errors="replace", timeout=120
                )
                code, out, err = r.returncode, r.stdout, r.stderr
            except FileNotFoundError:
                pass  # keep original error
            except Exception as e:
                err = str(e)

        log_op("部署 Compose", name, code == 0, err if code else "")
        self._json({
            "ok": code == 0,
            "project": name,
            "path": yml_path,
            "output": out,
            "error": err if code else "",
        })

    def _list_compose(self):
        if not os.path.isdir(COMPOSE_DIR):
            return []
        items = []
        for d in sorted(os.listdir(COMPOSE_DIR)):
            yml = os.path.join(COMPOSE_DIR, d, "docker-compose.yml")
            if os.path.isfile(yml):
                code, out, err = run_docker(["compose", "-p", d, "ps", "--format", "{{json .}}"], timeout=10)
                services = parse_json_list(out) if code == 0 else []
                items.append({"name": d, "path": yml, "services": services})
        return items


def main():
    _load_ops_log()
    _load_pull_times()
    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    print(f"dockerctl listening on {BIND_HOST}:{PORT} docker={DOCKER_BIN}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
