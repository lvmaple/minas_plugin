# 小米 NAS Motrix 插件

小米智能存储上的 Motrix Server 容器与米家插件入口。当前固定使用官方 `ghcr.io/agalwood/motrix-server:2.0.0-beta.40` 镜像，适合本工程实测的 aarch64 NAS。

## 部署

使用 `deploy/package-and-upload.ps1` 打包、上传并在 NAS 上执行部署；也可只打包。部署脚本要求 root SSH、`/data/docker/docker` 和已拉取的官方镜像。Docker Hub 在本机不可达，GHCR 可用。

从 NAS 插件图标打开 Motrix。插件首页直接加载官方 Motrix Web 界面，静态资源由插件提供，RPC 请求经 NAS 的插件地址转发。备用的简易任务页保存在 `manager.html`。插件代理在 NAS 端读取 `/data/motrix-server/data/operator-token` 向 Motrix 认证；用户无需查找或输入它，Operator token 也不会传给浏览器。插件接口使用专用随机凭据限制访问。容器的 8080 端口仅绑定 NAS 本机回环地址，不能从局域网直接访问。

持久数据：`/data/motrix-server/data`。默认下载目录为 `/nas/pool0/u<UID>/data/motrix-downloads`；添加任务或在通用设置中使用 Motrix 自带的服务器文件夹选择器，可改为 `/nas/pool0/u<UID>/data` 内的其他目录。容器仅挂载并允许这个用户的 `data` 目录，Motrix 会拒绝越界路径和符号链接逃逸。插件界面使用小米橙白配色。

本插件使用平台的手动注册方法。`motrix-restore.service` 会在 NAS 重启后恢复插件目录、图标、列表条目、Web 代理配置及仅针对 Motrix 容器当前桥接地址的出站转换规则；`motrix-web-proxy.service` 通过仅 NAS 本地 root 可访问的 Unix socket 转发接口。容器本身由 Docker 的 `unless-stopped` 策略重启。
