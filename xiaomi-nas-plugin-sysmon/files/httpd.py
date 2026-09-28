#!/usr/bin/env python3
"""sysmon 本地 HTTP 服务 — 提供系统状态 API 与 UPS 超时关机监控。

监听 127.0.0.1:9301，由 Nginx 反代 / plugin.cgi 的 api/* 转发进来。
HTTP API 仅提供读取；后台监控固件 UPS 状态并在连续后备供电 5 分钟后关机。
"""
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PLUG_PORT", "9301"))
START_TIME = time.time()
UPS_SHUTDOWN_AFTER_SEC = 300
UPS_TIMER_FILE = "/run/sysmon/ups-timer.json"


# ── 基础采集 ──────────────────────────────────────────────

def read_proc_meminfo():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    try:
                        info[key.strip()] = int(parts[0])  # kB
                    except ValueError:
                        pass
    except OSError:
        pass
    return info


def read_loadavg():
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            return {
                "load1": float(parts[0]),
                "load5": float(parts[1]),
                "load15": float(parts[2]),
            }
    except (OSError, IndexError, ValueError):
        return {"load1": 0.0, "load5": 0.0, "load15": 0.0}


def read_cpu_times():
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:8]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        return idle, total
    except (OSError, IndexError, ValueError):
        return 0, 0


_prev_idle, _prev_total = read_cpu_times()


def cpu_percent():
    global _prev_idle, _prev_total
    idle, total = read_cpu_times()
    d_idle = idle - _prev_idle
    d_total = total - _prev_total
    _prev_idle, _prev_total = idle, total
    if d_total <= 0:
        return 0.0
    return round((1.0 - d_idle / d_total) * 100.0, 1)


def disk_usage(path="/"):
    try:
        if hasattr(os, "statvfs"):
            st = os.statvfs(path)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
        else:
            u = shutil.disk_usage(path)
            total, free = u.total, u.free
        used = total - free
        return {
            "path": path,
            "total": total,
            "used": used,
            "free": free,
            "percent": round(used / total * 100, 1) if total else 0.0,
        }
    except OSError:
        return {"path": path, "total": 0, "used": 0, "free": 0, "percent": 0.0}


# ── 温度 / 风扇 ─────────────────────────────────────────────

def read_temps():
    """从 /sys/class/hwmon 与 thermal_zone 收集温度传感器。"""
    sensors = []
    drive_counter = 1

    # hwmon 节点（CPU、NVMe、主板等）
    base = "/sys/class/hwmon"
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            hw = os.path.join(base, name)
            # 读传感器标签
            labels = {}
            for f in os.listdir(hw):
                if f.startswith("temp") and f.endswith("_input"):
                    idx = f[5:-6]  # tempN_input → N
                    val_path = os.path.join(hw, f)
                    label = ""
                    for suffix in ("_label",):
                        lp = os.path.join(hw, "temp" + idx + suffix)
                        if os.path.isfile(lp):
                            try:
                                label = open(lp).read().strip()
                            except OSError:
                                pass
                    try:
                        raw = int(open(val_path).read().strip())
                    except (OSError, ValueError):
                        continue
                    # hwmon 一般为毫摄氏度
                    celsius = raw / 1000.0 if raw > 200 else float(raw)
                    chip = ""
                    try:
                        chip = open(os.path.join(hw, "name")).read().strip()
                    except OSError:
                        pass
                    display = label or ("temp" + idx)
                    if chip == "drivetemp":
                        display = f"硬盘{drive_counter}"
                        drive_counter += 1
                    sensors.append({
                        "source": "hwmon",
                        "chip": chip or name,
                        "label": display,
                        "celsius": round(celsius, 1),
                    })

    # thermal_zone 兜底
    tz_base = "/sys/class/thermal"
    if os.path.isdir(tz_base) and len(sensors) < 3:
        for name in sorted(os.listdir(tz_base)):
            if not name.startswith("thermal_zone"):
                continue
            tdir = os.path.join(tz_base, name)
            tfile = os.path.join(tdir, "temp")
            if not os.path.isfile(tfile):
                continue
            try:
                raw = int(open(tfile).read().strip())
            except (OSError, ValueError):
                continue
            celsius = raw / 1000.0 if raw > 200 else float(raw)
            typ = ""
            try:
                typ = open(os.path.join(tdir, "type")).read().strip()
            except OSError:
                pass
            display = typ or name
            if "cpu" in display.lower() or "soc" in display.lower():
                display = "CPU"
            sensors.append({
                "source": "thermal",
                "chip": typ or name,
                "label": display,
                "celsius": round(celsius, 1),
            })

    return sensors


