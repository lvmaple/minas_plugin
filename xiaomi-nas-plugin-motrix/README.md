# 小米 NAS Motrix 插件

小米智能存储上的 Motrix Server 容器与米家插件入口。当前固定使用官方 `ghcr.io/agalwood/motrix-server:2.0.0-beta.40` 镜像，适合本工程实测的 aarch64 NAS。

## 部署

使用 `deploy/package-and-upload.ps1` 打包、上传并在 NAS 上执行部署；也可只打包。部署脚本要求 root SSH、`/data/docker/docker` 和已拉取的官方镜像。Docker Hub 在本机不可达，GHCR 可用。

从 NAS 插件图标打开 Motrix。插件首页直接加载官方 Motrix Web 界面，静态资源由插件提供，RPC 请求经 NAS 的插件地址转发。备用的简易任务页保存在 `manager.html`。插件代理在 NAS 端读取 `/data/motrix-server/data/operator-token` 向 Motrix 认证；用户无需查找或输入它，Operator token 也不会传给浏览器。插件接口使用专用随机凭据限制访问。容器的 8080 端口仅绑定 NAS 本机回环地址，不能从局域网直接访问。

持久数据：`/data/motrix-server/data`。默认下载目录为 `/nas/pool0/u<UID>/data/motrix-downloads`；添加任务或在通用设置中使用 Motrix 自带的服务器文件夹选择器，可改为 `/nas/pool0/u<UID>/data` 内的其他目录。容器仅挂载并允许这个用户的 `data` 目录，Motrix 会拒绝越界路径和符号链接逃逸。插件界面使用小米橙白配色。

本插件使用平台的手动注册方法。`motrix-restore.service` 会在 NAS 重启后恢复插件目录、图标、列表条目、Web 代理配置及仅针对 Motrix 容器当前桥接地址的出站转换规则；`motrix-web-proxy.service` 通过仅 NAS 本地 root 可访问的 Unix socket 转发接口。容器本身由 Docker 的 `unless-stopped` 策略重启。

资源补丁先在内存中转换并核对完整结果，通过后才写入。已经打过补丁或缺失上游资源的目录会明确报错，保留输入文件；请先覆盖原始资源再运行补丁。正常部署脚本会完成这次覆盖，可以重跑，也允许更新插件路径和接口凭据。

开机恢复优先使用明确传入的 NAS 地址，其次从默认路由和本机 IPv4 地址获取；仍无法获取时会明确失败，恢复服务每 5 秒重试，避免网络尚未就绪时静默跳过 Web 代理。

## 本地回归

在工程根目录运行：

```powershell
python -X utf8 -m unittest discover -s tests -p test_motrix_deploy.py -v
python -X utf8 -m unittest discover -s tests -p test_deploy_plugins.py -v
```

资源补丁测试只操作上游资源的临时副本。恢复脚本测试使用隔离目录与命令替身，需在 Linux 或 WSL 运行；Windows 会跳过这些 Shell 用例。真实 TLS 用例使用 OpenSSL 生成临时测试证书，仅连接本机临时服务，不读取 NAS 凭据或访问 NAS；缺少 OpenSSL 时仍保留不依赖外部程序的 TLS 回归测试。

2026-10-26：第 1、8、11 项修复的相关回归共 28 项，已在 Linux/WSL 隔离环境通过，涵盖资源校验失败与部署重跑、真实双向 TLS、地址兜底及恢复失败重试。此次验证未部署到 NAS，也未进行真机开机恢复测试。
