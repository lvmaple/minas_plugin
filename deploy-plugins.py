#!/usr/bin/env python3
"""在本机选择、打包并部署本仓库的小米 NAS 插件。运行 `python deploy-plugins.py --help`。

支持两种连接通道：
- ssh：传统 scp+ssh（要求 NAS 端 SSH exec 可用；固件 1.0.9.462 的 dropbear 有
  exec bug 时认证能过但命令无输出，此通道不可用）
- socat：经 root shell 的 TCP 通道执行 + WebDAV 上传（不依赖 SSH exec，
  固件 dropbear bug 下依然可用）
"""

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PLUGIN_PREFIX = "xiaomi-nas-plugin-"
PACKAGE_PARTS = ("INFO", "src", "files", "ui", "scripts", "deploy", "licenses")
SKIP_PARTS = {"__pycache__", ".git", ".pytest_cache"}
# 部署包里只对这些后缀做 CRLF 清理；二进制后缀绝不碰（真机事故：tr 碰 PNG 导致图标损坏）
TEXT_SUFFIXES = (".sh", ".py", ".cgi", ".lua", ".json", ".md", ".txt", ".conf", ".service")
BARE_TEXT_NAMES = {"INFO", "control"}
# NAS 端的部署步骤各有差异；这里只标注非默认入口和额外参数。
DEPLOY_OVERRIDES = {
    "sysmon": {"script": "deploy/deploy2.sh"},
    "motrix": {"pass_host": True},
}
CERT_SEARCH_DIRS = (
    os.path.expandvars(r"%LOCALAPPDATA%\minasCert") if os.name == "nt" else None,
    os.path.expanduser("~/AppData/Local/minasCert"),
    os.path.expanduser("~/.config/minasCert"),
)


@dataclass(frozen=True)
class Plugin:
    plugin_id: str
    name: str
    version: str
    directory: Path
    script: str
    pass_host: bool = False


@dataclass(frozen=True)
class Connection:
    host: str
    uid: str
    user: str
    port: int
    ssh_key: str | None
    transport: str
    channel: str = "ssh"          # ssh | socat
    shell_port: int = 2323        # socat root shell 端口
    cert_dir: str | None = None   # socat 通道的 mTLS 证书目录


