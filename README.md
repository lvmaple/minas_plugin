# 小米 NAS 插件合集

本仓库包含四个小米智能存储插件，以及用于打包和部署它们的统一命令行工具。每个插件都可以独立阅读、修改和部署；首次使用建议先查看对应插件的说明。

## 插件一览

| 插件 | 功能 | 详细说明 |
| --- | --- | --- |
| Docker 控制台（`dockerctl`） | 管理容器、镜像、存储卷和网络，支持部署容器 | [README](xiaomi-nas-plugin-dockerctl/README.md) |
| Motrix 下载（`motrix`） | 通过 Motrix Server 管理下载任务 | [README](xiaomi-nas-plugin-motrix/README.md) |
| PC 备份（`pcbackup`） | 将电脑目录增量备份到 NAS，支持断点续传 | [README](xiaomi-nas-plugin-pcbackup/README.md) |
| 系统状态（`sysmon`） | 查看 CPU、内存、磁盘、温度、风扇和 Docker 状态 | [README](xiaomi-nas-plugin-sysmon/README.md) |

## 快速开始

本地需要 Python 3.10 或更新版本。部署需要可连接 NAS 的 SSH 环境、NAS 的 root 权限，以及米家用户的**数字 UID**（不带 `u` 前缀）。部分插件还有专属前提，例如 Motrix 需要 NAS 上已有对应 Docker 镜像，请先阅读插件说明。

在仓库根目录执行：

```powershell
# 仅查看可部署插件；不会连接 NAS
python -X utf8 .\deploy-plugins.py --list

# 复制配置模板，填写 host、uid、ssh_key 等本机信息
Copy-Item .\deploy-config.example.json .\deploy-config.json

# 交互式选择插件并部署
python -X utf8 .\deploy-plugins.py

# 或直接选择一个或多个插件
python -X utf8 .\deploy-plugins.py --select sysmon,motrix
```

`deploy-config.json` 已被 Git 忽略。也可以用 `--host`、`--uid`、`--ssh-key` 等参数临时覆盖配置；运行 `python -X utf8 deploy-plugins.py --help` 查看完整选项。Windows 默认优先使用 WSL 的 SSH 工具；没有 WSL 时可将 `transport` 设为 `native`。首次连接 NAS 时，先在相同的 SSH 环境中确认主机密钥。

部署脚本会修改 NAS 的插件目录、注册信息和相关服务。只想检查连接、不上传或安装插件时，可运行 `python -X utf8 deploy-plugins.py --select none`。完整步骤见[统一部署说明](DEPLOY-PLUGINS.md)。

## 仓库结构与开发

- `xiaomi-nas-plugin-*/`：插件源码、`INFO` 元数据、前端资源和各自的 `deploy/` 脚本。
- `deploy-plugins.py`：发现、打包并部署所选插件。
- `tests/`：统一部署工具的 `unittest` 测试。

修改统一部署工具后，在仓库根目录运行：

```powershell
python -X utf8 -m unittest discover -s tests -v
```

插件的开发与部署细节见各目录的 README，以及[插件开发规范](小米NAS插件开发规范.md)。贡献前请阅读 [Repository Guidelines](AGENTS.md)。不要提交 SSH 私钥、访问令牌、真实设备地址或本机部署配置。