def read_fans():
    """从 /sys/class/hwmon 读取风扇转速（RPM）。"""
    fans = []
    base = "/sys/class/hwmon"
    if not os.path.isdir(base):
        return fans
    for name in sorted(os.listdir(base)):
        hw = os.path.join(base, name)
        chip = ""
        try:
            chip = open(os.path.join(hw, "name")).read().strip()
        except OSError:
            pass
        for f in sorted(os.listdir(hw)):
            if not (f.startswith("fan") and f.endswith("_input")):
                continue
            idx = f[3:-6]
            try:
                rpm = int(open(os.path.join(hw, f)).read().strip())
            except (OSError, ValueError):
                continue
            label = ""
            lp = os.path.join(hw, "fan" + idx + "_label")
            if os.path.isfile(lp):
                try:
                    label = open(lp).read().strip()
                except OSError:
                    pass
            fans.append({
                "chip": chip or name,
                "label": label or ("fan" + idx),
                "rpm": rpm,
            })
    return fans


# ── Docker ─────────────────────────────────────────────────

def read_docker():
    """通过 docker CLI 读取容器列表（只读 ps，无写操作）。"""
    result = {"available": False, "containers": [], "counts": {}}
    docker_bin = shutil.which("docker") or "/data/docker/docker"
    if not os.path.isfile(docker_bin) and docker_bin != "docker":
        return result
    try:
        out = subprocess.run(
            [docker_bin, "ps", "-a",
             "--format", "{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.State}}|{{.Ports}}"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            result["error"] = (out.stderr or "docker error").strip()[:200]
            return result
        result["available"] = True
        counts = {"running": 0, "exited": 0, "other": 0}
        for line in out.stdout.splitlines():
            parts = line.split("|", 5)
            if len(parts) < 5:
                continue
            cid, name, image, status, state = parts[:5]
            ports = parts[5] if len(parts) > 5 else ""
            if state == "running":
                counts["running"] += 1
            elif state == "exited":
                counts["exited"] += 1
            else:
                counts["other"] += 1
            result["containers"].append({
                "id": cid,
                "name": name,
                "image": image,
                "status": status,
                "state": state,
                "ports": ports,
            })
        result["counts"] = counts
    except (OSError, subprocess.TimeoutExpired) as e:
        result["error"] = str(e)[:200]
    return result


def read_ups():
    """读取 NAS 固件记录的供电模式和异常标志，不推断 UPS 是否已连接。"""
    result = {"available": False, "state": None, "health": None}
    if not shutil.which("uci"):
        return result

    for field in ("state", "health"):
        try:
            output = subprocess.run(
                ["uci", "-q", "get", "system.ups." + field],
                capture_output=True, text=True, timeout=2,
            )
            if output.returncode == 0:
                result[field] = output.stdout.strip().upper() or None
        except (OSError, subprocess.TimeoutExpired):
            pass

    result["available"] = bool(result["state"] or result["health"])
    return result


def read_ups_state():
    """仅读取固件供电模式；读取失败时不得推断为 UPS。"""
    if not shutil.which("uci"):
        return None
    try:
        output = subprocess.run(
            ["uci", "-q", "get", "system.ups.state"],
            capture_output=True, text=True, timeout=2,
        )
        return output.stdout.strip().upper() if output.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def read_boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return None


class UpsShutdownMonitor:
    """独立于网页请求计时，使用开机时钟防止系统时间调整影响关机时刻。"""

    def __init__(self, state_reader=read_ups_state, clock=None,
                 wall_clock=None, shutdown=None, timer_file=UPS_TIMER_FILE,
                 boot_id=None):
        self.state_reader = state_reader
        self.clock = clock or (lambda: time.clock_gettime(time.CLOCK_BOOTTIME))
        self.wall_clock = wall_clock or time.time
        self.shutdown = shutdown or self._system_poweroff
        self.timer_file = timer_file
        self.boot_id = boot_id if boot_id is not None else read_boot_id()
        self.lock = threading.Lock()
        self.started_boottime = None
        self.started_at = None
        self.last_shutdown_attempt = None
        self._load()

    @staticmethod
    def _system_poweroff():
        result = subprocess.run(["systemctl", "poweroff"], timeout=10)
        if result.returncode != 0:
            raise RuntimeError("systemctl poweroff exited %d" % result.returncode)

    def _load(self):
        try:
            with open(self.timer_file, encoding="utf-8") as f:
                saved = json.load(f)
            started = float(saved["started_boottime"])
            if (not self.boot_id or saved.get("boot_id") != self.boot_id
                    or not 0 <= started <= self.clock()):
                return
            self.started_boottime = started
            self.started_at = int(saved["started_at"])
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def _save(self):
        directory = os.path.dirname(self.timer_file)
        temp_file = self.timer_file + ".tmp"
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump({"boot_id": self.boot_id,
                           "started_boottime": self.started_boottime,
                           "started_at": self.started_at}, f)
            os.replace(temp_file, self.timer_file)
        except OSError as exc:
            print("[sysmon] UPS timer save failed: %s" % exc, flush=True)

    def _reset(self):
        self.started_boottime = None
        self.started_at = None
        self.last_shutdown_attempt = None
        try:
            os.unlink(self.timer_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print("[sysmon] UPS timer reset failed: %s" % exc, flush=True)

    def tick(self):
        """每次读取真实供电状态；只有连续确认 UPS 才累计时间。"""
        state = self.state_reader()
        now = self.clock()
        should_shutdown = False
        with self.lock:
            if state != "UPS":
                if self.started_boottime is not None:
                    self._reset()
                return
            if self.started_boottime is None:
                self.started_boottime = now
                self.started_at = int(self.wall_clock())
                self._save()
            elapsed = now - self.started_boottime
            if elapsed >= UPS_SHUTDOWN_AFTER_SEC and (
                    self.last_shutdown_attempt is None
                    or now - self.last_shutdown_attempt >= 30):
                self.last_shutdown_attempt = now
                should_shutdown = True

        if should_shutdown and self.state_reader() == "UPS":
            print("[sysmon] UPS power for 5 minutes; powering off", flush=True)
            try:
                self.shutdown()
            except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
                print("[sysmon] UPS poweroff failed: %s" % exc, flush=True)

    def status(self):
        with self.lock:
            if self.started_boottime is None:
                return {"active": False, "elapsed_sec": 0,
                        "shutdown_after_sec": UPS_SHUTDOWN_AFTER_SEC,
                        "remaining_sec": None, "started_at": None}
            elapsed = max(0, int(self.clock() - self.started_boottime))
            return {"active": True, "elapsed_sec": elapsed,
                    "shutdown_after_sec": UPS_SHUTDOWN_AFTER_SEC,
                    "remaining_sec": max(0, UPS_SHUTDOWN_AFTER_SEC - elapsed),
                    "started_at": self.started_at}

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as exc:
                print("[sysmon] UPS monitor error: %s" % exc, flush=True)
            time.sleep(1)


ups_monitor = UpsShutdownMonitor()


# ── 汇总 ───────────────────────────────────────────────────

def collect_info():
    mem = read_proc_meminfo()
    if mem:
        mem_total = mem.get("MemTotal", 0) * 1024
        mem_avail = mem.get("MemAvailable", mem.get("MemFree", 0)) * 1024
    else:
        # Windows / 无 /proc 兜底（本地开发用）
        try:
            class _MS:
                pass
            # ctypes 全局内存状态
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            mem_total = stat.ullTotalPhys
            mem_avail = stat.ullAvailPhys
        except Exception:
            mem_total = mem_avail = 0
    mem_used = max(mem_total - mem_avail, 0)

    ups = read_ups()
    ups["timer"] = ups_monitor.status()

    return {
        "code": 0,
        "message": "success",
        "data": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "kernel": platform.release(),
            "uptime_sec": int(time.time() - START_TIME),
            "cpu": {
                "percent": cpu_percent(),
                "cores": os.cpu_count() or 1,
                "load": read_loadavg(),
            },
            "memory": {
                "total": mem_total,
                "used": mem_used,
                "free": mem_avail,
                "percent": round(mem_used / mem_total * 100, 1) if mem_total else 0.0,
            },
            "disk": disk_usage("/nas/pool0"),
            "temps": read_temps(),
            "fans": read_fans(),
            "ups": ups,
            "docker": read_docker(),
            "ts": int(time.time()),
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "sysmon/1.0"

    def log_message(self, fmt, *args):
        print("[sysmon] %s - %s" % (self.address_string(), fmt % args))

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/api/info", "/", ""):
            self._send_json(collect_info())
        elif path == "/api/health":
            self._send_json({"code": 0, "message": "ok", "data": {"status": "running"}})
        elif path == "/api/docker":
            self._send_json({"code": 0, "message": "success", "data": read_docker()})
        elif path == "/api/temps":
            self._send_json({"code": 0, "message": "success", "data": {
                "temps": read_temps(), "fans": read_fans(),
            }})
        else:
            self._send_json({"code": 404, "message": "not found"}, status=404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    ups_monitor.tick()
    threading.Thread(target=ups_monitor.run, name="ups-shutdown-monitor",
                     daemon=True).start()
    print("[sysmon] listening on 127.0.0.1:%d" % PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