def discover_plugins(root=ROOT):
    plugins = []
    for directory in sorted(root.glob(PLUGIN_PREFIX + "*")):
        info_file = directory / "INFO"
        if not directory.is_dir() or not info_file.is_file():
            continue
        try:
            info = json.loads(info_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"无法读取 {info_file}: {exc}") from exc
        plugin_id = info.get("plugin", "")
        if not isinstance(plugin_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", plugin_id):
            raise ValueError(f"{info_file} 的 plugin 字段无效")
        if directory.name != PLUGIN_PREFIX + plugin_id:
            raise ValueError(f"{info_file} 的 plugin 与目录名不一致")
        override = DEPLOY_OVERRIDES.get(plugin_id, {})
        script = override.get("script", "deploy/deploy.sh")
        if not (directory / script).is_file():
            continue
        plugins.append(Plugin(plugin_id, str(info.get("name") or plugin_id),
                              str(info.get("version") or "?"), directory,
                              script, bool(override.get("pass_host"))))
    return plugins


def read_config(path, required=False):
    if not path.is_file():
        if required:
            raise ValueError(f"配置文件不存在：{path}")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取配置文件 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("配置文件顶层必须是 JSON 对象")
    return data


def required_value(value, prompt):
    if value is None or str(value).strip() == "":
        if not sys.stdin.isatty():
            raise ValueError(f"缺少{prompt}；请写入配置文件或通过命令行参数提供")
        try:
            value = input(prompt + "：").strip()
        except EOFError as exc:
            raise ValueError(f"缺少{prompt}；请写入配置文件或通过命令行参数提供") from exc
    if not str(value).strip():
        raise ValueError(f"{prompt}不能为空")
    return str(value).strip()


def make_connection(args, config):
    def setting(arg_name, config_name=None, default=None):
        value = getattr(args, arg_name)
        return value if value is not None else config.get(config_name or arg_name, default)

    host = required_value(setting("host"), "NAS 地址或主机名")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("NAS 地址只接受 IPv4 地址或普通主机名")
    uid = required_value(setting("uid"), "米家用户数字 UID（不带 u）")
    if not re.fullmatch(r"[0-9]+", uid):
        raise ValueError("UID 必须为纯数字，不带 u 前缀")
    user = str(setting("user", default="root"))
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise ValueError("SSH 用户名无效")
    try:
        port = int(setting("port", default=22))
    except (TypeError, ValueError) as exc:
        raise ValueError("SSH 端口必须是数字") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SSH 端口必须在 1–65535 之间")
    transport = str(setting("transport", default="auto")).lower()
    if transport == "auto":
        transport = "wsl" if os.name == "nt" and shutil.which("wsl") else "native"
    if transport not in ("native", "wsl"):
        raise ValueError("transport 只能是 auto、native 或 wsl")
    key = setting("ssh_key")
    if key is None and sys.stdin.isatty():
        try:
            key = input("SSH 私钥路径（留空使用 SSH 默认配置）：").strip()
        except EOFError:
            key = ""
    key = str(key).strip() if key is not None else ""
    channel = str(setting("channel", default="ssh")).lower()
    if channel == "auto":
        # socat 通道对 dropbear exec bug 免疫，Windows/固件 1.0.9.x 优先
        channel = "socat" if os.name == "nt" else "ssh"
    if channel not in ("ssh", "socat"):
        raise ValueError("channel 只能是 ssh、socat 或 auto")
    shell_port = int(setting("shell_port", default=2323))
    if not 1 <= shell_port <= 65535:
        raise ValueError("socat shell 端口必须在 1–65535 之间")
    cert_dir = setting("cert_dir")
    return Connection(host, uid, user, port, key or None, transport,
                      channel, shell_port, cert_dir)


class SshClient:
    def __init__(self, connection):
        self.connection = connection
        if connection.transport == "wsl":
            self.prefix = [shutil.which("wsl") or "wsl", "--"]
        else:
            self.prefix = []
        self.key = self._resolve_key(connection.ssh_key)

    def _run_local(self, command, capture=True):
        return subprocess.run(command, text=True, encoding="utf-8", errors="replace",
                              capture_output=capture, check=False)

    def _wsl_path(self, path):
        result = self._run_local(self.prefix + ["wslpath", "-a", "-u", Path(path).as_posix()])
        if result.returncode != 0:
            raise ValueError("无法将 Windows 路径转换为 WSL 路径：" + result.stderr.strip())
        return result.stdout.strip()

    def _resolve_key(self, key):
        if not key:
            return None
        if self.connection.transport == "native":
            path = Path(key).expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"SSH 私钥文件不存在：{path}")
            return str(path)
        if re.match(r"^[A-Za-z]:[\\/]", key):
            key = self._wsl_path(key)
        elif key == "~" or key.startswith("~/"):
            home = self._run_local(self.prefix + ["printenv", "HOME"])
            if home.returncode != 0 or not home.stdout.strip():
                raise ValueError("无法读取 WSL HOME 路径")
            key = home.stdout.strip() + key[1:]
        elif not key.startswith("/"):
            raise ValueError("WSL 私钥请使用绝对路径、~/... 或 Windows 绝对路径")
        result = self._run_local(self.prefix + ["test", "-f", key])
        if result.returncode != 0:
            raise ValueError(f"WSL 中找不到 SSH 私钥：{key}")
        return key

    def _options(self, scp=False):
        port_flag = "-P" if scp else "-p"
        options = [port_flag, str(self.connection.port),
                   "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                   "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]
        if self.key:
            options.extend(["-i", self.key, "-o", "IdentitiesOnly=yes"])
        return options

    def ssh(self, remote_command, capture=False):
        target = f"{self.connection.user}@{self.connection.host}"
        command = self.prefix + ["ssh"] + self._options() + [target, remote_command]
        return self._run_local(command, capture=capture)

    def scp(self, local_file, remote_file):
        if self.connection.transport == "wsl":
            local_file = self._wsl_path(local_file)
        target = f"{self.connection.user}@{self.connection.host}:{remote_file}"
        command = self.prefix + ["scp"] + self._options(scp=True) + [str(local_file), target]
        return self._run_local(command, capture=False)

    def check_connection(self):
        # 部署脚本都要求 root；提前确认 UID 用户存在，并确认 NAS 有解压环境。
        command = f"id -u && id -u u{self.connection.uid} && command -v unzip && command -v python3"
        result = self.ssh(command, capture=True)
        if result.returncode != 0:
            raise RuntimeError("SSH 连接或 NAS 前置检查失败：\n" +
                               (result.stderr.strip() or result.stdout.strip()))
        if not result.stdout.splitlines() or result.stdout.splitlines()[0].strip() != "0":
            raise RuntimeError("NAS 端部署需要 root 权限；当前 SSH 用户不是 root")


