#!/usr/bin/env python3
"""在本机选择、打包并部署本仓库的小米 NAS 插件。运行 `python deploy-plugins.py --help`。"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PLUGIN_PREFIX = "xiaomi-nas-plugin-"
PACKAGE_PARTS = ("INFO", "src", "files", "ui", "scripts", "deploy", "licenses")
SKIP_PARTS = {"__pycache__", ".git", ".pytest_cache"}
# NAS 端的部署步骤各有差异；这里只标注非默认入口和额外参数。
DEPLOY_OVERRIDES = {
    "sysmon": {"script": "deploy/deploy2.sh"},
    "motrix": {"pass_host": True},
}


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
    return Connection(host, uid, user, port, key or None, transport)


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


def deploy_plugin(plugin, connection, client, workdir):
    token = uuid.uuid4().hex[:12]
    stage = f"/tmp/codex-plugin-{plugin.plugin_id}-{token}"
    archive = workdir / f"{plugin.plugin_id}.zip"
    count = package_plugin(plugin, archive)
    print(f"\n==> {plugin.name}：已打包 {count} 个文件")
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
        print(f"==> 在 NAS 执行 {plugin.script}")
        result = client.ssh(remote)
        if result.returncode != 0:
            raise RuntimeError(f"{plugin.plugin_id} 的 NAS 部署脚本失败（退出码 {result.returncode}）")
        print(f"✓ {plugin.name} 部署完成")
    finally:
        if created:
            client.ssh(f"rm -rf -- {shlex.quote(stage)}", capture=True)


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
        client = SshClient(connection)
        print(f"正在测试 SSH：{connection.user}@{connection.host}:{connection.port} ({connection.transport})")
        client.check_connection()
        print("✓ SSH 可用，NAS 端为 root，米家 UID 用户存在")
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
