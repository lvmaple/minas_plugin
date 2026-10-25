# xiaomi-nas-root.py — 小米智能存储一键 Root（固件 1.0.9.x）

参照 [kid0114/xiaomi-nas-ssh-root-tool](https://github.com/kid0114/xiaomi-nas-ssh-root-tool) 的使用形态重构的**新攻击链一键脚本**（原漏洞已在 1.0.9.462 被修补）。攻击链各环节经真机逐一验证；脚本本身做过函数级真机回归，端到端完整链未在裸机整体重放（验证机已处于 root 态），详见 README 的验证声明。

## 前置条件

1. 本机已安装「小米智能存储」桌面端并**登录过目标 NAS**（客户端证书由 App 生成，脚本自动查找）
2. Python 3.9+，安装依赖：`pip install -r requirements.txt`（keystone-engine，现场汇编 aarch64 触发器）

## 用法

```powershell
python xiaomi-nas-root.py <NAS_IP>
# 可选参数:
#   --uid <米家数字UID>         指定米家数字 UID（默认从证书解析）
#   --cert-dir <路径>          指定证书目录（默认自动搜索 minasCert）
#   --shell-port 2323          root shell 端口
```

## 敏感信息处理

- **证书目录**：默认搜索 `~/AppData/Local/minasCert` 等常见路径，找不到时 `--cert-dir` 手动指定
- **WebDAV 密码**：脚本经 mTLS 调 `get_pool_info` 现场获取，**不落盘不显示**
- **SSH 密钥**：`~/.ssh/id_ed25519_minas` 不存在则现场生成，私钥只写本机

## 攻击链（3 步，约 2 分钟）

| 步骤 | 动作 | 原理 |
|---|---|---|
| ① | mTLS 证书 → get_pool_info | 拿 WebDAV 凭据（端口 5000） |
| ② | MKCOL 换行目录 + add_samba_account | `smb_mgr.sh` uci heredoc 注入 → `force user=root` 的 `/etc` SMB 共享 |
| ③ | ld.so.preload + 触发 .so | keystone 汇编的 aarch64 .so，cron 每分钟 root 任务触发 → socat root shell + dropbear |

## 产物

- **root shell**：`nc <IP> 2323`（或 `wsl bash -c 'nc <IP> 2323'`）
- **SSH 22**：`ssh -i ~/.ssh/id_ed25519_minas root@<IP>`（公钥注入；注意本固件 dropbear 有 exec bug——认证可用但命令无输出）

**不做持久化**：本脚本只获取本机会话——无挂载钩子、无开机自启。设备重启后 **socat root shell 与 dropbear 服务消失**，需要时重新运行即可（全程约 2 分钟）；**有意保留**：两处 SSH 公钥（`/etc/dropbear/authorized_keys`、`/home/rootx/.ssh/authorized_keys`）与 passwd 中 root home 修改——密钥认证重启后仍可用，如需彻底清除请删除这两个文件并把 home 改回 `/root`。是否及如何做持久化由使用者自行决定，不属于本脚本范围。

## 清理行为

进入触发阶段后，成功与失败路径都会尝试清理（经 root shell）：解除 preload 布防，并只清除本次新建且仍符合归属检查的 SMB 记录、临时目录和精确匹配的 samba 系统账号行。清理使用读回确认的实际 ID，核对账号、口令指纹、路径、启停状态和共享属性；记录发生变化时输出 `SMB_CLEAN_SKIP`，保留配置，不误报清理成功。root shell 不可达或清理未确认成功时，把带归属检查的完整命令写入本地 `cleanup-<IP>.sh`，并打印手动提示；命令不包含临时 SMB 明文口令。

SMB 新增或设置阶段若发生错误、响应不完整或读回不一致，立即停止，不猜测 ID，不自动覆盖或删除记录。此时可能留下本次暂存目录或新建的临时账号，需要先核对状态再处理。固件没有提供“仅当配置未变时写入”的原子接口，运行期间请勿同时在客户端修改 SMB 配置；预检和读回不能替代服务端的并发控制。

## 注意事项

- 仅用于**自有设备**
- 步骤②先调用 `get_samba_account` 和 `judge_samba_account`。已有 `u<uid>`（忽略大小写）或目标槽位已占用时停止，保留原密码、共享路径和启停状态；不把已有账号临时改作工具共享
- 固件首次分配 ID=1，`_1` 存在时选择 ID=2。脚本核对新增响应和读回记录，将实际 ID 用于临时共享、设置和清理；设置为 `/etc` 后共享名为 `etc-<uid>`
- `add_samba_account` 返回 2502（restart 失败）时，只有读回确认本次新建账号、路径和原有记录均符合预期，才继续修复该账号的共享路径。2509 或其他失败不再回退调用设置接口
- `--uid` 必须与当前认证证书中的 UID 一致
- SMB 写入依赖 Windows（net use）或 POSIX（mount cifs，需 cifs-utils）；均不可用时脚本报错并给出手动挂载指引

## 部署/运维硬规矩（真机踩坑提炼）

1. **文本/二进制分流**：任何 CRLF 修复（`tr -d '\r'`）绝不允许作用于 png/ico/jpg/woff/webp 等二进制——会删掉文件内 0x0D 字节导致损坏（图标空白事故根因）
2. **改 album DB 后重启 albumd**；member_id 必须相册内唯一（用 shot_time 微秒级），否则 `BUG: member id tree same key` 导致整体加载中止
3. socat 会话内 systemctl restart 会 D-Bus 死锁——用 cron 一次性任务执行 systemctl 类操作

## 离线回归

在仓库根目录执行：

```powershell
python -B -X utf8 -m unittest discover -s tests -p test_root_tool_smb.py -v
```

测试替换 NAS 传输，禁止真实 socket 和远程进程调用。清理命令在本地 POSIX shell（Windows 可用 WSL）的临时沙箱执行，UCI、系统服务和 NAS 路径操作均被隔离；没有可用 shell 时跳过清理沙箱用例。以上验证不代表 NAS 安装或完整 Root 链验证。