def find_cert_dir(explicit, uid):
    """查找桌面端生成的 mTLS 客户端证书目录（含 <uid>*_cert.pem）。"""
    candidates = [explicit] if explicit else [d for d in CERT_SEARCH_DIRS if d]
    for directory in candidates:
        if directory and Path(directory).is_dir():
            pems = [p for p in Path(directory).glob("*_cert.pem")]
            if pems and any(p.name.startswith(f"{uid}_") for p in pems):
                return Path(directory)
            if pems:  # 目录对但 uid 不匹配：仍返回，由调用方校验
                return Path(directory)
    return None


class SocatClient:
    """经 root shell TCP 通道执行命令 + WebDAV mTLS 上传（不依赖 SSH exec）。

    前提：NAS 上已通过 root 手段开启 socat root shell（默认 2323 端口，
    PTY 模式）。WebDAV 上传走 5000 端口，凭据经 mTLS 调 get_pool_info 现场获取。
    """

    def __init__(self, connection):
        self.connection = connection
        self.cert_dir = find_cert_dir(connection.cert_dir, connection.uid)
        if self.cert_dir is None:
            raise ValueError(
                "socat 通道需要桌面端客户端证书目录（含 *_cert.pem / *_private_key.pem / ca_chain.pem）；"
                "请用 --cert-dir 指定，或改用 --channel ssh")
        certs = sorted(self.cert_dir.glob(f"{connection.uid}_*_cert.pem"))
        if not certs:
            certs = sorted(self.cert_dir.glob("*_cert.pem"))
        if not certs:
            raise ValueError(f"证书目录 {self.cert_dir} 中没有 *_cert.pem")
        self.cert = certs[0]
        self.key = self.cert.with_name(self.cert.name.replace("_cert.pem", "_private_key.pem"))
        self.ca = self.cert_dir / "ca_chain.pem"
        for path, what in ((self.key, "私钥"), (self.ca, "CA 链")):
            if not path.is_file():
                raise ValueError(f"证书目录缺少{what}：{path}")
        # 服务器 CN 形如 nas.<uid>.<did>.<n>——从证书里解析
        der = base64.b64decode(b"".join(
            self.cert.read_bytes().splitlines()[1:-1]))
        match = re.search(rb"nas\.[0-9]+\.[0-9a-f]+\.[0-9]+", der)
        if not match:
            raise ValueError("无法从证书解析服务器 CN")
        self.server_cn = match.group(0).decode()
        self.ctx = ssl.create_default_context(cafile=str(self.ca))
        self.ctx.load_cert_chain(str(self.cert), str(self.key))
        self._webdav_auth = None

    # ---- 通道探测 ----
    def check_connection(self):
        output = self.run_command("id -u && id -u u%s && command -v unzip && command -v python3"
                                  % self.connection.uid, timeout=15)
        if not output.strip().startswith("0"):
            raise RuntimeError("socat root shell 不可用或不是 root：\n" + output[:200])
        print(f"✓ socat root shell 可用 ({self.connection.host}:{self.connection.shell_port})")

    # ---- 命令执行：base64 分块下发（规避 PTY 行缓冲限制） ----
    def run_command(self, script, timeout=60):
        b64 = base64.b64encode(script.encode()).decode()
        host = self.connection.host
        port = self.connection.shell_port
        if os.name == "nt":
            wrapper = ["wsl"] if shutil.which("wsl") else None
            if wrapper is None:
                raise RuntimeError("Windows 下 socat 通道依赖 WSL 中的 nc")
            # 单条短命令直接发；长脚本走分块
            if len(b64) <= 200:
                feed = (f"printf 'echo {b64} | base64 -d | bash\\nexit\\n' | timeout {timeout} nc {host} {port}")
            else:
                chunks = [b64[i:i + 100] for i in range(0, len(b64), 100)]
                lines = ["rm -f /tmp/dp.b64"] + [f"echo -n {c} >> /tmp/dp.b64" for c in chunks] + \
                        ["base64 -d /tmp/dp.b64 > /tmp/dp.sh; tr -d '\\r' < /tmp/dp.sh > /tmp/dp2.sh; "
                         f"bash /tmp/dp2.sh; rm -f /tmp/dp.b64 /tmp/dp.sh /tmp/dp2.sh",
                         "exit"]
                feed = "printf '" + "\\n".join(lines) + f"' | timeout {timeout} nc {host} {port}"
            result = subprocess.run(wrapper + ["bash", "-c", feed], text=True,
                                    encoding="utf-8", errors="replace",
                                    capture_output=True, timeout=timeout + 30)
            return self._strip_noise(result.stdout)
        # POSIX：直接管道
        chunks = [b64[i:i + 100] for i in range(0, len(b64), 100)]
        lines = ["rm -f /tmp/dp.b64"] + [f"echo -n {c} >> /tmp/dp.b64" for c in chunks] + \
                ["base64 -d /tmp/dp.b64 | bash", "exit"]
        command = "printf '" + "\\n".join(lines) + f"' | timeout {timeout} nc {host} {port}"
        result = subprocess.run(["bash", "-c", command], text=True,
                                encoding="utf-8", errors="replace",
                                capture_output=True, timeout=timeout + 30)
        return self._strip_noise(result.stdout)

    @staticmethod
    def _strip_noise(text):
        # 去掉 PTY 回显（base64 分块行、echo 命令行）与 dircolors/提示符噪声
        keep = []
        for line in text.splitlines():
            s = line.strip("\r")
            st = s.strip()
            if not st or st.startswith("root@") or "dircolors" in st:
                continue
            # 回显行：包含 "base64 -d" 或以 "echo" 开头的管道回显
            if "base64 -d" in st or st.startswith("echo ") or st.startswith("<"):
                continue
            # 分块下发回显
            if re.fullmatch(r"(echo -n )?[A-Za-z0-9+/=]{8,}( >> /tmp/dp\.b64)?", st):
                continue
            if st in ("exit", "注销", "logout"):
                continue
            keep.append(s)
        return "\n".join(keep)

    # ---- WebDAV 上传（mTLS + Basic，5000 端口，二进制安全） ----
    def _webdav_password(self):
        if self._webdav_auth is not None:
            return self._webdav_auth
        request = json.dumps({"selector": ["webDAV"]}).encode()
        raw = socket.create_connection((self.connection.host, 443), timeout=10)
        s = self.ctx.wrap_socket(raw, server_hostname=self.server_cn)
        s.sendall(("POST /cgi-bin/luci/filemgr/get_pool_info HTTP/1.1\r\nHost: n\r\n"
                   "Connection: close\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(request)}\r\n\r\n").encode() + request)
        chunks = []
        s.settimeout(10)
        while True:
            try:
                data = s.recv(65536)
            except Exception:
                break
            if not data:
                break
            chunks.append(data)
        s.close()
        response = b"".join(chunks).decode("utf-8", "replace")
        try:
            body = json.loads(response.split("\r\n\r\n", 1)[1])
            webdav = body["data"]["webDAV"]
            cred = f"{webdav['username']}:{webdav['password']}"
            self._webdav_auth = "Basic " + base64.b64encode(cred.encode()).decode()
        except Exception as exc:
            raise RuntimeError("get_pool_info 获取 WebDAV 凭据失败") from exc
        return self._webdav_auth

    def upload(self, local_path, remote_path):
        """remote_path 为 /pool0/data/ 下的路径（SMB 空间）；二进制安全。"""
        data = Path(local_path).read_bytes()
        auth = self._webdav_password()
        raw = socket.create_connection((self.connection.host, 5000), timeout=60)
        s = self.ctx.wrap_socket(raw, server_hostname=self.server_cn)
        s.sendall((f"PUT {remote_path} HTTP/1.1\r\nHost: n\r\nConnection: close\r\n"
                   f"Authorization: {auth}\r\nContent-Length: {len(data)}\r\n\r\n"
                   ).encode() + data)
        chunks = []
        s.settimeout(60)
        while True:
            try:
                data_part = s.recv(65536)
            except Exception:
                break
            if not data_part:
                break
            chunks.append(data_part)
        s.close()
        response = b"".join(chunks).decode("utf-8", "replace")
        status = response.split("\r\n")[0] if "\r\n" in response else response
        if " 20" not in status and " 201" not in status:
            raise RuntimeError(f"WebDAV 上传失败 {remote_path}: {status}")


