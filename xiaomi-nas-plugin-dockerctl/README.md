# xiaomi-nas-plugin-dockerctl

**完整 Docker 控制台** — 小米 NAS 插件，支持容器管理、镜像仓库、一键部署、存储卷与网络管理。

## 6 大模块

| 模块 | 功能 |
|------|------|
| 📊 概览 | 运行/停止/镜像/存储统计，状态分布图 |
| 📦 容器 | 列表、搜索、启停/重启/删除、日志、详情（端口+挂载） |
| 💿 镜像 | 列表、搜索、拉取、删除、一键部署 |
| 🚀 部署 | **表单部署**（端口/挂载/环境变量）+ **Compose YAML 部署** |
| 📁 存储 | 存储卷创建/删除 |
| 🌐 网络 | 网络创建/删除（bridge/macvlan/overlay） |

## 快速开始（本地测试）

```bash
cd src/files
PLUG_PORT=9302 DOCKER_BIN=$(which docker) python3 httpd.py
# 浏览器打开 http://localhost:9302/api/health
```

## 部署到 NAS

详见 [DEPLOY.md](./DEPLOY.md)

## 目录结构

```
├── INFO                    # 插件元数据
├── DEPLOY.md               # 部署指南
├── deploy/
│   └── list-entry.json     # .list 注册条目模板
├── scripts/
│   └── control             # 生命周期钩子
└── src/
    ├── files/
    │   ├── httpd.py        # 后端（全量 Docker API）
    │   └── server.sh
    └── ui/
        ├── index.html      # 主页面（6 模块）
        ├── dockerctl.cgi   # plugin.cgi 入口
        ├── config          # 前端配置
        ├── style.css       # 样式
        └── app.js          # 前端逻辑
```

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| GET | `/api/info` | Docker 版本 |
| GET | `/api/overview` | 统计概览 |
| GET | `/api/containers` | 容器列表 |
| GET | `/api/containers/:id` | 容器详情 |
| GET | `/api/containers/:id/logs` | 容器日志 |
| POST | `/api/containers/:id/{start,stop,restart,remove}` | 容器操作 |
| GET | `/api/images` | 镜像列表 |
| POST | `/api/images/pull` | 拉取镜像 |
| **POST** | **`/api/deploy/run`** | **部署容器** |
| **POST** | **`/api/deploy/compose`** | **部署 Compose** |
| GET/POST | `/api/volumes` | 存储卷管理 |
| GET/POST | `/api/networks` | 网络管理 |

## 技术栈

- **后端**：Python 标准库（http.server），零依赖
- **前端**：原生 HTML/CSS/JS，响应式，桌面端 Electron 兼容
- **Docker**：通过 CLI 调用（适配小米 NAS `/data/docker/docker`）

## License

仅供学习参考。
