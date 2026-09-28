# 小米 NAS 插件统一部署入口

在仓库根目录运行 `python deploy-plugins.py`。脚本会读取本地配置，或询问 NAS 地址、米家用户数字 UID 和 SSH 私钥路径；验证 SSH 可用且 NAS 端拥有 root 权限后，列出可部署插件。输入编号或插件 ID 部署一个插件，输入逗号分隔的多个编号部署多个，输入 `a` 部署全部，输入 `0` 则不部署任何插件。

## 配置

可将 `deploy-config.example.json` 复制为 `deploy-config.json` 并填写：

```json
{
  "host": "NAS 的 IP 或主机名",
  "uid": "米家用户数字 UID，不带 u",
  "ssh_key": "SSH 私钥绝对路径；留空使用 SSH 默认配置",
  "user": "root",
  "port": 22,
  "transport": "auto"
}
```

`deploy-config.json` 已被 Git 忽略。Windows 上 `auto` 优先使用 WSL 中的 `ssh` 和 `scp`；`ssh_key` 可写 WSL 绝对路径、`~/.ssh/...` 或 Windows 绝对路径。没有 WSL 时可将 `transport` 设为 `native`。首次连接新 NAS 时，应先在相同的 SSH 环境中手动确认主机密钥；脚本使用非交互式 SSH，不会跳过主机密钥检查。

## 常用命令

```powershell
python .\deploy-plugins.py --list
python .\deploy-plugins.py
python .\deploy-plugins.py --select none
python .\deploy-plugins.py --select sysmon,motrix
```

命令行参数可以覆盖配置文件，完整列表见 `python deploy-plugins.py --help`。脚本只打包和上传被选中的插件，然后调用该插件自带的 NAS 端安装脚本。sysmon 使用 `deploy/deploy2.sh`；Motrix 还会收到 NAS 地址参数。没有 `INFO` 和安装脚本的目录不会出现在菜单中。选择多个插件时，任何一个安装失败都会停止后续安装。

安装脚本会修改 NAS 上的插件目录、注册信息和相关服务。运行前请确认选择的插件及其专属部署要求；例如 Motrix 要求 NAS 上已有相应 Docker 镜像。