def make_client(connection):
    return SocatClient(connection) if connection.channel == "socat" else SshClient(connection)


def package_plugin(plugin, archive):
    archive.parent.mkdir(parents=True, exist_ok=True)
    included = []
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for part in PACKAGE_PARTS:
            path = plugin.directory / part
            if not path.exists():
                continue
            if path.is_symlink():
                raise ValueError(f"插件包包含符号链接，已拒绝打包：{path}")
            candidates = [path] if path.is_file() else sorted(path.rglob("*"))
            for item in candidates:
                if any(piece in SKIP_PARTS for piece in item.parts) or item.suffix in (".pyc", ".zip"):
                    continue
                if item.is_symlink():
                    raise ValueError(f"插件包包含符号链接，已拒绝打包：{item}")
                if item.is_file():
                    name = item.relative_to(plugin.directory).as_posix()
                    output.write(item, name)
                    included.append(name)
    if "INFO" not in included or plugin.script not in included:
        raise ValueError(f"{plugin.plugin_id} 缺少 INFO 或 {plugin.script}")
    return len(included)


def parse_selection(raw, plugins):
    raw = raw.strip().lower()
    if raw in ("0", "none", "n"):
        return []
    if raw in ("a", "all"):
        return plugins[:]
    selected = []
    for token in raw.split(","):
        token = token.strip()
        if token.isdigit() and 1 <= int(token) <= len(plugins):
            plugin = plugins[int(token) - 1]
        else:
            plugin = next((p for p in plugins if p.plugin_id == token), None)
        if plugin is None:
            raise ValueError(f"无效选择：{token or '(空)'}")
        if plugin not in selected:
            selected.append(plugin)
    return selected


