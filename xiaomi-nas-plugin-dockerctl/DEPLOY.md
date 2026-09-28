# Docker 控制台插件 — 部署指南

> 插件名：`dockerctl` ｜ 端口：`9302` ｜ 完整 Docker 管理控制台

---

## 功能模块

| 模块 | 功能 |
|------|------|
| **概览** | 运行统计、状态分布、快捷入口 |
| **容器** | 列表/搜索/启动/停止/重启/删除/日志/详情（端口+挂载） |
| **镜像** | 列表/搜索/拉取/删除/一键部署 |
| **部署** | 表单部署（端口/挂载/环境变量/重启策略）+ Compose YAML 部署 |
| **存储** | 存储卷列表/创建/删除 |
| **网络** | 网络列表/创建/删除（bridge/macvlan/overlay） |

---

## API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| GET | `/api/info` | Docker 版本 + 系统信息 |
| GET | `/api/overview` | 统计概览 |
| GET | `/api/containers?all=1` | 容器列表 |
| GET | `/api/containers/:id` | 容器详情 |
| GET | `/api/containers/:id/logs?tail=200` | 容器日志 |
| GET | `/api/containers/:id/stats` | 资源占用 |
| POST | `/api/containers/:id/{start,stop,restart,pause,unpause,kill,remove}` | 容器操作 |
| GET | `/api/images` | 镜像列表 |
| GET | `/api/images/:id` | 镜像详情 |
| POST | `/api/images/pull` | 拉取镜像 |
| POST | `/api/images/:id/remove` | 删除镜像 |
| **POST** | **`/api/deploy/run`** | **部署容器（表单模式）** |
| **POST** | **`/api/deploy/compose`** | **部署 Compose 项目** |
| GET | `/api/volumes` | 存储卷列表 |
| POST | `/api/volumes` | 创建存储卷 |
| POST | `/api/volumes/:name/remove` | 删除存储卷 |
| GET | `/api/networks` | 网络列表 |
| POST | `/api/networks` | 创建网络 |
| POST | `/api/networks/:id/remove` | 删除网络 |

---

## 部署步骤（真机，UID 示例 <NAS_UID>）

### 1. 上传插件

```bash
# 打包本地目录
# tar czf dockerctl.tar.gz -C xiaomi-nas-plugin-dockerctl .

scp dockerctl.tar.gz u<NAS_UID>@<NAS_IP>:/tmp/
ssh u<NAS_UID>@<NAS_IP>

mkdir -p /home/u<NAS_UID>/plugin/dockerctl
tar xzf /tmp/dockerctl.tar.gz -C /home/u<NAS_UID>/plugin/dockerctl

chown -R u<NAS_UID>:u<NAS_UID> /home/u<NAS_UID>/plugin/dockerctl
chmod +x /home/u<NAS_UID>/plugin/dockerctl/scripts/control
chmod +x /home/u<NAS_UID>/plugin/dockerctl/src/ui/dockerctl.cgi
chmod +x /home/u<NAS_UID>/plugin/dockerctl/src/files/server.sh
```

### 2. www 软链 + 图标

```bash
ln -sfn /home/u<NAS_UID>/plugin/dockerctl/src/ui \
        /data/plugin/www/u<NAS_UID>/dockerctl

cp /home/u<NAS_UID>/plugin/dockerctl/src/ui/icon.png \
   /data/plugin/www/icon/dockerctl.icon   # 需自行准备 300×300 PNG

chown -R u<NAS_UID>:u<NAS_UID> \
    /data/plugin/www/u<NAS_UID>/dockerctl \
    /data/plugin/www/icon/dockerctl.icon
```

### 3. 注册 .list

参考 `deploy/list-entry.json`，合并到 `/data/plugin/u<NAS_UID>.list`。
**修改前务必备份！**

### 4. 启动服务

```bash
/home/u<NAS_UID>/plugin/dockerctl/scripts/control postinstall
/home/u<NAS_UID>/plugin/dockerctl/scripts/control start

# 验证
curl -s http://127.0.0.1:9302/api/health
curl -s http://127.0.0.1:9302/api/overview
```

### 5. Nginx 反代（可选，API 直连用）

在 `/etc/nginx/conf.d/luci/plugincenter.conf` **顶部**添加：

```nginx
location ~ ^/plugin/u?<NAS_UID>/dockerctl/api/ {
    rewrite ^/plugin/u?<NAS_UID>/dockerctl/(api/.*)$ /$1 break;
    proxy_pass http://127.0.0.1:9302;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_http_version 1.1;
    add_header Access-Control-Allow-Origin "*" always;
    add_header Cache-Control "no-store";
}
```

`nginx -t && nginx -s reload`

---

## 验证清单

- [ ] `curl -s http://127.0.0.1:9302/api/health` → `{"ok":true,...}`
- [ ] `curl -s http://127.0.0.1:9302/api/overview` → 统计 JSON
- [ ] `curl -s http://127.0.0.1:9302/api/containers` → 容器列表
- [ ] `plugincenter -u u<NAS_UID> list | grep dockerctl` → code=0
- [ ] App 显示「Docker 控制台」图标
- [ ] 手机端 + 桌面端均可正常打开
- [ ] 表单部署容器成功
- [ ] Compose 部署成功
- [ ] 存储卷/网络创建删除成功

---

## 真机环境适配

| 项目 | 说明 |
|------|------|
| Docker CLI | `/data/docker/docker`（`DOCKER_BIN` 可覆盖） |
| Compose 目录 | `/tmp/dockerctl/compose/` |
| 端口 | 9302 |
| 桌面端 | `apiUrl()` 自动剥盘符前缀 |
| 滚动 | `#scroll-wrap` 内层滚动（Electron 兼容） |

---

## 卸载

```bash
/home/u<NAS_UID>/plugin/dockerctl/scripts/control preuninstall
/home/u<NAS_UID>/plugin/dockerctl/scripts/control postuninstall
rm -rf /home/u<NAS_UID>/plugin/dockerctl
rm -f /data/plugin/www/u<NAS_UID>/dockerctl
rm -f /data/plugin/www/icon/dockerctl.icon
# 从 .list 移除 dockerctl 条目
```
