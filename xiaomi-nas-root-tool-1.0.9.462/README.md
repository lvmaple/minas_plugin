# xiaomi-nas-root-tool-1.0.9.462 — 小米智能存储（RP05）固件 1.0.9.x SSH 开启工具

旧工具（kid0114/xiaomi-nas-ssh-root-tool）依赖的 WebDAV 视频分段 shell 注入在固件 1.0.9.462 中已被修补。本工具基于新研究出的攻击链（uci heredoc 注入）实现——攻击链各环节均经真机逐一验证（注入构造、共享写入、preload 触发）；一键脚本做过函数级真机回归（证书解析/凭据获取/SMB 通道/触发 .so 汇编），端到端完整链未在裸机上整体重放（验证机已处于 root 态）。实测固件 1.0.9.462.F24C26D24-REL。

## 目录内容

| 文件 | 说明 |
|---|---|
| `xiaomi-nas-root.py` | 一键开启 SSH 脚本（mTLS 证书自动查找 / WebDAV 凭据现场获取不落盘 / 触发 .so 现场汇编） |
| `USAGE.md` | 脚本使用说明（前置条件、参数、注意事项） |
| `技术复盘.md` | 技术复盘文档（链路原理、各步骤坑位、修复建议） |
| `requirements.txt` | Python 依赖（keystone-engine，标准库之外唯一依赖） |

## 快速开始

```powershell
pip install -r requirements.txt
python xiaomi-nas-root.py <NAS_IP>
```

前置：本机已安装「小米智能存储」桌面端并登录过目标 NAS（自有设备）。

**说明**：本工具只获取本机会话，不做重启持久化；成功与失败路径均执行清理。详见 `USAGE.md`。目录名中的 `1.0.9.462` 为实测验证的完整固件版本；新固件发布后有效性需重新评估。