def select_plugins(plugins, preset=None):
    print("\n可部署插件：")
    for number, plugin in enumerate(plugins, 1):
        print(f"  {number}. {plugin.name} ({plugin.plugin_id}, {plugin.version})")
    print("  a. 全部部署")
    print("  0. 全部不部署，退出")
    if preset is not None:
        return parse_selection(preset, plugins)
    while True:
        try:
            return parse_selection(input("选择编号或插件 ID（可用逗号分隔）："), plugins)
        except EOFError:
            return []
        except ValueError as exc:
            print(exc)


def fix_text_crlf(archive):
    """重写部署包：仅对文本后缀做 CRLF 清理，二进制原样保留（防 PNG/字体损坏）。"""
    fixed = archive.with_suffix(".fixed.zip")
    changed = 0
    with zipfile.ZipFile(archive) as source, \
         zipfile.ZipFile(fixed, "w", zipfile.ZIP_DEFLATED) as output:
        for name in source.namelist():
            data = source.read(name)
            is_text = name.lower().endswith(TEXT_SUFFIXES) or Path(name).name in BARE_TEXT_NAMES
            if is_text and b"\r\n" in data:
                data = data.replace(b"\r\n", b"\n")
                changed += 1
            output.writestr(name, data)
    fixed.replace(archive)
    return changed


