# 小米 NAS 插件统一部署入口

在仓库根目录运行 `python deploy-plugins.py`。脚本会读取本地配置，或询问 NAS 地址、米家用户数字 UID 等信息；验证连接通道可用且 NAS 端拥有 root 权限后，列出可部署插件。输入编号或插件 ID 部署一个插件，输入逗号分隔的多个编号部署多个，输入 `a` 部署全部，输入 `0` 则不部署任何插件。

## 连接通道（固件 1.0.9.462 起支持双通道）

| 通道 | 用法 | 说明 |
|---|---|---|
| **socat**（推荐） | `--channel socat` | 经 root shell TCP 通道（默认 2323 端口）执行命令 + WebDAV（mTLS 证书，端口 5000）上传。**对固件 dropbear exec bug 免疫**——SSH 认证能过但命令无输出时，此通道依然可用 |
| ssh | `--channel ssh` | 传统 scp+ssh。要求 NAS 端 SSH exec 正常 |

`--channel auto`（默认）：Windows 上选 socat，其他平台选 ssh。socat 通道前置条件：NAS 上已开启 socat root shell（2323 端口，开启方式见工具包 `xiaomi-nas-root-tool-1.0.9.462/` 内文档）；本机存在桌面端生成的 mTLS 客户端证书（`minasCert` 目录，自动搜索，可用 `--cert-dir` 指定）。

socat 初始化时通过本地 CA 校验 TLS 证书链，再从服务端证书读取当前名称；仅这次探测关闭主机名匹配，正式连接同时校验证书链和主机名。证书不可信或连接失败会明确报错，不再静默猜测客户端证书中的名称。该探测不上传或安装插件。

## 配置

可将 `deploy-config.example.json` 复制为 `deploy-config.json` 并填写：

```json
{
  "host": "NAS 的 IP 或主机名",
  "uid": "米家用户数字 UID，不带 u",
  "ssh_key": "SSH 私钥绝对路径；留空使用 SSH 默认配置（ssh 通道）",
  "user": "root",
  "port": 22,
  "transport": "auto",
  "channel": "auto",
  "shell_port": 2323,
  "cert_dir": ""
}
```

`deploy-config.json` 已被 Git 忽略。新增字段：`channel`（ssh/socat/auto）、`shell_port`（socat root shell 端口）、`cert_dir`（socat 通道的 mTLS 证书目录）。ssh 通道的 `transport`/`ssh_key` 语义不变：Windows 上 `auto` 优先使用 WSL 中的 `ssh` 和 `scp`；首次连接新 NAS 时应先手动确认主机密钥。

## 常用命令

```powershell
python .\deploy-plugins.py --list
python .\deploy-plugins.py --channel socat              # Windows 推荐：对 dropbear bug 免疫
python .\deploy-plugins.py --channel socat --select sysmon,motrix
python .\deploy-plugins.py --select none                # 只验证连接不部署
```

命令行参数可以覆盖配置文件，完整列表见 `python deploy-plugins.py --help`。脚本只打包和上传被选中的插件，然后调用该插件自带的 NAS 端安装脚本。sysmon 使用 `deploy/deploy2.sh`；Motrix 还会收到 NAS 地址参数。没有 `INFO` 和安装脚本的目录不会出现在菜单中。选择多个插件时，任何一个安装失败都会停止后续安装。

安装脚本会修改 NAS 上的插件目录、注册信息和相关服务。运行前请确认选择的插件及其专属部署要求；例如 Motrix 要求 NAS 上已有相应 Docker 镜像。

## 部署内置的真机经验（自动处理，无需手动干预）

1. **CRLF 分流**：打包时自动重写部署包——仅对 `.sh/.py/.cgi/.lua/.json/INFO/control` 等文本做 CRLF 修正，png/woff 等二进制原样保留（真机事故教训：`tr` 碰 PNG 会删掉 0x0D 字节导致图标损坏）
2. **PATH 补全**：socat 通道执行时自动 `export PATH=$PATH:/usr/sbin:/sbin`（iptables 等工具在 /sbin，非交互 shell 找不到）
3. **成功判定**：deploy 脚本输出各异（"部署完成"/"Motrix ready"等），失败时自动探测 `/home/u<uid>/plugin/<id>/INFO` 兜底确认，避免误报

## 部署后的插件自愈（重要前置）

插件会被"打开应用市场"触发的 `plugincenter update` 对账清除（不在云端 server.list 的判"已下架"删除）。若 NAS 上已部署自愈机制（server.list 条目缓存 + 定时 cron 脚本，原理见工具包内《技术复盘.md》"运行期防删除"一节），无需额外操作；全新机器首次部署后需按同原理初始化自愈，否则打开应用市场即丢插件。