def deploy_plugin(plugin, connection, client, workdir):
    archive = workdir / f"{plugin.plugin_id}.zip"
    count = package_plugin(plugin, archive)
    fixed = fix_text_crlf(archive)
    print(f"\n==> {plugin.name}：已打包 {count} 个文件（CRLF 修正 {fixed} 个文本文件）")

    if connection.channel == "socat":
        return _deploy_via_socat(plugin, connection, client, archive)
    return _deploy_via_ssh(plugin, connection, client, archive)


def _deploy_via_ssh(plugin, connection, client, archive):
    token = uuid.uuid4().hex[:12]
    stage = f"/tmp/codex-plugin-{plugin.plugin_id}-{token}"
    created = False
    try:
        result = client.ssh(f"mkdir -m 700 {shlex.quote(stage)}")
        if result.returncode != 0:
            raise RuntimeError("无法在 NAS 创建临时目录")
        created = True
        result = client.scp(archive, f"{stage}/plugin.zip")
        if result.returncode != 0:
            raise RuntimeError("上传插件包失败")
        source = f"{stage}/package"
        entry = f"{source}/{plugin.script}"
        arguments = [connection.uid]
        if plugin.pass_host:
            arguments.append(connection.host)
        install = " ".join(shlex.quote(arg) for arg in ["/bin/sh", entry, *arguments])
        remote = (f"set -eu; mkdir -m 700 {shlex.quote(source)}; "
                  f"unzip -q {shlex.quote(stage + '/plugin.zip')} -d {shlex.quote(source)}; "
                  f"test -f {shlex.quote(entry)}; {install}")
        print(f"==> 在 NAS 执行 {plugin.script}（SSH 通道）")
        result = client.ssh(remote)
        if result.returncode != 0:
            raise RuntimeError(f"{plugin.plugin_id} 的 NAS 部署脚本失败（退出码 {result.returncode}）")
        print(f"✓ {plugin.name} 部署完成")
    finally:
        if created:
            client.ssh(f"rm -rf -- {shlex.quote(stage)}", capture=True)


def _deploy_via_socat(plugin, connection, client, archive):
    # 上传走 WebDAV（/pool0/data = /home/u<uid>/pool0/data），执行走 root shell
    staging = f"/pool0/data/.deploy_staging-{plugin.plugin_id}"
    home_stage = f"/home/u{connection.uid}/pool0/data/.deploy_staging-{plugin.plugin_id}"
    client.run_command(f"mkdir -p {home_stage}", timeout=20)
    client.upload(archive, f"{staging}/{plugin.plugin_id}.zip")
    print(f"==> 已上传 {plugin.plugin_id}.zip（WebDAV 通道）")
    source = f"{home_stage}/package"
    entry = f"{source}/{plugin.script}"
    arguments = [connection.uid]
    if plugin.pass_host:
        arguments.append(connection.host)
    install = " ".join(shlex.quote(arg) for arg in ["/bin/sh", entry, *arguments])
    script = (f"set -eu; mkdir -m 700 {shlex.quote(source)}; "
              f"unzip -q {shlex.quote(home_stage + '/' + plugin.plugin_id + '.zip')} -d {shlex.quote(source)}; "
              f"export PATH=$PATH:/usr/sbin:/sbin; "
              f"test -f {shlex.quote(entry)}; {install}")
    print(f"==> 在 NAS 执行 {plugin.script}（socat 通道）")
    output = client.run_command(script, timeout=180)
    # 部署脚本输出较长，只在失败时报给用户
    if "CLEAN_OK" not in output and "部署完成" not in output and "Motrix ready" not in output \
            and "sysmon 部署完成" not in output:
        # 宽松判断：deploy 脚本成功标志各异，落回检查目标产物
        probe = client.run_command(
            f"test -f /home/u{connection.uid}/plugin/{plugin.plugin_id}/INFO && echo DEPLOY_OK || echo DEPLOY_FAIL",
            timeout=20)
        if "DEPLOY_OK" not in probe:
            raise RuntimeError(f"{plugin.plugin_id} 部署失败；通道输出：\n{output[-500:]}")
    print(f"✓ {plugin.name} 部署完成")
    client.run_command(f"rm -rf {shlex.quote(home_stage)}", timeout=20)


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(
        description="统一打包并部署本仓库的小米 NAS 插件",
        epilog="直接运行可交互输入连接信息。也可复制 deploy-config.example.json 为 "
               "deploy-config.json（已被 Git 忽略），填入 host、uid、ssh_key 后运行。",
    )
    parser.add_argument("--config", type=Path, help="JSON 配置文件；默认读取仓库根目录的 deploy-config.json")
    parser.add_argument("--host", help="NAS IPv4 地址或主机名")
    parser.add_argument("--uid", help="米家用户数字 UID，不带 u")
    parser.add_argument("--ssh-key", help="SSH 私钥路径；留空使用 SSH 默认配置")
    parser.add_argument("--user", help="SSH 用户，默认 root")
    parser.add_argument("--port", type=int, help="SSH 端口，默认 22")
    parser.add_argument("--transport", choices=("auto", "native", "wsl"), help="SSH 运行环境")
    parser.add_argument("--channel", choices=("auto", "ssh", "socat"),
                        help="连接通道：ssh（传统 scp+ssh）或 socat（root shell + WebDAV，"
                             "对固件 dropbear exec bug 免疫）；auto 时 Windows 默认 socat")
    parser.add_argument("--shell-port", type=int, help="socat root shell 端口，默认 2323")
    parser.add_argument("--cert-dir", help="mTLS 客户端证书目录（socat 通道用，默认自动搜索 minasCert）")
    parser.add_argument("--select", help="插件编号或 ID，逗号分隔；也可用 all / none")
    parser.add_argument("--list", action="store_true", help="只列出可部署插件，不连接 NAS")
    args = parser.parse_args(argv)

    try:
        plugins = discover_plugins(root)
        if not plugins:
            raise ValueError("未发现带 INFO 和 NAS 部署脚本的插件")
        if args.list:
            print("可部署插件：")
            for number, plugin in enumerate(plugins, 1):
                print(f"  {number}. {plugin.name} ({plugin.plugin_id}, {plugin.version})")
            return 0
        config_path = args.config or root / "deploy-config.json"
        config = read_config(config_path, required=args.config is not None)
        connection = make_connection(args, config)
        client = make_client(connection)
        if connection.channel == "socat":
            print(f"正在连接 socat root shell：{connection.host}:{connection.shell_port} "
                  f"(证书: {client.cert_dir})")
        else:
            print(f"正在测试 SSH：{connection.user}@{connection.host}:{connection.port} "
                  f"({connection.transport})")
        client.check_connection()
        selected = select_plugins(plugins, args.select)
        if not selected:
            print("未选择插件，没有上传或部署任何内容。")
            return 0
        with tempfile.TemporaryDirectory(prefix="xiaomi-nas-deploy-") as temp:
            for plugin in selected:
                deploy_plugin(plugin, connection, client, Path(temp))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
